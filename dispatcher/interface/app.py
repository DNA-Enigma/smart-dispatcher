"""薄接口层。

只做三件事：**解析请求 → 调流水线 → 把结果与错误渲染成契约的形状**。
它不含任何判断——路由判断在 LLM 与守卫里，配置在策略文件里。

契约规定错误是结构化的 ``Problem``，因此这里统一把 ``DispatcherError`` 映射成
对应 HTTP 状态 + Problem 体，**绝不把异常吞成字符串拼进正文**。

SSE 那一段值得单独说：它是"**先读库重放、再挂实时队列**"，两段之间无缝
（重放结束时的 seq 就是队列的起点，不会漏也不会重）。因此客户端断线重连时
只要回传 ``Last-Event-ID``，就能补齐缺失的事件——这是移动端的必需能力，
不是优化项。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..adapters.sqlite_tokens import SqliteTokenStore
from ..core.contract import Identity, TaskEnvelope, wire_dump
from ..core.errors import DispatcherError
from ..core.events import TERMINAL_EVENTS, to_sse, to_sse_heartbeat
from ..core.execution import ClarificationAnswer
from ..core.runlog import HumanSignal
from ..core.settings import get_settings
from ..core.state import TERMINAL_STATUSES
from ..evolution.loop import PolicyRollback, SuggestionApproval, SuggestionRejection
from ..pipeline import Dispatcher, DispatcherConfig, describe_config
from .auth import AuthConfig, BearerAuthMiddleware, request_identity
from .limiter import ConnectionLimiter
from .problems import problem_response
from .tokens import TokenIssueRequest, TokenRegistry
from .validation import read_body, read_json_document, read_raw_body

log = logging.getLogger("dispatcher")

_dispatcher: Dispatcher | None = None

#: 分页 ``limit`` 的上下界。与 ``openapi.yaml`` 对 ``limit`` 的声明
#: （``minimum: 1, maximum: 100``）同一组值——契约里写着界，代码里就得真夹。
_LIMIT_MIN = 1
_LIMIT_MAX = 100

#: SSE 超限时建议客户端等多久（毫秒）。进 ``Retry-After`` 头。
_SSE_RETRY_AFTER_MS = 5000


def _clamp_limit(limit: int) -> int:
    """把分页 ``limit`` 夹进契约声明的区间。

    不夹的后果是实测过的：SQLite 的 ``LIMIT -1`` 是"不限"，``GET /v1/tasks?limit=-1``
    于是拉全表（``sqlite_state.py`` / ``sqlite_evolution.py`` 三处查询）。

    **为什么是静默夹取而不是 400/422**：走 FastAPI 的 ``Query(ge=1, le=100)`` 会返回
    422 + ``{"detail": ...}``——那不是契约里的 Problem 体，为了夹一个分页参数而制造
    一次契约违规不划算。越界夹到边界仍然是一个能用的分页请求，口径与 ``retain_days``
    一致。下界取 1 而不是 0：``LIMIT 0`` 是"什么都不返回"，而调用方要的是第一页。
    """
    return max(_LIMIT_MIN, min(limit, _LIMIT_MAX))


def _retain_days(raw: str | None) -> int:
    """``retain_days`` 查询参数 → 保留天数。

    缺省/非法 → 配置的缺省保留期；数字 → 夹进 ``[1, dispatcher_media_retain_max_days]``。

    下界 1 是刻意的：``expires_at=None``（永不过期）正是本轮要消掉的那条路，而 ``0``
    （``docs/05-media.md`` 里的"任务结束即删"）不是 store 能实现的语义——它不知道任务
    什么时候结束。夹取同时消掉了 ``timedelta(days=巨大值)`` 的 OverflowError
    （此前 ``?retain_days=<21 位数字>`` 是稳定 500）。
    """
    settings = get_settings()
    ceiling = max(1, settings.dispatcher_media_retain_max_days)
    default = min(max(1, settings.dispatcher_media_retain_days), ceiling)
    if raw is None:
        return default
    try:
        # 超长数字串会在这里抛 ValueError（int() 有位数上限），与"不是数字"同罪。
        days = int(raw)
    except ValueError:
        log.warning("retain_days=%r 不是整数，按缺省保留期 %d 天处理", raw, default)
        return default
    return max(1, min(days, ceiling))


def get_dispatcher() -> Dispatcher:
    if _dispatcher is None:  # pragma: no cover - 正常由 lifespan 初始化
        raise RuntimeError("Dispatcher 尚未初始化")
    return _dispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _dispatcher
    settings = get_settings()
    # 后端从 ``.env`` 来，而不是在代码里写死。此前这里调的是
    # ``Dispatcher.build()``（不传 config），于是 ``state_backend`` 永远取
    # DispatcherConfig 的缺省值 "memory"——**生产必然跑内存后端，改 .env 换不了**
    # （docs/12-deployment.md 第 7 节末）。这一行就是那个缺口的补丁。
    _dispatcher = Dispatcher.build(
        DispatcherConfig(
            settings=settings,
            state_backend=settings.dispatcher_state_backend,
            sqlite_path=settings.dispatcher_state_path,
        )
    )
    # 打开状态库、恢复库里的生效策略。必须在开放请求之前完成。
    await _dispatcher.astart()
    # 令牌登记簿也要接上同一个库，否则重启后已发放的子令牌全部认不出来
    # （docs/12-deployment.md 第 7 节 #4：持有者当场 401，P2-c 的功能白做）。
    # 挂在这里而不是 create_app：应用可以在 import 期就被构造出来，而那时
    # 不该去碰磁盘；lifespan 才是 I/O 该发生的地方。
    token_store: SqliteTokenStore | None = None
    if settings.dispatcher_state_backend == "sqlite":
        token_store = SqliteTokenStore(settings.state_db_path)
        app.state.tokens.attach_store(token_store)
        log.info("已载入已发放令牌 %d 枚", len(app.state.tokens.records()))
    if (
        settings.dispatcher_state_backend == "memory"
        and settings.dispatcher_auth_token
    ):
        # 与"配了 token 却空着"那条同口径：不静默。配了鉴权令牌说明这是
        # 生产形态，而内存后端意味着重启后所有子令牌失效、已批准的策略回退。
        log.error(
            "状态后端是 memory（重启即丢：任务快照、事件流、已发放子令牌、"
            "已批准的策略版本、上传的媒体全部消失），但已配置 DISPATCHER_AUTH_TOKEN"
            "——这看起来是生产部署。请设 DISPATCHER_STATE_BACKEND=sqlite"
            "（见 .env.example）。"
        )
    log.info("smart-dispatcher 启动：%s", describe_config(_dispatcher))
    # 过期媒体的周期清理：docs/05-media.md 承诺"由定时任务驱动"，而此前
    # sweep_expired 全仓没有调用点——缺的正是这个任务。
    sweeper = asyncio.create_task(
        _dispatcher.media_sweeper(settings.dispatcher_media_sweep_interval_s)
    )
    try:
        yield
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await _dispatcher.aclose()
        _dispatcher = None
        if token_store is not None:
            token_store.close()


def create_app(*, auth: AuthConfig | None = None) -> FastAPI:
    """构造应用。

    ``auth`` 不传时从 ``Settings`` 读（``.env`` / 环境变量）。显式传入是为了测试
    能钉住"配了 token 会怎样"而不必去改进程环境——本仓库的 ``.env`` 是开发者的
    私人物品，测试结论不该随它的内容变化。
    """
    if auth is None:
        auth = AuthConfig.from_settings(get_settings())
        if not auth.required:
            # 不静默放行。这条日志是"生产忘了配 token"唯一能自己浮出水面的地方：
            # 鉴权关掉之后，任何能访问这个端口的人都能读全部任务快照、读别人的
            # 金融截图、改全局策略——而系统看起来一切正常。
            log.error(
                "DISPATCHER_AUTH_TOKEN 未配置：接口鉴权已关闭，"
                "所有任务、媒体与策略端点对任何可达本端口的人开放。仅限本地开发，"
                "生产部署必须配置该变量（见 .env.example）。"
            )

    # 交互式文档（/docs、/redoc）与运行时 /openapi.json 的开关**跟着鉴权走**，
    # 不另设环境变量。理由是这两件事本来就是同一件：**开了主令牌就是生产形态**
    # （create_app 在未配令牌时打 ERROR 日志，lifespan 也拿它判断"像不像生产"），
    # 而生产不想要一个把全部 API 面（端点、参数、错误码）摊给公网扫描器的页面。
    #
    # 为什么不给"生产也开文档"的开关：一个能重新打开文档的配置项，就是一条
    # "配错一行就把 API 面重新暴露出去"的路。契约（openapi.yaml）仍然是完整的，
    # 需要时读它、或在本机（无令牌）起服务看 /docs——**关的是可交互浏览，不是声明**。
    #
    # 结构上把路由去掉（而不是只靠中间件拦成 401）：中间件拦不拦得住取决于
    # PUBLIC_PATHS 那张表，将来若有人把 /docs 加进"公开路径"，文档会当场裸露；
    # 路由压根不存在，就没有这个面。审计项「/docs、/openapi.json 公网无鉴权可达」。
    docs_url = None if auth.required else "/docs"
    app = FastAPI(
        title="Smart 调度层",
        version="0.2.0",
        summary="评估 → 路由 → 拆解并行执行 → 自进化",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=None if auth.required else "/redoc",
        openapi_url=None if auth.required else "/openapi.json",
    )
    if auth.required:
        # 不静默：这是"文档为什么打不开"唯一能自己浮出水面的地方。
        log.info("已配置主令牌（生产形态）：/docs、/redoc、/openapi.json 不再挂载")
    else:
        log.info("未配置主令牌（本地开发）：/docs 可用；生产请配 DISPATCHER_AUTH_TOKEN")
    # 已发放令牌的登记簿。按 app 实例建（不是进程级单例），与 sse_limiter 同口径：
    # 测试里会构造多个 app，单例会让用例之间互相串味。中间件要它来认已发放令牌，
    # 端点要它来签发/撤销，因此两个都拿同一个引用。
    tokens = TokenRegistry()
    app.state.tokens = tokens
    # 鉴权挂在这里，而不是逐个端点加依赖：新端点自动被保护。
    app.add_middleware(BearerAuthMiddleware, config=auth, registry=tokens)

    # SSE 连接闸按 app 实例建（不是进程级单例）：测试里会构造多个 app，
    # 单例会让用例之间互相影响。挂在 state 上是为了测试能直接把它占满。
    sse_limiter = ConnectionLimiter(limit=get_settings().dispatcher_sse_max_connections)
    app.state.sse_limiter = sse_limiter

    # ------------------------------------------------------------------
    @app.exception_handler(DispatcherError)
    async def _dispatcher_error_handler(request: Request, exc: DispatcherError) -> JSONResponse:
        rid = request.headers.get("x-request-id") or ""
        return problem_response(exc, rid)

    # ------------------------------------------------------------------
    # 自省
    # ------------------------------------------------------------------
    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        d = get_dispatcher()
        return {
            "status": "ok",
            "tiers": [
                {"tier": t, "state": "closed", "latency_ms": None}
                for t in d.policy.model_tier_ids
            ],
            "policy_version": d.policy.policy_version,
            "execution_enabled": d.execution_enabled,
            "handlers": sorted(d.registry.ids),
            "executable_handlers": sorted(d.registry.executable_ids),
            "config_warnings": d.config_warnings,
        }

    @app.get("/v1/policy")
    async def get_policy() -> dict[str, Any]:
        d = get_dispatcher()
        raw = d.policy.model_dump(mode="json")
        # 密钥引用脱敏：只留前缀，避免把内部引用结构暴露出去
        for tier in raw.get("model_tiers", {}).values():
            ref = tier.get("model_ref", "")
            if isinstance(ref, str) and ref.startswith("secret://"):
                tier["model_ref"] = "secret://***"
        return raw

    @app.get("/v1/capabilities")
    async def capabilities() -> dict[str, Any]:
        d = get_dispatcher()
        handlers = []
        for hid in sorted(d.registry.ids):
            m = d.registry.manifest(hid)
            assert m is not None
            handlers.append({
                "handler_id": m.handler_id,
                "version": m.version,
                "capabilities": m.capabilities,
                "flows": m.flows,
                "executable": d.registry.has_executable(hid),
                "tools": [
                    {
                        "name": t.name, "description": t.description,
                        "side_effects": t.side_effects,
                        "requires_capabilities": t.requires_capabilities,
                        "idempotent": t.idempotent,
                        "input_schema": t.input_schema,
                        "output_schema_ref": t.output_schema_ref,
                    }
                    for t in m.tools
                ],
                "config_schema": m.config_schema,
            })
        return {"handlers": handlers}

    @app.get("/v1/agents")
    async def agents() -> dict[str, Any]:
        """角色自省。契约里角色是配置，这里把它暴露出来，免得只能读 YAML。"""
        d = get_dispatcher()
        return {
            "agents_version": d.agents.agents_version,
            "hard_bounds": d.agents.hard_bounds,
            "roles": [
                {"id": r.id, "when": r.when, "requires": r.requires,
                 "allowed_tools": r.allowed_tools, "default_tier": r.default_tier,
                 "max_rounds": r.max_rounds, "on_round_limit": r.on_round_limit}
                for r in d.agents.roles
            ],
        }

    @app.get("/v1/usage")
    async def usage(request: Request, user_id: str | None = None) -> dict[str, Any]:
        """用量。

        租户**只**来自 token——此前它是个 query 参数，``?tenant_id=受害者`` 就能
        读到别人的花销。``user_id`` 保留为筛选条件（契约里声明了它），但它只能在
        调用方自己的租户内选人，越不出授权范围。
        """
        d = get_dispatcher()
        tenant_id = request_identity(request).tenant_id
        return {
            "currency": d.ledger.currency,
            "enforcement": d.ledger.enforcement,
            "totals": {
                "user": d.ledger.user_total(user_id) if user_id else 0.0,
                "tenant": d.ledger.tenant_total(tenant_id),
            },
        }

    # ------------------------------------------------------------------
    # 凭据发放（P2-c）
    #
    # 为什么放在调度层、而不是"另起一个认证服务"：试点期要的是**多用户各持一枚
    # 凭据**，不是注册、找回密码、邮箱验证那套产品流程。把最小的一步做出来——
    # 主令牌签发子令牌、子令牌可撤销——比现在就引入一个用户体系更贴近需求。
    # 不做的部分（过期/续期/轮换、跨进程存储）在 openapi 里如实声明，见 tokens.py。
    #
    # **这三个端点只认主令牌**，由中间件的 ADMIN_PATH_PREFIXES 拦在路由之前；
    # 端点里因此没有第二处授权判断——一处判断、一处失败，才不会有第二种行为。
    # ------------------------------------------------------------------
    @app.post("/v1/tokens", status_code=201)
    async def issue_token(request: Request) -> dict[str, Any]:
        """签发一枚绑定到指定 tenant/user 的令牌。

        返回体里的 ``token`` **只在这里出现这一次**：登记簿只留它的摘要，
        之后 ``GET /v1/tokens`` 再也拿不回明文。丢了就撤销重发。
        """
        body = await read_body(
            request, TokenIssueRequest,
            required=("tenant_id", "user_id"), endpoint="POST /v1/tokens",
        )
        record, token = tokens.issue(
            tenant_id=body["tenant_id"], user_id=body["user_id"]
        )
        return {**record.to_wire(), "token": token}

    @app.get("/v1/tokens")
    async def list_tokens() -> dict[str, Any]:
        """列出已发放令牌（不含明文值）。

        有了它撤销才可用：否则调用方只能撤销自己在签发那一刻记下的 id，
        而"那个人离职了，把他那枚撤掉"需要先能看见有哪些枚。
        """
        return {"items": [r.to_wire() for r in tokens.records()]}

    @app.delete("/v1/tokens/{token_id}")
    async def revoke_token(token_id: str) -> Response:
        """撤销一枚令牌。撤销后它下一次请求就是 401——中间件先解析失败。"""
        if not tokens.revoke(token_id):
            raise DispatcherError("not_found", f"令牌不存在或已撤销：{token_id}")
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # 媒体
    # ------------------------------------------------------------------
    @app.post("/v1/media", status_code=201)
    async def upload_media(request: Request) -> dict[str, Any]:
        """上传媒体。

        归属取自 token，不再读 ``x-tenant-id``/``x-user-id``——那两个头此前与
        ``create_task`` 读请求体的口径不一致，同一份身份有两套说法，而两套都是
        客户端说了算。
        """
        d = get_dispatcher()
        identity = request_identity(request)
        # 这条路由的上限取"通用上限"与"存储层真正执行的上限"的**较大者**：
        # 后者在 routing.policy.yaml 的 limits.media.max_bytes 里，另设一个环境变量
        # 只会与它分叉（"两处各写一份上限一定会分叉"是本仓已有的教训）。
        # 收的是原始二进制而不是 base64，因此不需要 4/3 的编码余量。
        body = await read_raw_body(
            request,
            endpoint="POST /v1/media",
            max_bytes=max(get_settings().dispatcher_max_request_bytes,
                          d.policy.limits.media.max_bytes),
        )
        if not body:
            raise DispatcherError("invalid_request", "请求体为空")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        retain_days = _retain_days(request.query_params.get("retain_days"))
        rec = await d.media.put(
            body, mime,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            retain_days=retain_days,
        )
        return {
            "media_id": rec.media_id, "kind": rec.kind, "mime": rec.mime,
            "bytes": rec.size_bytes, "sha256": rec.sha256,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "url": None,  # 签名 URL 属于存储实现，内存实现不提供
        }

    @app.get("/v1/media/{media_id}")
    async def get_media(media_id: str, request: Request) -> Response:
        """取回媒体。**归属不符按不存在处理。**

        此前 ``media.get(media_id)`` 只看 id，不看 ``MediaRecord.tenant_id``——
        拿到 id 就能读别人的金融截图。这里返回 404 而不是 403：403 等于确认
        "这个 id 存在，只是不归你"，那本身就是不该给的信息。
        """
        d = get_dispatcher()
        got = await d.media.get(media_id)
        if got is None:
            raise DispatcherError("not_found", f"媒体不存在或已过保留期：{media_id}")
        blob, rec = got
        if not _media_belongs_to(rec, request_identity(request)):
            raise DispatcherError("not_found", f"媒体不存在或已过保留期：{media_id}")
        return Response(content=blob, media_type=rec.mime)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    @app.post("/v1/tasks")
    async def create_task(request: Request) -> JSONResponse:
        d = get_dispatcher()
        # 走统一的读体路径：非法 JSON / 非对象 / 空体是 **400 Problem**，
        # 而不是 ``Request.json()`` 抛出去的 500 纯文本。
        payload = await read_json_document(request, endpoint="POST /v1/tasks")
        try:
            envelope = TaskEnvelope.model_validate(payload)
        except Exception as e:
            raise DispatcherError(
                "invalid_request", f"请求体不符合契约：{e}",
                context={"schema": "schemas/task_envelope.json"},
            ) from e

        # 身份以 token 为准。**不返回 400**：``identity`` 是契约里的 required 字段，
        # 合规客户端必然要发；把一个不具授权含义的字段做成错误触发器，会让所有本地
        # user_id 与 token 绑定身份不同的客户端在升级后集体报错。覆盖 + 告警足够——
        # 客户端自报从来就不是事实，忽略它没有"错误"可言。
        identity = request_identity(request)
        claimed = envelope.identity
        if claimed.tenant_id != identity.tenant_id or claimed.user_id != identity.user_id:
            log.warning(
                "请求自报身份 %s/%s 与 token 身份 %s/%s 不符，已按 token 覆盖",
                claimed.tenant_id, claimed.user_id, identity.tenant_id, identity.user_id,
            )
        envelope = envelope.model_copy(
            update={
                "identity": Identity(
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    # 本地化是展示偏好，不是授权信息——它继续由客户端说了算。
                    locale=claimed.locale,
                    timezone=claimed.timezone,
                )
            }
        )

        # 媒体引用也要过归属校验：否则可以在请求体里引用**别人的** media_id，
        # 让评估器把那张金融截图取出来送给模型。放在请求边界，且不区分
        # "不存在"与"不是你的"——两者都必须是同一个 404，否则 415/404 的差
        # 本身就是"这个 id 存在"的探测器。
        await _assert_media_owned(d, envelope, identity)

        idem = request.headers.get("idempotency-key")
        if idem and not envelope.idempotency_key:
            envelope = envelope.model_copy(update={"idempotency_key": idem})

        # auto：先内联把评估/路由/拆解做完，再按决策决定这一侧要不要等
        pref = envelope.constraints.mode_preference
        background = True if pref == "async" else (False if pref == "sync" else None)

        record = await d.submit(
            envelope, request_id=request.headers.get("x-request-id", ""), background=background
        )

        # 同步且已跑到终态 → 200 内联返回全部（付了一次往返就该一次拿全）
        if record.status in TERMINAL_STATUSES:
            body = _accepted(record)
            body.update(
                status=record.status,
                profile=wire_dump(record.profile) if record.profile else None,
                decision=wire_dump(record.decision) if record.decision else None,
                result=record.artifacts,
                error=record.error,
            )
            return JSONResponse(status_code=200, content=body)

        return JSONResponse(status_code=202, content=_accepted(record))

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, request: Request) -> dict[str, Any]:
        record = await get_dispatcher().get(
            task_id, tenant_id=request_identity(request).tenant_id
        )
        return record.to_snapshot()

    @app.get("/v1/tasks")
    async def list_tasks(
        request: Request, user_id: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """列出任务。

        租户**只**来自 token。此前它是 query 参数，``?tenant_id=受害者`` 就能枚举
        别人的任务。``user_id`` 保留（契约里声明了它）但只能在自家租户内选人。
        """
        records = await get_dispatcher().list(
            tenant_id=request_identity(request).tenant_id, user_id=user_id,
            limit=_clamp_limit(limit),
        )
        return {"items": [r.to_snapshot() for r in records], "next_cursor": None}

    @app.get("/v1/tasks/{task_id}/result")
    async def get_result(task_id: str, request: Request) -> dict[str, Any]:
        record = await get_dispatcher().get(
            task_id, tenant_id=request_identity(request).tenant_id
        )
        if record.status not in TERMINAL_STATUSES:
            raise DispatcherError(
                "result_not_ready", f"任务尚未到达终态（当前 {record.status}）",
                task_id=task_id, context={"status": record.status},
            )
        return {
            "task_id": record.task_id, "status": record.status,
            # 失败或取消时也保留并上报部分产物——已抽取出的字段即使没能入账也有价值
            "artifacts": record.artifacts, "error": record.error,
            "spent": record.budget_spent,
        }

    @app.post("/v1/tasks/{task_id}/clarify")
    async def clarify(task_id: str, request: Request) -> dict[str, Any]:
        body = await read_body(
            request, ClarificationAnswer,
            required=("question_id",), endpoint="POST /v1/tasks/{task_id}/clarify",
        )
        record = await get_dispatcher().clarify(
            task_id, body, tenant_id=request_identity(request).tenant_id
        )
        return record.to_snapshot()

    @app.post("/v1/tasks/{task_id}/feedback")
    async def submit_feedback(task_id: str, request: Request) -> dict[str, Any]:
        """人工质量信号。**这是 04 最有价值的输入**——记账场景的自然采集点就是
        确认/修改页：用户把分类从"其他"改成"餐饮"，那一下同时给出了错在哪和对的是什么。
        没有它，04 只能靠延迟与成本反推质量，效果差一个量级。
        """
        body = await read_body(
            request, HumanSignal,
            required=("verdict",), endpoint="POST /v1/tasks/{task_id}/feedback",
        )
        # 不再用 ``except Exception`` 兜底转 400：请求形状已在校验层挡下，此处剩下的
        # 都是真实结果——任务不存在是 **404**，不是"反馈格式不符"。此前那把兜底伞把
        # 404 也压成了 400，与 openapi.yaml 为 feedback 声明的响应矛盾。
        signal = await get_dispatcher().record_feedback(
            task_id, body, tenant_id=request_identity(request).tenant_id
        )
        return {"task_id": task_id, "human_signal": signal}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str, request: Request) -> Response:
        await get_dispatcher().cancel(
            task_id, tenant_id=request_identity(request).tenant_id
        )
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # 自进化
    #
    # 这些端点看起来像管理接口，但契约里**不建模 admin 角色**：它建模的是
    # "策略范围的所有者"——单用户场景就是使用者本人。理由见 docs/06-self-evolve.md：
    # 使用者是唯一有资格判断"这个分类对不对"的人，而那正是所有建议的推导来源。
    # ------------------------------------------------------------------
    def _require_evolution():
        d = get_dispatcher()
        if d.evolution is None:
            raise DispatcherError(
                "policy_violation",
                "自进化未启用（evolution_enabled=False），因此没有建议与版本数据。",
            )
        assert d.evolution is not None
        return d, d.evolution

    @app.get("/v1/evolution/suggestions")
    async def list_suggestions(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        _, loop = _require_evolution()
        items = await loop.list_suggestions(status=status, limit=_clamp_limit(limit))
        proposals = [s for s in items if s.get("status") == "proposed"]
        return {
            "items": items,
            # 待审批的放在最前面单独给一份：这套机制的失败模式是"建议没人看"，
            # 因此把"有几条等你决定"做成一个一眼可见的数字，而不是让客户端自己数。
            "pending_count": len(proposals),
            "drop_rate": await loop.suggest_drop_rate(),
            "reject_rate": await loop.suggest_reject_rate(),
        }

    @app.post("/v1/evolution/analyze")
    async def trigger_analysis(request: Request) -> dict[str, Any]:
        """手动触发一次分析。正常由定时任务按 analysis_interval 跑。"""
        d, loop = _require_evolution()
        await loop.bootstrap()
        outcome = await loop.run_pass(tenant_id=request_identity(request).tenant_id)
        return outcome.to_wire()

    @app.post("/v1/evolution/suggestions/{suggestion_id}/approve")
    async def approve_suggestion(suggestion_id: str, request: Request) -> dict[str, Any]:
        """批准建议，并**热换策略**。

        这是全仓最危险的一个端点：它能在运行中改掉全局策略（路由、价格、限额），
        而此前它既无鉴权、审批人还能在请求体里随便填——事后审计看到的
        "谁批的"完全不可信。现在鉴权由中间件兜住，审批人只能用 token 绑定的身份。
        """
        d, loop = _require_evolution()
        identity = request_identity(request)
        # ``allow_empty``：这个端点历史上允许不带请求体（scope/note 都是可选的），
        # 空体继续按 {} 处理，只是非法 JSON 不再是 500 而是 400 Problem。
        body = await read_body(
            request, SuggestionApproval,
            endpoint="POST /v1/evolution/suggestions/{suggestion_id}/approve",
            # 契约写着 ``requestBody.required: false``：不带参数的"批准"是合法的，
            # 统一读体路径不能顺手把它拒掉（有测试钉住这一条）。
            allow_empty=True,
        )
        version = await loop.approve(
            suggestion_id,
            approved_by=identity.user_id,
            scope=body.get("scope"),
            note=body.get("note"),
        )
        # **热换策略**：批准之后立刻生效，否则"金丝雀"就变成"重启一次"。
        d.apply_policy(version["policy"])
        return {
            "suggestion_id": suggestion_id,
            "status": "canarying" if version["status"] == "canary" else "applied",
            "policy_version": version["policy_version"],
            "parent_version": version.get("parent_version"),
            "changed": version.get("changed"),
            "canary": (version.get("scope") or {}).get("canary"),
        }

    @app.post("/v1/evolution/suggestions/{suggestion_id}/reject")
    async def reject_suggestion(suggestion_id: str, request: Request) -> dict[str, Any]:
        _, loop = _require_evolution()
        identity = request_identity(request)
        body = await read_body(
            request, SuggestionRejection,
            required=("reason",),
            endpoint="POST /v1/evolution/suggestions/{suggestion_id}/reject",
        )
        if not body["reason"].strip():
            # 类型已由模型挡住，这里只剩"给了个空串"。它与"没给"不同：
            # 客户端确实发了这个字段，只是内容什么都没说，而理由本身是信号。
            raise DispatcherError("invalid_request", "拒绝必须给出理由——理由本身是信号")
        updated = await loop.reject(
            suggestion_id, reason=body["reason"],
            decided_by=identity.user_id,
        )
        return {"suggestion_id": suggestion_id, "status": updated["status"]}

    @app.get("/v1/policy/versions")
    async def list_policy_versions(limit: int = 50) -> dict[str, Any]:
        _, loop = _require_evolution()
        rows = await loop.list_versions(limit=_clamp_limit(limit))
        return {
            "items": [
                {"policy_version": v["policy_version"], "parent_version": v.get("parent_version"),
                 "created_at": v.get("created_at"), "approved_by": v.get("approved_by"),
                 "suggestion_id": v.get("suggestion_id"), "status": v.get("status")}
                for v in rows
            ]
        }

    @app.post("/v1/policy/rollback")
    async def rollback_policy(request: Request) -> dict[str, Any]:
        d, loop = _require_evolution()
        body = await read_body(
            request, PolicyRollback,
            required=("to_version",), endpoint="POST /v1/policy/rollback",
        )
        to_version = body["to_version"]
        if not to_version:
            # 空串与"没给"分开：前者是客户端给了个空的版本号，报 invalid_request
            # 比让它一路走到存储层变成 404 not_found 更贴近事实。
            raise DispatcherError("invalid_request", "缺少 to_version")
        version = await loop.rollback(to_version=to_version, note=body.get("note"))
        d.apply_policy(version["policy"])
        return {"policy_version": version["policy_version"], "rolled_back_to": to_version}

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------
    @app.get("/v1/tasks/{task_id}/events")
    async def stream_events(task_id: str, request: Request) -> StreamingResponse:
        d = get_dispatcher()
        # 不存在就 404，而不是挂一个永远不发事件的连接；不是自己的任务同样按不存在处理
        record = await d.get(task_id, tenant_id=request_identity(request).tenant_id)

        last = request.headers.get("last-event-id") or request.query_params.get("since")
        since = int(last) if last and str(last).isdigit() else 0
        heartbeat_s = d.policy.limits.sse_heartbeat_ms / 1000.0
        # **重放必须有上限。** 只按 seq 重放的话，客户端断线很久后重连会一次性收到
        # 几千条事件——内存与服务端发送队列都扛不住，而客户端也来不及处理。
        # 超上限时只重放最近的这些，并把是否截断如实告知（`replay_truncated`），
        # 让客户端知道"你漏掉的中间部分要靠快照补，不要指望事件流"。
        replay_limit = d.policy.limits.memory_events

        # **上限**：每个订阅占一个连接、一个生成器协程与一个轮询 watcher。
        # 放在 d.get() 之后：404 的请求不该消耗名额。
        lease = sse_limiter.acquire()
        if lease is None:
            # 429 而不是 503：码表里没有 503 的码（新增码属于契约变更），而
            # ``rate_limited`` 的语义正是"用量到顶、稍后重试"，它还会让
            # ``problem_response`` 带出 ``Retry-After`` 头。
            raise DispatcherError(
                "rate_limited",
                f"事件流并发连接已达上限 {sse_limiter.limit}，请稍后重试。",
                retry_after_ms=_SSE_RETRY_AFTER_MS,
                context={
                    "scope": "sse_connections",
                    "limit": sse_limiter.limit,
                    "active": sse_limiter.active,
                },
            )

        async def gen():
            done_flag = {"v": record.status in TERMINAL_STATUSES}
            # 游标：gen 内部要推进它，但不能直接改闭包里的 `since`
            # （对闭包变量赋值会让它变成 gen 的局部变量，前面的读取就失效了）。
            cursor = since
            seq = cursor

            async def watch_done() -> None:
                # 任务在别的协程里推进，用一个轻量轮询把"到终态了"这件事
                # 传达给流——SSE 的关闭时机需要一个信号，而不是等超时。
                while not done_flag["v"]:
                    await asyncio.sleep(0.05)
                    cur = await d.state.get_task(task_id)
                    if cur is not None and cur.status in TERMINAL_STATUSES:
                        done_flag["v"] = True

            watcher = asyncio.create_task(watch_done())
            try:
                backlog = await d.events.replay(task_id, since_seq=cursor)
                if len(backlog) > replay_limit:
                    skipped = len(backlog) - replay_limit
                    backlog = backlog[-replay_limit:]
                    # 如实告知被截断，而不是安静地少给
                    # **不要在这里 import to_sse**：函数内任何位置的一次赋值/导入
                    # 都会让这个名字在整个 gen 作用域里变成局部变量，于是下面
                    # `yield to_sse(ev)` 在没走截断分支时抛 UnboundLocalError（实测 500）。
                    from ..core.events import EventRecord

                    yield to_sse(EventRecord(
                        seq=backlog[0].seq - 1, task_id=task_id, type="error",
                        ts=backlog[0].ts,
                        data={"problem": {
                            "type": "https://smart-dispatcher/errors/result-not-ready",
                            "title": "Replay truncated", "status": 409,
                            "code": "result_not_ready", "retryable": True,
                            "request_id": "",
                            "detail": f"重放被截断：你漏掉的 {skipped} 条事件没有推送，"
                                      f"请以任务快照为准重建视图（GET /v1/tasks/{task_id}）。",
                            "context": {"skipped": skipped, "replay_limit": replay_limit},
                        }},
                    ))
                    cursor = backlog[0].seq - 1

                async for ev in d.events.stream(
                    task_id, since_seq=cursor, heartbeat_s=heartbeat_s,
                    is_done=lambda: done_flag["v"],
                ):
                    if ev is None:
                        yield to_sse_heartbeat(seq)
                        continue
                    seq = ev.seq
                    if ev.type in TERMINAL_EVENTS:
                        done_flag["v"] = True
                    yield to_sse(ev)
            finally:
                # 客户端断开、任务到终态、服务端取消——三条路都从这里出去，
                # 因此名额的归还点只有这一个（``release`` 幂等，重复调用无害）。
                watcher.cancel()
                lease.release()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _media_belongs_to(rec, identity) -> bool:
    """媒体是否属于本次请求的身份。

    采用 fail-closed：记录没写归属（``tenant_id is None``）也判为**不属于**。
    ``MediaRecord.tenant_id`` 是 ``MediaStorePort`` 定义的字段，参考实现的
    ``POST /v1/media`` 一定写入它；一个不记录归属的存储实现没有能力参与授权判定，
    此时让请求明确失败，比放行一个无法证明归属的对象好。
    """
    return rec.tenant_id is not None and rec.tenant_id == identity.tenant_id


async def _assert_media_owned(d, envelope: TaskEnvelope, identity) -> None:
    """校验请求体里引用的每一个 media_id 都属于本次请求的身份。

    **不校验的话，越权读取绕过了 ``GET /v1/media/{id}`` 那一道**：把别人的
    media_id 放进 ``input.media``，评估器会把它取出来送进模型——金融截图就这样
    出了门，而调用方一个字节的媒体内容都没碰过。

    "不存在"与"不是你的"返回**同一个** 404：两者若给出不同的码，探测者就能用
    一连串 media_id 问出"哪些 id 真实存在"。
    """
    for ref in envelope.input.media or []:
        rec = await d.media.stat(ref.media_id)
        if rec is None or not _media_belongs_to(rec, identity):
            raise DispatcherError(
                "not_found", f"媒体不存在或已过保留期：{ref.media_id}",
                context={"media_id": ref.media_id},
            )


def _accepted(record) -> dict[str, Any]:
    est: dict[str, Any] = {}
    if record.profile and record.profile.est_cost:
        c = record.profile.est_cost
        est["cost"] = {"min": c.min, "max": c.max, "currency": c.currency}
    if record.profile and record.profile.est_latency_ms:
        est["latency_ms"] = {
            "min": record.profile.est_latency_ms.min,
            "max": record.profile.est_latency_ms.max,
        }
    return {
        "task_id": record.task_id,
        "status": record.status,
        "mode": record.mode,
        "mode_changed": record.mode_changed,
        "mode_change_reason": record.mode_change_reason,
        "created_at": record.created_at.isoformat().replace("+00:00", "Z"),
        "policy_version": record.policy_version,
        "events_url": f"/v1/tasks/{record.task_id}/events",
        "result_url": f"/v1/tasks/{record.task_id}/result",
        "estimated": est,
    }


app = create_app()


__all__ = ["app", "create_app", "get_dispatcher"]
