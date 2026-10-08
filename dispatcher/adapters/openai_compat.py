"""LLMPort 的 OpenAI 兼容实现。

形状泛化自 ``duowei-ai/backend/app/core/llm_client.py``：
兼容 OpenAI 的 ``/chat/completions``、``QPSLimiter`` 令牌桶 + 并发闸。

**但有一处刻意不沿用。** 那边的 ``llm_chat()`` 把异常吞成
``"[DeepSeek错误] HTTP 500"`` 这样的字符串再拼进正文返回——调用方拿到的是一段
看起来正常的文本，无从判断该不该重试。本适配器把失败抛成 ``LLMError``，
再由端口层翻译成有类型的 ``Problem``。``fixtures/problem.invalid.json``
就是这个反例的可校验形态。

供应商细节止步于这一层：上游是档位名，下游是模型名与密钥，
两者之间的映射在本文件里完成，且映射来自配置（``model_tiers.*.model_ref``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from ..core.errors import DispatcherError
from ..core.policy import Policy
from ..core.settings import Settings, redact_secrets
from ..ports.llm import LLMError, LLMMessage, LLMResult

log = logging.getLogger(__name__)

# 模型有时会把 JSON 包在 ```json 围栏里，或在前后写一句话。
# 这里只做"把最外层的 JSON 对象抠出来"这一件事，不做任何修复性猜测——
# 猜测会让错误的数据看起来像正确的数据。
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_FIRST_BRACE = re.compile(r"[{\[]")


class QPSLimiter:
    """令牌桶 + 并发闸。

    两道限制不是冗余：QPS 限制的是**速率**（防止瞬时打满供应商额度），
    并发闸限制的是**同时在飞的请求数**（防止长尾请求堆积）。
    只有其中一道都会在另一维度上失控。
    """

    def __init__(self, qps: float, max_concurrency: int) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def _pace(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval

    class _Guard:
        def __init__(self, limiter: QPSLimiter) -> None:
            self._l = limiter

        async def __aenter__(self) -> None:
            await self._l._pace()
            await self._l._sem.acquire()

        async def __aexit__(self, *exc: object) -> None:
            self._l._sem.release()

    def slot(self) -> QPSLimiter._Guard:
        return QPSLimiter._Guard(self)


class OpenAICompatibleLLM:
    """按档位解析模型的 LLM 适配器。

    它持有 ``Policy``，因为"档位有哪些能力"是策略里的数据。
    模型名与密钥在**每次调用时**从设置里解析，而不是在构造时缓存——
    这样轮换密钥不需要重启进程。
    """

    def __init__(self, policy: Policy, settings: Settings) -> None:
        self._policy = policy
        self._settings = settings
        self._limiter = QPSLimiter(settings.llm_qps, settings.llm_max_concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_request_timeout_s),
            limits=httpx.Limits(max_connections=settings.llm_max_concurrency * 2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_policy(self, policy: Policy) -> None:
        """热换策略。**只换档位定义，不重建 HTTP 客户端**——
        自进化改的是档位的能力与价格，不是供应商连接。重建客户端会丢掉连接池，
        而切换策略本该是零成本的。"""
        self._policy = policy

    # ------------------------------------------------------------------
    def _resolve(self, tier: str, requires: tuple[str, ...]) -> tuple[str, str, str]:
        """档位 → (模型名, api_key, base_url)。需求不满足则拒绝。

        这里是"能力需求"与"档位能力"的接合点：一次纯子集测试。
        """
        declared = self._policy.model_tiers.get(tier)
        if declared is None:
            raise DispatcherError(
                "policy_violation", f"未知档位 {tier!r}", context={"tier": tier}
            )
        missing = sorted(set(requires) - set(declared.capabilities))
        if missing:
            # 让模型去做它做不到的事，比直接拒绝更糟：它会产出一个看起来合理的结果。
            raise DispatcherError(
                "no_capability_match",
                f"档位 {tier} 不具备所需能力：{missing}",
                context={"tier": tier, "missing_capabilities": missing},
            )
        model = self._settings.resolve_secret(declared.model_ref)
        api_key = self._settings.resolve_secret("secret://llm/api_key")
        base_url = self._settings.llm_base_url
        if not base_url:
            raise DispatcherError(
                "policy_violation",
                "未配置 LLM_BASE_URL。请填好 .env（该文件已被 .gitignore 忽略）。",
            )
        return model, api_key, base_url

    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tier: str,
        requires: tuple[str, ...] = (),
        temperature: float | None = None,
        timeout_ms: int | None = None,
        max_tokens: int | None = None,
        options: dict[str, Any] | None = None,
        json_mode: bool = False,
    ) -> LLMResult:
        model, api_key, base_url = self._resolve(tier, requires)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        if json_mode:
            # 声明即约束：档位的能力集合里没写 json_mode 就不能用。
            # 让一个不保证 JSON 的模型去干结构化输出，只会把失败推到解析那一跳。
            declared_caps = self._policy.model_tiers[tier].capabilities
            if "json_mode" not in declared_caps:
                raise DispatcherError(
                    "no_capability_match",
                    f"档位 {tier} 未声明 json_mode 能力，无法要求结构化输出",
                    context={"tier": tier, "requested": "json_mode"},
                )
            payload["response_format"] = {"type": "json_object"}

        # 供应商参数透传。**放在最后合并**，使配置可以覆盖上面的默认值——
        # 例如某个供应商的思考模式会强制覆盖 temperature，配置里就需要能表达这件事。
        if options:
            payload.update(options)

        timeout = (timeout_ms / 1000.0) if timeout_ms else None
        started = time.monotonic()

        last: LLMError | None = None
        for attempt in range(self._settings.llm_max_retries + 1):
            try:
                async with self._limiter.slot():
                    resp = await self._client.post(
                        f"{base_url}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=timeout,
                    )
            except httpx.TimeoutException as e:
                # 把**具体数值**写进消息。只说"请求超时"的话，排查时看不出是
                # 300ms 超了还是 30s 超了——而那决定了该改超时还是该查上游。
                # `e or "..."` 是错的：异常对象恒为真，所以那个兜底永远不会生效，
                # 而 httpx 的超时异常 str() 常常是空串——于是 detail 停在冒号后面什么都没有。
                # httpx 的原文改走 ``provider_detail``（只进日志）：它可能带主机名/URL，
                # 而 message 会一路回到客户端（审计「错误体泄露」）。
                reason = str(e).strip() or "上游未给出原因（连接超时/读超时都可能走到这里）"
                last = LLMError(
                    f"请求超时（{timeout if timeout else self._settings.llm_request_timeout_s}s，"
                    f"档位 {tier}）",
                    retryable=True, kind="transient",
                    provider_detail=redact_secrets(reason),
                )
            except httpx.HTTPError as e:
                # ``{e}`` 里可能带请求 URL/主机名，同样只进 provider_detail。
                last = LLMError(
                    "网络错误（无法连接到模型供应商）", retryable=True, kind="transient",
                    provider_detail=redact_secrets(str(e).strip() or "上游未给出原因"),
                )
            else:
                if resp.status_code >= 400:
                    last = self._http_error(resp)
                else:
                    return self._parse_ok(resp.json(), tier, model, started)

            if not last.retryable or attempt >= self._settings.llm_max_retries:
                raise last
            await asyncio.sleep(self._settings.llm_retry_backoff_s * (2**attempt))

        raise last or LLMError("未知失败")

    @staticmethod
    def _http_error(resp: httpx.Response) -> LLMError:
        """把供应商的 HTTP 状态翻译成本适配器的错误种类。

        401/403 是凭证问题、402 是余额问题——**它们不会因为重试或降级而好起来**，
        因此标成 fatal 让上层的降级逻辑绕开它们。把这些混进"上游抖动"，
        系统就会在密钥已失效的情况下继续安静地产出兜底结果。

        429 与 5xx 是抖动，重试有意义。

        **供应商响应体不进 ``message``。** 它可能带主机名、URL、模型名、key 片段、
        账单内容，而 ``message`` 会顺着 ``str(e)`` → ``Problem.detail`` → 任务快照
        回到客户端（审计「错误体泄露」）。原文截 300 字放进 ``provider_detail``：
        只被服务端日志与异常对象持有，并先过 ``redact_secrets``——供应商回显请求头
        （含 ``Authorization``）并不罕见。
        """
        detail = redact_secrets(resp.text[:300])
        code = resp.status_code
        if code in (401, 403):
            return LLMError(
                f"凭证被拒（{code}）", retryable=False, status=code, kind="auth",
                provider_detail=detail,
            )
        if code == 402:
            return LLMError(
                f"账户余额或配额不足（{code}）",
                retryable=False, status=code, kind="quota", provider_detail=detail,
            )
        if code == 429:
            return LLMError(
                f"供应商限流（{code}）", retryable=True, status=code, kind="transient",
                provider_detail=detail,
            )
        if code >= 500:
            return LLMError(
                f"供应商 {code}（服务端错误）", retryable=True, status=code,
                kind="transient", provider_detail=detail,
            )
        return LLMError(
            f"供应商 {code}（请求被拒）", retryable=False, status=code,
            kind="bad_request", provider_detail=detail,
        )

    # ------------------------------------------------------------------
    def _parse_ok(self, body: dict[str, Any], tier: str, model: str, started: float) -> LLMResult:
        try:
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"供应商响应结构异常：{e}", retryable=False) from e
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return LLMResult(
            text=text,
            tier=tier,
            model_resolved=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            # 深度思考的 token 计入 completion_tokens 并按输出计费。
            # 单独取出来是为了让成本可拆解：能看出"贵在思考还是贵在回答"。
            reasoning_tokens=details.get("reasoning_tokens"),
            finish_reason=choice.get("finish_reason"),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def extract_json(text: str) -> dict:
        """从模型输出里抠出最外层的 JSON 对象。

        只剥围栏、只从头找一个 ``{``——**不做任何修复性猜测**。
        猜出来的 JSON 会让错误的数据看起来像正确的数据，那比直接失败更糟。
        """
        cleaned = _FENCE.sub("", text).strip()
        start = _FIRST_BRACE.search(cleaned)
        if not start:
            raise ValueError("输出里找不到 JSON 的起始括号")
        if cleaned[start.start()] == "[":
            raise ValueError("期望一个 JSON 对象，得到的是数组")
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(cleaned[start.start():])
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败：{e}") from e
        if not isinstance(obj, dict):
            raise ValueError("顶层不是 JSON 对象")
        return obj

    async def generate_json(
        self,
        messages: list[LLMMessage],
        *,
        tier: str,
        requires: tuple[str, ...] = (),
        max_repair_attempts: int = 0,
        temperature: float | None = None,
        timeout_ms: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict, LLMResult]:
        convo = list(messages)
        last_err: Exception | None = None
        result: LLMResult | None = None

        for attempt in range(max_repair_attempts + 1):
            result = await self.complete(
                convo,
                tier=tier,
                requires=requires,
                temperature=temperature,
                timeout_ms=timeout_ms,
                options=options,
                # 结构化输出：档位声明了 json_mode 就用它。这只保证语法合法的 JSON，
                # 字段结构仍由提示词约束——因此解析后照样要过 TaskProfile 的严格构造。
                json_mode=True,
            )
            try:
                return self.extract_json(result.text), result
            except ValueError as e:
                last_err = e
                if attempt >= max_repair_attempts:
                    break
                # 把失败原因回灌，而不是原样重试——原样重试常常得到同样的坏输出
                convo = convo + [
                    LLMMessage.assistant(result.text),
                    LLMMessage.user(
                        f"你上一次的输出不是合法的 JSON 对象（{e}）。"
                        f"请只输出一个 JSON 对象，不要围栏、不要解释。"
                    ),
                ]
        # 失败时必须留下原始响应：上游只看到"计划没有任何节点"，
        # 真正的原因（模型吐了围栏外的文字？空 content？截断？）全在这段里。
        # 没有它，这条错误链只能靠猜——fin2 端到端时已经栽过一次。
        log.warning(
            "generate_json 失败：tier=%s attempts=%d err=%s | 原始响应: %s",
            tier,
            max_repair_attempts + 1,
            last_err,
            (result.text[:800] if result is not None and result.text else "<空>"),
        )
        raise LLMError(f"多次尝试后仍无法得到合法 JSON：{last_err}", retryable=False)


__all__ = ["OpenAICompatibleLLM", "QPSLimiter"]
