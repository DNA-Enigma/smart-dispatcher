"""LLMPort —— 模型访问的端口。

接口的形状本身就是一条约束：

    async def complete(self, messages, *, tier, requires=(), ...) -> LLMResult

**没有 model 参数。** 调用方给的是档位名与能力需求，端口负责验证该档位的能力
是否覆盖需求、并把档位解析成实际模型与密钥。因此调用方（handler、各阶段）
里不可能出现模型标识字符串——不是靠约定，是靠没有地方可写。

这是 ``docs/07-handler-seam.md`` 里"给 handler 的保证"第 2 条的实现：

    ctx.llm(messages, requires=["vision.extract"])

替换掉的是 ``duowei-ai/backend/app/core/llm_router.py`` ——那是个空壳透传，
正是接入档位路由的位置。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..core.errors import DispatcherError

Role = Literal["system", "user", "assistant"]

# content 可以是纯文本，也可以是 OpenAI 风格的内容片段列表
# （例如 [{"type": "text", ...}, {"type": "image_url", ...}]）。
# 多模态走这条通道，而不是把 base64 拼进文本——那是另一类"看起来能用"的做法。
Content = str | list[dict[str, Any]]


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    content: Content

    @staticmethod
    def system(content: str) -> LLMMessage:
        return LLMMessage(role="system", content=content)

    @staticmethod
    def user(content: Content) -> LLMMessage:
        return LLMMessage(role="user", content=content)

    @staticmethod
    def assistant(content: Content) -> LLMMessage:
        """用于把模型上一轮的输出回灌进对话（修复重试、多轮工具调用）。

        它和 system/user 一样是**构造器**而不是"模型才会产生的角色"——
        把上一轮的原始输出原样放回去，是让模型看到自己错在哪的唯一办法。
        """
        return LLMMessage(role="assistant", content=content)

    @staticmethod
    def user_with_images(text: str, images: list[tuple[str, str]]) -> LLMMessage:
        """``images`` 为 ``(data_uri, label)`` 列表。

        图像以内容片段形式与文本并列，而不是塞进字符串——这样模型能明确知道
        哪部分是图、哪部分是话，而我们也不必发明一套转义约定。
        """
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for data_uri, label in images:
            parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            if label:
                parts.append({"type": "text", "text": f"(上图：{label})"})
        return LLMMessage(role="user", content=parts)


class LLMResult(BaseModel):
    """一次调用的结果与遥测。

    ``tier`` 与 ``model_resolved`` 都不进入契约的对外字段——契约里只出现档位。
    它们用于 RunLog 与 04 的成本分析。
    """

    model_config = ConfigDict(extra="forbid")
    text: str
    tier: str
    model_resolved: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 深度思考产生的 token。**它们按输出计费**，所以必须单独记下来，
    # 否则成本估算会系统性偏低，而 04 也就看不到"思考在烧多少输出"。
    reasoning_tokens: int | None = None
    finish_reason: str | None = None


class LLMError(Exception):
    """适配器内部的失败，由端口层翻译成 ``upstream_llm_error``。

    存在的意义是让适配器不必知道契约的错误码表——那是端口层的事。

    ``kind`` 区分**可恢复的抖动**与**不会自己好的配置问题**，这个区分很重要：

    * ``transient`` —— 网络超时、5xx、限流。重试或降级有意义。
    * ``auth`` / ``quota`` / ``bad_request`` —— 密钥无效、余额不足、请求格式错。
      重试和降级都没有意义，而且**降级是有害的**：它会让系统在凭证已经失效的
      情况下继续安静地产出兜底结果，看起来一切正常，实际上每一条都是垃圾。
      这类错误必须一路上抛，让调用方立刻看到。

    这个区分不是想出来的——是把适配器接到真实供应商、拿到一个 402 余额不足
    之后才补上的：当时的实现会把它当成上游抖动降级掉。

    **``message`` 必须是本方写的分类文案，绝不拼进供应商的原始响应体。**
    它顺着 ``to_dispatcher_error()`` → ``Problem.detail`` → 任务快照一路回到客户端
    （审计项「错误体泄露」）。上游原文放在 ``provider_detail``：那个字段只进服务端
    日志与异常对象，用来排障，不对外。
    """

    FATAL_KINDS = frozenset({"auth", "quota", "bad_request"})

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status: int | None = None,
        kind: str = "transient",
        provider_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.kind = kind
        # 上游错误体（或网络异常原文）的截断副本，**仅供服务端排障**。
        # 构造方负责先过 ``redact_secrets``，免得把回显的密钥写进日志。
        self.provider_detail = provider_detail

    @property
    def fatal(self) -> bool:
        """不会自己好——不该被降级吞掉，应当一路上抛。"""
        return self.kind in self.FATAL_KINDS

    def to_dispatcher_error(self) -> DispatcherError:
        """翻译成契约里的错误类型。

        阶段层用它把 fatal 错误一路上抛成有类型的 ``Problem``，
        而不是让它变成 500 或一个安静的兜底画像。
        """
        detail = str(self)
        if self.kind == "auth":
            detail += "（凭证无效：请检查 LLM_API_KEY）"
        elif self.kind == "quota":
            detail += "（余额或配额不足：请到供应商控制台充值，或换一个 key）"
        return DispatcherError(
            "upstream_llm_error",
            detail,
            retryable=False,
            context={"provider_status": self.status, "kind": self.kind},
            # 上游原文进 ``internal``：它只被日志打印、不进 Problem 体，
            # 于是"细节留在服务端、request_id 在响应里"这两件事同时成立。
            internal=self.provider_detail,
        )


@runtime_checkable
class LLMPort(Protocol):
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
        """一次非流式补全。

        ``requires`` 是能力需求（如 ``vision.extract``），不是模型名。

        ``options`` 是**供应商参数透传**：调度层不认识里面的键，只是把它们从配置里
        读出来放进请求体。于是"某个供应商有个开关"这件事留在配置里，不渗进代码。
        ``json_mode`` 是标准参数（OpenAI 系通用），档位的能力集合里声明了
        ``json_mode`` 才生效——声明即约束。
        """
        ...

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
        """要求模型输出 JSON 对象。

        模型偶尔会包一层 ```json 围栏或在前面写一句话。``max_repair_attempts``
        允许在解析失败时重试若干次——这个次数来自策略（``evaluator.max_repair_attempts``
        之类），不写死在适配器里。用尽后抛出，由阶段层决定是降级还是失败。
        """
        ...

    async def aclose(self) -> None: ...


__all__ = ["LLMError", "LLMMessage", "LLMPort", "LLMResult", "Role"]
