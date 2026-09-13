#!/usr/bin/env python3
"""HTTP 客户端代理策略 —— 默认「直连」，不继承宿主环境里的代理变量。

为什么需要这个模块
------------------
httpx 的 Client/AsyncClient 默认 trust_env=True，会自动读取进程环境里的
HTTP_PROXY / HTTPS_PROXY / ALL_PROXY（以及 netrc）。本服务经常被别的程序
拉起（IDE、桌面启动器、CI、沙箱宿主），这些父进程的环境里常残留指向本地
代理端口的变量，例如：

    HTTP_PROXY=http://127.0.0.1:17173

一旦该代理端口没有进程监听，所有出网请求（签到、查积分、刷 token、转发）
都会直接失败：

    [WinError 10061] 由于目标计算机积极拒绝，无法连接。

这不是证书问题（那是 Errno 2），也不是服务没起来 —— 是「本该直连的请求被
塞进了一个不存在的代理」。本服务的目标后端都是直连可用的，所以默认直连。

需要走代理时（少数内网环境）
---------------------------
任选其一：
  1. 环境变量  CB2A_TRUST_ENV=1
  2. config.json 里加  "use_proxy": true
  3. 环境变量  CB2A_TRUST_ENV=0  可强制直连（覆盖配置）

用法
----
    import netenv
    with netenv.client(timeout=15) as c:      # 同步
        ...
    async with netenv.aclient(timeout=300) as c:   # 异步
        ...
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

__all__ = ["trust_env", "client", "aclient", "describe", "reset_cache", "config_path"]

_TRUE = ("1", "true", "yes", "on", "y", "t")
_FALSE = ("0", "false", "no", "off", "n", "f")

_cached: Optional[bool] = None


def config_path() -> Optional[str]:
    """定位 config.json：优先 exe 同目录（打包），其次源码目录。"""
    candidates = []
    if os.environ.get("CB2A_APPDIR"):
        candidates.append(os.path.join(os.environ["CB2A_APPDIR"], "config.json"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "config.json"))
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "config.json"))
    except NameError:
        pass
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def trust_env() -> bool:
    """是否允许 httpx 读取环境代理。默认 False（直连）。结果带缓存。"""
    global _cached
    if _cached is not None:
        return _cached

    raw = (os.environ.get("CB2A_TRUST_ENV") or "").strip().lower()
    if raw in _TRUE:
        _cached = True
        return _cached
    if raw in _FALSE:
        _cached = False
        return _cached

    enabled = False
    p = config_path()
    if p:
        try:
            with open(p, "r", encoding="utf-8") as f:
                enabled = bool((json.load(f) or {}).get("use_proxy", False))
        except Exception:
            enabled = False
    _cached = enabled
    return _cached


def reset_cache() -> None:
    """清空缓存（测试或运行期改配置后调用）。"""
    global _cached
    _cached = None


def describe() -> str:
    """给自检/日志用的一行说明。"""
    ok = trust_env()
    if not ok:
        return "直连（忽略环境代理变量）"
    seen = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                        "http_proxy", "https_proxy", "all_proxy")
            if os.environ.get(k)]
    return "走系统/环境代理" + ("（%s）" % ",".join(seen) if seen else "")


def _proxy_free_env() -> None:
    """兜底：把常见的代理环境变量从本进程抹掉。

    仅当策略为「直连」时调用，保证即使将来某处漏传 trust_env，
    也不会被环境变量劫持。设置 HTTPS_PROXY 的进程是我们自己拉起的，
    清理它不会影响父进程。
    """
    if trust_env():
        return
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy",
              "FTP_PROXY", "ftp_proxy", "NO_PROXY", "no_proxy"):
        os.environ.pop(k, None)


def client(timeout=15, **kw):
    """构造同步 httpx.Client，按本模块策略决定是否读环境代理。"""
    import httpx
    _proxy_free_env()
    kw.setdefault("trust_env", trust_env())
    return httpx.Client(timeout=timeout, **kw)


def aclient(timeout=15, **kw):
    """构造异步 httpx.AsyncClient，按本模块策略决定是否读环境代理。"""
    import httpx
    _proxy_free_env()
    kw.setdefault("trust_env", trust_env())
    return httpx.AsyncClient(timeout=timeout, **kw)
