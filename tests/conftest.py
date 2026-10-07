"""共享测试装置。

一律从**真实的契约文件**加载策略、价格、词表与提示词——不在这里造一份简化版。
造一份简化版会让测试通过而生产失败，那是最没价值的测试。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatcher.core.policy import Policy, load_policy
from dispatcher.core.pricing import Pricing, load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.registry import HandlerRegistry
from dispatcher.core.settings import REPO_ROOT, Settings, get_settings
from dispatcher.core.taxonomy import Taxonomy, load_taxonomy
from dispatcher.plugins import build_registry

from .fakes import ScriptedLLM


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def schemas(repo_root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted((repo_root / "schemas").glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        out[s["$id"]] = s
    return out


@pytest.fixture(scope="session")
def policy() -> Policy:
    return load_policy(REPO_ROOT / "config" / "routing.policy.yaml")


@pytest.fixture(scope="session")
def pricing() -> Pricing:
    return load_pricing(REPO_ROOT / "config" / "pricing.yaml")


@pytest.fixture(scope="session")
def taxonomy() -> Taxonomy:
    return load_taxonomy(REPO_ROOT / "config" / "taxonomy.yaml")


@pytest.fixture(scope="session")
def registry() -> HandlerRegistry:
    return build_registry(REPO_ROOT / "config" / "handlers.yaml")


@pytest.fixture(scope="session")
def prompts() -> PromptLibrary:
    return PromptLibrary(REPO_ROOT / "prompts")


@pytest.fixture
def settings() -> Settings:
    """不带 .env 的设置——测试不依赖真实密钥，也不该因为密钥缺失而失败。"""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        contract_root=REPO_ROOT,
        llm_api_key="test-key",
        llm_base_url="http://localhost:1/v1",
        llm_cheap_model="test-cheap",
        llm_standard_model="test-standard",
        llm_strong_model="test-strong",
    )


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch: pytest.MonkeyPatch):
    """整套测试默认在**鉴权关闭**下跑。

    显式把 ``DISPATCHER_AUTH_TOKEN`` 置空，而不是"指望它没被配置"：``.env`` 是
    开发者的私人物品（已被 gitignore），里头有没有 token 不该决定本仓测试是绿是红。
    空值会覆盖 ``.env`` 里的值（环境变量优先级高于 dotenv 文件），因此结论只取决于
    这一行。

    需要"配了 token 会怎样"的用例，用 ``create_app(auth=AuthConfig(...))`` 显式注入
    （见 tests/test_auth.py），不依赖环境，也就不受这个 fixture 影响。
    """
    monkeypatch.setenv("DISPATCHER_AUTH_TOKEN", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def scripted() -> ScriptedLLM:
    return ScriptedLLM([])
