"""SQLite 版演化存储：运行日志、建议、策略版本。

**为什么自进化特别需要持久存储**：分析窗口是 7 天。内存实现在一次重启后就空了，
于是"每周跑一次分析"这件事永远看不到一周的数据——它每次看到的都是"重启以来的"
那几条，样本量永远达不到 ``min_sample_size``，整套机制静默地不工作。

它**不解决**的：多进程写（SQLite 单写者）、迁移（版本不匹配时拒绝打开而不是
尽力读）。与 ``SqliteStateStore`` 相同的取舍，理由也相同。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import DispatcherError
from ..core.runlog import HumanSignal, RunLog

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_logs (
    task_id        TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    status         TEXT NOT NULL,
    doc            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_logs_tenant_time ON run_logs(tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_run_logs_policy ON run_logs(policy_version);
CREATE TABLE IF NOT EXISTS suggestions (
    suggestion_id TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    scope_level   TEXT,
    created_at    TEXT,
    doc           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    doc            TEXT NOT NULL
);
"""


class SqliteEvolutionStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._lock = asyncio.Lock()
        cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row[0]) != SCHEMA_VERSION:
            # 宁可拒绝打开，也不要按旧布局误读新数据——那会产出一堆看起来正常的错数据。
            raise DispatcherError(
                "policy_violation",
                f"演化库 schema 版本不匹配：文件是 {row[0]}，本代码期望 {SCHEMA_VERSION}。"
                f"请迁移或换一个库文件。",
            )

    async def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    async def append_run_log(self, log: RunLog) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO run_logs(task_id, tenant_id, user_id, policy_version,"
                " started_at, status, doc) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id) DO UPDATE SET doc=excluded.doc, status=excluded.status",
                (log.task_id, log.tenant_id, log.user_id, log.policy_version,
                 log.started_at.isoformat(), log.status, json.dumps(log.to_wire(), ensure_ascii=False)),
            )

    async def list_run_logs(
        self, *, tenant_id: str, since: datetime | None = None, until: datetime | None = None,
        policy_version: str | None = None, limit: int | None = None,
    ) -> list[RunLog]:
        sql = "SELECT doc FROM run_logs WHERE tenant_id = ?"
        args: list[Any] = [tenant_id]
        if since is not None:
            sql += " AND started_at >= ?"
            args.append(since.isoformat())
        if until is not None:
            sql += " AND started_at <= ?"
            args.append(until.isoformat())
        if policy_version is not None:
            sql += " AND policy_version = ?"
            args.append(policy_version)
        sql += " ORDER BY started_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cur = self._conn.execute(sql, args)
        return [RunLog.model_validate_json(r[0]) for r in cur.fetchall()]

    async def attach_human_signal(self, task_id: str, signal: HumanSignal) -> bool:
        async with self._lock:
            cur = self._conn.execute("SELECT doc FROM run_logs WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
            if row is None:
                return False
            doc = json.loads(row[0])
            doc["human_signal"] = signal.model_dump(mode="json")
            self._conn.execute(
                "UPDATE run_logs SET doc = ? WHERE task_id = ?",
                (json.dumps(doc, ensure_ascii=False), task_id),
            )
        return True

    async def prune_run_logs(self, *, before: datetime) -> int:
        async with self._lock:
            cur = self._conn.execute("DELETE FROM run_logs WHERE started_at < ?", (before.isoformat(),))
        return cur.rowcount or 0

    # ------------------------------------------------------------------
    async def put_suggestion(self, suggestion: dict) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO suggestions(suggestion_id, status, scope_level, created_at, doc)"
                " VALUES(?,?,?,?,?) ON CONFLICT(suggestion_id) DO UPDATE SET"
                " status=excluded.status, doc=excluded.doc",
                (suggestion["suggestion_id"], suggestion.get("status", "proposed"),
                 (suggestion.get("scope") or {}).get("level"),
                 suggestion.get("created_at"),
                 json.dumps(suggestion, ensure_ascii=False)),
            )

    async def get_suggestion(self, suggestion_id: str) -> dict | None:
        cur = self._conn.execute("SELECT doc FROM suggestions WHERE suggestion_id = ?", (suggestion_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    async def list_suggestions(
        self, *, status: str | None = None, scope_level: str | None = None, limit: int = 50
    ) -> list[dict]:
        sql = "SELECT doc FROM suggestions WHERE 1=1"
        args: list[Any] = []
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        if scope_level is not None:
            sql += " AND scope_level = ?"
            args.append(scope_level)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [json.loads(r[0]) for r in self._conn.execute(sql, args).fetchall()]

    async def update_suggestion(self, suggestion_id: str, patch: dict) -> dict | None:
        cur = await self.get_suggestion(suggestion_id)
        if cur is None:
            return None
        merged = {**cur, **patch}
        await self.put_suggestion(merged)
        return merged

    # ------------------------------------------------------------------
    async def put_version(self, version: dict) -> None:
        async with self._lock:
            self._conn.execute(
                "INSERT INTO policy_versions(policy_version, status, created_at, doc)"
                " VALUES(?,?,?,?) ON CONFLICT(policy_version) DO UPDATE SET"
                " status=excluded.status, doc=excluded.doc",
                (version["policy_version"], version.get("status", "active"),
                 version.get("created_at", datetime.now(UTC).isoformat()),
                 json.dumps(version, ensure_ascii=False)),
            )

    async def get_version(self, version_id: str) -> dict | None:
        cur = self._conn.execute("SELECT doc FROM policy_versions WHERE policy_version = ?", (version_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    async def list_versions(self, *, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT doc FROM policy_versions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    async def active_version(self) -> dict | None:
        row = self._conn.execute(
            "SELECT doc FROM policy_versions WHERE status IN ('active','canary')"
            " ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0]) if row else None

    async def set_active(self, version_id: str) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE policy_versions SET status='superseded'"
                " WHERE status IN ('active','canary') AND policy_version != ?", (version_id,)
            )


__all__ = ["SqliteEvolutionStore"]
