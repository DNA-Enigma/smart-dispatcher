"""``_running`` / ``_cancels`` 的出账（第 4 处）。

审计的原话是"只进不出、无 pop、无 done_callback"。因此本文件的核心断言不是"任务跑完了"，
而是**跑完之后那两个 dict 里没有它的条目**——只断言终态的话，泄漏依然存在。

四条退出路径各一条用例，它们是 ``_forget`` 的四个调用点：

* **后台完成**（``add_done_callback``）；
* **同步完成**（``submit`` 同步分支的 ``finally``）；
* **后台抛致命错误**（同一个 done 回调——异常路径不能只靠任务自己清）；
* **被取消**（``cancel`` 当场清 + done 回调兜一次）。

外加两条：澄清恢复也不留令牌（``clarify`` 的 ``finally``），以及
**媒体引用**与这两个 dict 的路数不同——它要活到终态，由 ``_advance`` 的真代码释放。

为了不把 01/02/03 全跑一遍，这里的 ``_advance`` 大多被换成一段脚本。但"清账"本身
是 ``submit`` / ``cancel`` / ``clarify`` 里的真代码，替身换掉的只是流水线内部——
要测的正是"谁在什么时候把条目拿掉"，而被替身挡住的流水线内部与这件事无关。
"""

from __future__ import annotations

import asyncio

import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.core.state import TaskRecord
from dispatcher.pipeline import Dispatcher
from tests.fakes import ScriptedLLM
from tests.test_pipeline import DECISION_JSON, PROFILE_JSON, build

# 一段合法的 PNG 头 + 内容：评估器会真的去 store 里取这张图，取不到就抛
# unsupported_media（fatal），那样测的就不是"终态释放引用"了。
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image"


@pytest.fixture
async def make(policy, pricing, taxonomy, registry, prompts):
    """按需造真调度器。``_advance`` 由各用例自己决定要不要换掉。"""
    built: list[Dispatcher] = []

    def _make(*, responses=(), execution_enabled=False, default_retain_days=1):
        media = InMemoryMediaStore(
            allowed_mime=policy.limits.media.allowed_mime,
            max_bytes=policy.limits.media.max_bytes,
            default_retain_days=default_retain_days,
        )
        d = build(
            policy, pricing, taxonomy, registry, prompts, media,
            ScriptedLLM(list(responses)), execution_enabled=execution_enabled,
        )
        built.append(d)
        return d, media

    yield _make
    for d in built:
        await d.aclose()


def text_env() -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {"identity": {"user_id": "u_1"}, "input": {"text": "记一笔"}}
    )


def media_env(media_id: str) -> TaskEnvelope:
    return TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {
            "text": "记一笔",
            "media": [{
                "media_id": media_id, "kind": "image", "mime": "image/png",
                "bytes": 100, "sha256": "a" * 64, "role": "source_document",
            }],
        },
    })


def scripted_advance(*, planning: str, execution: str):
    """规划阶段给 ``planning`` 状态，执行阶段给 ``execution`` 状态。"""

    async def advance(record, envelope, cancel, *, stop_after_planning=False, **kw):
        record.status = planning if stop_after_planning else execution
        return record

    return advance


# ------------------------------------------------------------------ 后台完成
async def test_background_task_leaves_the_registry_when_it_finishes(make, monkeypatch):
    d, _ = make()
    monkeypatch.setattr(d, "_advance", scripted_advance(planning="planning", execution="succeeded"))

    rec = await d.submit(text_env(), background=True)
    # 在册：这是后台任务能被取消的前提（test_integration 也钉着这一条）
    task = d._running[rec.task_id]
    assert rec.task_id in d._cancels

    await task
    await asyncio.sleep(0)  # 让 done_callback 跑完

    assert d._running == {}, "后台任务完成后 _running 必须出账"
    assert d._cancels == {}, "后台任务完成后 _cancels 必须出账"


# ------------------------------------------------------------------ 同步完成
async def test_sync_task_leaves_the_registry(make, monkeypatch):
    """同步任务从来不进 ``_running``，但**进过 ``_cancels``**——那一半也得清。"""
    d, _ = make()
    monkeypatch.setattr(d, "_advance", scripted_advance(planning="planning", execution="succeeded"))

    await d.submit(text_env())

    assert d._running == {}
    assert d._cancels == {}, "同步任务跑完不该留下取消令牌"


