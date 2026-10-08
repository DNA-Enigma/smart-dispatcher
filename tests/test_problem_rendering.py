"""Problem 响应体的渲染：**细节留在服务端，响应里只留 request_id**。

审计「错误体泄露」把 ``Problem.detail`` 列为回客户端的一路。脱敏之后，排障的
唯一落点就是服务端日志里的 ``internal``——因此它必须和响应里的 ``request_id``
对得上号，否则线上拿到一个 request_id、日志里却找不到对应那一行。

这个文件只钉这件事：``internal`` 既不进响应体，又确实写进日志且带 request_id。
"""

from __future__ import annotations

import logging

import pytest

from dispatcher.core.errors import DispatcherError
from dispatcher.interface.problems import problem_response

_DETAIL = "host=api.internal.example.com key=sk-live-abc123"


def test_internal_detail_is_logged_but_never_serialised(
    caplog: pytest.LogCaptureFixture,
):
    exc = DispatcherError(
        "upstream_llm_error", "供应商 500（服务端错误）", retryable=False,
        internal=_DETAIL,
    )
    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        resp = problem_response(exc, "rid-7")

    # 响应体：只有分类文案 + request_id，没有原文
    body = resp.body.decode()
    assert "api.internal.example.com" not in body
    assert "sk-live-abc123" not in body
    assert resp.status_code == 502
    assert b"rid-7" in resp.body

    # 日志：原文在，且与响应同一个 request_id
    assert "api.internal.example.com" in caplog.text
    assert "rid-7" in caplog.text


def test_problem_without_internal_logs_nothing(caplog: pytest.LogCaptureFixture):
    """没有 ``internal`` 时不产生这条 WARNING——否则每条 401 都会刷一行噪音。"""
    exc = DispatcherError("unauthorized", "缺少 Authorization: Bearer 凭据")
    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        problem_response(exc, "rid-8")
    assert "内部：" not in caplog.text
