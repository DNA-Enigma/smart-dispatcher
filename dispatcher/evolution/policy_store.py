"""策略版本：应用、金丝雀、回滚。

**永不原地修改配置。** 每一次改动都产出一个新的不可变版本，父版本可追溯。
这不是洁癖，它买到三样东西：

* **即时回滚** —— 切回上一个版本就完了，不用反向补丁。
* **可比较** —— 每条 RunLog 都带 ``policy_version``，因此能回答"这个改动之后
  指标变了吗"，金丝雀对比也才成立。
* **可追溯** —— 几个月后仍能回答"这条 run 是哪个策略产生的"。

金丝雀在单用户场景下按**时间窗**做，而不是按流量比例：一个人没有对照组，
按比例切片是自欺。做法是新版本先跑一段时间，期间把它的运行日志与基线的对比，
护栏指标劣化超阈值就自动回滚。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.errors import DispatcherError
from ..core.policy import Policy, set_policy_path
from ..core.runlog import RunLog

# 金丝雀期间盯的护栏指标。取值是"越小越好"，劣化即升高。
GUARDRAIL_METRICS = ("task_failure_rate", "guard_overrule_rate", "human_edit_rate", "budget_warn_rate")


def compute_guardrails(logs: list[RunLog]) -> dict[str, float]:
    """从一批运行日志算护栏指标。全部是比率，且**越小越好**。

    实现刻意保持透明：四个都是一眼能算的比率，没有加权、没有平滑。
    护栏的意义是"改动有没有把事情变糟"，一个需要解释才能懂的指标做不到这件事。
    """
    n = len(logs)
    if n == 0:
        return dict.fromkeys(GUARDRAIL_METRICS, 0.0)
    return {
        "task_failure_rate": sum(
            1 for x in logs if x.outcome.get("status") in {"failed", "rejected", "budget_exceeded"}
        ) / n,
        "guard_overrule_rate": sum(
            1 for x in logs if x.decision.get("guard_applied")
        ) / n,
        "human_edit_rate": sum(
            1 for x in logs if (x.human_signal and x.human_signal.verdict == "edited")
        ) / n,
        "budget_warn_rate": sum(
            1 for x in logs if x.outcome.get("budget_warned")
        ) / n,
    }


def apply_patches(
    policy_dict: dict, patches: list[dict]
) -> tuple[dict, list[str], dict[str, Any]]:
    """把补丁写进策略副本。返回 (新策略, 错误列表, 改动前的取值)。

    **在副本上操作**：任何一处失败都不改动原件。半途而废的策略比不应用更糟——
    它会变成一个新的、没人看过完整内容的版本。
    """
    import copy

    new = copy.deepcopy(policy_dict)
    errors: list[str] = []
    previous: dict[str, Any] = {}

    for p in patches:
        op = p.get("op")
        artifact = p.get("artifact")
        path = p.get("path")
        if op not in {"set", "append"}:
            errors.append(f"不支持的 op={op!r}（只允许 set / append）")
            continue
        if artifact != "routing.policy.yaml":
            # 其余产物（提示词、词表、模板）不在单文件里，由各自的装载器处理，
            # 见 ``apply_non_policy_patch``。这里只处理策略本体。
            errors.append(f"artifact={artifact!r} 不由本模块处理")
            continue
        if op == "append" and isinstance(p.get("value"), dict):
            ok = set_policy_path(new, f"{path}[-1]", p["value"])
            if not ok:
                errors.append(f"追加入口失败：{path}")
            continue
        from ..core.policy import resolve_policy_path

        exists, cur = resolve_policy_path(new, str(path))
        if not exists:
            errors.append(f"路径不存在：{path}")
            continue
        previous[str(path)] = cur
        if not set_policy_path(new, str(path), p.get("value")):
            errors.append(f"写入失败：{path}")

    if errors:
        return policy_dict, errors, {}
    return new, [], previous


class PolicyVersionManager:
    """版本库。所有写操作都产出新版本，从不原地改。"""

    def __init__(self, *, store, policy: Policy, evolution_cfg: dict | None = None) -> None:
        self._store = store
        self._policy = policy
        cfg = evolution_cfg or {}
        self._canary_cfg = cfg.get("canary", {"enabled": True, "mode": "time_window",
                                              "duration_ms": 86400000, "rollback_margin": 0.05})

    # ------------------------------------------------------------------
    async def ensure_initial_version(self, policy: Policy) -> dict:
        """把当前策略登记为第一个版本。没有它，任何改动都没有父版本可挂。"""
        existing = await self._store.get_version(policy.policy_version)
        if existing is not None:
            return existing
        record = {
            "policy_version": policy.policy_version,
            "parent_version": None,
            "created_at": datetime.now(UTC).isoformat(),
            "approved_by": "<initial>",
            "suggestion_id": None,
            "status": "active",
            "scope": {"level": "app", "canary": None},
            "patch": None,
            "policy": policy.model_dump(mode="json"),
            "changed": {},
        }
        await self._store.put_version(record)
        if hasattr(self._store, "set_active"):
            await self._store.set_active(policy.policy_version)
        return record

    # ------------------------------------------------------------------
    async def create_from_suggestion(
        self,
        *,
        suggestion: dict,
        approved_by: str,
        scope: dict | None = None,
        note: str | None = None,
    ) -> dict:
        """批准建议 → 产出新版本。**这是使用者点"批准"之后唯一发生的事。**"""
        base_id = str(suggestion.get("basis_policy_version") or suggestion.get("_base_version") or "")
        base = await self._store.get_version(base_id) if base_id else None
        if base is None:
            raise DispatcherError(
                "policy_violation",
                f"建议所依据的策略版本 {base_id!r} 不存在——无法保证补丁落在它预期的基底上。"
                f"请重新跑一次分析。",
                context={"basis_policy_version": base_id},
            )
        active = await self._store.active_version()
        if active is not None and active["policy_version"] != base["policy_version"]:
            # 基底不是当前生效版本 → 补丁可能落在已经变过的位置上。
            # 这里**拒绝**而不是"尽力而为"：一个基于旧版本猜出来的改动，
            # 落地后是什么效果没人说得清。
            raise DispatcherError(
                "idempotency_conflict",
                f"建议基于版本 {base['policy_version']}，而当前生效的是 "
                f"{active['policy_version']}。为避免落在已经变过的位置上，请重新分析。",
                context={"base": base["policy_version"], "active": active["policy_version"]},
            )

        target = suggestion.get("target") or {}
        patches = [{
            "op": "set",
            "artifact": target.get("artifact"),
            "path": target.get("path"),
            "previous": target.get("current"),
            "value": target.get("proposed"),
        }]
        new_dict, errors, previous = apply_patches(base["policy"], patches)
        if errors:
            raise DispatcherError(
                "policy_violation", f"补丁无法应用：{errors}", context={"errors": errors}
            )

        new_version_id = self._next_version_id(base["policy_version"])
        new_dict["policy_version"] = new_version_id
        # 校验新策略自洽——一个能通过建议校验、却让整份策略自相矛盾的改动
        # （例如把 default_tier 改成未定义的档位）必须在生效前挡住。
        from ..core.policy import Policy as P

        try:
            P.model_validate(new_dict)
        except Exception as e:
            raise DispatcherError(
                "policy_violation", f"应用后的策略不自洽：{e}", context={"patches": patches}
            ) from e

        canary_on = bool(self._canary_cfg.get("enabled"))
        duration = int(self._canary_cfg.get("duration_ms", 86400000))
        record = {
            "policy_version": new_version_id,
            "parent_version": base["policy_version"],
            "created_at": datetime.now(UTC).isoformat(),
            "approved_by": approved_by,
            "suggestion_id": suggestion.get("suggestion_id"),
            "status": "canary" if canary_on else "active",
            "scope": {
                "level": (scope or suggestion.get("scope") or {}).get("level", "user"),
                "canary": {
                    "mode": self._canary_cfg.get("mode", "time_window"),
                    "duration_ms": duration,
                    "until": (datetime.now(UTC) + timedelta(milliseconds=duration)).isoformat(),
                    "rollback_margin": self._canary_cfg.get("rollback_margin", 0.05),
                } if canary_on else None,
            },
            "patch": {"patches": patches, "note": note, "previous": previous},
            "policy": new_dict,
            "changed": {"path": target.get("path"), "from": target.get("current"),
                        "to": target.get("proposed")},
        }
        await self._store.put_version(record)
        if hasattr(self._store, "set_active"):
            await self._store.set_active(new_version_id)
        return record

    # ------------------------------------------------------------------
    async def activate(self, version_id: str) -> dict:
        rec = await self._store.get_version(version_id)
        if rec is None:
            raise DispatcherError("not_found", f"策略版本不存在：{version_id}")
        prior = await self._store.active_version()
        if prior is not None and prior["policy_version"] != version_id:
            await self._store.put_version({**prior, "status": "superseded"})
        rec = {**rec, "status": "active", "scope": {**rec.get("scope", {}), "canary": None}}
        await self._store.put_version(rec)
        if hasattr(self._store, "set_active"):
            await self._store.set_active(version_id)
        return rec

    async def rollback(self, *, to_version: str, note: str | None = None) -> dict:
        """回滚。**即时、彻底、无副作用**——这正是"进化只动配置"的收益之一。"""
        target = await self._store.get_version(to_version)
        if target is None:
            raise DispatcherError("not_found", f"要回滚到的版本不存在：{to_version}")
        active = await self._store.active_version()
        if active is not None:
            await self._store.put_version({
                **active, "status": "rolled_back",
                "scope": {**active.get("scope", {}), "rolled_back_at": datetime.now(UTC).isoformat(),
                          "rolled_back_note": note},
            })
        target = {**target, "status": "active", "scope": {**target.get("scope", {}), "canary": None}}
        await self._store.put_version(target)
        if hasattr(self._store, "set_active"):
            await self._store.set_active(to_version)
        return target

    # ------------------------------------------------------------------
    async def check_canary(self, *, logs: list[RunLog], now: datetime | None = None) -> dict:
        """金丝雀检查：转正还是回滚。

        对比的是**新版本自己的运行日志**与**基线版本在同一时间窗内的日志**。
        两边样本都不够时不下结论（返回 ``pending``）——凭两三条日志判优劣，
        比不判更糟。
        """
        now = now or datetime.now(UTC)
        active = await self._store.active_version()
        if active is None or active.get("status") != "canary":
            return {"outcome": "not_canarying"}

        canary = (active.get("scope") or {}).get("canary") or {}
        base_id = active.get("parent_version")
        margin = float(canary.get("rollback_margin", 0.05))

        new_logs = [x for x in logs if x.policy_version == active["policy_version"]]
        base_logs = [x for x in logs if x.policy_version == base_id]
        if len(new_logs) < 5 or len(base_logs) < 5:
            return {
                "outcome": "pending",
                "detail": f"样本不足（新版 {len(new_logs)} 条 / 基线 {len(base_logs)} 条），"
                          f"需要各至少 5 条才能比较",
                "canary_version": active["policy_version"],
                "baseline_version": base_id,
            }

        now_g = compute_guardrails(new_logs)
        base_g = compute_guardrails(base_logs)
        degradations = {
            k: now_g[k] - base_g[k] for k in GUARDRAIL_METRICS if now_g[k] - base_g[k] > margin
        }

        expired = True
        if canary.get("until"):
            try:
                expired = now >= datetime.fromisoformat(str(canary["until"]))
            except ValueError:
                expired = True

        if degradations:
            rolled = await self.rollback(
                to_version=str(base_id),
                note=f"金丝雀护栏劣化：{degradations}",
            )
            return {
                "outcome": "rolled_back",
                "canary_version": active["policy_version"],
                "baseline_version": base_id,
                "degradations": degradations,
                "now": now_g, "baseline": base_g,
                "active_version": rolled["policy_version"],
            }
        if not expired:
            return {
                "outcome": "pending",
                "detail": f"仍在金丝雀窗口内（至 {canary.get('until')}）",
                "now": now_g, "baseline": base_g,
            }
        promoted = await self.activate(active["policy_version"])
        return {
            "outcome": "promoted",
            "canary_version": promoted["policy_version"],
            "baseline_version": base_id,
            "now": now_g, "baseline": base_g,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _next_version_id(base: str) -> str:
        """版本号形如 ``pv_2026-10-01_03`` → ``pv_2026-10-01_04``。

        刻意保持**人类可读且有序**：排查时"哪一版更新"应该一眼看得出，
        用随机 id 会让这件事变成一个必须查表的问题。
        """
        head, sep, tail = base.rpartition("_")
        if sep and tail.isdigit():
            return f"{head}_{int(tail) + 1:02d}"
        return f"{base}_01"


__all__ = [
    "GUARDRAIL_METRICS", "PolicyVersionManager", "apply_patches", "compute_guardrails",
]
