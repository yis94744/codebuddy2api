# -*- coding: utf-8 -*-
"""打包完整性自检。

为什么需要它：
    PyInstaller 的静态分析看不见「动态导入」和「数据文件」，这两类缺口在
    源码环境下**永远不会暴露**，只有打包后才炸。已经踩过的坑：

      1. uvicorn.protocols.http.auto  → 服务起不来（动态导入）
      2. anyio._backends._asyncio     → 每个请求 500（动态导入）
      3. certifi 的 cacert.pem        → 所有 httpx 请求 Errno 2（数据文件）

    每次都是「用户报错 → 打调试版 → 抓堆栈」才发现，成本很高。这个自检
    把这三类检查前置：打包完立刻跑，缺什么一眼看到。

用法：
    CodeBuddy2API.exe --selfcheck      # 结果写 exe 同目录 selfcheck.log
    python selfcheck.py                # 源码环境直接跑，同时打印到 stdout
    python selfcheck.py --net          # 额外做一次 TLS 握手连通性测试
"""
from __future__ import annotations

import io
import os
import sys
import traceback

# ---------------------------------------------------------------------------
# 检查清单
# ---------------------------------------------------------------------------

# (模块名, 说明, 是否必需)。
# 必需项缺失 = 功能坏掉，计入 FAIL。
# 可选项缺失 = 只是降级路径不可用（httpx 会自动换实现），记为 WARN，
# 不拖累整体结论——否则每台机器都会报一堆无害的「缺依赖」。
# 带「动态导入」注释的是 PyInstaller 静态分析抓不到、必须靠
# --collect-submodules / --hidden-import 显式补的。
MODULES = [
    # --- Web 框架与服务器 ---
    ("fastapi", "Web 框架", True),
    ("starlette", "ASGI 工具集", True),
    ("starlette.routing", "路由（starlette 子模块）", True),
    ("pydantic", "数据校验", True),
    ("uvicorn", "ASGI 服务器", True),
    ("uvicorn.protocols.http.auto", "HTTP 协议自动选择（动态导入）", True),
    ("uvicorn.protocols.http.h11_impl", "h11 HTTP 实现", True),
    ("uvicorn.protocols.websockets.auto", "WS 协议自动选择（动态导入）", True),
    ("uvicorn.lifespan.on", "lifespan 协议（动态导入）", True),
    ("uvicorn.loops.auto", "事件循环自动选择（动态导入）", True),
    # --- 异步运行时 ---
    ("anyio", "异步运行时", True),
    ("anyio._backends._asyncio", "anyio 的 asyncio 后端（动态导入）", True),
    ("anyio._core._sockets", "anyio socket 层", True),
    ("sniffio", "异步库探测（可选，httpx 会退化）", False),
    # --- HTTP 客户端链 ---
    ("httpx", "HTTP 客户端（上游转发 + 计费）", True),
    ("httpx._transports.default", "httpx 默认传输", True),
    ("httpcore", "httpx 底层传输", True),
    ("httpcore._sync.connection", "httpcore 同步连接", True),
    ("httpcore._backends.sync", "httpcore 同步后端（动态导入）", True),
    ("idna", "国际化域名", True),
    ("charset_normalizer", "响应解码（可选）", False),
    # --- TLS ---
    ("ssl", "TLS", True),
    ("certifi", "CA 证书包", True),
    # --- GUI ---
    ("tkinter", "GUI 基础库", True),
    ("customtkinter", "GUI 工具包", True),
    # --- 标准库中被打包器容易漏的 ---
    ("sqlite3", "本地存储", True),
    # --- 本项目模块 ---
    ("ssl_bootstrap", "证书兜底", True),
    ("netenv", "代理策略（默认直连）", True),
    ("desensitize", "运行时脱敏", True),
    ("account_pool", "账号池", True),
    ("billing", "计费/签到", True),
    ("cn_importer", "CN 登录态导入", True),
    ("responses_adapter", "Responses 协议适配", True),
    ("responses_projection", "Responses 请求投影", True),
    ("anthropic_adapter", "Anthropic 协议适配", True),
    ("ui_admin", "管理面板后端", True),
    ("converter", "主服务", True),
]

