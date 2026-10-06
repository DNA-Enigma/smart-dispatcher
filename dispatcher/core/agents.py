"""Agent 角色表。

角色是**配置**，不是代码。这张表回答的是"这个位置由谁来做"，
**不回答"它后面接谁"**——拓扑由拆解器动态生成。

这与 ``duowei-ai`` 现有做法是关键区别：那边 9 个 Agent 的拓扑写死在
``core/graph.py``（``N1→N2→batch→batch2``），职责与工具用 ``GROUP_TEMPLATES``
硬编码。于是"加一个 Agent"要改代码；这里加一个角色只是加一段 YAML
与一个提示词文件。

角色声明里**永不出现模型名**，只出现能力需求（``requires``）——
角色说"我需要判断力"，档位由调度层映射。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .errors import DispatcherError
from .yamlio import load_yaml

Strict = ConfigDict(extra="forbid")


class AgentRole(BaseModel):
    model_config = Strict

    id: str
    when: str
    system_prompt_ref: str
    allowed_tools: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    default_tier: str
    max_rounds: int = Field(ge=1)
    output_must_satisfy_schema: bool = True
    on_round_limit: str | None = None


class VerificationDefault(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    applies_to_task_types: list[str] = Field(default_factory=list)
    mode: str
    reviewers: int | None = None
    reviewer_role: str | None = None
    arbiter_role: str | None = None
    on_disagreement: str | None = None
    max_cost_multiplier: float | None = None
    note: str | None = None


class AgentSpec(BaseModel):
    model_config = Strict

    agents_version: str
    collaboration_principles: str = ""
    roles: list[AgentRole]
    verification_defaults: list[VerificationDefault] = Field(default_factory=list)
    hard_bounds: dict[str, int] = Field(default_factory=dict)

    # -- 索引（全部是集合操作，无语义）----------------------------------
    @property
    def role_ids(self) -> frozenset[str]:
        return frozenset(r.id for r in self.roles)

    def role(self, role_id: str) -> AgentRole | None:
        return next((r for r in self.roles if r.id == role_id), None)

    def allowed_tools(self, role_id: str) -> frozenset[str]:
        r = self.role(role_id)
        return frozenset(r.allowed_tools) if r else frozenset()

    def verification_for(self, task_type: str) -> VerificationDefault | None:
        """按任务类型匹配默认评审配置。

        匹配用前缀通配（``bookkeeping.*``）——这是**模式匹配**，不是领域判断：
        它比较的是字符串模式，与"什么算记账"无关。换一个领域只是换一个模式串。
        """
        best: VerificationDefault | None = None
        for vd in self.verification_defaults:
            for pattern in vd.applies_to_task_types:
                if _matches(pattern, task_type):
                    # 更具体的模式优先
                    if best is None or len(pattern) > len(best.applies_to_task_types[0]):
                        best = vd
        return best


def _matches(pattern: str, value: str) -> bool:
    if pattern.endswith(".*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def load_agents(path: Path) -> AgentSpec:
    if not path.exists():
        raise DispatcherError("policy_violation", f"角色表不存在：{path}")
    try:
        spec = AgentSpec.model_validate(load_yaml(path))
    except Exception as e:
        raise DispatcherError("policy_violation", f"角色表校验失败：{e}") from e

    ids = [r.id for r in spec.roles]
    if len(ids) != len(set(ids)):
        raise DispatcherError("policy_violation", "角色 id 重复")
    # 提示词必须存在。引用一个不存在的文件会在第一次真跑某个角色时才炸，
    # 那时错误已经离原因很远了。
    missing = [r.system_prompt_ref for r in spec.roles if not _resolve_prompt(path, r.system_prompt_ref)]
    if missing:
        raise DispatcherError(
            "policy_violation",
            f"角色提示词文件不存在：{missing}（相对于仓库根）",
        )
    return spec


def _resolve_prompt(agents_path: Path, ref: str) -> Path | None:
    """``system_prompt_ref`` 相对仓库根。从角色表位置反推仓库根。"""
    root = agents_path.parent.parent
    p = root / ref
    return p if p.exists() else None


__all__ = ["AgentRole", "AgentSpec", "VerificationDefault", "load_agents"]
