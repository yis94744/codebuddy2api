#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# 以 `python converter.py` 方式运行时，__main__ 与模块名 "converter" 实为同一对象。
# 若不注册，account_pool / ui_admin 里的 `import converter` 会重复导入一份新模块，
# 导致其 CONFIG（含 pool 等运行期状态）与主模块分裂（典型症状：/api/accounts 报 503）。
if __name__ == "__main__":
    sys.modules.setdefault("converter", sys.modules["__main__"])

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from responses_projection import project_responses_chat_body
from anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)
from account_pool import AccountPool

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                return f
    return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m3-pay", "hy3-preview-agent", "auto",
]

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None,
                "desensitize": False, "no_compact": False,
                "pool": None, "started_at": time.time(),
                "host": "127.0.0.1", "port": 8787}  # pool: AccountPool | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。
    同时推送到 UI 日志总线（内存缓冲 + WebSocket 广播）。"""
    path = CONFIG.get("log_path")
    if path:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        try:
            with _LOG_LOCK:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass  # 日志失败不应影响主流程
    # UI 日志总线
    try:
        from ui_admin import LOG_BUS
        if "审核拦截" in msg or "content-filter" in msg:
            level = "warn"
        elif "✗" in msg or "错误" in msg or "失败" in msg or "invalid" in msg:
            level = "error"
        elif "💰" in msg or "▶" in msg or "◀" in msg or "↻" in msg:
            level = "info"
        else:
            level = "info"
        LOG_BUS.emit(msg, level=level)
    except Exception:
        pass  # UI 模块缺失或未初始化时不影响主流程


def _verbose() -> bool:
    return bool(CONFIG.get("verbose"))


def _log_v(build):
    """仅 verbose 模式记录；build 为无参函数，避免非 verbose 时构造大字符串。"""
    if _verbose():
        try:
            _log(build())
        except Exception:
            pass




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred() -> CredentialManager:
    """返回当前路由到的账号凭据：优先用账号池，兼容旧的单一 cred。"""
    pool = CONFIG.get("pool")
    if pool is not None:
        cred = pool.resolve()
        if cred is not None:
            return cred
    if CONFIG["cred"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["cred"]


# ---------------------------------------------------------------------------
# 故障转移：积分耗尽 / 限流 / 鉴权失败 时自动切换账号顶上
# ---------------------------------------------------------------------------

# 触发自动切换账号的错误类别
RETRY_KINDS = ("quota", "rate", "auth")

# 命中即判定为"积分/额度不足"的关键词（后端错误体里常见）
QUOTA_KEYWORDS = (
    "quota", "balance", "insufficient", "credit", "exhaust", "free.quota",
    "额度", "积分", "余额", "点数", "不足", "用尽", "耗尽", "已用完", "无可用",
)
RATE_KEYWORDS = ("rate.limit", "too many", "频繁", "限流", "请求过快")


def _classify_upstream_error(status: int, text: str = "") -> str:
    """把上游错误分类：quota(积分/额度) / rate(限流) / auth(鉴权) / other / 空(非错误)。"""
    low = (text or "").lower()
    if status == 402:
        return "quota"
    if status == 429:
        return "rate"
    if status == 401:
        return "auth"
    if status >= 400:
        if any(kw in low for kw in QUOTA_KEYWORDS):
            return "quota"
        if any(kw in low for kw in RATE_KEYWORDS):
            return "rate"
        return "other"
    return ""


class _SingleAccount:
    """单账号模式的 Account 兼容包装，让故障转移逻辑统一工作（pool 为空时使用）。"""
    __slots__ = ("id", "name", "credential")

    def __init__(self, cred: CredentialManager):
        self.id = ""
        self.name = "default"
        self.credential = cred


def _route_account():
    """路由到本次请求应使用的账号（failover 策略下自动跳过不可用账号）。

    返回对象需具备 .id / .name / .credential（Account 或 _SingleAccount）。
    """
    pool = CONFIG.get("pool")
    if pool is not None:
        acct = pool.resolve_account()
        if acct is not None:
            return acct
    if CONFIG["cred"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return _SingleAccount(CONFIG["cred"])


async def _with_failover(account, func):
    """以账号为粒度执行 func(account) -> (status, result)。

    当上游返回可切换错误（积分耗尽/限流/鉴权失败）时，标记当前账号为失败
    并自动切换到下一个可用账号重试，实现"这个账号积分空了其他账号顶上"。

    返回最终 (status, result, account)。result 在 status == 200 时由 func 决定类型，
    否则为 bytes（上游错误体）。
    """
    pool = CONFIG.get("pool")
    if pool is None or len(pool.accounts) <= 1:
        status, result = await func(account)
        return status, result, account
    cur = account
    attempts = len(pool.accounts)
    for i in range(attempts):
        status, result = await func(cur)
        if status == 200:
            pool.mark_success(cur.id)
            return status, result, cur
        text = result.decode("utf-8", "replace") if isinstance(result, bytes) else ""
        kind = _classify_upstream_error(status, text)
        if kind not in RETRY_KINDS or i == attempts - 1:
            return status, result, cur
        pool.mark_fail(cur.id, reason=f"HTTP {status} {_truncate(text, 160)}", kind=kind)
        nxt = pool.next_available(exclude_id=cur.id)
        if nxt is None:
            return status, result, cur
        _log(f"[failover] ↻ 切换账号 {cur.name} → {nxt.name}（{kind}，第 {i + 2}/{attempts} 次尝试）")
        cur = nxt
    return status, result, cur


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_file": str(find_auth_file() or "(未找到)"), "mode": "direct-proxy (native function calling)"}
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    return info


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in DEFAULT_MODELS]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    account = _route_account()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    if _verbose():
        _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
             + (f" | tools={tool_names}" if tool_names else "")
             + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）——仅 verbose
    _log_v(lambda: f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(account, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应；
    # 若账号积分耗尽/被限流/鉴权失败，自动切换账号顶上重试
    try:
        status, result, account = await _with_failover(
            account, lambda a: _post_chat_once(a, body, rid, model_name))
        if status != 200:
            _log(f"[{rid}] ✗ HTTP {status} | {model_name} | {_truncate(result.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status, detail=_safe_err_raw(result, status))
        collected = result
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
    _log_finish(model_name, t0, collected, rid, account=account)
    return JSONResponse(content=collected)


async def _post_chat_once(account, body, rid="", model_name="?"):
    """用指定账号向后端转发一次（后端仅支持流式，这里聚合返回）。

    返回 (status, result)：status != 200 时 result 为错误体 bytes。
    """
    headers = account.credential.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    async with httpx.AsyncClient(timeout=300) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code != 200:
                raw = await r.aread()
                _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                _log_v(lambda: f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                return r.status_code, raw
            collected = await _collect_stream(r)
            return 200, collected


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _fmt_c(c: float) -> str:
    s = ("%.4f" % c).rstrip("0").rstrip(".")
    return s or "0"


def _log_bill(usage: dict, model: str, rid: str = "", account=None,
              stream: bool = False):
    """一行账单式完成日志：模型 / token 明细 / 积分 / 扣费账号 / 本次运行累计。

    同时把 credit 记入 UI 统计总线（today_credit 聚合的数据源）。
    """
    usage = usage or {}
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    tt = usage.get("total_tokens", (pt or 0) + (ct or 0))
    try:
        c = float(usage.get("credit") or 0)
    except (TypeError, ValueError):
        c = 0.0
    spent = 0.0
    if account is not None and c > 0:
        try:
            spent = account.add_credit(c)
        except Exception:
            spent = 0.0
    name = getattr(account, "name", "") or "?"
    toks = f"{pt}+{ct}={tt}" if tt != "?" else "?"
    line = (f"💰 [{rid}] {model} | tok {toks} | {c:.4f}分"
            f" | 账号:{name} | 累计:{_fmt_c(spent)}"
            + (" | 流式" if stream else ""))
    # 文件日志
    path = CONFIG.get("log_path")
    if path:
        try:
            with _LOG_LOCK:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
        except OSError:
            pass
    # UI 总线（携带 credit 数值供 today_credit 聚合）
    try:
        from ui_admin import LOG_BUS
        LOG_BUS.emit(line, level="credit", rid=rid, model=model, credit=c)
    except Exception:
        pass


def _report_credit(usage: dict, model: str = "", rid: str = "", account=None,
                   stream: bool = False):
    """兼容入口：统一走账单行（verbose 模式额外补完整 usage 上下文）。"""
    _log_bill(usage, model, rid=rid, account=account, stream=stream)
    if _verbose():
        _log_v(lambda: f"[{rid}] ── USAGE ──\n{json.dumps(usage, ensure_ascii=False)}")


def _log_finish(model_name: str, t0: float, result: dict, rid: str = "",
                account=None):
    """记录一次完成的请求：账单一行；完整响应体仅 verbose 模式记录。"""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = " ⚠️内容审核拦截" if finish == "content-filter" else ""
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    extra = (f" | tool_calls={tc_names}" if tc_names else "") + tag
    # 完整响应体（verbose）
    _log_v(lambda: f"[{rid}] ── RESPONSE BODY{extra} ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    _report_credit(usage, model_name, rid=rid, account=account)


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(account, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    若上游返回可切换错误（积分耗尽/限流/鉴权失败），自动标记账号并切换重试。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""
    pool = CONFIG.get("pool")
    attempts = len(pool.accounts) if pool is not None and pool.accounts else 1
    url = f"{BACKEND}/v2/chat/completions"
    cur = account
    tried = 0

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    while True:
        headers = cur.credential.get_headers()
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        text = err.decode("utf-8", "replace")
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(text,200)}")
                        kind = _classify_upstream_error(r.status_code, text)
                        tried += 1
                        if kind in RETRY_KINDS and tried < attempts and pool is not None:
                            pool.mark_fail(cur.id, reason=f"HTTP {r.status_code} {_truncate(text,160)}", kind=kind)
                            nxt = pool.next_available(exclude_id=cur.id)
                            if nxt is not None:
                                _log(f"{prefix}↻ 切换账号 {cur.name} → {nxt.name}（{kind}，第 {tried+1}/{attempts} 次尝试）")
                                cur = nxt
                                continue
                        _log_v(lambda t=text: f"{prefix}── ERROR BODY ──\n{t}")
                        yield _err_event(err, r.status_code)
                        return
                    if pool is not None:
                        pool.mark_success(cur.id)
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            raw_parts.append(chunk)
                            _feed(chunk)
                            yield chunk
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            yield _err_event(str(e).encode(), 502)
        break

    # 流结束：账单一行（verbose 补充原始 SSE 与完成摘要）
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    if _verbose():
        elapsed = time.time() - t0 if t0 else 0
        tc_n = tool_names or []
        _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
             + (f" | tool_calls={tc_n}" if tc_n else "")
             + f" | tokens={(usage or {}).get('total_tokens', '?')}")
        _log_v(lambda: f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")
        if tag:
            _log(f"{prefix}◀ 流式响应被内容审核拦截")
    _report_credit(usage or {}, model_name, rid, account=account, stream=True)


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(url: str, account, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    """用指定账号请求后端；命中内容审核且未 compact 时，同账号 compact 重试一次。"""
    prefix = f"[{rid}] " if rid else ""
    headers = account.credential.get_headers()
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        _log_v(lambda: f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    account = _route_account()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    if _verbose():
        _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
        _log(
            f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log_v(lambda: f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(account, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象；账号积分耗尽/限流/鉴权失败自动切换
    try:
        status_code, raw, account = await _with_failover(
            account, lambda a: _post_responses_once(a, chat_body, rid, model_name))
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    if _verbose():
        elapsed = time.time() - t0
        _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
        _log_v(lambda: f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    _report_credit((result.get("usage") or {}), model_name, rid, account=account)
    return JSONResponse(content=result)


async def _post_responses_once(account, body: dict, rid: str = "", model_name: str = "?"):
    """用指定账号按 Responses 流程转发一次，返回 (status, raw)。"""
    url = f"{BACKEND}/v2/chat/completions"
    status, raw, _ = await _post_backend_with_filter_retry(url, account, body, rid, model_name)
    return status, raw


async def _stream_responses(account, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。

    若账号积分耗尽/被限流/鉴权失败，自动标记并切换账号顶上重试。
    """
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""
    pool = CONFIG.get("pool")
    attempts = len(pool.accounts) if pool is not None and pool.accounts else 1
    url = f"{BACKEND}/v2/chat/completions"
    cur = account
    tried = 0

    while True:
        try:
            status_code, raw, _ = await _post_backend_with_filter_retry(url, cur, body, rid, model_name)
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        if status_code != 200:
            text = raw.decode("utf-8", "replace")
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(text,200)}")
            kind = _classify_upstream_error(status_code, text)
            tried += 1
            if kind in RETRY_KINDS and tried < attempts and pool is not None:
                pool.mark_fail(cur.id, reason=f"HTTP {status_code} {_truncate(text,160)}", kind=kind)
                nxt = pool.next_available(exclude_id=cur.id)
                if nxt is not None:
                    converter = ResponsesStreamConverter(model=model_name)  # 重置转换器状态
                    _log(f"{prefix}↻ 切换账号 {cur.name} → {nxt.name}（{kind}，第 {tried+1}/{attempts} 次尝试）")
                    cur = nxt
                    continue
            error_evt = {"type": "error", "error": {"message": text[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        if pool is not None:
            pool.mark_success(cur.id)
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
        break

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    if _verbose():
        elapsed = time.time() - t0 if t0 else 0
        _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
        _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))
    raw_usage = getattr(converter, "_usage", None)
    _report_credit(raw_usage or {}, model_name, rid, account=cur, stream=True)


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    account = _route_account()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    if _verbose():
        _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
        _log_v(lambda: f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(account, chat_body, model_name, t0, rid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(account, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。

    若账号积分耗尽/被限流/鉴权失败，自动标记并切换账号顶上重试。
    """
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""
    pool = CONFIG.get("pool")
    attempts = len(pool.accounts) if pool is not None and pool.accounts else 1
    url = f"{BACKEND}/v2/chat/completions"
    cur = account
    tried = 0

    while True:
        headers = cur.credential.get_headers()
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        text = err.decode("utf-8", "replace")
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(text,200)}")
                        kind = _classify_upstream_error(r.status_code, text)
                        tried += 1
                        if kind in RETRY_KINDS and tried < attempts and pool is not None:
                            pool.mark_fail(cur.id, reason=f"HTTP {r.status_code} {_truncate(text,160)}", kind=kind)
                            nxt = pool.next_available(exclude_id=cur.id)
                            if nxt is not None:
                                converter = AnthropicStreamConverter(model=model_name)  # 重置转换器状态
                                _log(f"{prefix}↻ 切换账号 {cur.name} → {nxt.name}（{kind}，第 {tried+1}/{attempts} 次尝试）")
                                cur = nxt
                                continue
                        error_evt = {"type": "error", "error": {"message": text[:500], "type": "api_error", "code": r.status_code}}
                        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    if pool is not None:
                        pool.mark_success(cur.id)
                    async for line in r.aiter_lines():
                        events = converter.feed_line(line)
                        if events:
                            yield events.encode("utf-8")
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
            yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        break

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    if _verbose():
        elapsed = time.time() - t0 if t0 else 0
        _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")
    raw_usage = getattr(converter, "_usage", None)
    _report_credit(raw_usage or {}, model_name, rid, account=account, stream=True)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        try:
            pool = CONFIG.get("pool")
            if pool is not None and pool.accounts:
                stats = pool.summary().get("pool_stats", {})
                sys.stderr.write(
                    f"账号池    : {len(pool.accounts)} 个账号 | 策略 {pool.strategy} | "
                    f"健康 {stats.get('ok', 0)} 个 / 积分耗尽冷却中 {stats.get('exhausted', 0)} 个 / "
                    f"冷却中 {stats.get('cooldown', 0)} 个 / 停用 {stats.get('disabled', 0)} 个\n")
                for a in pool.accounts.values():
                    s = a.summary()
                    st = "有效" if not s["token_expired"] else "已过期"
                    sys.stderr.write(f"  - {s['name']} [{s['nickname']}] token: {st} | 健康: {s['health']}"
                                     + (f" | {s['last_error'][:80]}" if s["last_error"] else "") + "\n")
            else:
                cm = CredentialManager(af)
                info = cm.summary()
                sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n")
                sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    ap.add_argument("--verbose", action="store_true",
                    help="详细日志：记录完整请求/响应体（默认只记一行账单式日志）")
    ap.add_argument("--ui", action="store_true", default=True,
                    help="启用 Web 管理面板（默认开启，浏览器访问 / 即可打开）")
    ap.add_argument("--no-ui", action="store_true", dest="no_ui",
                    help="关闭 Web 管理面板（纯 API 模式，同原版行为）")
    ap.add_argument("--prefer", default="", metavar="NAME",
                    help="优先使用指定名称/昵称的账号（不指定时用登录时间最新的账号）")
    ap.add_argument("--strategy", default="failover", choices=["fixed", "auto", "failover"],
                    help="账号路由策略：fixed 固定账号 / auto 轮询 / "
                         "failover 自动顶上（默认，积分耗尽/限流/鉴权失败自动切换其他账号）")
    args = ap.parse_args()
    start(
        host=args.host, port=args.port, api_key=args.api_key,
        strategy=args.strategy, log_path=args.log,
        skip_check=args.skip_check, verbose=bool(getattr(args, "verbose", False)),
        desensitize=args.desensitize, no_compact=args.no_compact,
        prefer=args.prefer, with_ui=(args.ui and not args.no_ui),
    )


# 模块级 server 实例，供 stop() 优雅关闭
_server = None
# 每日签到调度器（个人账号签到领积分）
_checkin_scheduler = None


def start(host="127.0.0.1", port=8787, api_key="", strategy="failover",
          log_path=None, skip_check=True, verbose=False,
          desensitize=False, no_compact=False, prefer="",
          with_ui=True):
    """初始化配置 + 账号池 + UI，并阻塞运行 uvicorn。
    供命令行 main() 和桌面启动器 app.py 共同调用。"""
    global _server
    CONFIG["api_key"] = api_key
    CONFIG["desensitize"] = desensitize
    CONFIG["no_compact"] = no_compact
    CONFIG["verbose"] = verbose
    CONFIG["host"] = host
    CONFIG["port"] = port
    CONFIG["started_at"] = time.time()
    CONFIG["log_path"] = log_path if log_path else os.environ.get("CODEBUDDY2OPENAI_LOG")
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None

    try:
        pool = AccountPool(strategy=strategy)
        if not prefer and pool.accounts:
            newest = max(pool.accounts.values(), key=lambda a: a.path.stat().st_mtime)
            pool.active_id = newest.id
        if prefer:
            target = None
            for a in pool.accounts.values():
                if prefer in a.name or prefer in (a.summary().get("nickname") or ""):
                    target = a.id
                    break
            if target:
                pool.active_id = target
            else:
                sys.stderr.write(f"[警告] --prefer 未匹配到账号 '{prefer}'\n")
        CONFIG["pool"] = pool
    except Exception as e:
        sys.stderr.write(f"[警告] 账号池初始化失败（回退到单账号模式）：{e}\n")

    if not skip_check:
        preflight()

    _log(f"==== converter 启动 ====")

    if with_ui:
        try:
            from ui_admin import setup as ui_setup, LOG_BUS
            ui_setup(app, with_ui=True)
            LOG_BUS.emit(f"converter 启动 @ {host}:{port}，UI 已开启", level="info")

            @app.on_event("startup")
            async def _bind_logbus():
                import asyncio as _asyncio
                LOG_BUS.bind_loop(_asyncio.get_running_loop())
        except ImportError:
            sys.stderr.write("[警告] ui_admin 模块缺失，管理面板不可用（协议端点不受影响）\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 管理面板初始化失败：{e}\n")

    # 启动每日签到调度器（个人账号签到领积分；企业账号自动跳过）
    global _checkin_scheduler
    try:
        from billing import CheckinScheduler

        def _emit_log(msg, level="info", **kw):
            try:
                from ui_admin import LOG_BUS
                LOG_BUS.emit(msg, level=level)
            except Exception:
                pass

        _checkin_scheduler = CheckinScheduler(
            pool_getter=lambda: CONFIG.get("pool"),
            emit=_emit_log,
        )
        CONFIG["checkin_scheduler"] = _checkin_scheduler
        _checkin_scheduler.start()
        # 首次启动立即同步一次余额（异步线程，不阻塞）
        threading.Thread(target=_refresh_all_balances, daemon=True).start()
    except Exception as e:
        sys.stderr.write(f"[警告] 签到调度器初始化失败：{e}\n")

    # 启动 CodeBuddy CN 登录态自动同步：直接读取当前登录账号，切换时自动导入口+刷积分
    try:
        _cn_sync_stop.clear()
        threading.Thread(target=_cn_autosync_loop, daemon=True, name="cn-autosync").start()
    except Exception as e:
        sys.stderr.write(f"[警告] CN 自动同步线程启动失败：{e}\n")

    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    _server = uvicorn.Server(cfg)
    # 若不在主线程（如桌面启动器调用），需手动建 event loop
    import asyncio as _aio
    try:
        _aio.get_running_loop()
        _server.run()
    except RuntimeError:
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
        loop.run_until_complete(_server.serve())


def stop():
    """优雅停止服务（供桌面启动器调用）。"""
    global _server, _checkin_scheduler
    _cn_sync_stop.set()
    if _checkin_scheduler is not None:
        try:
            _checkin_scheduler.stop()
        except Exception:
            pass
    if _server is not None:
        _server.should_exit = True


def _refresh_all_balances():
    """后台线程：为账号池中所有账号刷新一次真实余额（个人账号有数据）。"""
    pool = CONFIG.get("pool")
    if pool is None:
        return
    for acct in list(pool.accounts.values()):
        try:
            acct.refresh_real_credit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CodeBuddy CN 登录态自动同步：
#   直接读取 CodeBuddy CN 桌面端当前登录的账号（state.vscdb 解密），
#   新账号自动导入口；用户切换登录时自动切换主力账号并刷新积分余额。
# ---------------------------------------------------------------------------

_cn_sync_stop = threading.Event()


def _cn_pool_account_by_uid(pool, uid: str):
    """在账号池中按 uid 查找账号（找不到返回 None）。"""
    for a in list(pool.accounts.values()):
        try:
            if (a.credential.summary() or {}).get("uid") == uid:
                return a
        except Exception:
            continue
    return None


def _cn_sync_uid(uid: str) -> Optional[str]:
    """把 CodeBuddy CN 当前登录账号（uid）同步到账号池（幂等）。

    新账号：解密凭据 → 写 .info → 加入账号池 → 设为主力 → 刷新积分余额；
    已有账号：把主力切换过去并刷新积分余额。
    返回动作描述（无动作返回 None）。
    """
    pool = CONFIG.get("pool")
    if pool is None:
        return None
    found = _cn_pool_account_by_uid(pool, uid)
    if found is None:
        from cn_importer import _read_cn_secret, import_account_from_secret
        secret = _read_cn_secret()
        if not secret:
            return None
        imported = import_account_from_secret(secret, pool)
        acct_id = (imported.get("account") or {}).get("id")
        if acct_id:
            try:
                pool.switch(acct_id)
            except Exception:
                pool.active_id = acct_id
        acct = pool.accounts.get(acct_id)
        if acct is not None:
            try:
                acct.refresh_real_credit()
            except Exception:
                pass
        return f"自动导入 CodeBuddy CN 当前登录账号「{imported['account']['name']}」并设为主力"
    if found.id != pool.active_id:
        try:
            pool.switch(found.id)
        except Exception:
            pass
        try:
            found.refresh_real_credit()
        except Exception:
            pass
        return f"检测到 CodeBuddy CN 登录切换 →「{found.name}」，积分池已同步"
    return None


def _cn_autosync_loop():
    """后台线程：定期读取 CodeBuddy CN 当前登录账号。

    _read_cn_secret 带文件签名缓存（vscdb/Local State 未变化时零开销），
    因此可以低频安全轮询。仅在登录账号变化时执行导入/切换动作。
    """
    from cn_importer import _read_cn_secret, _extract_account
    last_uid: Optional[str] = None
    while not _cn_sync_stop.is_set():
        uid = None
        try:
            ex = _extract_account(_read_cn_secret() or {})
            uid = (ex.get("account") or {}).get("uid") if ex else None
        except Exception:
            uid = None
        if uid != last_uid:
            last_uid = uid
            if uid:
                try:
                    action = _cn_sync_uid(uid)
                    if action:
                        _log(f"[CN自动同步] {action}")
                except Exception as e:
                    _log(f"[CN自动同步] 同步失败：{_truncate(str(e), 120)}")
        _cn_sync_stop.wait(10)


def run_checkin_once(force: bool = True) -> dict:
    """手动触发一轮签到（UI/API 调用）。"""
    global _checkin_scheduler
    if _checkin_scheduler is None:
        return {"error": "签到调度器未初始化"}
    return _checkin_scheduler.run_once(force=force)


if __name__ == "__main__":
    import sys as _sys
    # 修正双模块问题：`python converter.py` 时本文件以 __main__ 运行，
    # 但 account_pool/ui_admin 内部 `import converter` 会重新加载一个副本，
    # 导致 CONFIG 状态不同步。把 __main__ 注册为 converter，保证单实例共享状态。
    _sys.modules["converter"] = _sys.modules["__main__"]
    main()
