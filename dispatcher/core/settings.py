"""部署事实与密钥。

两类东西只在这里：**密钥**（永远不进代码与策略文件）与**部署事实**
（端点、路径）。业务阈值一律不在这里——那些在 ``config/routing.policy.yaml``。

密钥的解析规则只有一条，没有特例：

    secret://<group>/<key>  →  环境变量 <GROUP>_<KEY>（全大写）

于是策略里的 ``secret://llm/cheap_model`` 取到 ``LLM_CHEAP_MODEL``，
``secret://llm/api_key`` 取到 ``LLM_API_KEY``。加一个新的密钥引用不需要
改这里的任何代码——这正是"档位到模型的绑定不写在代码里"的落实方式。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import DispatcherError

# 仓库根目录：dispatcher/core/settings.py → 上溯三级
REPO_ROOT = Path(__file__).resolve().parents[2]

_SECRET_RE = re.compile(r"^secret://(?P<group>[a-z0-9_]+)/(?P<key>[a-z0-9_]+)$")


class Settings(BaseSettings):
    """环境变量覆盖的部署事实。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 契约目录。策略、提示词、schema 都在这里，与实现分离。
    contract_root: Path = Field(default=REPO_ROOT)

    # ---- LLM 供应商。契约里只出现档位名，供应商细节止步于这一层。----
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_cheap_model: str = ""
    llm_standard_model: str = ""
    llm_strong_model: str = ""

    # ---- 接口鉴权。----
    # 静态共享 token。契约声明了 bearerAuth 但没有发放/续期端点（那是消费端
    # 认证体系的事），因此参考实现只做一件事：比对。空值表示**鉴权关闭**，
    # 只用于本地开发，启动时会打一条 ERROR 级日志。
    dispatcher_auth_token: str = ""
    # 这个 token 绑定到哪个租户/用户。单 token 部署下身份就是一个常量——
    # 不为多租户抽象新模型，那是"消费端 IdP 签发带身份的 token"之后的事。
    dispatcher_tenant: str = "default"
    dispatcher_user: str = "owner"

    # ---- 请求体与连接的自保上限。----
    # 与 llm_qps/llm_max_concurrency 同类：属于"这个进程怎么保护自己"，
    # 不是可调的产品策略，因此在这里而不是在策略文件里。
    #
    # 通用请求体上限（字节）。所有 JSON 端点按它判，**边读边判**（见
    # interface/validation.py 的 read_raw_body）；媒体上传取它与
    # routing.policy.yaml 的 limits.media.max_bytes 的较大者。
    dispatcher_max_request_bytes: int = 10 * 1024 * 1024
    # SSE 并发连接上限。每个订阅占一个连接、一个生成器协程与一个轮询任务。
    # **0 或负数表示不限制**（部署方显式关掉这个闸）。
    dispatcher_sse_max_connections: int = 100
    # 媒体保留：缺省保留期（天）与上界（天）。缺省值必须有界——
    # ``expires_at=None``（永不过期）正是"金融截图常驻内存"那条路。
    dispatcher_media_retain_days: int = 1
    dispatcher_media_retain_max_days: int = 30
    # 过期媒体的清理周期（秒）。docs/05-media.md 承诺过"由定时任务驱动"，
    # 此前那个定时任务不存在。
    dispatcher_media_sweep_interval_s: float = 300.0

    # 调用的硬边界。与 routing.policy.yaml 的 limits 是两回事：
    # 那些是策略（可被建议修改），这些是进程级的自保（不可被任何东西改）。
    llm_request_timeout_s: float = 60.0
    llm_max_retries: int = 1
    # 重试的指数退避基数（秒）。放在设置里而不是写进适配器，
    # 是为了在供应商限流更严时可以调大而不用改代码。
    llm_retry_backoff_s: float = 0.3
    # 限流属于"这个进程怎么保护自己和供应商"，不是可调的产品策略，因此在这里
    # 而不是在策略文件里。默认值偏保守：宁可慢一点，也不要因为瞬时并发把额度打满。
    llm_qps: float = 2.0
    llm_max_concurrency: int = 2

    @field_validator("llm_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # ------------------------------------------------------------------
    @property
    def policy_path(self) -> Path:
        return self.contract_root / "config" / "routing.policy.yaml"

    @property
    def taxonomy_path(self) -> Path:
        return self.contract_root / "config" / "taxonomy.yaml"

    @property
    def agents_path(self) -> Path:
        return self.contract_root / "config" / "agents.yaml"

    @property
    def prompts_dir(self) -> Path:
        return self.contract_root / "prompts"

    @property
    def schemas_dir(self) -> Path:
        return self.contract_root / "schemas"

    # ------------------------------------------------------------------
    def resolve_secret(self, ref: str) -> str:
        """``secret://llm/cheap_model`` → 环境变量 ``LLM_CHEAP_MODEL`` 的值。

        取不到就抛 ``policy_violation``——一个引用了不存在密钥的策略是配置错误，
        不是运行时故障，让它在启动/首次使用时立刻暴露，而不是等到某条请求发出去。
        """
        m = _SECRET_RE.match(ref)
        if not m:
            raise DispatcherError(
                "policy_violation",
                f"密钥引用格式非法：{ref!r}，应为 secret://<group>/<key>",
                context={"ref": ref},
            )
        env_name = f"{m['group']}_{m['key']}".upper()
        value = os.environ.get(env_name) or getattr(self, env_name.lower(), "")
        if not value:
            raise DispatcherError(
                "policy_violation",
                f"密钥引用 {ref} 解析到环境变量 {env_name}，但它为空。"
                f"请填好 {REPO_ROOT / '.env'}（该文件已被 .gitignore 忽略）。",
                context={"ref": ref, "env_var": env_name},
            )
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def redact_secrets(text: str) -> str:
    """把文本里出现过的**已解析密钥值**抹掉。

    ``resolve_secret`` 的逆操作，所以放在同一处：既然密钥只从这一个地方进来，
    往外送的文本也该只从这一个地方过一道。

    用于**要出进程**的文本：任务快照的 notes、错误体的 context、日志。这些文本
    常来自上游异常消息，而适配器会把供应商响应体原文截 300 字放进去
    （``adapters/openai_compat.py::_http_error``）——供应商回显请求头并不罕见，
    密钥值于是有可能顺着这条路径漏出去。

    按**值**匹配替换，不猜格式：只抹掉真正在用的那个密钥，不会误伤正常的错误
    文本。密钥的引用名（``secret://llm/api_key``）与环境变量名不抹——它们不含
    秘密，抹掉反而让"密钥没配好"这条最需要行动的线索变得无从下手。
    """
    secret = get_settings().llm_api_key
    if not secret:
        return text
    return text.replace(secret, "***")


__all__ = ["REPO_ROOT", "Settings", "get_settings", "redact_secrets"]
