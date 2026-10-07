"""媒体保留期与清理（第 5 处）。

修复前的两半：``expires_at`` 在 ``retain_days`` 缺省时是 ``None``（**永不过期**），
而 ``sweep_expired`` 全仓没有调用点（``docs/05-media.md`` 说它"由定时任务驱动"，
那个定时任务不存在）。因此这里分三层钉：

* **store**：不带 ``retain_days`` 也必须有 ``expires_at``——"永不过期"这条路要不可达；
  过期的被清、``protected`` 里的不清；
* **调度层**：``sweep_media`` 传下去的 ``protected`` 就是"还活着的任务引用的媒体"，
  任务一到终态（取消）它就不再受保护；
* **接口层**：``retain_days`` 被夹进 ``[1, 上界]``，超大值不再是一个 500。

任务引用的保护是必需的：任务可能停在 ``awaiting_clarification`` 过夜，恢复时还要读
那张截图，提前删掉会让恢复必然失败（``unsupported_media`` 是 fatal）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.core.settings import get_settings
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from dispatcher.pipeline import Dispatcher
from tests.fakes import ScriptedLLM
from tests.test_pipeline import build

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image"


def _days_from_now(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds() / 86400.0


# ------------------------------------------------------------------ store
async def test_put_without_retain_days_still_expires():
    """**这是本次修复的核心**：缺省不再等于永不过期。"""
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000, default_retain_days=3)
    rec = await store.put(PNG, "image/png")
    assert rec.expires_at is not None, "expires_at=None 就是'一直躺在内存里'"
    assert 2.9 < _days_from_now(rec.expires_at.isoformat()) <= 3.0


async def test_retain_days_zero_falls_back_to_the_configured_default():
    """``retain_days=0``（docs 里的"任务结束即删"）不能退化成永不过期。

    store 无从知道"任务什么时候结束"——那是调度层的事。因此这里按缺省保留期处理，
    语义写在文档与汇报里，而不是让它变成一个无限期驻留的记录。
    """
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000, default_retain_days=2)
    rec = await store.put(PNG, "image/png", retain_days=0)
    assert rec.expires_at is not None
    assert 1.9 < _days_from_now(rec.expires_at.isoformat()) <= 2.0


async def test_absurd_retain_days_is_a_typed_error_not_an_overflow():
    """接口层会把 retain_days 夹住，但**直接调 store 的调用方**绕过那一层。

    那条路上 ``timedelta(days=巨大值)`` 抛的是 OverflowError —— 一个稳定 500
    （审计第 37 条）。这里要的是有类型的错误，不是再抄一份上限。
    """
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    with pytest.raises(DispatcherError) as ei:
        await store.put(PNG, "image/png", retain_days=10 ** 20)
    assert ei.value.code == "invalid_request"


async def test_sweep_removes_expired_media():
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    rec = await store.put(PNG, "image/png", retain_days=1)
    future = datetime.now(UTC) + timedelta(days=2)

    assert await store.sweep_expired(future) == 1
    assert await store.get(rec.media_id) is None
    assert len(store) == 0


async def test_sweep_keeps_media_still_referenced_by_a_live_task():
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    rec = await store.put(PNG, "image/png", retain_days=1)
    future = datetime.now(UTC) + timedelta(days=2)

    assert await store.sweep_expired(future, protected={rec.media_id}) == 0
    assert await store.get(rec.media_id) is not None, "被任务引用的媒体不能提前删掉"
    # 同一个时刻，不保护它就该被删——证明上面的 0 是保护生效而不是"还没过期"
    assert await store.sweep_expired(future) == 1


async def test_sweep_does_not_touch_records_without_expiry():
    """记录没有 ``expires_at`` 时按"不删"处理（防御性：实现可以自己决定不留存）。"""
    store = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    rec = await store.put(PNG, "image/png")
    store._records[rec.media_id] = rec.model_copy(update={"expires_at": None})
    assert await store.sweep_expired(datetime.now(UTC) + timedelta(days=3650)) == 0


# ------------------------------------------------------------------ 调度层
@pytest.fixture
async def make(policy, pricing, taxonomy, registry, prompts):
    built: list[Dispatcher] = []

    def _make(*, responses=(), default_retain_days=1):
        media = InMemoryMediaStore(
            allowed_mime=policy.limits.media.allowed_mime,
            max_bytes=policy.limits.media.max_bytes,
            default_retain_days=default_retain_days,
        )
        d = build(policy, pricing, taxonomy, registry, prompts, media,
                  ScriptedLLM(list(responses)))
        built.append(d)
        return d, media

    yield _make
    for d in built:
        await d.aclose()


def _paused_advance():
    async def advance(record, envelope, cancel, *, stop_after_planning=False, **kw):
        record.status = "planning" if stop_after_planning else "awaiting_clarification"
        return record

    return advance


def _media_env(media_id: str) -> TaskEnvelope:
    return TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {
            "text": "记一笔",
            "media": [{
                "media_id": media_id, "kind": "image", "mime": "image/png",
                "bytes": len(PNG), "sha256": "a" * 64, "role": "source_document",
            }],
        },
    })


async def test_sweep_protects_media_of_a_paused_task_until_it_ends(make, monkeypatch):
    d, media = make(default_retain_days=1)
    rec_media = await media.put(PNG, "image/png", retain_days=1)
    monkeypatch.setattr(d, "_advance", _paused_advance())
    future = datetime.now(UTC) + timedelta(days=2)

    rec = await d.submit(_media_env(rec_media.media_id))
    assert rec.status == "awaiting_clarification"

    assert await d.sweep_media(now=future) == 0, "还停着等人的任务，它的图不能被清"
    assert await media.get(rec_media.media_id) is not None

    await d.cancel(rec.task_id)
    assert await d.sweep_media(now=future) == 1, "任务结束后引用该放掉，下一轮清理才会删它"
    assert await media.get(rec_media.media_id) is None


async def test_sweeper_loop_sweeps_periodically_and_stops_on_cancel(make, monkeypatch):
    d, _ = make()
    calls: list[int] = []

    async def fake_sweep(*, now=None):
        calls.append(1)
        return 0

    monkeypatch.setattr(d, "sweep_media", fake_sweep)
    task = asyncio.create_task(d.media_sweeper(0.05))
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 2, "定时任务必须真的在周期性地跑清理"


# ------------------------------------------------------------------ 接口层
@pytest.fixture
async def client(policy, pricing, taxonomy, registry, prompts, monkeypatch):
    # 钉住保留期的配置：`.env` 是开发者的私人物品，本仓测试的结论不该随它的
    # 内容变化（同 conftest 的 _auth_off_by_default）。
    monkeypatch.setenv("DISPATCHER_MEDIA_RETAIN_DAYS", "1")
    monkeypatch.setenv("DISPATCHER_MEDIA_RETAIN_MAX_DAYS", "30")
    get_settings.cache_clear()

    media = InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
        default_retain_days=1,
    )
    d = build(policy, pricing, taxonomy, registry, prompts, media, ScriptedLLM([]))
    monkeypatch.setattr(app_module, "_dispatcher", d)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await d.aclose()


async def _upload(client, query: str = "") -> httpx.Response:
    return await client.post(
        f"/v1/media{query}", content=PNG, headers={"content-type": "image/png"}
    )


async def test_upload_without_retain_days_returns_a_bounded_expiry(client):
    """上传端点此前的响应里 ``expires_at`` 是 ``null``——那就是"永不过期"的线上形状。"""
    resp = await _upload(client)
    assert resp.status_code == 201, resp.text
    expires = resp.json()["expires_at"]
    assert expires is not None, "缺省保留期必须有值"
    assert 0 < _days_from_now(expires) <= 1.0


async def test_retain_days_is_clamped_to_the_configured_ceiling(client):
    resp = await _upload(client, "?retain_days=9999")
    assert resp.status_code == 201, resp.text
    # 缺省上界 30 天。断言区间而不是精确值：配置项本来就允许被部署改。
    assert 28 < _days_from_now(resp.json()["expires_at"]) <= 30.0


async def test_huge_retain_days_is_not_a_500(client):
    """21 位以上的数字此前会让 ``timedelta(days=...)`` 抛 OverflowError → 稳定 500。"""
    resp = await _upload(client, "?retain_days=" + "9" * 40)
    assert resp.status_code == 201, resp.text
    assert 0 < _days_from_now(resp.json()["expires_at"]) <= 30.0


async def test_negative_and_non_numeric_retain_days_do_not_reach_the_store(client):
    """负数夹到 1 天；非数字退回缺省——两种都不该变成 500 或永不过期。"""
    for query in ("?retain_days=-5", "?retain_days=abc"):
        resp = await _upload(client, query)
        assert resp.status_code == 201, (query, resp.text)
        assert 0 < _days_from_now(resp.json()["expires_at"]) <= 1.0


async def test_expired_media_is_gone_from_the_read_path(client):
    """到期之后 ``GET /v1/media/{id}`` 必须是 404——契约对它的说法是"不存在或已过保留期"。"""
    resp = await _upload(client, "?retain_days=1")
    media_id = resp.json()["media_id"]
    got = await client.get(f"/v1/media/{media_id}")
    assert got.status_code == 200, got.text
    # 直接把它改成已过期（不睡一天），再走一次读路径
    app_module._dispatcher.media._records[media_id] = (
        app_module._dispatcher.media._records[media_id].model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
    )
    expired = await client.get(f"/v1/media/{media_id}")
    assert expired.status_code == 404, expired.text
    assert expired.json()["code"] == "not_found"
