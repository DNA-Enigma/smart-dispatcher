"""DagRunner —— 把计划跑起来。

这里是相对现有实现改进最集中的地方。每一条改进都对应 ``duowei-ai`` 里的一个具体缺陷：

| 改进 | 那边的做法 |
|---|---|
| 动态就绪集 | ``wave1`` / ``wave2`` 是 Python 字面量，拓扑写死 |
| 并发度取自计划 | ``MAX_CONCURRENT_AGENTS = 9`` 是模块常量 |
| 取消传播 | 完全没有——客户端断连后 ``gather`` 还在跑 |
| 有类型的节点失败 | ``return_exceptions=True`` 后把异常用 ``"; "`` 拼成一个字符串 |
| 飞行中成本记账 | 没有；只有任务级的一个总数 |
| 每个节点可独立重试/升级/降级 | 只有整体成败 |

调度与执行刻意分开：``DagRunner`` 只管**何时、以什么并发、失败后怎么办**；
"一个节点具体做什么"由传进来的 ``NodeExecutorFn`` 决定。于是并发语义可以
脱离工具实现单独测——这也是下面那些并发测试能写出来的原因。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .budget import BudgetLedger
from .cancel import CancellationToken, Cancelled
from .eventbus import EventBus
from .execution import ToolResult
from .plan import ExecutionPlan, Node

log = logging.getLogger("dispatcher")

# 节点终态：算出进度时只数这些
TERMINAL_NODE_STATUS = frozenset(
    {"succeeded", "failed", "skipped", "cancelled", "defaulted"}
)


@dataclass
class NodeFailure:
    """有类型的节点失败。**禁止把多个错误拼成一个字符串。**"""

    code: str
    message: str = ""
    retryable: bool = False
    on_failure_applied: str | None = None
    attempts: int = 0


@dataclass
class NodeRun:
    node: Node
    status: str = "pending"
    output: dict[str, Any] | None = None
    attempts: int = 0
    cost: float = 0.0
    failure: NodeFailure | None = None
    escalated_from: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    needs_confirmation: Any = None

    @property
    def latency_ms(self) -> int | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at) * 1000)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_NODE_STATUS


@dataclass
class _AttemptResult:
    """节点跑完后投给主循环的消息。

    用一个 dataclass 而不是元组：元组在第 4 个位置放错东西时不会有任何提示，
    而这个结构后面还要加字段。
    """

    subtask_id: str
    result: ToolResult
    attempts: int
    tier_override: str | None
    # 本节点自身累计的成本，**不是任务总成本**——两者混起来会让"贵在哪一步"
    # 这个问题永远答不出来。
    node_cost: float


@dataclass
class RunReport:
    status: str  # succeeded | failed | cancelled | budget_exceeded | awaiting_clarification
    nodes: dict[str, NodeRun]
    artifacts: dict[str, Any] = field(default_factory=dict)
    spent: float = 0.0
    wall_ms: int = 0
    paused_at: str | None = None  # awaiting_clarification 时停在哪个节点
    error: dict[str, Any] | None = None

    @property
    def progress(self) -> float:
        if not self.nodes:
            return 0.0
        done = sum(1 for n in self.nodes.values() if n.is_terminal)
        return done / len(self.nodes)

    def outputs_by_node(self) -> dict[str, Any]:
        return {k: v.output for k, v in self.nodes.items() if v.output is not None}


class NodeExecutorFn(Protocol):
    """跑**一次**节点尝试。

    ``tier_override`` 非空时表示这是升级后的重试——执行者应当用它替代节点自身
    声明的档位。重试与升级的**决策**在 runner 里，执行者只负责照做。
    """

    def __call__(
        self,
        node: Node,
        *,
        attempt: int,
        tier_override: str | None,
        scope: dict[str, Any],
    ) -> Awaitable[ToolResult]: ...


class _Abort(Exception):
    """内部信号：整张图应当中止。会触发 TaskGroup 取消兄弟节点。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _Pause(Exception):
    """内部信号：任务停在等人（澄清）。**不是失败**，因此不能走 abort 那条路。"""

    def __init__(self, subtask_id: str) -> None:
        self.subtask_id = subtask_id
        super().__init__(subtask_id)


