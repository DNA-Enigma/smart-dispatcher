"""请求体校验：**所有**接 JSON 的端点。

``clarify`` / ``feedback`` 此前是裸 ``await request.json()``：字段名写错既 200 又不生效。
本文件钉住校验层的四条边界——正常放行、缺必填 400、类型错 400、未知字段**警告但不拒绝**
——并确认失败响应体就是 ``schemas/problem.json`` 的合法实例（不是随手拼的 dict）。

用 HTTP 层测而不是直接调 ``read_body``：要证的正是"端点接上了校验"，只测函数会漏掉
"端点忘了调"这一类错误——**而它真的发生了**：``create_task`` / ``approve`` / ``reject`` /
``rollback`` 四个端点当时就是裸 ``request.json()``，非法 JSON 一路冒成 500 纯文本
（不是 Problem）。后半部分把这四个端点也钉住，包括 ``approve`` 空体仍然合法。
"""

from __future__ import annotations

import logging

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.core.errors import DispatcherError
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"

CLARIFY = "/v1/tasks/t1/clarify"
FEEDBACK = "/v1/tasks/t1/feedback"
TASKS = "/v1/tasks"
APPROVE = "/v1/evolution/suggestions/s_1/approve"
REJECT = "/v1/evolution/suggestions/s_1/reject"
ROLLBACK = "/v1/policy/rollback"


class _Snapshot:
    def to_snapshot(self) -> dict:
        return {"task_id": "t1", "status": "running"}


class _FakeDispatcher:
    """只记录"流水线收到了什么"，不执行任何调度语义——本文件测的是校验层。

    ``tenant_id`` 是关键字参数：端点现在把 token 身份一起传下去（跨租户按不存在
    处理），替身要跟得上真实签名，否则测的是"签名对不对"而不是"校验对不对"。
    """

    def __init__(self) -> None:
        self.clarify_calls: list[dict] = []
        self.feedback_calls: list[dict] = []
        self.tenant_ids: list[str | None] = []

    async def clarify(self, task_id: str, body: dict, *, tenant_id: str | None = None) -> _Snapshot:
        self.clarify_calls.append(body)
        self.tenant_ids.append(tenant_id)
        return _Snapshot()

    async def record_feedback(
        self, task_id: str, body: dict, *, tenant_id: str | None = None
    ) -> dict:
        if task_id == "missing":
            raise DispatcherError("not_found", f"任务不存在：{task_id}", task_id=task_id)
        self.feedback_calls.append(body)
        self.tenant_ids.append(tenant_id)
        return {"verdict": body.get("verdict")}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatcher:
    f = _FakeDispatcher()
    monkeypatch.setattr(app_module, "_dispatcher", f)
    return f


@pytest.fixture
async def client(fake: _FakeDispatcher):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _StubEvolution:
    """只够回答"端点有没有把非法 JSON 挡在流水线之外"。

    approve / reject / rollback 的**语义**由 ``test_evolution.py`` 管；
    这里管的是请求边界，因此记录调用即可，不实现任何版本流转。
    """

    def __init__(self) -> None:
        self.approved: list[dict] = []
        self.rolled_back: list[dict] = []

    async def approve(self, suggestion_id, *, approved_by, scope=None, note=None):
        self.approved.append({"id": suggestion_id, "by": approved_by, "scope": scope})
        return {
            "policy_version": "p_2", "parent_version": "p_1", "changed": ["limits.x"],
            "scope": {"canary": {"percent": 10}}, "status": "active",
            "policy": {"policy_version": "p_2"},
        }

    async def reject(self, suggestion_id, *, reason, decided_by):
        return {"status": "rejected"}

    async def rollback(self, *, to_version, note=None):
        self.rolled_back.append({"to": to_version, "note": note})
        return {"policy_version": to_version, "policy": {"policy_version": to_version}}


class _EvolutionDispatcher:
    """``_require_evolution()`` 要求 ``evolution`` 非 None，``apply_policy`` 也得在。"""

    def __init__(self) -> None:
        self.evolution = _StubEvolution()
        self.applied: list = []

    def apply_policy(self, policy) -> None:
        self.applied.append(policy)


