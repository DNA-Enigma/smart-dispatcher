"""接口鉴权：一个中间件，一处解析身份，一处决定谁能发放凭据。

契约声明了全局 ``bearerAuth``。P2-c 之前，凭据只有一个静态的
``DISPATCHER_AUTH_TOKEN``，没有发放端点——单用户够用，多用户试点却无从下发：
换一个人就得改环境变量重启。现在多了一类**已发放令牌**
（``POST /v1/tokens``），由**主令牌**（master，即 ``DISPATCHER_AUTH_TOKEN``）
签发给指定的 ``tenant_id``/``user_id``，登记在 :class:`~.tokens.TokenRegistry` 里。

三类身份，边界很清楚：

* **主令牌**：配置里的静态串，绑定 ``DISPATCHER_TENANT``/``DISPATCHER_USER``。
  它既是这台部署所有者自己的凭据，也是**唯一的发放者**——``ADMIN_PATH_PREFIXES``
  下的路径只认它。主令牌不进登记簿，进程重启不影响它。
* **已发放令牌**：主令牌签发出来的，各绑定一份 tenant/user，可被 ``DELETE``
  撤销，进程内存储（重启即全部失效——见 tokens.py）。
* **无凭据**：只有 ``PUBLIC_PATHS`` 放行。

三件事在这里收口：

* **鉴权**：除 ``PUBLIC_PATHS`` 外的所有 HTTP 路径必须带对 token，否则 401。
  放在中间件而不是逐个端点加依赖：中间件是**按构造 fail-closed** 的——将来加一个
  端点，它自动被保护；靠人记得加依赖，就一定会漏掉一个（本仓库此前正是如此：
  全仓 grep ``Depends`` 零命中，而端点有十九个）。
* **身份**：唯一来源是 token。请求体 ``identity``、``x-tenant-id``/``x-user-id``、
  query 参数一概不再被当作事实——"客户端自报身份"与"零认证"是同一个缺陷的两半。
* **发放权**：谁能把别人放进来的凭据发出去。**一个开放的发放端点等于零鉴权**：
  任何人 ``POST`` 一次就拿到凭据，再把所有端点走一遍。因此 ``/v1/tokens`` 不是
  普通受保护端点，它要求主令牌；鉴权关闭（未配主令牌）时它一律 401，不存在
  "无凭据即可发放"的路径。
* **开关**：``DISPATCHER_AUTH_TOKEN`` 为空 = 鉴权关闭，只用于本地开发；
  启动时由 ``create_app`` 打一条 ERROR 级日志，不做静默放行。

**为什么不用 JWT**：验签要引入新依赖与一套密钥分发，而本轮约束明确不引新依赖。
将来消费端 IdP 上线，只需要替换 ``_resolve`` 这一处——中间件以下的所有代码都不
感知 token 长什么样。
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
from .tokens import TokenRegistry

log = logging.getLogger("dispatcher")

# 契约里显式声明 ``security: []`` 的路径。健康检查必须免鉴权：监控探针与
# 负载均衡器拿不到 token，也不该拿到——让它们为了探活而持有一把万能钥匙，
# 是把鉴权的价值削掉一半。
PUBLIC_PATHS: frozenset[str] = frozenset({"/v1/health"})

#: 只认**主令牌**的路径前缀。按前缀而不是逐条路径：将来在 ``/v1/tokens`` 下加
#: 端点时它自动是发放者专属，不需要有人记得往清单里补一条。这一侧的"忘记维护"
#: 只会让端点退回普通鉴权（仍受保护），不会变成开放——与 ``PUBLIC_PATHS`` 的
#: 失败方向相反，因此两者分开表达，合成一张表就等于把两种相反的风险混在一起。
ADMIN_PATH_PREFIXES: frozenset[str] = frozenset({"/v1/tokens"})

_IDENTITY_KEY = "identity"
_BEARER = "bearer"
_REALM = 'Bearer realm="smart-dispatcher"'


@dataclass(frozen=True)
class AuthConfig:
    """这套部署的**主令牌**与它绑定的身份。

    ``token`` 为空表示关闭鉴权。``tenant_id``/``user_id`` 是主令牌的**常量身份**：
    主令牌是部署所有者自己的凭据，身份就是这两个值。

    主令牌同时是**发放者**——``/v1/tokens`` 下的端点只认它。因此这份配置不含
    "谁能发放"的开关：配了主令牌就有人能发放，没配（鉴权关闭）就没人能发放，
    这是同一件事的两面，多一个开关只会多一种把两者配得互相矛盾的方式。
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


