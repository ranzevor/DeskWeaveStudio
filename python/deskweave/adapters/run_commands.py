from __future__ import annotations

from deskweave.adapters.receipts import find_receipt, replay_receipt, write_receipt
from deskweave.adapters.workspace import utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import digest


class RunCommand:
    def __init__(self, access, request_id, method, params, *, deadline, clock, authorize):
        self.access = access
        self.request_id = request_id
        self.method = method
        self.run_id = params["runId"]
        self.payload_hash = digest({"method": method, "params": params})
        self.deadline, self.clock, self.authorize = deadline, clock, authorize

    def check(self):
        self.access.require("run.control")
        self.authorize()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise DeskWeaveError("DEADLINE_EXCEEDED", "运行控制请求已超时。", retry="afterQuery")

    def replay(self, connection, checkpoint):
        self.check()
        row = find_receipt(connection, self.access.client_id, self.request_id)
        result = replay_receipt(row, self.method, self.payload_hash, checkpoint)
        if row is not None and (
            row["subject_kind"],
            row["subject_id"],
            row["authorization_ref"],
            row["authorization_version"],
        ) != ("run", self.run_id, self.access.authorization_ref, self.access.authorization_version):
            raise DeskWeaveError("ACCESS_DENIED", "当前授权不能读取此运行回执。")
        return result

    @staticmethod
    def result(state):
        return {
            "accepted": True,
            **{name: state[name] for name in ("runId", "state", "controlVersion")},
        }

    def write(self, connection, result):
        self.check()
        write_receipt(
            connection,
            client_id=self.access.client_id,
            request_id=self.request_id,
            method=self.method,
            subject_kind="run",
            subject_id=self.run_id,
            payload_hash=self.payload_hash,
            result=result,
            created_at=utc_time(),
            authorization_ref=self.access.authorization_ref,
            authorization_version=self.access.authorization_version,
        )
