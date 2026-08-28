# -*- coding: utf-8 -*-
"""CodeBuddy 计费/签到接口封装。

实测确认（2026-08-27）：
  - POST /v2/billing/meter/get-user-resource : 查询个人账号积分资源包（企业账号返回空）
  - POST /v2/billing/meter/daily-checkin     : 每日签到领积分（仅个人账号，企业账号返回
                                               code 10001「企业账号不支持该操作」）

两个接口都需要完整的企业头（X-Enterprise-Id / X-Tenant-Id / X-Domain），
缺头时返回 500；带全头时正常 200。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

log = logging.getLogger("billing")

BEIJING_TZ = timezone(timedelta(hours=8))
BACKEND = "https://copilot.tencent.com"
ENDPOINT_RESOURCE = BACKEND + "/v2/billing/meter/get-user-resource"
ENDPOINT_CHECKIN = BACKEND + "/v2/billing/meter/daily-checkin"
RESOURCE_BODY = {
    "PageNumber": 1,
    "PageSize": 100,
    "ProductCode": "p_tcaca",
    "Status": [0, 3],
    "OnlyValidPeriod": True,
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeBuddy/3.0.0"


def build_headers(credential) -> dict:
    """由 CredentialManager 构造计费接口需要的完整请求头。"""
    auth = credential._session().get("auth") or {}
    acct = credential._session().get("account") or {}
    eid = auth.get("enterpriseId") or acct.get("enterpriseId", "")
    return {
        "Authorization": "Bearer " + (auth.get("accessToken") or ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-User-Id": acct.get("uid", ""),
        "X-Enterprise-Id": eid,
        "X-Tenant-Id": eid,
        "X-Domain": auth.get("domain", "www.codebuddy.cn"),
        "User-Agent": USER_AGENT,
    }


def query_balance(credential) -> Tuple[Optional[float], str]:
    """查询账号当前可用积分。

    返回 (剩余积分, 提示文本)。企业账号返回 (None, 提示) —— 企业积分不走此接口。
    成功示例 data.Response.Data.Accounts[0].CycleCapacityRemainPrecise。
    """
    try:
        headers = build_headers(credential)
        with httpx.Client(timeout=15) as c:
            r = c.post(ENDPOINT_RESOURCE, headers=headers, json=RESOURCE_BODY)
        if r.status_code >= 400:
            return None, f"查询积分失败 (HTTP {r.status_code})"
        payload = r.json()
    except Exception as e:
        return None, f"查询积分网络失败: {e}"
    if payload.get("code") != 0:
        return None, f"查询积分失败: {payload.get('msg') or payload.get('message') or payload.get('code')}"
    data = (payload.get("data") or {}).get("Response") or {}
    d = data.get("Data") or {}
    accounts = d.get("Accounts") or []
    if not accounts:
        return None, "企业账号无个人积分资源"
    total = 0.0
    for item in accounts:
        if not isinstance(item, dict):
            continue
        for key in ("CycleCapacityRemainPrecise", "CycleCapacityRemain",
                    "CapacityRemainPrecise", "CapacityRemain"):
            if item.get(key) is not None:
                try:
                    total += float(item[key])
                except (TypeError, ValueError):
                    pass
                break
    return total, ""


def daily_checkin(credential) -> Tuple[bool, bool, int, int, str]:
    """执行每日签到。

    返回 (是否成功, 是否今日已签到, 奖励积分, 连续天数, 提示文本)。
    """
    try:
        headers = build_headers(credential)
        with httpx.Client(timeout=15) as c:
            r = c.post(ENDPOINT_CHECKIN, headers=headers, json={})
        body = r.text
        status = r.status_code
    except Exception as e:
        return False, False, 0, 0, f"签到网络失败: {e}"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = {}
    code = payload.get("code") if isinstance(payload, dict) else None
    msg = str((payload.get("msg") if isinstance(payload, dict) else "") or "")
    data = payload.get("data") if isinstance(payload, dict) else None
    awarded = 0
    streak = 0
    if isinstance(data, dict):
        try:
            awarded = int(data.get("credit") or 0)
        except (TypeError, ValueError):
            awarded = 0
        try:
            streak = int(data.get("streak_days") or 0)
        except (TypeError, ValueError):
            streak = 0
    if status == 200 and code in (0, None) and isinstance(data, dict):
        return True, False, awarded, streak, f"签到成功 +{awarded} 积分（连续 {streak} 天）"
    if code == 10001:
        if "企业" in msg or "不支持" in msg:
            return False, False, 0, 0, "企业账号不支持签到"
        if "已签到" in msg or "already" in body.lower():
            return True, True, 0, streak, "今日已签到"
        return False, False, 0, 0, f"签到失败: {msg or '未知错误'}"
    if status == 401:
        return False, False, 0, 0, "令牌失效（HTTP 401），请更新该账号 Token"
    return False, False, 0, 0, f"签到失败（HTTP {status}, code={code}）{msg}"


# ---------------------------------------------------------------------------
# 每日签到调度（后台线程）
# ---------------------------------------------------------------------------

class CheckinScheduler:
    """每天北京时间 00:05 对个人账号执行一次签到；启动后立即可手动触发。"""

    def __init__(self, pool_getter, emit=None):
        self._pool_getter = pool_getter   # () -> AccountPool
        self._emit = emit or (lambda msg, level="info", **kw: None)
        self._lock = threading.Lock()
        self._last_run_day: Optional[str] = None
        self._running = False
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="checkin-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(BEIJING_TZ)
            if now.hour >= 0 and now.minute >= 5:
                self.run_once(force=False)
            # 睡到下一个整点再检查（避免频繁空转）
            self._stop.wait(3600)

    def run_once(self, force: bool = False) -> dict:
        """对全部账号执行一轮签到（仅个人账号真正签到，企业账号跳过）。
        force=True 时忽略"今日已跑过"标记。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        with self._lock:
            if not force and self._last_run_day == today:
                return {"skipped": True, "reason": "今日已执行"}
            self._last_run_day = today
        pool = self._pool_getter()
        if pool is None:
            return {"skipped": True, "reason": "账号池未初始化"}
        results = []
        for acct in list(pool.accounts.values()):
            try:
                s = acct.credential.summary()
            except Exception:
                continue
            nickname = s.get("nickname") or acct.name
            try:
                # 只有 personal 账号签到（企业账号接口直接拒绝，跳过省一次请求）
                sess = acct.credential._session()
                acct_type = (sess.get("account") or {}).get("type", "")
            except Exception:
                acct_type = ""
            if acct_type != "personal":
                results.append({"name": acct.name, "nickname": nickname,
                                "checked": False, "message": "企业账号，跳过"})
                continue
            ok, already, awarded, streak, msg = daily_checkin(acct.credential)
            results.append({"name": acct.name, "nickname": nickname,
                            "checked": ok, "already": already,
                            "awarded": awarded, "streak": streak, "message": msg})
            if ok or already:
                self._emit(f"签到 {nickname} {msg}", level="credit")
            else:
                self._emit(f"签到失败 {nickname} {msg}", level="error")
        return {"skipped": False, "today": today, "results": results}