# ------------------------------------------------------------------ 失败
async def test_background_failure_leaves_the_registry(make, monkeypatch):
    """致命错误会从 ``_advance`` 里抛出来——后台任务没人 await，条目照样得出账。"""

    async def advance(record, envelope, cancel, *, stop_after_planning=False, **kw):
        if stop_after_planning:
            record.status = "planning"
            return record
        raise DispatcherError("policy_violation", "密钥没配")

    d, _ = make()
    monkeypatch.setattr(d, "_advance", advance)

    rec = await d.submit(text_env(), background=True)
    task = d._running[rec.task_id]
    with pytest.raises(DispatcherError):
        await task
    await asyncio.sleep(0)

    assert d._running == {}
    assert d._cancels == {}


# ------------------------------------------------------------------ 取消
async def test_cancelled_task_leaves_the_registry(make, monkeypatch):
    async def advance(record, envelope, cancel, *, stop_after_planning=False, **kw):
        if stop_after_planning:
            record.status = "planning"
            return record
        await asyncio.sleep(30)  # 挂住，等取消

    d, _ = make()
    monkeypatch.setattr(d, "_advance", advance)

    rec = await d.submit(text_env(), background=True)
    assert rec.task_id in d._running

    await d.cancel(rec.task_id)
    await asyncio.sleep(0.05)  # 等被取消的那个任务把 done_callback 走完

    assert d._running == {}, "取消后 _running 必须出账"
    assert d._cancels == {}, "取消后 _cancels 必须出账"


# ------------------------------------------------- 媒体引用：活到终态，不是活到"不跑"
async def test_paused_task_leaves_the_registry_but_keeps_its_media(make, monkeypatch):
    """停在等人与"跑完了"在**媒体**上不是一回事。

    暂停的任务恢复时要读那张截图，因此 ``_media_refs`` 必须留着；
    但它已经不在册（没有协程在跑，取消靠状态机），所以两个 dict 都要清。
    """
    d, media = make()
    rec_media = await media.put(PNG, "image/png")
    monkeypatch.setattr(
        d, "_advance", scripted_advance(planning="awaiting_clarification", execution="succeeded")
    )

    rec = await d.submit(media_env(rec_media.media_id))

    assert d._running == {} and d._cancels == {}
    assert d.protected_media_ids() == {rec_media.media_id}, "暂停的任务还要读这张图"

    # 一旦取消（终态），引用就该放掉——否则清理器永远不敢删它
    await d.cancel(rec.task_id)
    assert d.protected_media_ids() == set()


async def test_terminal_task_releases_its_media_refs_through_real_advance(make):
    """走**真** ``_advance``：终态由它的 ``finally`` 释放媒体引用。

    这条不换替身（路线是真实的 01 → 02 → 未接入执行层 ⇒ rejected），因此它同时
    证明了上一轮加的 finally 真的接在代码里，而不是只接在被替身换掉的那条路上。
    """
    d, media = make(responses=[PROFILE_JSON, DECISION_JSON], execution_enabled=False)
    rec_media = await media.put(PNG, "image/png")

    rec = await d.submit(media_env(rec_media.media_id))

    assert rec.status == "rejected", rec.error  # M1 没有执行层，明确地停在什么都没跑
    assert d._media_refs == {}, "终态任务不该继续占着媒体引用"
    assert d.protected_media_ids() == set()


# ----------------------------------------------------- 澄清恢复也不留令牌
async def test_clarify_does_not_leak_a_cancel_token(make, monkeypatch):
    """``clarify`` 是内联跑完的：它 ``setdefault`` 出来的令牌必须由自己清掉。

    只在这里清是不够的（后台/同步两条路各有各的清账点），但漏了这里就是
    "每澄清一次漏一个令牌"。
    """
    d, _ = make()
    monkeypatch.setattr(d, "_advance", scripted_advance(planning="planning", execution="succeeded"))

    await d.state.create_task(TaskRecord(
        task_id="t_paused", tenant_id="default", user_id="u_1",
        status="awaiting_clarification",
        envelope=text_env().model_dump(mode="json"),
        clarification={"question_id": "q_1", "question": "哪一笔？", "options": []},
    ))

    await d.clarify("t_paused", {"question_id": "q_1", "answer_id": "confirm"})

    assert d._cancels == {}, "澄清恢复跑完不该留下令牌"
    assert d._running == {}