# 随包数据文件。
# resolver="certifi" 的条目走 certifi.where()，源码环境在 site-packages
# 下、打包后在 _MEIPASS 下，两种布局都能正确判定。
DATA_FILES = [
    {"dir": "certifi", "name": "cacert.pem",
     "desc": "CA 证书（httpx 建 TLS 必需）", "resolver": "certifi"},
    {"dir": "ui", "name": "index.html", "desc": "管理面板页面"},
    {"dir": "ui", "name": "favicon.svg", "desc": "面板图标"},
    {"dir": "ui", "name": "vendor/vue.global.prod.js", "desc": "Vue 运行时"},
    {"dir": "ui", "name": "vendor/element-plus.full.min.js", "desc": "Element Plus 组件"},
    {"dir": "ui", "name": "vendor/element-plus.css", "desc": "Element Plus 样式"},
    {"dir": "ui", "name": "vendor/echarts.min.js", "desc": "ECharts 图表"},
    {"dir": "assets", "name": "icon.ico", "desc": "窗口/任务栏图标"},
]


def _asset_roots():
    """打包后资源的可能根目录，按优先级排列。"""
    roots = []
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(mei)
    mine = os.path.dirname(os.path.abspath(__file__))
    if mine not in roots:
        roots.append(mine)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir not in roots:
            roots.append(exe_dir)
    return roots


def _find_data(rel_dir, name):
    for root in _asset_roots():
        p = os.path.join(root, rel_dir, name)
        if os.path.exists(p):
            return p
    return None


def _app_dir():
    if os.environ.get("CB2A_APPDIR"):
        return os.environ["CB2A_APPDIR"]
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_modules():
    results = []
    for item in MODULES:
        name, desc, required = item
        try:
            __import__(name)
            results.append({"ok": True, "name": name, "desc": desc,
                            "detail": "", "required": required})
        except Exception as e:
            results.append({"ok": False, "name": name, "desc": desc,
                            "detail": "%s: %s" % (type(e).__name__, e),
                            "required": required})
    return results


def _resolve_certifi():
    try:
        import certifi
        p = certifi.where()
        return p if os.path.exists(p) else None
    except Exception:
        return None


def check_data_files():
    results = []
    for item in DATA_FILES:
        label = "%s/%s" % (item["dir"], item["name"])
        path = None
        if item.get("resolver") == "certifi":
            path = _resolve_certifi()
        else:
            path = _find_data(item["dir"], item["name"])
        if path:
            results.append({"ok": True, "name": label, "desc": item["desc"],
                            "detail": "%d bytes  %s" % (os.path.getsize(path), path),
                            "required": True})
        else:
            results.append({"ok": False, "name": label, "desc": item["desc"],
                            "detail": "未找到；查找根目录: %s"
                                      % ", ".join(_asset_roots()),
                            "required": True})
    return results


def check_ssl():
    """CA 证书可用性 + SSLContext 能否建立。"""
    results = []

    def add(ok, name, desc, detail):
        results.append({"ok": ok, "name": name, "desc": desc,
                        "detail": detail, "required": True})

    try:
        import ssl_bootstrap
        info = ssl_bootstrap.report()
        chosen = info["chosen"]
        if chosen:
            add(True, "CA bundle 定位", "证书兜底", chosen)
        else:
            tried = "; ".join("%s=%s" % (t["source"], t["path"])
                              for t in info["tried"][:6])
            add(False, "CA bundle 定位", "证书兜底",
                "所有候选都不存在: " + tried)
    except Exception as e:
        add(False, "CA bundle 定位", "证书兜底",
            "%s: %s" % (type(e).__name__, e))

    try:
        import ssl
        import certifi
        cafile = certifi.where()
        if os.path.exists(cafile):
            ctx = ssl.create_default_context(cafile=cafile)
            add(True, "SSLContext(certifi)", "用 certifi 证书建 SSL 上下文",
                "%d 个 CA" % len(ctx.get_ca_certs()))
        else:
            add(False, "SSLContext(certifi)", "用 certifi 证书建 SSL 上下文",
                "certifi.where() 不存在: %s" % cafile)
    except Exception as e:
        add(False, "SSLContext(certifi)", "用 certifi 证书建 SSL 上下文",
            "%s: %s" % (type(e).__name__, e))
    return results


