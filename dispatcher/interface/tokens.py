"""已发放令牌的进程内登记簿。

``openapi.yaml`` 此前只有全局 ``bearerAuth``，**没有任何发放端点**：单用户时一个
静态的 ``DISPATCHER_AUTH_TOKEN`` 够用，多用户试点却无从下发凭据——换一个人就得改
环境变量重启。P2-c 补上发放与撤销，这个模块是它们的状态。

三条边界，都在 openapi 里如实声明：

* **存储是进程内的**，与 ``InMemoryMediaStore`` / ``_media_refs`` 同口径，只对
  单进程部署成立。两个直接后果：**进程重启后所有已发放令牌失效**；多 worker 下
  A 发的令牌在 B 上认不出来。真要跨进程，得把它下沉到存储层（一张 token 表）。
* **没有过期时间、没有续期、没有轮换**——本轮范围只到发放与撤销。这不是漏做，
  是不做：因此 :class:`IssuedToken` **不带** ``expires_at``，契约里也不声明它
  （声明了不实现就是空头承诺，正是本仓反复踩过的那类不一致）。
* **只存摘要，不存明文**。明文令牌只在发放那一次响应里出现，登记簿里放的是
  SHA-256 摘要。明文常驻内存没有额外收益，却会在内存转储或误打日志时把一个可用
  凭据原样送出去。按摘要查表也不比定长比对弱：令牌是 ``token_urlsafe(32)`` 的
  256 位随机量，不是人类口令，抗原像性让"猜出摘要"与"猜出令牌"同样不可行。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

#: ``token_urlsafe`` 的字节数。32 字节 = 256 位熵，穷举不可行。
_TOKEN_BYTES = 32
#: 令牌 **id** 的字节数。id 只用于辨认与撤销，不是秘密，8 字节足够避免撞车。
_TOKEN_ID_BYTES = 8


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    """一枚已发放的令牌。**不含令牌值**——那个只在签发那一次返回过。"""

    token_id: str
    tenant_id: str
    user_id: str
    created_at: datetime

    def to_wire(self) -> dict[str, object]:
        return {
            "token_id": self.token_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
        }


class TokenIssueRequest(BaseModel):
    """``POST /v1/tokens`` 的请求体。

    ``tenant_id`` / ``user_id`` 都必填且非空：这枚令牌**绑定**到这两个值，而它们
    是服务端认定的身份唯一来源。留空会签出一枚身份为空串的令牌，那种令牌绑不到
    任何数据上，只会成为排查时的噪音。
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


class TokenRegistry:
    """进程内登记簿。**单进程假设**（见模块 docstring）。

    读写之间没有 ``await``，因此 dict 操作在事件循环里是原子的，不需要额外的锁。
    """

    def __init__(self) -> None:
        self._by_digest: dict[str, IssuedToken] = {}
        self._digest_by_id: dict[str, str] = {}

    def issue(self, *, tenant_id: str, user_id: str) -> tuple[IssuedToken, str]:
        """签一枚新令牌，返回 ``(记录, 明文令牌)``。明文只在这一处产生。"""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        record = IssuedToken(
            token_id="tok_" + secrets.token_hex(_TOKEN_ID_BYTES),
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=datetime.now(UTC),
        )
        digest = _digest(token)
        self._by_digest[digest] = record
        self._digest_by_id[record.token_id] = digest
        return record, token

    def resolve(self, token: str) -> IssuedToken | None:
        """按明文令牌查记录。查不到即"不是我们发的"（或已撤销）。"""
        return self._by_digest.get(_digest(token))

    def revoke(self, token_id: str) -> bool:
        """撤销。返回是否真的删掉了一枚——``False`` 交给端点报 404。

        重复撤销同一个 id 返回 ``False``：撤销是幂等的**效果**（那枚令牌早就不
        能用了），但不是幂等的**响应**——如实回 404 比假装成功更容易排查。
        """
        digest = self._digest_by_id.pop(token_id, None)
        if digest is None:
            return False
        self._by_digest.pop(digest, None)
        return True

    def records(self) -> list[IssuedToken]:
        """全部未撤销的令牌，按签发时间排序。"""
        return sorted(self._by_digest.values(), key=lambda r: (r.created_at, r.token_id))


__all__ = ["IssuedToken", "TokenIssueRequest", "TokenRegistry"]
