"""已发放令牌的 SQLite 登记簿。

``TokenRegistry`` 此前只有内存实现，直接后果是**每次重启所有子令牌持有者当场
401**（docs/12-deployment.md 第 7 节 #4）。这个适配器就是那条"真要跨进程，得把它
下沉到存储层（一张 token 表）"的路。

三个刻意的选择：

* **只落摘要，不落明文**。``token_id``/``digest``/身份/签发时间，没有令牌本身。
  登记的用途是"按摘要查得到身份"，而按摘要查不需要明文——因此**重启后令牌照常
  可用**（客户端手里的明文一算摘要就能查到这一行），而库文件被读走也拿不到任何
  可用凭据。这是这套设计最值钱的一点，落盘时不该把它丢掉。
* **自己的 ``tokens_meta`` 表，不碰 ``meta``**。同目录的 ``dispatcher.db`` 由
  ``SqliteStateStore`` 与 ``SqliteEvolutionStore`` 共用，两者都在读写
  ``meta.schema_version``。第三个写入者往同一个 key 上写自己的版本号，
  会让"版本不匹配就拒绝打开"这道闸变成随机数——谁最后写谁说了算。
* **同步 ``sqlite3``，不是 ``aiosqlite``**。与 ``SqliteEvolutionStore`` 同口径：
  令牌的读写是"签发/撤销/启动时载入"这几次，量小且不成热点。代价是调用点会
  短暂阻塞事件循环——把一个每次请求都要走的路径放在这里不可接受，而它恰好不是。

它**不解决**的与另外两个 sqlite 适配器相同：多进程写（SQLite 单写者）、迁移
（版本不匹配时拒绝打开而不是尽力读）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..interface.tokens import IssuedToken

SCHEMA_VERSION = 1

#: 版本号存在**独立**的表里，避开 ``SqliteStateStore`` 的 ``meta``。见模块 docstring。
_DDL = """
CREATE TABLE IF NOT EXISTS tokens_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issued_tokens (
    token_id   TEXT PRIMARY KEY,
    digest     TEXT NOT NULL UNIQUE,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SqliteTokenStore:
    """``token_id → (摘要, 身份, 签发时间)`` 的一张表，外加载入/写入/删除。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # timeout 是 SQLITE_BUSY 的等待上限（秒）。同一个文件还被状态存储的
        # 写事务用着，令牌签发撞上事件写入时应当等一会儿而不是当场失败。
        self._conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._check_schema_version()

    def _check_schema_version(self) -> None:
        cur = self._conn.execute("SELECT value FROM tokens_meta WHERE key = 'schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO tokens_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        found = int(row[0])
        if found != SCHEMA_VERSION:
            raise RuntimeError(
                f"令牌库的 schema_version={found}，本实现期望 {SCHEMA_VERSION}。"
                f"拒绝以错误的布局读写——那会产出看起来正常但字段错位的凭据数据。"
                f"请迁移或换一个库文件：{self._path}"
            )

    # ------------------------------------------------------------------
    def load(self) -> list[tuple[IssuedToken, str]]:
        """全部未撤销的令牌，返回 ``(记录, 摘要)``。**没有明文可返回**。"""
        rows = self._conn.execute(
            "SELECT token_id, digest, tenant_id, user_id, created_at FROM issued_tokens"
        ).fetchall()
        return [
            (
                IssuedToken(
                    token_id=r[0],
                    tenant_id=r[2],
                    user_id=r[3],
                    created_at=datetime.fromisoformat(r[4]),
                ),
                r[1],
            )
            for r in rows
        ]

    def put(self, record: IssuedToken, digest: str) -> None:
        self._conn.execute(
            "INSERT INTO issued_tokens(token_id, digest, tenant_id, user_id, created_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(token_id) DO UPDATE SET"
            " digest=excluded.digest, tenant_id=excluded.tenant_id,"
            " user_id=excluded.user_id, created_at=excluded.created_at",
            (
                record.token_id,
                digest,
                record.tenant_id,
                record.user_id,
                record.created_at.astimezone(UTC).isoformat(),
            ),
        )

    def delete(self, token_id: str) -> bool:
        """撤销。返回是否真的删掉了一行（``False`` 交给端点报 404）。"""
        cur = self._conn.execute("DELETE FROM issued_tokens WHERE token_id = ?", (token_id,))
        return bool(cur.rowcount)

    def close(self) -> None:
        self._conn.close()


__all__ = ["SCHEMA_VERSION", "SqliteTokenStore"]
