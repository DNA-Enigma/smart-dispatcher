"""P2-c：发放 / 列表 / 撤销令牌。

审计记的是 P2-7「无发放 token 端点」——单用户时一个静态 token 够用，多用户试点
却无从下发凭据，换人只能改环境变量重启。这个文件钉住补齐之后的行为，重点在**谁是
发放者**与**撤销真的生效**：

* **发放权**：只有主令牌能发。无凭据、错凭据、以及"有效但不是主令牌"的凭据都
  必须 401——第三种是最容易被漏掉的一条，它决定了权限会不会顺着发放端点扩散。
* **开放性反例**：不存在"无凭据即可发放"的路径，包括鉴权关闭（未配主令牌）时。
* **签出来的令牌真能用**：换一枚子令牌调既有端点，身份是它绑定的 tenant/user。
* **撤销**：撤销后同一枚令牌立刻 401，且不影响别的令牌。
* **请求体**：缺字段 400、类型错 400、多余字段**不**拒绝（与全仓读体口径一致）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from jsonschema import validate as jsonschema_validate

from dispatcher.core.contract import TaskEnvelope
from dispatcher.interface import app as app_module
from dispatcher.interface.app import create_app
from dispatcher.interface.auth import ADMIN_PATH_PREFIXES, PUBLIC_PATHS, AuthConfig

PROBLEM_SCHEMA_ID = "https://smart-dispatcher.dev/schemas/problem.json"

MASTER = "master-static-token"
AUTH = AuthConfig(token=MASTER, tenant_id="t_owner", user_id="u_owner")


# ---------------------------------------------------------------------------
# 假调度器：本文件测接口层，不跑流水线
# ---------------------------------------------------------------------------
class _Record:
    """``_accepted()`` 需要的最小字段集（与 test_auth.py 同形）。"""

    task_id = "t_new"
    status = "running"
    mode = "async"
    mode_changed = False
    mode_change_reason = None
    policy_version = "test-1"
    created_at = datetime(2026, 10, 8, tzinfo=UTC)
    profile = None


class _FakeLedger:
    currency = "CNY"
    enforcement = "advisory"

    def user_total(self, user_id: str) -> float:
        return 0.0

    def tenant_total(self, tenant_id: str) -> float:
        return 0.0


class _FakeDispatcher:
    def __init__(self) -> None:
        self.ledger = _FakeLedger()
        # ``GET /v1/tasks`` 记下 list() 拿到的租户；``POST /v1/tasks`` 记下信封，
        # 用来断言子令牌的身份真的走到了流水线入口。
        self.list_calls: list[dict] = []
        self.submitted: list[TaskEnvelope] = []

    async def list(self, *, tenant_id: str, user_id: str | None, limit: int):
        self.list_calls.append(
            {"tenant_id": tenant_id, "user_id": user_id, "limit": limit}
        )
        return []

    async def submit(self, envelope: TaskEnvelope, **_kw) -> _Record:
        self.submitted.append(envelope)
        return _Record()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatcher:
    f = _FakeDispatcher()
    monkeypatch.setattr(app_module, "_dispatcher", f)
    return f


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _master_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {MASTER}"}


async def _issue(client, *, tenant_id="t_beta", user_id="u_beta") -> dict:
    resp = await client.post(
        "/v1/tokens",
        json={"tenant_id": tenant_id, "user_id": user_id},
        headers=_master_headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ------------------------------------------------------------------ 谁有权发放
async def test_issuing_without_a_token_is_401(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens", json={"tenant_id": "t", "user_id": "u"}
        )
    assert resp.status_code == 401, resp.text


async def test_issuing_with_a_wrong_token_is_401(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": "t", "user_id": "u"},
            headers={"authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401, resp.text


async def test_a_valid_non_master_token_cannot_issue(fake):
    """**最关键的一条**：能读自己任务的子令牌不能给别人发凭据。

    没有这一条，子令牌持有者一次 ``POST`` 就能造出任意 tenant/user 的凭据——
    发放端点会把越权放大，而不是限制它。
    """
    async with _client(create_app(auth=AUTH)) as c:
        beta = (await _issue(c))["token"]
        # 子令牌能正常调既有端点……
        ok = await c.get("/v1/tasks", headers={"authorization": f"Bearer {beta}"})
        assert ok.status_code == 200, ok.text
        # ……但调发放端点必须被拒
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": "t_gamma", "user_id": "u_gamma"},
            headers={"authorization": f"Bearer {beta}"},
        )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthorized"


async def test_revoking_without_a_master_token_is_401(fake):
    """撤销与列表同理：普通凭据不能删别人的令牌。"""
    async with _client(create_app(auth=AUTH)) as c:
        issued = await _issue(c)
        beta = issued["token"]
        for method, path in (
            ("delete", f"/v1/tokens/{issued['token_id']}"),
            ("get", "/v1/tokens"),
        ):
            resp = await getattr(c, method)(
                path, headers={"authorization": f"Bearer {beta}"}
            )
            assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"


async def test_issuing_with_master_token_succeeds(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": "t_beta", "user_id": "u_beta"},
            headers=_master_headers(),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == "t_beta" and body["user_id"] == "u_beta"
    assert body["token"] and body["token_id"].startswith("tok_")
    assert body["created_at"].endswith("Z")


async def test_issued_token_authenticates_existing_endpoints(fake):
    """签出来的令牌是能用的凭据，租户身份就是它绑定的那一份。

    ``list()`` 收到的 ``user_id`` 是**筛选项**（契约里的 query 参数），不是身份
    ——身份的 user_id 由下一条用例从任务信封上验。
    """
    async with _client(create_app(auth=AUTH)) as c:
        beta = (await _issue(c, tenant_id="t_beta", user_id="u_beta"))["token"]
        resp = await c.get("/v1/tasks", headers={"authorization": f"Bearer {beta}"})
    assert resp.status_code == 200, resp.text
    assert fake.list_calls == [{"tenant_id": "t_beta", "user_id": None, "limit": 20}]


async def test_issued_token_identity_reaches_the_task_envelope(fake):
    """子令牌的 tenant/user 真的成了流水线入口认定的身份（两侧都被 token 覆盖）。"""
    payload = {
        "identity": {"tenant_id": "t_victim", "user_id": "u_victim"},
        "input": {"text": "记一笔"},
    }
    async with _client(create_app(auth=AUTH)) as c:
        beta = (await _issue(c, tenant_id="t_beta", user_id="u_beta"))["token"]
        resp = await c.post(
            "/v1/tasks", json=payload, headers={"authorization": f"Bearer {beta}"}
        )
    assert resp.status_code in (200, 202), resp.text
    got = fake.submitted[0].identity
    assert (got.tenant_id, got.user_id) == ("t_beta", "u_beta")


async def test_master_token_still_works_on_normal_endpoints(fake):
    """主令牌不只是发放者，它仍是所有者自己的凭据。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.get("/v1/tasks", headers=_master_headers())
    assert resp.status_code == 200, resp.text
    assert fake.list_calls == [{"tenant_id": "t_owner", "user_id": None, "limit": 20}]


