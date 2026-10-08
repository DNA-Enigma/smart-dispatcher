"""OpenAI 兼容适配器里那些不需要网络的部分。

重点是**错误分类**：把供应商的 HTTP 状态翻成"可恢复的抖动"还是"不会自己好的
配置问题"。这个区分决定了上层是降级还是大声失败，因此值得单独测。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from dispatcher.adapters.openai_compat import OpenAICompatibleLLM, QPSLimiter
from dispatcher.ports.llm import LLMError, LLMMessage

# 一份"什么都带"的供应商错误体：主机名、URL、模型名、key 片段、账单内容。
# 审计「错误体泄露」说的就是它——曾以 ``resp.text[:300]`` 原样回客户端。
_LEAKY_BODY = (
    '{"error":{"message":"cannot reach https://api.internal.example.com/v1",'
    '"model":"vendor-model-x","key":"sk-live-abc123","balance_cny":42.5}}'
)
_LEAKS = ("api.internal.example.com", "vendor-model-x", "sk-live-abc123", "balance_cny")


def _resp(status: int, body: str = "{}") -> httpx.Response:
    return httpx.Response(status_code=status, text=body, request=httpx.Request("POST", "http://x"))


@pytest.mark.parametrize(
    "status,expected_kind,expected_retryable",
    [
        (401, "auth", False),
        (403, "auth", False),
        (402, "quota", False),
        (400, "bad_request", False),
        (404, "bad_request", False),
        (429, "transient", True),
        (500, "transient", True),
        (503, "transient", True),
    ],
)
def test_http_status_maps_to_error_kind(status, expected_kind, expected_retryable):
    err = OpenAICompatibleLLM._http_error(_resp(status))
    assert err.kind == expected_kind
    assert err.retryable is expected_retryable
    assert err.status == status


@pytest.mark.parametrize("status", [401, 402, 403, 400])
def test_configuration_errors_are_fatal(status):
    """凭证、余额、请求格式问题不该被降级吞掉——它们不会自己好。"""
    assert OpenAICompatibleLLM._http_error(_resp(status)).fatal is True


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_errors_are_not_fatal(status):
    assert OpenAICompatibleLLM._http_error(_resp(status)).fatal is False


def test_fatal_error_translates_to_a_typed_problem_with_advice():
    err = LLMError("余额不足", retryable=False, status=402, kind="quota")
    problem = err.to_dispatcher_error()
    assert problem.code == "upstream_llm_error"
    assert problem.retryable is False
    assert problem.context["provider_status"] == 402
    # 错误信息要能指导下一步动作，而不只是宣布失败
    assert "充值" in problem.detail or "换一个 key" in problem.detail

    auth = LLMError("凭证被拒", retryable=False, status=401, kind="auth")
    assert "LLM_API_KEY" in auth.to_dispatcher_error().detail


# ---------------------------------------------------------------------------
# 错误体泄露：供应商原文只进 provider_detail（服务端），绝不进 message
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 503])
def test_provider_body_stays_out_of_the_message_for_every_status(status):
    """message 会顺着 ``str(e)`` → ``Problem.detail`` → 任务快照回到客户端。

    所以供应商响应体一个字符都不能进 message——无论哪个状态码。
    """
    err = OpenAICompatibleLLM._http_error(_resp(status, _LEAKY_BODY))
    for leak in _LEAKS:
        assert leak not in str(err), f"{status}：{leak} 漏进了 message"
    assert err.provider_detail is not None, "原文必须留在服务端，否则排障断了"


def test_provider_body_is_redacted_and_reaches_the_translated_problem_only_as_internal(
    monkeypatch: pytest.MonkeyPatch,
):
    """``provider_detail`` 里回显的 key 已按值脱敏，并从 ``internal`` 带走。

    ``internal`` 是"细节留在服务端"的落点：它不进 Problem 体，只被日志打印。
    """
    import dispatcher.core.settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_settings",
        lambda: SimpleNamespace(llm_api_key="sk-live-abc123"),
    )
    err = OpenAICompatibleLLM._http_error(_resp(500, _LEAKY_BODY))

    # 主机名/模型名/账单在服务端原文里是可用的排查线索
    assert "api.internal.example.com" in err.provider_detail
    assert "balance_cny" in err.provider_detail
    # 但密钥不留原文（日志会被外送）
    assert "sk-live-abc123" not in err.provider_detail
    assert "***" in err.provider_detail

    problem = err.to_dispatcher_error()
    assert problem.internal == err.provider_detail
    for leak in _LEAKS:
        assert leak not in problem.detail


async def test_network_error_hides_the_host_but_keeps_it_for_logs(policy, settings):
    """网络异常原文可能带 URL/主机名——同样只进 provider_detail。"""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接 https://api.internal.example.com/v1 失败")

    llm = OpenAICompatibleLLM(policy, settings)
    await llm.aclose()
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        with pytest.raises(LLMError) as ei:
            await llm.complete([LLMMessage.user("hi")], tier="standard")
    finally:
        await llm.aclose()

    err = ei.value
    assert "api.internal.example.com" not in str(err)
    assert "网络错误" in str(err)
    assert "api.internal.example.com" in (err.provider_detail or "")


# ---------------------------------------------------------------------------
# JSON 抽取
# ---------------------------------------------------------------------------
def test_extract_json_handles_fences_and_prose():
    assert OpenAICompatibleLLM.extract_json('{"a": 1}') == {"a": 1}
    assert OpenAICompatibleLLM.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert OpenAICompatibleLLM.extract_json('好的，这是结果：\n{"a": 1}\n希望有用') == {"a": 1}


def test_extract_json_refuses_to_guess():
    """宁可直接失败，也不做修复性猜测——猜出来的 JSON 会让错误的数据看起来像正确的。"""
    for bad in ["", "没有对象", "[1, 2]", '{"a": 1', "```json\n```"]:
        with pytest.raises(ValueError):
            OpenAICompatibleLLM.extract_json(bad)


# ---------------------------------------------------------------------------
# 深度思考与 token 计费
# ---------------------------------------------------------------------------
def _adapter(policy, settings):
    return OpenAICompatibleLLM(policy, settings)


def test_reasoning_tokens_are_captured(policy, settings):
    """推理 token 计入 completion_tokens 并按输出计费，必须单独取出来。

    取出来才能回答"贵在思考还是贵在回答"——不取的话成本明细里只有一笔糊涂账。
    """
    llm = _adapter(policy, settings)
    body = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 60,
            "completion_tokens": 103,
            "completion_tokens_details": {"reasoning_tokens": 14},
        },
    }
    r = llm._parse_ok(body, "cheap", "mimo-v2.6-flash", time.monotonic())
    assert r.output_tokens == 103
    assert r.reasoning_tokens == 14


def test_reasoning_tokens_absent_is_none_not_zero(policy, settings):
    """没开思考时该字段缺失。记成 None 而不是 0——
    "不知道"和"是零"是两回事，后者会让 04 以为思考没花过钱。"""
    llm = _adapter(policy, settings)
    body = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    r = llm._parse_ok(body, "cheap", "m", time.monotonic())
    assert r.reasoning_tokens is None


async def test_json_mode_requires_the_declared_capability(policy, settings):
    """声明即约束：档位没声明 json_mode 就不能要求结构化输出。

    否则会把"模型不保证 JSON"这件事推到解析那一跳才失败，
    而那已经是花过钱、也过了好几个中间步骤之后了。
    """
    import copy

    from dispatcher.core.errors import DispatcherError
    from dispatcher.ports.llm import LLMMessage

    stripped = copy.deepcopy(policy)
    stripped.model_tiers["strong"].capabilities = [
        c for c in stripped.model_tiers["strong"].capabilities if c != "json_mode"
    ]
    llm = OpenAICompatibleLLM(stripped, settings)
    with pytest.raises(DispatcherError) as ei:
        await llm.complete(
            [LLMMessage.user("hi")], tier="strong", requires=("text",), json_mode=True
        )
    assert ei.value.code == "no_capability_match"
    await llm.aclose()


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------
async def test_qps_limiter_caps_concurrency():
    """并发闸独立于速率闸：只有速率限制会让长尾请求堆积。"""
    import asyncio

    lim = QPSLimiter(qps=1000, max_concurrency=2)
    peak = 0
    current = 0

    async def work():
        nonlocal peak, current
        async with lim.slot():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert peak <= 2
