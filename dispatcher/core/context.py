"""``DispatchContext`` —— handler 唯一能看到的世界。

这个类的形状本身就是一组约束，而且是有牙齿的那种：

* ``llm()`` 收的是**能力需求**（``requires=["vision.extract"]``），不是模型名。
  handler 连模型标识字符串都没有地方可写——不是靠约定，是靠签名里没有那个参数。
* ``budget`` 是**窄接口**：只能记账、只能看剩余。改上限、看别人的账这些
  "handler 不该有能力做的事"在类型上就不存在。
* ``emit()`` 是唯一的上报通道。handler 不能改任务状态——状态机归调度层。

这些约束不是为了让 handler 难写，而是为了让"领域逻辑"与"编排决策"真的分开：
分开之后，接一个新领域才不需要改调度层。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..ports.llm import LLMMessage, LLMPort, LLMResult
from ..ports.media import MediaRecord, MediaStorePort
from ..ports.state import StateStorePort
from .budget import BudgetHandle
from .cancel import CancellationToken
from .errors import DispatcherError
from .eventbus import EventBus
from .events import EventRecord
from .policy import Policy
from .pricing import Pricing

_DATA_URI = "data:{mime};base64,{payload}"


class MediaResolver:
    """把 ``media_id`` 解析成字节或 data URI。

    handler 拿到的是**已经过 MIME 与大小校验**的内容，因此不需要写防御式检查——
    那些检查在更早、更便宜的地方做过一次了（见 ``InMemoryMediaStore.put``）。
    """

    def __init__(self, store: MediaStorePort) -> None:
        self._store = store

    async def resolve(self, media_id: str) -> tuple[bytes, MediaRecord]:
        got = await self._store.get(media_id)
        if got is None:
            raise DispatcherError(
                "unsupported_media",
                f"媒体不存在或已过保留期：{media_id}",
                context={"media_id": media_id},
            )
        return got

    async def data_uri(self, media_id: str) -> str:
        blob, rec = await self.resolve(media_id)
        return _DATA_URI.format(mime=rec.mime, payload=base64.b64encode(blob).decode("ascii"))

    async def stat(self, media_id: str) -> MediaRecord | None:
        return await self._store.stat(media_id)


@dataclass
class DispatchContext:
    """一次节点执行能接触到的一切。

    刻意做成 dataclass 而不是带方法的服务：它的字段就是权限清单，
    想给 handler 多一项能力，必须在这里显式加一个字段——那是一处需要被评审的改动。
    """

    # -- 身份 ----------------------------------------------------------
    task_id: str
    subtask_id: str
    tenant_id: str
    user_id: str
    trace_id: str
    route_id: str

    # -- 能力 ----------------------------------------------------------
    media: MediaResolver
    config: dict[str, Any]
    state: StateStorePort
    budget: BudgetHandle
    cancellation: CancellationToken
    events: EventBus

    # 下面这些不由 handler 直接使用，是给 ``llm()`` 的实现用的
    _policy: Policy = field(repr=False)
    _pricing: Pricing = field(repr=False)
    _llm: LLMPort = field(repr=False)
    _allowed_tiers: list[str] = field(repr=False, default_factory=list)
    _node_tier: str | None = field(repr=False, default=None)
    # 节点级的供应商参数（来自策略的 decomposer.node_defaults.options）。
    # 放在这里而不是让每个 handler 自己传：**"节点执行要不要开深度思考"是策略问题，
    # 不是 handler 问题**。让 handler 各自记得传，就等于制造了一堆会忘记的地方。
    _node_options: dict[str, Any] = field(repr=False, default_factory=dict)
    logger: logging.Logger = field(
        repr=False, default_factory=lambda: logging.getLogger("dispatcher.handler")
    )

    # -- 派生 ----------------------------------------------------------
    @property
    def idempotency_token(self) -> str:
        """按 ``(task_id, subtask_id)`` 派生的**稳定** token。

        重试拿到的是同一个值，因此写操作可以据此做到"重试不会重复入账"。
        派生而不是随机，是这条保证的全部依据：随机会让重试看起来像一次新操作。
        """
        return f"{self.task_id}:{self.subtask_id}"

    def now(self) -> datetime:
        return datetime.now(UTC)

    # -- 模型访问 ------------------------------------------------------
    def resolve_tier(self, requires: tuple[str, ...] | list[str]) -> str | None:
        """能力需求 → 档位。纯子集测试，没有映射表。

        返回 ``None`` 表示**这一步不需要模型**（``requires`` 为空）——
        调用方据此完全跳过模型调用。纯算术步骤就该走这条路。
        """
        need = list(requires)
        if not need:
            return None
        if self._node_tier and self._policy.satisfies(self._node_tier, need):
            return self._node_tier
        return self._policy.resolve_tier(need, self._allowed_tiers or list(self._policy.model_tier_ids))

    async def llm(
        self,
        messages: list[LLMMessage],
        *,
        requires: tuple[str, ...] = ("text",),
        temperature: float | None = None,
        timeout_ms: int | None = None,
        json_mode: bool = False,
        options: dict[str, Any] | None = None,
        note: str = "",
    ) -> LLMResult:
        """按能力调用模型，并把费用记到本任务账上。

        handler **不能**指定模型。它说"我需要视觉能力"，解析与绑定在这一层完成。
        """
        tier = self.resolve_tier(requires)
        if tier is None:
            raise DispatcherError(
                "no_capability_match",
                f"节点未声明能力需求，却发起了模型调用：requires={list(requires)}",
                context={"subtask_id": self.subtask_id, "requires": list(requires)},
            )
        result = await self._llm.complete(
            messages,
            tier=tier,
            requires=tuple(requires),
            temperature=temperature,
            timeout_ms=timeout_ms,
            json_mode=json_mode,
            # 节点默认参数打底，调用方显式给的覆盖它
            options={**self._node_options, **(options or {})},
        )
        cost = self._pricing.cost_of(tier, result.input_tokens, result.output_tokens)
        self.budget.charge(cost, subtask_id=self.subtask_id, note=note or f"llm:{tier}")
        return result

    async def llm_json(
        self,
        messages: list[LLMMessage],
        *,
        requires: tuple[str, ...] = ("text",),
        max_repair_attempts: int = 0,
        temperature: float | None = None,
        timeout_ms: int | None = None,
        options: dict[str, Any] | None = None,
        note: str = "",
    ) -> tuple[dict[str, Any], LLMResult]:
        """要模型输出 JSON，**并且记账**。

        与 ``llm()`` 的唯一区别是它会解析 JSON 并做修复重试。分开两个方法而不是
        加一个布尔参数，是因为"要不要 JSON"会改变返回值类型——用参数区分会让
        返回类型依赖于参数值，调用方得自己判断拿到的是什么。
        """
        tier = self.resolve_tier(requires)
        if tier is None:
            raise DispatcherError(
                "no_capability_match",
                f"节点未声明能力需求，却发起了模型调用：requires={list(requires)}",
                context={"subtask_id": self.subtask_id, "requires": list(requires)},
            )
        raw, result = await self._llm.generate_json(
            messages,
            tier=tier,
            requires=tuple(requires),
            max_repair_attempts=max_repair_attempts,
            temperature=temperature,
            timeout_ms=timeout_ms,
            options={**self._node_options, **(options or {})},
        )
        cost = self._pricing.cost_of(tier, result.input_tokens, result.output_tokens)
        self.budget.charge(cost, subtask_id=self.subtask_id, note=note or f"llm_json:{tier}")
        return raw, result

    # -- 上报 ----------------------------------------------------------
    async def emit(self, type: str, data: dict[str, Any] | None = None) -> EventRecord:
        """上报进度或部分结果。这是 handler 触达用户的唯一通道。"""
        return await self.events.emit(
            self.task_id, type, data or {}, subtask_id=self.subtask_id
        )


__all__ = ["DispatchContext", "MediaResolver"]
