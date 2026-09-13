"""构建产物校验工具。

用法:
    python tools/verify_build.py            # 校验 exe 是否内嵌了 assets/icon.ico
    python tools/verify_build.py --smoke    # 额外做一次隔离冒烟测试（起服务 -> 探活）

为什么需要它:
    PyInstaller 打包时 --icon 若失效（例如图标路径写错），构建照样"成功"，
    只有安装后才会发现桌面图标不对。这个脚本把这一步变成可自动化的检查。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
ICO = ROOT / "assets" / "icon.ico"
EXE = ROOT / "dist" / "CodeBuddy2API.exe"
SMOKE_PORT = 8789


# ---------------------------------------------------------------- icon check

def read_ico_entries(ico: pathlib.Path):
    data = ico.read_bytes()
    if data[:4] != b"\x00\x00\x01\x00":
        raise ValueError(f"{ico} 不是标准 ICO 文件")
    count = struct.unpack_from("<H", data, 4)[0]
    entries = []
    for i in range(count):
        off = 6 + i * 16
        w, h, _c, _r, _p, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, off)
        entries.append((256 if w == 0 else w, 256 if h == 0 else h, bpp,
                        data[offset:offset + size]))
    return entries


def verify_icon() -> bool:
    if not ICO.exists():
        print(f"[X] 缺少图标 {ICO}")
        return False
    if not EXE.exists():
        print(f"[X] 缺少产物 {EXE}（先运行 build.bat）")
        return False

    entries = read_ico_entries(ICO)
    blob = EXE.read_bytes()
    print(f"exe : {EXE.name}  {len(blob):,} bytes")
    print(f"icon: {ICO.name}  {len(entries)} 个尺寸")

    hit = 0
    for w, h, bpp, payload in entries:
        found = payload in blob
        hit += found
        print(f"  [{'OK' if found else 'XX'}] {w}x{h} {bpp}bpp  {len(payload):,} bytes")

    print(f"-> 图标命中 {hit}/{len(entries)}")
    return hit == len(entries)


# ---------------------------------------------------------------- smoke test

FAKE_INFO = {
    "account": {"uid": "00000000-0000-0000-0000-000000000000",
                "nickname": "smoke-test", "enterpriseId": ""},
    "auth": {"accessToken": "smoke-fake-token", "refreshToken": "smoke-fake-refresh",
             "expiresAt": 4102444800000, "domain": "copilot.tencent.com"},
    "accounts": [{"uid": "00000000-0000-0000-0000-000000000000", "nickname": "smoke-test"}],
    "allAccounts": [{"uid": "00000000-0000-0000-0000-000000000000", "nickname": "smoke-test"}],
}


def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def smoke_test(timeout_s: int = 120) -> bool:
    """用隔离的 auth 目录 + 临时端口启动 exe，探活后关闭。

    用 CODEBUDDY_AUTH_DIR 指向临时目录，避免读真实登录态（也便于在受限环境下运行）。
    """
    if not EXE.exists():
        print(f"[X] 缺少产物 {EXE}")
        return False

    dist = EXE.parent
    cfg = dist / "config.json"
    tmp_auth = ROOT / "_smoke_auth"
    tmp_auth.mkdir(exist_ok=True)
    (tmp_auth / "smoke.info").write_text(
        json.dumps(FAKE_INFO, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg.write_text(json.dumps({"host": "127.0.0.1", "port": SMOKE_PORT,
                               "api_key": "", "strategy": "failover"},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ, CODEBUDDY_AUTH_DIR=str(tmp_auth))
    print(f"隔离 auth: {tmp_auth}   端口: {SMOKE_PORT}")
    proc = subprocess.Popen([str(EXE)], cwd=str(dist), env=env)
    ok = False
    try:
        for i in range(1, timeout_s + 1):
            time.sleep(1)
            if proc.poll() is not None:
                print(f"  [{i}s] 进程退出 exitcode={proc.returncode}")
                break
            try:
                st, body = _get(f"http://127.0.0.1:{SMOKE_PORT}/health")
                print(f"  [{i}s] /health -> {st} {body[:150]}")
                ok = True
                break
            except Exception:
                pass
        else:
            print(f"  超时：{timeout_s}s 内未监听端口")

        if ok:
            for path in ("/v1/models", "/api/status"):
                try:
                    st, body = _get(f"http://127.0.0.1:{SMOKE_PORT}{path}")
                    print(f"  {path} -> {st}  {body[:200]}")
                except Exception as e:
                    print(f"  {path} 失败: {e}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        cfg.unlink(missing_ok=True)
        shutil.rmtree(tmp_auth, ignore_errors=True)
        print("已清理临时文件")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 CodeBuddy2API 构建产物")
    ap.add_argument("--smoke", action="store_true", help="额外做一次启动冒烟测试")
    args = ap.parse_args()

    icon_ok = verify_icon()
    smoke_ok = True
    if args.smoke:
        print("\n--- 冒烟测试 ---")
        smoke_ok = smoke_test()

    print("\n结果:", "全部通过" if (icon_ok and smoke_ok) else "存在失败项")
    return 0 if (icon_ok and smoke_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
