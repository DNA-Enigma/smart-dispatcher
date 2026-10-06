"""SQLite 状态存储。

给**手机端本地模式**与嵌入式服务用。它存在的理由只有一个，但足够充分：

> 内存实现一重启就没了，而手机应用会被系统杀掉、会被用户划掉、会换设备。
> 没有持久化就没有跨重启的 ``Last-Event-ID`` 重放，而没有重放，
> "退到后台再回来能看到完整进度"这件事就不成立。

**并发策略：单连接 + ``asyncio.Lock``。** 理由不是省事，而是 SQLite 的现实：
它同一时刻只允许一个写者，多连接并发写只会换来 ``SQLITE_BUSY`` 和重试逻辑。
把写入串行化在一个连接上，就从根本上没有争用；代价是写会排队，
而这里写的都是几百字节的事件行，排队可以忽略。

单进程内的串行**不等于**跨进程安全，因此 ``append_event`` 仍然显式开启
``BEGIN IMMEDIATE`` 事务——那一道是给"同一个库文件被第二个进程打开"准备的。

**它不解决的问题**（写下来是为了不被误用）：

* **多进程并发写**：``BEGIN IMMEDIATE`` 会让第二个写者拿到 ``SQLITE_BUSY``，
  这里不做退避重试。部署形态是"一个进程持有这个库"，不是"一个共享库多个服务"。
* **迁移**：只记录 ``schema_version``，不做升级。版本不匹配时**拒绝打开**而不是
  尽力读取——把新库当旧库读会产出看起来正常但字段错位的数据，那比打不开糟糕得多。
* **读取的分页与流式**：``read_events`` 一次性返回，因为事件总量由任务范围决定，
  不会无界。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.events import EventRecord, new_event
from ..core.state import TERMINAL_STATUSES, TaskRecord

# 存储布局的版本。改动任何一张表的结构时都要 +1，并在打开时校验。
# 手机上装好的应用不会因为服务端升级而自动重建本地库——版本标记是唯一能让
# "这个库是什么年代的"变成可判定问题的东西。
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    record     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_listing
    ON tasks (tenant_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    task_id    TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT    NOT NULL,
    ts         TEXT    NOT NULL,
    subtask_id TEXT,
    data       TEXT    NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_prune ON events (ts);

CREATE TABLE IF NOT EXISTS idempotency (
    key       TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    task_id   TEXT NOT NULL,
    PRIMARY KEY (key, tenant_id)
);
"""

_TERMINAL_PLACEHOLDERS = ", ".join("?" for _ in TERMINAL_STATUSES)


