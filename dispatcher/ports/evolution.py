"""自进化的持久化端口。

三样东西，都**不属于**请求路径：运行日志、改进建议、策略版本。它们被 04 读写，
偶尔被管理端点读写，而每个请求都要经过的 ``StateStorePort`` 不该被它们撑胖——
把冷数据混进热路径的接口里，会让每次任务读写都要面对一堆用不到的字段。

**事件日志与运行日志是两个东西**，别混：事件日志（``EventBus``）是任务的实时状态流，
给前端看的、带 ``seq`` 可重放；运行日志是**已结束那一轮运行的聚合摘要**，
给 04 做统计用的。前者是过程，后者是结果；把结果也塞进事件流会让重放变重，
把过程塞进运行日志则会让统计无从下手。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..core.runlog import HumanSignal, RunLog


@runtime_checkable
class RunLogStorePort(Protocol):
    async def append_run_log(self, log: RunLog) -> None: ...

    async def list_run_logs(
        self,
        *,
        tenant_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        policy_version: str | None = None,
        limit: int | None = None,
    ) -> list[RunLog]:
        """按时间窗取运行日志。

        ``policy_version`` 过滤是金丝雀对比用的：要判断一个新版本是不是更好，
        就得能把"新版本产生的那些 run"单独取出来与基线比。
        """
        ...

    async def attach_human_signal(self, task_id: str, signal: HumanSignal) -> bool:
        """把人工信号挂到已经落库的运行日志上，返回是否找到。

        信号总是**晚于**运行本身到达（用户看完结果才改），所以不能指望它
        在写日志时就有。返回 ``False`` 说明对应的 run 已经不在保留期内了——
        那不是错误，是正常的时间差。
        """
        ...

    async def prune_run_logs(self, *, before: datetime) -> int: ...


@runtime_checkable
class SuggestionStorePort(Protocol):
    async def put_suggestion(self, suggestion: dict) -> None: ...

    async def get_suggestion(self, suggestion_id: str) -> dict | None: ...

    async def list_suggestions(
        self, *, status: str | None = None, scope_level: str | None = None, limit: int = 50
    ) -> list[dict]: ...

    async def update_suggestion(self, suggestion_id: str, patch: dict) -> dict | None: ...


@runtime_checkable
class PolicyVersionStorePort(Protocol):
    async def put_version(self, version: dict) -> None: ...

    async def get_version(self, version_id: str) -> dict | None: ...

    async def list_versions(self, *, limit: int = 50) -> list[dict]: ...

    async def active_version(self) -> dict | None:
        """当前生效的版本（``status`` 为 ``active`` 或 ``canary``）。"""
        ...


@runtime_checkable
class EvolutionStorePort(RunLogStorePort, SuggestionStorePort, PolicyVersionStorePort, Protocol):
    """三者的合集。实现可以放在同一个库里——它们都是冷数据，且互相关联。"""


__all__ = [
    "EvolutionStorePort", "PolicyVersionStorePort", "RunLogStorePort", "SuggestionStorePort",
]
