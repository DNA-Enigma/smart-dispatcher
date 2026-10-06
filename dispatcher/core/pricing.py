"""价格表与成本估算。

这里体现一条分工：**LLM 出量级，代码做算术。**

评估器只被允许说"这活的输入量看着是 m 桶"，不允许说"大约 0.008 元"。
LLM 对"这活看着不小"的判断是有价值的，对具体数字的判断是没有价值的；
把算术放在这里，估算的偏差来源就只剩"桶判得准不准"这一个，可观测、可改进。
"""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .errors import DispatcherError
from .policy import Policy
from .yamlio import load_yaml


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pricing_version: str
    currency: str
    updated_at: str | None = None
    unit: str
    # 单价的分母。代码里不写 1000——换计费单位（有些供应商按百万 token 计价）
    # 应当只改配置。
    unit_divisor: int
    models: dict[str, dict[str, float]]  # 档位 → {"in": 单价, "out": 单价}
    token_buckets: dict[str, int]  # 桶名 → 该桶的**上界** token 数
    tools: dict | None = None
    media: dict | None = None
    basis_template: str

    # -- 桶序 --------------------------------------------------------------
    def bucket_names(self) -> list[str]:
        """按 token 数从小到大排序。

        **顺序来自数值大小，不来自 YAML 的书写顺序**——这里不能用书写顺序，
        因为量级桶的语义就是数值大小，写反了应当被排序纠正而不是被默默接受。
        """
        return sorted(self.token_buckets, key=lambda k: self.token_buckets[k])

    def bucket_bounds(self, bucket: str) -> tuple[int, int]:
        """返回该桶的 (下界, 上界)。

        下界取前一个桶的上界，上界取本桶的上界。这是对"桶"这一粗糙粒度的
        确定性插值，不需要额外的配置——没有前一个桶时下界为 0。
        """
        if bucket not in self.token_buckets:
            raise DispatcherError("policy_violation", f"未知的量级桶：{bucket!r}")
        names = self.bucket_names()
        upper = self.token_buckets[bucket]
        idx = names.index(bucket)
        lower = self.token_buckets[names[idx - 1]] if idx > 0 else 0
        return lower, upper

    def est_cost(
        self, tier: str, in_bucket: str | None, out_bucket: str | None
    ) -> tuple[float, float]:
        """估算 (最小, 最大) 成本。缺桶视为 0 token。"""
        prices = self.models.get(tier)
        if prices is None:
            raise DispatcherError("policy_violation", f"价格表里没有档位 {tier!r}")
        divisor = float(self.unit_divisor)
        lo = hi = 0.0
        for bucket, key in ((in_bucket, "in"), (out_bucket, "out")):
            if not bucket:
                continue
            lower, upper = self.bucket_bounds(bucket)
            lo += lower * prices[key] / divisor
            hi += upper * prices[key] / divisor
        return round(lo, 6), round(hi, 6)

    def cost_of(self, tier: str, in_tokens: int | None, out_tokens: int | None) -> float:
        """一次**实际**调用的费用。

        与 ``est_cost`` 的区别：那个是事前估算（LLM 给量级桶、代码做算术），
        这个是事后记账（用真实 token 数）。两者都要有——前者用来做预算与路由决策，
        后者用来发现估算偏差（``est_cost_error_ratio`` 检测器就在看这个差）。

        **推理 token 计入 ``out_tokens``。** 当前供应商的深度思考产生的
        ``reasoning_tokens`` 已经包含在 ``completion_tokens`` 里并按输出计费，
        因此这里不需要单独加——但要知道它们在，否则会误以为"关掉思考省不了多少钱"。
        """
        prices = self.models.get(tier)
        if prices is None:
            raise DispatcherError("policy_violation", f"价格表里没有档位 {tier!r}")
        divisor = float(self.unit_divisor)
        return round(
            (in_tokens or 0) * prices["in"] / divisor
            + (out_tokens or 0) * prices["out"] / divisor,
            8,
        )

    @property
    def basis(self) -> str:
        return self.basis_template.replace("{pricing_version}", self.pricing_version)


def load_pricing(path: Path) -> Pricing:
    if not path.exists():
        raise DispatcherError("policy_violation", f"价格表不存在：{path}")
    try:
        return Pricing.model_validate(load_yaml(path))
    except Exception as e:
        raise DispatcherError("policy_violation", f"价格表校验失败：{e}") from e


def check_pricing_consistency(policy: Policy, pricing: Pricing) -> list[str]:
    """价格表与策略里的档位单价必须一致。

    不一致时不抛错，而是返回警告列表——**以价格表为准**（价格来自供应商，
    是运营维护的事实），策略里的那份是冗余记录。这样"改了价但忘了同步策略"
    不会让系统停摆，但会留下一条可查的记录。
    """
    warnings: list[str] = []
    for tier in policy.model_tier_ids:
        if tier not in pricing.models:
            warnings.append(f"策略档位 {tier} 在价格表里没有对应条目")
            continue
        declared = policy.model_tiers[tier].price_per_1k
        if not declared:
            continue
        for key in ("in", "out"):
            if key in declared and not math.isclose(
                declared[key], pricing.models[tier][key], rel_tol=1e-9
            ):
                warnings.append(
                    f"档位 {tier} 的 {key} 单价不一致：策略 {declared[key]} vs 价格表 "
                    f"{pricing.models[tier][key]}（以价格表为准）"
                )
    return warnings


__all__ = ["Pricing", "check_pricing_consistency", "load_pricing"]
