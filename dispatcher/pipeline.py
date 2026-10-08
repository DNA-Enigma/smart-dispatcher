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
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
from .core.execution import ClarificationAnswer
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

log = logging.getLogger("dispatcher")


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
        # 每次运行需要独立的取消令牌与执行器（令牌是任务级的）。
        # **两个 dict 都必须有出账**：它们只进不出时，长跑进程会按任务数线性增长
        # （审计里的"内存泄漏"）。清账点见 ``_forget``。
        self._running: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, CancellationToken] = {}
        # 任务引用的媒体。与上面两个不同：它要活到任务**到达终态**为止——
        # 停在 awaiting_clarification 的任务恢复时还要读那张截图，因此清理过期
        # 媒体时必须把它排除在外（见 protected_media_ids / sweep_media）。
        self._media_refs: dict[str, tuple[str, ...]] = {}

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
                default_retain_days=cfg.settings.dispatcher_media_retain_days,
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
        # 两个存储都要关。此前只关 state：sqlite 后端下演化库的连接**没有出账**，
        # 每次"重启"（进程内重建 Dispatcher）都会多留一个连接与文件句柄。
        close = getattr(self.state, "close", None)
        if close is not None:
            await close()
        evo_close = getattr(self.evolution_store, "close", None)
        if evo_close is not None:
            await evo_close()
        await self.llm.aclose()

    # ------------------------------------------------------------------
    async def astart(self) -> None:
        """启动侧的异步准备。``build()`` 是同步的，但打开 sqlite 与读回已批准的
        策略都必须等 I/O，因此分两步：``build()`` 装配，``astart()`` 接上存储。

        两件事：

        1. **打开状态库**。``SqliteStateStore`` 是 ``open → 用 → close`` 的用法，
           而 ``build()`` 只构造不打开——不打开的话第一个请求会撞上
           "SqliteStateStore 尚未 open()"。内存实现没有 ``open``，跳过。
        2. **把已批准的策略版本读回来**。批准端点会热换内存里的策略
           （``apply_policy``），但进程一重启就只剩 ``config/routing.policy.yaml``
           的内容——用户批准过的改动静默消失，看起来像"批准了没用"
           （docs/12-deployment.md 第 7 节 #5）。
        """
        opener = getattr(self.state, "open", None)
        if opener is not None:
            await opener()
        await self._restore_active_policy()

    async def _restore_active_policy(self) -> None:
        """库里的 active/canary 版本优先于 YAML 文件。"""
        if self.evolution_store is None:
            return
        active = await self.evolution_store.active_version()
        if active is None:
            return
        version_id = str(active.get("policy_version") or "")
        if not version_id or version_id == self.policy.policy_version:
            return
        policy_dict = active.get("policy")
        if not isinstance(policy_dict, dict):
            log.error(
                "演化库里的生效策略版本 %s 没有策略正文，继续用 %s 的内容。",
                version_id, self.policy.policy_version,
            )
            return
        try:
            self.apply_policy(policy_dict)
        except Exception as e:
            # 不静默：这正是"以为生效了其实没有"的现场，必须在启动日志里留下痕迹。
            log.error(
                "演化库里的生效策略版本 %s 无法装载（%r），继续用 %s 的内容。",
                version_id, e, self.policy.policy_version,
            )
            return
        log.info(
            "已恢复生效策略版本 %s（文件里的版本是 %s）", version_id, policy_dict.get("policy_version")
        )

    # ------------------------------------------------------------------
    # 在册任务的清账
    # ------------------------------------------------------------------
    def _forget(self, task_id: str) -> None:
        """任务不再需要被追踪/取消时，把 ``_running`` / ``_cancels`` 里的条目去掉。

        **只清这两个**：媒体引用另算。它要活到终态——停在
        ``awaiting_clarification`` 的任务恢复时还要读那张截图，由
        ``_release_media`` 单独回收。
        """
        self._running.pop(task_id, None)
        self._cancels.pop(task_id, None)

    def _release_media(self, task_id: str) -> None:
        """任务到达终态：没有任何东西还会读它引用的媒体了。"""
        self._media_refs.pop(task_id, None)

    def _on_task_done(self, task_id: str, task: asyncio.Task) -> None:
        """后台任务的收尾钩子。**四种退出方式都会走到这里**：正常完成、失败、
        被 ``cancel()`` 取消、以及 ``_advance`` 抛出的致命错误。少了它，
        ``_running`` 里那条记录就永远留着（连它引用的 TaskRecord 一起）。

        异常在这里取一次：后台任务没有人 ``await``，不取的话事件循环关闭时会打一条
        "Task exception was never retrieved"。任务本身已经在 ``_advance`` 里落了终态
        与 ``task.failed`` 事件，日志里再喊一次只会掩盖真正的问题。
        """
        self._forget(task_id)
        if task.cancelled():
            # 取消是调用方要的，不是异常；但任务不会再推进了，媒体引用一并放掉。
            self._release_media(task_id)
            return
        exc = task.exception()
        if exc is not None:
            log.warning("后台任务 %s 异常退出：%r", task_id, exc)
            self._release_media(task_id)

    def protected_media_ids(self) -> set[str]:
        """仍被非终态任务引用的媒体 id。

        ``_media_refs`` 里只留非终态任务的条目（终态时由 ``_advance`` 的 finally
        放掉），因此这里直接取并集。停在 ``awaiting_clarification`` 的任务也在内：
        它恢复时要读那张截图，提前删掉会让恢复必然失败（``unsupported_media``
        是 fatal，见 core/errors.py）。
        """
        return {mid for refs in self._media_refs.values() for mid in refs}

    async def sweep_media(self, *, now: datetime | None = None) -> int:
        """清一次过期媒体，返回删除条数。"""
        return await self.media.sweep_expired(
            now or datetime.now(UTC), protected=self.protected_media_ids()
        )

    async def media_sweeper(self, interval_s: float) -> None:
        """周期性清理过期媒体，直到被取消。

        ``docs/05-media.md`` 一直写着"清理由 ``sweep_expired(now)`` 周期性执行，
        由定时任务驱动"——而全仓找不到那个定时任务，这是"媒体常驻不过期"的另一半。
        单次失败只记警告：清理是后台维护，不该把进程带走。

        下界 0.05s 只防"配置写成 0 变成忙循环"；默认值是 300s。
        """
        interval = max(0.05, float(interval_s))
        while True:
            await asyncio.sleep(interval)
            try:
                removed = await self.sweep_media()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 后台维护不该因为一次失败就停摆
                log.warning("媒体清理失败：%r", e)
                continue
            if removed:
                log.info("已清理过期媒体 %d 条", removed)

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

    async def record_feedback(
        self, task_id: str, payload: dict, *, tenant_id: str | None = None
    ) -> dict:
        """人工质量信号。是 04 最有价值的输入——用户的每次修改都给出了真值标注。"""
        record = await self.get(task_id, tenant_id=tenant_id)
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
        dump = envelope.model_dump(mode="json")

        if envelope.idempotency_key:
            existing = await self.state.get_idempotency(
                envelope.idempotency_key, envelope.identity.tenant_id
            )
            if existing:
                rec = await self.state.get_task(existing)
                # 属主不符按"没命中"处理：键空间已经按租户隔离，这一句是兜住
                # "存储实现没按租户隔离键空间"的可能。
                if rec is not None and rec.tenant_id == envelope.identity.tenant_id:
                    # **同一个 key 不代表同一个请求。** 只凭 key 命中就回既有任务，
                    # 等于让任何知道 key 的人用一次重试换走别人的任务快照
                    # （键空间按租户隔离后仍需这一层：同租户内 key 也可能撞车）。
                    # 契约早就写了这条（"同 key + 不同请求体 → 409"），此前从未实现。
                    if rec.envelope is None or _idempotency_fingerprint(
                        rec.envelope
                    ) != _idempotency_fingerprint(dump):
                        raise DispatcherError(
                            "idempotency_conflict",
                            "该幂等键已用于另一个请求体：重放必须原样重发",
                            task_id=rec.task_id,
                            context={
                                "idempotency_key": envelope.idempotency_key,
                                "existing_task_id": rec.task_id,
                            },
                        )
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
            # 原始请求要在**落库之前**挂上：幂等命中路径要拿它比对请求体，
            # 若等 create_task 之后再赋值，两者之间有一个"记录已存在但没有
            # envelope"的窗口，那时的命中只能无从比对。
            envelope=dump,
        )
        await self.state.create_task(record)
        await self.events.emit(record.task_id, "task.created",
                               {"task_id": record.task_id, "mode": "async"})
        if envelope.idempotency_key:
            await self.state.put_idempotency(
                envelope.idempotency_key, envelope.identity.tenant_id, record.task_id
            )

        # **提前开通预算**：评估、路由、拆解都发生在计划预算确定之前，
        # 而它们都要花钱。等到有决策了再开通，前面几步的账就丢了。
        # 这里先用每任务默认值，runner 拿到计划后会以计划上限更新它
        # （BudgetLedger.open 幂等，不会抹掉已花的钱）。
        self.ledger.open(record.task_id, limit=self.policy.budget.per_task_default)
        await self._save(record)

        cancel = CancellationToken()
        self._cancels[record.task_id] = cancel
        refs = tuple(r.media_id for r in (envelope.input.media or []))
        if refs:
            self._media_refs[record.task_id] = refs

        # 先把评估、路由、拆解内联做完——它们都很快，而且结果决定这一侧怎么等
        try:
            await self._advance(record, envelope, cancel, stop_after_planning=True)
        except Exception:
            # 规划阶段抛出（致命错误走这条）也要清账，否则这条路径同样只进不出。
            self._forget(record.task_id)
            raise

        if record.status in TERMINAL_STATUSES or record.status == "awaiting_clarification":
            # 这时候没有 asyncio 任务在跑了：终态不必再取消；等人则靠状态机取消
            # （``clarify`` 会用 setdefault 补一个新令牌，不依赖旧令牌活着）。
            self._forget(record.task_id)
            return record

        if background is None:
            background = self._should_background(record)

        if background:
            record.mode = "async"
            await self._save(record)
            task = asyncio.create_task(self._advance(record, envelope, cancel))
            self._running[record.task_id] = task
            # 出账挂在这里而不是写在 _advance 里：_advance 也被同步/澄清两条路径
            # 复用，而"这个 asyncio 任务什么时候结束"只有这里知道。
            task.add_done_callback(lambda t, tid=record.task_id: self._on_task_done(tid, t))
            return record

        record.mode = "sync"
        try:
            await self._advance(record, envelope, cancel)
        finally:
            # 内联跑完了，这一侧没有后台任务再需要取消令牌。
            self._forget(record.task_id)
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
        clarification: Any | None = None,
        stop_after_planning: bool = False,
    ) -> TaskRecord:
        """从当前状态往前推进。会被 submit、clarify、后台执行共用。

        ``stop_after_planning`` 让它在拆解完成后停下，把执行留给另一次调用——
        同步/异步的分流就靠它：先内联把评估、路由、拆解做完（都很快），
        再决定这一侧是等还是不等。

        ``clarification`` 只在从澄清恢复时非空：它是用户对上一次
        ``ToolResult.confirm`` 的答复，进 scope 后由节点执行器交给 handler。
        没有它，停下来问的那一步永远收不到回答。
        """
        task_id = record.task_id
        handle = self._budget_handle(task_id)
        scope_base = {
            "__task_id__": task_id,
            "__record__": record,
            "__handler_config__": {},
            "__clarification__": clarification,
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

            # **已成功节点的产出要落盘。** 它是澄清恢复时的 ``prior``——少了这一步，
            # 恢复时 prior 为空，上游节点会**从头重跑**：既白花钱，又可能产出不同的
            # 结果，而用户刚刚确认过的正是原来那一份。文档一直写着"答复后从断点继续，
            # 已完成节点的产出保留"，在此之前这句话没有实现。
            record.node_outputs = {
                sid: r.output for sid, r in report.nodes.items() if r.output is not None
            }

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
                # 暂停时也要如实上报**已经产出的部分**：抽取出来的字段即使还没入账
                # 也有价值（契约对失败/取消的要求是同一句）。RunReport.artifacts 在
                # 暂停这条路径上是空的，用节点产出直接合成。
                record.artifacts = record.node_outputs
                await self._save(record)
                return record

            record.artifacts = report.artifacts

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
            # 失败路径上同样要带阶段备注：降级与回退的原因只在这里
            # （"评估器全部尝试失败，使用兜底画像"），只给一个 code 与一句话，
            # 拿到 400/422 的人看不出中间发生过什么。
            #
            # **异常自带的那份优先**：拆解抛错时 record.plan_meta 根本没写成，
            # 而 DecomposeMeta.notes 随异常一起给了出来。
            notes = record.stage_notes()
            if any(notes.values()):
                record.error.setdefault("context", {}).setdefault("stage_notes", notes)
            record.ended_at = datetime.now(UTC)
            await self._save(record)
            await self.events.emit(task_id, "task.failed", {"error": record.error})
            if e.fatal:
                raise
            return record
        finally:
            # 到达终态之后，没有任何东西还会读这张截图了，媒体引用可以放掉。
            # 停在 awaiting_clarification / stop_after_planning 的**不放**——
            # 它们还会被下一趟 _advance 接着跑。
            if record.status in TERMINAL_STATUSES:
                self._release_media(task_id)

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
    async def clarify(
        self, task_id: str, answer: dict, *, tenant_id: str | None = None
    ) -> TaskRecord:
        """回答澄清问题，任务**从断点继续**而不是重跑。

        三条路：

        * 保留选项 id ``cancel`` —— 用户说"别做了"。任务直接置为 cancelled，
          **暂停的那个节点不再执行**（模板里"cancel → 不执行 write / 不建日程"
          就是这一条）。放在调度层而不是每个 handler 里，是因为"取消"与领域无关。
        * 其余答复 —— 进 scope 交给暂停的那个节点重跑，handler 从 ``ctx.clarification``
          读到用户答了什么。
        * ``edits`` —— 按节点 id 覆盖上游产出（用户的修改就是真值）。
        """
        record = await self.get(task_id, tenant_id=tenant_id)
        if record.status != "awaiting_clarification":
            raise DispatcherError(
                "invalid_request",
                f"任务当前状态为 {record.status}，不接受澄清",
                task_id=task_id,
            )
        # 客户端**自己给的**自由文本与"把选项的 label 抄一份"要分开：
        # 前者是新内容，后者只是一个决定的文字形式。下面拼进请求文本时只认前者。
        typed_free_text = answer.get("free_text")
        label = next(
            (opt.get("label") for opt in (record.clarification or {}).get("options", [])
             if answer.get("answer_id") and opt.get("id") == answer.get("answer_id")),
            None,
        )
        try:
            reply = ClarificationAnswer.model_validate(
                {
                    "question_id": (record.clarification or {}).get("question_id"),
                    "answer_id": answer.get("answer_id"),
                    # 给人看的那一份：自由文本优先，否则用选项 label
                    "free_text": typed_free_text or label,
                    "edits": answer.get("edits"),
                }
            )
        except ValidationError as e:
            # 形状不对（``edits`` 传成数组之类）是**请求**的问题，不是任务的问题。
            # 不接住的话这里会是一个 500——一个本该 400 的输入错误被报成"服务端挂了"，
            # 排查方向直接被带偏。端点上现在也有一层校验（``interface/validation.py``），
            # 这里是第二道：直接调 ``clarify`` 的调用方（测试、内嵌用法）绕过端点时仍要兜住。
            raise DispatcherError(
                "invalid_request",
                f"澄清答复不符合契约：{e.errors()}",
                task_id=task_id,
            ) from e
        record.clarification = None
        record.updated_at = datetime.now(UTC)
        await self._save(record)

        if reply.is_cancel:
            # 用户放弃了。已完成的产出**保留并如实上报**——抽出来的字段即使这次
            # 不入账也不是垃圾，取消不是失败。
            record.status = "cancelled"
            record.ended_at = datetime.now(UTC)
            record.artifacts = record.node_outputs or None
            self._release_media(task_id)
            await self._save(record)
            await self.events.emit(
                task_id, "task.cancelled", {"by": "clarification:cancel"}
            )
            await self._emit_run_log(record)
            return record

        envelope = TaskEnvelope.model_validate(record.envelope)
        cancel = self._cancels.setdefault(task_id, CancellationToken())
        # **只有客户端给的自由文本才并进请求文本。**
        #
        # 拼进去是为了让重新跑的步骤看得到新内容（"按截图的 38.50 记"）。而选项
        # 是一个**决定**，它经 ctx.clarification 直达 handler——把它的 label 也拼进
        # 用户那句话里，会污染绑着 envelope.input.text 的字段。日程就是活例子：
        # title 直接绑原话，拼进去会让日程标题变成
        # "下周三下午三点开会\n（用户澄清：对，就这么建）"。
        if typed_free_text:
            merged = (envelope.input.text or "") + f"\n（用户澄清：{typed_free_text}）"
            envelope = envelope.model_copy(
                update={"input": envelope.input.model_copy(update={"text": merged})}
            )
        prior = dict(record.node_outputs)
        if reply.edits:
            # 用户的修改直接覆盖上一个成功节点的产出——那正是"人工确认"的价值所在
            prior = {**prior, **reply.edits}
            record.node_outputs = prior
            record.artifacts = prior
        try:
            return await self._advance(
                record, envelope, cancel, prior=prior, clarification=reply
            )
        finally:
            # 恢复是内联跑完的（没有后台任务），跑完就出账；媒体引用的去留由
            # ``_advance`` 的 finally 按终态判定。
            self._forget(task_id)

    async def cancel(
        self, task_id: str, *, reason: str = "client_requested", tenant_id: str | None = None
    ) -> TaskRecord:
        record = await self.get(task_id, tenant_id=tenant_id)
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
        # 取消是终态：在册条目与媒体引用当场放掉。后台任务的 done 回调随后还会
        # 走一遍 ``_forget``，两个 pop 都是幂等的。
        self._forget(task_id)
        self._release_media(task_id)
        await self._save(record)
        await self.events.emit(task_id, "task.cancelled", {"by": reason})
        return record

    # ------------------------------------------------------------------
    async def get(self, task_id: str, *, tenant_id: str | None = None) -> TaskRecord:
        """按 id 取任务；给了 ``tenant_id`` 就一并校验归属。

        归属校验放在这里而不是每个端点各写一遍：``get`` 是按 id 取任务的**唯一**
        收口（快照、产物、事件流、澄清、反馈、取消全走它），因此放在这里就不存在
        "新加一个端点忘了校验"这种漏法。

        不匹配一律 ``not_found``：403 等于告诉调用方"这个 id 存在，只是不归你"，
        而那正是要藏起来的信息。

        ``tenant_id`` 可选是为了不带租户上下文的调用方（内嵌使用、单用户本机模式）
        仍然可用——**信任边界在接口层**，那里一定有身份。
        """
        rec = await self.state.get_task(task_id)
        if rec is None or (tenant_id is not None and rec.tenant_id != tenant_id):
            raise DispatcherError("not_found", f"任务不存在：{task_id}", task_id=task_id)
        return rec

    async def list(
        self, *, tenant_id: str = "default", user_id: str | None = None, limit: int = 20
    ) -> list[TaskRecord]:
        return await self.state.list_tasks(tenant_id=tenant_id, user_id=user_id, limit=limit)

    def is_terminal_event_seen(self, task_id: str) -> bool:  # pragma: no cover - 供 SSE 用
        return False


# 参与幂等指纹的字段：**请求的内容**，而不是这一次传输的包装。
#
# 排除项各有理由：``request_id`` 是每次尝试的关联 id（重试本来就会变）；
# ``idempotency_key`` 可能一次写在请求体、一次写在 Idempotency-Key 头；
# ``identity`` 已被服务端按 token 覆盖，客户端自报的那份不参与判定；
# ``client`` 是 app 版本这类遥测——客户端升个版本不该把重试变成 409。
_IDEMPOTENCY_FIELDS = ("parent_task_id", "input", "declared", "constraints", "metadata")


def _idempotency_fingerprint(envelope_dump: dict[str, Any] | None) -> str:
    """请求内容的规范化指纹。

    两侧都用这一个函数算：入参是 ``TaskEnvelope.model_dump(mode="json")`` 的结果，
    与记录里存的那一份同源，因此只要请求内容一致，指纹就一致。
    """
    material = {k: (envelope_dump or {}).get(k) for k in _IDEMPOTENCY_FIELDS}
    canon = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


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
            # 后端报**实际装上的实现的类名**，而不是回显配置值：配置说 sqlite
            # 而进程里跑的是内存实现，正是这轮要消掉的那类静默错配，
            # 因此启动日志必须能证明装的是哪一个。
            "state_backend": type(d.state).__name__,
            "evolution_store": type(d.evolution_store).__name__ if d.evolution_store else None,
            "media_store": type(d.media).__name__,
            "warnings": d.config_warnings,
        },
        ensure_ascii=False,
    )


__all__ = [
    "DEFAULT_SPEC_PATH", "DEFAULT_TEMPLATE_DIR", "Dispatcher", "DispatcherConfig",
    "TERMINAL_EVENTS", "describe_config",
]
