# -*- coding: utf-8 -*-
"""CodeBuddy CN 桌面端账号导入器。

用户点击「添加账号」→ 本模块打开 CodeBuddy CN 登录 → 用户完成登录后，
自动从 CodeBuddy CN 的 state.vscdb 解密新凭据，生成 .info 文件并加入账号池。

凭据存储位置：
  DB   : %APPDATA%\\CodeBuddy CN\\User\\globalStorage\\state.vscdb
  KEY  : secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}
  主密钥: %APPDATA%\\CodeBuddy CN\\Local State → os_crypt.encrypted_key (DPAPI)
  加密格式: Chromium OSCrypt v10/v11 → AES-256-GCM
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

import converter  # 复用 CredentialManager 的 header 构造等

# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------

def cn_appdata() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "CodeBuddy CN"


def cn_vscdb() -> Optional[Path]:
    p = cn_appdata() / "User" / "globalStorage" / "state.vscdb"
    return p if p.exists() else None


def cn_local_state() -> Optional[Path]:
    p = cn_appdata() / "Local State"
    return p if p.exists() else None


def find_cn_exe() -> Optional[str]:
    """定位 CodeBuddy CN 桌面端可执行文件。"""
    candidates = [
        os.environ.get("CODEBUDDY_CN_EXE", ""),
        r"E:\CodeBuddy CN\CodeBuddy CN.exe",
        str(Path.home() / "AppData" / "Local" / "Programs" / "CodeBuddy CN" / "CodeBuddy CN.exe"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "CodeBuddy" / "CodeBuddy.exe"),
        r"C:\Program Files\CodeBuddy CN\CodeBuddy CN.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # 开始菜单快捷方式兜底
    menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    for lnk in menu.rglob("*CodeBuddy*.lnk"):
        return str(lnk)
    return None


# ---------------------------------------------------------------------------
# DPAPI 解密（通过 PowerShell 的 ProtectedData，规避 ctypes 堆损坏问题）
# ---------------------------------------------------------------------------

def _dpapi_unprotect(b64_data: str) -> bytes:
    """把 base64 的 DPAPI 密文交给 PowerShell 解密，返回明文。"""
    import subprocess as sp
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Security;"
        "$d=[Convert]::FromBase64String('%s');"
        "$u=[Security.Cryptography.ProtectedData]::Unprotect($d,$null,'CurrentUser');"
        "[Convert]::ToBase64String($u)" % b64_data
    )
    r = sp.run(["powershell", "-NoProfile", "-Command", script],
               capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"DPAPI 解密失败: {r.stderr.decode('utf-8','replace')[:200]}")
    out = r.stdout.decode("utf-8", "replace").strip().splitlines()
    if not out:
        raise RuntimeError("DPAPI 解密无输出")
    return base64.b64decode(out[-1])


def _decrypt_secret(encrypted: bytes, aes_key: bytes) -> bytes:
    """CodeBuddy CN 的 OSCrypt 解密。

    实测（2026-08）：raw = 'v10'(3) + nonce(12) + ciphertext+tag。
    密文开头是 'pk'（v10pk 只是巧合，pk 属于密文），故用标准 3+12 偏移。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if encrypted[:3] not in (b"v10", b"v11"):
        raise ValueError(f"未知加密前缀: {encrypted[:8]!r}")
    nonce = encrypted[3:15]
    ct = encrypted[15:]
    return AESGCM(aes_key).decrypt(nonce, ct, None)


