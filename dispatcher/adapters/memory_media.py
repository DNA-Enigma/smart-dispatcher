"""内存媒体存储。

给测试、开发与单进程部署用。它也是 ``MediaStorePort`` 这个抽象成立的证明：
把接口实现成完全不含持久化的一坨内存，而调度层其余部分一行都不用改。

**它不做的事情值得写下来**：不落盘、不跨进程、重启即失。所以它只适合
"媒体活不过一次任务"的默认策略（``media_retained: false``）——那恰好是金融截图
的默认行为。

**总量有上界**（``max_total_bytes``）。保留期只管"过期的会被清掉"，不管
"没到期的会堆积"：上传了却一直没被任务引用的截图要躺满整个保留期（缺省 1 天），
而 ``expires_at=None`` 那条路已经被堵上之后，剩下唯一的失控方向就是量。
实测每张 10 MiB 截图的常驻成本是 **10.00 MiB RSS**（载荷与 RSS 近似 1:1），
因此"不限总量"在 2G 机器上就等于"100 张截图把机器撑爆"。超过上界时**拒绝入库**
（413 ``media_too_large``），不淘汰已在库里的——淘汰会静默让某个 ``media_id``
变成悬空引用，而那条链路上等待它的任务必然失败；拒绝新数据则只影响这一次上传，
调用方当场拿到可重试的错误。清理过期媒体仍由 ``sweep_expired`` 负责，
两者配合才构成"有界"：过期释放空间，超量当场拒绝。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from ..core.errors import DispatcherError
from ..ports.media import MediaKind, MediaRecord

log = logging.getLogger("dispatcher")

_KIND_BY_PREFIX: tuple[tuple[str, MediaKind], ...] = (
    ("image/", "image"),
    ("audio/", "audio"),
    ("application/pdf", "pdf"),
)


def _kind_of(mime: str) -> MediaKind:
    for prefix, kind in _KIND_BY_PREFIX:
        if mime.startswith(prefix):
            return kind
    return "document"


class InMemoryMediaStore:
    def __init__(
        self,
        *,
        allowed_mime: list[str],
        max_bytes: int,
        default_retain_days: int = 1,
        max_total_bytes: int | None = None,
    ) -> None:
        self._allowed = frozenset(allowed_mime)
        self._max_bytes = max_bytes
        # 缺省保留期，来自 Settings。下界 1 是刻意的：``expires_at=None``
        # （永不过期）是"金融截图常驻内存"那条路，不能靠配置把它打开。
        self._default_retain_days = max(1, default_retain_days)
        # ``None``/0/负数 = 不限总量。缺省不限是给**直接构造**的调用方（测试、
        # 嵌入式用法）留的：生产走 ``Dispatcher.build()``，它会显式传入
        # Settings 里的上界，那条路有测试钉住。
        self._max_total = max_total_bytes if max_total_bytes and max_total_bytes > 0 else None
        self._blobs: dict[str, bytes] = {}
        self._records: dict[str, MediaRecord] = {}
        # 单独记账而不是每次 sum(len(b) for b in self._blobs.values())：
        # 上界一旦生效，这个值就在**上传路径**上，而上传路径每次都全量求和
        # 是把一个 O(n) 扫描放进按字节计费的入口。
        self._total_bytes = 0

    # ------------------------------------------------------------------
    async def put(
        self, data: bytes, mime: str, *, user_id: str | None = None,
        tenant_id: str | None = None, retain_days: int | None = None,
    ) -> MediaRecord:
        # 校验在任何模型开销之前完成——一张 12MB 的图不该先被送给模型才发现太大
        if mime not in self._allowed:
            raise DispatcherError(
                "unsupported_media",
                f"不支持的类型 {mime}；允许：{sorted(self._allowed)}",
                context={"declared_mime": mime, "allowed_mime": sorted(self._allowed)},
            )
        if len(data) > self._max_bytes:
            raise DispatcherError(
                "media_too_large",
                f"媒体 {len(data)} 字节，超过上限 {self._max_bytes}",
                context={"bytes": len(data), "max_bytes": self._max_bytes},
            )
        # 总量上界。单张的上限与总量的上限是两件事：前者约束"一张图多大"，
        # 后者约束"这个进程总共持有多少"——只有后者能挡住"100 张合规的图"。
        if self._max_total is not None and self._total_bytes + len(data) > self._max_total:
            log.warning(
                "媒体存储已满：已持有 %d 字节，本次 %d 字节，上界 %d 字节",
                self._total_bytes, len(data), self._max_total,
            )
            raise DispatcherError(
                "media_too_large",
                f"媒体存储已满（已持有 {self._total_bytes} 字节 + 本次 {len(data)}"
                f" 字节 > 上界 {self._max_total}）。过期媒体会被定时清理，请稍后重试。",
                # 与单张超限不同，这一条**确实**会因为再试一次而好起来：
                # 清理任务是周期跑的（缺省 300s），空间会自己还回来。
                retryable=True,
                context={
                    "held_bytes": self._total_bytes,
                    "incoming_bytes": len(data),
                    "max_total_bytes": self._max_total,
                },
            )
        now = datetime.now(UTC)
        # 缺省与 0 都落到配置的缺省保留期：**每个记录都必须有 expires_at**
        # （``None`` = 永不过期，那正是本轮要消掉的那条路）。``retain_days=0``
        # 在 docs/05-media.md 里写着"任务结束即删"，而"任务什么时候结束"是调度层
        # 才知道的事，store 无从判断；接口层因此把它夹到 [1, 上界] 再进来，
        # 这里再兜一次底，免得绕过接口层的调用方拿到一个永不过期的记录。
        days = retain_days if retain_days and retain_days > 0 else self._default_retain_days
        try:
            expires = now + timedelta(days=days)
        except OverflowError as e:
            # 上限的权威在接口层（那里把 retain_days 夹进区间）。这里只把"绕过夹取
            # 直接调 store"的那条路从 500 变成一个**有类型的错误**，不再造第二个界
            # ——两处各写一份上限，就一定会分叉。
            raise DispatcherError(
                "invalid_request",
                f"retain_days={days} 超出可表示的保留期",
                context={"retain_days": days},
            ) from e
        mid = f"m_{uuid.uuid4().hex[:16]}"
        rec = MediaRecord(
            media_id=mid,
            kind=_kind_of(mime),
            mime=mime,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=now,
            expires_at=expires,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._blobs[mid] = data
        self._records[mid] = rec
        self._total_bytes += len(data)
        return rec

    async def get(self, media_id: str) -> tuple[bytes, MediaRecord] | None:
        rec = self._records.get(media_id)
        blob = self._blobs.get(media_id)
        if rec is None or blob is None:
            return None
        if rec.expires_at is not None and rec.expires_at <= datetime.now(UTC):
            await self.delete(media_id)
            return None
        return blob, rec

    async def stat(self, media_id: str) -> MediaRecord | None:
        return self._records.get(media_id)

    async def delete(self, media_id: str) -> bool:
        # 记账必须与 blob 同生共死：漏减一次，上界就单向漂移——库是空的，
        # 而存储认为它满了，此后所有上传都被拒。取出的就是被删掉的那个对象，
        # 不依赖 records 里还在（两者本应一致，但记账只信真正取到的那份）。
        blob = self._blobs.pop(media_id, None)
        if blob is not None:
            self._total_bytes -= len(blob)
        return self._records.pop(media_id, None) is not None

    async def sweep_expired(self, now: datetime, *, protected: Collection[str] = ()) -> int:
        keep = set(protected)
        expired = [
            mid for mid, r in self._records.items()
            if mid not in keep and r.expires_at is not None and r.expires_at <= now
        ]
        for mid in expired:
            await self.delete(mid)
        return len(expired)

    # -- 供测试与自省 ------------------------------------------------
    def __len__(self) -> int:
        return len(self._records)

    @property
    def total_bytes(self) -> int:
        """当前持有的载荷字节数。上界的实际值，供测试与自省读取。"""
        return self._total_bytes


__all__ = ["InMemoryMediaStore"]
