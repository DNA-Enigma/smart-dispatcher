"""对象级授权（IDOR）与幂等键归属。

"有 token 才能进来"（见 ``tests/test_auth.py``）只解决了一半：拿到**任意一个**
合法 token 的人，若还能用别人的 task_id 读快照/推进澄清/取消任务，那鉴权等于没做。
本文件钉住第二半——**进来了，然后只能碰自己的东西**。

四个维度：

* **管线层**：``get`` 是按 id 取任务的唯一收口（快照、产物、事件流、澄清、反馈、
  取消全走它），跨租户必须是 404，且与"任务压根不存在"不可区分；
* **接口层**：端点确实把 token 身份传下去了，而不是"管线支持了但没人调用"；
* **媒体**：``GET /v1/media/{id}`` 与"请求体里引用别人的 media_id"两条路都要挡
  ——后者绕开了前者，能把别人的截图直接送进模型；
* **幂等键**：同 key 不同体 → 409；同 key 跨租户拿不到别人的任务。
"""

from __future__ import annotations

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.core.state import TaskRecord
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from dispatcher.interface.auth import AuthConfig
from dispatcher.ports.llm import LLMError
from tests.fakes import ScriptedLLM
from tests.test_pipeline import DECISION_JSON, PROFILE_JSON, build

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"

TOKEN_A = "token-tenant-a"
TOKEN_B = "token-tenant-b"
AUTH_A = AuthConfig(token=TOKEN_A, tenant_id="t_a", user_id="u_a")
AUTH_B = AuthConfig(token=TOKEN_B, tenant_id="t_b", user_id="u_b")


@pytest.fixture
def media(policy) -> InMemoryMediaStore:
    """与 test_pipeline 的同名 fixture 一致：允许列表来自策略，不写死在测试里。"""
    return InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
    )


@pytest.fixture
def dispatcher(policy, pricing, taxonomy, registry, prompts, media, monkeypatch):
    """真调度器（内存存储）挂到接口层上——测的是授权，不是流水线。

    预置两段真实的模型响应（与 ``test_pipeline`` 的两阶段用例同源），让"所有者
    发同一个请求"能真的跑完：要证的是归属校验**没有挡住所有者**，那就得让流水线
    有可跑完的输入，而不是拿一个只会失败的 LLM 去猜。
    """
    llm = ScriptedLLM([PROFILE_JSON, DECISION_JSON])
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    monkeypatch.setattr(app_module, "_dispatcher", d)
    return d


def envelope(*, text: str = "记一笔", tenant: str = "t_a", user: str = "u_1",
             key: str | None = None, request_id: str | None = None,
             app_version: str | None = None) -> TaskEnvelope:
    payload: dict = {
        "identity": {"user_id": user, "tenant_id": tenant},
        "input": {"text": text},
    }
    if key:
        payload["idempotency_key"] = key
    if request_id:
        payload["request_id"] = request_id
    if app_version:
        payload["client"] = {"app": "bookkeeping", "app_version": app_version}
    return TaskEnvelope.model_validate(payload)


async def seed(d, task_id: str = "t_1", tenant: str = "t_a", **kw) -> TaskRecord:
    """直接把记录放进存储：这里要的是"存在一条别人的任务"，不是跑一遍流水线。"""
    rec = TaskRecord(task_id=task_id, tenant_id=tenant, user_id="u_1", **kw)
    await d.state.create_task(rec)
    return rec


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _raises_not_found(coro) -> DispatcherError:
    with pytest.raises(DispatcherError) as ei:
        await coro
    assert ei.value.code == "not_found"
    return ei.value


# ------------------------------------------------------------ 管线层：读与写
async def test_get_task_across_tenants_is_404(dispatcher):
    await seed(dispatcher)
    assert (await dispatcher.get("t_1", tenant_id="t_a")).task_id == "t_1"
    await _raises_not_found(dispatcher.get("t_1", tenant_id="t_b"))


async def test_foreign_and_missing_tasks_are_indistinguishable(dispatcher):
    """403 会告诉调用方"这个 id 存在，只是不归你"——那本身就是要藏的信息。"""
    await seed(dispatcher)
    missing = await _raises_not_found(dispatcher.get("nope", tenant_id="t_a"))
    foreign = await _raises_not_found(dispatcher.get("t_1", tenant_id="t_b"))
    assert (missing.status, foreign.status) == (404, 404)
    # detail 里只回显调用方自己给的那个 id，不透露别的
    assert "t_1" in foreign.detail


async def test_get_without_a_tenant_scope_still_works(dispatcher):
    """内嵌使用/单用户本机模式没有租户上下文，参数可选是为了它们。"""
    await seed(dispatcher)
    assert (await dispatcher.get("t_1")).task_id == "t_1"


async def test_clarify_across_tenants_is_404_not_invalid_request(dispatcher):
    """租户不符要在**状态机之前**判定。

    否则"这个任务当前不是 awaiting_clarification"（400）与"不是你的任务"（404）
    会按任务状态给出不同的码，等于用一个状态预言机替代了存在性预言机。
    """
    await seed(dispatcher, status="running")
    await _raises_not_found(
        dispatcher.clarify("t_1", {"question_id": "q1"}, tenant_id="t_b")
    )


