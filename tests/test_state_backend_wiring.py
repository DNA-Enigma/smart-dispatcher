"""持久后端的**接线**：配置项 → ``DispatcherConfig`` → 真正装上的实现。

这个文件回答的是 docs/12-deployment.md 第 7 节末那条发现：``SqliteStateStore`` /
``SqliteEvolutionStore`` 早就实现且有测试，但 ``Settings`` 没有开关、``lifespan``
不传 config，于是**生产必然跑内存后端，改 ``.env`` 换不了**。

因此这里不测存储实现本身（那些在 tests/test_sqlite_state.py），只测三件事：

1. 配置项存在、有值域、缺省是 ``memory``；
2. ``build()`` 按它装上对应的实现，且 ``astart()`` 把库**打开**了
   （``SqliteStateStore`` 是 open→用→close，不 open 的话第一个请求就炸）；
3. ``lifespan`` 真的读 ``.env``/环境变量并把开关传下去——即"改 .env 就能换后端"。
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from dispatcher.adapters.memory_evolution import InMemoryEvolutionStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.adapters.sqlite_evolution import SqliteEvolutionStore
from dispatcher.adapters.sqlite_state import SqliteStateStore
from dispatcher.core.policy import load_policy
from dispatcher.core.settings import REPO_ROOT, Settings, get_settings
from dispatcher.interface.app import create_app, get_dispatcher
from dispatcher.interface.auth import AuthConfig
from dispatcher.pipeline import Dispatcher, DispatcherConfig, describe_config

AUTH = AuthConfig(token="t_master", tenant_id="t_acme", user_id="u_owner")


# --------------------------------------------------------------- 配置项本身
def test_backend_defaults_to_memory(settings: Settings):
    """**代码缺省是 memory**：跑一次测试/起一次本地服务不该在仓库里留下 data/ 库。

    生产的持久化不靠缺省值兜底，靠 ``.env.example`` 里写死 sqlite + 启动时那条
    ERROR（见 test_warns_when_memory_looks_like_production）。
    """
    assert settings.dispatcher_state_backend == "memory"
    assert settings.dispatcher_state_path is None
    assert settings.state_db_path == REPO_ROOT / "data" / "dispatcher.db"


def test_explicit_path_wins(tmp_path):
    s = Settings(_env_file=None, dispatcher_state_path=tmp_path / "d.db")  # type: ignore[call-arg]
    assert s.state_db_path == tmp_path / "d.db"


def test_backend_rejects_typos():
    """值域是 ``Literal``，不是 ``str``。

    拼错成 ``sqlite3`` 若被静默当成"不是 sqlite"，就等于退回内存后端——
    那正是"用户以为持久了其实没有"的事故。必须在启动时就报错。

    走真正的构造（而不是 ``model_copy``）：后者不做校验，钉不住值域。
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dispatcher_state_backend="sqlite3")  # type: ignore[call-arg]


# --------------------------------------------------------------- build 装上什么
def test_build_uses_memory_adapters_by_default(settings: Settings):
    """不加开关时与改动前一致：两个后端都是内存实现。"""
    d = Dispatcher.build(DispatcherConfig(settings=settings))
    assert isinstance(d.state, InMemoryStateStore)
    assert isinstance(d.evolution_store, InMemoryEvolutionStore)


def test_build_uses_sqlite_adapters_when_asked(settings: Settings, tmp_path):
    db = tmp_path / "state.db"
    d = Dispatcher.build(
        DispatcherConfig(settings=settings, state_backend="sqlite", sqlite_path=db)
    )
    assert isinstance(d.state, SqliteStateStore)
    assert isinstance(d.evolution_store, SqliteEvolutionStore)


async def test_astart_opens_the_sqlite_store(settings: Settings, tmp_path):
    """``build()`` 只构造不打开；``astart()`` 必须把它打开。

    漏了这一步的表现不是"没持久化"，而是**第一个请求 500**
    （``RuntimeError: SqliteStateStore 尚未 open()``）——先在这里钉住。
    """
    db = tmp_path / "state.db"
    d = Dispatcher.build(
        DispatcherConfig(settings=settings, state_backend="sqlite", sqlite_path=db)
    )
    try:
        await d.astart()
        rec = await d.state.get_task("nope")
        assert rec is None  # 能查说明连接真的开了
        assert db.exists()
    finally:
        await d.aclose()


async def test_astart_is_a_noop_for_memory(settings: Settings):
    """内存实现没有 ``open``，``astart()`` 不该要求它有。"""
    d = Dispatcher.build(DispatcherConfig(settings=settings))
    try:
        await d.astart()
        assert isinstance(d.state, InMemoryStateStore)
    finally:
        await d.aclose()


async def test_aclose_closes_the_evolution_store(settings: Settings, tmp_path):
    """演化库的连接也要有出账（此前只关 state，sqlite 后端下每次重建都漏一个）。"""
    db = tmp_path / "state.db"
    d = Dispatcher.build(
        DispatcherConfig(settings=settings, state_backend="sqlite", sqlite_path=db)
    )
    await d.astart()
    await d.aclose()
    # 关掉之后再用就是"库已关闭"，证明 close 真的走到了演化库上
    with pytest.raises(sqlite3.ProgrammingError):
        await d.evolution_store.list_versions()


