"""已发放令牌的登记簿。

``openapi.yaml`` 此前只有全局 ``bearerAuth``，**没有任何发放端点**：单用户时一个
静态的 ``DISPATCHER_AUTH_TOKEN`` 够用，多用户试点却无从下发凭据——换一个人就得改
环境变量重启。P2-c 补上发放与撤销，这个模块是它们的状态。

三条边界：

* **存储缺省是进程内的**，与 ``InMemoryMediaStore`` / ``_media_refs`` 同口径，
  只对单进程部署成立。两个直接后果：**进程重启后所有已发放令牌失效**；多 worker
  下 A 发的令牌在 B 上认不出来。第二个后果至今成立——部署形态是单 worker
  （见 deploy/smart-dispatcher.service）。第一个后果在 ``state_backend=sqlite``
  时不再成立：启动时 :meth:`TokenRegistry.attach_store` 会从
  :class:`~..adapters.sqlite_tokens.SqliteTokenStore` 把登记簿读回来。
  内存后端下行为与改动前完全一致，一点没变。
* **没有过期时间、没有续期、没有轮换**——本轮范围只到发放与撤销。这不是漏做，
  是不做：因此 :class:`IssuedToken` **不带** ``expires_at``，契约里也不声明它
  （声明了不实现就是空头承诺，正是本仓反复踩过的那类不一致）。
* **只存摘要，不存明文**——内存与落盘都是。明文令牌只在发放那一次响应里出现，
  登记簿里放的是 SHA-256 摘要。明文常驻内存没有额外收益，却会在内存转储或误打
  日志时把一个可用凭据原样送出去；落盘更甚，那是一个会留在磁盘上、会被备份走的
  文件。按摘要查表也不比定长比对弱：令牌是 ``token_urlsafe(32)`` 的 256 位随机量，
  不是人类口令，抗原像性让"猜出摘要"与"猜出令牌"同样不可行。
  这也是"重启后令牌仍有效"能成立的原因：查表只用到摘要，客户端手里的明文
  在服务端算一次摘要就能命中，明文从头到尾不需要被存下来。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    """已发放令牌的登记簿。

    内存里的两个 dict 是**读路径的唯一来源**：``resolve`` 每个请求都会被调到，
    它不该去碰 I/O。可选的后端只做**写穿透 + 启动载入**——签发时写一行、撤销时
    删一行、启动时载入一次，之后读写都在内存里。因此挂上后端不改变任何请求的
    延迟特征。

    读写之间没有 ``await``，因此 dict 操作在事件循环里是原子的，不需要额外的锁。
    后端是同步 ``sqlite3``（见 adapters/sqlite_tokens.py 的取舍），调用点会短暂
    阻塞——只在签发/撤销这两处，不在 ``resolve``。
    """

    def __init__(self) -> None:
        self._by_digest: dict[str, IssuedToken] = {}
        self._digest_by_id: dict[str, str] = {}
        #: 可选持久后端：``load() -> list[(记录, 摘要)]`` / ``put(记录, 摘要)`` /
        #: ``delete(token_id) -> bool``。不实现成 Protocol：只有一个实现，
        #: 现在就抽一个接口是给还不存在的第二个实现写文档。
        self._store: Any | None = None

    # ------------------------------------------------------------------
    def attach_store(self, store: Any) -> None:
        """挂上持久后端并载入已有令牌。**只在启动时调用一次**。

        载入的是摘要，不是明文——但查表用的正是摘要，因此客户端手里的令牌
        重启后照常能用（见模块 docstring 第三条）。这一点可能有违直觉：
        "没存明文怎么还能认出来"——因为认令牌从来不需要明文。
        """
        self._store = store
        for record, digest in store.load():
            self._by_digest[digest] = record
            self._digest_by_id[record.token_id] = digest

    # ------------------------------------------------------------------
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
        if self._store is not None:
            self._store.put(record, digest)
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
        if self._store is not None:
            self._store.delete(token_id)
        return True

    def records(self) -> list[IssuedToken]:
        """全部未撤销的令牌，按签发时间排序。"""
        return sorted(self._by_digest.values(), key=lambda r: (r.created_at, r.token_id))


__all__ = ["IssuedToken", "TokenIssueRequest", "TokenRegistry"]
