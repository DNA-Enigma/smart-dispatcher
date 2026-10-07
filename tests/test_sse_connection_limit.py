"""SSE 连接上限与名额回收（第 6 处）。

三件事分别钉：

* **上限本身**：占满之后是 **429 Problem**（不是 503 纯文本，也不是"挂住直到有空位"），
  且带 ``Retry-After`` —— 客户端据此知道"稍后重试"是多久；
* **断开的连接要把名额还回来**：这是"别制造第 4 条那样的泄漏"那半句。用一个真的
  挂着的 SSE 连接（``tests/asgi_harness``）然后取消它，看计数归零；
* **流正常结束也要还**：把任务置为终态、心跳调到 1ms，流会自己收尾——上限设成 1
  时"第二个请求还能成功"就是名额确实还回来了的证据（没还的话它会 429）。

为什么不用 httpx 测挂着的连接：``httpx.ASGITransport`` 会把整个响应体收完才返回，
它等不到一个永不结束的流。见 ``tests/asgi_harness`` 的模块注释。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.policy import load_policy
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.state import TaskRecord
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from dispatcher.interface.limiter import ConnectionLimiter
from tests.asgi_harness import call_app, make_scope
from tests.fakes import ScriptedLLM
from tests.test_pipeline import build

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"


# ------------------------------------------------------------------ 计数闸本身
def test_limiter_hands_out_leases_up_to_the_limit_and_then_refuses():
    limiter = ConnectionLimiter(limit=2)
    first, second = limiter.acquire(), limiter.acquire()
    assert first is not None and second is not None
    assert limiter.active == 2
    assert limiter.acquire() is None, "满了必须拒绝，而不是把请求挂住"
    first.release()
    assert limiter.acquire() is not None


def test_lease_release_is_idempotent():
    """两个释放点（生成器 finally 与端点侧）都在时，计数不能减两次。"""
    limiter = ConnectionLimiter(limit=1)
    lease = limiter.acquire()
    assert lease is not None
    lease.release()
    lease.release()
    assert limiter.active == 0
    assert limiter.acquire() is not None


def test_non_positive_limit_means_unlimited():
    limiter = ConnectionLimiter(limit=0)
    for _ in range(50):
        assert limiter.acquire() is not None


# ------------------------------------------------------------------ 429
@pytest.fixture
async def one_slot(policy, pricing, taxonomy, registry, prompts, monkeypatch):
    """上限 = 1 的真实应用 + 一条已存在的任务。"""
    monkeypatch.setenv("DISPATCHER_SSE_MAX_CONNECTIONS", "1")
    get_settings.cache_clear()

    media = InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime, max_bytes=policy.limits.media.max_bytes
    )
    d = build(policy, pricing, taxonomy, registry, prompts, media, ScriptedLLM([]))
    monkeypatch.setattr(app_module, "_dispatcher", d)
    await d.state.create_task(
        TaskRecord(task_id="t_live", tenant_id="default", user_id="owner", status="running")
    )
    application = create_app()
    yield application
    await d.aclose()


async def test_sse_over_the_limit_is_429_with_retry_after(one_slot, schemas):
    limiter = one_slot.state.sse_limiter
    assert limiter.limit == 1
    assert limiter.acquire() is not None, "先把唯一的名额占满"

    transport = httpx.ASGITransport(app=one_slot)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t_live/events")

    assert resp.status_code == 429, resp.text
    problem = resp.json()
    jsonschema_validate(problem, schemas[PROBLEM_SCHEMA_ID])
    assert problem["code"] == "rate_limited"
    assert problem["retryable"] is True
    assert problem["retry_after_ms"] > 0
    assert problem["context"]["scope"] == "sse_connections"
    # 头也得有：移动端的重试退避读的是它
    assert resp.headers["retry-after"] == str(problem["retry_after_ms"] // 1000)


async def test_task_lookup_still_wins_over_the_limit(one_slot):
    """404 优先于 429：不存在的任务不该因为"连接满了"而变成另一个错。"""
    limiter = one_slot.state.sse_limiter
    limiter.acquire()
    transport = httpx.ASGITransport(app=one_slot)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/tasks/t_nope/events")
    assert resp.status_code == 404, resp.text


# --------------------------------------------------- 断开的连接要还名额
async def test_cancelled_stream_returns_its_slot(one_slot):
    limiter = one_slot.state.sse_limiter
    scope = make_scope(method="GET", path="/v1/tasks/t_live/events")

    # hang_after=True：客户端一直挂着，不给 http.disconnect——否则 Starlette 会
    # 立刻把响应任务取消，"连接还占着"这一刻就观察不到了。
    task = asyncio.create_task(call_app(one_slot, scope, hang_after=True))
    await asyncio.sleep(0.1)

    assert limiter.active == 1, "挂着的 SSE 连接必须计入名额"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)

    assert limiter.active == 0, "连接断开后名额必须归还（否则就是第 4 条那样的泄漏）"


# --------------------------------------------------- 流正常结束也要还名额
async def test_finished_stream_returns_its_slot(policy, pricing, taxonomy, registry, prompts,
                                               monkeypatch):
    """任务到终态 → 流自己收尾 → 名额归还。上限 1 时"第二个请求还能进"即为证据。"""
    monkeypatch.setenv("DISPATCHER_SSE_MAX_CONNECTIONS", "1")
    get_settings.cache_clear()

    # 心跳调成 1ms：终态任务会先发一次心跳再收尾，否则这一步要等默认的 15s。
    # 用**新加载**的策略对象改，避免污染 session 级的 policy fixture。
    fast_policy = load_policy(REPO_ROOT / "config" / "routing.policy.yaml")
    fast_policy.limits.sse_heartbeat_ms = 1

    media = InMemoryMediaStore(
        allowed_mime=fast_policy.limits.media.allowed_mime,
        max_bytes=fast_policy.limits.media.max_bytes,
    )
    d = build(fast_policy, pricing, taxonomy, registry, prompts, media, ScriptedLLM([]))
    monkeypatch.setattr(app_module, "_dispatcher", d)
    await d.state.create_task(
        TaskRecord(task_id="t_done", tenant_id="default", user_id="owner", status="succeeded")
    )
    application = create_app()
    assert application.state.sse_limiter.limit == 1

    try:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/v1/tasks/t_done/events")
            assert first.status_code == 200, first.text
            # 名额若没归还，第二个请求会撞上限而不是拿到流
            second = await client.get("/v1/tasks/t_done/events")
            assert second.status_code == 200, second.text
    finally:
        await d.aclose()

    assert application.state.sse_limiter.active == 0