# ---------------------------------------------------------------------- 撤销
async def test_revoked_token_is_rejected(fake):
    async with _client(create_app(auth=AUTH)) as c:
        issued = await _issue(c)
        beta = issued["token"]
        # 撤销前能用
        assert (
            await c.get("/v1/tasks", headers={"authorization": f"Bearer {beta}"})
        ).status_code == 200
        gone = await c.delete(
            f"/v1/tokens/{issued['token_id']}", headers=_master_headers()
        )
        assert gone.status_code == 204, gone.text
        # 撤销后同一枚立刻 401
        after = await c.get("/v1/tasks", headers={"authorization": f"Bearer {beta}"})
    assert after.status_code == 401, after.text


async def test_revoking_one_token_leaves_others_alone(fake):
    async with _client(create_app(auth=AUTH)) as c:
        a = await _issue(c, tenant_id="t_a", user_id="u_a")
        b = await _issue(c, tenant_id="t_b", user_id="u_b")
        await c.delete(f"/v1/tokens/{a['token_id']}", headers=_master_headers())
        resp = await c.get(
            "/v1/tasks", headers={"authorization": f"Bearer {b['token']}"}
        )
    assert resp.status_code == 200, resp.text
    assert fake.list_calls == [{"tenant_id": "t_b", "user_id": None, "limit": 20}]


async def test_revoking_an_unknown_token_is_404(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.delete("/v1/tokens/tok_nope", headers=_master_headers())
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "not_found"


async def test_double_revoke_is_404_not_500(fake):
    """重复撤销：效果幂等，响应如实——不是假装成功，也不是崩。"""
    async with _client(create_app(auth=AUTH)) as c:
        issued = await _issue(c)
        first = await c.delete(
            f"/v1/tokens/{issued['token_id']}", headers=_master_headers()
        )
        second = await c.delete(
            f"/v1/tokens/{issued['token_id']}", headers=_master_headers()
        )
    assert (first.status_code, second.status_code) == (204, 404)


# ---------------------------------------------------------------------- 列表
async def test_list_never_returns_the_plaintext_token(fake):
    """列表只给元数据。明文只在签发那一次出现——否则它就成了一个可被反复读的秘密。"""
    async with _client(create_app(auth=AUTH)) as c:
        issued = await _issue(c)
        resp = await c.get("/v1/tokens", headers=_master_headers())
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["token_id"] for i in items] == [issued["token_id"]]
    assert "token" not in items[0]
    assert issued["token"] not in resp.text