@dataclass(frozen=True)
class Credential:
    """一次通过校验的凭据：身份，以及它是不是主令牌。"""

    identity: Identity
    master: bool


class BearerAuthMiddleware:
    """纯 ASGI 中间件。

    刻意不用 ``BaseHTTPMiddleware``：那一个会给流式响应套上额外的缓冲与任务包装，
    客户端断开时下游生成器不一定能及时收到取消。本仓库的事件流（SSE + 断线重放）
    是移动端必需能力，不值得为了鉴权拿它冒险。
    """

    def __init__(self, app: ASGIApp, *, config: AuthConfig, registry: TokenRegistry) -> None:
        self.app = app
        self.config = config
        self.registry = registry
        # 主令牌身份是常量，构造一次即可——每个请求重建一个 pydantic 模型没有意义。
        self._identity = Identity(tenant_id=config.tenant_id, user_id=config.user_id)

    def _resolve(self, presented: str) -> Credential | None:
        """把一串凭据解析成身份，解析不出来就回 ``None``。

        主令牌先比：它是配置里的常量串，比对是定长的（``_verify``，不看前缀、
        不看长度差）。其余再去登记簿里按摘要查——**"不是主令牌"与"不是我们的
        令牌"在这里是同一件事**，都回 ``None``，交给调用方按路径决定 401 的措辞。
        """
        if self.config.required and _verify(presented, self.config.token):
            return Credential(self._identity, master=True)
        issued = self.registry.resolve(presented)
        if issued is None:
            return None
        return Credential(
            Identity(tenant_id=issued.tenant_id, user_id=issued.user_id), master=False
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket 直接放行。
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        challenge = _REALM
        credential: Credential | None = None
        presented = _bearer_token(request.headers.get("authorization"))
        if presented is not None:
            credential = self._resolve(presented)
            if credential is None:
                challenge = f'{_REALM}, error="invalid_token"'

        failure: DispatcherError | None = None
        if _is_admin_path(path):
            # 发放/撤销端点只认主令牌。凭据缺失、无效、或**有效但不是主令牌**
            # 三种情况在这里一并收口——一个能读自己任务的 token 不该能给别人发
            # 凭据，否则权限就顺着这一跳扩散开了。
            #
            # 第三种给 401 而不是 403：码表（core/errors.py）里没有 403 的码，
            # 新增码属于契约变更（与 413 复用 media_too_large 同一取舍），而
            # RFC 6750 的 ``401 + invalid_token``（"该令牌对这里无效"）是最贴近
            # 的可用表达。这是**已知的语义折衷**，不是"允许"。
            if credential is None or not credential.master:
                if credential is not None:
                    challenge = f'{_REALM}, error="invalid_token"'
                failure = DispatcherError(
                    "unauthorized",
                    "令牌的发放与撤销只接受主令牌（DISPATCHER_AUTH_TOKEN）",
                )
        elif self.config.required:
            if presented is None:
                failure = DispatcherError(
                    "unauthorized", "缺少 Authorization: Bearer 凭据"
                )
            elif credential is None:
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

        if credential is None:
            # 鉴权关闭（未配主令牌）下的普通路径：身份仍是配置的常量，
            # 不是客户端说了算。发放路径到不了这里（上面已 401）。
            credential = Credential(self._identity, master=True)
        scope.setdefault("state", {})[_IDENTITY_KEY] = credential.identity
        await self.app(scope, receive, send)


def _is_admin_path(path: str) -> bool:
    """路径是否落在只认主令牌的前缀下。

    要求精确匹配或**斜杠边界**匹配：``/v1/tokensfoo`` 不在 ``/v1/tokens`` 下。
    """
    return any(path == p or path.startswith(p + "/") for p in ADMIN_PATH_PREFIXES)


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


__all__ = [
    "ADMIN_PATH_PREFIXES",
    "PUBLIC_PATHS",
    "AuthConfig",
    "BearerAuthMiddleware",
    "Credential",
    "request_identity",
]
