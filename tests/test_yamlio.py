"""YAML 1.2 语义。

这个测试存在的唯一理由是那个陷阱：PyYAML 默认按 YAML 1.1 解析，``on`` 是布尔。
策略里 ``fallback.on`` 与 ``evaluator.escalation.on`` 都用 ``on`` 作键——
一旦被解析成 ``True``，配置静默失效，而人看着文件里明明写着 ``on``。

这类"改了配置但行为没变"是最难查的问题，因此在解析边界上按 1.2 处理，并在这里锁住。
"""

from __future__ import annotations

import yaml

from dispatcher.core.yamlio import Yaml12SafeLoader, load_yaml_text


def test_on_is_a_string_not_a_boolean():
    data = load_yaml_text("fallback:\n  on: [a, b]\n")
    assert "on" in data["fallback"]
    assert data["fallback"]["on"] == ["a", "b"]


def test_yes_no_off_are_strings():
    data = load_yaml_text("a: yes\nb: no\nc: off\nd: y\ne: n\n")
    assert data == {"a": "yes", "b": "no", "c": "off", "d": "y", "e": "n"}


def test_true_false_still_booleans():
    data = load_yaml_text("a: true\nb: false\nc: True\nd: FALSE\n")
    assert data == {"a": True, "b": False, "c": True, "d": False}


def test_does_not_pollute_the_global_safe_loader():
    """自定义加载器不能改到 yaml.SafeLoader 的全局表。

    否则进程内任何一次 yaml.safe_load 的语义都会被悄悄改掉——
    这是库级别的副作用，比本文件要解决的那个陷阱更危险。
    """
    assert "on" not in yaml.safe_load("on: [a]")
    assert True in yaml.safe_load("on: [a]")  # 1.1 语义：键被转成了布尔
    assert yaml.SafeLoader is not Yaml12SafeLoader


def test_the_real_policy_keeps_on_as_a_key(policy):
    assert "router_timeout" in policy.fallback.on
    assert "schema_validation_failed" in policy.evaluator.escalation["on"]
