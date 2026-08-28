#!/usr/bin/env python3
"""
test_ui_admin.py — 验证 Web 管理面板(ui_admin)的 API 路由。

直接运行：python3 test_ui_admin.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import converter
from account_pool import AccountPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ui_admin import setup as ui_setup


def _mk_info(path: Path, uid: str, nickname: str, token: str,
             expires_in_ms: int = 3600_000):
    path.write_text(json.dumps({
        "auth": {
            "accessToken": token,
            "expiresAt": int(time.time() * 1000) + expires_in_ms,
            "refreshToken": "rt-" + uid,
            "domain": "www.codebuddy.cn",
        },
        "account": {
            "uid": uid,
            "nickname": nickname,
            "enterpriseId": "",
            "enterpriseName": None,
        },
    }, ensure_ascii=False), encoding="utf-8")


def _make_app():
    app = FastAPI()
    ui_setup(app, with_ui=False)
    return app


def test_list_accounts():
    """测试：/api/accounts 返回账号池摘要。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-b")
        pool = AccountPool(search_dirs=[d])
        old = converter.CONFIG.get("pool")
        converter.CONFIG["pool"] = pool
        try:
            with TestClient(_make_app()) as client:
                r = client.get("/api/accounts")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["strategy"] == "fixed"
                assert len(body["accounts"]) == 2
                assert body["active_id"] == pool.active_id
        finally:
            converter.CONFIG["pool"] = old
    print("✅ test_list_accounts")


def test_switch_account():
    """测试：切换账号；切换不存在的账号返回 404。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-b")
        pool = AccountPool(search_dirs=[d])
        old = converter.CONFIG.get("pool")
        converter.CONFIG["pool"] = pool
        try:
            with TestClient(_make_app()) as client:
                other = next(aid for aid in pool.accounts if aid != pool.active_id)
                r = client.post(f"/api/accounts/{other}/switch")
                assert r.status_code == 200, r.text
                assert pool.active_id == other
                r = client.post("/api/accounts/nonexistent/switch")
                assert r.status_code == 404, r.text
        finally:
            converter.CONFIG["pool"] = old
    print("✅ test_switch_account")


def test_config_get_and_update():
    """测试：/api/config 读取与热更新（desensitize）。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        old_pool = converter.CONFIG.get("pool")
        old_des = converter.CONFIG.get("desensitize")
        converter.CONFIG["pool"] = pool
        converter.CONFIG["desensitize"] = False
        try:
            with TestClient(_make_app()) as client:
                r = client.get("/api/config")
                assert r.status_code == 200, r.text
                assert r.json()["desensitize"] is False
                r = client.put("/api/config", json={"desensitize": True})
                assert r.status_code == 200, r.text
                assert r.json()["desensitize"] is True
                assert converter.CONFIG["desensitize"] is True
        finally:
            converter.CONFIG["pool"] = old_pool
            converter.CONFIG["desensitize"] = old_des
    print("✅ test_config_get_and_update")


def test_logs_and_status_endpoints():
    """测试：/api/logs、/api/logs/stats、/api/status 正常响应。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        old = converter.CONFIG.get("pool")
        converter.CONFIG["pool"] = pool
        try:
            with TestClient(_make_app()) as client:
                r = client.get("/api/logs")
                assert r.status_code == 200, r.text
                assert "items" in r.json()
                r = client.get("/api/logs/stats")
                assert r.status_code == 200, r.text
                r = client.get("/api/status")
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "ok"
        finally:
            converter.CONFIG["pool"] = old
    print("✅ test_logs_and_status_endpoints")


def test_api_key_auth():
    """测试：设置 API key 后，未携带/错误 key 返回 401。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        old_pool = converter.CONFIG.get("pool")
        old_key = converter.CONFIG.get("api_key")
        converter.CONFIG["pool"] = pool
        converter.CONFIG["api_key"] = "secret"
        try:
            with TestClient(_make_app()) as client:
                r = client.get("/api/accounts")
                assert r.status_code == 401, r.text
                r = client.get("/api/accounts",
                               headers={"Authorization": "Bearer wrong"})
                assert r.status_code == 401, r.text
                r = client.get("/api/accounts",
                               headers={"Authorization": "Bearer secret"})
                assert r.status_code == 200, r.text
        finally:
            converter.CONFIG["pool"] = old_pool
            converter.CONFIG["api_key"] = old_key
    print("✅ test_api_key_auth")


def test_import_account():
    """测试：导入账号的校验与成功路径。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        old = converter.CONFIG.get("pool")
        converter.CONFIG["pool"] = pool
        try:
            with TestClient(_make_app()) as client:
                r = client.post("/api/accounts/import", json={})
                assert r.status_code == 400, r.text
                r = client.post("/api/accounts/import",
                                json={"path": str(d / "nope.info")})
                assert r.status_code == 400, r.text
                p = d / "c.info"
                _mk_info(p, "uid-3", "用户C", "token-c")
                r = client.post("/api/accounts/import",
                                json={"path": str(p), "name": "手动"})
                assert r.status_code == 200, r.text
                assert r.json()["name"] == "手动"
                assert len(pool.accounts) == 2
        finally:
            converter.CONFIG["pool"] = old
    print("✅ test_import_account")


if __name__ == "__main__":
    test_list_accounts()
    test_switch_account()
    test_config_get_and_update()
    test_logs_and_status_endpoints()
    test_api_key_auth()
    test_import_account()
    print(f"\n🎉 All {6} tests passed!")
