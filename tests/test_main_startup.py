"""main.py 的启动顺序不变式。

回归背景（2026-10-08 fin2 端到端发现）：``load_dotenv()`` 原本写在
``if __name__ == "__main__":`` 里，而本文件 docstring 自己给的启动命令是
``python -m uvicorn main:app`` —— 那样 ``__name__`` 是 ``main`` 而非
``__main__``，``load_dotenv()`` 永远不执行，``.env`` 里的 LLM_BASE_URL /
LLM_API_KEY 全部静默丢失：接口照常起、health 照常 200，但模型配置是空的。

这正是本仓最忌讳的「静默失效」——症状离病灶很远（起服务的人只会看到模型调不通），
所以用测试钉死，不靠人记得。
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"


def _module_level_call_order(tree: ast.Module) -> list[str]:
    """模块顶层（不含任何 if/class/def 内部）的调用名，按出现顺序。"""
    order: list[str] = []
    for node in tree.body:  # 只有顶层节点，天然不含 if __name__ 内部
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            order.append(name)
        elif isinstance(node, ast.ImportFrom):
            order.append(f"import:{node.module}")
    return order


def test_load_dotenv_is_at_module_level() -> None:
    """``load_dotenv()`` 必须在模块顶层执行，不能躲在 ``if __name__`` 里。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name):
                names.append(func.id)
    assert "load_dotenv" in names, (
        "load_dotenv() 不在模块顶层——uvicorn 以 main:app 导入本模块时 "
        "__name__ != '__main__'，.env 不会被加载"
    )


def test_load_dotenv_runs_before_app_import() -> None:
    """``load_dotenv()`` 必须早于 ``import app``：app 构造时就读 Settings。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    order = _module_level_call_order(tree)

    dotenv_idx = next(
        (i for i, n in enumerate(order) if n == "load_dotenv"), None
    )
    app_idx = next(
        (i for i, n in enumerate(order) if n == "import:dispatcher.interface.app"), None
    )

    assert dotenv_idx is not None, "模块顶层没有 load_dotenv()"
    assert app_idx is not None, "模块顶层没有 import dispatcher.interface.app"
    assert dotenv_idx < app_idx, (
        "load_dotenv() 必须排在 import app 之前——"
        "create_app() 会同步读 Settings，晚于 import 就读到空配置"
    )


def test_logging_configured_at_module_level() -> None:
    """``logging.basicConfig`` 也必须在顶层——与 .env 是同一类病。

    藏进 ``if __name__`` 块时，按文档启动**没有任何应用日志**：评估器降级、
    节点失败只剩任务快照里一行 detail，排障只能靠猜。
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "basicConfig":
                names.append("logging.basicConfig")
            elif isinstance(func, ast.Name) and func.id == "basicConfig":
                names.append("logging.basicConfig")
    assert "logging.basicConfig" in names, (
        "logging.basicConfig() 不在模块顶层——按文档启动时应用日志全丢失"
    )


def test_nothing_load_bearing_left_in_main_guard() -> None:
    """``if __name__ == "__main__"`` 里只该剩 uvicorn.run —— 别再往里塞初始化。

    这条是通用闸：本仓已经栽过两次（load_dotenv、basicConfig），
    任何"必须在 import 时生效"的调用进了这个块都是静默失效。
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    allowed = {"run"}
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                f = inner.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                assert name in allowed, (
                    f"if __name__ 块里出现 {name}()——初始化调用必须放模块顶层，"
                    "否则按 python -m uvicorn main:app 启动时永不执行"
                )

