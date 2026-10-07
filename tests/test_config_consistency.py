"""配置之间与配置对契约的交叉一致性。

这些检查抓的是"配置里写了一处、另一处忘了同步"——那类问题不会让程序崩，
只会让系统安静地做错事，因此必须在 CI 里挡住。

用的都是真实文件。任何一个 handler 角色提示词被改名、任何一个检测器引用了
``run_log.json`` 里不存在的字段，这里都会红。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatcher.core.policy import Policy
from dispatcher.core.yamlio import load_yaml


@pytest.fixture(scope="module")
def agents(repo_root: Path) -> dict:
    return load_yaml(repo_root / "config" / "agents.yaml")


@pytest.fixture(scope="module")
def detectors(repo_root: Path) -> dict:
    return load_yaml(repo_root / "config" / "evolution" / "detectors.yaml")


@pytest.fixture(scope="module")
def suggestion_kinds(repo_root: Path) -> dict:
    return load_yaml(repo_root / "config" / "evolution" / "suggestion_kinds.yaml")


# ---------------------------------------------------------------------------
# agents.yaml
# ---------------------------------------------------------------------------
def test_every_role_prompt_file_exists(repo_root: Path, agents: dict):
    """每个角色的 system_prompt_ref 必须解析到真实文件。

    一条指向不存在文件的引用会在这里就红，而不是等到 M3 第一次真跑某个角色时
    才发现——那时错误会出现在运行期，远离改动发生的地方。
    """
    missing = [
        r["system_prompt_ref"] for r in agents["roles"]
        if not (repo_root / r["system_prompt_ref"]).exists()
    ]
    assert not missing, f"角色提示词文件缺失：{missing}"


def test_role_rounds_within_hard_bounds(agents: dict, policy: Policy):
    """角色的 max_rounds 不得超过 limits 里的硬界。"""
    ceiling = policy.limits.max_agent_rounds
    for r in agents["roles"]:
        assert r["max_rounds"] <= ceiling, (
            f"角色 {r['id']} 的 max_rounds={r['max_rounds']} 超过 limits.max_agent_rounds={ceiling}"
        )


def test_verification_defaults_within_bounds(agents: dict, policy: Policy):
    for v in agents["verification_defaults"]:
        if v.get("reviewers"):
            assert v["reviewers"] <= policy.limits.max_reviewers


def test_hard_bounds_match_limits(agents: dict, policy: Policy):
    """agents.yaml 的 hard_bounds 与 routing.policy.yaml 的 limits 必须一致。

    两处都声明同一组上限，是为了让"读哪一份文件的人都能看到界"——
    不一致时取更小者会让行为取决于读者，那就失去了意义。
    """
    hb = agents["hard_bounds"]
    assert hb["max_rounds_ceiling"] == policy.limits.max_agent_rounds
    assert hb["max_reviewers_ceiling"] == policy.limits.max_reviewers
    assert hb["max_total_rounds_per_task"] == policy.limits.max_total_rounds_per_task


def test_roles_never_name_a_tier_as_capability(agents: dict):
    """角色的 requires 是能力名，不是档位名。"""
    for r in agents["roles"]:
        for cap in r["requires"]:
            assert cap not in {"cheap", "standard", "strong"}, f"角色 {r['id']} 直接引用档位名"


def test_role_default_tiers_are_defined(agents: dict, policy: Policy):
    for r in agents["roles"]:
        assert r["default_tier"] in policy.model_tier_ids, f"角色 {r['id']} 的档位未定义"


def test_role_required_capabilities_are_satisfiable(agents: dict, policy: Policy):
    """每个角色声明的能力需求，至少要有一个档位能满足——否则这个角色永远不可用。"""
    for r in agents["roles"]:
        assert policy.resolve_tier(r["requires"], list(policy.model_tier_ids)), (
            f"角色 {r['id']} 的能力需求 {r['requires']} 没有任何档位能满足"
        )


# ---------------------------------------------------------------------------
# detectors.yaml ↔ run_log.json
# ---------------------------------------------------------------------------
def _resolve_runlog_path(schema: dict, path: str) -> bool:
    """在 run_log.json 里按点分路径走一遍，数组用 ``[]`` 表示。"""
    node = schema
    for part in path.split("."):
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        if node.get("type") == "array":
            node = node.get("items", {})
        props = node.get("properties", {})
        if name not in props:
            return False
        node = props[name]
        if is_array and "items" in node:
            node = node["items"]
        elif node.get("type") == "array":
            node = node.get("items", {})
    return True


def test_detector_source_fields_exist_in_run_log(repo_root: Path, detectors: dict):
    """每个检测器引用的字段都必须真实存在于 run_log.json。

    否则那个检测器算的是一个永远为空的指标——它会静静地永不触发，
    而人以为它在监控。
    """
    run_log = json.loads((repo_root / "schemas" / "run_log.json").read_text(encoding="utf-8"))
    unresolved: list[str] = []
    for d in detectors["detectors"]:
        sf = d["source_field"]
        if sf == "internal":
            # 自进化机制自身的健康度，不来自 run log
            continue
        if not _resolve_runlog_path(run_log, sf):
            unresolved.append(f"{d['id']} -> {sf}")
    assert not unresolved, f"检测器引用了 run_log.json 里不存在的字段：{unresolved}"


def test_detector_thresholds_are_numbers_not_strings(detectors: dict):
    """阈值必须是数字。写成字符串会让比较静默失败（或抛在运行期）。"""
    for d in detectors["detectors"]:
        assert isinstance(d["threshold"], (int, float)), f"{d['id']} 的阈值不是数字"
        assert d["min_sample"] >= 1, f"{d['id']} 的 min_sample 非法"


def test_detectors_only_suggest_known_kinds(repo_root: Path, detectors: dict, suggestion_kinds: dict):
    """检测器建议的类型必须在封闭集里。

    否则分析环节会尝试产出一条契约里不存在的建议类型。
    """
    known = {k["id"] for k in suggestion_kinds["kinds"]}
    for d in detectors["detectors"]:
        for kind in d.get("suggests", []):
            assert kind in known, f"检测器 {d['id']} 建议了未知类型 {kind}"


# ---------------------------------------------------------------------------
# suggestion_kinds.yaml ↔ routing.policy.yaml 的禁区
# ---------------------------------------------------------------------------
def test_suggestion_kinds_never_target_code(suggestion_kinds: dict):
    """建议类型的目标产物只能是配置与提示词。"""
    forbidden_suffixes = (".py", ".ts", ".dart", ".js")
    for k in suggestion_kinds["kinds"]:
        targets = k["target_artifact"]
        targets = [targets] if isinstance(targets, str) else targets
        for t in targets:
            assert not t.endswith(forbidden_suffixes), f"{k['id']} 的产物指向代码：{t}"


def test_forbidden_artifacts_cover_the_locked_areas(repo_root: Path, suggestion_kinds: dict, policy: Policy):
    """``forbidden_target_artifacts`` 必须覆盖 ``locked_paths`` 覆盖的东西。

    两处的作用不同：``locked_paths`` 由守卫在运行时强制，``forbidden_target_artifacts``
    是给分析提示词与人工审查看的。两者不重叠的部分就是漏洞。
    """
    forbidden = "\n".join(suggestion_kinds["forbidden_target_artifacts"])
    for area in ("pricing.yaml", "limits.", "fallback", "evolution.", "agents."):
        assert area in forbidden, f"forbidden_target_artifacts 未覆盖 {area}"

    for must_lock in ("pricing.", "limits.", "fallback", "evolution.", "agents."):
        assert any(p.startswith(must_lock) for p in policy.locked_paths), (
            f"locked_paths 未覆盖 {must_lock}"
        )


# ---------------------------------------------------------------------------
# flow_templates
# ---------------------------------------------------------------------------
def test_flow_template_node_fields_match_executor(repo_root: Path, registry):
    """``executor: tool`` 必须给 ``tool`` 且不给 ``role``；``executor: agent`` 反之。

    这是 ``schemas/execution_plan.json`` 里那条 allOf 的 YAML 版前置检查——
    计划在生成时会被 schema 挡住，但模板本身是人手写的，值得在 CI 里也挡一次。
    """
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        for node in tpl["nodes"]:
            ex = node.get("executor")
            assert ex in {"tool", "agent"}, f"{p.name} 节点 {node['id']} 的 executor 非法"
            if ex == "tool":
                assert "tool" in node and "role" not in node, f"{p.name}/{node['id']}"
            else:
                assert "role" in node and "tool" not in node, f"{p.name}/{node['id']}"


def test_flow_template_tools_exist_in_handler(repo_root: Path, registry):
    """模板引用的工具必须由该 handler 真实声明。"""
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        hid = tpl["handler"]
        declared = registry.tool_names(hid)
        assert declared, f"{p.name} 指向的 handler {hid} 没有声明任何工具"
        used: list[str] = []
        for node in tpl["nodes"]:
            if node.get("executor") == "tool":
                used.append(node["tool"])
            used.extend(node.get("tool_whitelist") or [])
        unknown = sorted(set(used) - declared)
        assert not unknown, f"{p.name} 引用了 {hid} 未声明的工具：{unknown}"


def test_flow_template_roles_exist(repo_root: Path, registry):
    import yaml as _y

    agents = load_yaml(repo_root / "config" / "agents.yaml")
    known = {r["id"] for r in agents["roles"]}
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        for node in tpl["nodes"]:
            if node.get("executor") == "agent":
                assert node["role"] in known, f"{p.name}/{node['id']} 引用了未知角色 {node['role']}"
    assert _y  # 保持导入被使用


def test_flow_template_roles_tool_whitelist_is_subset_of_role(repo_root: Path):
    """节点的 tool_whitelist 必须是角色 allowed_tools 的子集（两级白名单的第一级）。"""
    agents = load_yaml(repo_root / "config" / "agents.yaml")
    by_id = {r["id"]: set(r["allowed_tools"]) for r in agents["roles"]}
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        for node in tpl["nodes"]:
            if node.get("executor") != "agent":
                continue
            wl = set(node.get("tool_whitelist") or [])
            extra = wl - by_id.get(node["role"], set())
            assert not extra, f"{p.name}/{node['id']} 的工具超出角色 {node['role']} 的允许集：{extra}"


def test_flow_template_max_parallelism_within_limits(repo_root: Path, policy: Policy):
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        assert tpl["max_parallelism"] <= policy.limits.max_parallelism, p.name


#: 模板里允许出现的、**不是档位名**的哨兵。每个哨兵都必须有代码在解析它
#: （当前只有 ``Decomposer._declared_tier`` 认 ``vision`` → 决策里的 vision_tier）。
_TIER_SENTINELS = frozenset({"vision"})


def test_flow_template_tiers_are_real_tiers_or_known_sentinels(repo_root: Path, policy: Policy):
    """模板节点写的 ``tier`` 必须是真实档位名，或者是**有代码在解析**的哨兵。

    这是 2026-10-07 extract 故障的防线。``tier: vision`` 读起来像个档位名，
    而策略里根本没有叫 ``vision`` 的档位：它被原样塞进 ``Node.model_tier``，
    而 runner 的升档起点正是这个字段——起点报错，升档目标就落回节点当前正在用的
    档位，"升档重试"于是变成原地重跑。拼错一个真实档位名（``standrad``）同样会静默
    失效，所以这里一并挡掉。

    ``None`` 是合法的：它表示"不指定档位，由 requires 解析"。
    """
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        for node in tpl["nodes"]:
            tier = node.get("tier")
            if tier is None:
                continue
            assert tier in policy.model_tier_ids or tier in _TIER_SENTINELS, (
                f"{p.name}/{node['id']} 的 tier={tier!r} 既不是已定义的档位 "
                f"{tuple(policy.model_tier_ids)}，也不是认得的哨兵 {sorted(_TIER_SENTINELS)}"
                f"——它会被 Decomposer 当成 None 丢掉，而丢掉之后升档就没法从正确的"
                f"档位起算"
            )


def test_route_flow_template_hint_resolves_to_a_real_template(repo_root: Path, policy: Policy):
    """路由的 ``flow_template_hint`` 必须指向一个真实存在的模板文件。

    这是"契约与配置各写一份就会分叉"的一处现场：hint 写在 routing.policy.yaml，
    模板写在 flow_templates/*.yaml，两边没有任何代码把对应关系钉住。
    拼错一个 id **不会报错**——``Decomposer._matching_template`` 只会在模板表里
    查不到，于是静默退化成自由拆解，那张模板等于不存在。静默失效比报错更糟，
    因此在 CI 里挡一次。
    """
    templates = {
        load_yaml(p)["template_id"]
        for p in (repo_root / "config" / "flow_templates").glob("*.yaml")
    }
    missing = sorted(
        f"{r.id} -> {r.flow_template_hint}"
        for r in policy.routes
        if r.flow_template_hint and r.flow_template_hint not in templates
    )
    assert not missing, f"路由的 flow_template_hint 指向不存在的模板：{missing}"


def test_every_flow_template_is_declared_in_its_handler_flows(repo_root: Path, registry):
    """每个模板都要在它 handler 的 manifest ``flows`` 里登记。

    handler 的 ``flows`` 是"这个领域随附了哪些模板"的声明。模板文件存在、
    handler 却没登记，说明两处已经不同步——反方向不做检查，因为 ``flows``
    可以声明一个尚未落地的模板（例如 bookkeeping 的 monthly_reconciliation），
    那是有意的占位而不是错误。
    """
    for p in sorted((repo_root / "config" / "flow_templates").glob("*.yaml")):
        tpl = load_yaml(p)
        manifest = registry.manifest(tpl["handler"])
        assert manifest is not None, f"{p.name} 指向未注册的 handler {tpl['handler']!r}"
        assert tpl["template_id"] in manifest.flows, (
            f"{p.name} 的模板 {tpl['template_id']} 未在 handler "
            f"{tpl['handler']} 的 flows 中登记"
        )


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------
def test_taxonomy_domains_have_or_knowingly_lack_handlers(taxonomy, registry):
    """词表里的 domain 应当是已注册的 handler，唯一例外是通用兜底域。

    新加一类意图却忘了接 handler，会让评估器建议一个没人能满足的能力——
    这类问题值得在 CI 里挡，因为它表现为运行期的 no_capability_match，
    而根因在很早以前的配置改动。
    """
    generic = {"chat", "generic", "document"}
    unknown = sorted(
        d for d in taxonomy.domains
        if d not in registry.ids and d not in generic
    )
    assert not unknown, f"词表里的 domain 没有对应 handler：{unknown}"


def test_capability_names_use_dotted_namespace(registry):
    """能力名统一用 ``<领域>.<动作>``，与档位能力（如 vision.extract）同构。

    统一的命名让"档位能否满足需求"是一次纯子集测试——不需要映射表。
    """
    for cap in registry.all_capabilities():
        assert "." in cap, f"能力名 {cap} 缺少点分命名空间"
        assert cap == cap.lower(), f"能力名 {cap} 应为小写"