class SqliteStateStore:
    """一个 SQLite 文件承载任务、事件与幂等键。

    用法是 ``open → 用 → close``，或用 async context manager::

        async with SqliteStateStore("data/dispatcher.db") as store:
            ...
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        # 所有写操作的串行闸。见模块 docstring：这是为了从根本上消除写争用，
        # 而不是为了弥补 SQLite 的某个缺陷。
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    async def open(self) -> SqliteStateStore:
        if self._conn is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None 关掉 aiosqlite 的隐式事务管理，改由我们显式
        # BEGIN / COMMIT——隐式事务会让 BEGIN IMMEDIATE 报"事务已在进行中"。
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        # WAL 是本地应用库的正解：读不阻塞写，写不阻塞读。
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_DDL)
        await self._check_schema_version(conn)
        self._conn = conn
        return self

    @staticmethod
    async def _check_schema_version(conn: aiosqlite.Connection) -> None:
        cur = await conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        found = int(row[0])
        if found != SCHEMA_VERSION:
            raise RuntimeError(
                f"SQLite 库的 schema_version={found}，本实现期望 {SCHEMA_VERSION}。"
                f"拒绝以错误的布局读写——那会产出看起来正常但字段错位的数据。"
                f"请迁移或换一个库文件：{conn}"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SqliteStateStore:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteStateStore 尚未 open()")
        return self._conn

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    async def create_task(self, record: TaskRecord) -> TaskRecord:
        return await self.put_task(record)

    async def put_task(self, record: TaskRecord) -> TaskRecord:
        conn = self._require_conn()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO tasks (task_id, tenant_id, user_id, status,
                                   created_at, updated_at, record)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    tenant_id  = excluded.tenant_id,
                    user_id    = excluded.user_id,
                    status     = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    record     = excluded.record
                """,
                (
                    record.task_id,
                    record.tenant_id,
                    record.user_id,
                    record.status,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.model_dump_json(),
                ),
            )
        return record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        conn = self._require_conn()
        cur = await conn.execute("SELECT record FROM tasks WHERE task_id = ?", (task_id,))
        row = await cur.fetchone()
        await cur.close()
        return TaskRecord.model_validate_json(row[0]) if row else None

    async def list_tasks(
        self, *, tenant_id: str, user_id: str | None, limit: int
    ) -> list[TaskRecord]:
        conn = self._require_conn()
        # 列表条件走反范式列而不是 JSON：JSON 里的字段没法走索引，
        # 而列表是唯一会被高频调用的查询。
        if user_id is None:
            sql = (
                "SELECT record FROM tasks WHERE tenant_id = ?"
                " ORDER BY created_at DESC LIMIT ?"
            )
            args: tuple[Any, ...] = (tenant_id, limit)
        else:
            sql = (
                "SELECT record FROM tasks WHERE tenant_id = ? AND user_id = ?"
                " ORDER BY created_at DESC LIMIT ?"
            )
            args = (tenant_id, user_id, limit)
        cur = await conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [TaskRecord.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    async def append_event(
        self,
        task_id: str,
        type: str,
        data: dict | None = None,
        *,
        subtask_id: str | None = None,
    ) -> EventRecord:
        conn = self._require_conn()
        async with self._lock:
            # BEGIN IMMEDIATE 立刻取写锁。若用普通的 BEGIN（延迟锁），
            # "读 MAX(seq) → 写"之间存在窗口，另一个进程可以插进来拿到同一个 seq。
            # 而 seq 是重放游标：重复的 seq 会让客户端重放错位。
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE task_id = ?",
                    (task_id,),
                )
                row = await cur.fetchone()
                await cur.close()
                seq = int(row[0])

                rec = new_event(
                    seq=seq, task_id=task_id, type=type, data=data, subtask_id=subtask_id
                )
                await conn.execute(
                    "INSERT INTO events (task_id, seq, type, ts, subtask_id, data)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        seq,
                        rec.type,
                        self._iso(rec.ts),
                        subtask_id,
                        json.dumps(rec.data, ensure_ascii=False),
                    ),
                )
                await conn.execute("COMMIT")
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
        return rec

    @staticmethod
    def _iso(ts: datetime) -> str:
        """统一按 UTC 存 ISO 串。

        ``prune_events`` 用字符串比较时间戳（SQLite 没有原生时间类型），
        而字符串比较只有在所有时间戳都是同一时区、同一格式时才等价于时间比较。
        归一化到 UTC 是让那条比较成立的前提。
        """
        return ts.astimezone(UTC).isoformat()

    async def read_events(
        self, task_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[EventRecord]:
        conn = self._require_conn()
        sql = (
            "SELECT seq, type, ts, subtask_id, data FROM events"
            " WHERE task_id = ? AND seq > ? ORDER BY seq ASC"
        )
        args: list[Any] = [task_id, since_seq]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cur = await conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [
            EventRecord(
                seq=r[0],
                task_id=task_id,
                type=r[1],
                ts=datetime.fromisoformat(r[2]),
                subtask_id=r[3],
                data=json.loads(r[4]),
            )
            for r in rows
        ]

    async def latest_seq(self, task_id: str) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE task_id = ?", (task_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0])

    async def prune_events(self, *, before: datetime, keep_terminal_days: int) -> int:
        """删掉过期事件。**终态任务的事件走另一把尺子。**

        * 非终态任务 / 无任务记录的事件 —— ``before`` 之前的删掉。
        * 已终态任务 —— ``now - keep_terminal_days`` 之前的删掉。

        两把尺子是**互相独立**的，不是取更宽的那个。原因：它们问的是两个不同问题。
        ``before`` 问"这条事件有多旧"，用于清理还在跑的任务留下的过程记录；
        ``keep_terminal_days`` 问"这个任务结束多久了"，用于保留审计账本。
        用 ``max(before, ...)`` 把它们绑在一起会导致一个反直觉的结果：
        ``before`` 落在未来时，本该被保护的终态事件反而一起被删了。

        ``keep_terminal_days=0`` 表示终态事件随任务结束即删——明确但危险，
        因此策略里给的是 180 天（``evolution.retention.run_log_days``），
        让"保留多久"是一个写出来的决定。
        """
        conn = self._require_conn()
        terminal_cutoff = datetime.now(UTC) - timedelta(days=keep_terminal_days)
        # CASE 的两条分支共用一个 DELETE，避免两次扫描与两次加锁。
        # 占位符个数来自模块常量，f-string 里没有任何外部输入进入 SQL。
        sql = (
            "DELETE FROM events WHERE ts < CASE"
            " WHEN task_id IN (SELECT task_id FROM tasks WHERE status IN"
            f" ({_TERMINAL_PLACEHOLDERS}))"
            " THEN ? ELSE ? END"
        )
        args = [*sorted(TERMINAL_STATUSES), self._iso(terminal_cutoff), self._iso(before)]
        async with self._lock:
            cur = await conn.execute(sql, args)
            removed = cur.rowcount or 0
            await cur.close()
        return removed

    # ------------------------------------------------------------------
    # 幂等
    # ------------------------------------------------------------------
    async def get_idempotency(self, key: str, tenant_id: str) -> str | None:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT task_id FROM idempotency WHERE key = ? AND tenant_id = ?",
            (key, tenant_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def put_idempotency(self, key: str, tenant_id: str, task_id: str) -> None:
        conn = self._require_conn()
        async with self._lock:
            await conn.execute(
                "INSERT INTO idempotency (key, tenant_id, task_id) VALUES (?, ?, ?)"
                " ON CONFLICT(key, tenant_id) DO UPDATE SET task_id = excluded.task_id",
                (key, tenant_id, task_id),
            )

    # -- 供自省 ---------------------------------------------------------
    async def event_count(self, task_id: str) -> int:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0])


__all__ = ["SCHEMA_VERSION", "SqliteStateStore"]