def _read_cn_secret() -> Optional[dict]:
    """从 CodeBuddy CN 读取并解密 access token。

    返回解码后的 JSON（含 account/auth 信息）；失败返回 None。
    """
    db = cn_vscdb()
    ls = cn_local_state()
    if not db or not ls:
        return None
    # 1) 取 DPAPI 加密的 AES 主密钥
    try:
        with open(ls, "r", encoding="utf-8") as f:
            state = json.load(f)
        ek = (state.get("os_crypt") or {}).get("encrypted_key", "")
        if not ek:
            return None
        raw = base64.b64decode(ek)
        if raw[:5] != b"DPAPI":
            return None
        aes_key = _dpapi_unprotect(base64.b64encode(raw[5:]).decode())
    except Exception:
        return None
    # 2) 读 vscdb 里的密文
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?",
                    ('secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}',))
        row = cur.fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    raw_val = row[0]
    # 值可能是 JSON Buffer 包装（{"type":"Buffer","data":[...]}）或直接 bytes
    if isinstance(raw_val, str):
        try:
            obj = json.loads(raw_val)
            if isinstance(obj, dict) and obj.get("type") == "Buffer" and isinstance(obj.get("data"), list):
                encrypted = bytes(obj["data"])
            else:
                encrypted = base64.b64decode(raw_val)
        except Exception:
            encrypted = base64.b64decode(raw_val)
    elif isinstance(raw_val, bytes):
        encrypted = raw_val
    else:
        return None
    # 3) AES-GCM 解密
    try:
        plain = _decrypt_secret(encrypted, aes_key)
        return json.loads(plain.decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 账号导入
# ---------------------------------------------------------------------------

def _extract_account(secret: dict) -> Optional[dict]:
    """从解密出的 secret 提取标准 .info 结构。"""
    if not isinstance(secret, dict):
        return None
    # 兼容两种形态：直接含 account/auth，或套了一层
    data = secret.get("data") if isinstance(secret.get("data"), dict) else secret
    account = data.get("account") or data.get("accounts") or {}
    if isinstance(account, list):
        account = account[0] if account else {}
    auth = data.get("auth") or {}
    if not account or not auth:
        # 尝试从 auth.accounts 取 account 信息
        auth_accts = auth.get("accounts") or []
        if auth_accts and not account:
            account = auth_accts[0]
    if not account.get("uid") or not auth.get("accessToken"):
        return None
    return {"account": account, "auth": auth}


def open_cn_login() -> str:
    """打开 CodeBuddy CN 桌面端（若未运行）。"""
    exe = find_cn_exe()
    if not exe:
        return "未找到 CodeBuddy CN 安装，请手动打开桌面端登录"
    try:
        subprocess.Popen([exe], close_fds=True)
        return "已打开 CodeBuddy CN，请完成登录"
    except Exception as e:
        return f"打开 CodeBuddy CN 失败: {e}"


def detect_new_accounts(pool) -> dict:
    """检测 CodeBuddy CN 中是否有当前账号池里不存在的账号。

    返回 {found: bool, info: 解密后的 secret 或 None, reason: str}
    """
    secret = _read_cn_secret()
    if secret is None:
        return {"found": False, "info": None, "reason": "未检测到 CodeBuddy CN 凭据（可能未登录或读取失败）"}
    extracted = _extract_account(secret)
    if extracted is None:
        return {"found": False, "info": None, "reason": "凭据格式异常"}
    uid = (extracted["account"] or {}).get("uid")
    # 与现有账号池比对
    if pool is not None:
        for acct in pool.accounts.values():
            try:
                s = acct.credential.summary()
                if s.get("uid") == uid:
                    return {"found": False, "info": None,
                            "reason": f"账号已存在（uid {uid}）"}
            except Exception:
                continue
    return {"found": True, "info": secret, "reason": "发现新账号"}


def import_account_from_secret(secret: dict, pool, name: Optional[str] = None) -> dict:
    """把解密出的 secret 写入 .info 并加入账号池。"""
    extracted = _extract_account(secret)
    if extracted is None:
        raise ValueError("凭据格式异常，无法导入")
    account = extracted["account"]
    auth = extracted["auth"]
    uid = account.get("uid", "")
    nickname = account.get("nickname") or account.get("displayName") or account.get("label") or f"cn-{uid[:8]}"
    # 写入 auth 目录
    auth_dir = converter.auth_dirs()[0]
    auth_dir.mkdir(parents=True, exist_ok=True)
    path = auth_dir / f"{nickname}.info"
    # 防覆盖：若文件名已存在，加后缀
    counter = 1
    while path.exists():
        path = auth_dir / f"{nickname}-{counter}.info"
        counter += 1
    payload = {"account": account, "auth": auth}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # 加入账号池
    added = pool.add(str(path), name or nickname)
    return {"file": path.name, "account": added}
