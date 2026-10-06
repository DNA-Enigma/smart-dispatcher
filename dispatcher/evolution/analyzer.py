"""LLM 分析：在确定性检测器筛出的证据包上做归因与取舍。

**它不计算指标。** 指标已经算好了（``detectors.py``），而且比模型算得准。
模型在这里做的事只有一件：**从证据里找出机制，再据此提改哪一处配置。**

分工的边界值得再说一遍，因为它是这套机制安全性的来源：

* 检测器说"守卫推翻率 25%，超过阈值 15%"——这是算术。
* 分析说"被推翻的都是查询本人账目的请求，说明 ``direct_answer`` 的 when 漏了
  '不需要用户私有数据'这一条"——这是判断。

产出必须过 ``validator.py`` 才能入库。因此即使这里的提示词被注入成功，
能产出的也只是"某个白名单配置键的某个合法取值，交给人审"——注入的收益上限
被输出形状本身封死了，不依赖模型的自觉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.errors import DispatcherError
from ..core.policy import Policy, resolve_policy_path
from ..core.prompts import PromptLibrary, data_block, fill
from ..ports.llm import LLMError, LLMMessage, LLMPort
from .detectors import DetectionReport, DetectorEngine
from .validator import SuggestionValidator

ANALYSIS_PROMPT = "evolution_analysis.md"


@dataclass
class AnalysisOutcome:
    suggestions: list[dict] = field(default_factory=list)      # 通过校验、待审批
    discarded: list[dict] = field(default_factory=list)        # 未过校验，附原因
    findings: list[dict] = field(default_factory=list)
    skipped_detectors: list[dict] = field(default_factory=list)
    sample_size: int = 0
    window: tuple[datetime, datetime] | None = None
    tier: str = ""
    latency_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "tier": self.tier,
            "latency_ms": self.latency_ms,
            "sample_size": self.sample_size,
            "window": None if self.window is None else {
                "from": self.window[0].isoformat(), "to": self.window[1].isoformat()
            },
            "findings": self.findings,
            "skipped_detectors": self.skipped_detectors,
            "accepted": len(self.suggestions),
            "discarded": len(self.discarded),
            "notes": self.notes,
        }


class Analyzer:
    def __init__(
        self,
        *,
        policy: Policy,
        prompts: PromptLibrary,
        llm: LLMPort,
        engines_dir: Path,
        suggestions_doc: dict | None = None,
    ) -> None:
        self._policy = policy
        self._prompts = prompts
        self._llm = llm
        self._engine = DetectorEngine(
            policy=policy, config_path=engines_dir / "detectors.yaml"
        )
        self._validator = SuggestionValidator(
            policy=policy,
            kinds_path=engines_dir / "suggestion_kinds.yaml",
            suggestion_kinds=suggestions_doc,
        )

    # ------------------------------------------------------------------
    def analyze_deterministic(
        self, logs: list, *, now: datetime | None = None
    ) -> DetectionReport:
        """只跑检测器，不调模型。**给测试与快速自省用**——
        想看"现在有没有异常"不该被迫花一次模型调用。"""
        return self._engine.run(logs, now=now)

    # ------------------------------------------------------------------
    async def analyze(
        self,
        logs: list,
        *,
        scope: dict | None = None,
        now: datetime | None = None,
    ) -> AnalysisOutcome:
        report = self._engine.run(logs, now=now)
        outcome = AnalysisOutcome(
            findings=report.to_wire()["findings"],
            skipped_detectors=report.to_wire()["skipped"],
            sample_size=len(logs),
            window=report.window,
            tier=self._policy.evolution.analysis_tier,
        )

        if not report.findings:
            # **没有触发任何检测器时不该调模型。** 让模型在"一切正常"上找问题，
            # 它一定会找出一些来——那是最典型的"看起来合理但没用"的建议。
            outcome.notes.append("没有检测器触发，未调用模型")
            return outcome

        messages = self._messages(report, logs)
        try:
            raw, res = await self._llm.generate_json(
                messages,
                tier=self._policy.evolution.analysis_tier,
                requires=("text",),
                max_repair_attempts=1,
            )
            outcome.latency_ms = res.latency_ms
        except (LLMError, DispatcherError) as e:
            if getattr(e, "fatal", False):
                raise
            outcome.notes.append(f"分析调用失败：{e}")
            return outcome

        items = _extract_items(raw)
        if items is None:
            # **把实际收到的形状写进备注。** 只说"不是数组"的话，下一个人还得
            # 自己再跑一次才知道模型给了什么；而这条备注会留在分析结果里。
            keys = sorted(raw.keys())[:8] if isinstance(raw, dict) else []
            outcome.notes.append(
                f"模型输出里找不到建议数组：顶层是 {type(raw).__name__}"
                + (f"，键为 {keys}" if keys else "")
                + "。已在提示词里要求 {\"suggestions\": [...]} 的形状。"
            )
            return outcome

        limit = self._policy.evolution.max_suggestions_per_pass
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            suggestion = self._normalize(item, scope=scope, finding_ids=[
                f["detector_id"] for f in outcome.findings
            ])
            result = self._validator.validate(suggestion)
            suggestion["guards"] = result.guards
            suggestion["validation_notes"] = result.notes
            if result.ok:
                suggestion["status"] = "proposed"
                outcome.suggestions.append(suggestion)
            else:
                suggestion["status"] = "discarded"
                outcome.discarded.append(suggestion)

        if outcome.discarded:
            # 丢弃率本身是一个信号：它升高说明分析提示词产出的东西不合规，
            # 而那条信号最终指向的是**提示词**的审查，不是继续让模型重试。
            outcome.notes.append(
                f"{len(outcome.discarded)} 条建议未通过确定性校验（丢弃率 "
                f"{len(outcome.discarded) / max(1, len(items)):.0%}）"
            )
        return outcome

    # ------------------------------------------------------------------
    def _messages(self, report: DetectionReport, logs: list) -> list[LLMMessage]:
        limit = self._policy.evolution.sample_limit
        wire_findings = report.to_wire()["findings"]
        # 只取触发过的检测器所涉及的那些 run 做抽样；全量既贵又没必要
        sample = logs[-limit:]

        kinds = [
            {"id": k["id"], "description": k.get("description"),
             "target_artifact": k.get("target_artifact"), "value_type": k.get("value_type"),
             "delta_rule": k.get("delta_rule")}
            for k in self._validator._kinds.values()
        ]
        policy_snapshot = self._relevant_policy_slice(wire_findings)

        system = fill(
            self._prompts.get(ANALYSIS_PROMPT),
            {
                "findings": json.dumps(wire_findings, ensure_ascii=False, indent=2),
                "run_log_sample": data_block(
                    f"运行日志抽样（{len(sample)} 条）",
                    json.dumps([x.to_wire() for x in sample], ensure_ascii=False, indent=2),
                ),
                "policy_snapshot": json.dumps(policy_snapshot, ensure_ascii=False, indent=2),
                "suggestion_kinds": json.dumps(kinds, ensure_ascii=False, indent=2),
                "locked_paths": json.dumps(self._policy.locked_paths, ensure_ascii=False),
                "max_delta_ratio": f"{self._policy.evolution.max_delta_ratio:.0%}",
            },
        )
        return [
            LLMMessage.system(system),
            LLMMessage.user(
                "请按系统提示的要求，只输出一个建议数组（JSON array）。"
                "没有值得提的建议就输出 []。"
            ),
        ]

    def _relevant_policy_slice(self, findings: list[dict]) -> dict[str, Any]:
        """只把**相关**的策略片段给模型看，而不是整份策略。

        整份策略有几万字，塞进去既贵又会稀释重点；而模型只需要看到"被指控的
        那几处现在长什么样"才能提出具体改动。相关范围由检测器建议的类型推出。
        """
        wants = {k for f in findings for k in (f.get("suggests") or [])}
        out: dict[str, Any] = {
            "policy_version": self._policy.policy_version,
            "thresholds": self._policy.thresholds.model_dump(mode="json"),
            "budget": {"enforcement": self._policy.budget.enforcement,
                       "warn_at_ratio": self._policy.budget.warn_at_ratio},
        }
        if wants & {"route_guidance_patch", "tier_change", "tool_set_change", "threshold_change"}:
            out["routes"] = [
                {"id": r.id, "when": r.when, "default_tier": r.default_tier,
                 "allowed_tiers": r.allowed_tiers, "max_cost": r.max_cost,
                 "path": r.path, "requires_handler": r.requires_handler}
                for r in self._policy.routes
            ]
        if wants & {"tier_change"}:
            out["model_tiers"] = {
                name: {"capabilities": t.capabilities, "provider": t.provider}
                for name, t in self._policy.model_tiers.items()
            }
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _aliases(item: dict) -> dict:
        """把模型常见的字段名偏差归一到契约字段名。

        **只处理无歧义的形式偏差，不猜语义。** ``type`` → ``kind``、
        ``target_path`` → ``target.path`` 这类改名的意图是清楚的；
        而"它到底想改哪里"这种语义问题一律留给校验器，不在这里替它判断。

        这些偏差是真实跑出来的：模型的**分析内容完全正确**（准确指出了 when 里
        漏掉的两条排除项、反事实也从真实采样里读出），却因为把 ``kind`` 写成 ``type``
        而被整条丢弃。丢的是格式，不是判断——那不值得。
        """
        out = dict(item)
        for src, dst in (("type", "kind"), ("suggestion_type", "kind")):
            if dst not in out and src in out:
                out[dst] = out.pop(src)
        if "target" not in out:
            path = out.pop("target_path", out.pop("path", None))
            if path is not None:
                out["target"] = {
                    "artifact": out.pop("artifact", out.pop("target_artifact",
                                                            "routing.policy.yaml")),
                    "path": path,
                    "current": out.pop("current_value", out.pop("current", None)),
                    "proposed": out.pop("proposed_value", out.pop("proposed", None)),
                }
        elif isinstance(out.get("target"), dict):
            tgt = dict(out["target"])
            for src, dst in (("current_value", "current"), ("proposed_value", "proposed"),
                             ("target_path", "path")):
                if dst not in tgt and src in tgt:
                    tgt[dst] = tgt.pop(src)
            out["target"] = tgt
        # 路径里写成 routes[id=xxx] 的地方归一成 routes[xxx]
        tgt = out.get("target")
        if isinstance(tgt, dict) and isinstance(tgt.get("path"), str):
            tgt["path"] = _normalize_path(tgt["path"])
        return out

    def _normalize(self, item: dict, *, scope: dict | None, finding_ids: list[str]) -> dict:
        """把模型的输出补成一个完整的建议对象。

        只**补**不**改**：默认值、时间戳、id、scope 由代码填；模型给出的
        kind / target / evidence / rationale 一律原样保留，交给校验器判定。
        在这里"顺手修正"模型的输出，会让校验器看到一份非模型产出的东西——
        那等于把校验对象换掉了。
        """
        item = self._aliases(item)
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        path = str(target.get("path") or "")
        exists, current = resolve_policy_path(self._policy.model_dump(mode="json"), path)
        if exists and "current" not in target:
            target["current"] = current

        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        return {
            "suggestion_id": item.get("suggestion_id")
            or f"sg_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{abs(hash(path)) % 997:03d}",
            "created_at": datetime.now(UTC).isoformat(),
            "scope": scope or {"level": "user", "tenant_id": "default", "user_id": None},
            "analysis_window": item.get("analysis_window") or {},
            "sample_size": int(item.get("sample_size") or 0),
            "basis_policy_version": self._policy.policy_version,
            "kind": item.get("kind"),
            "target": target,
            "evidence": {
                "metric": evidence.get("metric"),
                "before": evidence.get("before"),
                "after_estimate": evidence.get("after_estimate"),
                "counterfactual": evidence.get("counterfactual"),
                "affected_run_ids": evidence.get("affected_run_ids") or [],
                "distinct_users_affected": evidence.get("distinct_users_affected"),
            },
            "rationale": str(item.get("rationale") or ""),
            "confidence": _clamp01(item.get("confidence")),
            "risk": item.get("risk") if item.get("risk") in {"low", "medium", "high"} else "medium",
            "blast_radius": [str(b) for b in (item.get("blast_radius") or [])],
            "rollback": {"method": "policy_version_rollback", "to_version": self._policy.policy_version},
            "decided_by": None,
            "decided_at": None,
            "decision_note": None,
            "resulting_policy_version": None,
            "detector_ids": finding_ids,
        }


def _normalize_path(path: str) -> str:
    """``routes[id=direct_answer].when`` → ``routes[direct_answer].when``。

    纯语法归一：``id=<值>`` 和直接写值在语义上没有区别，模型两种写法都可能用。
    这不属于"替模型猜意图"——它只是把同一种意思的两种拼法统一成一种。
    """
    import re as _re

    return _re.sub(r"\[id=([^\]]+)\]", r"[\1]", path)


def _extract_items(raw: Any) -> list | None:
    """从模型的输出里取出建议数组。

    接受几种常见包装，是因为**结构化输出模式只保证顶层是对象**——模型必须把
    数组放在某个键下面，而那个键叫什么它有权自己决定。为此把整次分析判为失败，
    不值得：名字不对不是内容不对。收不到再报，并把实际键名写进备注。
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return None
    for key in ("suggestions", "items", "data", "result", "suggestion"):
        val = raw.get(key)
        if isinstance(val, list):
            return val
    # 单个建议对象也接受（只提一条时模型可能直接给对象）
    if "kind" in raw and "target" in raw:
        return [raw]
    return None


def _clamp01(v: Any) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["ANALYSIS_PROMPT", "AnalysisOutcome", "Analyzer"]
