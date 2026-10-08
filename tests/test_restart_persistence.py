"""**重启后状态还在**——本轮的验收。

docs/12-deployment.md 第 7 节把两条列为"不可接受"，因为它们对用户是静默的：

* **已发放的子令牌全丢** → 持有者当场 401（P2-c 刚做的功能白做）。用户没有
  做错任何事，只是服务重启了一次。
* **已批准的策略版本静默回退** → 用户以为改生效了，其实回到了
  ``config/routing.policy.yaml`` 的内容。没有任何报错，只有"批准了没用"。

这里的做法是**真的关掉再起来**：跑完一个 ``lifespan`` 周期（进程内的等价物），
再用同一个库文件构造一个新应用，然后**从 HTTP 上看**状态还在不在。
不直接断言 store 的对象——那只能证明"存进去了"，证明不了"接口层还认得"。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from dispatcher.adapters.sqlite_evolution import SqliteEvolutionStore
from dispatcher.core.policy import load_policy
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.interface.app import create_app, get_dispatcher
from dispatcher.interface.auth import AuthConfig

MASTER = AuthConfig(token="t_master", tenant_id="t_acme", user_id="u_owner")
BASE_VERSION = "pv_2026-10-01_03"  # config/routing.policy.yaml 里的版本
EVOLVED_TEMPERATURE = 0.42


@pytest.fixture
def sqlite_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把这一组用例切到 sqlite 后端，库放在 tmp 里。

    走环境变量而不是注入对象：``.env`` 与真实环境变量是同一个入口，
    这样测到的正是"改 .env 就能换后端"这条路径本身。
    """
    db = tmp_path / "dispatcher.db"
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("DISPATCHER_STATE_PATH", str(db))
    get_settings.cache_clear()
    return db


@asynccontextmanager
async def _running_app():
    """起一个应用、跑完它的 lifespan 周期，退出时干净关闭（= 一次进程生命周期）。"""
    app = create_app(auth=MASTER)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {MASTER.token}"}
        ) as client:
            yield client


async def _seed_proposed_suggestion(db: Path) -> None:
    """往库里种一条待批准的建议，以及它必须挂靠的"当前生效版本"。

    绕过 ``/v1/evolution/analyze``：那条路要调 LLM，而这里要验的是
    **批准之后的持久化**，不是分析。种进去的内容与 ``ensure_initial_version``
    / ``run_pass`` 产出的形状一致（见 evolution/policy_store.py）。
    """
    base = load_policy(REPO_ROOT / "config" / "routing.policy.yaml")
    store = SqliteEvolutionStore(db)
    try:
        await store.put_version(
            {
                "policy_version": base.policy_version,
                "parent_version": None,
                "created_at": "2026-10-01T00:00:00+00:00",
                "approved_by": "<initial>",
                "suggestion_id": None,
                "status": "active",
                "scope": {"level": "app", "canary": None},
                "patch": None,
                "policy": base.model_dump(mode="json"),
                "changed": {},
            }
        )
        await store.set_active(base.policy_version)
        await store.put_suggestion(
            {
                "suggestion_id": "sg_restart_1",
                "status": "proposed",
                "created_at": "2026-10-08T00:00:00+00:00",
                "basis_policy_version": base.policy_version,
                "scope": {"level": "user"},
                "target": {
                    "artifact": "routing.policy.yaml",
                    "path": "router.temperature",
                    "current": 0,
                    "proposed": EVOLVED_TEMPERATURE,
                },
            }
        )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# 已发放的子令牌