async def test_cancel_across_tenants_is_404_and_leaves_the_task_alone(dispatcher):
    await seed(dispatcher, status="running")
    await _raises_not_found(dispatcher.cancel("t_1", tenant_id="t_b"))
    assert (await dispatcher.state.get_task("t_1")).status == "running"


async def test_feedback_across_tenants_is_404_and_writes_nothing(dispatcher):
    rec = await seed(dispatcher)
    await _raises_not_found(
        dispatcher.record_feedback("t_1", {"verdict": "accepted"}, tenant_id="t_b")
    )
    assert (await dispatcher.state.get_task("t_1")).human_signal == rec.human_signal


async def test_list_is_scoped_to_the_tenant(dispatcher):
    await seed(dispatcher, task_id="t_a1", tenant="t_a")
    await seed(dispatcher, task_id="t_b1", tenant="t_b")
    items = await dispatcher.list(tenant_id="t_a", user_id=None, limit=20)
    assert [r.task_id for r in items] == ["t_a1"]


# ----------------------------------------------------------------- 幂等键
async def test_same_key_with_a_different_body_is_409(dispatcher):
    """契约早就写了这条（"同 key + 不同请求体 → 409"），此前从未实现。

    没有它，一次失败重试就能把任务替换成另一个请求的产物；有了它，重放必须是
    原样重放。
    """
    first = envelope(text="买咖啡 38", key="K1")
    await seed(dispatcher, envelope=first.model_dump(mode="json"))
    await dispatcher.state.put_idempotency("K1", "t_a", "t_1")

    with pytest.raises(DispatcherError) as ei:
        await dispatcher.submit(envelope(text="买咖啡 39", key="K1"))
    assert ei.value.code == "idempotency_conflict"
    assert ei.value.status == 409
    # 冲突不产生新任务：库里的任务数不变
    assert len(await dispatcher.state.list_tasks(tenant_id="t_a", user_id=None, limit=10)) == 1


async def test_same_key_with_the_same_body_replays(dispatcher):
    first = envelope(text="买咖啡 38", key="K1")
    await seed(dispatcher, envelope=first.model_dump(mode="json"))
    await dispatcher.state.put_idempotency("K1", "t_a", "t_1")

    again = await dispatcher.submit(envelope(text="买咖啡 38", key="K1"))
    assert again.task_id == "t_1"


async def test_replay_ignores_transport_and_telemetry_fields(dispatcher):
    """指纹认的是**请求内容**，不是这一次传输的包装。

    ``request_id``（每次尝试都变）、``client.app_version``（客户端升级）、
    请求体里的 identity（服务端已按 token 覆盖）都不该把一次正常重试变成 409。
    """
    first = envelope(text="买咖啡 38", key="K1")
    await seed(dispatcher, envelope=first.model_dump(mode="json"))
    await dispatcher.state.put_idempotency("K1", "t_a", "t_1")

    again = await dispatcher.submit(envelope(
        text="买咖啡 38", key="K1", request_id="r_2",
        app_version="1.1", user="u_someone_else",
    ))
    assert again.task_id == "t_1"


async def test_shared_idempotency_key_does_not_cross_tenants(
    policy, pricing, taxonomy, registry, prompts, media
):
    """攻击者知道受害者的幂等键也没用：键空间按租户隔离。

    这一条同时盯住"命中即无条件覆盖"——``put_idempotency`` 曾用
    ``ON CONFLICT DO UPDATE SET task_id = excluded.task_id``，攻击者提交同键就能把
    受害者的映射改指到自己的任务，让受害者下次重试拿到攻击者的产物。
    """
    llm = ScriptedLLM([], fail_with=LLMError("upstream down", retryable=True))
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    await d.state.create_task(
        TaskRecord(task_id="t_victim", tenant_id="t_a", user_id="u_v")
    )
    await d.state.put_idempotency("K", "t_a", "t_victim")

    got = await d.submit(envelope(text="攻击者自己的请求", tenant="t_b", key="K"))

    assert got.tenant_id == "t_b"
    assert got.task_id != "t_victim"
    # 受害者的映射纹丝不动
    assert await d.state.get_idempotency("K", "t_a") == "t_victim"


# ----------------------------------------------------------------- 接口层
async def test_http_foreign_task_is_404(dispatcher, schemas):
    await seed(dispatcher)
    async with _client(create_app(auth=AUTH_A)) as ca:
        own = await ca.get("/v1/tasks/t_1", headers=_bearer(TOKEN_A))
    assert own.status_code == 200, own.text

    async with _client(create_app(auth=AUTH_B)) as cb:
        foreign = await cb.get("/v1/tasks/t_1", headers=_bearer(TOKEN_B))
    assert foreign.status_code == 404, foreign.text
    jsonschema_validate(foreign.json(), schemas[PROBLEM_SCHEMA_ID])
    assert foreign.json()["code"] == "not_found"


