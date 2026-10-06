"""接缝的验收测试：**接入一个新领域，改动 `dispatcher/` 的文件数为 0。**

这条断言是整套设计的核心承诺之一，而"我在文档里写了它"算不上证据。
这里用机械的方式证明它：

1. 先算出 `dispatcher/` 下所有文件的哈希；
2. 在 `dispatcher/` **之外**造一个全新的领域（一个新目录：声明 + 实现）；
3. 经配置把它装进来，**真的跑一个任务**，让这个新 handler 被调用；
4. 再算一次哈希，断言**一模一样**。

这不是"看起来像插件"——它证明调度层确实不知道那个领域的存在。

另外两条同样机械的检查：

* 可执行代码里不出现任何领域名（`bookkeeping` / `calendar` …）——出现了就说明
  调度层知道了领域概念；
* 领域端口（如账本存储）定义在 handler 目录里，而不是 `dispatcher/ports/`。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import sys
from pathlib import Path

import pytest
import yaml

from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.registry import HandlerManifest
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.pipeline import Dispatcher
from dispatcher.plugins import build_registry, import_impl, instantiate, load_specs
from tests.fakes import ScriptedLLM

# 用来验证"新领域"的名字。刻意选一个仓库里没有出现过的词——
# 如果调度层里原本就到处是 bookkeeping，那用它做实验就证明不了什么。
NEW_DOMAIN = "notes"

NEW_MANIFEST = {
    "handler_id": NEW_DOMAIN,
    "version": "0.1.0",
    "capabilities": [f"{NEW_DOMAIN}.append", f"{NEW_DOMAIN}.list"],
    "flows": [],
    "required_ports": ["store"],
    "tools": [
        {
            "name": "add_note", "description": "记一条笔记。",
            "side_effects": "write", "requires_capabilities": [], "idempotent": True,
        },
        {
            "name": "list_notes", "description": "列出笔记。",
            "side_effects": "read", "requires_capabilities": [], "idempotent": True,
        },
    ],
    "config_schema": {"type": "object"},
}

# 一个完整的、能跑的新领域实现。它只依赖 dispatcher 的**端口**形状，
# 不依赖调度层的任何实现细节——这正是"插件"该有的依赖方向。
NEW_IMPL = '''
"""一个全新的领域 handler，位于 dispatcher/ 之外。"""
from __future__ import annotations

from typing import Any

from dispatcher.core.execution import ToolResult


class NotesHandler:
    def __init__(self, manifest) -> None:
        self.manifest = manifest
        self._notes: list[dict] = []

    async def health(self) -> str:
        return "closed"

    async def execute_tool(self, tool_name: str, args: dict, ctx: Any) -> ToolResult:
        if tool_name == "add_note":
            note = {"id": f"n_{len(self._notes) + 1}",
                    "text": args.get("text"), "token": ctx.idempotency_token}
            self._notes.append(note)
            return ToolResult(ok=True, output={"note": note})
        if tool_name == "list_notes":
            return ToolResult(ok=True, output={"notes": self._notes})
        return ToolResult.fail("tool_not_declared", tool_name, retryable=False)
'''


def _tree_hash(root: Path) -> str:
    """对一棵源码树取哈希。排除缓存与字节码——它们不该影响这个判断。"""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}:
            continue
        h.update(str(p.relative_to(root)).encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def new_domain_project(tmp_path: Path):
    """在仓库之外造一个新领域：目录 + 声明 + 实现 + 配置。"""
    pkg = tmp_path / "handlers" / NEW_DOMAIN
    pkg.mkdir(parents=True)
    (tmp_path / "handlers" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "handler.yaml").write_text(
        yaml.safe_dump(NEW_MANIFEST, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (pkg / "handler.py").write_text(NEW_IMPL, encoding="utf-8")

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    spec_path = cfg_dir / "handlers.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {"handlers": [{"manifest": f"handlers/{NEW_DOMAIN}/handler.yaml",
                           "impl": f"handlers.{NEW_DOMAIN}.handler:NotesHandler"}]},
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    # **快照并精确还原 sys.modules。** 这个 fixture 造的包名与仓库里的
    # `handlers` 同名；只删自己加的那几个不够——临时目录里那个 `handlers`
    # 会留在模块缓存里，把真的 `handlers` 包盖住，于是**后续测试**在
    # `import handlers.bookkeeping` 时报 ModuleNotFoundError。
    # 这种污染的特点是"报错出现在别的测试里"，所以值得显式处理。
    saved = {k: v for k, v in sys.modules.items()
             if k == "handlers" or k.startswith("handlers.")}
    # 把已缓存的 `handlers` 先摘掉，否则临时目录里那个不会被导入——
    # 全量跑时它一定已经被别的测试导入过了，于是这个用例只在单独跑时通过。
    # 这类"单独跑过、全量跑挂"的问题，根因几乎都是模块缓存。
    for mod in list(saved):
        sys.modules.pop(mod, None)
    sys.path.insert(0, str(tmp_path))
    try:
        yield {"root": tmp_path, "spec": spec_path, "pkg": pkg}
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules
                    if m == "handlers" or m.startswith("handlers.")]:
            sys.modules.pop(mod, None)
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
def test_adding_a_handler_does_not_change_dispatcher(new_domain_project):
    """**核心断言**：装一个新领域、并真的用它跑一个任务，`dispatcher/` 一字未动。"""
    
    before = _tree_hash(REPO_ROOT / "dispatcher")

    registry = build_registry(new_domain_project["spec"])
    assert NEW_DOMAIN in registry.ids
    assert NEW_DOMAIN in registry.executable_ids

    # 真的跑一遍：让这个新 handler 被调度层调用到
    asyncio.run(_run_one_task(registry))

    after = _tree_hash(REPO_ROOT / "dispatcher")
    assert after == before, (
        "接入新 handler 之后 dispatcher/ 的哈希变了——接缝没有真正成立。"
        "变化说明调度层里有东西知道（或需要知道）这个新领域。"
    )


async def _run_one_task(registry) -> None:
    s = get_settings()
    profile = {
        "task_type": "chat.explain", "intent_summary": "记一条笔记",
        "complexity": {"score": 0.2}, "urgency": {"level": "normal"},
        "required_capabilities": [f"{NEW_DOMAIN}.append"],
        "candidate_capabilities": [f"{NEW_DOMAIN}.append"],
        "recommended_mode": "sync", "needs_clarification": False, "confidence": 0.9,
    }
    decision = {
        "route_id": "single_tool_action", "model_tier": "cheap",
        "handler": NEW_DOMAIN, "tool_set": ["add_note"],
        "execution_mode": "sync", "decompose": False,
        "budget": {"max_cost": 0.01, "max_wall_ms": 5000, "max_llm_calls": 2},
        "rationale": "单步记笔记。", "confidence": 0.9,
    }
    # 单步路径会先做一次参数抽取
    slot_fill = {"text": "买牛奶"}
    llm = ScriptedLLM([profile, decision, slot_fill])

    from dispatcher.adapters.memory_media import InMemoryMediaStore
    from dispatcher.adapters.memory_state import InMemoryStateStore
    from dispatcher.core.agents import load_agents
    from dispatcher.core.budget import BudgetLedger
    from dispatcher.core.eventbus import EventBus
    from dispatcher.core.policy import load_policy
    from dispatcher.core.pricing import load_pricing
    from dispatcher.core.prompts import PromptLibrary
    from dispatcher.core.taxonomy import load_taxonomy

    policy = load_policy(s.policy_path)
    state = InMemoryStateStore()
    d = Dispatcher(
        policy=policy, pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=registry, prompts=PromptLibrary(s.prompts_dir), llm=llm,
        media=InMemoryMediaStore(allowed_mime=policy.limits.media.allowed_mime,
                                 max_bytes=policy.limits.media.max_bytes),
        state=state, taxonomy=load_taxonomy(s.taxonomy_path),
        agents=load_agents(s.agents_path), events=EventBus(state),
        ledger=BudgetLedger(enforcement="advisory", warn_at_ratio=0.8, currency="CNY"),
        execution_enabled=True,
    )
    try:
        rec = await d.submit(TaskEnvelope.model_validate(
            {"identity": {"user_id": "u_1"}, "input": {"text": "买牛奶"}}))
        assert rec.status == "succeeded", rec.error
        # 新 handler 真的被调用了，而且它的产出进了任务产物
        assert rec.artifacts["main"]["note"]["text"] == "买牛奶"
    finally:
        await d.aclose()


def test_plugins_loader_knows_no_handler_names():
    """加载器里不出现任何领域名——它只认得配置格式与导入路径写法。

    排除文档字符串与注释：在文档里举例说明导入路径的写法是应当的，
    禁止的是**可执行代码**真的认识某个领域。
    """
    p = REPO_ROOT / "dispatcher" / "plugins.py"
    tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    docs = _docstring_ids(tree)
    known = _known_domains()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docs:
                continue
            for name in known:
                assert name not in node.value, f"plugins.py:{node.lineno} 出现领域名 {name!r}"


def test_dispatcher_source_contains_no_domain_names():
    """可执行代码里不出现领域名。**注释与文档字符串除外**——
    在文档里举例说明是应当的，禁止的是代码真的认识某个领域。
    """
    import ast

    offenders: list[str] = []
    known = _known_domains()
    for p in sorted((REPO_ROOT / "dispatcher").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                for name in known:
                    if name in node.value:
                        offenders.append(f"{p.relative_to(REPO_ROOT)}:{node.lineno} {name!r}")
    assert not offenders, (
        f"调度层的可执行代码里出现了领域名：{offenders}。"
        f"那意味着调度层知道了领域概念，接缝就漏了。"
    )


def _known_domains() -> set[str]:
    """领域名从**配置**里读，而不是在这里写死——这个测试文件自己也不该知道有哪些领域。"""
    from dispatcher.core.yamlio import load_yaml

    raw = load_yaml(REPO_ROOT / "config" / "handlers.yaml")
    return {
        Path(item["manifest"]).parent.name
        for item in raw.get("handlers") or []
    }


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def test_domain_ports_live_with_the_domain():
    """领域端口（账本存储之类）住在 handler 目录里，不在 `dispatcher/ports/`。

    把账本端口放进调度层，等于让调度层知道了"账本"这个概念——接缝就漏了。
    它属于领域，就该跟领域住在一起。
    """
    assert (REPO_ROOT / "handlers" / "bookkeeping" / "ports.py").exists()
    for p in (REPO_ROOT / "dispatcher" / "ports").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "Ledger" not in text, f"{p.name} 里出现了领域端口 LedgerPort"
        assert "Calendar" not in text, f"{p.name} 里出现了领域端口 Calendar"


def test_a_handler_can_be_injected_with_its_own_dependencies():
    """外部的应用要能带着自己的数据库把 handler 注进来。

    这是"账本 schema 属于消费端"的落地方式：调度层提供一个注入通道，
    它不知道注入进来的东西连的是什么库。
    """
    from handlers.bookkeeping.handler import BookkeepingHandler
    from handlers.bookkeeping.ports import LedgerPort

    class MyLedger:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        async def append(self, entry: dict, *, idempotency_token: str):
            stored = {**entry, "id": f"row_{len(self.rows)}", "_token": idempotency_token}
            self.rows.append(stored)
            return stored, False

        async def find_by_token(self, token: str):
            return next((r for r in self.rows if r.get("_token") == token), None)

        async def recent(self, *, limit: int):
            return list(reversed(self.rows))[:limit]

    manifest = HandlerManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "handlers" / "bookkeeping" / "handler.yaml").read_text("utf-8"))
    )
    ledger = MyLedger()
    handler = BookkeepingHandler(manifest, ledger=ledger)
    assert isinstance(ledger, LedgerPort), "注入的存储必须满足领域端口"
    assert handler._ledger is ledger


def test_broken_impl_path_fails_at_load_time_not_first_request():
    """拼错的 impl 路径必须在**加载期**报错。

    留到第一次请求才炸不是"晚了一点"，而是"服务正常启动了却处理不了请求"。
    """
    import tempfile

    from dispatcher.core.errors import DispatcherError

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config" / "handlers.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(yaml.safe_dump({
            "handlers": [{
                # 绝对路径：load_specs 对绝对路径原样使用，这样这个用例
                # 只测"impl 拼错"，不牵扯 manifest 的相对路径解析
                "manifest": str(REPO_ROOT / "handlers" / "bookkeeping" / "handler.yaml"),
                "impl": "no.such.module:Nope",
            }]
        }, allow_unicode=True), encoding="utf-8")
        with pytest.raises(DispatcherError) as ei:
            build_registry(cfg)
        assert "导入 handler 模块失败" in ei.value.detail


def test_impl_path_format_is_strict():
    from dispatcher.core.errors import DispatcherError

    with pytest.raises(DispatcherError):
        import_impl("handlers.bookkeeping.handler")     # 少了 :类名
    with pytest.raises(DispatcherError):
        import_impl(":OnlyClass")


def test_instantiate_reports_a_bad_constructor_clearly():
    from dispatcher.core.errors import DispatcherError

    class NeedsTwoArgs:
        def __init__(self, a, b) -> None:
            pass

    manifest = HandlerManifest.model_validate(NEW_MANIFEST)
    with pytest.raises(DispatcherError) as ei:
        instantiate(NeedsTwoArgs, manifest)
    assert "manifest" in ei.value.detail


def test_specs_load_from_config_with_relative_paths(new_domain_project):
    specs = load_specs(new_domain_project["spec"])
    assert len(specs) == 1
    assert specs[0].manifest_path.exists()
    assert specs[0].impl_path == f"handlers.{NEW_DOMAIN}.handler:NotesHandler"
