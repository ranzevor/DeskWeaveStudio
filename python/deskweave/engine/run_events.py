from __future__ import annotations

import uuid

from deskweave.adapters.execution import ExecutionStore
from deskweave.adapters.run_lifecycle import RunLifecycleStore
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps


class RunEvents:
    def __init__(self, engine):
        self.engine, self.workspace = engine, engine.workspace
        self.epoch = uuid.uuid4().hex
        self.subscriptions = {}

    def subscribe(self, session, params, _request_id):
        if (
            len(self.subscriptions) >= 64
            or sum(s["owner"] == session.id for s in self.subscriptions.values()) >= 16
        ):
            raise DeskWeaveError("LIMIT_EXCEEDED", "运行订阅数量已满。")
        snapshot = self.engine.run_queries.snapshot(session, params["runId"])
        identity = uuid.uuid4().hex
        self.subscriptions[identity] = {
            "owner": session.id,
            "run": params["runId"],
            "after": int(snapshot["lastSeq"]),
            "active": False,
        }
        return {"subscriptionId": identity, "instanceEpoch": self.epoch, "snapshot": snapshot}

    def activate(self, owner, identity):
        item = self.subscriptions.get(identity)
        if item is not None and item["owner"] == owner:
            item["active"] = True

    def unsubscribe(self, session, params, _request_id):
        item = self.subscriptions.get(params["subscriptionId"])
        if item is not None and item["owner"] == session.id:
            del self.subscriptions[params["subscriptionId"]]
        return {"closed": True}

    def release(self, owner):
        self.subscriptions = {k: v for k, v in self.subscriptions.items() if v["owner"] != owner}

    def read(self, session, params, _request_id):
        access = self.engine.run_access(session)
        ExecutionStore(self.workspace).check_access(params["runId"], access, "run.read")
        row = RunLifecycleStore(self.workspace).read(params["runId"])
        if params["instanceEpoch"] != self.epoch or int(params["afterSeq"]) > row["last_seq"]:
            return {"events": [], "hasMore": False, "resyncRequired": True}
        events, size = [], len(dumps({"events": [], "hasMore": False, "resyncRequired": False}))
        after = int(params["afterSeq"])
        with self.workspace.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM run_event WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (params["runId"], after, params["limit"]),
            )
            for event in rows:
                self.workspace.check_deadline()
                message = {
                    "protocolVersion": "1.0",
                    "kind": "event",
                    "streamId": params["runId"],
                    "instanceEpoch": self.epoch,
                    "seq": str(event["seq"]),
                    "event": event["kind"],
                    "occurredAt": event["occurred_at"],
                    "payload": self.workspace._read_object(event["payload_hash"]),
                }
                length = len(dumps(message))
                if size + length > self.engine.result_limit("event.read"):
                    if not events:
                        raise DeskWeaveError("QUERY_ITEM_TOO_LARGE", "事件超过连接帧大小限制。")
                    break
                size += length + 1
                events.append(message)
                after = event["seq"]
        return {"events": events, "hasMore": after < row["last_seq"], "resyncRequired": False}

    def pending(self, session):
        for identity in tuple(self.subscriptions):
            item = self.subscriptions.get(identity)
            if item is None or item["owner"] != session.id or not item["active"]:
                continue
            self.subscriptions[identity] = self.subscriptions.pop(identity)
            try:
                result = self.read(
                    session,
                    {
                        "runId": item["run"],
                        "instanceEpoch": self.epoch,
                        "afterSeq": str(item["after"]),
                        "limit": 32,
                    },
                    None,
                )
            except DeskWeaveError as error:
                if error.code == "ACCESS_DENIED":
                    session.revalidate()
                if error.code in {"ACCESS_DENIED", "RESOURCE_NOT_FOUND", "QUERY_ITEM_TOO_LARGE"}:
                    self.subscriptions.pop(identity, None)
                    continue
                raise
            if result["events"]:
                return identity, result["events"]
        return None

    def acknowledge(self, owner, identity, seq):
        item = self.subscriptions.get(identity)
        if item is not None and item["owner"] == owner:
            item["after"] = max(item["after"], int(seq))