async def test_http_foreign_task_subresources_are_404(dispatcher):
    """产物、事件流、澄清、反馈、取消——同一个 task_id，一条都不能通。"""
    await seed(dispatcher, status="awaiting_clarification")
    async with _client(create_app(auth=AUTH_B)) as cb:
        result = await cb.get("/v1/tasks/t_1/result", headers=_bearer(TOKEN_B))
        events = await cb.get("/v1/tasks/t_1/events", headers=_bearer(TOKEN_B))
        clarify = await cb.post(
            "/v1/tasks/t_1/clarify", json={"question_id": "q1"}, headers=_bearer(TOKEN_B)
        )
        feedback = await cb.post(
            "/v1/tasks/t_1/feedback", json={"verdict": "accepted"}, headers=_bearer(TOKEN_B)
        )
        cancel = await cb.post("/v1/tasks/t_1/cancel", headers=_bearer(TOKEN_B))
    assert [r.status_code for r in (result, events, clarify, feedback, cancel)] == [404] * 5


async def test_http_media_from_another_tenant_is_404(dispatcher, schemas):
    """跨租户读别人的金融截图——A5，此前 ``media.get()`` 只看 id。"""
    async with _client(create_app(auth=AUTH_A)) as ca:
        up = await ca.post(
            "/v1/media", content=b"\x89PNG\r\n\x1a\nfake",
            headers={"content-type": "image/png", **_bearer(TOKEN_A)},
        )
    assert up.status_code == 201, up.text
    media_id = up.json()["media_id"]

    async with _client(create_app(auth=AUTH_A)) as ca:
        own = await ca.get(f"/v1/media/{media_id}", headers=_bearer(TOKEN_A))
    assert own.status_code == 200, own.text

    async with _client(create_app(auth=AUTH_B)) as cb:
        foreign = await cb.get(f"/v1/media/{media_id}", headers=_bearer(TOKEN_B))
    assert foreign.status_code == 404, foreign.text
    jsonschema_validate(foreign.json(), schemas[PROBLEM_SCHEMA_ID])


async def test_http_upload_records_the_token_identity_not_the_headers(dispatcher, media):
    """``x-tenant-id``/``x-user-id`` 不再被读取——同一份身份不能有两套说法。"""
    async with _client(create_app(auth=AUTH_A)) as ca:
        up = await ca.post(
            "/v1/media", content=b"\x89PNG\r\n\x1a\nfake",
            headers={
                "content-type": "image/png",
                "x-tenant-id": "t_victim",
                "x-user-id": "u_victim",
                **_bearer(TOKEN_A),
            },
        )
    assert up.status_code == 201, up.text
    rec = await media.stat(up.json()["media_id"])
    assert (rec.tenant_id, rec.user_id) == ("t_a", "u_a")


async def test_http_task_referencing_foreign_media_is_404(dispatcher, schemas):
    """请求体里引用别人的 media_id 是绕过 ``GET /v1/media/{id}`` 的那条路：
    评估器会把媒体取出来拼成 data URI 送给模型，调用方一个字节都不用碰。"""
    async with _client(create_app(auth=AUTH_A)) as ca:
        up = await ca.post(
            "/v1/media", content=b"\x89PNG\r\n\x1a\nfake",
            headers={"content-type": "image/png", **_bearer(TOKEN_A)},
        )
    media_id = up.json()["media_id"]
    payload = {
        "identity": {"user_id": "u_b"},
        "input": {
            "text": "记一笔",
            "media": [{"media_id": media_id, "kind": "image", "mime": "image/png"}],
        },
    }

    async with _client(create_app(auth=AUTH_B)) as cb:
        foreign = await cb.post("/v1/tasks", json=payload, headers=_bearer(TOKEN_B))
    assert foreign.status_code == 404, foreign.text
    jsonschema_validate(foreign.json(), schemas[PROBLEM_SCHEMA_ID])
    # 拒绝发生在建任务之前：不会留下一个"引用了别人媒体"的失败任务
    assert await dispatcher.state.list_tasks(tenant_id="t_b", user_id=None, limit=10) == []

    # 同一个请求体换回媒体所有者：归属校验必须放行，任务照常跑起来
    async with _client(create_app(auth=AUTH_A)) as ca:
        own = await ca.post("/v1/tasks", json=payload, headers=_bearer(TOKEN_A))
    assert own.status_code in (200, 202), own.text
    assert len(await dispatcher.state.list_tasks(tenant_id="t_a", user_id=None, limit=10)) == 1


async def test_http_missing_media_is_404_too(dispatcher):
    """「不存在」与「不是你的」必须是同一个码——否则这组码差就是存在性探测器。"""
    payload = {
        "identity": {"user_id": "u_a"},
        "input": {
            "text": "记一笔",
            "media": [{"media_id": "m_nope", "kind": "image", "mime": "image/png"}],
        },
    }
    async with _client(create_app(auth=AUTH_A)) as ca:
        resp = await ca.post("/v1/tasks", json=payload, headers=_bearer(TOKEN_A))
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "not_found"
