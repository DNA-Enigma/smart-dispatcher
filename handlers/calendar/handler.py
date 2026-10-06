"""日程 handler（参考实现）

**这个文件不在 ``dispatcher/`` 里，这是刻意的。**

接缝的验收标准是"接入一个 handler 需要改动 ``dispatcher/`` 下的文件数为 0"。
只要 handler 的实现住在调度层的包里面，那句话就无从验证——它看起来像插件，
实际上是调度层的一部分。因此这里的每一个文件都可以被删掉、被替换、被搬到
另一个仓库，而调度层一行都不用改（``tests/test_handler_seam.py`` 会机械地检查这件事）。
"""

from __future__ import annotations

import json
from typing import Any

from dispatcher.core.execution import ToolResult
from dispatcher.core.registry import HandlerManifest
from dispatcher.ports.llm import LLMMessage

from ..base import HandlerBase


class CalendarHandler(HandlerBase):
    """日程的示例实现。绝大多数工具是确定性的——这正是它值得存在的理由：
    它验证了"不需要模型的路径"同样顺畅。"""

    def __init__(self, manifest: HandlerManifest) -> None:
        super().__init__(manifest)
        self._events: list[dict[str, Any]] = []

    async def tool_create_event(self, args: dict, ctx: Any) -> ToolResult:
        token = ctx.idempotency_token
        existing = next((e for e in self._events if e.get("token") == token), None)
        if existing:
            return ToolResult(ok=True, output={"event": existing, "idempotent_replay": True})
        ev = {
            "id": f"ev_{len(self._events) + 1}",
            "token": token,
            "title": args.get("title"),
            "start": args.get("start"),
        }
        self._events.append(ev)
        return ToolResult(ok=True, output={"event": ev})

    async def tool_query_free_busy(self, args: dict, ctx: Any) -> ToolResult:
        return ToolResult(
            ok=True,
            output={"busy": [{"start": e["start"], "title": e["title"]} for e in self._events]},
        )

    async def tool_reschedule_event(self, args: dict, ctx: Any) -> ToolResult:
        target = next((e for e in self._events if e["id"] == args.get("event_id")), None)
        if target is None:
            return ToolResult.fail("not_found", "日程不存在", retryable=False)
        target["start"] = args.get("start")
        return ToolResult(ok=True, output={"event": target})

    async def tool_parse_natural_time(self, args: dict, ctx: Any) -> ToolResult:
        """自然语言时间解析——标了 ``text`` 能力，因为它确实需要理解语言。

        "下周三"是哪个周三、"下午三点"是 15 点还是 3 点，这类歧义不适合用规则表穷举。
        """
        res = await ctx.llm(
            [
                LLMMessage.user(
                    "把这句话解析成绝对时间，只输出 JSON："
                    '{"iso": string|null, "ambiguous": boolean, "note": string}'
                    f"\n现在：{ctx.now().isoformat()}\n时区：Asia/Shanghai\n说法：{args.get('text')}"
                )
            ],
            requires=("text",),
            json_mode=True,
            note="parse_natural_time",
        )
        try:
            data = json.loads(res.text.strip().removeprefix("```json").removesuffix("```").strip())
        except Exception:
            return ToolResult.fail("schema_validation_failed", "时间解析输出非法", retryable=False)
        return ToolResult(ok=True, output=data)


