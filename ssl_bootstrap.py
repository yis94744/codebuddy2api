# -*- coding: utf-8 -*-
"""CA 证书定位兜底。

背景（2026-09-13 实测）：
    PyInstaller 只收集了 `certifi` 的 .py，**没收集 `cacert.pem`**，
    于是 `certifi.where()` 指向一个不存在的路径。httpx 建 SSL context 时
    直接抛 `FileNotFoundError: [Errno 2] No such file or directory`，
    表现为所有 httpx 请求全挂（签到「签到网络失败: [Errno 2] ...」、
    上游对话请求同样受影响），而源码环境完全正常。

    根因修复在 build.bat（--collect-data certifi）。本模块是第二道防线：
    即使证书文件再次缺失（用户自行打包漏参数、hook 行为变动等），也能
    自动换用其它可用 CA，而不是让全链路 fail。

顺序：环境变量 → certifi → PyInstaller 解压目录 → 系统默认证书库。
找到后写入 SSL_CERT_FILE / REQUESTS_CA_BUNDLE（httpx 与 requests 都认），
只 setdefault，不覆盖用户显式设置。
"""
from __future__ import annotations

import os
import sys

_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

_ca_bundle = None
_resolved = False


def _candidates():
    """按优先级产出候选 CA 路径。"""
    # 1) 用户/系统已显式指定
    for var in _ENV_VARS:
        p = os.environ.get(var)
        if p:
            yield "env:%s" % var, p
    # 2) certifi 自带（正常路径）
    try:
        import certifi  # noqa: WPS433
        yield "certifi", certifi.where()
    except Exception:
        pass
    # 3) certifi 数据文件被收进 _MEIPASS 但包元数据没跟上的情况
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        yield "_MEIPASS/certifi", os.path.join(mei, "certifi", "cacert.pem")
    # 4) 打包目录 / 源码目录旁的 certifi
    for base in (os.path.dirname(os.path.abspath(__file__)),):
        yield "sibling/certifi", os.path.join(base, "certifi", "cacert.pem")
    # 5) 系统默认证书库（Windows 上通常为空，Linux/macOS 常可用）
    try:
        import ssl  # noqa: WPS433
        paths = ssl.get_default_verify_paths()
        if paths.cafile:
            yield "ssl.cafile", paths.cafile
        if paths.openssl_cafile:
            yield "ssl.openssl_cafile", paths.openssl_cafile
        if paths.capath:
            yield "ssl.capath", paths.capath
    except Exception:
        pass
    # 6) 常见系统 CA 文件
    for p in ("/etc/ssl/certs/ca-certificates.crt",
              "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/ssl/cert.pem"):
        yield "system", p


def find_ca_bundle() -> "str | None":
    """返回第一个真实存在的 CA 路径；找不到返回 None。"""
    for _source, path in _candidates():
        if path and os.path.exists(path):
            return path
    return None


def install(verbose: bool = False) -> "str | None":
    """定位并导出 CA 环境变量。幂等，可重复调用。

    返回最终使用的 CA 路径（None 表示没找到，交给 httpx 默认行为）。
    """
    global _ca_bundle, _resolved
    if _resolved:
        return _ca_bundle

    _ca_bundle = find_ca_bundle()
    _resolved = True
    if _ca_bundle:
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            os.environ.setdefault(var, _ca_bundle)
    if verbose:
        print("[ssl_bootstrap] CA bundle =", _ca_bundle)
    return _ca_bundle


def report() -> dict:
    """给自检用的诊断信息。"""
    chosen = install()
    tried = []
    for source, path in _candidates():
        tried.append({"source": source, "path": path,
                      "exists": bool(path and os.path.exists(path))})
    return {"chosen": chosen, "tried": tried}


# 导入即生效：converter / app / account_pool 都依赖 httpx，
# 让兜底在任何 httpx.Client 构造之前完成。
install()
