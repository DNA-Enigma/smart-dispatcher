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

from ..core.contract import TaskEnvelope, wire_dump
from ..core.errors import DispatcherError
from ..core.events import TERMINAL_EVENTS, to_sse_heartbeat
from ..core.state import TERMINAL_STATUSES
from ..pipeline import Dispatcher, describe_config

log = logging.getLogger("dispatcher")

_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    if _dispatcher is None:  # pragma: no cover - 正常由 lifespan 初始化
        raise RuntimeError("Dispatcher 尚未初始化")
    return _dispatcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _dispatcher
    _dispatcher = Dispatcher.build()
    log.info("smart-dispatcher 启动：%s", describe_config(_dispatcher))
    try:
        yield
    finally:
        await _dispatcher.aclose()
        _dispatcher = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Smart 调度层",
        version="0.2.0",
        summary="评估 → 路由 → 拆解并行执行 → 自进化",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    @app.exception_handler(DispatcherError)
    async def _dispatcher_error_handler(request: Request, exc: DispatcherError) -> JSONResponse:
        rid = request.headers.get("x-request-id") or ""
        problem = exc.to_problem(rid)
        if exc.internal:
            log.warning("问题 %s（内部：%s）", exc.code, exc.internal)
        headers: dict[str, str] = {}
        if exc.retry_after_ms:
            headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
        return JSONResponse(status_code=exc.status, content=problem, headers=headers)

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
    async def usage(user_id: str | None = None, tenant_id: str = "default") -> dict[str, Any]:
        d = get_dispatcher()
        return {
            "currency": d.ledger.currency,
            "enforcement": d.ledger.enforcement,
            "totals": {
                "user": d.ledger.user_total(user_id) if user_id else 0.0,
                "tenant": d.ledger.tenant_total(tenant_id),
            },
        }

    # ------------------------------------------------------------------
    # 媒体
    # ------------------------------------------------------------------
    @app.post("/v1/media", status_code=201)
    async def upload_media(request: Request) -> dict[str, Any]:
        d = get_dispatcher()
        body = await request.body()
        if not body:
            raise DispatcherError("invalid_request", "请求体为空")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        retain_raw = request.query_params.get("retain_days")
        retain_days = int(retain_raw) if retain_raw and retain_raw.isdigit() else None
        rec = await d.media.put(
            body, mime,
            user_id=request.headers.get("x-user-id"),
            tenant_id=request.headers.get("x-tenant-id") or "default",
            retain_days=retain_days,
        )
        return {
            "media_id": rec.media_id, "kind": rec.kind, "mime": rec.mime,
            "bytes": rec.size_bytes, "sha256": rec.sha256,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "url": None,  # 签名 URL 属于存储实现，内存实现不提供
        }

    @app.get("/v1/media/{media_id}")
    async def get_media(media_id: str) -> Response:
        d = get_dispatcher()
        got = await d.media.get(media_id)
        if got is None:
            raise DispatcherError("not_found", f"媒体不存在或已过保留期：{media_id}")
        blob, rec = got
        return Response(content=blob, media_type=rec.mime)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    @app.post("/v1/tasks")
    async def create_task(request: Request) -> JSONResponse:
        d = get_dispatcher()
        payload = await request.json()
        try:
            envelope = TaskEnvelope.model_validate(payload)
        except Exception as e:
            raise DispatcherError(
                "invalid_request", f"请求体不符合契约：{e}",
                context={"schema": "schemas/task_envelope.json"},
            ) from e

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
    async def get_task(task_id: str) -> dict[str, Any]:
        record = await get_dispatcher().get(task_id)
        return record.to_snapshot()

    @app.get("/v1/tasks")
    async def list_tasks(
        user_id: str | None = None, tenant_id: str = "default", limit: int = 20
    ) -> dict[str, Any]:
        records = await get_dispatcher().list(tenant_id=tenant_id, user_id=user_id, limit=limit)
        return {"items": [r.to_snapshot() for r in records], "next_cursor": None}

    @app.get("/v1/tasks/{task_id}/result")
    async def get_result(task_id: str) -> dict[str, Any]:
        record = await get_dispatcher().get(task_id)
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
        body = await request.json()
        record = await get_dispatcher().clarify(task_id, body)
        return record.to_snapshot()

    @app.post("/v1/tasks/{task_id}/feedback")
    async def submit_feedback(task_id: str, request: Request) -> dict[str, Any]:
        """人工质量信号。**这是 04 最有价值的输入**——记账场景的自然采集点就是
        确认/修改页：用户把分类从"其他"改成"餐饮"，那一下同时给出了错在哪和对的是什么。
        没有它，04 只能靠延迟与成本反推质量，效果差一个量级。
        """
        d = get_dispatcher()
        body = await request.json()
        try:
            signal = await d.record_feedback(task_id, body)
        except Exception as e:
            raise DispatcherError("invalid_request", f"反馈格式不符：{e}") from e
        return {"task_id": task_id, "human_signal": signal}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> Response:
        await get_dispatcher().cancel(task_id)
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
        items = await loop.list_suggestions(status=status, limit=limit)
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
    async def trigger_analysis() -> dict[str, Any]:
        """手动触发一次分析。正常由定时任务按 analysis_interval 跑。"""
        d, loop = _require_evolution()
        await loop.bootstrap()
        outcome = await loop.run_pass(tenant_id="default")
        return outcome.to_wire()

    @app.post("/v1/evolution/suggestions/{suggestion_id}/approve")
    async def approve_suggestion(suggestion_id: str, request: Request) -> dict[str, Any]:
        d, loop = _require_evolution()
        body = await request.json() if await request.body() else {}
        # 审批人 = 使用者本人。请求里可以显式带上是谁，缺省就是任务的用户。
        approved_by = str(body.get("approved_by") or request.headers.get("x-user-id") or "owner")
        version = await loop.approve(
            suggestion_id,
            approved_by=approved_by,
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
        body = await request.json()
        if not body.get("reason"):
            raise DispatcherError("invalid_request", "拒绝必须给出理由——理由本身是信号")
        updated = await loop.reject(
            suggestion_id, reason=str(body["reason"]),
            decided_by=str(body.get("decided_by") or "owner"),
        )
        return {"suggestion_id": suggestion_id, "status": updated["status"]}

    @app.get("/v1/policy/versions")
    async def list_policy_versions(limit: int = 50) -> dict[str, Any]:
        _, loop = _require_evolution()
        rows = await loop.list_versions(limit=limit)
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
        body = await request.json()
        to_version = str(body.get("to_version") or "")
        if not to_version:
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
        record = await d.get(task_id)  # 不存在就 404，而不是挂一个永远不发事件的连接

        last = request.headers.get("last-event-id") or request.query_params.get("since")
        since = int(last) if last and str(last).isdigit() else 0
        heartbeat_s = d.policy.limits.sse_heartbeat_ms / 1000.0
        # **重放必须有上限。** 只按 seq 重放的话，客户端断线很久后重连会一次性收到
        # 几千条事件——内存与服务端发送队列都扛不住，而客户端也来不及处理。
        # 超上限时只重放最近的这些，并把是否截断如实告知（`replay_truncated`），
        # 让客户端知道"你漏掉的中间部分要靠快照补，不要指望事件流"。
        replay_limit = d.policy.limits.memory_events

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
                    from ..core.events import EventRecord, to_sse

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
                watcher.cancel()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


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
