from __future__ import annotations

from deskweave.adapters.execution import ExecutionStore
from deskweave.adapters.run_views import record_frame, record_run
from deskweave.adapters.workspace import utc_time
from deskweave.domain.execution import TERMINAL
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps

UNFINISHED = "('queued','preparing','running','pausing','paused','stopping')"


class RunLifecycleStore:
    def __init__(self, workspace):
        self.workspace = workspace

    def read(self, run_id):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT r.*,m.frame_count FROM run r JOIN run_metrics m ON m.run_id=r.id "
                "WHERE r.id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise DeskWeaveError("RESOURCE_NOT_FOUND", "运行不存在。")
        return dict(row)

    @staticmethod
    def response(row):
        return {
            "accepted": True,
            "runId": row["id"],
            "state": row["state"],
            "controlVersion": row["control_version"],
        }

    def transition(
        self,
        run_id,
        state,
        expected,
        *,
        access=None,
        receipt=None,
        system=False,
        error=None,
        on_commit=None,
        checkpoint=lambda: None,
    ):
        if type(expected) is not int or not 0 <= expected <= 9007199254740991:
            raise DeskWeaveError("INVALID_ARGUMENT", "运行控制版本无效。")

        def authorize():
            checkpoint()
            if not system:
                access.require("run.control")
            if receipt is not None:
                receipt.check()

        with self.workspace.budget(None, authorize=authorize):
            if not system:
                ExecutionStore(self.workspace).check_access(run_id, access, "run.control")
            with self.workspace.transaction() as connection:
                result = self._transition(
                    connection, run_id, state, expected, access, receipt, system, error
                )
            if on_commit is not None:
                on_commit(dict(result))
            return result

    def _transition(self, connection, run_id, state, expected, access, receipt, system, error):
        row = self.read(run_id)
        if row["control_version"] != expected:
            raise DeskWeaveError("REVISION_CONFLICT", "运行控制版本已变化。")
        if row["state"] == state:
            result = self.response(row)
            if receipt is not None:
                receipt.write(connection, result)
            return result
        version, data_version = expected + 1, row["data_version"]
        if state in TERMINAL:
            first = connection.execute(
                "SELECT 1 FROM frame WHERE run_id=? AND state NOT IN "
                "('succeeded','failed','cancelled') LIMIT 1",
                (run_id,),
            ).fetchone()
            data_version += first is not None
            after = 0
            while True:
                frames = connection.execute(
                    "SELECT sequence,id,node_id FROM frame WHERE run_id=? AND state NOT IN "
                    "('succeeded','failed','cancelled') AND sequence>? "
                    "ORDER BY sequence LIMIT 128",
                    (run_id, after),
                ).fetchall()
                if not frames:
                    break
                for frame in frames:
                    self.workspace.check_deadline()
                    connection.execute(
                        "UPDATE frame SET state='cancelled' WHERE id=?", (frame["id"],)
                    )
                    record_frame(
                        connection,
                        run_id,
                        frame["id"],
                        data_version,
                        "cancelled",
                        frame["node_id"],
                    )
                    connection.execute(
                        "UPDATE execution_frame SET error=? WHERE frame_id=?",
                        (dumps(error).decode(), frame["id"]),
                    )
                after = frames[-1]["sequence"]
            connection.execute(
                "DELETE FROM execution_unhandled WHERE parent_id IN "
                "(SELECT id FROM frame WHERE run_id=?)",
                (run_id,),
            )
            connection.execute(
                "UPDATE execution_run SET version=version+1,background=0,error=? WHERE run_id=?",
                (dumps(error).decode(), run_id),
            )
        created = utc_time()
        last_seq = row["last_seq"] + 1
        payload = self.workspace._put_object(
            {
                "state": state,
                "controlVersion": version,
                "dataVersion": data_version,
            }
        )
        connection.execute(
            "INSERT INTO run_event VALUES(?,?,'run.state.changed',?,?)",
            (run_id, last_seq, payload, created),
        )
        changed = connection.execute(
            "UPDATE run SET state=?,control_version=?,data_version=?,last_seq=?,"
            "updated_at=? WHERE id=? AND control_version=?",
            (state, version, data_version, last_seq, created, run_id, expected),
        )
        if changed.rowcount != 1:
            raise DeskWeaveError("REVISION_CONFLICT", "运行控制版本已变化。")
        record_run(connection, run_id, state, created)
        result = {
            "accepted": True,
            "runId": run_id,
            "state": state,
            "controlVersion": version,
        }
        if receipt is not None:
            receipt.write(connection, result)
        return result

    def recover(self):
        count, after = 0, 0
        with self.workspace.budget(None), self.workspace.transaction() as connection:
            while True:
                rows = connection.execute(
                    f"SELECT sequence,id,control_version FROM run WHERE state IN {UNFINISHED} "
                    "AND sequence>? ORDER BY sequence LIMIT 64",
                    (after,),
                ).fetchall()
                if not rows:
                    return count
                for row in rows:
                    self.transition(
                        row["id"],
                        "interrupted",
                        row["control_version"],
                        system=True,
                        error={"code": "INTERRUPTED", "message": "执行服务已重新启动。"},
                    )
                    count += 1
                after = rows[-1]["sequence"]
