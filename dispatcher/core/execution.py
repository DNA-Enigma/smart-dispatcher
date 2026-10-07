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


class ClarificationAnswer(BaseModel):
    """用户对一次 :class:`ConfirmationRequest` 的答复。

    与 :class:`ConfirmationRequest` 成对——**问了什么在这里，答了什么在那里**。
    分开而不是塞回同一个类型，是因为两者的时机、来源与可信度都不同：
    问是 handler 产出的，答是**外部输入**（客户端提交的 JSON）。

    没有这个类型时 ``ToolResult.confirm`` 是**单程**的：任务停下来问了用户，
    答复却永远到不了提问的那一步，于是 handler 只能在第二次被调用时凭本地状态
    猜"用户大概是同意了吧"。凭证那条路看着能用，是因为客户端必须绕道把修改塞进
    ``clarify`` 的 ``edits``（按节点 id 覆盖上游产出），**不是因为回答被收到了**。
    """

    model_config = ConfigDict(extra="forbid")
    question_id: str | None = None
    answer_id: str | None = None
    free_text: str | None = None
    # 用户对已抽取字段的修改：按节点 id 覆盖上游产出（见 pipeline.clarify）
    edits: dict[str, Any] | None = None

    @property
    def is_cancel(self) -> bool:
        """保留选项 id：``cancel`` = "别做了"。

        它由**调度层**处理：任务直接置为 cancelled，暂停的那个节点不再执行。
        这样每个 handler 不必各自实现一遍"取消"，而两个模板里已经在用的
        ``{id: cancel, label: 不用建了}`` 从散文变成了语义。

        判定用 ``answer_id`` 而不是 label——label 是给人看的文案，可以随便改；
        id 是机器要认的东西。
        """
        return (self.answer_id or "").strip().lower() == "cancel"


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


__all__ = ["ClarificationAnswer", "ConfirmationRequest", "ToolFailure", "ToolResult"]
