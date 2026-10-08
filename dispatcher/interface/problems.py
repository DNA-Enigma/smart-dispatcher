"""Problem 响应体的唯一渲染点。

为什么值得单独一个模块：401 是在**中间件**里产生的，而中间件位于 Starlette 的
``ExceptionMiddleware`` 之外——在中间件里 ``raise DispatcherError`` 不会被
``@app.exception_handler(DispatcherError)`` 接住，它会一路冒到 ``ServerErrorMiddleware``
变成 500。因此中间件必须自己渲染响应。渲染逻辑抄第二份就一定会分叉（一份改了
``Retry-After``、另一份没改，这类分叉最难查），所以两处共用这一个函数。
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from ..core.errors import DispatcherError

log = logging.getLogger("dispatcher")


def problem_response(
    exc: DispatcherError,
    request_id: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    """把 ``DispatcherError`` 渲染成契约里的 Problem 实例。

    ``extra_headers`` 给 401 用（RFC 6750 要求 401 带 ``WWW-Authenticate``）；
    其余情况由 ``retry_after_ms`` 推出 ``Retry-After``。
    """
    headers: dict[str, str] = dict(extra_headers or {})
    if exc.retry_after_ms:
        headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
    if exc.internal:
        # ``internal`` 是"细节留在服务端"的落点（如供应商错误体原文）：
        # 它**不进响应体**，但必须和响应里的 ``request_id`` 对上号，否则线上排查
        # 只能靠时间猜。第一次把 request_id 拼进这条日志就是为了这件事。
        log.warning(
            "问题 %s request_id=%s（内部：%s）", exc.code, request_id or "-", exc.internal
        )
    return JSONResponse(
        status_code=exc.status,
        content=exc.to_problem(request_id),
        headers=headers,
    )


__all__ = ["problem_response"]
