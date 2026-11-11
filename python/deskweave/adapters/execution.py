from __future__ import annotations

from dataclasses import dataclass, field, replace

from deskweave.adapters.workspace import utc_time
from deskweave.domain.frozen import Value
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads


def initialize_execution_storage(connection):
    for sql in (
        "CREATE TABLE IF NOT EXISTS execution_run (run_id TEXT PRIMARY KEY REFERENCES run(id), "
        "version INTEGER NOT NULL, shared_version INTEGER NOT NULL, steps INTEGER NOT NULL, "
        "active_ms INTEGER NOT NULL, background INTEGER NOT NULL, error TEXT, "
        "clock_state TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS execution_frame (frame_id TEXT PRIMARY KEY "
        "REFERENCES frame(id), "
        "run_id TEXT NOT NULL REFERENCES run(id), metadata TEXT NOT NULL, error TEXT)",
        "CREATE TABLE IF NOT EXISTS execution_value (run_id TEXT NOT NULL REFERENCES run(id), "
        "frame_id TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL, version "
        "INTEGER NOT NULL, "
        "value_hash TEXT NOT NULL, labels_hash TEXT NOT NULL, valid_until INTEGER, "
        "PRIMARY KEY(run_id,frame_id,scope,name,version))",
        "CREATE UNIQUE INDEX IF NOT EXISTS execution_current_value "
        "ON execution_value(run_id,frame_id,scope,name) WHERE valid_until IS NULL",
        "CREATE TABLE IF NOT EXISTS execution_unhandled (parent_id TEXT NOT NULL "
        "REFERENCES frame(id), "
        "child_id TEXT NOT NULL REFERENCES frame(id), PRIMARY KEY(parent_id,child_id))",
    ):
        connection.execute(sql)


@dataclass
class Change:
    run: object
    frames: dict = field(default_factory=dict)
    new: set = field(default_factory=set)
    values: dict = field(default_factory=dict)
    removed: set = field(default_factory=set)
    handled: set = field(default_factory=set)
    unhandled: set = field(default_factory=set)
    events: list = field(default_factory=list)
    ready: set = field(default_factory=set)
    system: bool = False

    def get(self, identity):
        return self.frames.get(identity) or self.run.frames[identity]

    def edit(self, identity):
        if identity not in self.frames:
            source = self.run.frames[identity]
            self.frames[identity] = replace(
                source, phase=dict(source.phase), taint_epochs=dict(source.taint_epochs)
            )
        return self.frames[identity]

    def value(self, frame_id, scope, name, value):
        self.values[(frame_id, scope, name)] = value