# ---------------------------------------------------------------------------
async def test_issued_token_survives_a_restart(sqlite_env):
    """重启前签发的子令牌，重启后在受保护端点上仍然有效。

    这条如果没有，P2-c 的子令牌功能在**每一次**重启（systemd 的
    ``Restart=always`` 让它必然发生）后都会静默失效。
    """
    async with _running_app() as c:
        resp = await c.post(
            "/v1/tokens", json={"tenant_id": "t_acme", "user_id": "u_alice"}
        )
        assert resp.status_code == 201, resp.text
        issued = resp.json()
        child = issued["token"]
        token_id = issued["token_id"]
        assert child and child != MASTER.token

        # 重启前：这枚子令牌能用
        r = await c.get("/v1/policy", headers={"Authorization": f"Bearer {child}"})
        assert r.status_code == 200, r.text

    # ---- 进程重来一遍，同一个库文件 ----
    async with _running_app() as c:
        r = await c.get("/v1/policy", headers={"Authorization": f"Bearer {child}"})
        assert r.status_code == 200, f"重启后子令牌失效了：{r.text}"

        # 身份跟着回来了，不是"放行一切"：它签给 t_acme/u_alice
        r = await c.get("/v1/tokens")
        assert r.status_code == 200, r.text
        ids = [t["token_id"] for t in r.json()["items"]]
        assert token_id in ids

        # 反向对照：乱造的令牌仍然 401。没有这一条，上面的 200 可能只是
        # "鉴权整个没生效"，而不是"这枚令牌被认出来了"。
        r = await c.get("/v1/policy", headers={"Authorization": "Bearer not-a-real-token"})
        assert r.status_code == 401, r.text


async def test_revoked_token_stays_revoked_after_a_restart(sqlite_env):
    """撤销也要跨重启——否则"撤销"变成"重启后再生效"，等于没撤。"""
    async with _running_app() as c:
        issued = (await c.post(
            "/v1/tokens", json={"tenant_id": "t_acme", "user_id": "u_bob"}
        )).json()
        child = issued["token"]
        resp = await c.delete(f"/v1/tokens/{issued['token_id']}")
        assert resp.status_code in (200, 204), resp.text

    async with _running_app() as c:
        r = await c.get("/v1/policy", headers={"Authorization": f"Bearer {child}"})
        assert r.status_code == 401, r.text


async def test_token_store_never_holds_the_plaintext(sqlite_env):
    """库文件里没有明文令牌——落盘的只有 SHA-256 摘要。

    金融场景下这条不是洁癖：库文件会被备份、会被复制到别处，
    明文落在里面就等于凭据跟着走。
    """
    async with _running_app() as c:
        child = (await c.post(
            "/v1/tokens", json={"tenant_id": "t_acme", "user_id": "u_carol"}
        )).json()["token"]

    raw = sqlite_env.read_bytes()
    assert child.encode() not in raw
    # 而摘要一定在（否则上面的"重启后仍有效"无从解释）
    import hashlib

    assert hashlib.sha256(child.encode()).hexdigest().encode() in raw


# ---------------------------------------------------------------------------
# 已批准的策略版本
# ---------------------------------------------------------------------------
async def test_approved_policy_does_not_revert_on_restart(sqlite_env):
    """批准过的策略版本在重启后仍然生效，不回退到 YAML 文件。"""
    await _seed_proposed_suggestion(sqlite_env)

    async with _running_app() as c:
        before = (await c.get("/v1/health")).json()["policy_version"]
        assert before == BASE_VERSION  # 还没批准

        resp = await c.post("/v1/evolution/suggestions/sg_restart_1/approve", json={})
        assert resp.status_code == 200, resp.text
        approved = resp.json()["policy_version"]
        assert approved != BASE_VERSION, "批准没有产出新版本，后面的断言会失去意义"

        policy = (await c.get("/v1/policy")).json()
        assert policy["policy_version"] == approved
        assert policy["router"]["temperature"] == EVOLVED_TEMPERATURE

    # ---- 重启 ----
    async with _running_app() as c:
        health = (await c.get("/v1/health")).json()
        assert health["policy_version"] == approved, (
            f"重启后策略回退到了 {health['policy_version']}（文件里的版本是 {BASE_VERSION}）"
        )
        policy = (await c.get("/v1/policy")).json()
        assert policy["router"]["temperature"] == EVOLVED_TEMPERATURE

        # 版本历史也在，不只是"最后那个数还在"
        items = (await c.get("/v1/policy/versions")).json()["items"]
        assert approved in [v["policy_version"] for v in items]


