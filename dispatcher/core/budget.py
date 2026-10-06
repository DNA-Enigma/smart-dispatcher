"""成本记账。

**当前是 advisory 模式**：完整记账、超过阈值告警，但**永不在执行中打断任务**。

这是明确的取舍，不是偷懒：先在真实使用里攒出成本数据，让 04 依据数据提出阈值建议，
而不是一开始就用一个拍脑袋的数字硬拦——那样拦错了既没有数据支撑，
用户也只会觉得"它莫名其妙不给我干"。

需要硬拦时把策略里 ``budget.enforcement.mode`` 改成 ``hard`` 即可，代码不用动。

一个实现上的选择：**充电发生在每个节点之后**，不是任务结束时。飞行中记账才能回答
"贵在哪一步"，也才能在 hard 模式下及时停住——事后记账只能做事后诸葛。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Enforcement = Literal["advisory", "hard"]


@dataclass
class Charge:
    """一笔成本。逐 ``(task, subtask)`` 归属，这样成本可以按步骤拆解。"""

    subtask_id: str | None
    amount: float
    currency: str
    note: str = ""


@dataclass
class BudgetState:
    limit: float
    spent: float = 0.0
    warned: bool = False
    charges: list[Charge] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return (self.spent / self.limit) if self.limit > 0 else 0.0

    @property
    def exceeded(self) -> bool:
        return self.limit > 0 and self.spent > self.limit


class BudgetExceeded(Exception):
    """仅在 ``hard`` 模式下、且真的超出上限时抛出。"""

    def __init__(self, state: BudgetState) -> None:
        super().__init__(f"成本 {state.spent:.6f} 超出上限 {state.limit:.6f}")
        self.state = state


class BudgetLedger:
    """按任务累加成本，并在跨过告警线时通知。

    目前是进程内存储。持久化（``GET /v1/usage`` 要按用户/租户跨任务聚合）属于 M6，
    届时换成一个端口实现；接口形状现在按那个目标设计，以免到时候要改调用方。
    """

    def __init__(self, *, enforcement: Enforcement, warn_at_ratio: float, currency: str) -> None:
        self.enforcement = enforcement
        self.warn_at_ratio = warn_at_ratio
        self.currency = currency
        self._tasks: dict[str, BudgetState] = {}
        self._by_user: dict[str, float] = {}
        self._by_tenant: dict[str, float] = {}

    # ------------------------------------------------------------------
    def open(self, task_id: str, *, limit: float) -> BudgetState:
        """开通（或更新）一个任务的预算。**幂等**。

        重复打开**保留已经花掉的钱**，只更新上限。这一点是必需的：
        评估、路由、拆解都发生在计划预算确定之前，那时只能先按每任务默认值开通；
        等决策出来后再用更准的上限更新它。如果这里重置 spent，
        前面几步已经花的钱就会被抹掉——账目凭空变少，而钱是真的花了。
        """
        state = self._tasks.get(task_id)
        if state is None:
            state = BudgetState(limit=limit)
            self._tasks[task_id] = state
        else:
            state.limit = limit
        return state

    def state(self, task_id: str) -> BudgetState | None:
        return self._tasks.get(task_id)

    def spent(self, task_id: str) -> float:
        st = self._tasks.get(task_id)
        return st.spent if st else 0.0

    def warned(self, task_id: str) -> bool:
        st = self._tasks.get(task_id)
        return st.warned if st else False

    def breakdown(self, task_id: str) -> list[Charge]:
        st = self._tasks.get(task_id)
        return list(st.charges) if st else []

    # ------------------------------------------------------------------
    def charge(
        self,
        task_id: str,
        amount: float,
        *,
        subtask_id: str | None = None,
        note: str = "",
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> tuple[bool, bool]:
        """记一笔账。返回 ``(跨过告警线, 超出上限)``。

        **永不因为超限而抛错**（除非调用方显式检查）——advisory 模式的意义就是
        记账而不打断。``hard`` 模式的判断交给 ``assert_within``，由执行器在
        合适的时机调用，而不是在记账的那一刻。
        """
        st = self._tasks.get(task_id)
        if st is None:
            return False, False
        st.spent += amount
        st.charges.append(
            Charge(subtask_id=subtask_id, amount=amount, currency=self.currency, note=note)
        )
        if user_id:
            self._by_user[user_id] = self._by_user.get(user_id, 0.0) + amount
        if tenant_id:
            self._by_tenant[tenant_id] = self._by_tenant.get(tenant_id, 0.0) + amount

        just_warned = False
        if not st.warned and st.limit > 0 and st.ratio >= self.warn_at_ratio:
            st.warned = True
            just_warned = True
        return just_warned, st.exceeded

    def check(self, task_id: str) -> tuple[bool, bool]:
        """只看状态、不记账。返回 ``(首次跨过告警线, 是否超限)``。

        与 ``charge`` 分开，是因为"充电"和"检查"是两件事：执行器在节点之间查一次，
        却不想因此凭空多记一笔账。之前的写法是 ``charge(0.0)`` 来蹭它的返回值——
        用一次金额为零的记账当查询，读代码的人得想一下才明白。
        """
        st = self._tasks.get(task_id)
        if st is None:
            return False, False
        just_warned = False
        if not st.warned and st.limit > 0 and st.ratio >= self.warn_at_ratio:
            st.warned = True
            just_warned = True
        return just_warned, st.exceeded

    def assert_within(self, task_id: str) -> None:
        """``hard`` 模式下由执行器调用。advisory 模式下永远不抛。"""
        if self.enforcement != "hard":
            return
        st = self._tasks.get(task_id)
        if st is not None and st.exceeded:
            raise BudgetExceeded(st)

    # ------------------------------------------------------------------
    def user_total(self, user_id: str) -> float:
        return self._by_user.get(user_id, 0.0)

    def tenant_total(self, tenant_id: str) -> float:
        return self._by_tenant.get(tenant_id, 0.0)


@dataclass
class BudgetHandle:
    """交给 ``DispatchContext`` 的窄接口。

    handler 只能记账、只能看剩余额度——不能改上限、不能看别人的账。
    给窄接口而不是整个 ledger，是因为"handler 不该有能力做的事"最好在类型上就不存在。
    """

    _ledger: BudgetLedger
    task_id: str
    limit: float
    spent: float
    currency: str

    def charge(self, amount: float, *, subtask_id: str | None = None, note: str = "") -> None:
        self._ledger.charge(self.task_id, amount, subtask_id=subtask_id, note=note)
        self.spent = self._ledger.spent(self.task_id)

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.spent)

    def assert_within(self) -> None:
        self._ledger.assert_within(self.task_id)


__all__ = [
    "BudgetExceeded", "BudgetHandle", "BudgetLedger", "BudgetState", "Charge", "Enforcement",
]
