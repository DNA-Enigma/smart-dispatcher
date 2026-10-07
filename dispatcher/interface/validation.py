"""请求体校验。

``clarify`` / ``feedback`` 此前是裸 ``await request.json()``：字段名写错既不报错也不
生效——请求的问题被静默吞掉，调用方看到的是一次"成功"的空操作。更糟的是形状不对时
异常一路冒到流水线里变成 500，把**输入的错**报成**服务端的错**，排查方向直接被带偏。

这里在**进入流水线之前**把协议边界该做的三件事补上：

* **必填缺失 / 类型不符** → ``invalid_request``（400）。这是请求的问题，不是任务的问题。
* **未知字段** → 记一条警告后**放行**（剥掉再传下去）。契约里写的是
  ``additionalProperties: false``，但直接拒绝会让"客户端先发、服务端后支持"的演进
  变成断崖；向前兼容优先，代价只是几行日志。
* **请求体为空 / 不是合法 JSON** → 同上 400，而不是让 ``JSONDecodeError`` 冒成 500。

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

log = logging.getLogger("dispatcher")


async def read_body(
    request: Request,
    model: type[BaseModel],
    *,
    required: tuple[str, ...] = (),
    endpoint: str,
) -> dict[str, Any]:
    """读并校验请求体，返回**已剥掉未知字段**的 dict，可直接交给流水线。

    ``required`` 是协议层的必填字段（对应 ``openapi.yaml`` 的 ``required``），
    与模型的默认值无关：``ClarificationAnswer`` 每个字段都有默认值（流水线能从
    记录里回填），但线路上仍要求客户端回传 ``question_id`` 以确认答的是哪一问。
    """
    raw_bytes = await request.body()
    if not raw_bytes:
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


__all__ = ["read_body"]
