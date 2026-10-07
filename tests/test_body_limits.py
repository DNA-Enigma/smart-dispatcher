"""请求体上限（第 1 处）。

两条断言值得单独说，它们是"真的防住了"与"看起来防住了"的唯一区别：

* ``receive_calls == 0`` —— 声明超限时**一个字节都没读**。只断言 413 的话，
  把整个请求体收进内存再判也能通过，而那正是审计里"10MiB 上限形同虚设"的写法。
* ``receive_calls == 3``（给了 5 块）—— 边读边判、越限即停，而不是读满再判。

第三条钉住"媒体路径单独放宽"：通用上限压到 1 KiB 时，JSON 端点照旧 413，
而 ``POST /v1/media`` 仍按策略里 ``limits.media.max_bytes`` 收 —— 放宽不是一个
可以调歪的第二环境变量，而是"取存储层真正执行的那个界"。
"""

from __future__ import annotations

import json

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.settings import get_settings
from dispatcher.core.state import TaskRecord
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from tests.asgi_harness import call_app, make_scope
from tests.fakes import ScriptedLLM
from tests.test_pipeline import build

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"
JSON_HEADERS = [(b"content-type", b"application/json")]


class _NeverCalled:
    """本文件只测请求边界：任何一个走到流水线的请求都是测试写错了。"""

    async def submit(self, *a, **kw):  # pragma: no cover
        raise AssertionError("请求体超限/非法时不该走到流水线")


@pytest.fixture
def tiny_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """把通用上限压到 1 KiB。"""
    monkeypatch.setenv("DISPATCHER_MAX_REQUEST_BYTES", "1024")
    get_settings.cache_clear()
    return 1024


@pytest.fixture
def app_under_test(monkeypatch: pytest.MonkeyPatch, tiny_cap):
    monkeypatch.setattr(app_module, "_dispatcher", _NeverCalled())
    return create_app()


# ------------------------------------------------------------------ 快速拒
async def test_declared_oversize_body_is_rejected_without_reading_it(app_under_test, schemas, tiny_cap):
    """``Content-Length`` 声明超限 → 当场 413，一个字节都不读。"""
    result = await call_app(
        app_under_test,
        make_scope(
            method="POST",
            path="/v1/tasks",
            headers=[*JSON_HEADERS, (b"content-length", b"999999")],
        ),
        chunks=[b"{}"],
    )
    assert result.status == 413, result.body
    assert result.receive_calls == 0, "声明超限就不该再去读请求体（先读后判等于没判）"
    problem = json.loads(result.body)
    jsonschema_validate(problem, schemas[PROBLEM_SCHEMA_ID])
    assert problem["code"] == "media_too_large"
    assert problem["status"] == 413
    assert problem["context"]["declared_bytes"] == 999999


# ------------------------------------------------------- 边读边判、越限即停
async def test_body_is_cut_off_while_reading_not_after(app_under_test, tiny_cap):
    """没带 Content-Length（或写小了）时，靠边读边累计。

    上限 1024、每块 512 字节：第 3 块到 1536 才越限，因此 ``receive`` 恰好被调 3 次。
    读满 5 块说明实现是"读完再判"。
    """
    result = await call_app(
        app_under_test,
        make_scope(method="POST", path="/v1/tasks", headers=JSON_HEADERS),
        chunks=[b"x" * 512] * 5,
    )
    assert result.status == 413, result.body
    assert result.receive_calls == 3, "越限即停；读满 5 块说明是读完才判"


async def test_body_within_the_cap_still_reaches_the_pipeline(monkeypatch, tiny_cap):
    """上限之内的请求照常往下走——防的是超限，不是把端点关掉。"""
    reached: list[dict] = []

    class _Accepts:
        async def submit(self, envelope, *, request_id="", background=None):
            reached.append(envelope.model_dump(mode="json"))
            return TaskRecord(task_id="t_1", tenant_id="default", user_id="u")

    monkeypatch.setattr(app_module, "_dispatcher", _Accepts())
    app = create_app()
    small = json.dumps({"identity": {"user_id": "u"}, "input": {"text": "hi"}}).encode()
    assert len(small) < tiny_cap
    result = await call_app(
        app, make_scope(method="POST", path="/v1/tasks", headers=JSON_HEADERS), chunks=[small]
    )
    assert result.status == 202, result.body
    assert reached, "上限之内的请求必须真的进到流水线"


# -------------------------------------------------- 媒体路径单独放宽
async def test_media_upload_is_not_narrowed_by_the_general_cap(
    monkeypatch, tiny_cap, policy, pricing, taxonomy, registry, prompts
):
    """通用上限 1 KiB 时媒体仍能收到 4 KiB —— 上限取的是存储层那个界。"""
    media = InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
    )
    d = build(policy, pricing, taxonomy, registry, prompts, media, ScriptedLLM([]))
    monkeypatch.setattr(app_module, "_dispatcher", d)

    blob = b"\x89PNG\r\n\x1a\n" + b"a" * 4096
    assert len(blob) > tiny_cap
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        up = await client.post(
            "/v1/media", content=blob, headers={"content-type": "image/png"}
        )
        # 同一个上限下，JSON 端点照旧被挡——证明放宽只发生在媒体这一条路由上
        over = await client.post(
            "/v1/tasks",
            content=json.dumps({"pad": "x" * 4096}),
            headers={"content-type": "application/json"},
        )
    assert up.status_code == 201, up.text
    assert up.json()["bytes"] == len(blob)
    assert over.status_code == 413, over.text


async def test_media_upload_over_the_store_limit_is_still_413(
    monkeypatch, policy, pricing, taxonomy, registry, prompts
):
    """媒体上限放宽到策略那个值，不代表没有上限。"""
    media = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1024)
    d = build(policy, pricing, taxonomy, registry, prompts, media, ScriptedLLM([]))
    monkeypatch.setattr(app_module, "_dispatcher", d)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        up = await client.post(
            "/v1/media", content=b"a" * 4096, headers={"content-type": "image/png"}
        )
    assert up.status_code == 413, up.text
    assert up.json()["code"] == "media_too_large"


async def test_oversize_content_length_with_a_huge_number_is_not_a_500(app_under_test, schemas):
    """5000 位的 ``Content-Length`` 不该冒成 500。

    ``int()`` 对超长数字串会抛 ValueError（默认 4300 位上限），未处理的话就是一个
    稳定 500。这里的正确行为是"这个头不可信，退化成按实读字节数判"：于是要么
    400（body 不是合法信封）、要么 413（实读也超限），**两者都是 Problem**。
    """
    result = await call_app(
        app_under_test,
        make_scope(
            method="POST",
            path="/v1/tasks",
            headers=[*JSON_HEADERS, (b"content-length", b"9" * 5000)],
        ),
        chunks=[b"{}"],
    )
    assert result.status in (400, 413), result.body
    jsonschema_validate(json.loads(result.body), schemas[PROBLEM_SCHEMA_ID])
