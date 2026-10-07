"""接口鉴权。

契约（``openapi.yaml`` 的 securitySchemes）早就声明了全局 ``bearerAuth`` 与
"取出 user_id 与 tenant_id"的语义，而实现此前**一行都没有**：``create_app()`` 里
没有任何中间件或依赖，``app.user_middleware == []``。这个文件钉住补齐之后的行为。

三条要求分开测，因为它们是三件事：

1. **谁能进来**：没 token / 错 token / 非 bearer 方案 → 401，且响应体是合法的
   Problem（不是 FastAPI 默认的 ``{"detail": ...}``）。
2. **一个都别漏**：机械地遍历 ``app.routes``，除公开路径外每条都必须 401。人手
   维护一份"要保护的端点清单"一定会漏（这正是当初十九个端点零鉴权的成因）。
3. **进来的是谁**：身份只能来自 token。请求体自报的 tenant_id/user_id 被覆盖，
   不再有任何一条路径去读 ``x-tenant-id``/``x-user-id``/query 参数。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.core.settings import get_settings
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from dispatcher.interface.auth import PUBLIC_PATHS, AuthConfig

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"

TOKEN = "s3cret-shared-token"
AUTH = AuthConfig(token=TOKEN, tenant_id="t_acme", user_id="u_owner")


# ---------------------------------------------------------------------------
# 假的调度器：本文件测的是接口层，不跑流水线
# ---------------------------------------------------------------------------
class _Record:
    """``_accepted()`` 需要的最小字段集。"""

    task_id = "t_new"
    status = "running"
    mode = "async"
    mode_changed = False
    mode_change_reason = None
    policy_version = "test-1"
    created_at = datetime(2026, 10, 7, tzinfo=UTC)
    # ``_accepted()`` 会读 ``record.profile`` 取 est_cost / est_latency_ms；
    # 没有画像的任务（开模式或评估未跑完）这一位就是 None，短路掉即可。
    profile = None


class _FakePolicy:
    model_tier_ids = ("cheap", "standard", "strong")
    policy_version = "test-1"


class _FakeRegistry:
    ids = frozenset({"bookkeeping"})
    executable_ids = frozenset({"bookkeeping"})


class _FakeLedger:
    currency = "CNY"
    enforcement = "advisory"

    def __init__(self) -> None:
        self.tenant_calls: list[str] = []

    def user_total(self, user_id: str) -> float:
        return 0.0

    def tenant_total(self, tenant_id: str) -> float:
        self.tenant_calls.append(tenant_id)
        return 0.0


class _FakeDispatcher:
    def __init__(self) -> None:
        self.policy = _FakePolicy()
        self.registry = _FakeRegistry()
        self.ledger = _FakeLedger()
        self.execution_enabled = False
        self.config_warnings: list[str] = []
        self.submitted: list[TaskEnvelope] = []
        self.list_calls: list[dict] = []

    async def submit(self, envelope: TaskEnvelope, **_kw) -> _Record:
        self.submitted.append(envelope)
        return _Record()

    async def list(self, *, tenant_id: str, user_id: str | None, limit: int):
        self.list_calls.append(
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit}
        )
        return []


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatcher:
    f = _FakeDispatcher()
    monkeypatch.setattr(app_module, "_dispatcher", f)
    return f


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --------------------------------------------------------------- 鉴权开关
async def test_health_is_reachable_without_a_token(fake):
    """契约里 ``/health`` 显式声明 ``security: []``——监控探针不该需要凭据。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/health")
    assert resp.status_code == 200, resp.text


async def test_health_is_reachable_with_a_wrong_token_too(fake):
    """公开路径不校验 token：带一个错的也放行，而不是"带了就必须对"。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/health", headers={"authorization": "Bearer nonsense"})
    assert resp.status_code == 200, resp.text


async def test_missing_token_is_401(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks")
    assert resp.status_code == 401, resp.text


async def test_wrong_token_is_401(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": "Bearer wrong"})
    assert resp.status_code == 401, resp.text


async def test_partial_token_is_401(fake):
    """前缀/超串都不行——防的是"用 startswith 比对"这类实现。"""
    async with _client(create_app(auth=AUTH)) as c:
        for bogus in (TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
            resp = await c.get("/v1/tasks", headers={"authorization": f"Bearer {bogus}"})
            assert resp.status_code == 401, f"{bogus!r} 不该被接受"


async def test_non_bearer_scheme_is_401(fake):
    """方案名不匹配就不是 bearer 凭据，不能因为"值恰好等于 token"而放行。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": f"Basic {TOKEN}"})
    assert resp.status_code == 401, resp.text


