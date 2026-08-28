# -*- coding: utf-8 -*-
"""Web 管理面板后端：管理 API + 日志总线 + 静态页面托管。

在不改动 /v1/* 协议转发逻辑的前提下，为 converter 增加：
  - /api/accounts   账号池管理（列表/切换/导入/检查）
  - /api/config     配置查看与热更新
  - /api/logs       日志查询与统计
  - /ws/logs        实时日志推送
  - /ui/*           前端静态页面
"""
from __future__ import annotations

import asyncio
import collections
import json
import threading
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from account_pool import AccountPool

# ---------------------------------------------------------------------------
# 日志总线：内存环形缓冲 + WebSocket 广播
# ---------------------------------------------------------------------------

class LogBus:
    def __init__(self, maxlen: int = 5000):
        self._items: collections.deque = collections.deque(maxlen=maxlen)
        self._subs: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, msg: str, level: str = "info", rid: str = "",
             model: str = "", payload: object = None,
             credit: float = 0.0) -> dict:
        item = {
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S", time.localtime()),
            "level": level,
            "rid": rid,
            "model": model,
            "msg": msg,
            "payload": payload,
            "credit": float(credit or 0),
        }
        with self._lock:
            self._items.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(q.put_nowait, item)
            except Exception:
                pass
        return item

    def history(self, limit: int = 200) -> list:
        with self._lock:
            return list(self._items)[-limit:]

    def stats(self) -> dict:
        with self._lock:
            items = list(self._items)
        now = time.time()
        today = [i for i in items if i["ts"] >= now - 86400]
        warns = [i for i in today if i["level"] == "warn"]
        errors = [i for i in today if i["level"] == "error"]
        filters = [i for i in today if "审核拦截" in i["msg"] or "content-filter" in i["msg"].lower()]
        # 平均耗时：从 RESPONSE 日志解析
        durations = []
        for i in today:
            if i["msg"].startswith("◀ RESPONSE") or "◀ RESPONSE" in i["msg"]:
                try:
                    seg = i["msg"].split("|")[1].strip()
                    durations.append(float(seg.replace("s", "").strip()))
                except Exception:
                    pass
        return {
            "today_requests": len(today),
            "warns": len(warns),
            "errors": len(errors),
            "content_filters": len(filters),
            "avg_duration": round(sum(durations) / len(durations), 2) if durations else 0,
            "today_credit": round(sum(float(i.get("credit") or 0) for i in today), 4),
            "last_update": items[-1]["time"] if items else "-",
        }

    # -- 订阅（WS） ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)


LOG_BUS = LogBus()


# ---------------------------------------------------------------------------
# 配置读取（与 converter 共享 CONFIG，延迟导入避免循环依赖）
# ---------------------------------------------------------------------------

def _converter():
    """获取 converter 模块单例。

    优先用 __main__（`python converter.py` 时主模块），其次才 import converter，
    确保 CONFIG 等全局状态与主进程共享。
    """
    import sys
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "CONFIG"):
        return main_mod
    import converter
    return converter


def _check_admin_auth(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    """管理 API 鉴权：与协议端点一致，未设 key 时放行但 UI 会有警告。"""
    conv = _converter()
    key = conv.CONFIG.get("api_key") or ""
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _get_pool() -> AccountPool:
    conv = _converter()
    pool = conv.CONFIG.get("pool")
    if pool is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "账号池未初始化", "type": "server_error"}})
    return pool


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.get("/accounts")
def list_accounts(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return _get_pool().summary()


@router.post("/accounts/scan")
def scan_accounts(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return _get_pool().scan()


@router.post("/accounts/import")
async def import_account(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json"}})
    path = (body or {}).get("path") or ""
    name = (body or {}).get("name")
    if not path:
        raise HTTPException(status_code=400, detail={"error": {"message": "path is required"}})
    try:
        return _get_pool().add(path, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e)}})


