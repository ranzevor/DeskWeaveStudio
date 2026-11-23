from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

from deskweave.contracts import validate
from deskweave.domain.platform import topology_errors
from deskweave.errors import DeskWeaveError
from deskweave.json import digest

_MAX_SAFE_INT = 9_007_199_254_740_991
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|([+-])([0-9]{2}):([0-9]{2}))$"
)


def _is_valid_ident(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 256 and "\x00" not in value


def _try_parse_rfc3339(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    match = _RFC3339_RE.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    frac = match.group(7) or ""
    microsecond = int((frac + "000000")[:6])
    if match.group(8) == "Z":
        tz = UTC
    else:
        sign = 1 if match.group(9) == "+" else -1
        off_hours = int(match.group(10))
        off_mins = int(match.group(11))
        if not (0 <= off_hours <= 23 and 0 <= off_mins <= 59):
            return None
        tz = timezone(sign * timedelta(hours=off_hours, minutes=off_mins))
    try:
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tz)
    except ValueError:
        return None


def pause_snapshot_errors(value: dict) -> list[str]:
    errors: list[str] = []
    cursor = value["cursor"]
    if cursor is not None and cursor["nextEventIndex"] > cursor["eventCount"]:
        errors.append("暂停游标超过源事件总数")
    created_at = _try_parse_rfc3339(value["createdAt"])
    expires_at = _try_parse_rfc3339(value["expiresAt"])
    if created_at is None or expires_at is None:
        errors.append("暂停时间格式无效")
    elif created_at >= expires_at:
        errors.append("暂停期限没有覆盖创建时间")
    held = value["heldInputs"]
    ordered = sorted(held, key=lambda v: (v["kind"], v.get("key", v.get("button", ""))))
    if held != ordered:
        errors.append("暂停持有集合顺序不稳定")
    tokens = [v["targetToken"] for v in value["targets"]]
    if len(tokens) != len(set(tokens)) or tokens != sorted(tokens):
        errors.append("暂停目标重复或顺序不稳定")
    for name, field in (("heldInputs", "heldInputsHash"), ("targets", "targetStateHash")):
        if digest(value[name]) != value[field]:
            errors.append("暂停集合摘要不符")
    if value["inputTargetToken"] is not None and value["inputTargetToken"] not in tokens:
        errors.append("暂停输入目标不在授权目标集合")
    if (cursor is not None or held) and value["inputTargetToken"] is None:
        errors.append("暂停输入缺少唯一目标")
    selected = (
        "pauseId",
        "leaseId",
        "agentEpoch",
        "generation",
        "cursor",
        "heldInputsHash",
        "pointer",
        "targetStateHash",
        "inputTargetToken",
    )
    if digest({key: value[key] for key in selected}) != value["checkpointHash"]:
        errors.append("暂停检查点摘要不符")
    return errors


def lease_snapshot_errors(value: dict) -> list[str]:
    errors: list[str] = []
    snapshot = value["pauseSnapshot"]
    if snapshot is not None:
        errors.extend(pause_snapshot_errors(snapshot))
        if any(snapshot[key] != value[key] for key in ("leaseId", "agentEpoch", "generation")):
            errors.append("租约与暂停检查点身份不符")
    return errors


def resume_plan_errors(value: dict, context: dict) -> list[str]:
    errors: list[str] = []
    pause = context["pauseSnapshot"]
    errors.extend(pause_snapshot_errors(pause))
    for key in ("pauseId", "agentEpoch", "generation"):
        if value[key] != pause[key]:
            errors.append("继续计划与实时检查点不符")
    if value["runId"] != context["runId"] or value["controlVersion"] != context["controlVersion"]:
        errors.append("继续计划与运行控制版本不符")
    if value["heldInputs"] != pause["heldInputs"]:
        errors.append("继续计划持有集合改变")
    if value["pointerToRestore"] is not None and value["pointerToRestore"] != pause["pointer"]:
        errors.append("继续指针目的地不符")
    if value["restoreTargetToken"] is not None:
        if value["restoreTargetToken"] != pause["inputTargetToken"] or value[
            "restoreTargetToken"
        ] not in {target["targetToken"] for target in pause["targets"]}:
            errors.append("继续恢复目标没有绑定暂停输入目标")
    elif value["heldInputs"] or value["pointerToRestore"]:
        errors.append("继续恢复输入缺少目标")
    if value["requiresConfirmation"] != bool(
        value["heldInputs"] or value["pointerToRestore"] or value["restoreTargetToken"]
    ):
        errors.append("继续确认要求不符")
    expires = _try_parse_rfc3339(value["expiresAt"])
    prepared = _try_parse_rfc3339(context["preparedAt"])
    pause_expires = _try_parse_rfc3339(pause["expiresAt"])
    now = _try_parse_rfc3339(context["now"])
    if expires is None or prepared is None or pause_expires is None or now is None:
        errors.append("继续计划时间格式无效")
    else:
        if not prepared < expires <= pause_expires or (expires - prepared).total_seconds() > 60:
            errors.append("继续计划有效期越界")
        if now >= expires:
            errors.append("继续计划已过期")
    if (
        context["currentAgentEpoch"] != pause["agentEpoch"]
        or context["currentGeneration"] != pause["generation"]
    ):
        errors.append("原生代次已经改变")
    if context["currentTargets"] != pause["targets"]:
        errors.append("目标或拓扑已经改变")
    if context["planState"] != "prepared":
        errors.append("继续计划已经消耗或取消")
    selected = (
        "runId",
        "controlVersion",
        "pauseId",
        "agentEpoch",
        "generation",
        "heldInputs",
        "pointerToRestore",
        "restoreTargetToken",
        "expiresAt",
    )
    payload = {key: value[key] for key in selected}
    payload["checkpointHash"] = pause["checkpointHash"]
    if digest(payload) != value["planHash"]:
        errors.append("继续计划摘要不符")
    return errors


def resume_request_errors(value: dict, context: dict) -> list[str]:
    plan = context["resumePlan"]
    errors = resume_plan_errors(plan, context)
    params = value["params"]
    if (
        params["runId"] != plan["runId"]
        or params["expectedControlVersion"] != plan["controlVersion"]
    ):
        errors.append("继续请求运行控制版本不符")
    if plan["requiresConfirmation"]:
        if context.get("clientKind") != "trustedHost":
            errors.append("当前认证来源不能使用本地确认能力")
        registered = context.get("registeredGrant")
        provided = params.get("confirmationGrant")
        if (
            not _is_valid_ident(registered)
            or type(provided) is not str
            or params.get("resumePlanId") != plan["planId"]
            or provided != registered
        ):
            errors.append("继续请求缺少绑定本地确认")
    elif params.get("resumePlanId", plan["planId"]) != plan["planId"]:
        errors.append("继续请求引用其他计划")
    return errors


def validate_pause_snapshot(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:execution-control:1#/$defs/pauseSnapshot",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    issues = pause_snapshot_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_lease_snapshot(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:execution-control:1#/$defs/leaseSnapshot",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    issues = lease_snapshot_errors(value)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def _validate_resume_context(
    context: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    required = {
        "runId",
        "controlVersion",
        "pauseSnapshot",
        "preparedAt",
        "now",
        "currentAgentEpoch",
        "currentGeneration",
        "currentTargets",
        "planState",
    }
    optional = {"resumePlan", "registeredGrant", "clientKind", "sameOriginalRequest"}
    if type(context) is not dict or not required <= context.keys() <= (required | optional):
        raise DeskWeaveError("INVALID_ARGUMENT", "继续计划缺少实时核对上下文。")
    if (
        not _is_valid_ident(context["runId"])
        or not _is_valid_ident(context["currentAgentEpoch"])
        or not _is_valid_ident(context["planState"])
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "继续上下文标识字段无效。")
    for int_key in ("controlVersion", "currentGeneration"):
        val = context[int_key]
        if type(val) is not int or not 0 <= val <= _MAX_SAFE_INT:
            raise DeskWeaveError("INVALID_ARGUMENT", "继续上下文版本或代次字段无效。")
    if (
        _try_parse_rfc3339(context["preparedAt"]) is None
        or _try_parse_rfc3339(context["now"]) is None
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "继续上下文时间字段格式无效。")
    targets = context["currentTargets"]
    if type(targets) is not list or not 1 <= len(targets) <= 32:
        raise DeskWeaveError("INVALID_ARGUMENT", "继续上下文当前目标列表无效。")
    for item in targets:
        validate(
            "urn:deskweave:execution-control:1#/$defs/targetState",
            item,
            checkpoint=checkpoint,
        )
    if "sameOriginalRequest" in context and type(context["sameOriginalRequest"]) is not bool:
        raise DeskWeaveError("INVALID_ARGUMENT", "sameOriginalRequest 必须为布尔值。")
    if "clientKind" in context and not _is_valid_ident(context["clientKind"]):
        raise DeskWeaveError("INVALID_ARGUMENT", "clientKind 字段无效。")
    if (
        "registeredGrant" in context
        and context["registeredGrant"] is not None
        and not _is_valid_ident(context["registeredGrant"])
    ):
        raise DeskWeaveError("INVALID_ARGUMENT", "registeredGrant 字段无效。")
    if "resumePlan" in context:
        validate(
            "urn:deskweave:execution-control:1#/$defs/resumePlan",
            context["resumePlan"],
            checkpoint=checkpoint,
        )
    validate_pause_snapshot(context["pauseSnapshot"], checkpoint=checkpoint)
    return context


def validate_resume_plan(
    value: object,
    context: object,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> dict:
    validate(
        "urn:deskweave:execution-control:1#/$defs/resumePlan",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    ctx = _validate_resume_context(context, checkpoint=checkpoint)
    issues = resume_plan_errors(value, ctx)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_resume_request(
    value: object,
    context: object,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> dict:
    validate("urn:deskweave:message:1", value, checkpoint=checkpoint)
    validate("urn:deskweave:service:1#/$defs/run.resume", value, checkpoint=checkpoint)
    assert isinstance(value, dict)
    ctx = _validate_resume_context(context, checkpoint=checkpoint)
    if "resumePlan" not in ctx:
        raise DeskWeaveError("INVALID_ARGUMENT", "继续请求缺少实时核对上下文。")
    validate(
        "urn:deskweave:execution-control:1#/$defs/resumePlan",
        ctx["resumePlan"],
        checkpoint=checkpoint,
    )
    issues = resume_request_errors(value, ctx)
    if issues:
        raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_target_bind_result(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:agent:1#/$defs/target.bind/properties/result",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    if _try_parse_rfc3339(value["expiresAt"]) is None:
        raise DeskWeaveError("INVALID_ARGUMENT", "目标绑定过期时间格式无效。")
    if "topology" in value and value["topology"] is not None:
        issues = topology_errors(value["topology"])
        if issues:
            raise DeskWeaveError("INVALID_ARGUMENT", issues[0])
    return value


def validate_lease_acquire_result(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:agent:1#/$defs/lease.acquire/properties/result",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    return value


def validate_lease_release_result(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:agent:1#/$defs/lease.release/properties/result",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    return value


def validate_segment_result(
    value: object, *, checkpoint: Callable[[], None] = lambda: None
) -> dict:
    validate(
        "urn:deskweave:agent:1#/$defs/segment.submit/properties/result",
        value,
        checkpoint=checkpoint,
    )
    assert isinstance(value, dict)
    return value


def compute_segment_payload_hash(
    target_token: str,
    operation: dict,
    trajectory_ref: dict | None = None,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> str:
    if type(target_token) is not str or not 1 <= len(target_token) <= 256 or "\x00" in target_token:
        raise DeskWeaveError("INVALID_ARGUMENT", "输入段目标令牌无效。")
    if type(operation) is not dict:
        raise DeskWeaveError("INVALID_ARGUMENT", "输入段 operation 必须为对象。")
    payload: dict[str, object] = {
        "targetToken": target_token,
        "operation": operation,
    }
    if trajectory_ref is not None:
        if (
            type(trajectory_ref) is not dict
            or trajectory_ref.keys() != {"resourceId", "sha256"}
            or not _is_valid_ident(trajectory_ref["resourceId"])
            or type(trajectory_ref["sha256"]) is not str
            or _SHA256_HEX_RE.fullmatch(trajectory_ref["sha256"]) is None
        ):
            raise DeskWeaveError("INVALID_ARGUMENT", "轨迹引用结构无效。")
        payload["trajectorySha256"] = trajectory_ref["sha256"]
    return digest(payload, checkpoint=checkpoint)
