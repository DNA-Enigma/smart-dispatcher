"""测试替身。

``ScriptedLLM`` 按调用顺序返回预置的 JSON。它比 mock 更好用的地方是：
它遵守真实的 ``LLMPort`` 协议（同样的签名、同样的错误类型），因此流水线在测试里
走过的代码路径与生产一致——除了网络那一跳。

它还记录每一次调用，于是测试可以断言"渲染出来的菜单里确实包含策略里的路由"
这类关于**提示词内容**的事实，而不只是断言最终结果。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dispatcher.ports.llm import LLMError, LLMMessage, LLMResult


@dataclass
class RecordedCall:
    messages: list[LLMMessage]
    tier: str
    requires: tuple[str, ...]
    temperature: float | None
    options: dict
    json_mode: bool

    @property
    def system_text(self) -> str:
        for m in self.messages:
            if m.role == "system" and isinstance(m.content, str):
                return m.content
        return ""

    @property
    def user_text(self) -> str:
        parts: list[str] = []
        for m in self.messages:
            if m.role != "user":
                continue
            if isinstance(m.content, str):
                parts.append(m.content)
            else:
                parts.extend(
                    p.get("text", "") for p in m.content if p.get("type") == "text"
                )
        return "\n".join(parts)

    def has_image(self) -> bool:
        return any(
            isinstance(m.content, list) and any(p.get("type") == "image_url" for p in m.content)
            for m in self.messages
        )


class ScriptedLLM:
    """按顺序返回预置响应。

    ``responses`` 里的元素可以是 dict（会被 json.dumps）或 str（原样返回，
    用于测试"模型吐了非 JSON"的路径）。

    ``fail_first_n`` 让**前 N 次调用**失败、之后恢复正常。用来构造"评估器那一次
    挂了、后面几步还好"的场景（P0-1c 正是这样：降级发生在 01，任务却要继续走完
    02/03）。不传（``None``）时保持原语义——只要给了 ``fail_with`` 就每次都失败。
    """

    def __init__(
        self,
        responses: list[Any],
        *,
        fail_with: Exception | None = None,
        fail_first_n: int | None = None,
    ) -> None:
        self._responses = list(responses)
        self._fail_with = fail_with
        self._fail_first_n = fail_first_n
        self._n_calls = 0
        self.calls: list[RecordedCall] = []
        self.tiers_used: list[str] = []

    def _maybe_fail(self) -> None:
        """记录调用次数，并在前 ``fail_first_n`` 次内抛出预置错误。"""
        self._n_calls += 1
        if self._fail_with is None:
            return
        if self._fail_first_n is None or self._n_calls <= self._fail_first_n:
            raise self._fail_with

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        tier: str,
        requires: tuple[str, ...] = (),
        temperature: float | None = None,
        timeout_ms: int | None = None,
        max_tokens: int | None = None,
        options: dict | None = None,
        json_mode: bool = False,
    ) -> LLMResult:
        self.calls.append(
            RecordedCall(
                messages=list(messages), tier=tier, requires=tuple(requires),
                temperature=temperature, options=options or {}, json_mode=json_mode,
            )
        )
        self.tiers_used.append(tier)
        self._maybe_fail()
        if not self._responses:
            raise LLMError("ScriptedLLM 没有更多预置响应了", retryable=False)
        item = self._responses.pop(0)
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return LLMResult(
            text=text, tier=tier, model_resolved=f"scripted:{tier}",
            latency_ms=1, input_tokens=10, output_tokens=5, finish_reason="stop",
        )

    async def generate_json(
        self,
        messages: list[LLMMessage],
        *,
        tier: str,
        requires: tuple[str, ...] = (),
        max_repair_attempts: int = 0,
        temperature: float | None = None,
        timeout_ms: int | None = None,
        options: dict | None = None,
    ) -> tuple[dict, LLMResult]:
        self.calls.append(
            RecordedCall(
                messages=list(messages), tier=tier, requires=tuple(requires),
                temperature=temperature, options=options or {}, json_mode=True,
            )
        )
        self.tiers_used.append(tier)
        self._maybe_fail()
        if not self._responses:
            raise LLMError("ScriptedLLM 没有更多预置响应了", retryable=False)
        item = self._responses.pop(0)
        if isinstance(item, str):
            # 模拟模型输出非 JSON：真实适配器会重试，这里直接抛，
            # 让阶段层的降级路径被走到
            raise LLMError(f"无法解析为 JSON：{item[:40]!r}", retryable=False)
        return item, LLMResult(
            text=json.dumps(item, ensure_ascii=False), tier=tier,
            model_resolved=f"scripted:{tier}", latency_ms=1,
            input_tokens=10, output_tokens=5, finish_reason="stop",
        )

    async def aclose(self) -> None:
        return None


@dataclass
class FakeVisionLLM(ScriptedLLM):
    """记录是否收到了图像内容片段。用于断言"带图请求确实走了多模态通道"。"""

    saw_image: bool = field(default=False)

    async def generate_json(self, messages, **kw):  # type: ignore[override]
        if any(
            isinstance(m.content, list) and any(p.get("type") == "image_url" for p in m.content)
            for m in messages
        ):
            self.saw_image = True
        return await super().generate_json(messages, **kw)


__all__ = ["FakeVisionLLM", "RecordedCall", "ScriptedLLM"]
