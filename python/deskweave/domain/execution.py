from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from types import MappingProxyType

from deskweave.domain.frozen import EMPTY, NULL, Value
from deskweave.domain.workflow import validate_workflow
from deskweave.errors import DeskWeaveError

CORE_ACTIONS = frozenset(
    {
        "time.wait",
        "data.assign",
        "logic.branch",
        "logic.assert",
        "flow.return",
        "flow.call",
        "flow.join",
        "flow.repeat",
        "flow.loop",
        "flow.break",
        "flow.continue",
    }
)
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    definition: dict
    nodes: object
    joins: object
    error_joins: object
    order: tuple
    regions: object
    effects: object = None
    shared_effects: object = None


def compile_workflow(definition, checkpoint=lambda: None):
    if validate_workflow(definition, checkpoint=checkpoint):
        raise DeskWeaveError("INVALID_WORKFLOW", "流程结构或连接无效。")
    nodes = {node["id"]: node for node in definition["nodes"]}
    edges, incoming = {}, dict.fromkeys(nodes, 0)
    for key, node in nodes.items():
        successors = {target for target in node["on"].values() if target is not None}
        edges[key] = successors
        for target in successors:
            incoming[target] += 1
    pending = deque(key for key in nodes if incoming[key] == 0)
    order = []
    while pending:
        checkpoint()
        key = pending.popleft()
        order.append(key)
        for target in edges[key]:
            incoming[target] -= 1
            if not incoming[target]:
                pending.append(target)
    levels = max(1, (len(nodes) + 1).bit_length())
    ancestors, depth = {None: [None] * levels}, {None: 0}

    def lca(left, right):
        if depth[left] < depth[right]:
            left, right = right, left
        delta = depth[left] - depth[right]
        for bit in range(levels):
            if delta & (1 << bit):
                left = ancestors[left][bit]
        if left == right:
            return left
        for bit in reversed(range(levels)):
            if ancestors[left][bit] != ancestors[right][bit]:
                left, right = ancestors[left][bit], ancestors[right][bit]
        return ancestors[left][0]

    def common(targets):
        iterator = iter(targets)
        result = next(iterator, None)
        for target in iterator:
            result = lca(result, target)
        return result

    joins, error_joins, parents = {}, {}, {}
    for key in reversed(order):
        checkpoint()
        node = nodes[key]
        normal = (
            [node["on"].get("yes"), node["on"].get("no")]
            if node["action"] == "logic.branch"
            else [None]
            if node["action"] in {"flow.return", "flow.break", "flow.continue"}
            else [node["on"].get("next")]
        )
        joins[key] = common(normal)
        targets = normal + ([node["on"]["error"]] if node["on"].get("error") else [])
        parent = common(targets)
        error_joins[key] = common(normal + [node["on"].get("error")])
        parents.setdefault(parent, []).append(key)
        depth[key] = depth[parent] + 1
        ancestors[key] = [parent]
        for bit in range(1, levels):
            ancestors[key].append(ancestors[ancestors[key][bit - 1]][bit - 1])
    regions, serial = {}, 0
    stack = [(None, False)]
    while stack:
        key, exiting = stack.pop()
        checkpoint()
        if exiting:
            regions[key] = (regions[key][0], serial)
        else:
            serial += 1
            regions[key] = (serial, None)
            stack.append((key, True))
            stack.extend((child, False) for child in parents.get(key, ()))
    return CompiledWorkflow(
        definition,
        MappingProxyType(nodes),
        MappingProxyType(joins),
        MappingProxyType(error_joins),
        tuple(order),
        MappingProxyType(regions),
    )


def bind_effects(programs, bindings, checkpoint=lambda: None):
    from dataclasses import replace

    done = set()
    for root in programs:
        stack = [(root, False)]
        while stack:
            key, exiting = stack.pop()
            checkpoint()
            if key in done:
                continue
            if not exiting:
                stack.append((key, True))
                stack.extend(
                    ((item["workflowId"], item["revision"]), False)
                    for item in bindings[key].values()
                    if item["kind"] == "workflow"
                )
                continue
            program = programs[key]
            effects, shared = {}, {}
            for identity in reversed(program.order):
                checkpoint()
                node = program.nodes[identity]
                action, params = node["action"], node["params"]
                own = (
                    action == "data.assign"
                    or "store" in node
                    or action in {"flow.call", "flow.join", "flow.repeat", "flow.loop"}
                )
                writes_shared = action == "data.assign" and params.get("scope") == "run"
                if action in {"flow.call", "flow.repeat", "flow.loop"}:
                    item = bindings[key][params["workflow"]]
                    child = programs[item["workflowId"], item["revision"]]
                    writes_shared |= child.shared_effects[child.definition["entry"]]
                effects[identity] = (
                    own
                    or writes_shared
                    or any(effects[target] for target in node["on"].values() if target)
                )
                shared[identity] = writes_shared or any(
                    shared[target] for target in node["on"].values() if target
                )
            programs[key] = replace(
                program, effects=MappingProxyType(effects), shared_effects=MappingProxyType(shared)
            )
            done.add(key)


@dataclass(slots=True)
class Frame:
    id: str
    parent: str | None
    workflow: tuple[str, str]
    node: str
    inputs: Value = EMPTY
    variables: Value = EMPTY
    previous: Value = NULL
    outputs: Value = NULL
    calls: Value = EMPTY
    iteration_result: Value = NULL
    state: str = "ready"
    targets: dict = field(default_factory=dict)
    phase: dict = field(default_factory=dict)
    children: frozenset[str] = frozenset()
    control: tuple = ()
    taint_epochs: dict = field(default_factory=dict)
    inherited: bool = False
    loop_owner: str | None = None
    loop_index: int = 0
    loop_last: Value = NULL
    depth: int = 1
    depth_limit: int = 8
    background: bool = False
    parallel: int = 0
    steps: int = 0
    deadline: float = 0
    error: dict | None = None
    signal: str | None = None
    signal_owner: str | None = None
    visit: int = 0

    @property
    def sensitive(self):
        return self.inherited or any(marked for _join, marked in self.control)

    def metadata(self):
        return {
            "phase": self.phase,
            "targets": {name: list(target) for name, target in self.targets.items()},
            "outcomeSensitive": self.sensitive,
            "control": [list(item) for item in self.control],
            "taintEpochs": [list(item) for item in self.taint_epochs.items()],
            "inherited": self.inherited,
            "loopOwner": self.loop_owner,
            "loopIndex": self.loop_index,
            "depth": self.depth,
            "depthLimit": self.depth_limit,
            "background": self.background,
            "parallel": self.parallel,
            "steps": self.steps,
            "deadline": self.deadline,
            "signal": self.signal,
            "signalOwner": self.signal_owner,
            "visit": self.visit,
        }


@dataclass(slots=True)
class Run:
    id: str
    root: str
    plan: dict
    programs: dict
    access: object
    frames: dict
    shared: Value
    shared_version: int
    state: str
    control_version: int
    data_version: int
    last_seq: int
    version: int
    started: float
    paused_at: float | None = None
    paused_total: float = 0
    steps: int = 0
    background: int = 0
    label_epoch: int = 0
    error: dict | None = None

    def active(self, now):
        return (
            (self.paused_at if self.paused_at is not None else now)
            - self.started
            - self.paused_total
        )
