"""直答执行器 —— 契约里的第三种路径（``path=direct_llm``）。

契约把 ``path`` 定义成**结构性**词表，并写明它"对应实现里的三种执行器"
（``docs/08-config-model.md``）。但只有两种被执行过：单步工具与拆解。``direct_llm``
从来没接上，于是走这条路由的请求落进拆解器，被套进"单步工具调用"的形状——
一个 ``handler=""``、``tool=None`` 的节点——执行器在 ``registry.executable("")``
上拿到 ``None``，任务以 **502 ``handler_error``**「handler  已声明但没有可执行实现」
收场。消费端看到的现象正是"问账、归类都报 502"。

**为什么它不是 ``handlers/`` 下的一个 handler。** 这一条在契约里是封闭的，不是取舍：

* ``schemas/route_decision.json`` 的 allOf 规定 ``path=direct_llm`` 时 ``handler``
  必须为 ``null``、``tool_set`` 必须为空（``fixtures/route_decision.invalid.json``
  就是给它当反例的）；
* ``direct_answer`` 路由声明着 ``requires_handler: false``，``PolicyGuard`` 据此
  **主动清空** handler 与工具集（"direct_llm 路径上根本没有 handler 的概念"）。

因此往 ``handlers/`` 里挂一个 handler 不会被执行：它会在守卫那一跳被抹掉，
502 照旧，只是多了一个永远不被调用的目录。直答没有领域、也没有工具，它就是
一次补全——所以它在这里，与评估、路由、拆解并列，而不是在 ``handlers/`` 里。

**直答没有计划。** 把它硬塞进 DAG 的形状（哪怕只有一个节点）正是上面那个 bug 的
来源：那个节点既不是工具也不是 agent，它什么都不做。一条没有步骤的路径在状态里
就该是"没有计划"——快照的 ``plan`` 为 ``null``，``progress`` 按终态折算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.contract import RouteDecision, TaskEnvelope
from ..core.errors import DispatcherError
from ..core.policy import Policy
from ..core.pricing import Pricing
from ..core.prompts import PromptLibrary, data_block
from ..ports.llm import LLMError, LLMMessage

DIRECT_PROMPT = "direct_answer.md"


@dataclass
class DirectOutcome:
    """一次直答的结果与遥测。

    ``structured`` 为 ``None`` 表示模型没按约定给 JSON（见 :meth:`DirectAnswerer.answer`
    的降级说明）；``answer`` 在任何情况下都是**给用户看的那段话**。
    """

    answer: str
    tier: str
    latency_ms: int = 0
    confidence: float | None = None
    structured: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    notes: list[str] = field(default_factory=list)

    def artifact(self) -> dict[str, Any]:
        """进 ``artifacts`` 的形状。

        没有 ``subtask_id`` 可挂——直答没有节点。因此是平的，而不是
        ``{"main": {...}}``：一个名为 ``main`` 的键会让人以为执行过某个节点，
        而这里什么都没有执行。
        """
        out: dict[str, Any] = {
            "answer": self.answer,
            "tier": self.tier,
            "latency_ms": self.latency_ms,
        }
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if not self.structured:
            # **降级要留痕。** 模型没给约定形状时答案仍然可用，但这是一次
            # "约定没被遵守"，不说出来的话，提示词或供应商悄悄变了也没人知道。
            out["degraded"] = "unstructured_output"
        return out


class DirectAnswerer:
    """一次补全，没有工具、没有拆解、没有节点。"""

    def __init__(
        self,
        *,
        policy: Policy,
        pricing: Pricing,
        prompts: PromptLibrary,
        llm: Any,
    ) -> None:
        self._policy = policy
        self._pricing = pricing
        self._prompts = prompts
        self._llm = llm

    async def answer(
        self,
        envelope: TaskEnvelope,
        decision: RouteDecision,
        *,
        budget: Any | None = None,
    ) -> DirectOutcome:
        cfg = self._policy.direct_llm
        # 档位来自**决策**，不是本段配置：路由已经按 allowed_tiers 选过了。
        tier = decision.model_tier
        messages = [
            LLMMessage.system(self._prompts.get(DIRECT_PROMPT)),
            # 用户文本永远进 user 槽位、永远带数据围栏。
            LLMMessage.user(data_block("用户请求", _request_text(envelope))),
        ]
        try:
            # 走 ``complete`` 而不是 ``generate_json``：**要交付的是那段话，
            # JSON 只是它的包装**。``generate_json`` 在解析不出 JSON 时抛错，
            # 于是"模型直接说了一段话"这个最可能发生的降级情形反而会失败——
            # 而那段话正是用户要的。这里拿回原文自己宽容解析：解得出来就用
            # 结构化字段（answer / confidence），解不出来就把原文当答案，
            # 并在 artifact 上留一个 degraded 痕迹。
            res = await self._llm.complete(
                messages,
                tier=tier,
                requires=tuple(cfg.requires),
                timeout_ms=cfg.timeout_ms,
                options=cfg.options,
                json_mode=True,
            )
        except LLMError as e:
            # 直答没有可降级的东西——降级成一段"兜底回答"就等于编话给用户。
            # 但两类错误必须分开处置（与 errors.py 的 fatal 语义一致）：
            #   凭证失效 / 余额不足 —— 一路上抛，调用方当场看见（别糊成一条"任务失败"，
            #                          否则系统会在明知不可用的情况下继续接单）；
            #   上游抖动            —— 任务以 upstream_llm_error 失败，且标可重试，
            #                          这样它是"failed"而不是"rejected"。
            if e.fatal:
                raise e.to_dispatcher_error() from e
            raise DispatcherError(
                "upstream_llm_error", str(e), retryable=True,
                context={"provider_status": e.status, "kind": e.kind},
            ) from e

        if budget is not None and self._pricing is not None:
            budget.charge(
                self._pricing.cost_of(tier, res.input_tokens, res.output_tokens),
                subtask_id="__direct_llm__",
                note="stage:direct_llm",
            )

        return self._outcome(res, tier)

    @staticmethod
    def _outcome(res: Any, tier: str) -> DirectOutcome:
        """把一次补全读成结果。

        两条分支的区别是**模型有没有打算用那个包装**，而不是"解不解得出来"：

        * 解出来是对象 —— 它在按约定说话。那就要求 ``answer`` 非空：一个空的
          ``answer`` 是"这次没答出来"，不是"降级成纯文本"，不能拿整段 JSON
          当答案交给用户。
        * 解出来不是对象 —— 它直接用散文答了。那段话就是要交付的东西，收下，
          但记一个 ``degraded`` 痕迹（提示词或供应商哪天悄悄变了，翻产物能看出来）。
        """
        parsed = _parse(res.text)
        confidence: Any = None
        if parsed:
            answer = str(parsed.get("answer") or "").strip()
            if not answer:
                raise DispatcherError(
                    "upstream_llm_error",
                    f"直答返回了空的 answer 字段（档位 {tier}，"
                    f"finish_reason={res.finish_reason}）",
                    retryable=True,
                )
            confidence, structured = parsed.get("confidence"), True
            notes: list[str] = []
        else:
            answer = res.text.strip()
            if not answer:
                raise DispatcherError(
                    "upstream_llm_error",
                    f"直答返回了空内容（档位 {tier}，finish_reason={res.finish_reason}）",
                    retryable=True,
                )
            structured = False
            notes = ["模型未按约定输出 JSON，已按纯文本收下这段答案"]
        return DirectOutcome(
            answer=answer,
            tier=tier,
            latency_ms=res.latency_ms,
            confidence=confidence if isinstance(confidence, (int, float)) else None,
            structured=structured,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            notes=notes,
        )


def _parse(text: str) -> dict[str, Any]:
    """宽容解析 ``{"answer": ..., "confidence": ...}``。

    围栏、前后多余的话都容忍，与适配器里对模型输出的宽容一致。解不出来返回空
    dict（表示"这不是一个 JSON 对象"），由调用方决定怎么处置——**这个函数不做
    判断**，它只回答"原文里有没有一个 JSON 对象"。
    """
    cleaned = text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _request_text(envelope: TaskEnvelope) -> str:
    parts = [envelope.input.text or ""]
    if envelope.declared.intent:
        # 调用方自己声明的意图是**有用的提示**（"这是一个解释类请求"），
        # 但它同样来自外部，因此放在数据块里而不是系统槽位。
        parts.append(f"[调用方声明意图：{envelope.declared.intent}]")
    for m in envelope.input.media or []:
        parts.append(f"[媒体 {m.media_id}，{m.kind}/{m.mime}]")
    return "\n".join(p for p in parts if p) or "（用户没有给出文字）"


__all__ = ["DIRECT_PROMPT", "DirectAnswerer", "DirectOutcome"]