# --------------------------------------------------------------- 启动日志
def test_describe_config_reports_the_installed_backends(settings: Settings):
    """启动日志报的是**实际装上的类名**，不是回显配置值。

    配置说 sqlite、进程里跑内存实现，是这轮要消掉的那类静默错配；
    运维要能从启动那行日志里直接看出装的是哪一个。
    """
    mem = describe_config(Dispatcher.build(DispatcherConfig(settings=settings)))
    assert '"state_backend": "InMemoryStateStore"' in mem
    assert '"media_store": "InMemoryMediaStore"' in mem


# --------------------------------------------------------------- 开关真的接上了
async def test_lifespan_reads_the_backend_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """**改环境变量（.env 是同一份内容）就能换后端**——这正是此前做不到的事。

    走完整的 lifespan，而不是直接调 ``build()``：不这样测，就无法证明
    ``lifespan`` 把配置传下去了（此前它调用的是不传 config 的 ``build()``）。
    """
    db = tmp_path / "from_env.db"
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("DISPATCHER_STATE_PATH", str(db))
    get_settings.cache_clear()

    app = create_app(auth=AUTH)
    async with app.router.lifespan_context(app):
        d = get_dispatcher()
        assert isinstance(d.state, SqliteStateStore)
        assert isinstance(d.evolution_store, SqliteEvolutionStore)
        # astart() 在 lifespan 里跑过：库文件已经在，且连接可用
        assert db.exists()
        assert await d.state.get_task("nope") is None


async def test_lifespan_warns_when_memory_looks_like_production(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """配了主令牌（= 生产形态）却跑内存后端时，启动日志里必须有一条 ERROR。

    这是"生产忘配"唯一的自曝路径：``.env.example`` 里写死 sqlite 是第一道，
    这条日志是第二道——它拦不住运行，但能让"重启丢全部子令牌"这件事在
    ``journalctl`` 里一眼可见，而不是等用户报 401。
    """
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "memory")
    monkeypatch.setenv("DISPATCHER_AUTH_TOKEN", "t_master")
    get_settings.cache_clear()

    app = create_app()
    with caplog.at_level("ERROR", logger="dispatcher"):
        async with app.router.lifespan_context(app):
            pass
    assert any(
        "重启即丢" in r.message and "DISPATCHER_STATE_BACKEND=sqlite" in r.message
        for r in caplog.records
    ), caplog.text


async def test_lifespan_is_quiet_on_memory_without_auth(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """本地开发（鉴权关）跑内存后端不该被喊——否则这条 ERROR 会变成噪音被忽略。"""
    monkeypatch.setenv("DISPATCHER_STATE_BACKEND", "memory")
    monkeypatch.setenv("DISPATCHER_AUTH_TOKEN", "")
    get_settings.cache_clear()

    app = create_app()
    with caplog.at_level("ERROR", logger="dispatcher"):
        async with app.router.lifespan_context(app):
            pass
    assert not any("重启即丢" in r.message for r in caplog.records)


# --------------------------------------------------------------- 策略不回退
async def test_astart_restores_the_active_policy_version(settings: Settings, tmp_path):
    """库里的生效版本优先于 ``routing.policy.yaml``。

    批准端点会热换内存里的策略，但重启后策略是从 YAML 文件重新装载的——
    用户批准过的改动于是静默消失（docs/12-deployment.md 第 7 节 #5）。
    这里直接种一个"生效版本"再 ``astart()``，验证它被读回来了。
    """
    db = tmp_path / "state.db"
    base = load_policy(REPO_ROOT / "config" / "routing.policy.yaml")
    evolved = base.model_dump(mode="json")
    evolved["policy_version"] = "pv_2026-10-08_99"
    evolved["router"]["temperature"] = 0.42  # 一个一眼可辨、又不影响契约合法性的改动

    store = SqliteEvolutionStore(db)
    try:
        await store.put_version(
            {
                "policy_version": "pv_2026-10-08_99",
                "status": "active",
                "created_at": "2026-10-08T00:00:00+00:00",
                "approved_by": "u_owner",
                "policy": evolved,
            }
        )
        await store.set_active("pv_2026-10-08_99")
    finally:
        await store.close()

    d = Dispatcher.build(
        DispatcherConfig(settings=settings, state_backend="sqlite", sqlite_path=db)
    )
    try:
        assert d.policy.policy_version == base.policy_version  # 装配时还是文件里的版本
        await d.astart()
        assert d.policy.policy_version == "pv_2026-10-08_99"
        assert d.policy.router.temperature == 0.42
    finally:
        await d.aclose()


async def test_astart_keeps_the_file_policy_when_store_is_empty(
    settings: Settings, tmp_path
):
    """库里没有生效版本时不要动策略——空库是正常状态，不是"回退"。"""
    db = tmp_path / "state.db"
    base = load_policy(REPO_ROOT / "config" / "routing.policy.yaml")
    d = Dispatcher.build(
        DispatcherConfig(settings=settings, state_backend="sqlite", sqlite_path=db)
    )
    try:
        await d.astart()
        assert d.policy.policy_version == base.policy_version
    finally:
        await d.aclose()
