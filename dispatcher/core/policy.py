"""策略加载与校验。

``config/routing.policy.yaml`` 里 ``routes[].when`` 那段散文**就是决策表**——
读它的是 LLM。本模块只负责把它读进来、校验它的自洽性、并暴露给守卫与提示词渲染。

两件必须坚持的事：

1. **``extra="forbid"``。** 策略文件里写错一个键名会立刻报错，而不是被静默忽略。
   静默忽略意味着"我改了配置但行为没变"，而那是最难查的一类问题。
2. **这里的校验只有集合成员判定，没有领域谓词。** ``default_tier`` 必须在
   ``model_tiers`` 里、``allowed_tiers`` 必须是它的子集、``fallback.route_id``
   必须是一条真实路由——全是 ``in`` / ``issubset``。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from .errors import DispatcherError
from .yamlio import load_yaml

# 执行路径是**结构性**词表：实现里对应三种执行器，改它等于改契约。
# 因此在这里（以及 schemas/route_decision.json）封闭。路由 id 与档位名则相反，
# 它们是配置性词表，不封闭——见 docs/08-config-model.md。
ExecPath = Literal["direct_llm", "single_step_tool", "decompose"]
ExecMode = Literal["sync", "async"]
StrictModel = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 模型档位
# ---------------------------------------------------------------------------
class ModelTier(BaseModel):
    model_config = StrictModel
    provider: str
    model_ref: str  # secret://llm/<tier>_model
    capabilities: list[str]
    context_window: int | None = None
    price_per_1k: dict[str, float] | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class Route(BaseModel):
    model_config = StrictModel

    id: str
    when: str  # 给 LLM 读的适用条件。这是决策表的核心内容。
    path: ExecPath
    default_tier: str
    allowed_tiers: list[str]
    requires_handler: bool
    evaluate: str = "always"
    max_cost: float
    mode: ExecMode

    # 以下均为按路由可选的细化项
    vision_tier: str | None = None
    tool_policy: str | None = None
    flow_template_hint: str | None = None
    requires_confirmation_below_confidence: float | None = None


class Thresholds(BaseModel):
    model_config = StrictModel
    complexity_band_cutoffs: dict[str, float]
    complexity_band_tier_map: dict[str, str]
    clarify_below: float
    escalate_below_confidence: float
    escalation_tiers: dict[str, str]
    max_escalations: int
    profile_cache_similarity_min: float
    profile_cache_ttl_s: int


class Budget(BaseModel):
    model_config = StrictModel
    enforcement: dict[str, Any]  # {"mode": "advisory"|"hard", "applies_to": [...]}
    per_task_default: float
    per_user_daily: float
    per_tenant_daily: float
    warn_at_ratio: float
    currency: str


class Fallback(BaseModel):
    model_config = StrictModel
    route_id: str
    tier: str
    on: list[str]
    profile_id: str


class EvaluatorConfig(BaseModel):
    model_config = StrictModel
    tier: str
    timeout_ms: int
    max_repair_attempts: int
    requires: list[str]
    escalation: dict[str, Any]
    cache: dict[str, Any]
    # 供应商参数透传：代码不认识这些键，只把它们放进请求体。
    # 于是"某供应商有个开关"留在配置里，不渗进代码。
    options: dict[str, Any] = {}


class RouterConfig(BaseModel):
    model_config = StrictModel
    tier: str
    timeout_ms: int
    max_repair_attempts: int
    temperature: float
    persist_rationale: bool
    options: dict[str, Any] = {}


class DirectLLMConfig(BaseModel):
    """直答（``path=direct_llm``）自己的超时与供应商参数。

    **档位不在这里**：它来自路由决策（``direct_answer`` 的 ``default_tier``）。
    "用哪个档位"是路由要选的东西，直答不该有主张——与 handler 拿不到模型名是
    同一条纪律。这里只有"这一次补全怎么发出去"。
    """

    model_config = StrictModel
    timeout_ms: int
    requires: list[str]
    options: dict[str, Any] = {}


class DecomposerConfig(BaseModel):
    model_config = StrictModel
    tier: str
    max_replans: int
    agents: dict[str, Any]
    parallel: dict[str, Any]
    node_defaults: dict[str, Any]
    join_policy: str
    options: dict[str, Any] = {}


class MediaLimits(BaseModel):
    model_config = StrictModel
    allowed_mime: list[str]
    max_bytes: int
    inline_max_bytes: int


class Limits(BaseModel):
    model_config = StrictModel
    sync_timeout_ms: int
    default_task_wall_ms: int
    max_parallelism: int
    max_replans: int
    max_escalations: int
    max_agent_rounds: int
    max_reviewers: int
    max_total_rounds_per_task: int
    max_agent_nodes_per_plan: int
    media: MediaLimits
    sse_heartbeat_ms: int
    memory_events: int


class EvolutionConfig(BaseModel):
    model_config = StrictModel
    enabled: bool
    analysis_interval: str
    analysis_window: str
    min_sample_size: int
    max_delta_ratio: float
    max_suggestions_per_pass: int
    analysis_tier: str
    sample_limit: int
    retention: dict[str, Any]
    canary: dict[str, Any]
    locked_prompts: list[str]


# ---------------------------------------------------------------------------
# 策略本体
# ---------------------------------------------------------------------------
class Policy(BaseModel):
    model_config = StrictModel

    policy_version: str
    schema_version: str
    locked_paths: list[str]
    model_tiers: dict[str, ModelTier]
    routes: list[Route]
    thresholds: Thresholds
    budget: Budget
    fallback: Fallback
    evaluator: EvaluatorConfig
    router: RouterConfig
    direct_llm: DirectLLMConfig
    decomposer: DecomposerConfig
    limits: Limits
    evolution: EvolutionConfig

    # 以下是校验后的派生索引，由 model_validator 填充
    _routes_by_id: dict[str, Route] = {}
    _locked_tiers: frozenset[str] = frozenset()

    # -- 自洽性校验：全部是集合成员判定，无语义 ---------------
    @model_validator(mode="after")
    def _validate_self_consistency(self) -> Policy:
        tiers = set(self.model_tiers)
        if not tiers:
            raise ValueError("model_tiers 为空：至少需要一个档位")

        route_ids = [r.id for r in self.routes]
        if len(route_ids) != len(set(route_ids)):
            dupes = sorted({i for i in route_ids if route_ids.count(i) > 1})
            raise ValueError(f"路由 id 重复：{dupes}")
        route_id_set = set(route_ids)

        for r in self.routes:
            if r.default_tier not in tiers:
                raise ValueError(f"路由 {r.id} 的 default_tier={r.default_tier!r} 不是已定义的档位")
            unknown = [t for t in r.allowed_tiers if t not in tiers]
            if unknown:
                raise ValueError(f"路由 {r.id} 的 allowed_tiers 含未定义档位：{unknown}")
            if r.default_tier not in r.allowed_tiers:
                raise ValueError(f"路由 {r.id} 的 default_tier 不在其 allowed_tiers 内")
            if r.vision_tier is not None and r.vision_tier not in tiers:
                raise ValueError(f"路由 {r.id} 的 vision_tier={r.vision_tier!r} 不是已定义的档位")

        if self.fallback.route_id not in route_id_set:
            raise ValueError(f"fallback.route_id={self.fallback.route_id!r} 不是一条真实路由")
        if self.fallback.tier not in tiers:
            raise ValueError(f"fallback.tier={self.fallback.tier!r} 不是已定义的档位")

        # 评估器/路由器用的档位也必须是真实档位
        for name, tier in (("evaluator", self.evaluator.tier), ("router", self.router.tier),
                           ("decomposer", self.decomposer.tier)):
            if tier not in tiers:
                raise ValueError(f"{name}.tier={tier!r} 不是已定义的档位")

        # 升级目标必须是真实档位（否则升级会升到一个不存在的地方）
        for src, dst in self.thresholds.escalation_tiers.items():
            if src not in tiers or dst not in tiers:
                raise ValueError(f"escalation_tiers 含未定义档位：{src} -> {dst}")

        # 直答的能力需求必须至少有一个档位能满足。否则 direct_answer 路由会**永远
        # 发不出去**——那是静态可判定的，不该等到第一次真跑才撞上（与
        # config/agents.yaml 里角色的能力可满足性检查同一条理由）。
        if not self.resolve_tier(self.direct_llm.requires, list(tiers)):
            raise ValueError(
                f"direct_llm.requires={self.direct_llm.requires} 没有任何档位能满足"
            )

        object.__setattr__(self, "_routes_by_id", {r.id: r for r in self.routes})
        object.__setattr__(self, "_locked_tiers", frozenset(tiers))
        return self

    # -- 索引与查询（全部无语义）-------------------------------
    @property
    def route_ids(self) -> frozenset[str]:
        return frozenset(self._routes_by_id)

    @property
    def model_tier_ids(self) -> tuple[str, ...]:
        """按策略文件里的声明顺序返回档位。

        顺序有意义：``mean_tier_index`` 检测器用它把档位名映射成序数，
        从而发现"档位通胀"。顺序来自 YAML 的书写顺序，不来自代码里的常量。
        """
        return tuple(self.model_tiers)

    @property
    def tier_order(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(self.model_tier_ids)}

    def route(self, route_id: str) -> Route | None:
        return self._routes_by_id.get(route_id)

    def has_tier(self, tier: str) -> bool:
        return tier in self._locked_tiers

    def escalation_target(self, tier: str) -> str:
        return self.thresholds.escalation_tiers.get(tier, tier)

    def resolve_tier(self, requires: list[str] | tuple[str, ...], allowed_tiers: list[str]) -> str | None:
        """在 ``allowed_tiers`` 中挑一个能力足以覆盖 ``requires`` 的档位。

        这是**纯子集测试**：档位的能力集合 ⊇ 需求集合。没有映射表，没有语义判断，
        也不需要知道 ``vision.extract`` 和 ``reasoning.strong`` 分别意味着什么。

        顺序按策略文件里的书写顺序——**档位在 YAML 里的先后即由便宜到贵**，
        于是"取第一个满足的"就是"取最便宜的那个满足的"。这个约定是数据，
        不是代码里的常量；重排 YAML 就改变了成本策略。

        ``requires`` 为空表示这一步不需要模型（例如纯算术工具），
        此时返回 ``None``——调用方据此完全不配模型。
        """
        need = frozenset(requires)
        if not need:
            return None
        for name in self.model_tier_ids:
            if name not in allowed_tiers:
                continue
            if need <= frozenset(self.model_tiers[name].capabilities):
                return name
        return None

    def satisfies(self, tier: str, requires: list[str] | tuple[str, ...]) -> bool:
        t = self.model_tiers.get(tier)
        return bool(t) and frozenset(requires) <= frozenset(t.capabilities)

    # -- 自进化禁区 -------------------------------------------
    def is_locked(self, path: str) -> bool:
        """``locked_paths`` 用 fnmatch 通配，例如 ``limits.*``。"""
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.locked_paths)

    @property
    def enforcement_mode(self) -> str:
        return str(self.budget.enforcement.get("mode", "advisory"))


# ---------------------------------------------------------------------------
def load_policy(path: Path) -> Policy:
    """从 YAML 加载并校验。任何问题都抛 policy_violation 而不是裸异常——
    策略坏了是配置错误，要有明确的错误码。"""
    if not path.exists():
        raise DispatcherError("policy_violation", f"策略文件不存在：{path}")
    try:
        raw = load_yaml(path)
    except yaml.YAMLError as e:
        raise DispatcherError("policy_violation", f"策略文件 YAML 解析失败：{e}") from e
    if not isinstance(raw, dict):
        raise DispatcherError("policy_violation", f"策略文件顶层必须是映射：{path}")
    try:
        return Policy.model_validate(raw)
    except Exception as e:
        raise DispatcherError("policy_violation", f"策略自洽性校验失败：{e}") from e


__all__ = ["ExecMode", "ExecPath", "Policy", "Route", "load_policy"]


# ---------------------------------------------------------------------------
# 策略内的路径定位与改写
#
# 自进化的建议用 ``routes[direct_answer].max_cost`` 这样的路径指向要改的地方。
# 两个用途：**校验**（路径存不存在、是不是禁区、类型对不对）与**应用**（把值写进去）。
#
# 这套路径语法是**封闭**的：只支持 ``routes[<id>].<field>``、``thresholds.<key>``、
# ``budget.<key>``、``model_tiers.<tier>.<field>`` 这几种形状。不支持的写法
# 在校验期就拒绝——评审一条建议的人需要能一眼看懂它改的是哪里，
# 一个支持任意 JSON Pointer 的实现会让"改动落在哪"变成一个要推理的问题。
# ---------------------------------------------------------------------------
_PATH_RE = re.compile(r"^(?P<head>[a-z_]+)(?:\[(?P<key>[^\]]+)\])?(?:\.(?P<tail>[a-z_][\w\.]*))?$")
_INDEXED = {"routes": "id", "model_tiers": None}


def resolve_policy_path(policy_dict: dict, path: str):
    """返回 ``(是否存在, 当前值)``。不抛错——调用方要能把"路径不存在"当成一条校验结论。"""
    m = _PATH_RE.match(path.strip())
    if not m:
        return False, None
    head = m["head"]
    key = m["key"]
    tail = m["tail"]
    node = policy_dict.get(head)
    if node is None:
        return False, None
    if key is not None:
        if isinstance(node, list):
            found = next((x for x in node if str(x.get("id")) == key), None)
            if found is None:
                return False, None
            node = found
        elif isinstance(node, dict):
            if key not in node:
                return False, None
            node = node[key]
        else:
            return False, None
    if not tail:
        return True, node
    for part in tail.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def set_policy_path(policy_dict: dict, path: str, value) -> bool:
    """就地写入。返回是否成功。路径不存在时**不改动任何东西**。"""
    m = _PATH_RE.match(path.strip())
    if not m:
        return False
    head, key, tail = m["head"], m["key"], m["tail"]
    node = policy_dict.get(head)
    if node is None:
        return False
    if key is not None:
        if isinstance(node, list):
            found = next((x for x in node if str(x.get("id")) == key), None)
            if found is None:
                return False
            node = found
        elif isinstance(node, dict):
            if key not in node:
                return False
            node = node[key]
        else:
            return False
    if not tail:
        policy_dict[head] = value
        return True
    parts = tail.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    node[parts[-1]] = value
    return True
