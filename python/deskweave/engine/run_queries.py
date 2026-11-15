from __future__ import annotations

import unicodedata
import uuid

from deskweave.adapters.execution import ExecutionStore
from deskweave.adapters.run_lifecycle import RunLifecycleStore
from deskweave.adapters.workspace import utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import digest, dumps


def fold(value):
    return unicodedata.normalize("NFC", value).casefold()


class RunQueries:
    def __init__(self, engine):
        self.engine = engine
        self.workspace = engine.workspace
        self.store = ExecutionStore(self.workspace)
        self.lifecycle = RunLifecycleStore(self.workspace)
        self.collections = {}

    def expire(self):
        now = self.engine.monotonic()
        self.collections = {k: v for k, v in self.collections.items() if v["deadline"] > now}

    def collection(self, session, kind, count, *, expires=None, **source):
        self.expire()
        if (
            len(self.collections) >= 128
            or sum(c["owner"] == session.id for c in self.collections.values()) >= 32
        ):
            raise DeskWeaveError("LIMIT_EXCEEDED", "结果集合数量已满，请关闭已有集合。")
        lifetime = 300 if expires is None else min(300, expires - self.engine.clock())
        identity = uuid.uuid4().hex
        self.collections[identity] = {
            "owner": session.id,
            "authorization": session.authorization_version,
            "kind": kind,
            "expires": self.engine.clock() + lifetime,
            "deadline": self.engine.monotonic() + lifetime,
            **source,
        }
        return {
            "collectionId": identity,
            "kind": kind,
            "itemCount": count,
            "expiresAt": utc_time(self.engine.clock() + lifetime),
        }

    def snapshot(self, session, run_id):
        self.store.check_access(run_id, self.engine.run_access(session), "run.read")
        row = self.lifecycle.read(run_id)
        source = {
            "run": run_id,
            "version": row["data_version"],
            "state": row["state"],
            "source_version": f"{row['data_version']}:{row['control_version']}:{row['last_seq']}",
        }
        frames = self.collection(
            session, "runFrames", row["frame_count"], expires=self.engine.clock() + 60, **source
        )
        try:
            effects = self.collection(
                session, "runEffects", 0, expires=self.engine.clock() + 60, **source
            )
        except BaseException:
            self.collections.pop(frames["collectionId"], None)
            raise
        return {
            "runId": run_id,
            "workflowId": row["workflow_id"],
            "revision": row["revision_id"],
            "state": row["state"],
            "controlVersion": row["control_version"],
            "dataVersion": row["data_version"],
            "lastSeq": str(row["last_seq"]),
            "cleanup": row["cleanup"],
            "frameCount": row["frame_count"],
            "effectCount": 0,
            "framesQuery": frames,
            "effectsQuery": effects,
        }

    def release(self, owner):
        self.collections = {k: v for k, v in self.collections.items() if v["owner"] != owner}

    def close(self, owner, identity):
        item = self.collections.get(identity)
        if item is not None and item["owner"] == owner:
            del self.collections[identity]

    def catalog(self, session, params):
        access = self.engine.run_access(session)
        access.require("run.read")
        params = {
            **params,
            "nameContains": fold(params["nameContains"]),
            "states": sorted(params["states"]),
        }
        binding = digest({"method": "workspace.catalog", **params, "cursor": None})

        def factory(connection):
            watermark = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM run_catalog_version"
            ).fetchone()[0]
            return {
                **params,
                "watermark": watermark,
                "workflowIds": None if access.workflow_ids is None else sorted(access.workflow_ids),
            }, {}

        def reader(connection, source, position, limit):
            where = [
                "v.sequence<=?",
                "(v.valid_until IS NULL OR v.valid_until>?)",
                "instr(v.name_key,?)>0",
            ]
            args = [source["watermark"], source["watermark"], source["nameContains"]]
            if source["states"]:
                where.append("v.state IN (SELECT value FROM json_each(?))")
                args.append(dumps(source["states"]).decode())
            if source["workflowIds"] is not None:
                where.append(
                    "NOT EXISTS (SELECT 1 FROM run_workflow_ref ref JOIN revision r "
                    "ON r.id=ref.revision_id WHERE ref.run_id=v.run_id AND "
                    "r.workflow_id NOT IN (SELECT value FROM json_each(?)))"
                )
                args.append(dumps(source["workflowIds"]).decode())
            ascending = source["order"] == "nameAsc"
            column = "updated_at" if source["order"] == "updatedDesc" else "name_key"
            order = "ASC" if ascending else "DESC"
            index = {"updatedDesc": "updated", "nameAsc": "name_asc", "nameDesc": "name_desc"}[
                source["order"]
            ]
            if position is not None:
                operator = ">" if ascending else "<"
                where.append(f"(v.{column}{operator}? OR (v.{column}=? AND v.run_id>?))")
                args.extend((position[0], position[0], position[1]))
            rows = connection.execute(
                f"SELECT v.* FROM run_catalog_version v INDEXED BY run_catalog_{index} WHERE "
                + " AND ".join(where)
                + f" ORDER BY v.{column} {order},v.run_id ASC LIMIT ?",
                (*args, limit),
            )
            for row in rows:
                yield (
                    [row[column], row["run_id"]],
                    {
                        "id": row["run_id"],
                        "name": row["name"],
                        "version": row["revision_id"],
                        "state": row["state"],
                    },
                )

        def project(page):
            page.pop("extra")
            return page

        return self.engine.pages.read(
            session.id,
            session.authorization_version,
            binding,
            params["pageSize"],
            factory=factory,
            reader=reader,
            project=project,
            max_bytes=self.engine.result_limit("workspace.catalog"),
            cursor=params["cursor"],
        )

    def read(self, session, params):
        item = self.collections.get(params["collectionId"])
        if item is None or item["owner"] != session.id or item["kind"] != params["kind"]:
            raise DeskWeaveError("COLLECTION_NOT_FOUND", "结果集合不存在。")
        if item["deadline"] <= self.engine.monotonic() or item["expires"] <= self.engine.clock():
            raise DeskWeaveError("CURSOR_EXPIRED", "结果集合已过期，请重新查询。")
        access = self.engine.run_access(session)
        if "run" in item:
            self.store.check_access(item["run"], access, "run.read")
        else:
            if item.get("plan") is not None:
                access.check_snapshot(item["plan"])
            else:
                access.require("run.execute", "workflow.read")
                for identity in item["workflows"]:
                    access.workflow(identity)
        defaults = {
            "diagnostics": {"text": "", "codes": []},
            "runRequirements": {"text": "", "kinds": []},
            "runFrames": {"states": []},
            "runEffects": {"states": []},
        }
        filters = {**defaults[item["kind"]], **params["filter"]}
        if "text" in filters:
            filters["text"] = fold(filters["text"])
        for key in ("states", "codes", "kinds"):
            if key in filters:
                filters[key] = sorted(filters[key])
        params = {**params, "filter": filters}
        binding = digest({"method": "query.read", **params, "cursor": None})

        def rows(connection, _source, position, limit):
            if item["kind"] == "runEffects":
                return
            if item["kind"] == "runFrames":
                yield from self._frames(connection, item, params, position, limit)
                return
            source = item["items"]
            indices = (
                range(len(source) - 1 if position is None else position - 1, -1, -1)
                if params["order"] == "desc"
                else range(0 if position is None else position + 1, len(source))
            )
            for index in indices:
                self.workspace.check_deadline()
                value = source[index]
                if filters.get("text"):
                    needle = filters["text"]
                    if item["kind"] == "diagnostics":
                        haystack = (value.get("message", ""), value.get("code", ""))
                    else:
                        haystack = (value.get("name", ""),)
                    if not any(needle in fold(part) for part in haystack):
                        continue
                if filters.get("kinds") and value.get("kind") not in filters["kinds"]:
                    continue
                if "satisfied" in filters and value.get("satisfied") != filters["satisfied"]:
                    continue
                if filters.get("codes") and value.get("code") not in filters["codes"]:
                    continue
                if "nodeId" in filters and value.get("nodeId") != filters["nodeId"]:
                    continue
                yield index, {"index": index, "value": value}

        def factory(connection):
            count = None
            if params["includeTotal"]:
                count = sum(1 for _ in rows(connection, None, None, None))
            return {}, {
                "count": count,
                "sourceVersion": str(item.get("source_version", item["version"])),
                "collectionId": params["collectionId"],
            }

        def project(page):
            return {
                "collectionId": params["collectionId"],
                "kind": params["kind"],
                "querySnapshot": {
                    key: page[key] for key in ("snapshotId", "snapshotAt", "expiresAt")
                }
                | {"sourceVersion": page["extra"]["sourceVersion"]},
                "items": page["items"],
                "nextCursor": page["nextCursor"],
                "matchedCount": page["extra"]["count"],
                "countState": "exact" if params["includeTotal"] else "notComputed",
            }

        return self.engine.pages.read(
            session.id,
            session.authorization_version,
            binding,
            params["pageSize"],
            factory=factory,
            reader=rows,
            project=project,
            max_bytes=self.engine.result_limit("query.read"),
            cursor=params["cursor"],
            expires_at=item["expires"],
        )

    @staticmethod
    def _frames(connection, item, params, position, limit):
        filters, args = params["filter"], [item["run"], item["version"], item["version"]]
        state = (
            "CASE WHEN v.state IN ('succeeded','failed','cancelled') THEN v.state "
            + ("WHEN 1 THEN 'paused' " if item["state"] == "paused" else "")
            + "WHEN v.state='ready' THEN 'running' ELSE 'waiting' END"
        )
        where = ["f.run_id=?", "v.version<=?", "(v.valid_until IS NULL OR v.valid_until>?)"]
        for key, column in (("parentId", "f.parent_id"), ("nodeId", "v.node_id")):
            if key in filters:
                where.append(column + " IS ?")
                args.append(filters[key])
        if filters.get("states"):
            where.append(state + " IN (SELECT value FROM json_each(?))")
            args.append(dumps(filters["states"]).decode())
        descending = params["order"] == "desc"
        if position is not None:
            where.append("f.sequence" + ("<?" if descending else ">?"))
            args.append(position)
        query = (
            "SELECT f.sequence,f.id,f.parent_id,f.revision_id,v.node_id,"
            + state
            + " AS state FROM frame f JOIN run_frame_version v "
            "ON v.run_id=f.run_id AND v.frame_id=f.id WHERE "
            + " AND ".join(where)
            + " ORDER BY f.sequence "
            + ("DESC" if descending else "ASC")
        )
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        for row in connection.execute(query, args):
            yield (
                row["sequence"],
                {
                    "index": row["sequence"],
                    "value": {
                        "frameId": row["id"],
                        "parentId": row["parent_id"],
                        "revision": row["revision_id"],
                        "state": row["state"],
                        "nodeId": row["node_id"] or None,
                    },
                },
            )
