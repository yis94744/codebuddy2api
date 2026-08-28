#!/usr/bin/env python3
"""
test_account_pool.py — 验证账号池(account_pool)核心逻辑。

直接运行：python3 test_account_pool.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from account_pool import AccountPool


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


def test_scan_discovers_accounts():
    """测试：扫描临时目录发现所有 .info 账号，摘要脱敏。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-abcdef-123456")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-ghijkl-789012")
        pool = AccountPool(search_dirs=[d])
        assert len(pool.accounts) == 2, len(pool.accounts)
        assert pool.active() is not None
        s = pool.summary()
        assert s["active_id"] == pool.active_id
        assert len(s["accounts"]) == 2
        # 摘要不应泄露完整 token
        for a in s["accounts"]:
            assert "token-abcdef-123456" not in a["token_prefix"]
            assert "token-ghijkl-789012" not in a["token_prefix"]
            assert a["token_expired"] is False
    print("✅ test_scan_discovers_accounts")


def test_scan_no_deadlock():
    """回归测试：scan() 持锁时调用 summary() 不得死锁（RLock 修复）。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])  # __init__ -> scan() -> summary()
        assert len(pool.summary()["accounts"]) == 1
        pool.scan()  # 再次触发
        assert pool.active_id is not None
    print("✅ test_scan_no_deadlock")


def test_switch_and_resolve_fixed():
    """测试：fixed 策略下切换账号，resolve 返回当前账号凭据。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-b")
        pool = AccountPool(search_dirs=[d])
        first = pool.active_id
        other = next(aid for aid in pool.accounts if aid != first)
        pool.switch(other)
        assert pool.active_id == other
        cred = pool.resolve()
        assert cred is not None
        assert cred.summary()["uid"] == pool.accounts[other].summary()["uid"]
        pool.switch(first)
        assert pool.resolve().summary()["uid"] == pool.accounts[first].summary()["uid"]
    print("✅ test_switch_and_resolve_fixed")


def test_resolve_auto_round_robin():
    """测试：auto 策略按轮询路由到所有启用的账号。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-b")
        pool = AccountPool(search_dirs=[d])
        pool.set_strategy("auto")
        got = {pool.resolve().summary()["uid"] for _ in range(6)}
        assert got == {"uid-1", "uid-2"}, got
    print("✅ test_resolve_auto_round_robin")


def test_set_enabled():
    """测试：禁用账号后 fixed/auto 都只会路由到剩余启用账号。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        _mk_info(d / "b.info", "uid-2", "用户B", "token-b")
        pool = AccountPool(search_dirs=[d])
        disabled_id = pool.active_id
        pool.set_enabled(disabled_id, False)
        s = pool.summary()
        target = next(a for a in s["accounts"] if a["id"] == disabled_id)
        assert target["enabled"] is False
        # fixed：resolve 回退到其他启用账号
        cred = pool.resolve()
        assert cred is not None
        assert cred.summary()["uid"] != pool.accounts[disabled_id].summary()["uid"]
        # auto：只用剩余启用账号
        pool.set_strategy("auto")
        uids = {pool.resolve().summary()["uid"] for _ in range(4)}
        assert uids == {pool.accounts[a].summary()["uid"]
                        for a in pool.accounts if a != disabled_id}, uids
    print("✅ test_set_enabled")


def test_rename_and_add():
    """测试：重命名账号；手动添加 .info；不存在文件报错。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        aid = pool.active_id
        pool.rename(aid, "新名字")
        assert pool.accounts[aid].name == "新名字"
        p2 = d / "c.info"
        _mk_info(p2, "uid-3", "用户C", "token-c")
        added = pool.add(str(p2), "手动账号")
        assert added["name"] == "手动账号"
        assert len(pool.accounts) == 2
        # add 不存在的文件
        try:
            pool.add(str(d / "nope.info"))
            assert False, "应抛出 ValueError"
        except ValueError:
            pass
    print("✅ test_rename_and_add")


def test_set_strategy_validation():
    """测试：非法策略名报错。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        pool = AccountPool(search_dirs=[d])
        pool.set_strategy("auto")
        assert pool.strategy == "auto"
        try:
            pool.set_strategy("bogus")
            assert False, "应抛出 ValueError"
        except ValueError:
            pass
    print("✅ test_set_strategy_validation")


def test_invalid_info_tolerated():
    """测试：损坏的 .info 文件被容忍，不阻断其他账号。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _mk_info(d / "a.info", "uid-1", "用户A", "token-a")
        (d / "bad.info").write_text("{not json", encoding="utf-8")
        pool = AccountPool(search_dirs=[d])
        assert len(pool.accounts) == 2
        s = pool.summary()
        bad = next(a for a in s["accounts"] if a["file"] == "bad.info")
        assert bad["token_expired"] is True
        good = next(a for a in s["accounts"] if a["file"] == "a.info")
        assert good["token_expired"] is False
    print("✅ test_invalid_info_tolerated")


if __name__ == "__main__":
    test_scan_discovers_accounts()
    test_scan_no_deadlock()
    test_switch_and_resolve_fixed()
    test_resolve_auto_round_robin()
    test_set_enabled()
    test_rename_and_add()
    test_set_strategy_validation()
    test_invalid_info_tolerated()
    print(f"\n🎉 All {8} tests passed!")