class ExecutionStore:
    def __init__(self, workspace):
        self.workspace = workspace

    def commit(self, before, change, now, *, initial=False, checkpoint=lambda: None):
        run = change.run
        data_version = before.data_version + bool(change.frames or change.values)
        version = before.version + 1
        objects, initial_objects = {}, {}

        def authorize():
            if not change.system:
                run.access.revalidate()
                checkpoint()

        with self.workspace.budget(None, authorize=authorize):
            for key, value in change.values.items():
                objects[key] = (
                    self.workspace._put_object(value.raw()),
                    self.workspace._put_object(value.labels()),
                )
            for identity in change.new:
                frame = change.frames[identity]
                initial_objects[identity] = self.workspace._put_object(
                    {"inputs": frame.inputs.raw(), "variables": frame.variables.raw()}
                )
            events = list(change.events)
            scopes = set()
            for identity, scope, _name in change.values:
                if scope not in {"runtime", "calls"} and (identity, scope) not in scopes:
                    scopes.add((identity, scope))
                    event = (
                        "runtime.variables.changed",
                        {
                            "runId": run.id,
                            "frameId": identity or None,
                            "scope": scope,
                            "dataVersion": data_version,
                            "controlVersion": run.control_version,
                        },
                    )
                    events.append(event)
            if run.state != before.state:
                events.append(
                    (
                        "run.state.changed",
                        {
                            "state": run.state,
                            "controlVersion": run.control_version,
                            "dataVersion": data_version,
                        },
                    )
                )
            event_objects = [
                (kind, self.workspace._put_object(payload)) for kind, payload in events
            ]
        created = utc_time()
        with self.workspace.transaction(authorize=authorize) as connection:
            if initial:
                connection.execute(
                    "INSERT INTO execution_run VALUES(?,?,?,?,?,?,?,?)",
                    (run.id, before.version, before.shared_version, 0, 0, 0, None, "{}"),
                )
            row = connection.execute(
                "UPDATE execution_run SET version=?,shared_version=?,steps=?,active_ms=?,"
                "background=?,error=?,clock_state=? WHERE run_id=? AND version=?",
                (
                    version,
                    run.shared_version,
                    run.steps,
                    int(run.active(now) * 1000),
                    run.background,
                    dumps(run.error).decode(),
                    dumps(
                        {
                            "started": run.started,
                            "pausedAt": run.paused_at,
                            "pausedTotal": run.paused_total,
                            "labelEpoch": run.label_epoch,
                        }
                    ).decode(),
                    run.id,
                    before.version,
                ),
            )
            if row.rowcount != 1:
                raise DeskWeaveError("REVISION_CONFLICT", "运行状态版本已变化。")
            for identity, frame in change.frames.items():
                if identity in change.new:
                    connection.execute(
                        "INSERT INTO "
                        "frame(id,run_id,parent_id,workflow_id,revision_id,state,node_id,"
                        "state_hash) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            identity,
                            run.id,
                            frame.parent,
                            *frame.workflow,
                            frame.state,
                            frame.node,
                            initial_objects[identity],
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE frame SET state=?,node_id=? WHERE id=? AND run_id=?",
                        (frame.state, frame.node, identity, run.id),
                    )
                connection.execute(
                    "INSERT INTO execution_frame VALUES(?,?,?,?) "
                    "ON CONFLICT(frame_id) DO UPDATE SET "
                    "metadata=excluded.metadata,error=excluded.error",
                    (
                        identity,
                        run.id,
                        dumps(frame.metadata()).decode(),
                        dumps(frame.error).decode(),
                    ),
                )
            for (identity, scope, name), (value_hash, labels_hash) in objects.items():
                old = connection.execute(
                    "SELECT value_hash,labels_hash FROM execution_value WHERE run_id=? "
                    "AND frame_id=? "
                    "AND scope=? AND name=? AND valid_until IS NULL",
                    (run.id, identity, scope, name),
                ).fetchone()
                if old is not None and tuple(old) == (value_hash, labels_hash):
                    continue
                connection.execute(
                    "UPDATE execution_value SET valid_until=? WHERE run_id=? AND frame_id=? AND "
                    "scope=? AND name=? AND valid_until IS NULL",
                    (data_version, run.id, identity, scope, name),
                )
                connection.execute(
                    "INSERT INTO execution_value VALUES(?,?,?,?,?,?,?,NULL)",
                    (run.id, identity, scope, name, data_version, value_hash, labels_hash),
                )
            for parent, child in change.unhandled:
                connection.execute(
                    "INSERT OR IGNORE INTO execution_unhandled VALUES(?,?)", (parent, child)
                )
            for parent, child in change.handled:
                connection.execute(
                    "DELETE FROM execution_unhandled WHERE parent_id=? AND child_id=?",
                    (parent, child),
                )
            for seq, (kind, payload) in enumerate(event_objects, before.last_seq + 1):
                connection.execute(
                    "INSERT INTO run_event VALUES(?,?,?,?,?)", (run.id, seq, kind, payload, created)
                )
            last_seq = before.last_seq + len(event_objects)
            connection.execute(
                "UPDATE run SET "
                "state=?,control_version=?,data_version=?,last_seq=?,updated_at=? WHERE id=?",
                (run.state, run.control_version, data_version, last_seq, created, run.id),
            )
        run.version, run.data_version, run.last_seq = version, data_version, last_seq
        for identity, frame in change.frames.items():
            if identity in change.removed:
                run.frames.pop(identity, None)
            else:
                run.frames[identity] = frame

    def check_access(self, run_id, access, permission):
        access.require(permission, "workflow.read")
        with self.workspace.transaction(authorize=access.revalidate) as connection:
            row = connection.execute(
                "SELECT owner_client_id,authorization_ref,authorization_version,root_frame_id "
                "FROM run WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise DeskWeaveError("RESOURCE_NOT_FOUND", "运行不存在。")
            if tuple(row)[:3] != (
                access.client_id,
                access.authorization_ref,
                access.authorization_version,
            ):
                raise DeskWeaveError("ACCESS_DENIED", "当前授权不能访问此运行。")
            if access.workflow_ids is not None:
                denied = connection.execute(
                    "SELECT 1 FROM run_workflow_ref ref JOIN revision r ON r.id=ref.revision_id "
                    "WHERE ref.run_id=? AND r.workflow_id NOT IN (SELECT value FROM "
                    "json_each(?)) LIMIT 1",
                    (run_id, dumps(sorted(access.workflow_ids)).decode()),
                ).fetchone()
                if denied:
                    raise DeskWeaveError("ACCESS_DENIED", "当前授权不能访问运行的依赖流程。")
            return row["root_frame_id"]

    def check_frame(self, run_id, frame_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM frame WHERE run_id=? AND id=?", (run_id, frame_id)
            ).fetchone()
        if row is None:
            raise DeskWeaveError("INVALID_ARGUMENT", "调用帧不属于此运行。")

    def value(self, run_id, frame_id, scope, name, *, version=None):
        with self.workspace.transaction() as connection:
            predicate = (
                "valid_until IS NULL"
                if version is None
                else "version<=? AND (valid_until IS NULL OR valid_until>?)"
            )
            args = (run_id, frame_id, scope, name)
            if version is not None:
                args += (version, version)
            row = connection.execute(
                "SELECT value_hash,labels_hash FROM execution_value WHERE run_id=? AND frame_id=? "
                f"AND scope=? AND name=? AND {predicate} ORDER BY version DESC LIMIT 1",
                args,
            ).fetchone()
        if row is None:
            return None
        return Value.freeze(
            self.workspace._read_object(row[0]), self.workspace._read_object(row[1])
        )

    def value_names(self, run_id, frame_id, scope, version, *, after=None, page_size=64):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("变量名称页大小应为 1～100。")
        with self.workspace.transaction() as connection:
            rows = connection.execute(
                "SELECT name FROM execution_value WHERE run_id=? AND frame_id=? AND scope=? "
                "AND version<=? AND (valid_until IS NULL OR valid_until>?) "
                "AND name>? ORDER BY name LIMIT ?",
                (run_id, frame_id, scope, version, version, after or "", page_size),
            ).fetchall()
        return [row[0] for row in rows]

    def outcome(self, run_id, frame_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT f.parent_id,f.state,e.error,e.metadata FROM frame f JOIN execution_frame e "
                "ON e.frame_id=f.id WHERE f.run_id=? AND f.id=?",
                (run_id, frame_id),
            ).fetchone()
        if row is None:
            raise DeskWeaveError("INVALID_ARGUMENT", "调用句柄不属于此运行。")
        return {
            "parent": row["parent_id"],
            "state": row["state"],
            "error": loads(row["error"]),
            "metadata": loads(row["metadata"]),
            "output": self.value(run_id, frame_id, "outputs", "") or Value.freeze({}),
        }

    def unhandled(self, run_id, parent_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT u.child_id FROM execution_unhandled u JOIN frame f ON f.id=u.child_id "
                "WHERE u.parent_id=? AND f.run_id=? ORDER BY f.sequence LIMIT 1",
                (parent_id, run_id),
            ).fetchone()
        return None if row is None else self.outcome(run_id, row[0])["error"]

    def snapshot(self, run_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT r.*,e.shared_version,e.active_ms,e.error,e.steps FROM run r "
                "JOIN execution_run e ON r.id=e.run_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise DeskWeaveError("RESOURCE_NOT_FOUND", "运行执行状态不存在。")
        return {
            "runId": run_id,
            "state": row["state"],
            "controlVersion": row["control_version"],
            "dataVersion": row["data_version"],
            "lastSeq": str(row["last_seq"]),
            "sharedVersion": row["shared_version"],
            "activeMs": row["active_ms"],
            "steps": row["steps"],
            "error": loads(row["error"]),
        }
