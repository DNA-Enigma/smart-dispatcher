"""记账 handler（参考实现）

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
from dispatcher.ports.llm import LLMMessage

from ..base import HandlerBase
from .ports import InMemoryLedger, LedgerPort

# 用户没配分类词表时的兜底。**真实词表来自 handler 配置**（ctx.config.categories），
# 那是用户自己的体系；写死在代码里就等于替用户决定了他怎么记账。
FALLBACK_CATEGORIES = ["餐饮", "交通", "购物", "居住", "其他"]


class BookkeepingHandler(HandlerBase):
    """记账 handler 的**参考实现**。

    领域数据通过 ``LedgerPort`` 存取，默认用随附的内存实现。
    你自己的应用实现那个端口、把真实数据库接进去即可——
    **账本 schema 属于消费端，不属于这里**。

    构造函数接受一个可选的 ``ledger``，这就是接自己数据库的入口::

        from handlers.bookkeeping.handler import BookkeepingHandler
        from handlers.bookkeeping.ports import LedgerPort

        class MyLedger(LedgerPort):   # 你的真实数据库
            ...

        Dispatcher.build(DispatcherConfig(handlers=[BookkeepingHandler(manifest, ledger=MyLedger())]))

    ``manifest`` 由插件加载器注入（见 ``config/handlers.yaml``），不需要自己构造。
    """

    def __init__(self, manifest, ledger: LedgerPort | None = None) -> None:
        super().__init__(manifest)
        self._ledger: LedgerPort = ledger or InMemoryLedger()
        self._merchants: dict[str, str] = {}

    def _categories(self, ctx) -> list[str]:
        """分类词表从**用户配置**来，不是代码里的常量。

        调度层会把该用户的 handler 配置放在 ``ctx.config`` 里并已按其
        ``config_schema`` 校验过，所以这里可以放心直接用。
        """
        cats = (ctx.config or {}).get("categories")
        return [str(c) for c in cats] if isinstance(cats, list) and cats else FALLBACK_CATEGORIES

    # -- 需要视觉：通过能力名要模型，不问模型是谁 -------------------------
    async def tool_extract_receipt_fields(self, args: dict, ctx: Any) -> ToolResult:
        media_id = args.get("media_ref")
        if not media_id:
            return ToolResult.fail("bad_input", "缺少 media_ref", retryable=False)
        uri = await ctx.media.data_uri(str(media_id))
        hint = args.get("text_hint") or ""
        prompt = (
            # 字段集与 schemas/bookkeeping_receipt_fields.json 一致。
            # **两边必须同步**：消费端照契约建了表，少产一个字段就等于那一列永远是空。
            "从这张凭证里抽取字段，只输出 JSON："
            '{"amount": number|null, "currency": string|null, "merchant": string|null, '
            '"datetime": string|null, "payment_method": string|null, '
            '"direction": "expense"|"income"|null, "category": string|null, '
            '"confidence": number, "notes": string|null}'
            f"\n用户附言：{hint}"
            "\n读不到的字段填 null，**不要编造**——尤其方向与金额，猜错会污染账目。"
            "\ndirection 判不出时填 null，不要默认成 expense。"
        )
        res = await ctx.llm(
            [LLMMessage.user_with_images(prompt, [(uri, "凭证")])],
            requires=("vision.extract", "text"),
            json_mode=True,
            note="extract_receipt_fields",
        )
        try:
            data = json.loads(res.text.strip().removeprefix("```json").removesuffix("```").strip())
        except Exception:
            return ToolResult.fail(
                "schema_validation_failed", "模型输出不是合法 JSON", retryable=False
            )
        if not isinstance(data, dict) or data.get("amount") is None:
            return ToolResult.fail(
                "schema_validation_failed", "缺少金额字段", retryable=False
            )
        return ToolResult(ok=True, output=data)

    async def tool_crop_and_zoom(self, args: dict, ctx: Any) -> ToolResult:
        # 真实实现会做图像裁剪；示例只回一个标记，把"看清楚了"这件事表达出来
        return ToolResult(ok=True, output={"region": args.get("region") or "full", "zoomed": True})

    async def tool_read_media_region(self, args: dict, ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={"text": args.get("hint") or ""})

    # -- 不需要视觉，但仍需要判断 ----------------------------------------
    async def tool_normalize_merchant(self, args: dict, ctx: Any) -> ToolResult:
        raw = str(args.get("merchant_raw") or "").strip()
        if not raw:
            return ToolResult(ok=True, output={"merchant": "未知", "category": "其他"})
        known = self._merchants.get(raw)
        if known:
            return ToolResult(ok=True, output={"merchant": raw, "category": known})
        res = await ctx.llm(
            [
                LLMMessage.user(
                    f"把商户名归入下列分类之一：{self._categories(ctx)}。"
                    f'只输出 JSON：{{"merchant": string, "category": string}}。'
                    f"\n商户名：{raw}"
                )
            ],
            requires=("text",),
            json_mode=True,
            note="normalize_merchant",
        )
        try:
            data = json.loads(res.text.strip().removeprefix("```json").removesuffix("```").strip())
        except Exception:
            data = {"merchant": raw, "category": "其他"}
        self._merchants[raw] = data.get("category", "其他")
        return ToolResult(ok=True, output=data)

    # -- 纯查表：不碰模型 -------------------------------------------------
    async def tool_lookup_merchant(self, args: dict, ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={"category": self._merchants.get(str(args.get("name") or ""))})

    async def tool_categorize_merchants(self, args: dict, ctx: Any) -> ToolResult:
        """把一批商户名归类——**一份名单一次调用**，不是一条商户一次。

        与 ``normalize_merchant`` 的差别是基数而不是能力。导入流水之后要归类的
        陌生商户常常几十个，逐条调用既慢又贵，而且每条的判断标准会在几十次
        往返之间漂移——同一批商户可能被分到两个不同的类里。

        两条纪律：

        * **分类只能落在用户自己的体系里**（``ctx.config.categories``）。模型给出
          体系外的分类名时**丢掉这条**而不是就近改写成别的分类——消费端就是按
          "认不出来时返回空表，绝不猜"来设计界面的。
        * **已经见过的商户不再问模型**。这条路与 ``normalize_merchant`` 共用一张
          本地表，于是同一批流水里重复出现的商户名，判断标准与逐条归类时一致。
        """
        raw = args.get("merchants")
        if isinstance(raw, str):
            # 参数抽取偶尔会把一个名单压成一个字符串（逗号或顿号分隔）。
            # 直接判 bad_input 会把一个能救的输入丢掉，这里按分隔符再切一次。
            raw = [x for x in raw.replace("、", ",").replace("，", ",").split(",") if x.strip()]
        if not isinstance(raw, list) or not raw:
            return ToolResult.fail(
                "bad_input",
                f"merchants 必须是非空数组，收到 {raw!r}",
                retryable=False,
            )
        names = [str(m).strip() for m in raw if str(m).strip()]
        if not names:
            return ToolResult.fail("bad_input", "merchants 里没有有效的商户名", retryable=False)

        categories = self._categories(ctx)
        # 判断不了时的落点必须**由配置决定**，不能在代码里写死一个分类名——
        # 词表是用户的（ctx.config.categories）。"其他"是随附兜底词表里的那个，
        # 用户自己的词表里没有它就退回最后一个，而不是硬塞一个它不认识的分类。
        fallback = "其他" if "其他" in categories else categories[-1]
        unknown = [n for n in dict.fromkeys(names) if n not in self._merchants]
        if unknown:
            listing = "\n".join(f"- {n}" for n in unknown)
            res = await ctx.llm(
                [
                    LLMMessage.user(
                        f"把下列商户名各自归入这些分类之一：{categories}。"
                        '只输出 JSON：{"suggestions": '
                        '[{"merchant": string, "category": string, "confidence": number}]}。'
                        "\n每个商户名都要出现一次，merchant 原样返回，不要改写、不要合并。"
                        f'\n实在判断不了的填 "{fallback}"，不要留空。'
                        f"\n商户名：\n{listing}"
                    )
                ],
                requires=("text",),
                json_mode=True,
                note="categorize_merchants",
            )
            try:
                data = json.loads(
                    res.text.strip().removeprefix("```json").removesuffix("```").strip()
                )
            except Exception:
                return ToolResult.fail(
                    "schema_validation_failed",
                    "商户归类输出不是合法 JSON",
                    retryable=False,
                )
            for item in data.get("suggestions") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("merchant") or "").strip()
                cat = str(item.get("category") or "").strip()
                if name and cat in categories:
                    self._merchants[name] = cat

        suggestions = [
            {"merchant": n, "category": self._merchants[n]}
            for n in dict.fromkeys(names)
            if n in self._merchants
        ]
        return ToolResult(ok=True, output={"suggestions": suggestions})

    async def tool_list_categories(self, args: dict, ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={"categories": self._categories(ctx)})

    async def tool_dedupe_check(self, args: dict, ctx: Any) -> ToolResult:
        fields = args.get("fields") or {}
        amount = fields.get("amount")
        merchant = fields.get("merchant")
        hit = next(
            (e for e in await self._ledger.recent(limit=int(args.get("lookback_days") or 10) * 20)
             if e.get("amount") == amount and e.get("merchant") == merchant),
            None,
        )
        return ToolResult(
            ok=True,
            output={"duplicate": hit is not None, "matched_entry_id": (hit or {}).get("id")},
        )

    async def tool_query_ledger(self, args: dict, ctx: Any) -> ToolResult:
        entries = await self._ledger.recent(limit=100)
        return ToolResult(ok=True, output={"entries": entries, "count": len(entries)})

    async def tool_compare_entries(self, args: dict, ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={"differences": []})

    # -- 写操作：以 idempotency_token 为依据，重试不会重复入账 -------------
    async def tool_build_ledger_entry(self, args: dict, ctx: Any) -> ToolResult:
        """**构造一份待入账的凭证，不写账本。**

        名字以前是 `write_ledger_entry`，那是错的：这个 handler 跑在后端，
        而账本在消费端本地的 Room 库里——后端写不进去。现在它只产出一份
        平铺的凭证草稿，由消费端用自己的复式模型落库。

        `entry_id` 由 `ctx.idempotency_token` 推出且**稳定**：消费端把它当主键或
        唯一约束，重试就不会重复入账。这件事由存储端保证，不该靠调用方自觉。
        """
        fields = args.get("fields") or {}
        # **金额与方向是硬前提，缺了就失败，不写空壳。**
        # 之前这里会把 amount=None 写成一条空记录并报成功——账里多一笔空记录，
        # 而调用方以为记好了。失败会被看见，空记录要等对账时才发现。
        amount = fields.get("amount")
        if amount is None:
            return ToolResult.fail(
                "bad_input",
                f"缺少金额：无法在没有金额的情况下生成凭证。收到的参数是 "
                f"{json.dumps(args, ensure_ascii=False)[:200]}",
                retryable=False,
            )
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return ToolResult.fail("bad_input", f"金额不是数字：{amount!r}", retryable=False)
        if amount <= 0:
            return ToolResult.fail("bad_input", f"金额必须为正数：{amount}", retryable=False)

        direction = fields.get("direction")
        if direction not in {"expense", "income"}:
            # 方向不确定时**停下来问**，而不是默认成 expense。
            # 猜错方向等于把账记反，而用户很难发现（见 schemas/bookkeeping_ledger_entry.json）。
            return ToolResult.confirm(
                f"这笔 {amount} {fields.get('currency') or 'CNY'} 是支出还是收入？",
                [{"id": "expense", "label": "支出"}, {"id": "income", "label": "收入"}],
                partial={"fields": {**fields, "amount": amount}},
            )

        merchant_info = args.get("merchant") or {}
        return ToolResult(ok=True, output={
            "entry_id": ctx.idempotency_token,
            "amount": amount,
            "currency": fields.get("currency") or "CNY",
            "direction": direction,
            "category": merchant_info.get("category") or fields.get("category"),
            "merchant": merchant_info.get("merchant") or fields.get("merchant"),
            "occurred_at": fields.get("datetime"),
            "source_task": ctx.task_id,
            "note": fields.get("notes"),
        })

    # -- 纯算术：能力需求为空，就真的只有算术 -----------------------------
    async def tool_compute_portfolio(self, args: dict, ctx: Any) -> ToolResult:
        positions = args.get("positions") or []
        cost = sum(float(p.get("cost", 0)) for p in positions)
        value = sum(float(p.get("value", 0)) for p in positions)
        return ToolResult(
            ok=True,
            output={
                "cost": round(cost, 2),
                "value": round(value, 2),
                "gain": round(value - cost, 2),
                "return_pct": round((value - cost) / cost * 100, 2) if cost else None,
            },
        )

    async def tool_generate_financial_plan(self, args: dict, ctx: Any) -> ToolResult:
        payload = json.dumps(args, ensure_ascii=False)
        res = await ctx.llm(
            [LLMMessage.user(f"基于这些数据给出财务规划建议，输出 JSON：\n{payload}")],
            requires=("text", "reasoning.strong"),
            json_mode=True,
            note="generate_financial_plan",
        )
        try:
            data = json.loads(res.text.strip().removeprefix("```json").removesuffix("```").strip())
        except Exception:
            data = {"advice": res.text[:500]}
        return ToolResult(ok=True, output=data)


