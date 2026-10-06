"""确定性检测器：自进化的第一段。

**这一层完全不使用 LLM。** 它便宜、确定、每次都跑；LLM 只在它筛出的证据包上
做归因与取舍（见 ``analyzer.py``）。两段式的理由跟 ``quality_service`` 一样：
把"算指标"和"解释指标"分开——前者是算术，后者才是判断。让 LLM 去算算术，
既贵又不准。

判定规则**全部读自** ``config/evolution/detectors.yaml``：阈值、谓词、聚合方式
都在配置里。代码只认得一组**封闭的**谓词形式与聚合方式，遇到不认识的在加载期
就报错——否则一个写错的谓词会变成一个**永不触发的检测器**，而人以为它在监控。

最后那一点不是假想的：``suggestion_drop_rate`` 这类检测器的 ``source_field`` 是
``internal``，它不来自运行日志。引擎会把这类显式标成"不在此处求值"并写进
报告，而不是安静地跳过——安静的跳过等于一个看起来在跑、实际永不报警的监控。
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.errors import DispatcherError
from ..core.policy import Policy
from ..core.runlog import RunLog
from ..core.yamlio import load_yaml

# 封闭的运算符集合。谓词里出现别的写法就在加载期失败。
_OPS = ("==", "!=", ">=", "<=", ">", "<")
_UNARY = ("is non-empty", "is empty", "is not null", "is null")

_PREDICATE_RE = re.compile(
    r"^\s*(?P<lhs>[A-Za-z_][\w\.\[\]]*)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<rhs>.+?)\s*$"
)
_UNARY_RE = re.compile(
    r"^\s*(?P<lhs>[A-Za-z_][\w\.\[\]]*)\s+(?P<op>is non-empty|is empty|is not null|is null)\s*$"
)

# 聚合方式同样是封闭集合
AGGREGATES = frozenset(
    {"rate", "p90", "p95", "mean", "mean_of_ordinal", "p90_of_ratio", "per_key_rate"}
)

# 这些 source_field 不来自运行日志，引擎不求值它们（并如实报告）
NON_RUNLOG_SOURCES = frozenset({"internal"})


@dataclass
class MetricFinding:
    detector_id: str
    metric: str
    observed: float
    threshold: float
    sample_size: int
    window_days: int
    severity: str
    suggests: list[str] = field(default_factory=list)
    group: str | None = None
    note: str | None = None
    affected_task_ids: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """观测值相对阈值的位置。>1 表示越过阈值。"""
        return (self.observed / self.threshold) if self.threshold else 0.0


@dataclass
class DetectionReport:
    findings: list[MetricFinding]
    skipped: list[tuple[str, str]]      # (detector_id, 原因)
    evaluated: int
    window: tuple[datetime, datetime]

    def to_wire(self) -> dict[str, Any]:
        return {
            "evaluated_detectors": self.evaluated,
            "fired": len(self.findings),
            "window": {"from": self.window[0].isoformat(), "to": self.window[1].isoformat()},
            "findings": [
                {
                    "detector_id": f.detector_id, "metric": f.metric,
                    "observed": round(f.observed, 6), "threshold": f.threshold,
                    "sample_size": f.sample_size, "severity": f.severity,
                    "suggests": f.suggests, "group": f.group, "note": f.note,
                }
                for f in self.findings
            ],
            "skipped": [{"detector_id": d, "reason": r} for d, r in self.skipped],
        }


# ---------------------------------------------------------------------------
# 谓词求值
# ---------------------------------------------------------------------------
def _resolve_path(obj: Any, path: str) -> Any:
    """按点分路径取值，``[]`` 表示"展开列表并继续走剩下的路径"。

    ``[]`` **不是终点**——``human_signal.edits[].field`` 的意思是
    "取 edits 每一项的 field"，如果一遇到 ``[]`` 就返回整个列表，
    后面的 ``.field`` 就被丢掉了，检测器拿到一堆 dict 而不是字段名。
    """
    parts = path.split(".")
    node: Any = obj
    for i, part in enumerate(parts):
        if part.endswith("[]"):
            name = part[:-2]
            seq = node.get(name) if isinstance(node, dict) else None
            rest = ".".join(parts[i + 1:])
            if not isinstance(seq, list):
                return []
            if not rest:
                return seq
            out: list[Any] = []
            for item in seq:
                got = _resolve_path(item, rest)
                out.extend(got if isinstance(got, list) else [got])
            return out
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _flatten(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _coerce(rhs: str, policy: Policy) -> Any:
    raw = rhs.strip()
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if raw.startswith("thresholds."):
        # 前缀要去掉：它是要往 policy.thresholds 里查，而字典**已经**是那个段落了。
        # 带着前缀去查会静默返回 None，然后比较时抛类型错误——
        # 报错还算好的，如果谓词是 `== None` 就会变成一个永不触发的检测器。
        return _resolve_path(policy.thresholds.model_dump(), raw[len("thresholds."):])
    try:
        return float(raw) if ("." in raw) else int(raw)
    except ValueError:
        return raw.strip("\"'")


def _eval_predicate(pred: str, value: Any, policy: Policy) -> bool:
    if m := _UNARY_RE.match(pred):
        v = value
        op = m["op"]
        if op == "is non-empty":
            return bool(v)
        if op == "is empty":
            return not v
        if op == "is not null":
            return v is not None
        return v is None
    if m := _PREDICATE_RE.match(pred):
        rhs = _coerce(m["rhs"], policy)
        op = m["op"]
        if isinstance(rhs, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
            # 数值字段与字符串字面量比较 → 无法判定。**报错而不是返回 False**：
            # 返回 False 会让这个检测器永不触发，而人以为它在监控。
            raise DispatcherError(
                "policy_violation",
                f"谓词 {pred!r} 把数值与字符串 {rhs!r} 比较；请检查 detectors.yaml",
            )
        try:
            if op == "==":
                return value == rhs
            if op == "!=":
                return value != rhs
            if op == ">":
                return value is not None and value > rhs  # type: ignore[operator]
            if op == ">=":
                return value is not None and value >= rhs  # type: ignore[operator]
            if op == "<":
                return value is not None and value < rhs  # type: ignore[operator]
            return value is not None and value <= rhs  # type: ignore[operator]
        except TypeError as e:
            raise DispatcherError(
                "policy_violation", f"谓词 {pred!r} 类型不匹配：{e}"
            ) from e
    raise DispatcherError(
        "policy_violation",
        f"不认识的谓词写法：{pred!r}。支持的运算符：{list(_OPS)} 与 {list(_UNARY)}。"
        f"请在 detectors.yaml 里改用受支持的写法——写错的谓词会变成一个永不触发的检测器。",
    )


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * q) - 1))
    return ordered[idx]


def _aggregate(
    name: str, values: list[Any], flags: list[bool], *, policy: Policy, config: dict
) -> float | dict[str, float]:
    if name == "rate":
        return (sum(1 for f in flags if f) / len(flags)) if flags else 0.0
    nums = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if name == "p90":
        return _percentile(nums, 0.90)
    if name == "p95":
        return _percentile(nums, 0.95)
    if name == "mean":
        return statistics.fmean(nums) if nums else 0.0
    if name == "mean_of_ordinal":
        order = config.get("tier_order") or list(policy.model_tier_ids)
        ordinals = [order.index(v) for v in values if isinstance(v, str) and v in order]
        return statistics.fmean(ordinals) if ordinals else 0.0
    if name == "p90_of_ratio":
        # 逐 run 求 估算/实际 的比值，再取 p90
        return 0.0  # 由调用方用 compare_with 专门处理
    if name == "per_key_rate":
        return {}  # 由调用方按 group_by 专门处理
    raise DispatcherError("policy_violation", f"不认识的聚合方式：{name!r}")


# ---------------------------------------------------------------------------
class DetectorEngine:
    def __init__(self, *, policy: Policy, config_path: Path) -> None:
        raw = load_yaml(config_path)
        self._detectors: list[dict] = list(raw.get("detectors") or [])
        self._policy = policy
        self._validate()
        # 检测器阈值不属于策略文件，但 analyze 时要用；把它带给引擎做校验用
        self._active_policy = policy

    def _validate(self) -> None:
        """加载期校验：写错的谓词、聚合、source_field 都在这里失败。

        不留到运行期——一个写错的检测器不会报错，它只是**永不触发**，
        而那是最难发现的一类问题。
        """
        seen: set[str] = set()
        for d in self._detectors:
            did = d.get("id")
            if not did or did in seen:
                raise DispatcherError("policy_violation", f"检测器 id 缺失或重复：{did!r}")
            seen.add(did)
            if d.get("aggregate") not in AGGREGATES:
                raise DispatcherError(
                    "policy_violation",
                    f"检测器 {did} 的 aggregate={d.get('aggregate')!r} 不在 {sorted(AGGREGATES)} 内",
                )
            pred = d.get("predicate")
            if pred and not (_PREDICATE_RE.match(pred) or _UNARY_RE.match(pred)):
                raise DispatcherError(
                    "policy_violation",
                    f"检测器 {did} 的谓词写法不受支持：{pred!r}"
                    f"（支持 {list(_OPS) + list(_UNARY)}）",
                )
            if not isinstance(d.get("threshold"), (int, float)):
                raise DispatcherError("policy_violation", f"检测器 {did} 的阈值不是数字")
            if int(d.get("min_sample", 0)) < 1:
                raise DispatcherError("policy_violation", f"检测器 {did} 的 min_sample 非法")

    @property
    def detectors(self) -> list[dict]:
        return list(self._detectors)

    # ------------------------------------------------------------------
    def run(
        self,
        logs: list[RunLog],
        *,
        window_days: int | None = None,
        now: datetime | None = None,
    ) -> DetectionReport:
        now = now or datetime.now(UTC)
        findings: list[MetricFinding] = []
        skipped: list[tuple[str, str]] = []
        evaluated = 0
        widest = window_days or 0

        for d in self._detectors:
            did = d["id"]
            days = _days(d.get("window", "7d"))
            widest = max(widest, days)
            src = str(d.get("source_field", ""))

            if src in NON_RUNLOG_SOURCES:
                skipped.append((did, f"source_field={src!r} 不来自运行日志（需由建议循环自身统计）"))
                continue

            windowed = _within(logs, now=now, days=days)
            sample = len(windowed)
            if sample < d["min_sample"]:
                skipped.append((did, f"样本不足：{sample} < min_sample {d['min_sample']}"))
                continue

            try:
                observed, affected, group = self._compute(d, windowed)
            except DispatcherError as e:
                skipped.append((did, f"求值失败：{e}"))
                continue

            evaluated += 1
            if observed is None:
                skipped.append((did, "指标无法计算（缺少必要字段）"))
                continue
            if not _fires(observed, float(d["threshold"]), d["metric"]):
                continue

            findings.append(
                MetricFinding(
                    detector_id=did,
                    metric=str(d["metric"]),
                    observed=float(observed),
                    threshold=float(d["threshold"]),
                    sample_size=sample,
                    window_days=days,
                    severity=str(d.get("severity", "medium")),
                    suggests=list(d.get("suggests") or []),
                    group=group,
                    note=d.get("note") if isinstance(d.get("note"), str) else None,
                    affected_task_ids=affected[:20],
                )
            )

        return DetectionReport(
            findings=findings, skipped=skipped, evaluated=evaluated,
            window=(now - timedelta(days=widest), now),
        )

    # ------------------------------------------------------------------
    def _compute(
        self, d: dict, logs: list[RunLog]
    ) -> tuple[float | dict[str, float] | None, list[str], str | None]:
        src = str(d["source_field"])
        agg = str(d["aggregate"])
        pred = d.get("predicate")
        group_by = d.get("group_by")

        # per_key_rate：按列表字段的每个取值分别算比率
        if agg == "per_key_rate":
            key_path = str(group_by) if group_by else src
            counts: dict[str, list[bool]] = {}
            for log in logs:
                wire = log.model_dump(mode="json")
                for key in _flatten(_resolve_path(wire, key_path)):
                    if not isinstance(key, str):
                        continue
                    counts.setdefault(key, []).append(True)
            if not counts:
                return None, [], None
            rates = {k: len(v) / len(logs) for k, v in counts.items()}
            worst_key, worst = max(rates.items(), key=lambda kv: kv[1])
            return worst, [], worst_key

        # p90_of_ratio：需要 compare_with
        if agg == "p90_of_ratio":
            other = d.get("compare_with")
            if not other:
                return None, [], None
            ratios: list[float] = []
            for log in logs:
                wire = log.model_dump(mode="json")
                a = _resolve_path(wire, src)
                b = _resolve_path(wire, str(other))
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0:
                    ratios.append(abs(a - b) / b)
            if not ratios:
                return None, [], None
            return _percentile(ratios, 0.90), [], None

        # 其余：逐 run 取值 → 谓词 → 聚合
        # **"空不空"是针对整个集合的判断，不能逐元素展开。**
        # ``guard_applied is non-empty`` 问的是"这次运行有没有被守卫纠正过"，
        # 逐元素求值会把列表里每个字符串都算成"非空"，于是比率恒为 1.0——
        # 一个永远触发的检测器，比不触发更糟：它会持续产出假建议。
        whole_value_predicate = bool(pred) and (
            "is non-empty" in str(pred) or "is empty" in str(pred) or "is null" in str(pred)
            or "is not null" in str(pred)
        )

        values: list[Any] = []
        flags: list[bool] = []
        affected: list[str] = []
        for log in logs:
            wire = log.model_dump(mode="json")
            raw = _resolve_path(wire, src)
            if whole_value_predicate:
                values.append(raw)
                fire = _eval_predicate(str(pred), raw, self._active_policy)
                flags.append(fire)
                if fire:
                    affected.append(log.task_id)
                continue
            if group_by:
                # 一个 run 里可能有多条（例如每个节点一条），逐条展开
                raw = raw if isinstance(raw, list) else [raw]
            for v in _flatten(raw):
                values.append(v)
                fire = _eval_predicate(str(pred), v, self._active_policy) if pred else True
                flags.append(fire)
                if fire and pred:
                    affected.append(log.task_id)
        if not values:
            return None, [], None
        return _aggregate(agg, values, flags, policy=self._active_policy, config=d), affected, None


def _fires(observed: float | dict, threshold: float, metric: str) -> bool:
    if isinstance(observed, dict):
        return False
    return observed > threshold


def _days(window: str) -> int:
    w = window.strip().lower()
    if w.endswith("d"):
        return max(1, int(w[:-1]))
    if w.endswith("h"):
        return 1
    return 7


def _within(logs: list[RunLog], *, now: datetime, days: int) -> list[RunLog]:
    cutoff = now - timedelta(days=days)
    return [x for x in logs if x.started_at >= cutoff]


__all__ = ["AGGREGATES", "DetectionReport", "DetectorEngine", "MetricFinding"]