async def test_rollback_survives_a_restart(sqlite_env):
    """回滚同样是"生效的版本"，重启后不该弹回被回滚掉的那一版。"""
    await _seed_proposed_suggestion(sqlite_env)

    async with _running_app() as c:
        approved = (await c.post(
            "/v1/evolution/suggestions/sg_restart_1/approve", json={}
        )).json()["policy_version"]
        assert approved != BASE_VERSION

        resp = await c.post("/v1/policy/rollback", json={"to_version": BASE_VERSION})
        assert resp.status_code == 200, resp.text
        assert (await c.get("/v1/health")).json()["policy_version"] == BASE_VERSION

    async with _running_app() as c:
        # 回滚之后生效的是 BASE_VERSION，而它**恰好**等于文件里的版本——
        # 于是这条用例真正验的是"库里的 rolled_back 状态没有被当成什么都没有"：
        # 重启后生效的仍是文件里的那一版，而不是又跳回 approved。
        assert (await c.get("/v1/health")).json()["policy_version"] == BASE_VERSION
        policy = (await c.get("/v1/policy")).json()
        assert policy["router"]["temperature"] == 0


# ---------------------------------------------------------------------------
# 任务快照与事件流（另一类"客户端拿着 task_id 查 404"）
# ---------------------------------------------------------------------------
async def test_task_snapshot_survives_a_restart(sqlite_env):
    """任务快照与事件流跨重启存活，``Last-Event-ID`` 重放源还在。

    这里直接写状态存储再经由接口层读——真正的任务提交要跑完整条流水线
    （要 LLM），而这一步验的是"库里的任务在接口层还认得"。
    """
    from dispatcher.adapters.sqlite_state import SqliteStateStore
    from tests.test_sqlite_state import sample_task  # 复用既有的样本构造

    store = SqliteStateStore(sqlite_env)
    await store.open()
    try:
        # 终态任务：SSE 流的关闭时机由状态决定，终态任务才会把流收尾，
        # 于是 `await client.get(...)` 能拿到完整重放而不会挂住（与
        # tests/test_sse_connection_limit.py:163 同一手法）。
        # 租户必须与主令牌一致——取任务按租户校验归属，不匹配一律 404。
        await store.put_task(sample_task(
            "task_restart", status="succeeded",
            tenant_id=MASTER.tenant_id, user_id=MASTER.user_id,
        ))
        for i in range(3):
            await store.append_event("task_restart", "subtask.progress", {"i": i})
    finally:
        await store.close()

    app = create_app(auth=MASTER)
    async with app.router.lifespan_context(app):
        # 心跳默认 15s，而**终态任务也会先发一次心跳再收尾**——不调小的话
        # 下面那个请求要等满 15 秒（test_sse_connection_limit.py:143 同一处理）。
        # 在启动后改运行中的策略对象：load_policy 每次返回新对象，不会串到别的用例。
        get_dispatcher().policy.limits.sse_heartbeat_ms = 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {MASTER.token}"},
        ) as c:
            r = await c.get("/v1/tasks/task_restart")
            assert r.status_code == 200, r.text
            assert r.json()["task_id"] == "task_restart"

            # 事件流重放：客户端断线重连靠的就是它。此前这些事件在重启后
            # 一条都不剩，`Last-Event-ID` 重放于是变成一个空流。
            r = await c.get("/v1/tasks/task_restart/events")
            assert r.status_code == 200, r.text
            assert r.text.count("subtask.progress") == 3, r.text


# ---------------------------------------------------------------------------
# 向后兼容：不加开关时行为与改动前一致
# ---------------------------------------------------------------------------
async def test_memory_backend_still_forgets_on_restart(monkeypatch, tmp_path):
    """``memory`` 后端下重启仍然"全丢"——这是刻意的，也是向后兼容的证明。

    有了持久化之后，"开发/测试不留脏状态"依赖的正是这条：缺省后端下
    一次重启就该回到干净状态。
    """
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "memory")
    monkeypatch.delenv("DISPATCHER_STATE_PATH", raising=False)
    get_settings.cache_clear()

    async with _running_app() as c:
        child = (await c.post(
            "/v1/tokens", json={"tenant_id": "t_acme", "user_id": "u_dave"}
        )).json()["token"]
        assert (await c.get(
            "/v1/policy", headers={"Authorization": f"Bearer {child}"}
        )).status_code == 200

    async with _running_app() as c:
        r = await c.get("/v1/policy", headers={"Authorization": f"Bearer {child}"})
        assert r.status_code == 401, "memory 后端不该让令牌活过重启"
