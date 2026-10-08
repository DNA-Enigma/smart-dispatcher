"""本地启动入口。

    .venv/bin/python -m uvicorn main:app --port 8000

M1 只暴露评估与路由相关端点；任务在路由完成后被标记为 rejected，
decision 字段包含完整决策（执行层属于 M2）。
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from dotenv import load_dotenv  # type: ignore[import-not-found]  # 可选依赖

# 必须在 import app 之前：按本文件 docstring 的启动命令 ``python -m uvicorn main:app``，
# uvicorn 是以 ``main`` 而非 ``__main__`` 导入本模块的，load_dotenv 放进 if 块
# 就永远不执行 —— .env 的 LLM_BASE_URL/LLM_API_KEY 全部静默丢失。
load_dotenv()
# 同理：日志配置也得在模块顶层。藏进 if 块意味着按文档启动时**没有任何应用日志**，
# 评估器降级、节点失败这类错误只在任务快照里留一行 detail，排障时只能靠猜。
# 用 stderr + 时间格式，uvicorn 自己的 access log 不受影响。
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

from dispatcher.interface.app import app  # noqa: E402, F401  (uvicorn 按名字加载)

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
