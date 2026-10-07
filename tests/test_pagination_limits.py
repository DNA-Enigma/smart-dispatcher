"""分页 ``limit`` 的夹取（第 3 处）。

三条路都要夹：``GET /v1/tasks``、``GET /v1/evolution/suggestions``、
``GET /v1/policy/versions``。上界直接取 ``openapi.yaml`` 已声明的
``minimum: 1, maximum: 100``，不在代码里另发明一组数。

**为什么钉"下游收到了几"而不是钉响应体长度**：夹取点在接口层，越界值原样下传时
两种后端都不会报错——内存实现是 `rows[:limit]`（``limit=-1`` 静默少给一条），
SQLite 是 ``LIMIT -1``（**不限，拉全表**）。行为差异藏在存储实现里，因此只能盯住
传下去的那个数。

**为什么是夹取而不是 400**：走 FastAPI 的 ``Query(ge=1, le=100)`` 会返回 422 +
``{"detail": ...}``，那不是契约里的 Problem 体；为夹一个分页参数而制造一次契约违规
不划算。这里与 ``retain_days`` 的口径一致：越界就夹到边界。
"""

from __future__ import annotations

import httpx
import pytest
import yaml

from dispatcher.interface import app as app_module
from dispatcher.interface.app import _LIMIT_MAX, _LIMIT_MIN, create_app

TASKS = "/v1/tasks"
SUGGESTIONS = "/v1/evolution/suggestions"
VERSIONS = "/v1/policy/versions"


class _RecordingEvolution:
    def __init__(self) -> None:
        self.limits: list[int] = []

    async def list_suggestions(self, *, status=None, limit=50):
        self.limits.append(limit)
        return []

    async def list_versions(self, *, limit=50):
        self.limits.append(limit)
        return []

    async def suggest_drop_rate(self):
        return 0.0

    async def suggest_reject_rate(self):
        return 0.0


class _RecordingDispatcher:
    """记录"下游收到的 limit 是几"，不实现任何查询语义。"""

    def __init__(self) -> None:
        self.limits: list[int] = []
        self.evolution = _RecordingEvolution()

    async def list(self, *, tenant_id, user_id=None, limit=20):
        self.limits.append(limit)
        return []


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _RecordingDispatcher:
    f = _RecordingDispatcher()
    monkeypatch.setattr(app_module, "_dispatcher", f)
    return f


@pytest.fixture
async def client(rec: _RecordingDispatcher):
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ------------------------------------------------------------------ 任务列表
@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("-1", 1),        # SQLite 的 LIMIT -1 = 不限 → 全表
        ("0", 1),         # LIMIT 0 = 什么都不返回，而调用方要的是第一页
        ("-99999999", 1),
        ("100000", 100),  # 上界就是契约声明的 100
        ("101", 100),
        ("100", 100),
        ("20", 20),       # 界内原样
        ("1", 1),
    ],
)
async def test_task_list_limit_is_clamped(client, rec, sent: str, expected: int):
    resp = await client.get(f"{TASKS}?limit={sent}")
    assert resp.status_code == 200, resp.text
    assert rec.limits == [expected], f"limit={sent} 应夹到 {expected}"


async def test_task_list_default_limit_is_untouched(client, rec):
    resp = await client.get(TASKS)
    assert resp.status_code == 200, resp.text
    assert rec.limits == [20]


# ------------------------------------------------------------------ 建议与版本
@pytest.mark.parametrize(("sent", "expected"), [("-1", 1), ("0", 1), ("99999", 100), ("7", 7)])
async def test_suggestion_list_limit_is_clamped(client, rec, sent: str, expected: int):
    resp = await client.get(f"{SUGGESTIONS}?limit={sent}")
    assert resp.status_code == 200, resp.text
    assert rec.evolution.limits == [expected]


@pytest.mark.parametrize(("sent", "expected"), [("-1", 1), ("0", 1), ("99999", 100), ("7", 7)])
async def test_policy_version_list_limit_is_clamped(client, rec, sent: str, expected: int):
    resp = await client.get(f"{VERSIONS}?limit={sent}")
    assert resp.status_code == 200, resp.text
    assert rec.evolution.limits == [expected]


def test_clamp_bounds_match_the_contract(repo_root):
    """上界不是随手定的：``openapi.yaml`` 对 limit 声明了 1..100，代码里用同一组。

    两处不一致时，客户端按契约发 100 会被夹成别的数，而它无从知道。
    """
    spec = yaml.safe_load((repo_root / "openapi.yaml").read_text(encoding="utf-8"))
    params = spec["paths"]["/tasks"]["get"]["parameters"]
    limit_schema = next(p["schema"] for p in params if p["name"] == "limit")
    assert (_LIMIT_MIN, _LIMIT_MAX) == (limit_schema["minimum"], limit_schema["maximum"])
