"""SQLite 状态存储。

两类断言：

1. **它自己该对的事**——尤其是 ``seq`` 的单调无洞（它是重放游标）、
   跨重启持久化（这是它相对内存实现的全部意义）、终态任务事件的保护。
2. **它与内存实现行为一致**——用同一套场景跑两个实现。这条比第一类更值钱：
   存储是可替换的，如果两个实现在某个边界上分岔，那么"换存储不用改上层"
   这句话就是假的。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.adapters.sqlite_state import SCHEMA_VERSION, SqliteStateStore
from dispatcher.core.contract import Complexity, RouteDecision, TaskProfile, Urgency
from dispatcher.core.guard import RawDecision, apply_guard
from dispatcher.core.policy import Policy
from dispatcher.core.registry import HandlerRegistry
from dispatcher.core.state import TaskRecord


def sample_task(task_id: str = "task_1", **over) -> TaskRecord:
    base = dict(
        task_id=task_id,
        tenant_id="default",
        user_id="u_1",
        status="running",
        profile=TaskProfile(
            task_type="chat.explain",
            modality=["text"],
            complexity=Complexity(score=0.2),
            urgency=Urgency(level="low"),
            recommended_mode="sync",
            confidence=0.9,
        ),
        raw_decision=RawDecision(
            route_id="direct_answer", model_tier="cheap", tool_set=["query_ledger"],
            max_cost=0.5, confidence=0.7,
        ),
    )
    base.update(over)
    return TaskRecord(**base)


@pytest.fixture
async def store(tmp_path):
    s = SqliteStateStore(tmp_path / "state.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
async def test_task_round_trip_preserves_everything(store):
    """任务记录必须**逐字段**还原，包括只用于内部归因的遥测字段。

    那些字段（raw_decision、evaluation_meta）不进快照，但 04 自进化要用它们
    做归因。丢一个字段不会报错，只会让某类分析永远算不出来。
    """
    rec = sample_task()
    await store.put_task(rec)
    back = await store.get_task("task_1")

    assert back is not None
    assert back.model_dump() == rec.model_dump()
    # 特别确认那个 stdlib dataclass 字段——它是序列化最脆弱的一环
    assert isinstance(back.raw_decision, RawDecision)
    assert back.raw_decision.tool_set == ["query_ledger"]
    assert back.profile is not None and back.profile.task_type == "chat.explain"


async def test_get_missing_task_returns_none(store):
    assert await store.get_task("nope") is None


async def test_put_task_is_an_upsert(store):
    """同一个 task_id 再写一次应当覆盖，而不是抛主键冲突。

    上层会反复 ``put_task`` 推进同一个任务的状态，这是主路径。
    """
    await store.put_task(sample_task())
    await store.put_task(sample_task(status="succeeded"))
    back = await store.get_task("task_1")
    assert back.status == "succeeded"


async def test_list_tasks_filters_by_tenant_and_user(store):
    for i, (tenant, user) in enumerate(
        [("t1", "u1"), ("t1", "u2"), ("t2", "u1"), ("t1", "u1")]
    ):
        await store.put_task(
            sample_task(f"task_{i}", tenant_id=tenant, user_id=user,
                        created_at=datetime.now(UTC) + timedelta(seconds=i))
        )

    only_t1 = await store.list_tasks(tenant_id="t1", user_id=None, limit=10)
    assert {t.task_id for t in only_t1} == {"task_0", "task_1", "task_3"}

    t1_u1 = await store.list_tasks(tenant_id="t1", user_id="u1", limit=10)
    assert {t.task_id for t in t1_u1} == {"task_0", "task_3"}


async def test_list_tasks_is_newest_first_and_honors_limit(store):
    for i in range(5):
        await store.put_task(
            sample_task(f"task_{i}", created_at=datetime.now(UTC) + timedelta(minutes=i))
        )
    rows = await store.list_tasks(tenant_id="default", user_id=None, limit=3)
    assert [t.task_id for t in rows] == ["task_4", "task_3", "task_2"]


# ---------------------------------------------------------------------------
# 事件：seq 是重放游标
# ---------------------------------------------------------------------------
async def test_append_event_numbers_from_one(store):
    a = await store.append_event("t", "task.created")
    b = await store.append_event("t", "profile.ready", {"task_type": "chat.explain"})
    assert (a.seq, b.seq) == (1, 2)
    assert b.data == {"task_type": "chat.explain"}


async def test_seq_is_gapless_under_concurrent_appends(store):
    """并发追加必须正好得到 1..N，不重不漏。

    这条是整个持久层的核心断言：``seq`` 是客户端重放游标，重复会让重放错位，
    跳号会让"我没收到第 5 条"变成一个无法判定的问题。
    """
    n = 60
    recs = await asyncio.gather(
        *(store.append_event("t", "subtask.progress", {"i": i}) for i in range(n))
    )
    seqs = sorted(r.seq for r in recs)
    assert seqs == list(range(1, n + 1))
    assert len(set(seqs)) == n


async def test_seq_is_per_task(store):
    """不同任务的 seq 各自从 1 开始——它是任务内的游标，不是全局序号。"""
    await store.append_event("a", "task.created")
    await store.append_event("a", "profile.ready")
    first_b = await store.append_event("b", "task.created")
    assert first_b.seq == 1


async def test_read_events_since_seq_and_limit(store):
    for i in range(5):
        await store.append_event("t", "subtask.progress", {"i": i})

    all_rows = await store.read_events("t")
    assert [e.seq for e in all_rows] == [1, 2, 3, 4, 5]

    resumed = await store.read_events("t", since_seq=3)
    assert [e.seq for e in resumed] == [4, 5], "重放必须从 after 开始，不能重复已收到的"

    limited = await store.read_events("t", since_seq=1, limit=2)
    assert [e.seq for e in limited] == [2, 3]


async def test_read_events_keeps_subtask_id_and_timestamp(store):
    await store.append_event("t", "subtask.started", {"attempt": 1}, subtask_id="extract")
    rows = await store.read_events("t")
    assert rows[0].subtask_id == "extract"
    assert rows[0].data == {"attempt": 1}
    # 时间戳必须带时区。丢掉时区后跨时区比较会静默错位，
    # 而"事件顺序对不对"正是靠时间与 seq 一起判断的。
    assert rows[0].ts.tzinfo is not None
    assert abs((datetime.now(UTC) - rows[0].ts).total_seconds()) < 60


async def test_latest_seq(store):
    assert await store.latest_seq("t") == 0
    await store.append_event("t", "task.created")
    await store.append_event("t", "profile.ready")
    assert await store.latest_seq("t") == 2
    assert await store.latest_seq("other") == 0


# ---------------------------------------------------------------------------
# 持久化：这个实现存在的理由
# ---------------------------------------------------------------------------
async def test_survives_reopen(tmp_path):
    """关掉再打开，任务与事件的 ``seq`` 都要原样还在。

    这是手机端"退到后台再回来能看到完整进度"的前提——应用被杀掉是常态，
    而不是异常。
    """
    db = tmp_path / "state.db"

    first = SqliteStateStore(db)
    await first.open()
    await first.put_task(sample_task("task_persist", status="running"))
    for i in range(4):
        await first.append_event("task_persist", "subtask.progress", {"i": i})
    await first.close()

    second = SqliteStateStore(db)
    await second.open()
    try:
        rec = await second.get_task("task_persist")
        assert rec is not None and rec.task_id == "task_persist"
        assert rec.profile is not None and rec.profile.task_type == "chat.explain"

        events = await second.read_events("task_persist")
        assert [e.seq for e in events] == [1, 2, 3, 4]
        assert await second.latest_seq("task_persist") == 4

        # 重开之后继续追加，seq 接着往下走而不是从头开始
        nxt = await second.append_event("task_persist", "task.completed")
        assert nxt.seq == 5
    finally:
        await second.close()


async def test_refuses_to_open_a_newer_schema(tmp_path):
    """版本不匹配就拒绝打开，不做"尽力读取"。

    把新布局当旧布局读会产出**看起来正常但字段错位**的数据——那比打不开糟糕得多，
    因为它会静默地污染账目。
    """
    db = tmp_path / "state.db"
    async with SqliteStateStore(db):
        pass

    import aiosqlite

    conn = await aiosqlite.connect(db, isolation_level=None)
    await conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                       (str(SCHEMA_VERSION + 1),))
    await conn.close()

    with pytest.raises(RuntimeError, match="schema_version"):
        await SqliteStateStore(db).open()


async def test_creates_parent_directories(tmp_path):
    """手机上首次启动时目录还不存在，不该因此起不来。"""
    nested = tmp_path / "a" / "b" / "state.db"
    async with SqliteStateStore(nested) as s:
        await s.append_event("t", "task.created")
    assert nested.exists()


# ---------------------------------------------------------------------------
# 清理：终态事件的绝对保护
# ---------------------------------------------------------------------------
async def test_prune_extends_retention_for_terminal_tasks(store):
    """终态任务的事件获得**更长**的保留期，但不是无限期。

    ``keep_terminal_days`` 是延长量，不是开关。用"将来"的时刻做 cutoff，
    让所有事件都算过期：非终态的立刻删掉，终态的因为还在保留期内而活下来。
    """
    await store.put_task(sample_task("done", status="succeeded"))
    await store.put_task(sample_task("alive", status="running"))
    for tid in ("done", "alive"):
        for i in range(3):
            await store.append_event(tid, "subtask.progress", {"i": i})

    future = datetime.now(UTC) + timedelta(hours=1)
    removed = await store.prune_events(before=future, keep_terminal_days=180)

    assert removed == 3, "只有非终态任务的三条该被删掉"
    assert len(await store.read_events("done")) == 3, "终态事件在保留期内必须一条不少"
    assert await store.read_events("alive") == []


async def test_terminal_retention_is_independent_of_before(store):
    """终态保留期与 ``before`` 互相独立——它们回答的是两个不同问题。

    ``before`` 落在未来（意味着"所有事件都算旧"），但终态事件仍在 180 天
    保留期内，因此必须活下来。如果把两者写成 ``max(before, ...)``，
    这里就会一起被删掉——那正是这个测试要挡住的。
    """
    await store.put_task(sample_task("done", status="succeeded"))
    await store.append_event("done", "task.completed")

    future = datetime.now(UTC) + timedelta(days=365)
    assert await store.prune_events(before=future, keep_terminal_days=180) == 0
    assert len(await store.read_events("done")) == 1


async def test_prune_with_zero_days_drops_terminal_events_immediately(store):
    """``keep_terminal_days=0`` 表示随任务结束即删——明确但危险。

    策略里给的是 180 天，不是 0，让"保留多久"是一个写出来的决定。
    """
    await store.put_task(sample_task("done", status="succeeded"))
    await store.append_event("done", "task.completed")

    past = datetime.now(UTC) - timedelta(days=1)
    assert await store.prune_events(before=past, keep_terminal_days=0) == 1
    assert await store.read_events("done") == []


async def test_prune_keeps_recent_events(store):
    await store.put_task(sample_task("alive", status="running"))
    await store.append_event("alive", "task.created")

    past = datetime.now(UTC) - timedelta(hours=1)
    assert await store.prune_events(before=past, keep_terminal_days=0) == 0
    assert len(await store.read_events("alive")) == 1


async def test_prune_removes_orphan_events(store):
    """没有对应任务记录的事件也该被清掉，而不是永远留着。"""
    await store.append_event("ghost", "task.created")
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await store.prune_events(before=future, keep_terminal_days=0) == 1


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
async def test_idempotency_round_trip_and_tenant_scope(store):
    assert await store.get_idempotency("k1", "t1") is None
    await store.put_idempotency("k1", "t1", "task_a")
    assert await store.get_idempotency("k1", "t1") == "task_a"
    # 同一个键在不同租户下互不干扰
    assert await store.get_idempotency("k1", "t2") is None
    # 重复写入是覆盖而不是报错——重放路径会走到这里
    await store.put_idempotency("k1", "t1", "task_b")
    assert await store.get_idempotency("k1", "t1") == "task_b"


# ---------------------------------------------------------------------------
# 与内存实现行为一致：存储可替换这句话必须是真的
# ---------------------------------------------------------------------------
async def _run_shared_scenario(store) -> dict:
    """一段对两种实现都应当产出相同结果的操作序列。"""
    out: dict = {}

    await store.put_task(sample_task("a", status="running"))
    await store.put_task(sample_task("b", status="succeeded", tenant_id="t2", user_id="u9"))

    for i in range(3):
        await store.append_event("a", "subtask.progress", {"i": i})
    await store.append_event("a", "subtask.started", {"attempt": 1}, subtask_id="extract")
    await store.append_event("b", "task.completed")

    out["seqs_a"] = [e.seq for e in await store.read_events("a")]
    out["since_2"] = [e.seq for e in await store.read_events("a", since_seq=2)]
    out["latest_a"] = await store.latest_seq("a")
    out["latest_missing"] = await store.latest_seq("zzz")
    out["subtask"] = (await store.read_events("a"))[-1].subtask_id
    out["types"] = [e.type for e in await store.read_events("a")]
    out["list_default"] = sorted(
        t.task_id for t in await store.list_tasks(tenant_id="default", user_id=None, limit=10)
    )
    out["list_t2"] = sorted(
        t.task_id for t in await store.list_tasks(tenant_id="t2", user_id=None, limit=10)
    )
    out["list_u1"] = sorted(
        t.task_id for t in await store.list_tasks(tenant_id="default", user_id="u_1", limit=10)
    )

    await store.put_idempotency("k", "tn", "a")
    out["idem"] = await store.get_idempotency("k", "tn")
    out["idem_other_tenant"] = await store.get_idempotency("k", "zz")

    future = datetime.now(UTC) + timedelta(hours=1)
    out["pruned"] = await store.prune_events(before=future, keep_terminal_days=0)
    out["after_prune_a"] = [e.seq for e in await store.read_events("a")]
    out["after_prune_b"] = [e.seq for e in await store.read_events("b")]

    return out


async def test_sqlite_matches_in_memory(tmp_path):
    """同一段操作在两个实现上必须产出逐项相同的结果。

    存储是可替换的——如果两个实现在边界行为上分岔，"换存储不用改上层"
    这句话就是假的，而那种分叉会以"线上和测试不一样"的形式出现。
    """
    async with SqliteStateStore(tmp_path / "parity.db") as sqlite_store:
        from_sqlite = await _run_shared_scenario(sqlite_store)

    from_memory = await _run_shared_scenario(InMemoryStateStore())

    assert from_sqlite == from_memory, (
        "两个存储在同样的操作序列上给出了不同结果——"
        f"SQLite={from_sqlite}\n  内存={from_memory}"
    )


# ---------------------------------------------------------------------------
# 与真实链路的接合：不做假数据
# ---------------------------------------------------------------------------
async def test_stores_a_real_guard_decision(store, policy: Policy, registry: HandlerRegistry):
    """用真实策略跑一次守卫，把结果存下来再取出来必须一致。

    前面那些用的是手工构造的 TaskRecord；这一条走真实的 ``apply_guard``，
    确认序列化对真实产出的决策对象也是无损的。
    """
    prof = TaskProfile(
        task_type="bookkeeping.capture_from_receipt",
        modality=["image", "text"],
        complexity=Complexity(score=0.42),
        urgency=Urgency(level="normal"),
        recommended_mode="async",
        confidence=0.86,
    )
    outcome = apply_guard(
        policy,
        RawDecision(
            route_id="vision_extract_then_write", model_tier="strong",
            handler="bookkeeping",
            tool_set=["extract_receipt_fields", "made_up_tool"],
            execution_mode="sync", max_cost=0.99,
        ),
        profile=prof,
        handler_ids=registry.ids,
        handler_tools=registry.tool_map(),
        constraints_max_cost=0.05,
        mode_preference="sync",
    )

    rec = sample_task("real", profile=prof, decision=outcome.decision)
    await store.put_task(rec)
    back = await store.get_task("real")

    assert isinstance(back.decision, RouteDecision)
    assert back.decision.model_dump() == outcome.decision.model_dump()

    # 逐项确认守卫的真实修正被无损存回。注意**没有**断言档位被降级：
    # vision_extract_then_write 的 allowed_tiers 本来就含 strong，
    # 所以 strong 是合法的、不会被动。断言"应当降级"会是一个错误的期望。
    assert back.decision.model_tier == "strong"
    assert back.decision.tool_set == ["extract_receipt_fields"], "编造的工具必须被剔除"
    assert "tool_set_intersected" in back.decision.guard.applied
    assert back.decision.budget.max_cost == 0.05, "成本上限必须收敛到请求约束"
    assert back.decision.execution_mode == "async", "要拆解的任务不能同步执行"
    assert "mode_promoted" in back.decision.guard.applied
