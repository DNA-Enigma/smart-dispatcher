"""Capability handler 注册表。

形状泛化自 ``ai-workmate/server/app/agent/toolkit.py`` 的 ``@tool`` 注册表
与 ``base.py::validate_tools()`` 的启动期 fail-fast——那里已经验证过这套写法可行。

M1 阶段还没有真正的 handler 实现（记账 handler 在 M4），所以这里只承载**声明**：
能力目录、工具白名单、副作用类型。评估器需要能力目录才能只从真实存在的能力里选；
守卫需要工具白名单才能做子集判定。

两件事值得注意：

1. **注册期 fail-fast。** 工具的输出 schema 不可解析、能力无人满足、
   flow 引用了未声明的工具——这些在**启动时**报错，不留到运行期当惊喜。
2. **注册表不知道任何领域语义。** 它只存集合，回答"这个 handler 有哪些工具"。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from .errors import DispatcherError
from .yamlio import load_yaml

if TYPE_CHECKING:
    from ..ports.handler import CapabilityHandler

Strict = ConfigDict(extra="forbid")


class ToolDecl(BaseModel):
    """一个工具的声明。注意 ``requires_capabilities`` —— 它是能力名，不是模型名。

    handler 说"我需要视觉抽取能力"，档位与模型的映射由调度层完成。
    这是"禁止硬编码模型"在结构上的强制：这个字段里没有地方可写模型标识。
    """

    model_config = Strict
    name: str
    description: str | None = None
    side_effects: str = "none"  # none | read | write
    requires_capabilities: list[str] = Field(default_factory=list)
    idempotent: bool = False
    input_schema: dict | None = None
    output_schema_ref: str | None = None


class HandlerManifest(BaseModel):
    model_config = Strict
    handler_id: str
    version: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[ToolDecl] = Field(default_factory=list)
    flows: list[str] = Field(default_factory=list)
    required_ports: list[str] = Field(default_factory=list)
    config_schema: dict | None = None


class HandlerRegistry:
    """已注册 handler 的集合。构造之后不可变。"""

    def __init__(self, manifests: list[HandlerManifest]) -> None:
        self._manifests: dict[str, HandlerManifest] = {}
        self._handlers: dict[str, CapabilityHandler] = {}
        for m in manifests:
            self._register(m)

    def _register(self, m: HandlerManifest) -> None:
        if m.handler_id in self._manifests:
            raise DispatcherError(
                "handler_error", f"handler_id 重复：{m.handler_id}",
                context={"handler_id": m.handler_id},
            )
        names = [t.name for t in m.tools]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise DispatcherError(
                "handler_error", f"handler {m.handler_id} 内工具名重复：{dupes}",
                context={"handler_id": m.handler_id, "duplicates": dupes},
            )
        # 工具的能力需求必须能在某个档位上得到满足——否则这个工具永远不可执行，
        # 而失败会发生在运行期最不好的时刻。启动期就报出来。
        for t in m.tools:
            if t.side_effects not in {"none", "read", "write"}:
                raise DispatcherError(
                    "handler_error",
                    f"工具 {m.handler_id}.{t.name} 的 side_effects={t.side_effects!r} 非法",
                )
        self._manifests[m.handler_id] = m

    # ------------------------------------------------------------------
    # 可执行实现
    #
    # 声明与实现刻意分开：调度层的决策、校验、提示词渲染**只依赖声明**，
    # 一次都不需要知道实现长什么样。因此注册表可以在只有声明的情况下正常服务
    # 评估与路由（M1 就是这样），执行时才要求实现存在。
    # ------------------------------------------------------------------
    def bind(self, handler: CapabilityHandler) -> None:
        """挂上一个可执行实现。它的 ``manifest`` 必须已注册且一致。"""
        hid = handler.manifest.handler_id
        declared = self._manifests.get(hid)
        if declared is None:
            raise DispatcherError(
                "handler_error",
                f"handler {hid} 尚未声明就先绑定了实现——声明是注册的前提",
                context={"handler_id": hid},
            )
        if declared.model_dump() != handler.manifest.model_dump():
            # 声明与实现不一致时，调度层的决策会基于一份假的声明做出。
            # 这比"实现缺失"危险：缺失会报错，不一致会安静地做错事。
            raise DispatcherError(
                "handler_error",
                f"handler {hid} 的实现与已注册的声明不一致",
                context={"handler_id": hid},
            )
        self._handlers[hid] = handler

    def executable(self, handler_id: str) -> CapabilityHandler | None:
        return self._handlers.get(handler_id)

    def has_executable(self, handler_id: str) -> bool:
        return handler_id in self._handlers

    @property
    def executable_ids(self) -> frozenset[str]:
        return frozenset(self._handlers)

    # -- 查询：全部是集合操作，无语义 -------------------------------------
    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._manifests)

    def manifest(self, handler_id: str) -> HandlerManifest | None:
        return self._manifests.get(handler_id)

    def tool_names(self, handler_id: str) -> frozenset[str]:
        m = self._manifests.get(handler_id)
        return frozenset(t.name for t in m.tools) if m else frozenset()

    def tool_map(self) -> dict[str, frozenset[str]]:
        """守卫用的 {handler_id: 工具名集合}。"""
        return {h: self.tool_names(h) for h in self._manifests}

    @property
    def schema_refs(self) -> frozenset[str]:
        """注册表里全部 ``output_schema_ref`` 的集合。

        ``Node.output_schema_ref`` 是**模型产出**（自由拆解那条路上由 LLM 写），
        而它会被填进角色的 system 槽。集合校验需要一份可信的成员名单，这份名单
        只能来自人写的声明——就是这份集合。工具的 ``output_schema_ref`` 声明的
        是"这个工具会产出什么形状"，节点引用它等于引用一份已声明的契约。

        空集合是**有意义的**：一个没有声明任何输出 schema 的注册表里，任何引用
        都不合法。不因为"集合为空"就放行——那正好是"没得校验就默认通过"。
        """
        return frozenset(
            t.output_schema_ref for _, t in self.all_tool_decls() if t.output_schema_ref
        )

    def capability_map(self) -> dict[str, frozenset[str]]:
        """守卫用的 {handler_id: 能力名集合}。

        与 ``tool_map`` 并列：**归属判定有两条可用的集合**。工具名能定归属，
        能力名同样能——能力名按约定带领域前缀，交集判定与"这组工具归谁"一样是纯子集运算。
        """
        return {h: frozenset(m.capabilities) for h, m in self._manifests.items()}

    def all_capabilities(self) -> frozenset[str]:
        out: set[str] = set()
        for m in self._manifests.values():
            out.update(m.capabilities)
        return frozenset(out)

    def all_tool_decls(self) -> list[tuple[str, ToolDecl]]:
        return [(h, t) for h, m in self._manifests.items() for t in m.tools]

    def tools_requiring(self, capability: str) -> list[str]:
        return [t.name for _, t in self.all_tool_decls() if capability in t.requires_capabilities]

    def capability_catalog_for_prompt(self) -> str:
        """渲染成给评估器读的能力目录。

        评估器只能从这份清单里挑 ``candidate_capabilities``——这就是插件接缝：
        接一个新领域，评估器立刻"看得见"它的能力，不需要改评估器。
        """
        lines: list[str] = []
        for hid in sorted(self._manifests):
            m = self._manifests[hid]
            lines.append(f"{hid} ({m.version})")
            for cap in m.capabilities:
                lines.append(f"  - {cap}")
        return "\n".join(lines)

    def tool_catalog_for_prompt(self) -> str:
        """渲染成给路由器读的可用工具清单，含副作用类型。"""
        lines: list[str] = []
        for hid in sorted(self._manifests):
            m = self._manifests[hid]
            if not m.tools:
                continue
            lines.append(f"{hid}:")
            for t in m.tools:
                caps = ",".join(t.requires_capabilities) or "-"
                lines.append(
                    f"  {t.name}({t.side_effects}; needs={caps}; idempotent={str(t.idempotent).lower()})"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
def load_manifests(directory: Path) -> list[HandlerManifest]:
    """从一个目录加载全部 handler 清单（``*.yaml``）。

    声明从 ``config/handlers.yaml`` 列出的路径读入（见 ``plugins.py``）。
    **调度层不知道任何 handler 的名字**——那正是接缝成立的标志。
    """
    if not directory.exists():
        raise DispatcherError("handler_error", f"handler 清单目录不存在：{directory}")
    out: list[HandlerManifest] = []
    for p in sorted(directory.glob("*.yaml")):
        try:
            out.append(HandlerManifest.model_validate(load_yaml(p)))
        except Exception as e:
            raise DispatcherError("handler_error", f"清单 {p.name} 校验失败：{e}") from e
    return out


__all__ = ["HandlerManifest", "HandlerRegistry", "ToolDecl", "load_manifests"]
