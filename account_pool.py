# -*- coding: utf-8 -*-
"""账号池：管理本机多个 CodeBuddy/WorkBuddy 登录凭据，支持一键切换与路由。

解决原版"只读第一个 .info 文件、切号需改文件名"的痛点：
  - 自动扫描 auth 目录下所有 *.info 文件
  - 每个账号持有一个 CredentialManager（复用原版的读/刷新/回写逻辑）
  - 支持三种路由策略：
      fixed    固定账号（不自动切换）
      auto     轮询（在可用账号间均摊流量）
      failover 自动顶上（默认）：当前账号积分/额度耗尽、被限流或鉴权失败时，
               自动标记该账号并切换到下一个可用账号重试，实现"积分空了其他号顶上"
  - 账号健康状态跟踪：积分耗尽 / 冷却中 / 正常，冷却到期自动恢复参与路由
  - 检测 token 实际有效性（本地过期判断 + 可选的连通性测试）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

# 各错误类别的冷却时长（秒）。冷却期内的账号不参与路由，到期自动恢复。
COOLDOWN_QUOTA = 600     # 积分/额度耗尽：10 分钟（每日额度场景足够让额度刷新后自动顶上）
COOLDOWN_RATE = 60       # 限流：1 分钟
COOLDOWN_AUTH = 30       # 鉴权失败：30 秒
COOLDOWN_OTHER = 30      # 其他错误：30 秒


class Account:
    """单个登录账号。"""

    def __init__(self, path: Path, name: Optional[str] = None):
        from converter import CredentialManager  # 延迟导入，避免循环依赖
        self.id = uuid.uuid4().hex[:12]
        self.path = Path(path)
        self.name = name or self.path.stem
        self.enabled = True
        self.credential = CredentialManager(self.path)
        self.last_used_at: float = 0.0
        self._check_error: Optional[str] = None
        # -- 健康/故障转移状态 -------------------------------------------
        self._lock = threading.Lock()
        self.quota_exhausted = False          # 是否被标记为积分/额度耗尽
        self.cooldown_until: float = 0.0      # 冷却截止（unix 秒），0 表示无冷却
        self.fail_count: int = 0              # 连续失败次数
        self.last_error: Optional[str] = None # 最近一次失败原因
        self.last_error_at: float = 0.0
        self.last_success_at: float = 0.0
        # -- 积分台账（本次运行累计；上游无余额接口，无法显示绝对剩余） --
        self.credit_spent: float = 0.0
        # -- 真实余额（个人账号经 /billing/meter/get-user-resource 查询，企业账号为空） --
        self.real_credit: Optional[float] = None
        self.real_credit_at: float = 0.0
        self.real_credit_note: str = ""

    # -- 信息 ------------------------------------------------------------
    def summary(self) -> dict:
        """脱敏摘要，供 UI 展示。"""
        try:
            s = self.credential.summary()
        except Exception as e:
            s = {"uid": None, "nickname": None, "enterpriseName": None,
                 "token_expires_at": 0, "token_expired": True}
            self._check_error = str(e)
        token = self._mask_token()
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "file": self.path.name,
            "enabled": self.enabled,
            "uid": s.get("uid"),
            "nickname": s.get("nickname"),
            "enterprise_name": s.get("enterpriseName"),
            "token_expires_at": s.get("token_expires_at") or 0,
            "token_expired": bool(s.get("token_expired")),
            "token_prefix": token,
            "last_used_at": self.last_used_at,
            "check_error": self._check_error,
            # 健康/故障转移状态
            "health": self.health(),
            "available": self.is_available(),
            "quota_exhausted": self.quota_exhausted,
            "cooldown_until": self.cooldown_until,
            "fail_count": self.fail_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_success_at": self.last_success_at,
            "credit_spent": round(self.credit_spent, 4),
            "real_credit": (round(self.real_credit, 2)
                            if self.real_credit is not None else None),
            "real_credit_at": self.real_credit_at,
            "real_credit_note": self.real_credit_note,
        }

    def refresh_real_credit(self) -> Optional[float]:
        """调用上游查询真实积分余额（仅个人账号有数据）。

        成功则更新缓存并返回余额；失败保留旧值并记录提示。线程安全。
        """
        try:
            from billing import query_balance
            value, note = query_balance(self.credential)
            with self._lock:
                self.real_credit_at = time.time()
                self.real_credit_note = note
                if value is not None:
                    self.real_credit = value
                return self.real_credit
        except Exception as e:
            with self._lock:
                self.real_credit_note = f"查询异常: {e}"
            return self.real_credit

    def add_credit(self, c: float) -> float:
        """累加一次积分消耗，返回累计值（线程安全）。

        若已知道真实余额，同时做一次本地即时扣减（供面板实时反映这次消耗）。
        该值仅为过渡显示，随后会由上游真实查询（refresh_real_credit）校正为绝对真实。
        """
        try:
            c = float(c or 0)
        except (TypeError, ValueError):
            c = 0.0
        with self._lock:
            if c > 0:
                self.credit_spent += c
                # 本地乐观扣减真实余额，让面板立即响应消耗（真实值在异步刷新后校正）
                if self.real_credit is not None:
                    self.real_credit = max(0.0, self.real_credit - c)
            return self.credit_spent

    def _mask_token(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tok = (data.get("auth") or {}).get("accessToken") or ""
            if not tok:
                return ""
            if len(tok) <= 12:
                return "*" * len(tok)
            return tok[:6] + "****" + tok[-6:]
        except Exception:
            return ""

    # -- 状态检查 --------------------------------------------------------
    def check(self) -> dict:
        """检查凭据有效性：本地过期判断 + 网络连通性测试。

        返回 {status: ok/warn/error, detail}。
        warn: 本地未过期但网络校验失败（token 可能已被后端撤销）
        """
        try:
            summary = self.credential.summary()
        except Exception as e:
            self._check_error = str(e)
            return {"status": "error", "detail": f"读取凭据失败: {e}"}
        if summary.get("token_expired"):
            self._check_error = "token 已过期（本地判定）"
            return {"status": "error", "detail": "token 已过期（本地判定）"}

        # 网络校验：直接调一次后端（极小 max_tokens），看鉴权是否通过
        try:
            from converter import BACKEND  # 延迟导入，避免循环依赖
            headers = self.credential.get_headers()
            body = {"model": "auto",
                    "messages": [{"role": "user", "content": "ping"}],
                    "stream": True, "max_tokens": 1}
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{BACKEND}/v2/chat/completions", headers=headers, json=body)
            if r.status_code == 200:
                self._check_error = None
                return {"status": "ok", "detail": "凭据有效，后端鉴权通过"}
            self._check_error = f"后端拒绝 (HTTP {r.status_code})"
            return {"status": "warn", "detail": f"后端拒绝 (HTTP {r.status_code})，token 可能已被撤销"}
        except Exception as e:
            self._check_error = f"网络异常: {e}"
            return {"status": "warn", "detail": f"网络异常: {e}"}

    # -- 健康状态 / 故障转移 ----------------------------------------------
    def is_available(self, now: Optional[float] = None) -> bool:
        """是否可参与路由：enabled 且未在冷却期 且 token 未过期。"""
        if not self.enabled:
            return False
        now = now or time.time()
        with self._lock:
            if self.cooldown_until and now < self.cooldown_until:
                return False
        try:
            if self.credential.summary().get("token_expired"):
                return False
        except Exception:
            pass
        return True

    def health(self) -> str:
        """健康状态: ok / exhausted(积分耗尽冷却中) / cooldown(冷却中) / disabled。"""
        if not self.enabled:
            return "disabled"
        now = time.time()
        with self._lock:
            cooling = bool(self.cooldown_until and now < self.cooldown_until)
            exhausted = self.quota_exhausted
        if cooling:
            return "exhausted" if exhausted else "cooldown"
        return "ok"

    def mark_fail(self, reason: str = "", *, kind: str = "other",
                  cooldown: Optional[float] = None) -> None:
        """失败标记：累加失败次数、设置冷却；积分耗尽类错误同时打标记。

        kind: quota(积分/额度) / rate(限流) / auth(鉴权) / other
        """
        if cooldown is None:
            cooldown = {
                "quota": COOLDOWN_QUOTA,
                "rate": COOLDOWN_RATE,
                "auth": COOLDOWN_AUTH,
                "other": COOLDOWN_OTHER,
            }.get(kind, COOLDOWN_OTHER)
        with self._lock:
            self.fail_count += 1
            self.last_error = reason or f"upstream error ({kind})"
            self.last_error_at = time.time()
            self.cooldown_until = time.time() + cooldown
            if kind == "quota":
                self.quota_exhausted = True

    def mark_success(self) -> None:
        """成功标记：清零失败计数并恢复健康。"""
        with self._lock:
            self.fail_count = 0
            self.last_error = None
            self.last_success_at = time.time()
            self.cooldown_until = 0.0
            self.quota_exhausted = False

    def mark_recovered(self) -> None:
        """手动恢复（解除耗尽/冷却标记）。"""
        with self._lock:
            self.fail_count = 0
            self.last_error = None
            self.cooldown_until = 0.0
            self.quota_exhausted = False


class AccountPool:
    """账号池：扫描/切换/路由。"""

    def __init__(self, search_dirs: Optional[list[Path]] = None,
                 strategy: str = "fixed"):
        from converter import auth_dirs  # 延迟导入，避免循环依赖
        # RLock：scan()/add() 等方法持锁时可能再调用 summary()，普通 Lock 会自锁死
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.search_dirs = search_dirs or auth_dirs()
        self.strategy = strategy          # fixed | auto
        self.active_id: Optional[str] = None
        self._rr_index = 0
        self.scan()

    # -- 扫描 ------------------------------------------------------------
    def scan(self) -> dict:
        """扫描所有 auth 目录下的 *.info，发现新账号/移除失效账号。"""
        with self._lock:
            found: list[Path] = []
            for d in self.search_dirs:
                if not d.is_dir():
                    continue
                for f in sorted(d.glob("*.info")):
                    found.append(f)
            # 移除已不存在的账号
            for aid in list(self.accounts.keys()):
                acct = self.accounts[aid]
                if acct.path not in found:
                    if self.active_id == aid:
                        self.active_id = None
                    del self.accounts[aid]
            # 新增账号（按文件路径去重）
            for f in found:
                if not any(a.path == f for a in self.accounts.values()):
                    acct = Account(f)
                    self.accounts[acct.id] = acct
            # 确保有 active 账号
            if self.active_id is None or self.active_id not in self.accounts:
                self.active_id = next(iter(self.accounts.keys()), None)
            return self.summary()

    def add(self, path: str, name: Optional[str] = None) -> dict:
        """手动添加一个账号（按 .info 文件路径）。"""
        p = Path(path)
        if not p.exists():
            raise ValueError(f"文件不存在: {path}")
        with self._lock:
            for a in self.accounts.values():
                if a.path == p:
                    return a.summary()
            acct = Account(p, name)
            self.accounts[acct.id] = acct
            if self.active_id is None:
                self.active_id = acct.id
            return acct.summary()

    def rename(self, account_id: str, name: str) -> dict:
        with self._lock:
            acct = self._get(account_id)
            acct.name = name.strip() or acct.path.stem
            return acct.summary()

    def set_enabled(self, account_id: str, enabled: bool) -> dict:
        with self._lock:
            acct = self._get(account_id)
            acct.enabled = enabled
            return acct.summary()

    def switch(self, account_id: str) -> dict:
        """切换当前账号。"""
        with self._lock:
            acct = self._get(account_id)
            self.active_id = acct.id
            return acct.summary()

    def set_strategy(self, strategy: str) -> None:
        if strategy not in ("fixed", "auto", "failover"):
            raise ValueError(f"未知策略: {strategy}")
        self.strategy = strategy

    # -- 路由 ------------------------------------------------------------
    def resolve(self) -> Optional[CredentialManager]:
        """返回当前应使用的 CredentialManager（供协议层调用，向后兼容）。"""
        acct = self.resolve_account()
        return acct.credential if acct is not None else None

    def resolve_account(self) -> Optional[Account]:
        """路由到本次请求应使用的账号（failover 策略下自动跳过不可用账号）。"""
        with self._lock:
            if not self.accounts:
                return None
            if self.strategy == "failover":
                acct = self._pick_failover()
            elif self.strategy == "auto":
                acct = self._pick_round_robin()
            else:
                acct = self._pick_fixed()
            if acct is not None:
                acct.last_used_at = time.time()
            return acct

    def _pick_failover(self) -> Optional[Account]:
        """failover：从当前 active 账号开始，找第一个健康可用账号；
        找到后把 active 推进到该账号，实现"积分空了自动顶上"。"""
        now = time.time()
        keys = list(self.accounts.keys())
        if self.active_id in self.accounts:
            idx = keys.index(self.active_id)
            ordered = keys[idx:] + keys[:idx]
        else:
            ordered = keys
        for aid in ordered:
            a = self.accounts[aid]
            if a.enabled and a.is_available(now):
                self.active_id = aid
                return a
        return None

    def _pick_round_robin(self) -> Optional[Account]:
        """auto：在健康可用账号间轮询，均摊流量（冷却中/耗尽的自动跳过）。"""
        pool_list = [a for a in self.accounts.values() if a.enabled and a.is_available()]
        if not pool_list:
            pool_list = [a for a in self.accounts.values() if a.enabled]
        if not pool_list:
            return None
        self._rr_index %= len(pool_list)
        acct = pool_list[self._rr_index]
        self._rr_index += 1
        return acct

    def _pick_fixed(self) -> Optional[Account]:
        """fixed：严格固定 active 账号（不因健康状态自动切换）。"""
        acct = self.accounts.get(self.active_id)
        if acct is not None and acct.enabled:
            return acct
        for a in self.accounts.values():
            if a.enabled:
                return a
        return None

    def next_available(self, exclude_id: Optional[str] = None) -> Optional[Account]:
        """返回下一个可用账号（供故障转移时切换）；若全部不可用，
        回退到任意 enabled 账号作为最后手段。"""
        with self._lock:
            now = time.time()
            keys = list(self.accounts.keys())
            if self.active_id in self.accounts:
                idx = keys.index(self.active_id)
                ordered = keys[idx:] + keys[:idx]
            else:
                ordered = keys
            for aid in ordered:
                if aid == exclude_id:
                    continue
                a = self.accounts[aid]
                if a.enabled and a.is_available(now):
                    if self.strategy != "auto":
                        self.active_id = aid
                    return a
            for aid in keys:
                if aid == exclude_id:
                    continue
                a = self.accounts[aid]
                if a.enabled:
                    if self.strategy != "auto":
                        self.active_id = aid
                    return a
            return None

    # -- 故障转移标记（由协议层在请求失败/成功后调用） --------------------
    def mark_fail(self, account_id: str, reason: str = "", *, kind: str = "other") -> None:
        with self._lock:
            acct = self.accounts.get(account_id)
            if acct is not None:
                acct.mark_fail(reason, kind=kind)

    def mark_success(self, account_id: str) -> None:
        with self._lock:
            acct = self.accounts.get(account_id)
            if acct is not None:
                acct.mark_success()

    def mark_recovered(self, account_id: str) -> None:
        with self._lock:
            acct = self._get(account_id)
            acct.mark_recovered()

    # -- 真实余额实时刷新（节流，让面板反映最新/最真实积分） --------------
    def schedule_balance_refresh(self, account_id: str, *, force: bool = False,
                                 min_interval: float = 2.0) -> bool:
        """节流地调度一次真实余额刷新（后台线程，不阻塞调用方）。

        force=True 忽略节流强制刷新；返回 True 表示已调度，False 表示被节流跳过。
        用于在每次请求消耗积分后，异步把余额校正为上游绝对真实值。
        """
        with self._lock:
            acct = self.accounts.get(account_id)
            if acct is None:
                return False
            # 同一账号距上次真实查询过近则合并（避免高频请求打爆计费接口）
            if not force and (time.time() - acct.real_credit_at) < min_interval:
                return False
        threading.Thread(target=acct.refresh_real_credit, daemon=True).start()
        return True

    def active(self) -> Optional[Account]:
        with self._lock:
            return self.accounts.get(self.active_id)

    def _get(self, account_id: str) -> Account:
        acct = self.accounts.get(account_id)
        if acct is None:
            raise KeyError(f"账号不存在: {account_id}")
        return acct

    # -- 汇总 ------------------------------------------------------------
    def summary(self) -> dict:
        with self._lock:
            accts = [a.summary() for a in self.accounts.values()]
            active = self.accounts.get(self.active_id)
            # 健康统计
            stats = {"total": 0, "ok": 0, "exhausted": 0, "cooldown": 0, "disabled": 0}
            for a in self.accounts.values():
                stats["total"] += 1
                h = a.health()
                if h in stats:
                    stats[h] += 1
            return {
                "strategy": self.strategy,
                "active_id": self.active_id,
                "active_name": active.name if active else None,
                "accounts": accts,
                "search_dirs": [str(d) for d in self.search_dirs],
                "pool_stats": stats,
            }
