"""本地启动入口。

    .venv/bin/python -m uvicorn main:app --port 8000

M1 只暴露评估与路由相关端点；任务在路由完成后被标记为 rejected，
decision 字段包含完整决策（执行层属于 M2）。
"""

from __future__ import annotations

import logging

import uvicorn
from dotenv import load_dotenv  # type: ignore[import-not-found]  # 可选依赖

from dispatcher.interface.app import app  # noqa: F401  (uvicorn 按名字加载)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