def check_httpx():
    """构造 httpx.Client —— 报 Errno 2 的那个点就在这一步之后的首个请求。"""
    results = []

    def add(ok, name, desc, detail):
        results.append({"ok": ok, "name": name, "desc": desc,
                        "detail": detail, "required": True})

    try:
        import netenv
        st = netenv.describe()
        raw = (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
               or os.environ.get("http_proxy") or os.environ.get("https_proxy") or "")
        if netenv.trust_env():
            add(True, "代理策略", "出网是否继承环境代理", "走环境代理：" + st)
        else:
            add(True, "代理策略", "出网是否继承环境代理",
                "直连（已忽略环境代理%s）" % ("：" + raw if raw else ""))
    except Exception as e:
        add(False, "代理策略", "出网是否继承环境代理",
            "%s: %s" % (type(e).__name__, e))

    try:
        import netenv
        with netenv.client(5):
            pass
        add(True, "httpx.Client()", "同步客户端构造", "")
    except Exception as e:
        add(False, "httpx.Client()", "同步客户端构造",
            "%s: %s" % (type(e).__name__, e))
    # 客户端构造成功不代表真能建连接，这里直接取出传输层的 SSLContext，
    # 验证 CA 已装载——等价于首个真实请求前的准备工作。
    try:
        import netenv
        c = netenv.client(5)
        try:
            pool = getattr(c._transport, "_pool", None)
            ssl_ctx = pool._ssl_context if pool is not None else None
            if ssl_ctx is not None:
                add(True, "httpx 传输层 SSL", "客户端传输层已装载 CA",
                    "%d 个 CA" % len(ssl_ctx.get_ca_certs()))
            else:
                add(True, "httpx 传输层 SSL", "客户端传输层已装载 CA",
                    "（内部结构不可见，已跳过）")
        finally:
            c.close()
    except Exception as e:
        add(False, "httpx 传输层 SSL", "客户端传输层已装载 CA",
            "%s: %s" % (type(e).__name__, e))
    return results


def check_net(timeout=8):
    """真实 TLS 握手，确认证书链可用。只做握手，不发业务请求。"""
    results = []
    try:
        import socket
        import ssl
        import ssl_bootstrap
        cafile = ssl_bootstrap.install()
        ctx = (ssl.create_default_context(cafile=cafile) if cafile
               else ssl.create_default_context())
        host = "copilot.tencent.com"
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                results.append({"ok": True, "name": "TLS 握手 " + host,
                                "desc": "证书链校验通过",
                                "detail": ssock.version() or "", "required": True})
    except Exception as e:
        results.append({"ok": False, "name": "TLS 握手", "desc": "证书链校验",
                        "detail": "%s: %s" % (type(e).__name__, e),
                        "required": True})
    return results


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    do_net = "--net" in argv

    groups = []
    groups.append(("模块导入", check_modules()))
    groups.append(("随包数据", check_data_files()))
    groups.append(("TLS / 证书", check_ssl()))
    groups.append(("HTTP 客户端", check_httpx()))
    if do_net:
        groups.append(("网络连通", check_net()))

    lines = []
    lines.append("CodeBuddy2API 打包自检")
    lines.append("frozen=%s  python=%s" % (getattr(sys, "frozen", False),
                                           sys.version.split()[0]))
    lines.append("executable=%s" % (sys.executable,))
    lines.append("_MEIPASS=%s" % (getattr(sys, "_MEIPASS", "(none)"),))
    lines.append("")
    fail = 0
    warn = 0
    total = 0
    for title, results in groups:
        ok_n = sum(1 for r in results if r["ok"])
        total += len(results)
        for r in results:
            if not r["ok"]:
                if r.get("required", True):
                    fail += 1
                else:
                    warn += 1
        lines.append("── %s (%d/%d) ──" % (title, ok_n, len(results)))
        for r in results:
            if r["ok"]:
                mark = "PASS"
            elif r.get("required", True):
                mark = "FAIL"
            else:
                mark = "WARN"
            line = "[%s] %-38s %s" % (mark, r["name"], r["desc"])
            if r["detail"]:
                line += "  —— %s" % r["detail"]
            lines.append(line)
        lines.append("")

    lines.append("=" * 60)
    if fail:
        lines.append("结果: %d/%d 通过，%d 项失败 ← 打包缺件，需补 --collect-*"
                     % (total - fail - warn, total, fail))
    else:
        summary = "结果: %d/%d 全部通过" % (total - warn, total)
        if warn:
            summary += "（%d 项可选依赖缺失，不影响功能）" % warn
        lines.append(summary)

    text = "\n".join(lines)
    out_path = os.path.join(_app_dir(), "selfcheck.log")
    try:
        with io.open(out_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        out_path = "(写入失败)"
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.write("→ 结果已写入 %s\n" % out_path)
    except Exception:
        pass
    return 1 if fail else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
