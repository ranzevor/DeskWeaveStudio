from __future__ import annotations

import bisect
import secrets
import uuid

from deskweave.adapters.execution import ExecutionStore
from deskweave.adapters.run_lifecycle import RunLifecycleStore
from deskweave.adapters.workspace import utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps


class Inspection:
    def __init__(self, engine):
        self.engine, self.workspace = engine, engine.workspace
        self.store = ExecutionStore(self.workspace)
        self.snapshots, self.cursors, self.positions = {}, {}, {}
        self.cached_bytes = 0

    def _drop(self, identity):
        item = self.snapshots.pop(identity)
        self.cached_bytes -= item["bytes"]
        for resource_id in item["resources"]:
            resource = self.engine.contents.resources.get(resource_id)
            if resource is not None:
                self.engine.contents._drop(resource)
        self.cursors = {k: v for k, v in self.cursors.items() if v[0] != identity}
        self.positions = {v: k for k, v in self.cursors.items()}

    def release(self, owner):
        for key, value in tuple(self.snapshots.items()):
            if value["owner"] == owner:
                self._drop(key)

    def expire(self):
        for key, value in tuple(self.snapshots.items()):
            if (
                value["deadline"] <= self.engine.monotonic()
                or value["expires"] <= self.engine.clock()
            ):
                self._drop(key)

    def check(self, session, identity):
        self.expire()
        item = self.snapshots.get(identity)
        if item is None or item["owner"] != session.id:
            raise DeskWeaveError("INSPECTION_SNAPSHOT_EXPIRED", "变量快照已过期，请重新读取。")
        self.store.check_access(item["run"], self.engine.run_access(session), "run.read")
        return item

    def read(self, session, params):
        self.store.check_access(params["runId"], self.engine.run_access(session), "run.read")
        self.expire()
        identity = params["snapshotId"]
        created = identity is None
        if created:
            if params["cursor"] is not None:
                raise DeskWeaveError("CURSOR_EXPIRED", "变量游标与当前范围不匹配。")
            if len(self.snapshots) >= 32:
                raise DeskWeaveError("LIMIT_EXCEEDED", "变量快照数量已满。")
            row = RunLifecycleStore(self.workspace).read(params["runId"])
            identity = uuid.uuid4().hex
            self.snapshots[identity] = {
                "owner": session.id,
                "run": params["runId"],
                "version": row["data_version"],
                "control": row["control_version"],
                "created": self.engine.clock(),
                "expires": self.engine.clock() + 60,
                "deadline": self.engine.monotonic() + 60,
                "scopes": {},
                "resources": set(),
                "bytes": 0,
            }
        exported = []
        try:
            item = self.check(session, identity)
            if item["run"] != params["runId"]:
                raise DeskWeaveError("INVALID_ARGUMENT", "变量快照不属于此运行。")
            frame, scope = params["frameId"] or "", params["scope"]
            if frame:
                with self.workspace.transaction() as connection:
                    visible = connection.execute(
                        "SELECT 1 FROM run_frame_version WHERE run_id=? AND frame_id=? "
                        "AND version<=? LIMIT 1",
                        (item["run"], frame, item["version"]),
                    ).fetchone()
                if visible is None:
                    raise DeskWeaveError("INVALID_ARGUMENT", "调用帧不属于此运行快照。")
            after = None
            if params["cursor"] is not None:
                marker = self.cursors.get(params["cursor"])
                if marker is None or marker[:4] != (identity, frame, scope, params["pageSize"]):
                    raise DeskWeaveError("CURSOR_EXPIRED", "变量游标与当前范围不匹配。")
                after = marker[4]
            rows, total = self._rows(item, frame, scope, after, params["pageSize"] + 1)
            result = {
                "runId": item["run"],
                "snapshotId": identity,
                "dataVersion": item["version"],
                "controlVersion": item["control"],
                "frameId": params["frameId"],
                "scope": scope,
                "snapshotAt": utc_time(item["created"]),
                "expiresAt": utc_time(item["expires"]),
                "total": total,
                "items": [],
                "nextCursor": "x" * 32,
            }
            values, size = [], len(dumps(result))
            for name, value in rows[: params["pageSize"]]:
                self.workspace.check_deadline()
                projected, resource, added = self._project(session, identity, item, name, value)
                length = len(dumps(projected))
                if size + length > self.engine.result_limit("debug.variables.read"):
                    if added:
                        item["resources"].discard(resource.resource_id)
                        self.engine.contents._drop(resource)
                    if not values:
                        raise DeskWeaveError("QUERY_ITEM_TOO_LARGE", "变量条目超过页面大小限制。")
                    break
                if added:
                    exported.append(resource)
                values.append(projected)
                size += length + 1
            cursor = None
            if len(values) < len(rows):
                marker = (identity, frame, scope, params["pageSize"], rows[len(values) - 1][0])
                cursor = self.positions.get(marker)
                if cursor is None:
                    if len(self.cursors) >= 32768:
                        raise DeskWeaveError("LIMIT_EXCEEDED", "变量游标数量已满。")
                    cursor = secrets.token_urlsafe(24)
                    self.cursors[cursor] = marker
                    self.positions[marker] = cursor
            return result | {"items": values, "nextCursor": cursor}
        except BaseException:
            if created:
                if identity in self.snapshots:
                    self._drop(identity)
            else:
                for resource in exported:
                    item["resources"].discard(resource.resource_id)
                    if resource.resource_id in self.engine.contents.resources:
                        self.engine.contents._drop(resource)
            raise

    def _rows(self, item, frame, scope, after, limit):
        key = frame, scope
        if scope in {"previous", "outputs"}:
            if key not in item["scopes"]:
                value = self.store.value(item["run"], frame, scope, "", version=item["version"])
                size = 0 if value is None else value.json_size
                if self.cached_bytes + size > 67_108_864:
                    raise DeskWeaveError("LIMIT_EXCEEDED", "变量快照内容容量已满。")
                if value is None:
                    entries = {}
                elif value.kind == "object":
                    entries = {
                        name: child.marked(value.label or bool(value.absent))
                        for name, child in value.data.items()
                    }
                else:
                    entries = {"value": value}
                names = sorted(entries, key=lambda name: name.encode("utf-16-be"))
                item["scopes"][key] = (names, entries)
                item["bytes"] += size
                self.cached_bytes += size
            names, entries = item["scopes"][key]
            start = (
                0
                if after is None
                else bisect.bisect_right(
                    names, after.encode("utf-16-be"), key=lambda name: name.encode("utf-16-be")
                )
            )
            return [(name, entries[name]) for name in names[start : start + limit]], len(names)
        with self.workspace.transaction() as connection:
            predicate = (
                "run_id=? AND frame_id=? AND scope=? AND version<=? "
                "AND (valid_until IS NULL OR valid_until>?)"
            )
            args = (item["run"], frame, scope, item["version"], item["version"])
            if key not in item["scopes"]:
                item["scopes"][key] = connection.execute(
                    "SELECT COUNT(*) FROM execution_value WHERE " + predicate, args
                ).fetchone()[0]
            if after is None:
                names = connection.execute(
                    "SELECT name FROM execution_value WHERE "
                    + predicate
                    + " ORDER BY name COLLATE UTF16 LIMIT ?",
                    (*args, limit),
                ).fetchall()
            else:
                names = connection.execute(
                    "SELECT name FROM execution_value WHERE "
                    + predicate
                    + " AND name COLLATE UTF16>? ORDER BY name COLLATE UTF16 LIMIT ?",
                    (*args, after, limit),
                ).fetchall()
        return [
            (row[0], self.store.value(item["run"], frame, scope, row[0], version=item["version"]))
            for row in names
        ], item["scopes"][key]

    def _project(self, session, identity, item, name, value):
        projected = {"name": name, **value.projection()}
        resource = None
        added = False
        if projected["visibility"] == "visible" and value.json_size > 4096:
            self.engine._require_content(session)
            descriptor = self.engine.contents.export(
                session.id,
                None,
                dumps(projected.pop("value"), max_bytes=33_554_432),
                permission="run.read",
                purpose="runVariable",
                inspection_id=identity,
            )
            resource = self.engine.contents.resources[descriptor["resourceId"]]
            added = resource.resource_id not in item["resources"]
            if added:
                resource.expires = min(resource.expires, item["expires"])
                resource.deadline = min(resource.deadline, item["deadline"])
                item["resources"].add(resource.resource_id)
            projected["valueResource"] = self.engine.contents.descriptor(resource)
        return projected, resource, added
