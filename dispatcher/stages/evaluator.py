"""阶段 01 — TaskEvaluator。

把一个不透明的请求变成结构化画像。**不做**：不选路由、不执行任何事。

分工（见 ``docs/02-stages.md`` 的分工表）：

* LLM 判断：任务类型、意图、复杂度分数、紧急度、能力候选、是否需要澄清。
* 代码确定：媒体校验、量级桶 → 金额的算术、分数 → 档位的查表、
  ``modality`` 的推导、缓存键。

两处**代码覆盖 LLM 输出**，理由是单一事实来源：

* ``complexity.band`` 由 ``score`` 按 ``thresholds.complexity_band_cutoffs`` 查表得出。
  让模型同时给分数和档位，只会引入两者不一致的可能，而档位是纯查表结果。
* ``modality`` 由请求里的实际媒体类型推导，不采信模型的自述。

**关于缓存的一处偏离。** 策略里配了 ``evaluator.cache.similarity_min``，设想是"相似度
由 LLM 判定"。实现时发现那条路走不通：判断两个请求相不相似本身就要一次模型调用，
而缓存的全部意义是省掉一次调用——为了省一次调用先花一次调用，账算不过来。
按策略里声明的 ``key_fields``（模态、敏感级、字数桶、媒体数）做精确匹配同样不安全：
两段字数相同、媒体数相同的文本完全可能是完全不同的任务，复用画像会直接给出错误的
任务类型。

因此这里只做**能保证正确的最小缓存**：整个请求的规范化哈希相同才复用。
它对重试与幂等重放有效，不需要任何相似度判断，也不会把 A 的画像用到 B 上。
真正的模糊匹配需要本地嵌入模型，那是另一件事，已记入待办而不是在这里凑一个近似。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..core.contract import (
    Clarification,
    Complexity,
    CostEstimate,
    EstimatedScale,
    EvaluatorTelemetry,
    ProfileCache,
    TaskEnvelope,
    TaskProfile,
    Urgency,
    VisionNeeds,
)
from ..core.errors import DispatcherError
from ..core.policy import Policy
from ..core.pricing import Pricing
from ..core.prompts import (
    PromptLibrary,
    capability_catalog,
    data_block,
    fill,
    request_block,
    taxonomy_block,
)
from ..core.registry import HandlerRegistry
from ..core.taxonomy import Taxonomy
from ..ports.llm import LLMError, LLMMessage, LLMPort
from ..ports.media import MediaStorePort

EVALUATOR_PROMPT = "evaluator.md"

# 图像经 base64 进入多模态调用时，先取回字节再拼 data URI。
# 这一步在这里而不是在提示词渲染里：字节不走文本通道。
_DATA_URI_TEMPLATE = "data:{mime};base64,{payload}"


@dataclass
class EvaluationMeta:
    """画像之外的观测数据。**不进入契约的对外字段**，只进 RunLog 与自省。"""

    tier: str = ""
    resolved_tier: str = ""
    escalated_from: str | None = None
    model_resolved: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    taxonomy_miss: bool = False
    degraded: bool = False
    cache_hit: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class EvaluationOutcome:
    profile: TaskProfile
    meta: EvaluationMeta


class Evaluator:
    def __init__(
        self,
        *,
        policy: Policy,
        pricing: Pricing,
        registry: HandlerRegistry,
        prompts: PromptLibrary,
        llm: LLMPort,
        media: MediaStorePort,
        taxonomy: Taxonomy,
    ) -> None:
        self._policy = policy
        self._pricing = pricing
        self._registry = registry
        self._prompts = prompts
        self._llm = llm
        self._media = media
        self._taxonomy = taxonomy
        self._cache: dict[str, TaskProfile] = {}

    # ------------------------------------------------------------------
    # 缓存键：规范化请求的哈希。相同输入 ⇒ 相同画像，这个方向是安全的。
    # ------------------------------------------------------------------
    @staticmethod
    def _cache_key(envelope: TaskEnvelope) -> str:
        material = json.dumps(
            {
                "text": (envelope.input.text or "").strip(),
                "media": sorted(
                    (m.sha256 or m.media_id) for m in (envelope.input.media or [])
                ),
                "declared": envelope.declared.model_dump(),
                "locale": envelope.identity.locale,
                "sensitivity": envelope.constraints.data_sensitivity,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    def _vision_required(self, envelope: TaskEnvelope) -> bool:
        return any(m.kind == "image" for m in (envelope.input.media or []))

    def _resolve_tier(self, envelope: TaskEnvelope) -> str:
        """挑一个能力足够的档位。

        图像请求需要 ``vision.extract``，文本请求不需要——因此带图的请求会落到
        更高的档位，而纯文本的仍停在最便宜的档位上。这个选择完全由能力集合决定，
        不需要"如果带图就用某档"这样的分支。
        """
        requires = ["text"]
        if self._vision_required(envelope):
            requires.append("vision.extract")
        configured = self._policy.evaluator.tier
        # 先在配置的档位里试，不行再在全部档位里按序数（由便宜到贵）找第一个满足的
        resolved = self._policy.resolve_tier(requires, [configured])
        if resolved is None:
            resolved = self._policy.resolve_tier(requires, list(self._policy.model_tier_ids))
        if resolved is None:
            raise DispatcherError(
                "no_capability_match",
                f"没有任何已配置档位能满足评估器所需能力：{requires}",
                context={"requires": requires},
            )
        return resolved

    async def _image_parts(self, envelope: TaskEnvelope) -> list[tuple[str, str]]:
        """取回图像字节并拼成 data URI。取不到的媒体直接报错——
        把"图丢了"当成"没有图"会让评估器在缺信息的情况下猜任务类型。"""
        import base64

        out: list[tuple[str, str]] = []
        for ref in envelope.input.media or []:
            if ref.kind != "image":
                continue
            got = await self._media.get(ref.media_id)
            if got is None:
                raise DispatcherError(
                    "unsupported_media",
                    f"媒体 {ref.media_id} 不存在或已过保留期",
                    context={"media_id": ref.media_id},
                )
            blob, rec = got
            payload = base64.b64encode(blob).decode("ascii")
            out.append((
                _DATA_URI_TEMPLATE.format(mime=rec.mime, payload=payload),
                ref.role or "源图",
            ))
        return out

    # ------------------------------------------------------------------
    def _build_messages(self, envelope: TaskEnvelope, images: list[tuple[str, str]]) -> list[LLMMessage]:
        system = fill(
            self._prompts.get(EVALUATOR_PROMPT),
            {
                "taxonomy": taxonomy_block(self._taxonomy.model_dump()),
                "capability_catalog": capability_catalog(self._registry),
                "token_bucket_guide": self._bucket_guide(),
                "fallback_type": self._taxonomy.fallback_type,
            },
        )
        body = data_block("本次请求", request_block(envelope))
        if images:
            user = LLMMessage.user_with_images(body, images)
        else:
            user = LLMMessage.user(body)
        return [LLMMessage.system(system), user]

    def _bucket_guide(self) -> str:
        names = self._pricing.bucket_names()
        lines = ["量级桶（只给桶名，不要估算金额）："]
        for n in names:
            lo, hi = self._pricing.bucket_bounds(n)
            lines.append(f"- {n}: 约 {lo}–{hi} tokens")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    async def evaluate(
        self,
        envelope: TaskEnvelope,
        *,
        request_id: str = "",
        budget: Any | None = None,
    ) -> EvaluationOutcome:
        meta = EvaluationMeta()
        configured_tier = self._policy.evaluator.tier

        key = self._cache_key(envelope)
        if cached := self._cache.get(key):
            meta.cache_hit = True
            meta.notes.append("画像缓存命中（请求规范化哈希一致）")
            return EvaluationOutcome(
                profile=cached.model_copy(
                    update={"profile_cache": ProfileCache(hit=True, key=key[:16], similarity=1.0)}
                ),
                meta=meta,
            )

        tier = self._resolve_tier(envelope)
        meta.tier = configured_tier
        meta.resolved_tier = tier
        if tier != configured_tier:
            # 能力驱动的升档不同于"信息不足再升级"，如实记下原因供 04 观察
            meta.notes.append(
                f"评估器档位由 {configured_tier} 提升到 {tier}（能力需求："
                f"{'vision.extract' if self._vision_required(envelope) else 'text'}）"
            )

        images = await self._image_parts(envelope)
        messages = self._build_messages(envelope, images)

        raw, result, meta = await self._call_with_escalation(
            messages, envelope=envelope, tier=tier, configured_tier=configured_tier,
            meta=meta, budget=budget
        )
        meta.model_resolved = result.model_resolved
        meta.latency_ms = result.latency_ms
        meta.input_tokens = result.input_tokens
        meta.output_tokens = result.output_tokens

        profile = self._build_profile(raw, envelope, meta)

        cacheable = not profile.needs_clarification and not meta.degraded
        if cacheable:
            self._cache[key] = profile
        profile = profile.model_copy(update={"profile_cache": ProfileCache(hit=False, key=key[:16])})
        return EvaluationOutcome(profile=profile, meta=meta)

    # ------------------------------------------------------------------
    async def _call_with_escalation(
        self,
        messages: list[LLMMessage],
        *,
        envelope: TaskEnvelope,
        tier: str,
        configured_tier: str,
        meta: EvaluationMeta,
        budget: Any | None = None,
    ):
        """调用模型，失败时按策略有界升级。

        升级条件里的 ``schema_validation_failed`` 是可检测的那个，因此 M1 实现它。
        ``insufficient_information`` 需要模型自述"我信息不够"，那是另一条路径，
        不在这里假装实现。
        """
        esc = self._policy.evaluator.escalation
        attempts: list[str] = [tier]
        if esc.get("enabled") and esc.get("to_tier") in self._policy.model_tier_ids:
            for _ in range(int(esc.get("max_escalations", 0))):
                attempts.append(str(esc["to_tier"]))

        requires = ("text", "vision.extract") if self._vision_required_for(messages) else ("text",)
        last: Exception | None = None
        for idx, attempt_tier in enumerate(attempts):
            try:
                # 每次尝试都要确认该档位满足能力需求——升级目标可能不具备视觉能力
                if not self._policy.satisfies(attempt_tier, requires):
                    last = DispatcherError(
                        "no_capability_match",
                        f"档位 {attempt_tier} 不满足评估器需求 {requires}",
                    )
                    continue
                raw, result = await self._llm.generate_json(
                    messages,
                    tier=attempt_tier,
                    requires=requires,
                    max_repair_attempts=self._policy.evaluator.max_repair_attempts,
                    timeout_ms=self._policy.evaluator.timeout_ms,
                    # 供应商参数（如关掉深度思考）来自策略，代码不认识它们
                    options=self._policy.evaluator.options,
                )
                if budget is not None:
                    # 记账。评估是每个请求都跑的，因此它是最容易被忽略的固定开销——
                    # 不记的话"每单成本"会被系统性低估。
                    budget.charge(
                        self._pricing.cost_of(
                            attempt_tier, result.input_tokens, result.output_tokens
                        ),
                        subtask_id="__evaluator__",
                        note="stage:evaluator",
                    )
                if idx > 0:
                    meta.escalated_from = attempts[idx - 1]
                    meta.notes.append(
                        f"评估器由 {attempts[idx-1]} 升级到 {attempt_tier} 后成功"
                    )
                return raw, result, meta
            except LLMError as e:
                if e.fatal:
                    # 凭证/余额/请求格式问题不会因为换档位重试或降级而好起来。
                    # 降级在这里是有害的：它让系统在凭证已失效时继续安静地产出
                    # 兜底画像，看起来一切正常，实际每条结果都是垃圾。
                    raise e.to_dispatcher_error() from e
                last = e
                meta.notes.append(f"档位 {attempt_tier} 评估失败：{e}")
            except DispatcherError as e:
                if e.fatal:
                    # 配置/程序错误（含密钥没配）。降级在这里是有害的：
                    # 它会让系统在明知不可用的情况下继续产出兜底画像。
                    raise
                last = e
                meta.notes.append(f"档位 {attempt_tier} 评估失败：{e}")

        # 全部尝试失败 → 兜底画像，degraded=true，任务继续（见 docs/02-stages.md 失败模式表）
        meta.degraded = True
        meta.notes.append(f"评估器全部尝试失败，使用兜底画像：{last}")
        raw = self._fallback_raw(envelope)
        if raw["task_type"] != self._taxonomy.fallback_type:
            meta.notes.append(
                f"兜底画像沿用调用方声明的类型 {raw['task_type']}"
                f"（declared.authoritative=true），未落到 {self._taxonomy.fallback_type}"
            )
        return raw, _DegradedResult(tier=configured_tier), meta

    @staticmethod
    def _vision_required_for(messages: list[LLMMessage]) -> bool:
        return any(
            isinstance(m.content, list)
            and any(p.get("type") == "image_url" for p in m.content)
            for m in messages
        )

    def _fallback_raw(self, envelope: TaskEnvelope) -> dict:
        """评估失败时的兜底画像。

        **调用方的权威声明不能在这里丢掉。** ``declared.authoritative: true`` 是
        契约里唯一的合法快捷路径（``schemas/task_envelope.json``）：调用方在做结构化
        断言"我已经知道这是什么"。而评估器失败恰恰是最需要这条信息的时刻——之前无条件
        返回 ``generic.unknown``，于是"带图记账 + 已声明意图"在评估器抖动一次之后就变成
        一份内容为空的画像：能力候选为空 → 流程模板匹配不上 → 自由拆解拿着空画像拆 →
        产出空计划 → 整单 422（P0-1c）。声明本来就在手里，没有理由丢掉。

        只认**词表里有的**声明：声明是断言，不是特权。越出封闭词表的声明等于没有声明
        （词表封闭是刻意的，见 ``core/taxonomy.py``）。
        """
        declared_type: str | None = None
        if envelope.declared.authoritative and envelope.declared.intent:
            if envelope.declared.intent in self._taxonomy.ids:
                declared_type = envelope.declared.intent

        if declared_type is None:
            return {
                "task_type": self._taxonomy.fallback_type,
                "intent_summary": "评估器未能完成，使用兜底画像。",
                "complexity": {"score": 0.0, "reasons": ["评估器降级"]},
                "urgency": {"level": "normal"},
                "required_capabilities": [],
                "candidate_capabilities": [],
                "recommended_mode": "async",
                "needs_clarification": False,
                "confidence": 0.0,
            }

        t = self._taxonomy.get(declared_type)
        return {
            "task_type": declared_type,
            "intent_summary": f"评估器未能完成，沿用调用方声明的类型：{declared_type}",
            "complexity": {"score": 0.0, "reasons": ["评估器降级，沿用调用方声明"]},
            "urgency": {"level": "normal"},
            "required_capabilities": [],
            "candidate_capabilities": self._capabilities_for(declared_type, envelope),
            "recommended_mode": "async",
            "needs_clarification": False,
            "confidence": 0.0,
            "data_sensitivity": t.sensitivity if t else None,
        }

    def _capabilities_for(self, type_id: str, envelope: TaskEnvelope) -> list[str]:
        """从声明推出候选能力——**纯集合读取，降级路径上不再引入一次抽样**。

        能力清单来自 handler 自己的声明：词表的 ``domain`` 按命名约定就是
        ``handler_id``（见 ``config/taxonomy.yaml``）。调用方若还显式声明了
        ``capability`` 且它确实存在，就把它放在最前面。
        """
        caps: list[str] = []
        declared_cap = envelope.declared.capability
        if declared_cap and declared_cap in self._registry.all_capabilities():
            caps.append(declared_cap)
        domain = self._taxonomy.domain_of(type_id)
        m = self._registry.manifest(domain) if domain else None
        if m is not None:
            caps.extend(c for c in m.capabilities if c not in caps)
        return caps

    # ------------------------------------------------------------------
    def _build_profile(
        self, raw: dict, envelope: TaskEnvelope, meta: EvaluationMeta
    ) -> TaskProfile:
        """把 LLM 的原始对象拼成一个完整的 TaskProfile。

        这里对 LLM 的输出是**宽容读取**（缺字段用默认值），但对最终对象是**严格构造**
        （TaskProfile 是 extra=forbid 的）。宽容读取是因为模型的输出永远会有小偏差；
        严格构造是因为画像的每个字段都会被下游用到，缺一个就会在下游某个地方变成默认值——
        而默认值往往不是这个请求该有的值。
        """
        # **模型输出是不可信输入，类型也会错。** 这一课是拿真实模型跑出来的：
        # 脚本化测试永远给正确的形状，而真实模型会返回 `estimated_scale: "medium"`
        # 这种"看起来合理但不是对象"的东西。下面统一用 _as_dict/_as_list 收口，
        # 让每个字段的读取都退化成"拿不到就当没有"，而不是抛 AttributeError。
        complexity = _as_dict(raw.get("complexity"))
        try:
            score = float(complexity.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = min(1.0, max(0.0, score))

        task_type = str(raw.get("task_type") or self._taxonomy.fallback_type)
        if task_type not in self._taxonomy.ids:
            # 词表未命中：回落到兜底类型，并记下来。这个指标升高意味着词表缺项——
            # 那正是 04 提出 taxonomy_add 建议的依据。
            meta.taxonomy_miss = True
            meta.notes.append(f"类型 {task_type!r} 不在词表中，回落到 {self._taxonomy.fallback_type}")
            task_type = self._taxonomy.fallback_type

        urgency_raw = _as_dict(raw.get("urgency"))
        urgency_level = urgency_raw.get("level", "normal")
        if urgency_level not in {"low", "normal", "high"}:
            urgency_level = "normal"

        modality = self._derive_modality(envelope)

        vision = None
        if self._vision_required(envelope):
            v = _as_dict(raw.get("vision"))
            roles = [m.role for m in (envelope.input.media or []) if m.role]
            vision = VisionNeeds(
                required=True,
                image_roles=roles or ["source_document"],
                expected_extraction=_as_str_list(v.get("expected_extraction")),
            )

        scale_raw = _as_dict(raw.get("estimated_scale"))
        scale = None
        if scale_raw:
            buckets = set(self._pricing.bucket_names())
            scale = EstimatedScale(
                input_tokens_bucket=scale_raw.get("input_tokens_bucket")
                if scale_raw.get("input_tokens_bucket") in buckets else None,
                output_tokens_bucket=scale_raw.get("output_tokens_bucket")
                if scale_raw.get("output_tokens_bucket") in buckets else None,
                est_tool_calls=_as_int(scale_raw.get("est_tool_calls")),
            )

        # 成本估算：LLM 只给桶，算术在这里。
        tier_for_cost = self._resolve_tier(envelope)
        est_cost = None
        if scale is not None:
            lo, hi = self._pricing.est_cost(
                tier_for_cost, scale.input_tokens_bucket, scale.output_tokens_bucket
            )
            est_cost = CostEstimate(
                currency=self._pricing.currency, min=lo, max=hi, basis=self._pricing.basis
            )

        needs_clarification = bool(raw.get("needs_clarification"))
        clarification = None
        if needs_clarification:
            c = _as_dict(raw.get("clarification"))
            options = [
                {"id": str(o.get("id")), "label": str(o.get("label"))}
                for o in _as_list(c.get("options"))
                if isinstance(o, dict) and o.get("id") and o.get("label")
            ]
            partial = c.get("partial_profile")
            clarification = Clarification(
                question=str(c.get("question") or "需要更多信息才能继续。"),
                options=options,
                blocking=bool(c.get("blocking", True)),
                partial_profile=partial if isinstance(partial, dict) else None,
            )

        confidence = _clamp01(raw.get("confidence", 0.0))

        return TaskProfile(
            policy_version=self._policy.policy_version,
            task_type=task_type,
            intent_summary=str(raw.get("intent_summary") or ""),
            modality=modality,
            complexity=Complexity(score=score, band=self._band_of(score),
                                  reasons=_as_str_list(complexity.get("reasons"))),
            urgency=Urgency(level=urgency_level, reasons=_as_str_list(urgency_raw.get("reasons"))),
            vision=vision,
            candidate_capabilities=_as_str_list(raw.get("candidate_capabilities")),
            required_capabilities=_as_str_list(raw.get("required_capabilities")),
            data_sensitivity=raw.get("data_sensitivity")
            if raw.get("data_sensitivity") in {"public", "internal", "personal", "financial"} else None,
            estimated_scale=scale,
            est_cost=est_cost,
            recommended_mode="async" if raw.get("recommended_mode") == "async" else "sync",
            needs_clarification=needs_clarification,
            clarification=clarification,
            confidence=confidence,
            degraded=meta.degraded,
            evaluator=EvaluatorTelemetry(
                tier=meta.resolved_tier or meta.tier,
                model_resolved=meta.model_resolved,
                escalated_from=meta.escalated_from,
                latency_ms=meta.latency_ms,
            ),
        )

    # ------------------------------------------------------------------
    def _derive_modality(self, envelope: TaskEnvelope) -> list[str]:
        """模态由请求里的实际内容推导，不采信模型自述。

        模型说"这是文本请求"而请求里带着一张图，那是模型错；按它的话走会让下游
        不配视觉能力，最后失败在一个很难回溯的地方。

        顺序固定为"媒体按枚举顺序在前，text 在后"，与 ``schemas/task_profile.json``
        里的示例一致。顺序本身没有语义，但固定下来能让快照可以逐字节比对——
        否则同一份输入两次运行可能产出不同顺序的数组，diff 起来全是噪音。
        """
        kinds = {m.kind for m in (envelope.input.media or [])}
        out = [k for k in ("image", "audio", "pdf") if k in kinds]
        if envelope.input.text:
            out.append("text")
        return out or ["text"]

    def _band_of(self, score: float) -> str:
        """分数 → 档位，纯查表。阈值来自策略。"""
        cut = self._policy.thresholds.complexity_band_cutoffs
        if score < cut["low"]:
            return "low"
        if score < cut["high"]:
            return "medium"
        return "high"


# ---------------------------------------------------------------------------
class _DegradedResult:
    """兜底画像的伪 LLMResult。它没有真的调用过模型，因此遥测全为空/零，
    而不是伪造一个看起来正常的延迟。"""

    def __init__(self, *, tier: str) -> None:
        self.text = ""
        self.tier = tier
        self.model_resolved = None
        self.latency_ms = 0
        self.input_tokens = None
        self.output_tokens = None
        self.finish_reason = "degraded"


def _as_dict(v: Any) -> dict:
    """模型给的可能是字符串、列表、null。**不在预期形状就当作没有**，
    而不是抛异常——一个字段形状不对不该让整条任务崩掉。"""
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _as_str_list(v: Any) -> list[str]:
    return [str(x) for x in _as_list(v) if isinstance(x, (str, int, float))]


def _clamp01(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = ["EvaluationMeta", "EvaluationOutcome", "Evaluator"]
