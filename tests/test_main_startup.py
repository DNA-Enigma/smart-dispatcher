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
