"""内存媒体存储。

给测试、开发与单进程部署用。它也是 ``MediaStorePort`` 这个抽象成立的证明：
把接口实现成完全不含持久化的一坨内存，而调度层其余部分一行都不用改。

**它不做的事情值得写下来**：不落盘、不跨进程、重启即失。所以它只适合
"媒体活不过一次任务"的默认策略（``media_retained: false``）——那恰好是金融截图
的默认行为。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

from ..core.errors import DispatcherError
from ..ports.media import MediaKind, MediaRecord

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
        self, *, allowed_mime: list[str], max_bytes: int, default_retain_days: int = 1
    ) -> None:
        self._allowed = frozenset(allowed_mime)
        self._max_bytes = max_bytes
        # 缺省保留期，来自 Settings。下界 1 是刻意的：``expires_at=None``
        # （永不过期）是"金融截图常驻内存"那条路，不能靠配置把它打开。
        self._default_retain_days = max(1, default_retain_days)
        self._blobs: dict[str, bytes] = {}
        self._records: dict[str, MediaRecord] = {}

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
        self._blobs.pop(media_id, None)
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


__all__ = ["InMemoryMediaStore"]
