"""本地启动入口。

    .venv/bin/python -m uvicorn main:app --port 8000

M1 只暴露评估与路由相关端点；任务在路由完成后被标记为 rejected，
decision 字段包含完整决策（执行层属于 M2）。
"""

from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv  # type: ignore[import-not-found]  # 可选依赖

# 必须在 import app 之前：按本文件 docstring 的启动命令 ``python -m uvicorn main:app``，
# uvicorn 是以 ``main`` 而非 ``__main__`` 导入本模块的，load_dotenv 放进 if 块
# 就永远不执行 —— .env 的 LLM_BASE_URL/LLM_API_KEY 全部静默丢失。
load_dotenv()

from dispatcher.interface.app import app  # noqa: E402, F401  (uvicorn 按名字加载)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
