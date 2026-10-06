"""实验：改 `when` 散文就能改行为吗？

这是 M1 存在的理由。整套设计押在一个断言上：

> 决策表是配置，判断交给 LLM。**新增一条路由 = 追加一段 YAML；改行为 = 改那段散文。**

如果这个断言不成立，后面 M2-M6 全是白做。所以在建状态存储、SSE、执行器之前，
先用一条真实的策略改动去实证它。

实验做法：

1. 用**当前**策略跑一批请求，其中一半是需要查用户私有账目的（"我上个月花了多少"），
   一半是真正适合直接回答的（"解释一下复利"）。
2. 把 ``direct_answer`` 的 ``when`` 里那句"凡涉及查询本人流水、余额、持仓…一律不得
   选择本路由"**删掉**，其余一个字不动。
3. 用改后的策略重跑同一批请求。
4. 对比分流结果。

**预期**：第 1 轮里私有数据请求 0 次落到 ``direct_answer``；第 3 轮里明显有若干次落进去。
若两轮结果几乎相同，说明散文没有被真正消费——那时该回炉改设计，而不是继续往下做。

用法::

    .venv/bin/python scripts/experiment_when_edit.py
    .venv/bin/python scripts/experiment_when_edit.py --repeat 3

需要 .env 里填好 LLM_BASE_URL 与三个档位的模型名。会真实调用模型（几毛钱量级）。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatcher.adapters.example_handlers import load_example_registry  # noqa: E402
from dispatcher.adapters.memory_media import InMemoryMediaStore  # noqa: E402
from dispatcher.adapters.memory_state import InMemoryStateStore  # noqa: E402
from dispatcher.adapters.openai_compat import OpenAICompatibleLLM  # noqa: E402
from dispatcher.core.agents import load_agents  # noqa: E402
from dispatcher.core.budget import BudgetLedger  # noqa: E402
from dispatcher.core.contract import TaskEnvelope  # noqa: E402
from dispatcher.core.eventbus import EventBus  # noqa: E402
from dispatcher.core.policy import load_policy  # noqa: E402
from dispatcher.core.pricing import load_pricing  # noqa: E402
from dispatcher.core.prompts import PromptLibrary, policy_menu  # noqa: E402
from dispatcher.core.settings import REPO_ROOT, get_settings  # noqa: E402
from dispatcher.core.taxonomy import load_taxonomy  # noqa: E402
from dispatcher.pipeline import Dispatcher  # noqa: E402

TARGET_ROUTE = "direct_answer"

# 这些请求**需要**读用户自己的账目数据，因此不该走 direct_answer
NEEDS_PRIVATE_DATA = [
    "我上个月花了多少？",
    "帮我看看这个月的餐饮支出是多少",
    "我的余额还剩多少",
    "我最近三个月在交通上一共花了多少钱",
    "我持有的基金现在收益怎么样",
    "我上周有哪些支出是可以省下来的",
    "我这个月的支出比上个月多了还是少了",
    "我去年在餐饮上花了多少",
]

# 这些请求确实不需要用户私有数据，走 direct_answer 是正确的
GENUINELY_DIRECT = [
    "解释一下什么是复利",
    "把这句话改写得更正式一些：明天开会",
    "你好",
    "预算和决算有什么区别",
    "记账里的权责发生制是什么意思",
]

# 判定"这句在讲那条限定"的标记。
#
# 不硬编码整句：YAML 的 `>` 折叠会在换行处插入空格，硬编码的字符串一改策略就匹配不上，
# 那时实验会静默地删掉别的东西——比失败更糟。
MARKER = "私有数据"


# 实验的第二轮用**这段**文本替换掉 ``when``。
#
# 为什么是明写一段，而不是做文本切除：我先试过"删掉含关键词的那一句"，结果是把它
# 删空了（两句都在讲这件事）；又试过"按顿号切子句"，结果把括号里的顿号也切了，
# 句子被切碎。字符串手术在这里既不精确也不可读。
#
# 而实验真正想问的是有限定的问题：**"一个忘了写这条边界的作者，会写成什么？"**
# 那就把它写出来。下面是同一段散文，只是漏掉了私有数据那一条——这是真实会发生的
# 遗漏，也是这个实验要复现的情形。
# **改动必须是"有诱惑力的"，否则实验没有判别力。** 这是我第三次修正它：
#
#   第 1 版：只删最后一句 → 那句是重复陈述，两轮一样（测不出东西）
#   第 2 版：删掉全部提到私有数据的文字 → 剩下的例子（解释/改写/闲聊/常识/翻译）
#            本身就不含"查我的账"，模型靠例子就排除了，两轮还是一样
#   第 3 版（本版）：把"查询用户的账目、余额与持仓"**明确列进** direct_answer 的适用示例
#
# 第 3 版才是真实遗漏的形态：作者把"查账"误当成"简单问题"而写进了直接回答的示例里。
# 现在模型面前有两个互相矛盾的信号——示例说"查账可以走这条路"，
# 而"单步、不需要写操作"又说它不该写。行为是否改变，才是对这套机制的真检验。
NAIVE_REWRITE = {
    "direct_answer": (
        "单步、无副作用、不需要写操作的请求：解释、改写、闲聊、常识问答、语言翻译，"
        "以及查询用户的账目、余额与持仓。"
    ),
}


def naive_variant(route) -> tuple[str, str]:
    """返回 (改动后的 when, 被漏掉的那部分原文)。"""
    if route.id not in NAIVE_REWRITE:
        raise ValueError(
            f"没有为路由 {route.id!r} 准备实验用的 when 改写版本。"
            f"请在 NAIVE_REWRITE 里写一段『漏掉这条边界的人会写成什么』的文本——"
            f"不要用文本切除代替，那个做法在这里既不准也读不懂。"
        )
    original = " ".join(route.when.split())
    naive = NAIVE_REWRITE[route.id]
    if MARKER not in original:
        raise ValueError(f"原 when 里没有 {MARKER!r}，实验无从下手：{original!r}")
    if MARKER in naive:
        raise ValueError(f"改写版里仍然提到 {MARKER!r}——那就没构成『漏掉』")
    if len(naive) < 20:
        raise ValueError("改写版太短，测的会是『没有路由描述』而不是『漏掉一句限定』")
    return naive, original


def envelope(text: str) -> TaskEnvelope:
    return TaskEnvelope.model_validate({
        "identity": {"user_id": "u_experiment", "timezone": "Asia/Shanghai"},
        "input": {"text": text},
    })


def build(policy_path, settings, llm) -> Dispatcher:
    """按给定策略文件装配一个调度器（执行层关闭——本实验只关心决策）。

    手工装配是为了能把**假想的策略文件**注进去（真实适配器自己从 .env 取配置）。
    除此之外每一处都走真实路径：真策略加载与自洽校验、真词表、真角色表、
    真示例 handler、真守卫。实验唯一替换掉的就是"用哪份策略"。
    """
    policy = load_policy(policy_path)
    state = InMemoryStateStore()
    return Dispatcher(
        policy=policy,
        pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=load_example_registry(REPO_ROOT / "examples" / "handlers"),
        prompts=PromptLibrary(settings.prompts_dir),
        llm=llm,
        media=InMemoryMediaStore(
            allowed_mime=policy.limits.media.allowed_mime,
            max_bytes=policy.limits.media.max_bytes,
        ),
        state=state,
        taxonomy=load_taxonomy(settings.taxonomy_path),
        agents=load_agents(settings.agents_path),
        events=EventBus(state),
        ledger=BudgetLedger(
            enforcement=policy.enforcement_mode,
            warn_at_ratio=policy.budget.warn_at_ratio,
            currency=policy.budget.currency,
        ),
        execution_enabled=False,
    )


async def route_all(d: Dispatcher, texts: list[str]) -> list[tuple[str, str, list[str]]]:
    out: list[tuple[str, str, list[str]]] = []
    for t in texts:
        rec = await d.submit(envelope(t))
        route = rec.decision.route_id if rec.decision else "(无决策)"
        applied = list(rec.decision.guard.applied) if rec.decision else []
        out.append((t, route, applied))
    return out


def summarize(rows, label: str) -> tuple[int, Counter]:
    leaked = [r for r in rows if r[1] == TARGET_ROUTE]
    print(f"\n【{label}】")
    for text, route, applied in rows:
        mark = "  ← 落到 target" if route == TARGET_ROUTE else ""
        guard = f"  guard={applied}" if applied else ""
        print(f"  {route:<26} {text}{guard}{mark}")
    print(f"  → {len(leaked)}/{len(rows)} 落到 {TARGET_ROUTE}")
    return len(leaked), Counter(r[1] for r in rows)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="每轮重复次数，用于观察随机性")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.llm_base_url or not settings.llm_cheap_model:
        print(
            "✗ .env 尚未填好：需要 LLM_BASE_URL 与 LLM_CHEAP_MODEL/STANDARD/STRONG。\n"
            f"  见 {REPO_ROOT / '.env.example'}",
            file=sys.stderr,
        )
        return 2

    policy = load_policy(settings.policy_path)
    route = policy.route(TARGET_ROUTE)
    assert route is not None, f"策略里没有路由 {TARGET_ROUTE}"

    print("=" * 78)
    print(f"实验：删掉 {TARGET_ROUTE}.when 里的『私有数据』那一条，行为会变吗？")
    print("=" * 78)
    norm = " ".join(route.when.split())
    print(f"\n原文（{len(norm)} 字）：\n  {norm}")
    try:
        stripped, original = naive_variant(route)
    except ValueError as e:
        print(f"\n✗ {e}")
        return 2
    print(f"\n改后（{len(stripped)} 字）：\n  {stripped}")
    print("\n（这一版是『忘了写那条边界的人会写的样子』——漏掉的是原文里的私有数据限定）")

    texts = NEEDS_PRIVATE_DATA * args.repeat + GENUINELY_DIRECT * args.repeat
    needs = set(NEEDS_PRIVATE_DATA)

    # ---- 第 1 轮：当前策略 ----
    # 用真实的策略文件路径，让加载与自洽校验也走一遍
    llm_a = OpenAICompatibleLLM(policy, settings)
    before = await route_all(build(settings.policy_path, settings, llm_a), texts)
    await llm_a.aclose()
    summarize(before, "第 1 轮：当前策略（when 里有限定）")

    # ---- 第 2 轮：删掉那一句，其余一字不动 ----
    edited = copy.deepcopy(policy)
    edited.routes = [
        r.model_copy(update={"when": stripped}) if r.id == TARGET_ROUTE else r
        for r in edited.routes
    ]
    # 确认改动真的进入了渲染后的菜单——否则实验测的是别的东西
    assert stripped in policy_menu(edited), "改动没有进入渲染后的菜单"
    assert MARKER not in policy_menu(edited), (
        f"渲染后的菜单里仍然出现 {MARKER!r}——改动没生效，实验会测到别的东西"
    )

    # 把改过的策略写成临时文件再加载——这样"改配置"这件事走的是真实路径，
    # 而不是把一个内存对象塞进调度器。两者在真实场景里完全不同。
    import tempfile

    import yaml as _yaml

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        _yaml.safe_dump(edited.model_dump(mode="json"), fh, allow_unicode=True,
                        sort_keys=False, default_flow_style=False)
        patched_path = Path(fh.name)
    print(f"\n（改过的策略写到了 {patched_path}，用真实的加载路径读回）")

    llm_b = OpenAICompatibleLLM(edited, settings)
    after = await route_all(build(patched_path, settings, llm_b), texts)
    await llm_b.aclose()
    summarize(after, "第 2 轮：删掉那一句之后（代码未动）")

    # ---- 判定 ----
    print("\n" + "=" * 78)
    print("判定")
    print("=" * 78)
    private_before = sum(1 for r in before if r[0] in needs and r[1] == TARGET_ROUTE)
    private_after = sum(1 for r in after if r[0] in needs and r[1] == TARGET_ROUTE)
    direct_before = sum(1 for r in before if r[0] not in needs and r[1] == TARGET_ROUTE)
    direct_after = sum(1 for r in after if r[0] not in needs and r[1] == TARGET_ROUTE)

    print(f"  需要私有数据的请求落到 {TARGET_ROUTE}：改前 {private_before} → 改后 {private_after}")
    print(f"  本就该直接回答的请求落到 {TARGET_ROUTE}：改前 {direct_before} → 改后 {direct_after}")

    if private_before == 0 and private_after > 0:
        print("\n  ✓ 机制成立：删掉一句散文就把请求吸进了另一条路由，而代码一行未改。")
        print("    『决策表是配置』这条断言得到了实证，可以继续 M2。")
        return 0
    if private_after > private_before:
        print("\n  ~ 方向正确但幅度有限：散文被消费了，只是信号不够强。")
        print("    可以把 when 写得更具体，或在菜单里补一句『不得选本路由』的强调。")
        return 0
    print("\n  ✗ 机制未成立：删掉一句限定后行为几乎没变。")
    print("    说明那条散文没有被真正消费。回炉改设计，不要带着这个疑点进入 M2。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
