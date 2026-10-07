"""流水线装配：配置 → 端口 → 阶段 → 执行器。

这里是完整状态机推进的地方：

    received → evaluating → routing → planning → running
        ⇄ awaiting_clarification（停住等人，答复后**从断点继续**）
        ⇄ escalated
    → succeeded | failed | cancelled | budget_exceeded | rejected

两个刻意的设计：

* **同步是优化，异步是基底。** 同一条流水线只实现一次：``mode`` 决定的是"调用方
  这一侧怎么等"，而不是跑哪套逻辑。投影耗时超预算时同步会被提升为异步，
  并把原因写进响应。
* **``awaiting_clarification`` 用 ``prior`` 恢复而不是重跑。** 已完成的抽取、归类、
  查重都还有效，重跑既浪费 token，又可能产出不同的结果——而用户刚刚确认过的那一份，
  重跑之后就未必一样了。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters.memory_media import InMemoryMediaStore
from .adapters.memory_state import InMemoryStateStore
from .adapters.openai_compat import OpenAICompatibleLLM
from .core.agents import AgentSpec, load_agents
from .core.budget import BudgetLedger
from .core.cancel import CancellationToken
from .core.context import MediaResolver
from .core.contract import TaskEnvelope
from .core.errors import DispatcherError
from .core.eventbus import EventBus
from .core.events import TERMINAL_EVENTS
from .core.nodeexec import NodeExecutor
from .core.plan import ExecutionPlan
from .core.policy import Policy, load_policy
from .core.pricing import Pricing, check_pricing_consistency, load_pricing
from .core.prompts import PromptLibrary
from .core.registry import HandlerRegistry
from .core.runlog import HumanSignal, build_run_log
from .core.runner import DagRunner
from .core.settings import Settings, get_settings
from .core.state import TERMINAL_STATUSES, TaskRecord
from .core.taxonomy import Taxonomy, load_taxonomy
from .plugins import DEFAULT_SPEC_PATH, build_registry
from .ports.llm import LLMPort
from .ports.media import MediaStorePort
from .ports.state import StateStorePort
from .stages.decomposer import Decomposer
from .stages.direct import DirectAnswerer
from .stages.evaluator import Evaluator
from .stages.router import Router

DEFAULT_TEMPLATE_DIR = "config/flow_templates"


@dataclass
class DispatcherConfig:
    settings: Settings
    policy_path: Path | None = None
    handler_dir: Path | None = None
    template_dir: Path | None = None
    # 执行层是否接入。M1 时为 False（走到路由就停并标记 rejected）；
    # M2/M3 之后为 True，任务会真的被执行。
    execution_enabled: bool = True
    # 状态存储：``memory`` 或 ``sqlite``。手机端本地模式用后者（需要跨重启重放）。
    state_backend: str = "memory"
    sqlite_path: Path | None = None
    # 自进化是否启用。关掉时任务照跑，只是不采 RunLog、不起分析循环。
    evolution_enabled: bool = True
    # handler 插件清单。默认读 ``config/handlers.yaml``。
    handler_spec_path: Path | None = None
    # **外部注入通道**：需要把应用自己的数据库接进 handler 时，
    # 调用方构造好带依赖的实例从这里传进来。
    handlers: list[Any] | None = None


class Dispatcher:
    def __init__(
        self,
        *,
        policy: Policy,
        pricing: Pricing,
        registry: HandlerRegistry,
        prompts: PromptLibrary,
        llm: LLMPort,
        media: MediaStorePort,
        state: StateStorePort,
        taxonomy: Taxonomy,
        agents: AgentSpec,
        events: EventBus,
        ledger: BudgetLedger,
        execution_enabled: bool = True,
        config_warnings: list[str] | None = None,
        evolution_store: Any | None = None,
    ) -> None:
        self.policy = policy
        self.pricing = pricing
        self.registry = registry
        self.prompts = prompts
        self.llm = llm
        self.media = media
        self.state = state
        self.taxonomy = taxonomy
        self.agents = agents
        self.events = events
        self.ledger = ledger
        self.execution_enabled = execution_enabled
        self.config_warnings = config_warnings or []
        # 演化存储与循环：**不在请求路径上**。用单独的存储实现是因为它是冷数据，
        # 生命周期也更长（分析窗口 7 天），混进 StateStorePort 会让每个请求
        # 都要面对一堆用不到的字段。
        self.evolution_store = evolution_store
        self.evolution: Any | None = None

        self.evaluator = Evaluator(
            policy=policy, pricing=pricing, registry=registry, prompts=prompts,
            llm=llm, media=media, taxonomy=taxonomy,
        )
        self.router = Router(policy=policy, registry=registry, prompts=prompts, llm=llm,
                             pricing=pricing)
        self.decomposer = Decomposer(
            policy=policy, registry=registry, agents=agents, prompts=prompts,
            llm=llm, templates_dir=Path(prompts._root).parent / DEFAULT_TEMPLATE_DIR,
            pricing=pricing,
        )
        # 第三种执行路径。它不进拆解——直答没有步骤。
        self.direct = DirectAnswerer(
            policy=policy, pricing=pricing, prompts=prompts, llm=llm
        )
        self.media_resolver = MediaResolver(media)
        if evolution_store is not None:
            from .evolution.analyzer import Analyzer
            from .evolution.loop import EvolutionLoop

            self.evolution = EvolutionLoop(
                store=evolution_store,
                analyzer=Analyzer(
                    policy=policy, prompts=prompts, llm=llm,
                    engines_dir=Path(prompts._root).parent / "config" / "evolution",
                ),
                policy=policy,
                evolution_cfg=policy.evolution.model_dump(mode="json"),
            )
        # 每次运行需要独立的取消令牌与执行器（令牌是任务级的）
        self._running: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, CancellationToken] = {}

    # ------------------------------------------------------------------
    @classmethod
    def build(cls, config: DispatcherConfig | None = None) -> Dispatcher:
        cfg = config or DispatcherConfig(settings=get_settings())
        root = cfg.settings.contract_root
        policy = load_policy(cfg.policy_path or cfg.settings.policy_path)
        pricing = load_pricing(root / "config" / "pricing.yaml")
        taxonomy = load_taxonomy(cfg.settings.taxonomy_path)
        agents = load_agents(cfg.settings.agents_path)
        registry = build_registry(
            cfg.handler_spec_path or (root / DEFAULT_SPEC_PATH),
            extra=cfg.handlers,
        )
        warnings = check_pricing_consistency(policy, pricing)

        evolution_store = None
        if cfg.evolution_enabled:
            if cfg.state_backend == "sqlite":
                from .adapters.sqlite_evolution import SqliteEvolutionStore

                evolution_store = SqliteEvolutionStore(
                    cfg.sqlite_path or (root / "data" / "dispatcher.db")
                )
            else:
                from .adapters.memory_evolution import InMemoryEvolutionStore

                evolution_store = InMemoryEvolutionStore()

        state: StateStorePort
        if cfg.state_backend == "sqlite":
            from .adapters.sqlite_state import SqliteStateStore

            state = SqliteStateStore(cfg.sqlite_path or (root / "data" / "dispatcher.db"))
        else:
            state = InMemoryStateStore()

        return cls(
            policy=policy,
            pricing=pricing,
            registry=registry,
            prompts=PromptLibrary(cfg.settings.prompts_dir),
            llm=OpenAICompatibleLLM(policy, cfg.settings),
            media=InMemoryMediaStore(
                allowed_mime=policy.limits.media.allowed_mime,
                max_bytes=policy.limits.media.max_bytes,
            ),
            state=state,
            taxonomy=taxonomy,
            agents=agents,
            events=EventBus(state),
            ledger=BudgetLedger(
                enforcement=policy.enforcement_mode,
                warn_at_ratio=policy.budget.warn_at_ratio,
                currency=policy.budget.currency,
            ),
            execution_enabled=cfg.execution_enabled,
            config_warnings=warnings,
            evolution_store=evolution_store,
        )

    async def aclose(self) -> None:
        for t in list(self._running.values()):
            if not t.done():
                t.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        close = getattr(self.state, "close", None)
        if close is not None:
            await close()
        await self.llm.aclose()

    # ------------------------------------------------------------------
    @staticmethod
    def new_task_id() -> str:
        return f"task_{uuid.uuid4().hex[:20]}"

    def _executor(self, task_id: str, cancel: CancellationToken) -> NodeExecutor:
        return NodeExecutor(
            policy=self.policy, pricing=self.pricing, registry=self.registry,
            agents=self.agents, prompts=self.prompts, llm=self.llm,
            media=self.media_resolver, events=self.events, ledger=self.ledger,
            state=self.state, cancellation=cancel,
        )

    async def _emit_run_log(self, record: TaskRecord) -> None:
        """终态时把这一轮运行的摘要交给演化存储。

        放在**终态**而不是每个阶段：RunLog 是"结果"的聚合，不是"过程"的流。
        过程由事件流负责（给前端看、可重放），结果由 RunLog 负责（给 04 统计）。
        把结果塞进事件流会让重放变重，把过程塞进 RunLog 则让统计无从下手。
        """
        if self.evolution_store is None:
            return
        record.llm_charges = [
            {"subtask_id": c.subtask_id, "note": c.note, "amount": c.amount}
            for c in self.ledger.breakdown(record.task_id)
        ]
        signal = HumanSignal.model_validate(record.human_signal) if record.human_signal else None
        try:
            await self.evolution_store.append_run_log(build_run_log(record, human_signal=signal))
        except Exception as e:  # 遥测写失败**不该影响任务结果**
            record.error = record.error or None
            self.config_warnings.append(f"RunLog 写入失败：{e}")

    async def record_feedback(self, task_id: str, payload: dict) -> dict:
        """人工质量信号。是 04 最有价值的输入——用户的每次修改都给出了真值标注。"""
        record = await self.get(task_id)
        signal = HumanSignal.model_validate(payload)
        record.human_signal = signal.model_dump(mode="json")
        await self._save(record)
        if self.evolution_store is not None:
            # 补挂到已落库的 RunLog 上（信号总是晚于运行到达）
            await self.evolution_store.attach_human_signal(task_id, signal)
        return record.human_signal

    # ------------------------------------------------------------------
    def apply_policy(self, policy_dict: dict) -> None:
        """热换策略。**配置改动不该需要重启进程**——否则金丝雀就不是"跑一段时间"，
        而是"重启一次"。

        阶段对象是按策略构造的，所以这里重建它们；都是轻量对象，重建很便宜。
        """
        new_policy = Policy.model_validate(policy_dict)
        self.policy = new_policy
        self.evaluator = Evaluator(
            policy=new_policy, pricing=self.pricing, registry=self.registry,
            prompts=self.prompts, llm=self.llm, media=self.media, taxonomy=self.taxonomy,
        )
        self.router = Router(
            policy=new_policy, registry=self.registry, prompts=self.prompts,
            llm=self.llm, pricing=self.pricing,
        )
        self.decomposer = Decomposer(
            policy=new_policy, registry=self.registry, agents=self.agents,
            prompts=self.prompts, llm=self.llm,
            templates_dir=self.decomposer._templates_dir, pricing=self.pricing,
        )
        self.direct = DirectAnswerer(
            policy=new_policy, pricing=self.pricing, prompts=self.prompts, llm=self.llm
        )
        setter = getattr(self.llm, "set_policy", None)
        if callable(setter):
            setter(new_policy)
        # 分析器也持有策略（阈值、禁区、建议类型都在里面），一并重建
        if self.evolution_store is not None:
            from .evolution.analyzer import Analyzer

            assert self.evolution is not None
            self.evolution._analyzer = Analyzer(
                policy=new_policy, prompts=self.prompts, llm=self.llm,
                engines_dir=Path(self.prompts._root).parent / "config" / "evolution",
            )
            self.evolution._policy = new_policy
            self.evolution._versions._policy = new_policy

    def _budget_handle(self, task_id: str):
        from .core.budget import BudgetHandle

        st = self.ledger.state(task_id)
        return BudgetHandle(
            _ledger=self.ledger, task_id=task_id,
            limit=st.limit if st else 0.0,
            spent=st.spent if st else 0.0,
            currency=self.ledger.currency,
        )

    async def _save(self, record: TaskRecord) -> TaskRecord:
        record.updated_at = datetime.now(UTC)
        return await self.state.put_task(record)

    # ------------------------------------------------------------------
    async def submit(
        self,
        envelope: TaskEnvelope,
        *,
        source: str = "api",
        request_id: str = "",
        background: bool | None = None,
    ) -> TaskRecord:
        """受理并推进。

        ``background`` 决定"调用方这一侧怎么等"，而不是跑哪套逻辑——
        同一条流水线只实现一次。``None`` 表示按决策自动判定：

        * 决策说 async → 后台跑，立刻返回；
        * 决策说 sync → 内联跑完再返回。

        另外做一次**飞行前**的提升判断：投影耗时超过同步预算时就改走异步。
        刻意放在飞行前而不是"跑到一半超时了再改判"——后者要把已完成的工作接回来，
        而"重新跑一遍"会重放副作用（虽然幂等 token 挡得住，但浪费）。
        设计文档里说的也是飞行前投影，不是中途改判。
        """
        rid = request_id or envelope.request_id or f"req_{uuid.uuid4().hex[:12]}"

        if envelope.idempotency_key:
            existing = await self.state.get_idempotency(
                envelope.idempotency_key, envelope.identity.tenant_id
            )
            if existing:
                rec = await self.state.get_task(existing)
                if rec is not None:
                    return rec

        record = TaskRecord(
            task_id=self.new_task_id(),
            tenant_id=envelope.identity.tenant_id,
            user_id=envelope.identity.user_id,
            parent_task_id=envelope.parent_task_id,
            request_id=rid,
            status="received",
            source=source,
            policy_version=self.policy.policy_version,
            budget_enforcement=self.policy.enforcement_mode,
        )
        await self.state.create_task(record)
        await self.events.emit(record.task_id, "task.created",
                               {"task_id": record.task_id, "mode": "async"})
        if envelope.idempotency_key:
            await self.state.put_idempotency(
                envelope.idempotency_key, envelope.identity.tenant_id, record.task_id
            )

        record.envelope = envelope.model_dump(mode="json")
        # **提前开通预算**：评估、路由、拆解都发生在计划预算确定之前，
        # 而它们都要花钱。等到有决策了再开通，前面几步的账就丢了。
        # 这里先用每任务默认值，runner 拿到计划后会以计划上限更新它
        # （BudgetLedger.open 幂等，不会抹掉已花的钱）。
        self.ledger.open(record.task_id, limit=self.policy.budget.per_task_default)
        await self._save(record)

        cancel = CancellationToken()
        self._cancels[record.task_id] = cancel

        # 先把评估、路由、拆解内联做完——它们都很快，而且结果决定这一侧怎么等
        await self._advance(record, envelope, cancel, stop_after_planning=True)

        if record.status in TERMINAL_STATUSES or record.status == "awaiting_clarification":
            return record

        if background is None:
            background = self._should_background(record)

        if background:
            record.mode = "async"
            await self._save(record)
            self._running[record.task_id] = asyncio.create_task(
                self._advance(record, envelope, cancel)
            )
            return record

        record.mode = "sync"
        await self._advance(record, envelope, cancel)
        return record

    def _should_background(self, record: TaskRecord) -> bool:
        """飞行前判断这一侧要不要等。

        三个来源按优先级：调用方的明确主张 > 路由配置 > 评估器建议。
        最后再看一次投影耗时——即便都倾向同步，投影超预算也要改走异步。
        """
        if record.decision is None:
            return False
        if record.decision.execution_mode == "async":
            return True
        est = record.profile.est_latency_ms if record.profile else None
        if est is not None and est.max > self.policy.limits.sync_timeout_ms:
            record.mode_changed = True
            record.mode_change_reason = "projected_exceeds_sync_budget"
            record.mode_change_detail = (
                f"投影耗时 {est.max}ms 超过同步预算 {self.policy.limits.sync_timeout_ms}ms"
            )
            return True
        return False

    # ------------------------------------------------------------------
    async def _advance(
        self,
        record: TaskRecord,
        envelope: TaskEnvelope,
        cancel: CancellationToken,
        *,
        prior: dict | None = None,
        stop_after_planning: bool = False,
    ) -> TaskRecord:
        """从当前状态往前推进。会被 submit、clarify、后台执行共用。

        ``stop_after_planning`` 让它在拆解完成后停下，把执行留给另一次调用——
        同步/异步的分流就靠它：先内联把评估、路由、拆解做完（都很快），
        再决定这一侧是等还是不等。
        """
        task_id = record.task_id
        handle = self._budget_handle(task_id)
        scope_base = {
            "__task_id__": task_id,
            "__record__": record,
            "__handler_config__": {},
        }

        try:
            # ---- 01 评估 ----
            if record.profile is None:
                record.status = "evaluating"
                await self._save(record)
                outcome = await self.evaluator.evaluate(
                    envelope, request_id=record.request_id, budget=handle
                )
                record.profile = outcome.profile
                record.evaluation_meta = {
                    "tier": outcome.meta.tier, "resolved_tier": outcome.meta.resolved_tier,
                    "escalated_from": outcome.meta.escalated_from,
                    "latency_ms": outcome.meta.latency_ms,
                    "taxonomy_miss": outcome.meta.taxonomy_miss,
                    "degraded": outcome.meta.degraded, "notes": outcome.meta.notes,
                }
                record.title = outcome.profile.intent_summary or None
                domain = self.taxonomy.domain_of(outcome.profile.task_type)
                record.subtitle = outcome.profile.task_type + (f" · {domain}" if domain else "")
                await self._save(record)
                await self.events.emit(
                    task_id, "profile.ready",
                    {"profile": outcome.profile.model_dump(mode="json", exclude_none=True)},
                )

                if outcome.profile.needs_clarification and outcome.profile.clarification:
                    c = outcome.profile.clarification
                    record.status = "awaiting_clarification"
                    record.clarification = {
                        "question_id": f"q_{task_id[-8:]}",
                        "question": c.question,
                        "options": [o.model_dump() for o in c.options],
                        "blocking": c.blocking,
                        "asked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "expires_at": None,
                    }
                    await self._save(record)
                    await self.events.emit(
                        task_id, "clarification.needed",
                        {"question_id": record.clarification["question_id"],
                         "question": c.question,
                         "options": record.clarification["options"], "blocking": c.blocking},
                    )
                    return record

            # ---- 02 路由 ----
            if record.decision is None:
                record.status = "routing"
                await self._save(record)
                routed = await self.router.route(envelope, record.profile, budget=handle)
                record.decision = routed.decision
                record.raw_decision = routed.raw
                record.mode = routed.decision.execution_mode
                record.mode_change_reason = routed.decision.mode_change_reason
                record.route_meta = {
                    "fallback_used": routed.meta.fallback_used,
                    "guard_applied": routed.meta.guard_applied,
                    "guard_violations": routed.meta.guard_violations,
                    "latency_ms": routed.meta.latency_ms, "notes": routed.meta.notes,
                }
                await self._save(record)
                await self.events.emit(
                    task_id, "route.decided",
                    {"decision": routed.decision.model_dump(mode="json", exclude_none=True)},
                )

            # ---- 执行层未接入（M1 遗留模式）----
            if not self.execution_enabled:
                record.status = "rejected"
                record.ended_at = datetime.now(UTC)
                record.error = _problem(
                    "handler_error",
                    "执行层未接入（execution_enabled=False），任务只跑完评估与路由。"
                    "决策已完整产出，见 decision 字段。",
                    task_id, record.request_id,
                    context={"m1_scope": "evaluate_and_route_only",
                             "route_id": record.decision.route_id},
                )
                await self._save(record)
                await self.events.emit(task_id, "task.failed", {"error": record.error})
                return record

            # ---- 02.5 直答（path=direct_llm）：一次补全，没有计划 ----
            #
            # 放在拆解**之前**：直答没有步骤，因此没有计划。以前它掉进下面那段
            # "单步工具调用"的构造里，产出一个 handler 为空的节点，执行器在
            # registry 里找不到实现 → 502 handler_error（见 stages/direct.py 的说明）。
            #
            # ``stop_after_planning`` 这一趟只做评估/路由/拆解来决定"这一侧等不等"，
            # 因此这里先交回去；真正的补全在下一次 _advance（或后台任务）里发生。
            # 这也让 async 的直答真的异步——不然调用方会为一次同步补全白等。
            if record.decision.path == "direct_llm":
                if stop_after_planning:
                    return record
                return await self._answer_directly(record, envelope, handle)

            # ---- 03 拆解 ----
            if record.plan is None:
                record.status = "planning"
                await self._save(record)
                dec = await self.decomposer.decompose(
                    envelope, record.profile, record.decision, task_id=task_id, budget=handle
                )
                record.plan = dec.plan.to_wire()
                record.plan_meta = {
                    "source": dec.meta.source, "template_miss": dec.meta.template_miss,
                    "revisions": dec.meta.revisions, "violations": dec.meta.violations,
                    "notes": dec.meta.notes,
                }
                record.max_parallelism = dec.plan.max_parallelism
                await self._save(record)
                await self.events.emit(
                    task_id, "plan.ready",
                    {"strategy": dec.plan.strategy, "source": dec.plan.source,
                     "node_count": len(dec.plan.nodes),
                     "max_parallelism": dec.plan.max_parallelism,
                     "revision": dec.plan.revision},
                )
            plan = ExecutionPlan.model_validate(record.plan)

            if stop_after_planning:
                return record

            # ---- 03 执行 ----
            record.status = "running"
            await self._save(record)
            scope = dict(scope_base)
            # 原始请求进 scope，供 $ref 的 envelope.* 命名空间解析。
            # 不进的话，模板里 `$ref: envelope.input.text` 这类绑定会解析失败。
            scope["envelope"] = envelope.model_dump(mode="json")
            scope["__route_id__"] = record.decision.route_id
            scope["__allowed_tiers__"] = self.policy.route(record.decision.route_id).allowed_tiers

            runner = DagRunner(
                events=self.events, ledger=self.ledger, policy=self.policy,
                executor=self._executor(task_id, cancel), cancellation=cancel,
            )
            report = await runner.run(
                plan, task_id=task_id, scope=scope,
                max_escalations=(
                    record.decision.escalation_rule.max_escalations
                    if record.decision.escalation_rule else 0
                ),
                prior=prior if prior is not None else record.node_outputs,
            )

            record.node_runs = {
                sid: {
                    "subtask_id": sid, "name": r.node.name,
                    "status": r.status, "progress": 1.0 if r.is_terminal else 0.0,
                    "attempts": r.attempts, "tier": r.node.model_tier,
                    "started_at": _iso(r.started_at), "ended_at": _iso(r.ended_at),
                    "cost": {"amount": r.cost, "currency": self.ledger.currency},
                    "error": ({"code": r.failure.code, "message": r.failure.message,
                               "retryable": r.failure.retryable,
                               "on_failure_applied": r.failure.on_failure_applied}
                              if r.failure else None),
                }
                for sid, r in report.nodes.items()
            }
            record.max_parallelism = plan.max_parallelism
            record.budget_spent = report.spent
            record.budget_warned = self.ledger.warned(task_id)
            record.artifacts = report.artifacts

            if report.status == "awaiting_clarification":
                record.status = "awaiting_clarification"
                rec = report.nodes.get(report.paused_at) if report.paused_at else None
                conf = rec.needs_confirmation if rec else None
                record.clarification = {
                    "question_id": f"q_{task_id[-8:]}",
                    "question": conf.question if conf else "请确认后继续",
                    "options": conf.options if conf else [],
                    "blocking": True,
                    "asked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
                await self._save(record)
                return record

            if report.status == "cancelled":
                record.status = "cancelled"
            elif report.status == "failed":
                record.status = "failed"
                record.error = report.error or _problem(
                    "handler_error", "执行失败", task_id, record.request_id)
            else:
                record.status = "succeeded"
            record.ended_at = datetime.now(UTC)
            await self._save(record)
            await self._emit_run_log(record)
            await self.events.emit(
                task_id,
                {"succeeded": "task.completed", "failed": "task.failed",
                 "cancelled": "task.cancelled"}.get(record.status, "task.failed"),
                {"task_id": task_id, "status": record.status,
                 "artifacts": record.artifacts,
                 "cost": {"amount": report.spent, "currency": self.ledger.currency}}
                if record.status == "succeeded"
                else {"task_id": task_id, "error": record.error,
                      "by": cancel.reason if record.status == "cancelled" else None},
            )
            return record

        except DispatcherError as e:
            # 有类型的失败**不吞**。配置错误、无能力匹配这类问题重试与降级都没用，
            # 它们必须让调用方看见（见 core/errors.py 的 fatal 说明）。
            record.status = "rejected" if e.fatal else "failed"
            record.error = e.to_problem(record.request_id)
            record.ended_at = datetime.now(UTC)
            await self._save(record)
            await self.events.emit(task_id, "task.failed", {"error": record.error})
            if e.fatal:
                raise
            return record

    # ------------------------------------------------------------------
    async def _answer_directly(
        self, record: TaskRecord, envelope: TaskEnvelope, handle: Any
    ) -> TaskRecord:
        """跑完一条直答路径并落终态。

        **状态机仍然归调度层**：直答只产出一段话与一笔账，改状态、发事件都在这里，
        与节点执行走 runner 是同一条纪律（handler 不能改状态）。

        事件流里补一条 ``token``：它的契约形状就是"模型产出的那段文本"，
        断线重连的客户端因此能拿到答案，而不是只看到路由决策之后直接跳到终态。
        """
        assert record.decision is not None  # 只有在路由之后才会走到这里
        task_id = record.task_id
        record.status = "running"
        await self._save(record)

        outcome = await self.direct.answer(envelope, record.decision, budget=handle)

        record.artifacts = outcome.artifact()
        record.budget_spent = self.ledger.spent(task_id)
        record.budget_warned = self.ledger.warned(task_id)
        record.status = "succeeded"
        record.ended_at = datetime.now(UTC)
        await self._save(record)

        await self.events.emit(task_id, "token", {"token": outcome.answer})
        await self._emit_run_log(record)
        await self.events.emit(
            task_id, "task.completed",
            {"task_id": task_id, "status": "succeeded", "artifacts": record.artifacts,
             "cost": {"amount": record.budget_spent, "currency": self.ledger.currency}},
        )
        return record

    # ------------------------------------------------------------------
    async def clarify(self, task_id: str, answer: dict) -> TaskRecord:
        """回答澄清问题，任务**从断点继续**而不是重跑。"""
        record = await self.get(task_id)
        if record.status != "awaiting_clarification":
            raise DispatcherError(
                "invalid_request",
                f"任务当前状态为 {record.status}，不接受澄清",
                task_id=task_id,
            )
        for opt in (record.clarification or {}).get("options", []):
            if answer.get("answer_id") and opt.get("id") == answer.get("answer_id"):
                answer.setdefault("free_text", opt.get("label"))
        record.clarification = None
        record.updated_at = datetime.now(UTC)
        await self._save(record)

        envelope = TaskEnvelope.model_validate(record.envelope)
        cancel = self._cancels.setdefault(task_id, CancellationToken())
        # 把答复并进请求文本，让评估与执行都看得到
        if envelope.input.text is not None or answer.get("free_text"):
            reply = answer.get("free_text") or answer.get("answer_id")
            merged = (envelope.input.text or "") + f"\n（用户澄清：{reply}）"
            envelope = envelope.model_copy(
                update={"input": envelope.input.model_copy(update={"text": merged})}
            )
        prior = dict(record.node_outputs)
        if answer.get("edits"):
            # 用户的修改直接覆盖上一个成功节点的产出——那正是"人工确认"的价值所在
            prior = {**prior, **answer["edits"]}
            record.node_outputs = prior
            record.artifacts = prior
        return await self._advance(record, envelope, cancel, prior=prior)

    async def cancel(self, task_id: str, *, reason: str = "client_requested") -> TaskRecord:
        record = await self.get(task_id)
        if record.status in TERMINAL_STATUSES:
            return record
        token = self._cancels.get(task_id)
        if token is not None:
            token.cancel(reason)
        task = self._running.get(task_id)
        if task is not None and not task.done():
            task.cancel()
        record.status = "cancelled"
        record.ended_at = datetime.now(UTC)
        await self._save(record)
        await self.events.emit(task_id, "task.cancelled", {"by": reason})
        return record

    # ------------------------------------------------------------------
    async def get(self, task_id: str) -> TaskRecord:
        rec = await self.state.get_task(task_id)
        if rec is None:
            raise DispatcherError("not_found", f"任务不存在：{task_id}", task_id=task_id)
        return rec

    async def list(
        self, *, tenant_id: str = "default", user_id: str | None = None, limit: int = 20
    ) -> list[TaskRecord]:
        return await self.state.list_tasks(tenant_id=tenant_id, user_id=user_id, limit=limit)

    def is_terminal_event_seen(self, task_id: str) -> bool:  # pragma: no cover - 供 SSE 用
        return False


def _problem(code: str, detail: str, task_id: str, request_id: str, **kw) -> dict:
    from .core.errors import ERROR_TABLE, ERROR_TITLES

    return {
        "type": f"https://smart-dispatcher/errors/{code.replace('_', '-')}",
        "title": ERROR_TITLES[code],
        "status": ERROR_TABLE[code][0],
        "code": code,
        "detail": detail,
        "retryable": ERROR_TABLE[code][1],
        "request_id": request_id,
        "task_id": task_id,
        **({"context": kw["context"]} if "context" in kw else {}),
    }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def describe_config(d: Dispatcher) -> str:
    return json.dumps(
        {
            "policy_version": d.policy.policy_version,
            "pricing_version": d.pricing.pricing_version,
            "taxonomy_version": d.taxonomy.taxonomy_version,
            "agents_version": d.agents.agents_version,
            "tiers": list(d.policy.model_tier_ids),
            "routes": sorted(d.policy.route_ids),
            "roles": sorted(d.agents.role_ids),
            "handlers": sorted(d.registry.ids),
            "executable": sorted(d.registry.executable_ids),
            "budget_enforcement": d.policy.enforcement_mode,
            "execution_enabled": d.execution_enabled,
            "warnings": d.config_warnings,
        },
        ensure_ascii=False,
    )


__all__ = [
    "DEFAULT_SPEC_PATH", "DEFAULT_TEMPLATE_DIR", "Dispatcher", "DispatcherConfig",
    "TERMINAL_EVENTS", "describe_config",
]
