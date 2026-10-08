"""令牌登记簿的持久化（``SqliteTokenStore`` + ``TokenRegistry.attach_store``）。

端到端那条在 ``test_restart_persistence.py``（重启后子令牌仍有效）。这里钉的是
几个**单元级**的决定，端到端测不出它们：

* 不挂后端时行为与改动前**一模一样**（向后兼容）；
* 落盘只有摘要，没有明文；
* 撤销真的删行，重开不复活；
* 版本不匹配拒绝打开（与另外两个 sqlite 适配器同口径）；
* **不碰 ``meta`` 表**——同目录的 ``dispatcher.db`` 由状态存储与演化存储共用，
  两边都在读写 ``meta.schema_version``，第三个写入者会让"版本不匹配就拒绝打开"
  这道闸变成"谁最后写谁说了算"。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from dispatcher.adapters.sqlite_state import SqliteStateStore
from dispatcher.adapters.sqlite_tokens import SCHEMA_VERSION, SqliteTokenStore
from dispatcher.interface.tokens import TokenRegistry


# --------------------------------------------------------------- 向后兼容
def test_registry_without_a_store_behaves_as_before():
    """不挂后端 = 改动前的行为，逐条对齐（issue / resolve / revoke / records）。"""
    reg = TokenRegistry()
    record, token = reg.issue(tenant_id="t_acme", user_id="u_alice")

    assert reg.resolve(token) == record
    assert reg.resolve("nope") is None
    assert [r.token_id for r in reg.records()] == [record.token_id]
    assert reg.revoke(record.token_id) is True
    assert reg.revoke(record.token_id) is False  # 重复撤销仍是 False（端点报 404）
    assert reg.resolve(token) is None


# --------------------------------------------------------------- 存储本身
def test_round_trip_survives_reopen(tmp_path: Path):
    db = tmp_path / "tokens.db"
    reg = TokenRegistry()
    store = SqliteTokenStore(db)
    reg.attach_store(store)
    try:
        record, token = reg.issue(tenant_id="t_acme", user_id="u_alice")
    finally:
        store.close()

    # ---- 重开：新进程的等价物 ----
    reopened = SqliteTokenStore(db)
    try:
        loaded = {r.token_id: (r, digest) for r, digest in reopened.load()}
        assert record.token_id in loaded
        got, digest = loaded[record.token_id]
        assert (got.tenant_id, got.user_id) == ("t_acme", "u_alice")
        assert digest == hashlib.sha256(token.encode()).hexdigest()
        # 明文不在库里——能被读回来的只有摘要，而摘要足够认出这枚令牌
        assert token.encode() not in db.read_bytes()
    finally:
        reopened.close()


def test_attach_store_restores_resolution(tmp_path: Path):
    """挂上库之后，**重启前签发的明文令牌**照样能解析出身份。

    这是"没存明文怎么还能认出来"的答案：认令牌只用到摘要，明文一直在客户端手里。
    """
    db = tmp_path / "tokens.db"
    first = TokenRegistry()
    store = SqliteTokenStore(db)
    first.attach_store(store)
    record, token = first.issue(tenant_id="t_acme", user_id="u_alice")
    store.close()

    restarted = TokenRegistry()
    reopened = SqliteTokenStore(db)
    try:
        restarted.attach_store(reopened)
        resolved = restarted.resolve(token)
        assert resolved is not None
        assert resolved.token_id == record.token_id
        assert resolved.created_at == record.created_at
    finally:
        reopened.close()


def test_revoke_deletes_the_row(tmp_path: Path):
    """撤销必须落盘——否则"撤销"在重启后复活，等于没撤。"""
    db = tmp_path / "tokens.db"
    reg = TokenRegistry()
    store = SqliteTokenStore(db)
    reg.attach_store(store)
    record, token = reg.issue(tenant_id="t_acme", user_id="u_alice")
    assert reg.revoke(record.token_id) is True
    store.close()

    reopened = SqliteTokenStore(db)
    try:
        assert reopened.load() == []
    finally:
        reopened.close()


def test_refuses_to_open_a_newer_schema(tmp_path: Path):
    """版本不匹配就拒绝打开，不做"尽力读取"（与 SqliteStateStore 同口径）。

    把新布局当旧布局读会产出**看起来正常但字段错位**的数据，而这里错位的是
    凭据的归属——那比打不开糟糕得多。
    """
    db = tmp_path / "tokens.db"
    SqliteTokenStore(db).close()

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tokens_meta SET value = ? WHERE key = 'schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="schema_version"):
        SqliteTokenStore(db)


def test_does_not_touch_the_state_stores_meta_table(tmp_path: Path):
    """令牌库用自己的 ``tokens_meta``，**不碰状态存储的 ``meta``**。

    同一个 ``dispatcher.db`` 里，``SqliteStateStore`` 与 ``SqliteEvolutionStore``
    都在读写 ``meta.schema_version``。第三个写入者往同一个 key 上写自己的版本号，
    会让"版本不匹配就拒绝打开"失效。这里机械地钉住：两个库共处一文件，
    互不干扰，且状态存储的版本号没被动过。
    """
    db = tmp_path / "dispatcher.db"
    # 状态存储先建库并写下它自己的 schema_version
    import asyncio

    async def _init_state() -> None:
        async with SqliteStateStore(db):
            pass

    asyncio.run(_init_state())

    tok = SqliteTokenStore(db)
    reg = TokenRegistry()
    reg.attach_store(tok)
    reg.issue(tenant_id="t_acme", user_id="u_alice")
    tok.close()

    conn = sqlite3.connect(db)
    try:
        (state_ver,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        (token_ver,) = conn.execute(
            "SELECT value FROM tokens_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()

    # 两张表各自有版本号，值都对——而不是被对方覆盖成一个数
    assert state_ver == "1"
    assert token_ver == str(SCHEMA_VERSION)

    # 状态存储仍然打得开（它的 meta 没被搅乱）
    async def _reopen() -> None:
        async with SqliteStateStore(db) as s:
            assert await s.get_task("nope") is None

    asyncio.run(_reopen())
