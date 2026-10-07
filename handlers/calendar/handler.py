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
        """新建日程。**时间有歧义时停下来问，而不是拿一个猜的时间直接建。**

        ``ambiguous`` 由模板从 ``parse_natural_time`` 的产出绑进来
        （``config/flow_templates/schedule_parse_to_create.yaml``）——一个工具看不到
        另一个工具的产出，这条数据依赖必须在模板里显式声明。

        为什么值得停下来：日程建错了用户**往往到点才发现**，那时补救比现在问一句贵得多。
        答复经 ``ctx.clarification`` 送达（用户答了什么，见 core/execution.py 的
        ``ClarificationAnswer``）：

        * ``confirm`` —— 就按解析出的时间建；
        * ``cancel``  —— 由**调度层**处理，任务直接 cancelled，这个方法不会被调用；
        * 其它答复（选了"改一下时间"却没给出新时间、或自由文本）—— **明确失败**，
          不重复问同一个问题，也不拿旧时间去建。用户想改时间时应当由客户端把改好的
          解析结果放进 ``clarify`` 的 ``edits``（那会把 ``ambiguous`` 覆盖掉，
          于是这里直接走"建"那条路）。
        """
        token = ctx.idempotency_token
        existing = next((e for e in self._events if e.get("token") == token), None)
        if existing:
            return ToolResult(ok=True, output={"event": existing, "idempotent_replay": True})

        if args.get("ambiguous"):
            answer = ctx.clarification
            if answer is None:
                return ToolResult.confirm(
                    f"我把时间理解成 {args.get('start')}，对吗？",
                    [{"id": "confirm", "label": "对，就这么建"},
                     {"id": "cancel", "label": "不用建了"}],
                    partial={"title": args.get("title"), "start": args.get("start")},
                )
            if not _is_confirmation(answer):
                return ToolResult.fail(
                    "clarification_not_actionable",
                    "用户表示要改时间，但没有给出修改后的时间；"
                    "请在 clarify 的 edits 里给出新的解析结果（如 "
                    '{"parse": {"iso": "...", "ambiguous": false}}），'
                    "或选择 confirm / cancel。",
                    retryable=False,
                )

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


def _is_confirmation(answer: Any) -> bool:
    """用户的答复是不是"就按这个来"。

    认答案的 **id**，不认 label——label 是给人看的文案，改一个字不该改变行为。
    """
    return str(getattr(answer, "answer_id", None) or "").strip().lower() == "confirm"


