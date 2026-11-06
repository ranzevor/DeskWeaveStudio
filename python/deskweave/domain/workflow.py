from __future__ import annotations

from collections.abc import Callable

from deskweave.contracts import schema_issues
from deskweave.domain.values import _shape, matches_type
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps


def validate_workflow(
    definition: dict,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> list[dict]:
    dumps(definition, max_bytes=33_554_432, checkpoint=checkpoint)
    diagnostics = []

    def ordered():
        result = sorted(
            diagnostics,
            key=lambda row: (row.get("nodeId", ""), row.get("fieldPath", ""), row["code"]),
        )
        checkpoint()
        return result

    def add(code, message, node=None, field=None):
        checkpoint()
        item = {"code": code, "message": message}
        if node is not None:
            item["nodeId"] = node
        if field is not None:
            item["fieldPath"] = field[:256]
        diagnostics.append(item)

    for path, _reason in schema_issues("urn:deskweave:workflow:1", definition, checkpoint):
        field = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)
        add("WORKFLOW_SCHEMA", "字段不符合流程格式。", field=field)
    checkpoint()
    if diagnostics:
        return ordered()
    nodes = {node["id"]: node for node in definition["nodes"]}
    if len(nodes) != len(definition["nodes"]):
        add("DUPLICATE_NODE", "节点名称重复。", field="/nodes")
    if definition["entry"] not in nodes:
        add("ENTRY_NOT_FOUND", "流程入口不存在。", field="/entry")
    resource_actions = {
        "flow.call": ("workflow", "workflow"),
        "flow.repeat": ("workflow", "workflow"),
        "flow.loop": ("workflow", "workflow"),
        "vision.match": ("image", "image"),
        "input.trajectory": ("trajectory", "trajectory"),
    }
    edges = {}
    for index, node in enumerate(definition["nodes"]):
        checkpoint()
        identity, params = node["id"], node["params"]
        edges[identity] = [target for target in node["on"].values() if target is not None]
        for outlet, target in node["on"].items():
            checkpoint()
            if target is not None and target not in nodes:
                add(
                    "OUTLET_NOT_FOUND",
                    "连接的目标节点不存在。",
                    identity,
                    f"/nodes/{index}/on/{outlet}",
                )
        if "target" in params and params["target"] not in definition["targets"]:
            add(
                "TARGET_NOT_FOUND",
                "动作使用的目标槽不存在。",
                identity,
                f"/nodes/{index}/params/target",
            )
        binding = resource_actions.get(node["action"])
        if binding:
            field, kind = binding
            if definition["resources"].get(params[field], {}).get("kind") != kind:
                add(
                    "RESOURCE_NOT_FOUND",
                    "动作引用的资源缺失或类型不符。",
                    identity,
                    f"/nodes/{index}/params/{field}",
                )
        if (
            node["action"] in {"flow.break", "flow.continue"}
            and definition.get("kind") != "loopBody"
        ):
            add("LOOP_CONTEXT_REQUIRED", "循环控制动作需要循环体流程。", identity)
        try:
            _shape(params, checkpoint)
        except DeskWeaveError as error:
            if error.code == "DEADLINE_EXCEEDED":
                raise
            add(error.code, error.message, identity, f"/nodes/{index}/params")
    for name, parameter in definition["parameters"].items():
        checkpoint()
        if "default" in parameter and not matches_type(parameter["default"], parameter["type"]):
            add("PARAMETER_TYPE", "默认值与参数类型不一致。", field=f"/parameters/{name}/default")
    visited, active = set(), set()
    for root in nodes:
        checkpoint()
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            checkpoint()
            identity, exiting = stack.pop()
            if exiting:
                active.discard(identity)
            elif identity in active:
                add("GRAPH_CYCLE", "普通节点连接形成循环。", identity)
            elif identity in nodes and identity not in visited:
                visited.add(identity)
                active.add(identity)
                stack.append((identity, True))
                stack.extend((target, False) for target in reversed(edges[identity]))
    reachable, pending = set(), [definition["entry"]]
    while pending:
        checkpoint()
        identity = pending.pop()
        if identity in reachable or identity not in nodes:
            continue
        reachable.add(identity)
        pending.extend(edges[identity])
    for identity in nodes.keys() - reachable:
        add("UNREACHABLE_NODE", "节点无法从流程入口到达。", identity)
    return ordered()