@router.post("/accounts/{aid}/switch")
def switch_account(aid: str,
                   authorization: Optional[str] = Header(default=None),
                   x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    try:
        acct = _get_pool().switch(aid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail={"error": {"message": str(e)}})
    LOG_BUS.emit(f"账号切换 -> {acct['name']}", level="info", model=acct.get("nickname") or "")
    return acct


@router.put("/accounts/{aid}")
async def update_account(aid: str, request: "Request",
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        body = {}
    pool = _get_pool()
    try:
        if "name" in body:
            pool.rename(aid, body.get("name") or "")
        if "enabled" in body:
            pool.set_enabled(aid, bool(body.get("enabled")))
        return pool.accounts[aid].summary()
    except KeyError as e:
        raise HTTPException(status_code=404, detail={"error": {"message": str(e)}})


@router.post("/accounts/{aid}/check")
def check_account(aid: str,
                  authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    pool = _get_pool()
    try:
        acct = pool.accounts[aid]
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在"}})
    result = acct.check()
    LOG_BUS.emit(f"账号检查 [{acct.name}]: {result['detail']}",
                 level="warn" if result["status"] != "ok" else "info")
    return {**result, "account": acct.summary()}


@router.post("/accounts/{aid}/exhaust")
async def mark_exhausted(aid: str, request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动标记账号积分/额度耗尽：立即进入长冷却，路由自动顶上其他账号。"""
    _check_admin_auth(authorization, x_api_key)
    pool = _get_pool()
    try:
        acct = pool.accounts[aid]
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在"}})
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body or {}).get("reason") or "manual mark exhausted"
    acct.mark_fail(reason, kind="quota")
    LOG_BUS.emit(f"标记积分耗尽 [{acct.name}]: {reason}", level="warn", model=acct.name)
    return acct.summary()


@router.post("/accounts/{aid}/recover")
def recover_account(aid: str,
                    authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动恢复账号：解除积分耗尽/冷却标记，重新参与路由。"""
    _check_admin_auth(authorization, x_api_key)
    pool = _get_pool()
    try:
        acct = pool.accounts[aid]
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": {"message": "账号不存在"}})
    acct.mark_recovered()
    LOG_BUS.emit(f"恢复账号 [{acct.name}]：解除耗尽/冷却标记", level="info", model=acct.name)
    return acct.summary()


@router.post("/accounts/strategy")
async def set_strategy(request: "Request",
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json"}})
    strategy = (body or {}).get("strategy")
    try:
        _get_pool().set_strategy(strategy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e)}})
    return {"strategy": strategy}


# -- 配置 ----------------------------------------------------------------

@router.get("/config")
def get_config(authorization: Optional[str] = Header(default=None),
               x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    return {
        "host": conv.CONFIG.get("host"),
        "port": conv.CONFIG.get("port"),
        "api_key": bool(conv.CONFIG.get("api_key")),
        "api_key_hint": (conv.CONFIG.get("api_key") or "")[:4] + "****" if conv.CONFIG.get("api_key") else "",
        "desensitize": bool(conv.CONFIG.get("desensitize")),
        "no_compact": bool(conv.CONFIG.get("no_compact")),
        "log_path": conv.CONFIG.get("log_path"),
        "models": conv.DEFAULT_MODELS,
        "backend": conv.BACKEND,
        "strategy": _get_pool().strategy,
    }


@router.put("/config")
async def update_config(request: Request,
                        authorization: Optional[str] = Header(default=None),
                        x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json"}})
    body = body or {}
    note = []
    # 热更新：不重启即生效的项
    if "desensitize" in body:
        conv.CONFIG["desensitize"] = bool(body["desensitize"])
        note.append("脱敏: " + ("开" if body["desensitize"] else "关"))
    if "no_compact" in body:
        conv.CONFIG["no_compact"] = bool(body["no_compact"])
        note.append("no_compact: " + ("开" if body["no_compact"] else "关"))
    if "log_path" in body:
        conv.CONFIG["log_path"] = body["log_path"] or None
        note.append("日志路径已更新")
    if "models" in body and isinstance(body["models"], list):
        conv.DEFAULT_MODELS[:] = [str(m) for m in body["models"]]
        note.append(f"模型列表更新为 {len(conv.DEFAULT_MODELS)} 个")
    if "api_key" in body and isinstance(body["api_key"], str):
        conv.CONFIG["api_key"] = body["api_key"].strip()
        note.append("API Key 已更新")
    # 需重启生效的项
    if "port" in body or "host" in body:
        note.append("⚠ host/port 修改需重启服务生效")
    if note:
        LOG_BUS.emit("配置更新: " + "；".join(note), level="info")
    return {"updated": note, **get_config(authorization, x_api_key)}


# -- 日志 ----------------------------------------------------------------

@router.get("/logs")
def query_logs(level: str = Query(default=""),
               q: str = Query(default=""),
               limit: int = Query(default=200, ge=1, le=1000),
               authorization: Optional[str] = Header(default=None),
               x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    items = LOG_BUS.history(limit)
    if level:
        items = [i for i in items if i["level"] == level]
    if q:
        items = [i for i in items if q.lower() in i["msg"].lower()
                 or q.lower() in i["rid"].lower() or q.lower() in i["model"].lower()]
    return {"items": items}


@router.get("/logs/stats")
def log_stats(authorization: Optional[str] = Header(default=None),
              x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    return LOG_BUS.stats()


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    # 管理 WS 不做强鉴权（本地回环），仅提示
    await websocket.accept()
    q = LOG_BUS.subscribe()
    try:
        # 先推送历史
        for item in LOG_BUS.history(100):
            await websocket.send_text(json.dumps(item, ensure_ascii=False))
        # 再实时推送
        while True:
            item = await q.get()
            await websocket.send_text(json.dumps(item, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        LOG_BUS.unsubscribe(q)


# -- 服务状态 ------------------------------------------------------------

@router.get("/status")
def service_status(authorization: Optional[str] = Header(default=None),
                   x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    pool = _get_pool()
    cred = pool.resolve()
    started = conv.CONFIG.get("started_at") or time.time()
    info = {"status": "ok", "started_at": started,
            "uptime": int(time.time() - started),
            "backend": conv.BACKEND, "api_key_required": bool(conv.CONFIG.get("api_key"))}
    try:
        info["pool_stats"] = pool.summary().get("pool_stats", {})
        info["strategy"] = pool.strategy
    except Exception:
        pass
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    return info


# -- 计费/签到 -------------------------------------------------------------

@router.get("/billing/status")
def billing_status(authorization: Optional[str] = Header(default=None),
                   x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """签到调度器状态 + 各账号余额（个人账号为真实余额，企业账号为本地累计）。"""
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    pool = _get_pool()
    scheduler = conv.CONFIG.get("checkin_scheduler")
    accounts = []
    for a in pool.accounts.values():
        s = a.summary()
        accounts.append({
            "name": s["name"],
            "nickname": s.get("nickname"),
            "health": s["health"],
            "credit_spent": s["credit_spent"],
            "real_credit": s["real_credit"],
            "real_credit_note": s["real_credit_note"],
        })
    return {
        "scheduler_running": scheduler is not None,
        "last_run": getattr(scheduler, "_last_run_day", None) if scheduler else None,
        "accounts": accounts,
    }


@router.post("/billing/checkin")
def billing_checkin(authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动触发一轮签到（仅个人账号）。"""
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    try:
        result = conv.run_checkin_once(force=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})
    return result


@router.post("/billing/refresh")
def billing_refresh(authorization: Optional[str] = Header(default=None),
                    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """手动刷新全部账号余额（异步触发）。"""
    _check_admin_auth(authorization, x_api_key)
    conv = _converter()
    threading.Thread(target=conv._refresh_all_balances, daemon=True).start()
    return {"ok": True, "message": "余额刷新已触发"}


# -- CodeBuddy CN 一键添加账号 ----------------------------------------------

@router.post("/accounts/cn/open")
def cn_open_login(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """打开 CodeBuddy CN 桌面端登录界面。"""
    _check_admin_auth(authorization, x_api_key)
    try:
        from cn_importer import open_cn_login
        message = open_cn_login()
        return {"ok": True, "message": message}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


@router.get("/accounts/cn/detect")
def cn_detect(authorization: Optional[str] = Header(default=None),
              x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """检测 CodeBuddy CN 中是否有未导入的新账号。"""
    _check_admin_auth(authorization, x_api_key)
    try:
        from cn_importer import detect_new_accounts
        result = detect_new_accounts(_get_pool())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


@router.post("/accounts/cn/import")
def cn_import(authorization: Optional[str] = Header(default=None),
              x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """把 CodeBuddy CN 当前凭据导入账号池（仅当是新账号时）。"""
    _check_admin_auth(authorization, x_api_key)
    try:
        from cn_importer import detect_new_accounts, import_account_from_secret
        result = detect_new_accounts(_get_pool())
        if not result.get("found"):
            return {"ok": False, "message": result.get("reason", "未发现新账号")}
        imported = import_account_from_secret(result["info"], _get_pool())
        LOG_BUS.emit(f"一键导入账号 -> {imported['account']['name']}", level="info")
        return {"ok": True, "message": f"已导入账号：{imported['account']['name']}",
                "imported": imported}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


# ---------------------------------------------------------------------------
# 挂载到 FastAPI app
# ---------------------------------------------------------------------------

def _ui_dir() -> Path:
    """定位 UI 静态目录：源码模式为项目 ui/，打包模式为 _MEIPASS/ui。"""
    import sys as _sys
    if getattr(_sys, "frozen", False):
        base = Path(getattr(_sys, "_MEIPASS", Path(__file__).parent))
        cand = base / "ui"
        if cand.is_dir():
            return cand
    return Path(__file__).parent / "ui"


UI_DIR = _ui_dir()


def setup(app, *, with_ui: bool = True):
    """注册管理路由与静态页面。with_ui=False 时仅注册 API（供测试）。"""
    app.include_router(router)
    if with_ui and UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(str(UI_DIR / "index.html"))

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon():
            return FileResponse(str(UI_DIR / "favicon.svg"))
    return app
