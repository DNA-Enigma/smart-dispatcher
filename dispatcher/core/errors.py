"""结构化错误。

契约要求错误必须是有类型的、可传播的。对照反例：
``duowei-ai/backend/app/core/llm_client.py::llm_chat()`` 把异常吞成
``"[DeepSeek错误] HTTP 500"`` 这样的字符串哨兵再拼进正文——调用方既无法判断
该不该重试，也没法按错误类型分支处理。``fixtures/problem.invalid.json``
就是那个反例的可校验形态。

这里的码表是**协议结构**，不是可调策略：每个码对应一个 HTTP 状态与一个
默认重试语义，改它等于改契约（``schemas/problem.json`` 的 ``code`` 枚举）。
因此它写在代码里而不是配置里——配置放的是可调的量，不是协议。
"""

from __future__ import annotations

from typing import Any, Final

# code -> (HTTP 状态, 默认是否可重试)
# 与 schemas/problem.json 的 code 枚举一一对应，顺序也保持一致。
ERROR_TABLE: Final[dict[str, tuple[int, bool]]] = {
    "unauthorized": (401, False),
    "invalid_request": (400, False),
    "unsupported_media": (415, False),
    "media_too_large": (413, False),
    "idempotency_conflict": (409, False),
    "no_capability_match": (422, False),
    "budget_exceeded": (402, False),
    "policy_violation": (422, False),
    "handler_error": (502, False),
    "upstream_llm_error": (502, True),
    "timeout": (504, True),
    "cancelled": (499, False),
    "rate_limited": (429, True),
    "not_found": (404, False),
    "result_not_ready": (409, True),
}

ERROR_TITLES: Final[dict[str, str]] = {
    "unauthorized": "Missing or invalid credentials",
    "invalid_request": "Request does not satisfy the schema",
    "unsupported_media": "Unsupported media type",
    "media_too_large": "Media exceeds the size limit",
    "idempotency_conflict": "Idempotency key reused with a different body",
    "no_capability_match": "No registered handler satisfies the required capabilities",
    "budget_exceeded": "Cost exceeds the configured ceiling",
    "policy_violation": "The guard could not produce a legal decision",
    "handler_error": "The capability handler raised",
    "upstream_llm_error": "The model provider failed",
    "timeout": "Wall-clock budget exceeded",
    "cancelled": "Cancelled by the client",
    "rate_limited": "Rate limited",
    "not_found": "Resource not found",
    "result_not_ready": "The task has not reached a terminal state",
}

_ERROR_TYPE_BASE: Final[str] = "https://smart-dispatcher/errors/"

# 这些错误**不会因为"再试一次"或"降级继续"而好起来**，因此绝不能被降级路径吞掉，
# 也不该被"建一个任务然后把它标记成失败"这种写法糊过去。
#
# 这条清单是踩出来的，不是想出来的。两处具体的教训：
#
# * 密钥没配时 ``resolve_secret`` 抛 policy_violation，而评估器把它当成上游抖动
#   降级掉，于是一路降级到兜底画像 + 兜底路由，任务"成功"返回。
#   **密钥为空而系统看起来在正常工作**——比直接报错危险得多。
# * 媒体不存在时抛 unsupported_media，被转成了"任务失败"。但那是**请求**的问题，
#   不是**任务**的问题：正确行为是当场返回 415，而不是先建个任务再宣布它失败——
#   后者会在任务列表里留下一堆其实从未开始的"失败任务"，污染失败率。
NON_DEGRADABLE_CODES: Final[frozenset[str]] = frozenset(
    {
        "unauthorized",
        "policy_violation",
        "no_capability_match",
        "invalid_request",
        "unsupported_media",
        "media_too_large",
        "idempotency_conflict",
    }
)


class DispatcherError(Exception):
    """所有对外可见的失败都走这里。

    ``internal`` 只用于日志与排查，绝不进入响应体——响应体里放的是契约允许的字段，
    多一个字段就是契约泄漏。
    """

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        retryable: bool | None = None,
        task_id: str | None = None,
        retry_after_ms: int | None = None,
        context: dict[str, Any] | None = None,
        internal: str | None = None,
    ) -> None:
        if code not in ERROR_TABLE:
            # 契约里 code 是封闭词表，写错一个码就是契约违规，早失败早发现。
            raise ValueError(f"unknown problem code: {code!r} (see schemas/problem.json)")
        self.code = code
        self.detail = detail
        self.status, default_retryable = ERROR_TABLE[code]
        self.retryable = default_retryable if retryable is None else retryable
        self.task_id = task_id
        self.retry_after_ms = retry_after_ms
        self.context = context or {}
        self.internal = internal
        super().__init__(f"{code}: {detail}" if detail else code)

    @property
    def fatal(self) -> bool:
        """是否应当一路上抛，而不是被降级路径吞掉。

        两类：**配置/程序错误**（重试与降级都没意义），以及**不可重试的上游错误**
        （凭证失效、余额不足——降级只会让系统在明知不可用的情况下继续产出兜底结果）。
        """
        if self.code in NON_DEGRADABLE_CODES:
            return True
        return self.code == "upstream_llm_error" and not self.retryable

    def to_problem(self, request_id: str) -> dict[str, Any]:
        """渲染成 ``schemas/problem.json`` 的实例。"""
        problem: dict[str, Any] = {
            "type": _ERROR_TYPE_BASE + self.code.replace("_", "-"),
            "title": ERROR_TITLES[self.code],
            "status": self.status,
            "code": self.code,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if self.detail:
            problem["detail"] = self.detail
        if self.task_id is not None:
            problem["task_id"] = self.task_id
        if self.retry_after_ms is not None:
            problem["retry_after_ms"] = self.retry_after_ms
        if self.context:
            problem["context"] = self.context
        return problem


__all__ = ["ERROR_TABLE", "ERROR_TITLES", "DispatcherError"]
