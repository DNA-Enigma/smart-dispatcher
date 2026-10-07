"""鉴权与契约的对齐：把"声明与实现一致"变成可执行的断言。

本轮缺陷的根因是**契约承诺了、实现没有**：``openapi.yaml`` 声明了全局
``bearerAuth`` 与"从 token 取出 user_id / tenant_id"，``docs/HANDOFF.md`` 也这么写，
而 ``create_app()`` 里一行鉴权都没有。两份文本各自看起来都没错，只有把人凑齐、
互相问一句才会发现——所以这里把它变成机器来问：

* Problem 的码表（``core/errors.py``）与 ``schemas/problem.json`` 的封闭词表；
* ``openapi.yaml`` 里每个操作是否声明了 401，``/health`` 是否明确免鉴权；
* ``openapi.yaml`` 的路径集合与 ``create_app()`` 实际挂上的路由集合是否一致
  ——**两个方向都要**：声明了没实现是空头承诺，实现了没声明是悄悄扩权。
* ``servers`` 的本地地址是否与启动入口（``main.py``）对得上（此前写的是 8080，
  而实际监听 8000）。
"""

from __future__ import annotations

import re

import pytest
import yaml

from dispatcher.core.errors import ERROR_TABLE, ERROR_TITLES
from dispatcher.core.settings import REPO_ROOT
from dispatcher.interface.app import create_app
from dispatcher.interface.auth import PUBLIC_PATHS

OPENAPI = REPO_ROOT / "openapi.yaml"
_API_PREFIX = "/v1"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                yield path, method, op


# ------------------------------------------------------------------ Problem 码表
def test_problem_codes_match_the_schema_enum(schemas):
    """两侧是**两份手写的东西**，只能靠断言防漂移。

    码表在代码里（因为它同时带着 HTTP 状态与重试语义），封闭词表在
    ``schemas/problem.json`` 里（因为它是对外契约）。改一边忘一边，
    客户端就会收到一个 schema 判为非法的 Problem。
    """
    problem = schemas["https://smart-dispatcher.dev/schemas/problem.json"]
    assert set(ERROR_TABLE) == set(problem["properties"]["code"]["enum"])


def test_every_code_has_a_title():
    """``to_problem`` 用 ``ERROR_TITLES[code]`` 取值——漏一个就是 KeyError（500）。"""
    assert set(ERROR_TITLES) == set(ERROR_TABLE)


def test_unauthorized_is_401_and_not_retryable():
    """凭据不对不会因为"再试一次"而变对。"""
    assert ERROR_TABLE["unauthorized"] == (401, False)


# ------------------------------------------------------------- openapi 一致性
def test_bearer_auth_is_declared_globally(spec):
    scheme = spec["components"]["securitySchemes"]["bearerAuth"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    assert spec["security"] == [{"bearerAuth": []}]
    # 声明了令牌形状就必须真的实现它。参考实现比对的是静态串，不解析 JWT——
    # 写一个谁都不校验的 bearerFormat 正是本文件要防的那类"空头承诺"。
    assert "bearerFormat" not in scheme


def test_every_operation_declares_401_except_health(spec):
    missing = [
        f"{method.upper()} {path}"
        for path, method, op in _operations(spec)
        if "401" not in op["responses"] and path != "/health"
    ]
    assert missing == []


def test_health_opts_out_of_auth(spec):
    """契约里 ``security: []`` 与中间件的 ``PUBLIC_PATHS`` 必须是同一件事。"""
    assert spec["paths"]["/health"]["get"]["security"] == []
    assert PUBLIC_PATHS == {f"{_API_PREFIX}/health"}


def test_declared_paths_are_actually_mounted(spec):
    """声明了没实现 = 空头承诺。"""
    mounted = {
        r.path for r in create_app().routes if getattr(r, "path", "").startswith(_API_PREFIX)
    }
    declared = {f"{_API_PREFIX}{p}" for p in spec["paths"]}
    assert declared - mounted == set()


def test_mounted_routes_are_all_declared(spec):
    """实现了没声明 = 悄悄扩权：一个不在契约里的端点，客户端与审计都看不见它。

    反方向同样要查，因为"多出来的那个端点"恰恰是最容易忘记保护的那个。
    前缀过滤顺带排除了 FastAPI 自带的文档路由（``/docs`` / ``/openapi.json``
    等，它们不挂在 ``/v1`` 下，属于框架实现细节而非本契约的端点）。
    """
    app = create_app()
    mounted = {
        r.path
        for r in app.routes
        if getattr(r, "path", "").startswith(_API_PREFIX)
    }
    declared = {f"{_API_PREFIX}{p}" for p in spec["paths"]}
    assert mounted - declared == set()


def test_servers_match_the_launch_entrypoint(spec):
    """``servers`` 与实际启动方式不一致，客户端照契约连就会连不上。

    审计发现这里写着 ``localhost:8080``，而 ``main.py`` 监听 127.0.0.1:8000。
    """
    main_py = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    host = re.search(r'host="([^"]+)"', main_py)
    port = re.search(r"port=(\d+)", main_py)
    assert host and port, "main.py 的启动参数变了，本断言需要跟着改"
    local = [s["url"] for s in spec["servers"] if "127.0.0.1" in s["url"] or "localhost" in s["url"]]
    assert local == [f"http://{host.group(1)}:{port.group(1)}{_API_PREFIX}"]


def test_identity_fields_are_documented_as_not_authoritative():
    """请求体里的身份不构成授权声明——这一点必须写在契约里。

    客户端读 contract 才知道自己发的 tenant_id 不作数；否则它会让"我在客户端
    切了租户"看起来像生效了。
    """
    envelope = yaml.safe_load(
        (REPO_ROOT / "schemas" / "task_envelope.json").read_text(encoding="utf-8")
    )
    desc = envelope["properties"]["identity"]["description"]
    assert "token" in desc and "覆盖" in desc
