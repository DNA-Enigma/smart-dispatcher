"""``clarify`` / ``feedback`` 的请求体校验。

这两个端点此前是裸 ``await request.json()``：字段名写错既 200 又不生效。本文件钉住
校验层的四条边界——正常放行、缺必填 400、类型错 400、未知字段**警告但不拒绝**——
并确认失败响应体就是 ``schemas/problem.json`` 的合法实例（不是随手拼的 dict）。

用 HTTP 层测而不是直接调 ``read_body``：要证的正是"端点接上了校验"，
只测函数会漏掉"端点忘了调"这一类错误。
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


class _Snapshot:
    def to_snapshot(self) -> dict:
        return {"task_id": "t1", "status": "running"}


class _FakeDispatcher:
    """只记录"流水线收到了什么"，不执行任何调度语义——本文件测的是校验层。"""

    def __init__(self) -> None:
        self.clarify_calls: list[dict] = []
        self.feedback_calls: list[dict] = []

    async def clarify(self, task_id: str, body: dict) -> _Snapshot:
        self.clarify_calls.append(body)
        return _Snapshot()

    async def record_feedback(self, task_id: str, body: dict) -> dict:
        if task_id == "missing":
            raise DispatcherError("not_found", f"任务不存在：{task_id}", task_id=task_id)
        self.feedback_calls.append(body)
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
