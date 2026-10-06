"""YAML 读取，按 YAML 1.2 的核心 schema。

**为什么需要这个模块。** PyYAML 默认实现的是 YAML 1.1，其中 ``yes`` / ``no`` /
``on`` / ``off`` / ``y`` / ``n`` 都被当作布尔值。于是策略文件里写：

    fallback:
      on: [router_timeout, router_low_confidence]

``on`` 这个**键**会被解析成布尔 ``True``，于是 ``fallback.on`` 不存在，
而多出一个键为 ``True`` 的字段。校验器报的是"缺少 on 字段"，
而人看着文件里明明写着 ``on`` —— 这是最难查的一类问题：**改了配置，行为没变。**

策略文件里 ``fallback.on``、``evaluator.escalation.on`` 都用到了 ``on``，
将来也一定还会有人写 ``no`` / ``yes``。与其要求每个人记住这个陷阱，
不如在解析边界上按 YAML 1.2 处理——1.2 是现在的事实标准，
JSON Schema 生态与大多数现代工具都是这个语义。

**只保留 ``true`` / ``false`` 为布尔**，其余一律当字符串。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_BOOL_1_2 = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class Yaml12SafeLoader(yaml.SafeLoader):
    """只把 true/false 当布尔的 SafeLoader。

    先在子类上做一份自己的 resolver 拷贝，**再**修改——否则会改到
    ``yaml.SafeLoader`` 的全局表，污染进程内其他 ``yaml.safe_load`` 调用。
    """


# 显式拷贝，避免污染基类
Yaml12SafeLoader.yaml_implicit_resolvers = {
    ch: list(resolvers) for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

# 摘掉 YAML 1.1 的布尔规则（yes/no/on/off/y/n/...）
for _ch, _resolvers in list(Yaml12SafeLoader.yaml_implicit_resolvers.items()):
    Yaml12SafeLoader.yaml_implicit_resolvers[_ch] = [
        (tag, regexp) for tag, regexp in _resolvers if tag != "tag:yaml.org,2002:bool"
    ]

# 只装回 true/false
Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", _BOOL_1_2, list("tTfF")
)

# 同理，``none`` 在 1.1 里不是 null，但 ``~``/``null``/空 是——1.2 语义相同，
# 无需改动。这里只记录一句：策略里 ``mode: none`` 是字符串 "none"，不是 null。


def load_yaml(path: Path | str) -> Any:
    """读取一个 YAML 文件。解析失败抛原异常，由调用方决定包成什么错误码。"""
    text = Path(path).read_text(encoding="utf-8")
    return yaml.load(text, Loader=Yaml12SafeLoader)


def load_yaml_text(text: str) -> Any:
    return yaml.load(text, Loader=Yaml12SafeLoader)


__all__ = ["Yaml12SafeLoader", "load_yaml", "load_yaml_text"]