class DagRunner:
    def __init__(
        self,
        *,
        events: EventBus,
        ledger: BudgetLedger,
        policy: Any,
        executor: NodeExecutorFn,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._events = events
        self._ledger = ledger
        self._policy = policy
        self._execute = executor
        self._cancel = cancellation or CancellationToken()

    # ------------------------------------------------------------------
    async def run(
        self,
        plan: ExecutionPlan,
        *,
        task_id: str,
        scope: dict[str, Any],
        max_escalations: int = 0,
        prior: dict[str, Any] | None = None,
    ) -> RunReport:
        """跑一张计划。

        ``prior`` 是**已经成功过的节点产出**，用于从暂停处恢复：澄清答复后不该
        从头重跑——已完成的抽取、归类、查重都还有效，重跑既浪费又可能产出不同结果。
        """
        started = time.monotonic()
        runs: dict[str, NodeRun] = {n.subtask_id: NodeRun(node=n) for n in plan.nodes}
        for sid, output in (prior or {}).items():
            if sid in runs:
                runs[sid].status = "succeeded"
                runs[sid].output = output
        self._ledger.open(task_id, limit=plan.plan_budget.max_cost)
        # 升级次数来自决策的 escalation_rule，由调用方传入——它是**硬界**，
        # 不受 LLM 影响，否则升级会成环。
        escalation_left = max_escalations

        # 就绪 = 依赖全部成功（或按默认值/跳过而"完成"）。
        # 恢复时，之前成功过的节点直接算作已完成。
        done_ok: set[str] = set(prior or {})
        completed: set[str] = set(prior or {})

        # 已完成节点的产出。**$ref 就是靠它解析的**——少了这张表，
        # 下游节点的输入会全部解析失败或解析成空，而任务表面上"成功"。
        # 恢复时把 prior 也放进来，否则重跑的第一步拿不到上游的产出。
        outputs: dict[str, Any] = dict(prior or {})

        abort_reason: str | None = None
        pause_at: str | None = None

        running: set[str] = set()

        def ready_nodes() -> list[Node]:
            out = []
            for n in plan.nodes:
                sid = n.subtask_id
                if sid in completed or sid in running:
                    continue
                if all(dep in done_ok for dep in n.depends_on):
                    out.append(n)
            return out

        async def run_node(node: Node) -> _AttemptResult:
            """跑一个节点（含重试与升级）。结果直接作为返回值——不经队列。

            队列会多一层"投递"语义，而调度器本来就在等任务本身；中间加一个队列
            只是让异常与取消的传播路径变长（取消一个已投递的任务时，
            结果已经在队列里了，读出来还得再判断一次该不该用）。
            """
            nonlocal escalation_left
            sid = node.subtask_id
            run = runs[sid]
            run.started_at = time.monotonic()
            tier_override: str | None = None
            max_attempts = 1 + (node.retry.max if node.retry else 0)
            node_cost = 0.0

            await self._events.emit(
                task_id, "subtask.started",
                {"subtask_id": sid, "name": node.name, "attempt": 1,
                 "tier": node.model_tier, "tool": node.tool},
                subtask_id=sid,
            )

            attempt = 1
            # **实际执行了几次**，与 ``attempt`` 分开记。
            #
            # ``attempt`` 是"第几次重试"，升档走的是一条不增加 attempt 的旁路
            # （见下面的 continue），所以它数不出升档那一趟。原先 ``attempts`` 报的就是
            # ``attempt``，于是"跑了两趟、其中一趟是升档"在 node_runs 里显示成 1 次——
            # 排查的人看到的执行次数比真实发生的少，而这正是复盘时最容易走错的一步。
            executions = 0
            result = ToolResult.fail("handler_error", "节点未执行")
            while True:
                executions += 1
                self._cancel.raise_if_cancelled()
                # 每次尝试都重新拼 scope：重试期间上游产出可能已经变了
                # （虽然本节点的依赖已固定，但把最新快照传下去不会有坏处，
                # 而缓存住一份旧的会引入难以复现的差异）。
                scoped = {**scope, **outputs}
                try:
                    result = await self._execute(
                        node, attempt=attempt, tier_override=tier_override, scope=scoped
                    )
                except (Cancelled, asyncio.CancelledError):
                    raise
                except Exception as e:
                    # 执行者抛了未包装的异常：包成有类型的失败。
                    # 让它冒泡出去会让 TaskGroup 变成一堆嵌套异常组，
                    # 而事件流里只会留下一个"任务挂了"——那不叫可观测。
                    #
                    # 若这个异常是从 LLM 端口冒上来的（handler 直接调 ctx.llm 而
                    # 没接住 LLMError），供应商原文在 ``provider_detail`` 里；它不进
                    # ``str(e)``，因此不会随节点失败消息回客户端，只在这里进日志。
                    provider_detail = getattr(e, "provider_detail", None)
                    if provider_detail:
                        log.warning(
                            "节点 %s 未包装异常 task=%s（上游原文：%s）",
                            sid, task_id, provider_detail,
                        )
                    result = ToolResult.fail("handler_error", str(e), retryable=False)

                run.attempts = executions
                if result.cost:
                    node_cost += result.cost
                    self._ledger.charge(
                        task_id, result.cost,
                        subtask_id=sid, note=f"tool:{node.tool or node.role}",
                    )

                # 档位升级：输出不合 schema 时换更强的模型重试一次。
                # 条件是"可升级 + 还有额度 + 本次尚未升级过"——三个都必须满足，
                # 因为无界升级就是无界花钱。
                #
                # 升档的**起点必须是真的在用的那个档位**。``escalation_target`` 是一张
                # 有限的对映表（``thresholds.escalation_tiers``），起点报错就会升到同一个
                # 档位：算是一次"升档"，实际什么都没变，白花一次重试的钱，
                # 而且在事件流里留下一句"已升档"的假话（2026-10-07 的 extract 故障）。
                # 因此能不能升、升到哪，先算清楚，**算不出去就不算数**：
                # 额度只在真的换了档位时才扣。
                if (
                    not result.ok
                    and result.failure is not None
                    and result.failure.code == "schema_validation_failed"
                    and escalation_left > 0
                    and tier_override is None
                ):
                    base_tier = node.model_tier or self._policy.model_tier_ids[0]
                    target_tier = self._policy.escalation_target(base_tier)
                    if target_tier != base_tier:
                        escalation_left -= 1
                        tier_override = target_tier
                        run.escalated_from = base_tier
                        await self._events.emit(
                            task_id, "task.escalated",
                            {"from_tier": base_tier, "to_tier": tier_override,
                             "reason": "schema_validation_failed", "subtask_id": sid},
                            subtask_id=sid,
                        )
                        continue

                if result.ok:
                    break
                if result.failure is None or not result.failure.retryable:
                    break
                if attempt >= max_attempts:
                    break

                backoff = (node.retry.backoff_ms if node.retry else 0) / 1000.0
                next_backoff = backoff * (2 ** (attempt - 1))
                await self._events.emit(
                    task_id, "subtask.retrying",
                    {"subtask_id": sid, "attempt": attempt,
                     "next_backoff_ms": int(next_backoff * 1000),
                     "error": {"code": result.failure.code, "retryable": True}},
                    subtask_id=sid,
                )
                if next_backoff > 0:
                    await asyncio.sleep(next_backoff)
                attempt += 1

            return _AttemptResult(
                subtask_id=sid, result=result, attempts=executions,
                tier_override=tier_override, node_cost=node_cost,
            )

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        # 显式任务管理，**不用** asyncio.TaskGroup。
        #
        # TaskGroup 会在 __aexit__ 里把子任务的异常包成 ExceptionGroup，
        # 于是"中止"和"暂停"这两个控制流信号会被裹进异常组，
        # 让 `except _Abort` / `except _Pause` 形同虚设——调试时表现为
        # "明明 raise 了却走进了另一个分支"。更根本的是它的语义是
        # "等所有子任务结束"，而动态调度需要的是"随时加任务、随时全停"。
        #
        # 显式管理这三件事都直白：spawn 时 create_task，中止时逐个 cancel，
        # 收尾时 gather(return_exceptions=True) 等它们真的停下来。
        # ------------------------------------------------------------------
        tasks: dict[asyncio.Task, str] = {}

        def spawn() -> None:
            for node in ready_nodes():
                if len(tasks) >= plan.max_parallelism:
                    break
                t = asyncio.create_task(run_node(node))
                tasks[t] = node.subtask_id
                running.add(node.subtask_id)

        cancelled = False
        try:
            spawn()
            while tasks:
                self._cancel.raise_if_cancelled()
                waiter = asyncio.ensure_future(self._cancel.wait())
                done, _ = await asyncio.wait(
                    set(tasks) | {waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                waiter.cancel()
                finished = [t for t in done if t is not waiter]
                if not finished and waiter in done:
                    raise Cancelled(f"任务已取消：{self._cancel.reason}")

                for t in finished:
                    sid = tasks.pop(t)
                    running.discard(sid)
                    try:
                        msg: _AttemptResult = t.result()
                    except (Cancelled, asyncio.CancelledError):
                        raise
                    except Exception as e:  # run_node 兜住了大多数；这里兜底
                        msg = _AttemptResult(
                            subtask_id=sid,
                            result=ToolResult.fail("handler_error", str(e)),
                            attempts=runs[sid].attempts or 1,
                            tier_override=None,
                            node_cost=0.0,
                        )
                    await self._apply(msg, plan, runs, task_id, completed, done_ok, outputs)
                    if runs[sid].failure is not None and runs[sid].failure.on_failure_applied == "fail_task":
                        abort_reason = (
                            f"节点 {sid} 失败："
                            f"{runs[sid].failure.message or runs[sid].failure.code}"
                        )
                    if runs[sid].needs_confirmation is not None:
                        pause_at = sid
                        break

                    # 预算：只查状态，不凭空记账
                    just_warned, exceeded = self._ledger.check(task_id)
                    st = self._ledger.state(task_id)
                    if exceeded or just_warned:
                        await self._events.emit(
                            task_id,
                            "budget.exceeded"
                            if (exceeded and self._ledger.enforcement == "hard")
                            else "budget.warning",
                            {"scope": "task",
                             "spent": st.spent if st else 0.0,
                             "limit": st.limit if st else 0.0,
                             "currency": self._ledger.currency,
                             "enforcement": self._ledger.enforcement},
                        )
                        # advisory 模式下超限**不中断**——这正是这个模式的全部意义
                        if exceeded and self._ledger.enforcement == "hard":
                            abort_reason = "成本超出上限"

                if abort_reason or pause_at:
                    break
                if len(tasks) < plan.max_parallelism:
                    spawn()

        except Cancelled:
            # 取消是一条正常路径，不是错误：客户端退出页面、用户点了取消。
            # 让它冒泡出去会把"取消"变成一个 500，而调用方其实只想知道"停了"。
            cancelled = True
        finally:
            if tasks:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        wall = int((time.monotonic() - started) * 1000)
        spent = self._ledger.spent(task_id)

        if cancelled:
            for run in runs.values():
                if not run.is_terminal:
                    run.status = "cancelled"
            await self._events.emit(
                task_id, "task.cancelled", {"by": self._cancel.reason or "unknown"}
            )
            return RunReport(
                status="cancelled", nodes=runs, spent=spent, wall_ms=wall
            )

        # 循环退出后仍非终态的节点一定是永远不就绪的。统一在这里标记 + 发事件——
        # 早先在循环里提前标记过一次，结果是它们变成终态、这里不再发事件，
        # "为什么这步没跑"就查不到了。暂停是例外：任务只是停下来等人，不是死了。
        if pause_at is None:
            await self._mark_unreachable(runs, plan, task_id)

        if pause_at:
            return RunReport(
                status="awaiting_clarification", nodes=runs, spent=spent,
                wall_ms=wall, paused_at=pause_at,
            )
        if abort_reason:
            return RunReport(
                status="failed", nodes=runs, spent=spent, wall_ms=wall,
                error={
                    "type": "https://smart-dispatcher/errors/handler-error",
                    "title": "Task failed",
                    "status": 502,
                    "code": "handler_error",
                    "detail": abort_reason,
                    "retryable": False,
                    "request_id": "",
                    "task_id": task_id,
                },
            )

        failed = [r for r in runs.values() if r.status == "failed"]
        return RunReport(
            status="failed" if failed else "succeeded",
            nodes=runs,
            artifacts={k: v.output for k, v in runs.items() if v.output is not None},
            spent=spent,
            wall_ms=wall,
            error=None if not failed else {
                "type": "https://smart-dispatcher/errors/handler-error",
                "title": "Task failed",
                "status": 502,
                "code": "handler_error",
                "detail": f"节点 {failed[0].node.subtask_id} 失败："
                          f"{failed[0].failure.message if failed[0].failure else ''}",
                "retryable": False,
                "request_id": "",
                "task_id": task_id,
            },
        )

    # ------------------------------------------------------------------
    async def _apply(
        self,
        msg: _AttemptResult,
        plan: ExecutionPlan,
        runs: dict[str, NodeRun],
        task_id: str,
        completed: set[str],
        done_ok: set[str],
        outputs: dict[str, Any],
    ) -> None:
        """把一个节点的结果落进状态，并发相应的事件。"""
        sid = msg.subtask_id
        node = plan.node(sid)
        assert node is not None
        run = runs[sid]
        result = msg.result
        run.ended_at = time.monotonic()
        run.cost = msg.node_cost

        if result.needs_confirmation is not None:
            # 停在等人——**不是失败**。整图暂停，答复后从这个节点继续。
            run.status = "pending"
            run.needs_confirmation = result.needs_confirmation
            await self._events.emit(
                task_id, "clarification.needed",
                {"subtask_id": sid,
                 "question": result.needs_confirmation.question,
                 "options": result.needs_confirmation.options,
                 "blocking": result.needs_confirmation.blocking},
                subtask_id=sid,
            )
            return

        if result.ok:
            run.status = "succeeded"
            run.output = result.output
            run.escalated_from = node.model_tier if msg.tier_override else None
            completed.add(sid)
            done_ok.add(sid)
            # 产出进表，供下游 $ref 解析
            if result.output is not None:
                outputs[sid] = result.output
            await self._events.emit(
                task_id, "subtask.completed",
                {"subtask_id": sid, "output": result.output or {},
                 "cost": {"amount": msg.node_cost, "currency": self._ledger.currency},
                 "latency_ms": run.latency_ms,
                 "escalated_from": run.escalated_from},
                subtask_id=sid,
            )
            return

        failure = result.failure
        run.failure = NodeFailure(
            code=failure.code if failure else "handler_error",
            message=failure.message if failure else "",
            retryable=failure.retryable if failure else False,
            on_failure_applied=node.on_failure,
            attempts=msg.attempts,
        )
        if node.on_failure == "continue_with_default":
            run.status = "defaulted"
            run.output = node.default_output
            completed.add(sid)
            done_ok.add(sid)
            # 默认值同样进表：下游引用的正是这个替代值，而不是"什么都没有"
            if node.default_output is not None:
                outputs[sid] = node.default_output
        elif node.on_failure == "skip":
            run.status = "skipped"
            completed.add(sid)
            done_ok.add(sid)
        else:
            run.status = "failed"
            completed.add(sid)

        await self._events.emit(
            task_id, "subtask.failed",
            {"subtask_id": sid,
             "error": {"code": run.failure.code, "retryable": run.failure.retryable},
             "on_failure_applied": node.on_failure},
            subtask_id=sid,
        )

    async def _mark_unreachable(
        self, runs: dict[str, NodeRun], plan: ExecutionPlan, task_id: str
    ) -> None:
        """把永远不就绪的节点标记为跳过并**发事件**。

        一个节点凭空消失比它失败更让人困惑——"为什么这步没跑"是使用者一定会问的，
        答案必须在事件流里找得到。

        原因**按节点分别判定**，不用一个统一的标签：依赖真的失败了就是
        ``upstream_failed``，任务因别的原因整体停住（比如 hard 模式超预算）就是
        ``task_aborted``。把两者混成一个码，会让排查时看不出到底发生了什么。
        """
        def root_cause(sid: str, seen: frozenset[str] = frozenset()) -> str:
            node = plan.node(sid)
            if node is None or sid in seen:
                return "task_aborted"
            for dep in node.depends_on:
                r = runs.get(dep)
                if r is not None and r.status == "failed":
                    return "upstream_failed"
                # 上游被跳过、而它自己就是因为上游失败被跳过的 → 原因继续往上传。
                # 不传的话，A→B→C 里的 C 会被标成 task_aborted，
                # 而真实原因是 A 失败——排查时会往错的方向找。
                if r is not None and r.failure is not None and r.failure.code in {
                    "upstream_failed", "task_aborted"
                } and r.failure.code == "upstream_failed":
                    return "upstream_failed"
                if r is not None and not r.is_terminal:
                    inner = root_cause(dep, seen | {sid})
                    if inner == "upstream_failed":
                        return inner
            return "task_aborted"

        for run in runs.values():
            if run.is_terminal:
                continue
            reason = root_cause(run.node.subtask_id)
            run.status = "skipped"
            run.failure = NodeFailure(
                code=reason,
                message="依赖的节点未产出，本节点无法执行"
                if reason == "upstream_failed"
                else "任务已中止，本节点未执行",
                on_failure_applied="skip",
            )
            await self._events.emit(
                task_id, "subtask.failed",
                {"subtask_id": run.node.subtask_id,
                 "error": {"code": reason, "retryable": False},
                 "on_failure_applied": "skip"},
                subtask_id=run.node.subtask_id,
            )


__all__ = [
    "DagRunner", "NodeExecutorFn", "NodeFailure", "NodeRun", "RunReport",
    "TERMINAL_NODE_STATUS",
]