@pytest.fixture
def evo(monkeypatch: pytest.MonkeyPatch) -> _EvolutionDispatcher:
    f = _EvolutionDispatcher()
    monkeypatch.setattr(app_module, "_dispatcher", f)
    return f


@pytest.fixture
async def evo_client(evo: _EvolutionDispatcher):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_problem(schemas: dict, resp: httpx.Response, *, status: int) -> dict:
    """失败响应必须是合法的 Problem，且状态码与 code 一致。"""
    assert resp.status_code == status, resp.text
    body = resp.json()
    jsonschema_validate(body, schemas[PROBLEM_SCHEMA_ID])
    assert body["code"] == "invalid_request"
    assert body["status"] == status
    return body


# ---------------------------------------------------------------- clarify
async def test_clarify_accepts_a_well_formed_answer(client, fake):
    resp = await client.post(CLARIFY, json={"question_id": "q1", "answer_id": "confirm"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert fake.clarify_calls == [{"question_id": "q1", "answer_id": "confirm"}]


async def test_clarify_missing_required_field_is_400(client, schemas):
    resp = await client.post(CLARIFY, json={"answer_id": "confirm"})
    body = _assert_problem(schemas, resp, status=400)
    assert body["context"]["missing"] == ["question_id"]


async def test_clarify_null_required_field_is_400(client, schemas):
    """``null`` 与"没给"同罪——否则 ``answer_id`` 这种合法可空字段会被当成漏填的范例。"""
    resp = await client.post(CLARIFY, json={"question_id": None})
    _assert_problem(schemas, resp, status=400)


async def test_clarify_type_error_is_400(client, schemas):
    """``edits`` 契约是对象，传数组是**请求**的错，不是任务失败。"""
    resp = await client.post(CLARIFY, json={"question_id": "q1", "edits": [1, 2]})
    body = _assert_problem(schemas, resp, status=400)
    assert body["context"]["errors"][0]["field"] == "edits"


async def test_clarify_unknown_field_is_kept_out_but_not_rejected(client, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        resp = await client.post(
            CLARIFY,
            json={"question_id": "q1", "answer_id": "confirm", "future_field": 123},
        )
    assert resp.status_code == 200, resp.text
    # 放行，但未知字段**不进流水线**——不认识的键不该在下游悄悄生效
    assert fake.clarify_calls == [{"question_id": "q1", "answer_id": "confirm"}]
    assert "future_field" in caplog.text


async def test_clarify_empty_body_is_400_not_500(client, schemas):
    resp = await client.post(CLARIFY, content=b"", headers={"content-type": "application/json"})
    _assert_problem(schemas, resp, status=400)


async def test_clarify_malformed_json_is_400_not_500(client, schemas):
    resp = await client.post(
        CLARIFY, content=b"{not json", headers={"content-type": "application/json"}
    )
    _assert_problem(schemas, resp, status=400)


async def test_clarify_non_object_body_is_400(client, schemas):
    resp = await client.post(CLARIFY, json=["q1"])
    _assert_problem(schemas, resp, status=400)


# --------------------------------------------------------------- feedback
async def test_feedback_accepts_a_well_formed_signal(client, fake):
    resp = await client.post(FEEDBACK, json={"verdict": "accepted", "reason": "分类对了"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["human_signal"] == {"verdict": "accepted"}
    assert fake.feedback_calls[0]["verdict"] == "accepted"


async def test_feedback_missing_verdict_is_400(client, schemas):
    resp = await client.post(FEEDBACK, json={"reason": "忘了填结论"})
    body = _assert_problem(schemas, resp, status=400)
    assert body["context"]["missing"] == ["verdict"]


async def test_feedback_verdict_outside_enum_is_400(client, schemas):
    resp = await client.post(FEEDBACK, json={"verdict": "maybe"})
    body = _assert_problem(schemas, resp, status=400)
    assert body["context"]["errors"][0]["field"] == "verdict"


async def test_feedback_type_error_is_400(client, schemas):
    """``edits`` 契约是数组；传字符串是请求的错。此前它会被兜底伞压成一句模糊的 400。"""
    resp = await client.post(FEEDBACK, json={"verdict": "edited", "edits": "not-a-list"})
    _assert_problem(schemas, resp, status=400)


async def test_feedback_unknown_field_is_accepted_with_warning(client, fake, caplog):
    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        resp = await client.post(FEEDBACK, json={"verdict": "accepted", "nonsense": True})
    assert resp.status_code == 200, resp.text
    assert fake.feedback_calls == [{"verdict": "accepted"}]
    assert "nonsense" in caplog.text


async def test_feedback_on_unknown_task_is_404_not_400(client, schemas):
    """任务不存在是 404。此前 ``except Exception`` 会把它改写成 400 invalid_request，
    与 openapi.yaml 为 feedback 声明的 404 矛盾，也把排查方向从"id 打错了"带偏到"请求格式"。"""
    resp = await client.post("/v1/tasks/missing/feedback", json={"verdict": "accepted"})
    assert resp.status_code == 404, resp.text
    body = resp.json()
    jsonschema_validate(body, schemas[PROBLEM_SCHEMA_ID])
    assert body["code"] == "not_found"


# ------------------------------------------- 另外四个端点：非法 JSON 也是 400
#
# 这四个此前是裸 ``await request.json()``。Starlette 的 ``Request.json()`` 抛的是
# ``JSONDecodeError``——不是 ``DispatcherError``，因此绕过 ``app.py`` 的异常处理器，
# 冒到 ServerErrorMiddleware 变成 **500 纯文本**。客户端拿到的东西既不是契约里的
# Problem，也分不清是自己发错了还是服务端挂了。
async def test_create_task_malformed_json_is_400_not_500(client, schemas):
    resp = await client.post(
        TASKS, content=b"{not json", headers={"content-type": "application/json"}
    )
    _assert_problem(schemas, resp, status=400)


async def test_create_task_empty_body_is_400_not_500(client, schemas):
    resp = await client.post(TASKS, content=b"", headers={"content-type": "application/json"})
    _assert_problem(schemas, resp, status=400)


async def test_create_task_non_object_body_is_400_not_500(client, schemas):
    resp = await client.post(TASKS, json=["不是对象"])
    _assert_problem(schemas, resp, status=400)


async def test_approve_malformed_json_is_400_not_500(evo_client, schemas):
    resp = await evo_client.post(
        APPROVE, content=b"{bad", headers={"content-type": "application/json"}
    )
    _assert_problem(schemas, resp, status=400)


async def test_approve_empty_body_is_still_allowed(evo_client, evo):
    """空体是**合法**的（``scope``/``note`` 都可选）——统一读体路径不能顺手把它拒掉。"""
    resp = await evo_client.post(APPROVE)
    assert resp.status_code == 200, resp.text
    assert evo.evolution.approved[0]["id"] == "s_1"
    assert evo.applied, "批准之后要热换策略"


async def test_reject_malformed_json_is_400_not_500(evo_client, schemas):
    resp = await evo_client.post(
        REJECT, content=b"<xml/>", headers={"content-type": "application/json"}
    )
    _assert_problem(schemas, resp, status=400)


async def test_reject_without_reason_is_400(evo_client, schemas):
    """理由本身是信号，缺了就拒——这条是老行为，钉住它没被读体路径改掉。"""
    resp = await evo_client.post(REJECT, json={"note": "忘了写理由"})
    body = _assert_problem(schemas, resp, status=400)
    assert "理由" in body["detail"]


async def test_rollback_malformed_json_is_400_not_500(evo_client, schemas):
    resp = await evo_client.post(
        ROLLBACK, content=b"{}", headers={"content-type": "application/json"}
    )
    # 空对象是**合法 JSON**，缺 to_version 是另一条 400；先钉住它不是 500
    _assert_problem(schemas, resp, status=400)


async def test_rollback_not_json_at_all_is_400_not_500(evo_client, schemas):
    resp = await evo_client.post(
        ROLLBACK, content=b"not-json", headers={"content-type": "application/json"}
    )
    _assert_problem(schemas, resp, status=400)
