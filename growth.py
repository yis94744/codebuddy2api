# -*- coding: utf-8 -*-
"""WorkBuddy 养虾活动（成长计划 / Buddy）接口封装。

实测确认（2026-09-14）：
  养虾记录 = 前端「成长计划 / 养虾记录（Shrimp Journey）」，buddy 即虾（猫形象）。
  接口位于 www.workbuddy.cn / www.codebuddy.cn 的 /activity/growth/* 与 /v2/activity/growth/*。

两个反直觉的调用要点（踩坑记录）：
  1. 大部分接口是 GET，不是 POST —— 用 POST 会 404；
  2. 必须带 X-Client-Platform: web 头 —— 缺头同样 404。

活动规则（实测）：
  - 18 个任务，每个奖励 300 积分 + 5~8 能量；
  - 先要有虾实例（花 10 能量开虾）才能接取后续任务，否则
    accept 返回 prerequisite not met: first_buddy (no buddy instance found)；
  - 任务完成状态由服务端依据真实客户端行为判定，本模块只做「查询 + 领取」，
    不伪造完成状态；
  - 企业账号不参与该活动。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

import httpx

import netenv  # 代理策略：默认直连，不继承宿主环境代理

BACKEND = "https://www.workbuddy.cn"

EP_PROFILE = BACKEND + "/v2/activity/growth/profile"
EP_TASKS = BACKEND + "/v2/activity/growth/tasks"
EP_ENERGY = BACKEND + "/activity/growth/energy"
EP_ACCEPT = BACKEND + "/activity/growth/tasks/accept"
EP_BUDDY_INFO = BACKEND + "/activity/growth/buddy/info"
EP_BUDDY_LIST = BACKEND + "/activity/growth/buddy/list"
EP_BUDDY_QUOTA = BACKEND + "/activity/growth/buddy/quota"
EP_STREAK = BACKEND + "/activity/growth/streak"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeBuddy/3.0.0"

# 开一只虾所需能量（服务端返回 cost_per_open，此处为兜底默认值）
DEFAULT_COST_PER_OPEN = 10


def build_headers(credential) -> dict:
    """构造养虾接口请求头。

    比 billing 多一个 X-Client-Platform: web —— 缺这个头所有接口都返回 404。
    """
    auth = credential._session().get("auth") or {}
    acct = credential._session().get("account") or {}
    eid = auth.get("enterpriseId") or acct.get("enterpriseId", "")
    return {
        "Authorization": "Bearer " + (auth.get("accessToken") or ""),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Client-Platform": "web",
        "X-User-Id": acct.get("uid", ""),
        "X-Enterprise-Id": eid,
        "X-Tenant-Id": eid,
        "X-Domain": auth.get("domain", "www.codebuddy.cn"),
        "User-Agent": USER_AGENT,
    }


def is_enterprise(credential) -> bool:
    """企业账号不参与养虾活动，直接跳过。"""
    try:
        acct = credential._session().get("account") or {}
        return bool(acct.get("enterpriseName") or acct.get("enterpriseId"))
    except Exception:
        return False


def _get(credential, url: str, timeout: float = 20.0) -> Tuple[int, Any]:
    """GET 一次并解析 JSON；失败返回 (状态码, 错误文本)。"""
    try:
        headers = build_headers(credential)
        with netenv.client(timeout) as c:
            r = c.get(url, headers=headers)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _post(credential, url: str, payload: Optional[dict] = None,
          timeout: float = 20.0) -> Tuple[int, Any]:
    """POST 一次并解析 JSON。"""
    try:
        headers = build_headers(credential)
        with netenv.client(timeout) as c:
            r = c.post(url, headers=headers, json=payload or {})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def query_state(credential) -> Dict[str, Any]:
    """汇总养虾状态：等级、能量、任务进度、虾数量。

    返回统一结构，任何一项失败都不影响其它项（值为 None 表示该项查询失败）。
    """
    state: Dict[str, Any] = {
        "ok": False,
        "level": None,
        "level_name": None,
        "tasks_done": None,
        "tasks_total": None,
        "energy": None,
        "cost_per_open": DEFAULT_COST_PER_OPEN,
        "max_open_count": None,
        "buddies": None,
        "claimable": 0,
        "note": "",
    }
    if is_enterprise(credential):
        state["note"] = "企业账号不参与养虾活动"
        return state

    code, prof = _get(credential, EP_PROFILE)
    if code == 200 and isinstance(prof, dict):
        data = prof.get("data") or {}
        state["level"] = data.get("level")
        state["level_name"] = data.get("level_name")
        state["tasks_done"] = data.get("completed")
        state["tasks_total"] = data.get("total")
        state["ok"] = True
    else:
        state["note"] = f"档案查询失败: HTTP {code}"
        return state

    code, en = _get(credential, EP_ENERGY)
    if code == 200 and isinstance(en, dict):
        state["energy"] = (en.get("data") or {}).get("balance")

    code, bq = _get(credential, EP_BUDDY_QUOTA)
    if code == 200 and isinstance(bq, dict):
        d = bq.get("data") or {}
        state["cost_per_open"] = d.get("cost_per_open") or DEFAULT_COST_PER_OPEN
        state["max_open_count"] = d.get("max_open_count")
        # 注意：quota 的 balance 是「可用能量」，不是虾数量，别混用

    # 虾数量以 buddy/list 的 count 为准（buddy/info 只能看单只）
    code, bl = _get(credential, EP_BUDDY_LIST)
    if code == 200 and isinstance(bl, dict):
        d = bl.get("data") or {}
        state["buddies"] = d.get("count")

    code, tk = _get(credential, EP_TASKS)
    if code == 200 and isinstance(tk, dict):
        tasks = (tk.get("data") or {}).get("tasks") or []
        state["claimable"] = sum(1 for t in tasks if _is_claimable(t))
    return state


def _is_claimable(task: dict) -> bool:
    """判断任务是否「已完成但未领取」。"""
    if task.get("accept_status") == "completed":
        return True
    return False


def list_tasks(credential) -> List[dict]:
    """返回任务列表（原始结构）。"""
    code, tk = _get(credential, EP_TASKS)
    if code != 200 or not isinstance(tk, dict):
        return []
    return (tk.get("data") or {}).get("tasks") or []


def claim_task(credential, task_code: str) -> Tuple[bool, int, int, str]:
    """领取单个任务奖励。

    返回 (是否成功, 获得积分, 获得能量, 提示文本)。
    """
    url = f"{BACKEND}/activity/growth/tasks/{task_code}/claim"
    code, res = _post(credential, url)
    if code != 200 or not isinstance(res, dict):
        return False, 0, 0, f"领取失败 (HTTP {code}) {str(res)[:80]}"
    if res.get("code") != 0:
        return False, 0, 0, str(res.get("msg") or "领取失败")[:80]
    data = res.get("data") or {}
    credit = int(data.get("credit") or 0)
    energy = int(data.get("energy") or 0)
    if data.get("already_claimed"):
        return True, 0, 0, "已领取过"
    return True, credit, energy, f"领取成功 +{credit} 积分 +{energy} 能量"


def accept_all(credential) -> Tuple[int, List[str]]:
    """批量接取该账号所有未接取的任务。

    接取只是登记意向，不会产生消耗；接取后客户端里的真实操作才会被计入进度。
    返回 (成功接取数, 明细列表)。
    """
    tasks = list_tasks(credential)
    pending = [t.get("task_code") for t in tasks
               if t.get("accept_status") == "not_accepted" and t.get("task_code")]
    if not pending:
        return 0, []
    code, res = _post(credential, EP_ACCEPT, {"task_codes": pending})
    if code != 200 or not isinstance(res, dict):
        return 0, [f"批量接取失败 (HTTP {code})"]
    if res.get("code") != 0:
        return 0, [f"批量接取失败: {res.get('msg') or ''}"]
    ok_count = 0
    details: List[str] = []
    for item in (res.get("data") or {}).get("results") or []:
        tc = item.get("task_code")
        st = item.get("status")
        if st == "accepted":
            ok_count += 1
        else:
            details.append(f"{tc}: {st} {item.get('message') or ''}".strip())
    return ok_count, details


def run_for_pool(pool, *, do_accept: bool = True, do_claim: bool = True):
    """对账号池中所有「个人账号」执行一轮养虾操作（企业账号自动跳过）。

    返回逐账号结果列表，供面板与日志展示。
    """
    results = []
    if pool is None:
        return results
    for acct in list(pool.accounts.values()):
        try:
            s = acct.credential.summary()
        except Exception:
            continue
        nickname = s.get("nickname") or acct.name
        base = {"name": acct.name, "nickname": nickname}
        if is_enterprise(acct.credential):
            results.append({**base, "skipped": True, "message": "企业账号，跳过"})
            continue
        try:
            accepted = 0
            if do_accept:
                accepted, _ = accept_all(acct.credential)
            claimed, credit, details = (0, 0, [])
            if do_claim:
                claimed, credit, details = claim_all(acct.credential)
            results.append({
                **base, "skipped": False,
                "accepted": accepted, "claimed": claimed, "credit": credit,
                "message": (f"接取 {accepted} 个任务，领取 {claimed} 项 +{credit} 积分"
                            if (accepted or claimed) else "无可操作项"),
                "details": details[:8],
            })
        except Exception as e:
            results.append({**base, "skipped": False, "error": f"{type(e).__name__}: {e}"})
    return results


class GrowthScheduler:
    """养虾巡检调度器：定时接取任务 + 领取已完成奖励。

    与签到不同，养虾奖励的产生依赖用户在客户端的真实操作，所以这里采用
    「定时轮询」而非「每天一次」：只要你在客户端做了任务，下一次巡检就会
    把奖励领掉。默认每 30 分钟检查一次。
    """

    INTERVAL_SECONDS = 1800

    def __init__(self, pool_getter, emit=None):
        self._pool_getter = pool_getter
        self._emit = emit or (lambda msg, level="info", **kw: None)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="growth-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 启动后先等一轮，避免与启动期的余额刷新等操作抢资源
        if self._stop.wait(120):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                self._emit(f"养虾巡检异常：{type(e).__name__}: {e}", level="error")
            self._stop.wait(self.INTERVAL_SECONDS)

    def run_once(self) -> dict:
        """执行一轮养虾巡检（接取 + 领奖）。"""
        with self._lock:
            pool = self._pool_getter()
            results = run_for_pool(pool)
        for r in results:
            if r.get("skipped") or r.get("error"):
                continue
            if r.get("claimed"):
                self._emit(f"养虾 {r['nickname']} 领取 {r['claimed']} 项奖励 +{r['credit']} 积分",
                           level="credit")
            elif r.get("accepted"):
                self._emit(f"养虾 {r['nickname']} 接取 {r['accepted']} 个任务", level="info")
        return {"results": results}


def claim_all(credential, limit: int = 20) -> Tuple[int, int, List[str]]:
    """领取该账号所有「已完成未领取」的任务奖励。

    返回 (领取条数, 获得积分总数, 明细列表)。
    """
    tasks = list_tasks(credential)
    got_count = 0
    got_credit = 0
    details: List[str] = []
    for t in tasks:
        if got_count >= limit:
            break
        if not _is_claimable(t):
            continue
        code_name = t.get("task_code")
        title = t.get("title") or code_name
        ok, credit, energy, msg = claim_task(credential, code_name)
        if ok and credit > 0:
            got_count += 1
            got_credit += credit
            details.append(f"{title} +{credit}分")
        elif ok:
            details.append(f"{title}（{msg}）")
        else:
            details.append(f"{title} 失败：{msg}")
    return got_count, got_credit, details
