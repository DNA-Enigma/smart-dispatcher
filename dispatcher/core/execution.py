"""工具执行的结果类型。

放在 ``core`` 而不是 ``ports``，是因为**执行器需要它们**，而 ``core`` 不该反向依赖
``ports``。``ports/handler.py`` 从这里导入以供协议签名使用——依赖方向是
``ports → core``，与其余部分一致。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolFailure(BaseModel):
    """有类型的失败。

    契约禁止把多个错误拼成一个字符串再往上抛（``duowei-ai`` 的 ``core/graph.py``
    用 ``"; "`` 拼接异常就是那个反例）——那样上层就没法按错误类型决定该重试、
    该跳过、还是该告诉用户。

    ``code`` 里有两个值有特殊含义，执行器会据此改变行为：

    * ``schema_validation_failed`` —— 输出不合声明的 schema，可触发**档位升级**
      （更强的模型往往能一次做对）；
    * ``upstream_unavailable`` —— 供应商不可用，执行器可降到健康档位。
    """

    model_config = ConfigDict(extra="forbid")
    code: str
    message: str = ""
    retryable: bool = False


class ConfirmationRequest(BaseModel):
    """handler 主动要求人工确认。

    **这不是异常路径。** 记账场景里"这张图是支出还是收入"必须问清楚——
    猜错会污染账目，而用户往往几个月后才发现。任务会停在
    ``awaiting_clarification``，答复后从这个节点继续，已完成节点的产物保留。
    """

    model_config = ConfigDict(extra="forbid")
    question: str
    options: list[dict[str, str]] = Field(default_factory=list)
    blocking: bool = True
    # 已确定的部分，答复后与它合并，避免从头再来
    partial: dict[str, Any] | None = None


class ToolResult(BaseModel):
    """一次工具调用的结果。

    ``cost`` 是**这个工具自己**产生的费用（例如一次外部 API 调用）。
    它通过 ``ctx.llm()`` 产生的模型费用**不由 handler 填**——那部分
    ``ctx`` 已经记过账了，让 handler 再记一次会重复计算。
    """

    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    cost: float = 0.0
    needs_confirmation: ConfirmationRequest | None = None

    @staticmethod
    def fail(code: str, message: str = "", *, retryable: bool = False) -> ToolResult:
        return ToolResult(ok=False, failure=ToolFailure(code=code, message=message, retryable=retryable))

    @staticmethod
    def confirm(
        question: str,
        options: list[dict[str, str]] | None = None,
        *,
        partial: dict[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            ok=True,
            needs_confirmation=ConfirmationRequest(
                question=question, options=options or [], blocking=True, partial=partial
            ),
        )


__all__ = ["ConfirmationRequest", "ToolFailure", "ToolResult"]
