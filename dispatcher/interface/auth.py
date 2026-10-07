"""接口鉴权：一个中间件，一处解析身份。

契约声明了全局 ``bearerAuth``（``openapi.yaml`` 的 securitySchemes），但**没有发放或
续期令牌的端点**——令牌由消费端自己的认证体系签发，调度层只信任收到的它。
因此参考实现只做契约承诺的那一件事：比对，然后把 ``user_id`` 与 ``tenant_id``
从 token 侧定下来。它不建用户、不发令牌、不管刷新。

三件事在这里收口：

* **鉴权**：除 ``PUBLIC_PATHS`` 外的所有 HTTP 路径必须带对 token，否则 401。
  放在中间件而不是逐个端点加依赖：中间件是**按构造 fail-closed** 的——将来加一个
  端点，它自动被保护；靠人记得加依赖，就一定会漏掉一个（本仓库此前正是如此：
  全仓 grep ``Depends`` 零命中，而端点有十九个）。
* **身份**：唯一来源是 token。请求体 ``identity``、``x-tenant-id``/``x-user-id``、
  query 参数一概不再被当作事实——"客户端自报身份"与"零认证"是同一个缺陷的两半。
* **开关**：``DISPATCHER_AUTH_TOKEN`` 为空 = 鉴权关闭，只用于本地开发；
  启动时由 ``create_app`` 打一条 ERROR 级日志，不做静默放行。

**为什么不用 JWT**：契约没有发放端点，验签要引入新依赖与一套密钥分发，而本轮约束
明确不引新依赖。将来消费端 IdP 上线，只需要替换 ``_verify`` 这一处——中间件以下
的所有代码都不感知 token 长什么样。
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.contract import Identity
from ..core.errors import DispatcherError
from ..core.settings import Settings
from .problems import problem_response

log = logging.getLogger("dispatcher")

# 契约里显式声明 ``security: []`` 的路径。健康检查必须免鉴权：监控探针与
# 负载均衡器拿不到 token，也不该拿到——让它们为了探活而持有一把万能钥匙，
# 是把鉴权的价值削掉一半。
PUBLIC_PATHS: frozenset[str] = frozenset({"/v1/health"})

_IDENTITY_KEY = "identity"
_BEARER = "bearer"
_REALM = 'Bearer realm="smart-dispatcher"'


@dataclass(frozen=True)
class AuthConfig:
    """这套部署的凭据与它绑定的身份。

    ``token`` 为空表示关闭鉴权。``tenant_id``/``user_id`` 是**常量身份**：
    单 token 部署下身份就是这两个值，不为多租户抽象新模型——那是消费端
    签发带身份的 token 之后才该做的事，而现在没有那个端点（见模块 docstring）。
    """

    token: str = ""
    tenant_id: str = "default"
    user_id: str = "owner"

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig:
        return cls(
            token=settings.dispatcher_auth_token,
            tenant_id=settings.dispatcher_tenant,
            user_id=settings.dispatcher_user,
        )

    @property
    def required(self) -> bool:
        return bool(self.token)


class BearerAuthMiddleware:
    """纯 ASGI 中间件。

    刻意不用 ``BaseHTTPMiddleware``：那一个会给流式响应套上额外的缓冲与任务包装，
    客户端断开时下游生成器不一定能及时收到取消。本仓库的事件流（SSE + 断线重放）
    是移动端必需能力，不值得为了鉴权拿它冒险。
    """

    def __init__(self, app: ASGIApp, *, config: AuthConfig) -> None:
        self.app = app
        self.config = config
        # 身份是常量，构造一次即可——每个请求重建一个 pydantic 模型没有意义。
        self._identity = Identity(tenant_id=config.tenant_id, user_id=config.user_id)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in PUBLIC_PATHS:
            # lifespan / websocket 与公开路径直接放行。
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        challenge = _REALM
        failure: DispatcherError | None = None

        if self.config.required:
            presented = _bearer_token(request.headers.get("authorization"))
            if presented is None:
                failure = DispatcherError(
                    "unauthorized", "缺少 Authorization: Bearer 凭据"
                )
            elif not _verify(presented, self.config.token):
                challenge = f'{_REALM}, error="invalid_token"'
                failure = DispatcherError("unauthorized", "token 无效")

        if failure is not None:
            response = problem_response(
                failure,
                request.headers.get("x-request-id") or "",
                extra_headers={"WWW-Authenticate": challenge},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})[_IDENTITY_KEY] = self._identity
        await self.app(scope, receive, send)


def _verify(presented: str, expected: str) -> bool:
    """定长比对，避免按字符逐位比较泄露前缀信息。

    比**字节**而不是字符串：``hmac.compare_digest`` 遇到非 ASCII 的 str 会抛
    TypeError，而 token 是客户端可控的任意字节——一个带中文的 Authorization 头
    不该让服务端 500。
    """
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _bearer_token(header: str | None) -> str | None:
    """从 ``Authorization`` 头取出 bearer token；方案名大小写不敏感（RFC 7235）。"""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != _BEARER:
        return None
    return value.strip() or None


def request_identity(request: Request) -> Identity:
    """本次请求的身份。**唯一**来源是中间件从 token 定下的那一份。

    拿不到就抛：那意味着有人绕开 ``create_app()`` 直接拼了一个 app/Request，
    属于编程错误。这里不兜一个默认身份——静默兜底正是这一轮要根除的东西。
    """
    identity = getattr(request.state, _IDENTITY_KEY, None)
    if not isinstance(identity, Identity):  # pragma: no cover - 部署接线错误
        raise RuntimeError(
            "请求身份缺失：BearerAuthMiddleware 未挂载（应经 create_app() 构造应用）"
        )
    return identity


__all__ = ["PUBLIC_PATHS", "AuthConfig", "BearerAuthMiddleware", "request_identity"]
