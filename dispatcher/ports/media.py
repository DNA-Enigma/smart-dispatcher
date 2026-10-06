"""MediaStorePort —— 媒体存取。

调度层自带上传端点与媒体存储（``POST /v1/media``），因此这里是一个真实的接口
而不是空壳。但**存储实现不被绑定**：手机端可以是本地文件、服务端可以是对象存储
或 Postgres 大对象。一个把媒体存储写死成 S3 的设计，在离线场景下直接失效——
而消费端包含一部手机，所以这条不能死。

金融截图的默认策略是**任务结束即删原图**（``evolution.retention.media_retained: false``）。
保留期由 ``privacy.retain_receipt_images_days`` 决定，默认 0。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

MediaKind = Literal["image", "audio", "pdf", "document"]


class MediaRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_id: str
    kind: MediaKind
    mime: str
    size_bytes: int
    sha256: str
    created_at: datetime
    expires_at: datetime | None = None
    # 归属，用于按用户清理与审计
    user_id: str | None = None
    tenant_id: str | None = None


@runtime_checkable
class MediaStorePort(Protocol):
    async def put(
        self, data: bytes, mime: str, *, user_id: str | None = None,
        tenant_id: str | None = None, retain_days: int | None = None,
    ) -> MediaRecord: ...

    async def get(self, media_id: str) -> tuple[bytes, MediaRecord] | None: ...

    async def stat(self, media_id: str) -> MediaRecord | None: ...

    async def delete(self, media_id: str) -> bool: ...

    async def sweep_expired(self, now: datetime) -> int:
        """删除已过保留期的媒体，返回删除条数。由定时任务驱动。"""
        ...


__all__ = ["MediaKind", "MediaRecord", "MediaStorePort"]