async def test_revoked_token_disappears_from_the_list(fake):
    async with _client(create_app(auth=AUTH)) as c:
        issued = await _issue(c)
        await c.delete(f"/v1/tokens/{issued['token_id']}", headers=_master_headers())
        resp = await c.get("/v1/tokens", headers=_master_headers())
    assert resp.json()["items"] == []


# ------------------------------------------------------- 开放性反例（关键）
async def test_open_mode_cannot_issue_tokens(fake):
    """鉴权关闭（未配主令牌）时**发放端点仍然 401**。

    这是"不存在无凭据即可发放的路径"在最宽松配置下的检验：关闭鉴权本意是本地
    开发放行读写，绝不能顺带把**发放**也放行——那会让一台忘配 token 的机器成为
    凭据工厂。
    """
    async with _client(create_app()) as c:  # 不传 auth → 未配主令牌
        resp = await c.post(
            "/v1/tokens", json={"tenant_id": "t", "user_id": "u"}
        )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthorized"


async def test_every_token_route_refuses_anonymous_access(fake):
    """把三个令牌端点逐个机械地探一遍，避免只测了 POST 而漏掉 DELETE/GET。

    POST 带的是**合法请求体**：若发放端点被误放进 ``PUBLIC_PATHS``，中间件会直接
    放行、端点会真的签出一枚令牌并回 201——这样断言就验红，而不是被"缺字段 → 400"
    这种无关的失败挡住（那会让这条反例看起来是绿的）。
    """
    app = create_app(auth=AUTH)
    paths = {
        r.path for r in app.routes if getattr(r, "path", "").startswith("/v1/tokens")
    }
    assert paths == {"/v1/tokens", "/v1/tokens/{token_id}"}, f"令牌端点集合变了：{paths}"
    async with _client(app) as c:
        resp = await c.post(
            "/v1/tokens", json={"tenant_id": "t", "user_id": "u"}
        )
        assert resp.status_code == 401, resp.text
        assert (await c.get("/v1/tokens")).status_code == 401
        assert (await c.delete("/v1/tokens/tok_x")).status_code == 401


def test_token_paths_are_not_public_and_not_overlapping():
    """两张清单的失败方向相反，必须互不相交。

    ``PUBLIC_PATHS``（免鉴权）与 ``ADMIN_PATH_PREFIXES``（只认主令牌）若重叠，
    中间件的分支顺序就会决定行为——那是"哪一行写在前面"这种最不该出现的语义。
    """
    for prefix in ADMIN_PATH_PREFIXES:
        for public in PUBLIC_PATHS:
            assert not (public == prefix or public.startswith(prefix + "/")), (
                f"公开路径 {public} 落在管理员前缀 {prefix} 下"
            )
    # 发放端点本身绝不在公开清单里（这是"任何人能发 token"的唯一入口）
    assert "/v1/tokens" not in PUBLIC_PATHS


# -------------------------------------------------------------- 请求体校验
async def test_missing_field_is_400(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens", json={"tenant_id": "t"}, headers=_master_headers()
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_request"
    assert "user_id" in resp.json()["context"]["missing"]


async def test_wrong_type_is_400(fake):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": 123, "user_id": ["u"]},
            headers=_master_headers(),
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_request"


async def test_empty_string_identity_is_400(fake):
    """空串能过 ``required``（不是 None），但会签出一枚绑不到任何数据的令牌。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": "", "user_id": "u"},
            headers=_master_headers(),
        )
    assert resp.status_code == 400, resp.text


async def test_extra_field_is_ignored_not_rejected(fake):
    """多余字段放行（向前兼容），与全仓 read_body 口径一致。"""
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            json={"tenant_id": "t", "user_id": "u", "expires_in": 3600},
            headers=_master_headers(),
        )
    assert resp.status_code == 201, resp.text


async def test_invalid_json_body_is_a_problem_not_a_500(fake, schemas):
    async with _client(create_app(auth=AUTH)) as c:
        resp = await c.post(
            "/v1/tokens",
            content=b"{not json",
            headers={**_master_headers(), "content-type": "application/json"},
        )
    assert resp.status_code == 400, resp.text
    jsonschema_validate(resp.json(), schemas[PROBLEM_SCHEMA_ID])
