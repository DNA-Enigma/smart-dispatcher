"""请求体读取与校验。

**进流水线之前请求体只有一条路**（这个模块），因为路径分叉过两次：

* 第一段：``clarify`` / ``feedback`` 走了 ``read_body``，而 ``create_task`` /
  ``approve`` / ``reject`` / ``rollback`` 四处是端点里裸 ``await request.json()``。
  分叉的代价是**错误形状不一致**：``Request.json()`` 抛的是 ``JSONDecodeError``，
  它不是 ``DispatcherError``，于是绕过 ``app.py`` 的处理器、冒到 ServerErrorMiddleware
  变成 500 纯文本——把**输入的错**报成**服务端的错**，客户端拿到的还不是契约里的
  ``Problem``。
* 第二段：那四处改用了 ``read_json_document``，**读取**统一了，但**校验**还是各写
  各的（``if not body.get("reason")``、``str(body.get("to_version") or "")``）。
  于是"缺必填"有的给 400 带 ``context.missing``、有的给一句手写的 detail，类型错
  则一概不查——``{"reason": 123}`` 会被 ``str()`` 悄悄转成 ``"123"`` 收下。
  现在这四处也走 ``read_body``，两条路才真的合成一条。

这里在**进入流水线之前**把协议边界该做的四件事补上：

* **请求体超限** → ``media_too_large``（413）。Content-Length 快速拒 + 边读边判。
* **必填缺失 / 类型不符** → ``invalid_request``（400）。这是请求的问题，不是任务的问题。
* **未知字段** → 记一条警告后**放行**（剥掉再传下去）。契约里写的是
  ``additionalProperties: false``，但直接拒绝会让"客户端先发、服务端后支持"的演进
  变成断崖；向前兼容优先，代价只是几行日志。
* **请求体为空 / 不是合法 JSON / 不是对象** → 400，而不是让 ``JSONDecodeError`` 冒成 500。

允许的字段集合**从 pydantic 模型现取**（``model_fields``），不在这里另抄一份：抄一份
就一定会和模型分叉，而分叉的契约比没有契约更难查（``openapi.yaml`` 里"契约与配置各写
一份允许列表，就一定会分叉"说的是同一件事）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ValidationError

from ..core.errors import DispatcherError
from ..core.settings import get_settings

log = logging.getLogger("dispatcher")


def _too_large(endpoint: str, limit: int, **observed: Any) -> DispatcherError:
    """413。用 ``media_too_large`` 是因为码表里只有这一个 413——
    ``schemas/problem.json`` 的 code 是封闭词表，新增码属于契约变更。"""
    return DispatcherError(
        "media_too_large",
        f"{endpoint} 的请求体超过上限 {limit} 字节",
        context={"limit_bytes": limit, **observed},
    )


async def read_raw_body(
    request: Request, *, endpoint: str, max_bytes: int | None = None
) -> bytes:
    """按上限读取请求体。

    **先全量读进内存再判超限等于没判**：``await request.body()`` 收完 10MiB 之后
    才比对上限，那时内存已经花掉了——一个连接就能把上限变成 OOM。这里两道：

    * ``Content-Length`` 声明超限 → 立刻 413，**一个字节都不读**（廉价快速拒）；
    * 边读边累计 → 越限立刻中断（客户端可以不带这个头，也可以把它写小）。

    ``max_bytes`` 缺省取 ``dispatcher_max_request_bytes``。媒体上传路径显式传入
    它自己的上限（见 ``app.py`` 的 ``upload_media``）。
    """
    limit = get_settings().dispatcher_max_request_bytes if max_bytes is None else max_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            # 超长数字串会在这里抛 ValueError（int() 有位数上限），
            # 与"不是数字"同样处理：交给下面按实读字节数判，而不是冒成 500。
            n = int(declared)
        except ValueError:
            n = -1
        if n > limit:
            raise _too_large(endpoint, limit, declared_bytes=n)

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise _too_large(endpoint, limit, read_bytes=len(buf))
    return bytes(buf)


async def read_json_document(
    request: Request,
    *,
    endpoint: str,
    allow_empty: bool = False,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """读一个 JSON 对象请求体。``allow_empty`` 时空体返回 ``{}``。

    没有对应模型的端点（``create_task`` 的包封由 :class:`TaskEnvelope` 之外的一层
    组装）用这个；有模型的用 ``read_body``，它在下面复用本函数。
    """
    raw_bytes = await read_raw_body(request, endpoint=endpoint, max_bytes=max_bytes)
    if not raw_bytes:
        if allow_empty:
            return {}
        raise DispatcherError("invalid_request", f"{endpoint} 的请求体不能为空")
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        raise DispatcherError(
            "invalid_request", f"{endpoint} 的请求体不是合法 JSON：{e}"
        ) from e
    if not isinstance(raw, dict):
        raise DispatcherError(
            "invalid_request", f"{endpoint} 的请求体必须是 JSON 对象"
        )
    return raw


async def read_body(
    request: Request,
    model: type[BaseModel],
    *,
    required: tuple[str, ...] = (),
    endpoint: str,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """读并校验请求体，返回**已剥掉未知字段**的 dict，可直接交给流水线。

    ``required`` 是协议层的必填字段（对应 ``openapi.yaml`` 的 ``required``），
    与模型的默认值无关：``ClarificationAnswer`` 每个字段都有默认值（流水线能从
    记录里回填），但线路上仍要求客户端回传 ``question_id`` 以确认答的是哪一问。

    ``allow_empty`` 给"整段请求体可缺"的端点用（``approve`` 的 ``openapi.yaml``
    写着 ``requestBody.required: false``）。它只放宽"空体"这一种，非法 JSON 与
    非对象仍然是 400——"没带参数"与"带了一段看不懂的东西"不是同一件事。

    读取与解析复用 ``read_json_document``，**不再自己 ``request.body()``**——
    两条读取路径就是两份上限判定与两套错误形状，那正是本轮要合掉的东西。
    """
    raw = await read_json_document(request, endpoint=endpoint, allow_empty=allow_empty)

    missing = [f for f in required if raw.get(f) is None]
    if missing:
        raise DispatcherError(
            "invalid_request",
            f"{endpoint} 缺少必填字段：{', '.join(missing)}",
            context={"missing": missing, "required": list(required)},
        )

    known = set(model.model_fields)
    unknown = sorted(k for k in raw if k not in known)
    if unknown:
        log.warning(
            "%s 收到未知字段，已忽略（向前兼容，不拒绝）：%s",
            endpoint, ", ".join(unknown),
        )

    payload = {k: v for k, v in raw.items() if k in known}
    try:
        model.model_validate(payload)
    except ValidationError as e:
        # 只取 JSON 可序列化的部分：``e.errors()`` 的 ctx 可能挂着异常对象，
        # 直接塞进 Problem 体里会在渲染响应时二次炸掉。
        errors = [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
            for err in e.errors()
        ]
        raise DispatcherError(
            "invalid_request",
            f"{endpoint} 的请求体类型不符：{errors}",
            context={"errors": errors},
        ) from e
    return payload


__all__ = ["read_body", "read_json_document", "read_raw_body"]