async def test_empty_bearer_value_is_401(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": "Bearer "})
    assert resp.status_code == 401, resp.text


async def test_bearer_scheme_is_case_insensitive(fake):
    """RFC 7235：方案名大小写不敏感。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text


async def test_valid_token_is_accepted(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_401_body_is_a_contract_problem(fake, schemas):
    """401 必须是契约里的 Problem，而不是 FastAPI 默认的 ``{"detail": ...}``。

    中间件在 ``ExceptionMiddleware`` 之外，异常处理器接不到它——所以这一条同时
    钉住"中间件自己渲染了 Problem 体"这件事。
    """
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"authorization": "Bearer wrong"})
    assert resp.status_code == 401
    body = resp.json()
    jsonschema_validate(body, schemas[PROBLEM_SCHEMA_ID])
    assert body["code"] == "unauthorized"
    assert body["status"] == 401
    assert body["retryable"] is False
    # RFC 6750：401 必须带 WWW-Authenticate，且指明 invalid_token
    assert resp.headers["www-authenticate"].startswith("Bearer ")
    assert "invalid_token" in resp.headers["www-authenticate"]


async def test_missing_credentials_challenge_has_no_error_code(fake):
    """没带凭据时不该声称 "invalid_token"——那是"带了但不对"的意思。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks")
    assert "error=" not in resp.headers["www-authenticate"]


async def test_401_echoes_the_request_id(fake):
    """被拒的请求也要能跨服务追踪，否则线上排查只能靠猜。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers={"x-request-id": "rid-42"})
    assert resp.json()["request_id"] == "rid-42"


# ----------------------------------------------------------- 端点覆盖率
def _all_paths(app) -> set[str]:
    return {
        r.path for r in app.routes if getattr(r, "path", "").startswith("/")
    }


async def test_no_route_is_reachable_without_a_token(fake):
    """机械遍历，而不是人工维护清单。

    鉴权在中间件里、**早于路由匹配**，因此任意方法都会先撞上 401——这让我们能
    用 GET 逐个探所有路径。公开路径之外一条都不许漏。
    """
    app = create_app(auth=AUTH)
    protected = sorted(_all_paths(app) - PUBLIC_PATHS)
    assert len(protected) >= 19, f"路径集合异常地小：{protected}"  # 端点被删掉时报警
    async with _client(app) as c:
        for path in protected:
            resp = await c.get(path.replace("{", "").replace("}", ""))
            assert resp.status_code == 401, f"{path} 未受鉴权保护：{resp.status_code}"


async def test_docs_and_openapi_are_protected(fake):
    """文档端点是自省面，会暴露路由/价格/限额，与 ``/v1/policy`` 同级。"""
    async with _client(create_app(auth=AUTH)) as c:
        for path in ("/docs", "/openapi.json", "/redoc"):
            resp = await c.get(path)
            assert resp.status_code == 401, f"{path} 未受保护"


async def test_policy_mutation_endpoints_need_a_token(fake):
    """热改全局策略的两个端点：无凭据必须 401。

    ``approve`` 会在运行中换掉整份策略（路由、价格、限额），``rollback`` 同理。
    本轮审计里它们是**无凭据可改**的，因此单列一条盯着。
    """
    async with _client(create_app(auth=AUTH)) as c:
        for path in (
            "/v1/policy/rollback",
            "/v1/evolution/suggestions/s_1/approve",
            "/v1/evolution/suggestions/s_1/reject",
            "/v1/evolution/analyze",
        ):
            resp = await c.post(path, json={"to_version": "v1", "reason": "x"})
            assert resp.status_code == 401, f"{path} 未受保护：{resp.status_code}"


# ------------------------------------------------------- 未配置 token 的行为
def test_open_mode_warns_loudly(caplog: pytest.LogCaptureFixture):
    """未配置 token 时放行，但必须留下一条 ERROR。

    "鉴权关着"最危险的形态是**静默**：系统一切正常，而任何能访问端口的人都能
    读全部任务快照与别人的金融截图。这条日志是它唯一能自己浮出水面的地方。
    """
    with caplog.at_level(logging.ERROR, logger="dispatcher"):
        create_app()
    assert "DISPATCHER_AUTH_TOKEN" in caplog.text
    assert "鉴权已关闭" in caplog.text


async def test_open_mode_serves_requests(fake):
    """本地开发开箱即跑：不配 token 就不是 401。"""
    async with _client(create_app()) as c:
        resp = await c.get("/v1/tasks")
    assert resp.status_code == 200, resp.text


async def test_configured_token_does_not_warn(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.ERROR, logger="dispatcher"):
        create_app(auth=AUTH)
    assert "鉴权已关闭" not in caplog.text


# --------------------------------------------------------------- 身份来源
async def test_identity_comes_from_the_token_not_the_body(fake):
    """请求体自报租户被覆盖——它从来不是事实，只是客户端的声明。"""
    payload = {
        "identity": {"tenant_id": "t_victim", "user_id": "u_victim"},
        "input": {"text": "买咖啡 38"},
    }
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tasks", json=payload, headers={"authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code in (200, 202), resp.text
    assert len(fake.submitted) == 1
    got = fake.submitted[0].identity
    assert (got.tenant_id, got.user_id) == ("t_acme", "u_owner")


async def test_identity_mismatch_is_logged_but_not_rejected(fake, caplog):
    """不一致只告警、不 400。

    ``identity`` 是契约里的 **required** 字段，合规客户端必然要发；把一个不具
    授权含义的字段做成错误触发器，会让所有本地 user_id 与 token 绑定身份不同的
    客户端在升级后集体报错——而"服务端不采信自报身份"本身不是错误。
    """
    payload = {
        "identity": {"tenant_id": "t_other", "user_id": "u_other"},
        "input": {"text": "买咖啡 38"},
    }
    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        async with _client(create_app(auth=AUTH)) as c:
            resp = await c.post(
                "/v1/tasks", json=payload, headers={"authorization": f"Bearer {TOKEN}"}
            )
    assert resp.status_code in (200, 202), resp.text
    assert "t_other" in caplog.text and "u_other" in caplog.text


async def test_locale_and_timezone_from_the_body_survive(fake):
    """本地化是展示偏好，不是授权信息——它继续由客户端说了算。"""
    payload = {
        "identity": {
            "tenant_id": "t_other", "user_id": "u_other",
            "locale": "zh-CN", "timezone": "Asia/Shanghai",
        },
        "input": {"text": "记一笔"},
    }
    async with _client(create_app(auth=AUTH)) as c:
        await c.post("/v1/tasks", json=payload, headers={"authorization": f"Bearer {TOKEN}"})
    got = fake.submitted[0].identity
    assert (got.locale, got.timezone) == ("zh-CN", "Asia/Shanghai")


async def test_list_tasks_ignores_a_client_supplied_tenant_id(fake):
    """``?tenant_id=受害者`` 不再能枚举别人的任务。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get(
            "/v1/tasks?tenant_id=t_victim&user_id=u_someone&limit=5",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 200, resp.text
    # tenant 来自 token；user_id 仍是合法筛选条件，但只能在自家租户内选人
    assert fake.list_calls == [{"tenant_id": "t_acme", "user_id": "u_someone", "limit": 5}]


async def test_usage_ignores_a_client_supplied_tenant_id(fake):
    """花销同理：租户只能来自 token。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get(
            "/v1/usage?tenant_id=t_victim", headers={"authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 200, resp.text
    assert fake.ledger.tenant_calls == ["t_acme"]


async def test_open_mode_identity_is_the_configured_constant(fake, monkeypatch):
    """关闭鉴权时身份同样是常量，仍不是客户端说了算。"""
    monkeypatch.setenv("DISPATCHER_TENANT", "t_local")
    monkeypatch.setenv("DISPATCHER_USER", "u_local")
    get_settings.cache_clear()
    payload = {
        "identity": {"tenant_id": "t_victim", "user_id": "u_victim"},
        "input": {"text": "记一笔"},
    }
    async with _client(create_app()) as c:
        resp = await c.post("/v1/tasks", json=payload)
    assert resp.status_code in (200, 202), resp.text
    got = fake.submitted[0].identity
    assert (got.tenant_id, got.user_id) == ("t_local", "u_local")


def test_problem_code_is_registered():
    """``unauthorized`` 必须真在码表里——``DispatcherError`` 会拒绝未知码。"""
    assert DispatcherError("unauthorized", "x").status == 401
