"""架构不变式。

这几条是让"不写死规则"有牙齿的那一半。没有检查的规则会在第一次赶工期时被忘掉。

检查用 AST 而不是 grep，因为 grep 会把注释、文档字符串、以及"在文档里举例说明
反模式"的字符串也当成违规。AST 只看真正会执行的代码。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from dispatcher.core.registry import load_manifests
from dispatcher.plugins import load_specs

CORE_DIRS = ("dispatcher/core", "dispatcher/stages")

# 供应商与模型的识别特征。**只在常量级代码里查**——适配器与文档里有具体名字是
# 应当的（档位绑定总得知道是哪家），但常量级代码里出现就是"硬编码模型"。
PROVIDER_PATTERN = re.compile(
    r"deepseek|kimi|moonshot|qwen|glm|gpt-?\d|claude-|gemini|llama|mistral|ollama",
    re.IGNORECASE,
)
# 形如 xxx-v4-flash / xxx-2.6 的型号串
MODEL_ID_PATTERN = re.compile(r"\b[a-z][a-z0-9]*[-_](?:v\d|\d+\.\d+)", re.IGNORECASE)

# 允许出现的数值字面量：结构性常量，不是策略
STRUCTURAL_NUMBERS = {0, 1, -1, 2}

# 领域谓词的识别：拿这些字段名去和字符串字面量比较
DOMAIN_FIELDS = {
    "task_type", "route_id", "handler", "capability", "tool", "tool_name",
    "domain", "intent",
}


def _py_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for d in CORE_DIRS:
        out.extend(sorted((root / d).rglob("*.py")))
    return out


def _trees(root: Path):
    for p in _py_files(root):
        yield p, ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


# ---------------------------------------------------------------------------
# R2 / R3：分层依赖
# ---------------------------------------------------------------------------
def test_core_and_stages_do_not_import_transport_types(repo_root: Path):
    """R2 —— 编排层不得导入传输类型。核心是"数据类之上的纯异步函数"。"""
    forbidden = {"fastapi", "starlette", "uvicorn", "requests", "sse_starlette"}
    offenders: list[str] = []
    for p, tree in _trees(repo_root):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in forbidden:
                        offenders.append(f"{p}: import {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden:
                    offenders.append(f"{p}: from {node.module}")
    assert not offenders, f"编排层导入了传输类型：{offenders}"


def test_core_and_stages_do_not_import_concrete_stores_or_providers(repo_root: Path):
    """R3 —— 端口层之外不得导入具体存储或供应商。"""
    forbidden = {
        "sqlite3", "aiosqlite", "sqlalchemy", "asyncpg", "psycopg", "psycopg2",
        "boto3", "redis", "openai", "anthropic", "httpx",
    }
    offenders: list[str] = []
    for p, tree in _trees(repo_root):
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in forbidden:
                    offenders.append(f"{p}: {n}")
    assert not offenders, f"编排层导入了具体实现：{offenders}"


# ---------------------------------------------------------------------------
# no-literal-policy
# ---------------------------------------------------------------------------
def test_no_threshold_comparisons_against_literals(repo_root: Path):
    """代码里不得出现 ``if x < 0.15`` 这类阈值比较——阈值属于配置。

    只查**比较**，不查所有数字：``range(3)``、``[0]``、``x / 2`` 里的数字是结构性
    的，把它们也禁掉只会让规则变得不可遵守，然后被人整体忽略。
    """
    offenders: list[str] = []
    for p, tree in _trees(repo_root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and isinstance(side.value, (int, float)):
                    if side.value not in STRUCTURAL_NUMBERS:
                        offenders.append(
                            f"{p}:{node.lineno} 与字面量 {side.value} 比较"
                        )
    assert not offenders, (
        "发现硬编码阈值比较；它们应当来自 config/routing.policy.yaml 或 "
        f"config/evolution/detectors.yaml：{offenders}"
    )


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有文档字符串节点的 id，供检查排除。

    文档字符串里出现供应商名是**应当的**——那通常正是在说明"不要这么写"的反例
    （``errors.py`` 就引用了 ``llm_client.py`` 那个字符串哨兵）。把文档也禁掉，
    检查就会禁止它自己解释自己为什么存在。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def test_no_provider_or_model_names_in_core(repo_root: Path):
    """可执行代码里不得出现供应商名或型号串（文档字符串除外）。"""
    offenders: list[str] = []
    for p, tree in _trees(repo_root):
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            if PROVIDER_PATTERN.search(node.value):
                offenders.append(f"{p}:{node.lineno} 供应商名 {node.value[:40]!r}")
            elif MODEL_ID_PATTERN.search(node.value):
                offenders.append(f"{p}:{node.lineno} 疑似型号 {node.value[:40]!r}")
    assert not offenders, f"编排层出现模型/供应商标识：{offenders}"


def test_no_domain_predicates_in_core(repo_root: Path):
    """守卫与注册表之外，不得出现按领域字段分支的比较。

    这是"加一个领域概念不会加一个分支"的静态验证。行为层面的证明见
    ``test_guard.py::test_output_is_invariant_under_task_semantics``；
    两者互补：行为测试证明结果不随语义变化，静态检查证明代码里没有那样的分支。
    """
    offenders: list[str] = []
    for p, tree in _trees(repo_root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            names: list[str] = []
            if isinstance(node.left, ast.Name):
                names.append(node.left.id)
            elif isinstance(node.left, ast.Attribute):
                names.append(node.left.attr)
            for comp in node.comparators:
                if isinstance(comp, ast.Name):
                    names.append(comp.id)
                elif isinstance(comp, ast.Attribute):
                    names.append(comp.attr)
            if any(n in DOMAIN_FIELDS for n in names):
                # 与字符串字面量比较才构成领域谓词；与变量比较是集合操作
                if any(
                    isinstance(s, ast.Constant) and isinstance(s.value, str)
                    for s in [node.left, *node.comparators]
                ):
                    offenders.append(f"{p}:{node.lineno} 按 {names} 做字符串比较")
    assert not offenders, (
        f"发现领域谓词；判定应当交给 LLM，合法性校验应当是集合成员判定：{offenders}"
    )


# ---------------------------------------------------------------------------
# handler 声明
# ---------------------------------------------------------------------------
def test_handler_manifests_name_no_models(repo_root: Path):
    """示例与真实 handler 的声明里不得出现模型名——能力名替代模型名是接缝的核心。"""
    offenders: list[str] = []
    for p in sorted((repo_root / "examples" / "handlers").glob("*.yaml")):
        text = p.read_text(encoding="utf-8")
        # 去掉注释行再查，注释里提到"不写模型名"是正常的
        body = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        for m in PROVIDER_PATTERN.finditer(body):
            offenders.append(f"{p.name}: {m.group(0)}")
    assert not offenders, f"handler 声明里出现模型名：{offenders}"


def _all_manifests(repo_root: Path):
    """从**配置**里读全部 handler 声明（而不是硬编码领域名）。

    这个测试文件本身也不该知道有哪些领域——它检查的是"所有已注册 handler 的
    声明是否守规矩"，那就应该问配置要清单。
    """
    out = []
    for spec in load_specs(repo_root / "config" / "handlers.yaml"):
        out.extend(load_manifests(spec.manifest_path.parent))
    return out


def test_tools_needing_strong_reasoning_do_not_name_a_tier(repo_root: Path):
    """强度需求用能力名（reasoning.strong）表达，不用档位名（strong）。

    如果工具直接写 ``strong``，那么重排或重命名档位就会破坏 handler——
    而档位划分正是应当能自由变化的东西。
    """

    offenders: list[str] = []
    for m in _all_manifests(repo_root):
        for t in m.tools:
            for cap in t.requires_capabilities:
                if cap in {"cheap", "standard", "strong"}:
                    offenders.append(f"{m.handler_id}.{t.name}: {cap}")
    assert not offenders, f"工具直接引用了档位名而非能力名：{offenders}"


def test_empty_capability_requirement_means_no_model(repo_root: Path):
    """声明 ``requires_capabilities: []`` 的工具，档位解析必须给出 None。

    这条是有约束力的：它意味着调度层不会给纯算术步骤配模型。
    """
    from dispatcher.core.policy import load_policy

    policy = load_policy(repo_root / "config" / "routing.policy.yaml")
    for m in _all_manifests(repo_root):
        for t in m.tools:
            if not t.requires_capabilities:
                assert policy.resolve_tier([], list(policy.model_tier_ids)) is None


# ---------------------------------------------------------------------------
# 契约与配置的对应
# ---------------------------------------------------------------------------
def test_every_route_is_reachable_by_some_tier(policy, registry):
    """每条需要 handler 的路由，至少要有一个档位能满足至少一个工具的声明。

    否则那条路由是个死条目：选中它之后没有任何可执行的动作。
    """
    for route in policy.routes:
        if not route.requires_handler:
            continue
        satisfiable = False
        for hid in registry.ids:
            for name in registry.tool_names(hid):
                decl = next(t for h, t in registry.all_tool_decls() if h == hid and t.name == name)
                if policy.resolve_tier(decl.requires_capabilities, route.allowed_tiers):
                    satisfiable = True
                    break
            if satisfiable:
                break
        assert satisfiable, f"路由 {route.id} 下没有任何可执行的工具（档位能力不足）"


def test_no_orphan_config_keys(repo_root: Path):
    """代码引用但配置里不存在的键 —— 拼写错误或漏了默认值。

    反方向（配置里存在但从不被引用的键）在这里只对**策略的顶层段落**做检查，
    因为深层的旋钮会有意留作将来使用，把它们全算成死配置会产生大量噪音，
    而噪音会让这条检查失去意义。
    """
    from dispatcher.core.policy import Policy
    from dispatcher.core.yamlio import load_yaml_text

    raw = load_yaml_text((repo_root / "config" / "routing.policy.yaml").read_text(encoding="utf-8"))
    declared = set(raw)
    expected = set(Policy.model_fields)
    missing = expected - declared
    assert not missing, f"策略缺少实现期望的段落：{sorted(missing)}"
    assert not (declared - expected), f"策略里有实现从不读取的段落：{sorted(declared - expected)}"


# ---------------------------------------------------------------------------
# 死代码不得冒充防线
# ---------------------------------------------------------------------------
def test_the_dead_guard_system_noop_is_not_reintroduced(repo_root: Path):
    """``prompts.guard_system`` 是恒等函数、全仓零调用点，已删。

    它危险的地方不是"没用"而是**名字**：``docs/`` 里三处把它写成"系统提示词槽位由
    ``guard_system()`` 独占"，读文档的人会以为 system 槽有一道运行时守卫。它做不到——
    参数是**渲染完的字符串**，函数无从知道这段文本来自 ``prompts/`` 下的文件还是
    调用方现拼的，所以它只能 ``return prompt``。一个恒等的"守卫"比没有更糟：它会让
    后来的人以为这条已经有人管了。

    ``ai-workmate`` 里那个同名函数做的是另一件事（拼产品身份 + 防注入前言的**变换**），
    本仓没有对应的需求——反注入条款写在各提示词文件自己头上。

    真正的防线在别处，而且是**可失败**的：``fill`` 的严格性，加上
    ``tests/test_injection_slots.py`` 逐站点钉住的分槽断言（含"任何 system 槽都不得
    带数据围栏"）。这条测试钉住的是"别把一个恒等函数搬回来充数"。

    用 AST 而不是 grep：本文件的说明文字里就写着这个名字，而注释与文档字符串不算
    引用（与上面几条同一条纪律）。
    """
    offenders: list[str] = []
    roots = [repo_root / "dispatcher", repo_root / "handlers"]
    for root in roots:
        for p in sorted(root.rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "guard_system":
                    offenders.append(f"{p}:{node.lineno} 引用了 guard_system")
                elif isinstance(node, ast.Attribute) and node.attr == "guard_system":
                    offenders.append(f"{p}:{node.lineno} 引用了 guard_system")
                elif (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == "guard_system"
                ):
                    offenders.append(f"{p}:{node.lineno} 又定义了 guard_system")
    assert not offenders, (
        "guard_system 又出现了。它是恒等函数，提供不了它名字暗示的那道守卫——"
        f"要守 system 槽就写在 test_injection_slots.py 的分槽测试里：{offenders}"
    )
