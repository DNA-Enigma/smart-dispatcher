"""插件加载：把 handler 从配置里装进来。

**这个模块不知道任何 handler 的名字。** 它只认得 ``config/handlers.yaml`` 的格式，
以及 ``包.模块:类名`` 这种导入路径写法。于是"新增一个领域"变成一次配置改动 +
一个新目录，而不是一次对调度层的改动——这正是接缝的验收标准
（``tests/test_handler_seam.py`` 会机械地证明 ``dispatcher/`` 下的文件哈希不变）。

加载失败一律**在启动期**抛出，不留到运行期：一个拼错的导入路径如果在第一次
请求到达时才炸，那不是"报错晚了一点"，而是"服务正常启动了却处理不了请求"。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.errors import DispatcherError
from .core.registry import HandlerManifest, HandlerRegistry
from .core.yamlio import load_yaml

DEFAULT_SPEC_PATH = "config/handlers.yaml"


@dataclass
class HandlerSpec:
    manifest_path: Path
    impl_path: str | None = None


def load_specs(config_path: Path) -> list[HandlerSpec]:
    if not config_path.exists():
        # 没有配置文件不是错误：一个 handler 都没有的调度层仍然能跑
        # （评估器会缺能力目录、路由会落到兜底），只是没什么用。
        return []
    raw = load_yaml(config_path)
    specs: list[HandlerSpec] = []
    for i, item in enumerate(raw.get("handlers") or []):
        if not isinstance(item, dict) or "manifest" not in item:
            raise DispatcherError(
                "handler_error", f"{config_path} 第 {i + 1} 条缺少 manifest 字段"
            )
        specs.append(HandlerSpec(
            manifest_path=config_path.parent.parent / str(item["manifest"]),
            impl_path=(str(item["impl"]) if item.get("impl") else None),
        ))
    return specs


def import_impl(dotted: str) -> Any:
    """``handlers.bookkeeping.handler:BookkeepingHandler`` → 那个类。

    只支持这一种写法，不搞花哨的（不支持属性链、不支持函数调用）。
    一条拼错时能被一眼看出来的路径，比一个灵活但难查的机制有用。
    """
    module_name, sep, attr = dotted.partition(":")
    if not sep or not module_name or not attr:
        raise DispatcherError(
            "handler_error",
            f"impl 路径格式非法：{dotted!r}，应形如 `包.模块:类名`",
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise DispatcherError(
            "handler_error",
            f"导入 handler 模块失败：{module_name}（{e}）。"
            f"检查 config/handlers.yaml 里的路径，以及该模块是否在导入路径上。",
        ) from e
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise DispatcherError(
            "handler_error", f"模块 {module_name} 里没有 {attr!r}"
        ) from e


def instantiate(cls: Any, manifest: HandlerManifest) -> Any:
    """实例化 handler。

    只传 ``manifest`` 一个参数。**handler 的其它依赖由它自己解决**（默认存储），
    或者由调用方构造好实例后经 ``DispatcherConfig(handlers=[...])`` 注入——
    需要接自己的数据库时走后者。让加载器去猜构造签名，会把依赖注入变成一个
    只能在运行期发现错误的猜谜游戏。
    """
    try:
        return cls(manifest)
    except TypeError:
        # 也接受不带参数的实现——有些 handler 用类属性声明 manifest
        try:
            inst = cls()
        except TypeError as e:
            raise DispatcherError(
                "handler_error",
                f"{cls.__name__} 无法实例化：它的构造函数必须接受一个 manifest 参数"
                f"（或完全不带参数，用类属性声明 manifest）。",
            ) from e
        if not hasattr(inst, "manifest"):
            inst.manifest = manifest
        return inst


def build_registry(
    config_path: Path | None = None,
    *,
    extra: list[Any] | None = None,
    root: Path | None = None,
) -> HandlerRegistry:
    """按配置装配注册表，再把调用方直接注入的实例合进来。

    ``extra`` 是**外部注入通道**：需要把应用自己的数据库接进 handler 时，
    调用方自己构造实例（带好依赖）再传进来。加载器不参与那件事。
    """
    cfg = config_path or ((root or Path.cwd()) / DEFAULT_SPEC_PATH)
    specs = load_specs(cfg)
    manifests = load_manifests_for(specs)
    registry = HandlerRegistry(manifests)

    for spec, manifest in zip(specs, manifests, strict=True):
        if spec.impl_path is None:
            continue
        registry.bind(instantiate(import_impl(spec.impl_path), manifest))

    for handler in extra or []:
        registry.bind(handler)
    return registry


def load_manifests_for(specs: list[HandlerSpec]) -> list[HandlerManifest]:
    """逐个读 manifest 文件。

    不直接用 ``load_manifests(dir)``：那要求一个目录里放全部声明，
    而"一个领域一个目录"才是接缝想要的组织方式——声明与实现在一起，
    删掉那个目录就等于卸掉那个领域。
    """
    out: list[HandlerManifest] = []
    for spec in specs:
        p = spec.manifest_path
        if not p.exists():
            raise DispatcherError(
                "handler_error", f"handler 声明文件不存在：{p}（检查 config/handlers.yaml）"
            )
        try:
            out.append(HandlerManifest.model_validate(load_yaml(p)))
        except Exception as e:
            raise DispatcherError("handler_error", f"声明 {p} 校验失败：{e}") from e
    return out


__all__ = [
    "DEFAULT_SPEC_PATH", "HandlerSpec", "build_registry", "import_impl",
    "instantiate", "load_specs",
]
