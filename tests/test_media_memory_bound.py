"""媒体存储的**总量上界**。

保留期（``test_media_retention.py``）只管"过期的会被清掉"，不管"没到期的会堆积"：
上传了却一直没被任务引用的截图要躺满整个保留期。而 2G 机器上 100 张 10 MiB 的
金融截图就是 1G——docs/12-deployment.md 把它记为"最现实的内存风险"。

实测依据（不是估的）：``InMemoryMediaStore`` 每张 10 MiB 截图的常驻成本是
**10.00 MiB RSS**（载荷与 RSS 近似 1:1；12 张、120 MiB 载荷 → RSS +120.15 MiB）。
因此"持有的载荷字节数"**就是**媒体那一块的 RSS，两者之间没有可观的系数。

取舍：超限时**拒绝新上传**（413 ``media_too_large``，可重试），不淘汰已在库里的。
淘汰会让某个 ``media_id`` 静默变成悬空引用，而等待它的任务必然失败——那时错的
不是上传者，是系统自己丢了一份已经收下的数据。拒绝只影响这一次上传，
调用方当场拿到一个可重试的错误，清理任务（缺省 300s）随后把空间还回来。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.core.errors import DispatcherError
from dispatcher.core.settings import REPO_ROOT, Settings, get_settings
from dispatcher.interface.app import create_app
from dispatcher.interface.auth import AuthConfig
from dispatcher.pipeline import Dispatcher, DispatcherConfig

AUTH = AuthConfig(token="t_master", tenant_id="t_acme", user_id="u_owner")
PNG = b"\x89PNG\r\n\x1a\n"


def _store(cap: int | None) -> InMemoryMediaStore:
    return InMemoryMediaStore(
        allowed_mime=["image/png"], max_bytes=1024, default_retain_days=1,
        max_total_bytes=cap,
    )


# ------------------------------------------------------------------ store
async def test_cap_rejects_the_upload_that_would_exceed_it():
    store = _store(cap=25)
    for _ in range(2):
        await store.put(b"x" * 10, "image/png")
    assert store.total_bytes == 20

    with pytest.raises(DispatcherError) as ei:
        await store.put(b"x" * 10, "image/png")
    assert ei.value.code == "media_too_large"
    assert ei.value.status == 413
    assert ei.value.context == {
        "held_bytes": 20, "incoming_bytes": 10, "max_total_bytes": 25,
    }
    # 被拒的那张**没有**入库，账也没有漂
    assert store.total_bytes == 20
    assert len(store) == 2


async def test_cap_is_retryable_unlike_the_per_image_limit():
    """总量超限与单张超限是两回事：前者会自己好起来，后者不会。

    这里如实区分：单张超限是"你这张图太大"（重试无用），总量超限是"现在满了"
    （清理任务跑过就有空间）。两条都回 413，但 ``retryable`` 不一样，
    客户端据此决定是提示用户换图还是稍后重试。
    """
    # 单张超限：max_bytes 先判，与总量无关
    per_image_store = InMemoryMediaStore(
        allowed_mime=["image/png"], max_bytes=4, default_retain_days=1,
        max_total_bytes=1024,
    )
    with pytest.raises(DispatcherError) as per_image:
        await per_image_store.put(b"x" * 10, "image/png")
    assert per_image.value.code == "media_too_large"
    assert per_image.value.retryable is False

    # 总量超限：单张合规（5 字节 ≤ max_bytes），但装不下了
    total_store = InMemoryMediaStore(
        allowed_mime=["image/png"], max_bytes=1024, default_retain_days=1,
        max_total_bytes=5,
    )
    await total_store.put(b"x" * 5, "image/png")
    with pytest.raises(DispatcherError) as total:
        await total_store.put(b"y", "image/png")
    assert total.value.code == "media_too_large"
    assert total.value.retryable is True


async def test_delete_gives_the_space_back():
    """删除必须**同时**减账。

    只减 blob 不减账，是一个单向漂移的 bug：库里空空如也，存储却认为它满了，
    此后每一次上传都被拒——而且重启（内存实现本就重启即空）之前都不会恢复。
    """
    store = _store(cap=10)
    rec = await store.put(b"x" * 10, "image/png")
    with pytest.raises(DispatcherError):
        await store.put(b"y", "image/png")

    assert await store.delete(rec.media_id) is True
    assert store.total_bytes == 0
    await store.put(b"y" * 10, "image/png")  # 空间回来了


async def test_sweep_expired_gives_the_space_back():
    """过期清理是另一半：只拒不放，等于把上界变成一个不可恢复的天花板。"""
    store = _store(cap=10)
    await store.put(b"x" * 10, "image/png")
    with pytest.raises(DispatcherError):
        await store.put(b"y", "image/png")

    removed = await store.sweep_expired(datetime.now(UTC) + timedelta(days=2))
    assert removed == 1
    assert store.total_bytes == 0
    await store.put(b"y" * 10, "image/png")


async def test_protected_media_is_not_swept_so_the_cap_still_holds():
    """受保护的媒体不会被清，因此**它仍然占着额度**——上界不该被保护绕过。"""
    store = _store(cap=10)
    rec = await store.put(b"x" * 10, "image/png")
    removed = await store.sweep_expired(
        datetime.now(UTC) + timedelta(days=2), protected=[rec.media_id]
    )
    assert removed == 0
    assert store.total_bytes == 10
    with pytest.raises(DispatcherError):
        await store.put(b"y", "image/png")


async def test_no_cap_means_unbounded_by_default():
    """直接构造时不传上界 = 不限。

    缺省不限是给测试与嵌入式用法留的；**生产走 ``Dispatcher.build()``**，
    它显式传入 Settings 里的上界，那条路有下面的用例钉住。
    """
    store = _store(cap=None)
    for _ in range(50):
        await store.put(b"x" * 100, "image/png")
    assert store.total_bytes == 5000


async def test_zero_or_negative_means_unbounded_too():
    """0/负数表示"明确关掉这个闸"，与 ``dispatcher_sse_max_connections`` 同口径。"""
    for cap in (0, -1):
        store = _store(cap=cap)
        await store.put(b"x" * 100, "image/png")
        assert store.total_bytes == 100


# ------------------------------------------------------------------ 接线
async def test_build_wires_the_cap_from_settings(settings: Settings):
    """生产路径：``build()`` 必须把 Settings 的上界传给存储。

    没有这一条，上界就只是一个"实现了但没人用"的字段——正是本仓反复出现的
    那类静默失效（配置在、代码在、两者没接上）。
    """
    tiny = settings.model_copy(update={"dispatcher_media_max_total_bytes": 10})
    d = Dispatcher.build(DispatcherConfig(settings=tiny))
    try:
        await d.media.put(b"x" * 10, "image/png")
        with pytest.raises(DispatcherError) as ei:
            await d.media.put(b"y", "image/png")
        assert ei.value.code == "media_too_large"
    finally:
        await d.aclose()


def test_default_cap_is_the_measured_128_mib(settings: Settings):
    """缺省值本身也钉住：它是有实测依据的，不该被顺手改小或改成不限。"""
    assert settings.dispatcher_media_max_total_bytes == 128 * 1024 * 1024


# ------------------------------------------------------------------ 接口层
async def test_upload_over_the_cap_is_a_413_problem_the_client_can_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """端到端：上界打满之后，上传返回 413 Problem，且 ``retryable`` 为真。

    这是客户端**看得到**的那一面——留给运维在日志里判断的是另一面。
    """
    monkeypatch.setenv("DISPATCHER_MEDIA_MAX_TOTAL_BYTES", "16")
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "memory")
    get_settings.cache_clear()

    app = create_app(auth=AUTH)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {AUTH.token}"},
        ) as c:
            first = await c.post(
                "/v1/media", content=PNG + b"a" * 8, headers={"content-type": "image/png"}
            )
            assert first.status_code == 201, first.text

            second = await c.post(
                "/v1/media", content=PNG + b"b" * 8, headers={"content-type": "image/png"}
            )
            assert second.status_code == 413, second.text
            problem = second.json()
            assert problem["code"] == "media_too_large"
            assert problem["retryable"] is True
            assert problem["context"]["max_total_bytes"] == 16

            # 前一张仍然取得到：拒绝新数据不该动已经收下的数据
            mid = first.json()["media_id"]
            got = await c.get(f"/v1/media/{mid}")
            assert got.status_code == 200
            assert got.content == PNG + b"a" * 8


def test_env_example_documents_the_new_switches():
    """``.env.example`` 是部署清单唯一照抄的来源，新增开关必须出现在那里。

    这一条防的是"功能做了但没人知道怎么开"——本轮的起点正是
    ``state_backend`` 有实现、没开关、也没写进 .env.example。
    """
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DISPATCHER_STATE_BACKEND=sqlite" in text
    assert "DISPATCHER_STATE_PATH" in text
    assert "DISPATCHER_MEDIA_MAX_TOTAL_BYTES" in text
