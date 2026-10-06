"""建议的确定性校验 —— "04 不能改代码"这条边界的执行者。

LLM 起草建议，**每一个字段**都要过这里。这不是不信任模型，而是这套机制的安全
性质所在：

> LLM 永远被配置定义的集合所限，而配置经人批准。

校验分三类，逐条对应 ``docs/06-self-evolve.md`` 里承诺的那些约束：

1. **形状合法** —— 建议类型在封闭集里，目标产物在允许清单里，**不能指向代码**。
2. **落点存在** —— 路径能解析到策略里的一个真实位置，且不在 ``locked_paths`` 里。
3. **改动有界** —— 类型一致、数值幅度不超过 ``max_delta_ratio``、样本量达标、
   影响面能解析到真实存在的对象。

不过关的建议**不入库**（``status=discarded``），也就永远不会呈现给使用者。
把它们混进待审列表会稀释注意力——一个满是垃圾的建议列表，人看两次就不看了。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from ..core.policy import Policy, resolve_policy_path
from ..core.yamlio import load_yaml

CODE_SUFFIXES = (".py", ".ts", ".dart", ".js", ".go", ".java", ".rs")


@dataclass
class ValidationResult:
    ok: bool
    guards: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class SuggestionValidator:
    def __init__(self, *, policy: Policy, kinds_path, suggestion_kinds: dict | None = None) -> None:
        self._policy = policy
        self._kinds_doc = suggestion_kinds or load_yaml(kinds_path)
        self._kinds = {k["id"]: k for k in self._kinds_doc.get("kinds", [])}
        self._forbidden = tuple(self._kinds_doc.get("forbidden_target_artifacts") or ())
        self._policy_dict = policy.model_dump(mode="json")

    # ------------------------------------------------------------------
    def validate(self, suggestion: dict) -> ValidationResult:
        notes: list[str] = []
        guards: dict[str, bool] = {}

        kind_id = suggestion.get("kind")
        kind = self._kinds.get(str(kind_id))
        guards["kind_known"] = kind is not None
        if kind is None:
            notes.append(f"未知的建议类型 {kind_id!r}——只能在 {sorted(self._kinds)} 里选")
            return ValidationResult(False, guards, notes)

        target = suggestion.get("target") or {}
        artifact = str(target.get("artifact") or "")
        path = str(target.get("path") or "")

        # --- 1. 产物不能是代码 ------------------------------------------
        is_code = artifact.endswith(CODE_SUFFIXES) or artifact.startswith("dispatcher/")
        guards["not_code"] = not is_code
        if is_code:
            notes.append(
                f"目标产物指向代码（{artifact}）。**代码改动必须由开发者写、经评审**——"
                f"运行日志里含用户文本与模型输出，让分析环节能写代码就等于打通了"
                f"'用户内容 → LLM → 可执行代码'的路径。"
            )
            return ValidationResult(False, guards, notes)

        allowed_artifacts = kind.get("target_artifact")
        allowed = (
            [allowed_artifacts]
            if isinstance(allowed_artifacts, str)
            else list(allowed_artifacts or [])
        )
        guards["artifact_allowed_for_kind"] = artifact in allowed
        if artifact not in allowed:
            notes.append(f"类型 {kind_id} 不允许改 {artifact!r}；允许的是 {allowed}")

        # --- 2. 禁区 ----------------------------------------------------
        hit_forbidden = _matches_any(artifact, path, self._forbidden)
        guards["not_forbidden"] = not hit_forbidden
        if hit_forbidden:
            notes.append(f"目标落在 forbidden_target_artifacts 里：{artifact} / {path}")

        locked = self._policy.is_locked(path) or any(
            path.startswith(p.replace(".*", ".")) for p in self._policy.locked_paths
        )
        guards["locked_path"] = locked
        if locked:
            notes.append(f"路径 {path!r} 命中 locked_paths——这些内容不可由自进化修改")

        # --- 3. 落点存在 + 类型一致 -------------------------------------
        exists, current = resolve_policy_path(self._policy_dict, path)
        guards["path_exists"] = exists
        if not exists and artifact == "routing.policy.yaml":
            notes.append(f"路径 {path!r} 在策略里不存在")
        elif exists:
            proposed = target.get("proposed")
            same_type = _type_compatible(current, proposed)
            guards["type_match"] = same_type
            if not same_type:
                notes.append(
                    f"类型不一致：当前 {type(current).__name__}，建议 {type(proposed).__name__}"
                )
            if isinstance(current, (int, float)) and isinstance(proposed, (int, float)):
                delta_ok, detail = self._delta_ok(float(current), float(proposed))
                guards["delta_within_limit"] = delta_ok
                if not delta_ok:
                    notes.append(detail)

        # --- 4. 样本量 --------------------------------------------------
        sample = int(suggestion.get("sample_size") or 0)
        min_sample = self._policy.evolution.min_sample_size
        guards["sample_ok"] = sample >= min_sample
        if sample < min_sample:
            notes.append(f"样本量 {sample} 低于 min_sample_size={min_sample}")

        # --- 5. 影响面可解析 --------------------------------------------
        blast = suggestion.get("blast_radius") or []
        unresolved = [b for b in blast if not _resolve_blast(b, self._policy)]
        guards["blast_radius_resolvable"] = not unresolved
        if unresolved:
            notes.append(f"影响面里有解析不到的对象：{unresolved}")

        # **`guards` 里有一个语义相反的标志位。**
        #
        # ``locked_path`` 按契约（``schemas/suggestion.json``）是"目标是否落在禁区里"，
        # **true 表示非法**；其余都是"必须为真才算通过"。把它们一起丢进
        # ``all(guards.values())`` 会让每一条建议都被拒，而理由栏是空的——
        # 因为这个 False 本身没有对应的 note。这个 bug 是"正常建议应当通过"
        # 那个用例抓出来的：只测拒绝的测试永远发现不了它。
        #
        # 所以这里显式分成两组，而不是靠 `all()`。
        must_be_true = (
            "kind_known",
            "not_code",
            "artifact_allowed_for_kind",
            "not_forbidden",
            "path_exists",
            "type_match",
            "delta_within_limit",
            "sample_ok",
            "blast_radius_resolvable",
        )
        ok = all(guards.get(k, True) for k in must_be_true) and not guards.get("locked_path", False)
        if ok:
            notes.append("全部确定性校验通过")
        return ValidationResult(ok, guards, notes)

    # ------------------------------------------------------------------
    def _delta_ok(self, current: float, proposed: float) -> tuple[bool, str]:
        """数值改动幅度受限。**防的是"把预算上限提高 100 倍"这类改动。**"""
        limit = self._policy.evolution.max_delta_ratio
        if current == 0:
            return (
                proposed == 0,
                "当前值为 0，改动幅度没有参照系，任何非零改动都会被拒",
            )
        ratio = abs(proposed - current) / abs(current)
        if ratio > limit:
            return (
                False,
                f"改动幅度 {ratio:.0%} 超过 max_delta_ratio={limit:.0%}"
                f"（{current} → {proposed}）。想要更大的改动，请分多次提。",
            )
        return True, ""


# ---------------------------------------------------------------------------
def _type_compatible(current: Any, proposed: Any) -> bool:
    if isinstance(current, bool) or isinstance(proposed, bool):
        return isinstance(current, bool) and isinstance(proposed, bool)
    if isinstance(current, (int, float)) and isinstance(proposed, (int, float)):
        return True
    if isinstance(current, str) and isinstance(proposed, str):
        return True
    return isinstance(current, type(proposed)) and isinstance(proposed, type(current))


def _matches_any(artifact: str, path: str, patterns: tuple[str, ...]) -> bool:
    """判断目标是否落在某个禁区模式里。

    ``**`` 有两种形态，混起来写会造成一个很宽的误伤：

    * ``dispatcher/core/**`` —— 目录前缀，匹配该目录下任意深度；
    * ``**/*.py`` —— 任意深度下的某类文件，匹配的是**后缀**。

    之前把两者都按"取 ``**`` 之前的部分做前缀"处理，于是 ``**/*.py`` 的前缀是空串，
    **匹配一切**——包括 ``routing.policy.yaml``。结果是每一条建议都被拒，
    而理由写着"落在禁区里"，看起来还很合理。这个 bug 是被"正常建议应当通过"
    这个用例抓出来的：只测拒绝、不测放行的测试永远发现不了它。
    """
    for pat in patterns:
        if "**/" in pat:
            tail = pat.split("**/", 1)[1]
            if fnmatch.fnmatch(artifact, tail) or fnmatch.fnmatch(artifact, f"*/{tail}"):
                return True
            continue
        if pat.endswith("/**"):
            prefix = pat[: -len("/**")]
            if prefix and artifact.startswith(prefix):
                return True
            continue
        if "**" in pat:
            prefix = pat.split("**")[0].rstrip("/")
            if prefix and artifact.startswith(prefix):
                return True
            continue
        if artifact == pat:
            return True
        if "#" in pat:
            art, frag = pat.split("#", 1)
            frag = frag.rstrip("*")
            if artifact.endswith(art.split("/")[-1]) and path.startswith(frag):
                return True
            continue
        if pat.endswith(".*") and path.startswith(pat[:-1]):
            return True
    return False


def _resolve_blast(entry: str, policy: Policy) -> bool:
    """``route:direct_answer`` / ``tier:standard`` 这类影响面必须能解析到真实对象。"""
    if ":" not in entry:
        return True
    kind, _, name = entry.partition(":")
    if kind == "route":
        return policy.route(name) is not None
    if kind == "tier":
        return policy.has_tier(name)
    # 工具与 handler 由注册表管，校验器不持有它——留给调用方校验
    return True


__all__ = ["SuggestionValidator", "ValidationResult"]
