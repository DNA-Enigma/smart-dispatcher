"""任务类型词表。

评估器只能从这份清单里选 ``task_type``。词表封闭有两个理由：

1. 类型名会进入 RunLog 与 04 的聚合指标，必须稳定可比；
2. 词表如果开放，04 就无法统计"词表缺项"——而缺项恰恰是最有价值的一类建议。

词表里的**顺序有意义**：``fallback_type`` 指向的 ``generic.unknown`` 是最后兜底的
那一类，其 ``domain`` 决定了没有领域 handler 时会发生什么。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .errors import DispatcherError
from .yamlio import load_yaml


class TaxonomyType(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    domain: str
    description: str = ""
    typical_modality: list[str] | None = None
    sensitivity: str | None = None


class Taxonomy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    taxonomy_version: str
    fallback_type: str
    types: list[TaxonomyType]
    add_suggestion_required_fields: list[str] | None = None

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(t.id for t in self.types)

    @property
    def domains(self) -> frozenset[str]:
        return frozenset(t.domain for t in self.types)

    def get(self, type_id: str) -> TaxonomyType | None:
        return next((t for t in self.types if t.id == type_id), None)

    def domain_of(self, type_id: str) -> str | None:
        t = self.get(type_id)
        return t.domain if t else None


def load_taxonomy(path: Path) -> Taxonomy:
    if not path.exists():
        raise DispatcherError("policy_violation", f"词表不存在：{path}")
    try:
        tax = Taxonomy.model_validate(load_yaml(path))
    except Exception as e:
        raise DispatcherError("policy_violation", f"词表校验失败：{e}") from e
    if tax.fallback_type not in tax.ids:
        raise DispatcherError(
            "policy_violation",
            f"fallback_type={tax.fallback_type!r} 不在词表里——兜底类型本身必须是词表中的一项",
        )
    return tax


__all__ = ["Taxonomy", "TaxonomyType", "load_taxonomy"]
