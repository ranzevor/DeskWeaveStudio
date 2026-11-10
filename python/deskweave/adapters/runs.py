from __future__ import annotations

import io
import uuid

from deskweave.adapters.blobs import Blobs
from deskweave.adapters.receipts import find_receipt, replay_receipt, write_receipt
from deskweave.adapters.workspace import utc_time
from deskweave.domain.runs import RunPlan, validate_snapshot
from deskweave.errors import DeskWeaveError

RUN_STATES = (
    "'queued','preparing','running','pausing','paused','stopping',"
    "'succeeded','failed','cancelled','interrupted'"
)


def initialize_run_storage(connection):
    for statement in (
        "CREATE TABLE IF NOT EXISTS run (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT UNIQUE NOT NULL, workflow_id TEXT NOT NULL, revision_id TEXT NOT NULL "
        "REFERENCES revision(id), snapshot_hash TEXT NOT NULL, owner_client_id TEXT NOT NULL, "
        "authorization_ref TEXT NOT NULL, authorization_version INTEGER NOT NULL, "
        f"state TEXT NOT NULL CHECK(state IN ({RUN_STATES})), "
        "control_version INTEGER NOT NULL, data_version INTEGER NOT NULL, "
        "last_seq INTEGER NOT NULL, "
        "root_frame_id TEXT NOT NULL, detached INTEGER NOT NULL CHECK(detached IN (0,1)), "
        "cleanup TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "FOREIGN KEY(id,root_frame_id) REFERENCES frame(run_id,id) DEFERRABLE INITIALLY DEFERRED)",
        "CREATE INDEX IF NOT EXISTS run_owner_state ON run(owner_client_id,state,sequence)",
        "CREATE TABLE IF NOT EXISTS frame (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "id TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL REFERENCES run(id), parent_id TEXT, "
        "workflow_id TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES revision(id), "
        "state TEXT NOT NULL, node_id TEXT NOT NULL, state_hash TEXT NOT NULL, "
        "UNIQUE(run_id,id), FOREIGN KEY(run_id,parent_id) REFERENCES frame(run_id,id))",
        "CREATE INDEX IF NOT EXISTS frame_run_sequence ON frame(run_id,sequence)",
        "CREATE TABLE IF NOT EXISTS run_event (run_id TEXT NOT NULL REFERENCES run(id), "
        "seq INTEGER NOT NULL, kind TEXT NOT NULL, payload_hash TEXT NOT NULL, "
        "occurred_at TEXT NOT NULL, PRIMARY KEY(run_id,seq))",
        "CREATE TABLE IF NOT EXISTS run_workflow_ref (run_id TEXT NOT NULL REFERENCES run(id), "
        "revision_id TEXT NOT NULL REFERENCES revision(id), PRIMARY KEY(run_id,revision_id))",
        "CREATE INDEX IF NOT EXISTS run_workflow_target ON run_workflow_ref(revision_id)",
        "CREATE TABLE IF NOT EXISTS run_asset_ref (run_id TEXT NOT NULL REFERENCES run(id), "
        "asset_version TEXT NOT NULL REFERENCES asset_version(id), "
        "PRIMARY KEY(run_id,asset_version))",
        "CREATE INDEX IF NOT EXISTS run_asset_target ON run_asset_ref(asset_version)",
    ):
        connection.execute(statement)


class RunStore:
    def __init__(self, workspace):
        self.workspace = workspace
        self.blobs = Blobs(workspace)

    def _verify_content(self, snapshot):
        checked = set()
        objects = [
            (item["definitionHash"], item["definitionLength"]) for item in snapshot["workflows"]
        ]
        objects += [
            (item["dependenciesHash"], item["dependenciesLength"]) for item in snapshot["workflows"]
        ]
        objects += [(item["sha256"], item["length"]) for item in snapshot["assets"]]
        for identity, length in objects:
            self.workspace.check_deadline()
            if (identity, length) not in checked:
                with self.blobs.open(identity, int(length)):
                    pass
                checked.add((identity, length))

    def _verify_versions(self, connection, snapshot):
        for item in snapshot["workflows"]:
            self.workspace.check_deadline()
            row = connection.execute(
                "SELECT workflow_id,definition_hash,dependencies_hash FROM revision WHERE id=?",
                (item["revision"],),
            ).fetchone()
            if row is None or tuple(row) != (
                item["workflowId"],
                item["definitionHash"],
                item["dependenciesHash"],
            ):
                raise DeskWeaveError("DEPENDENCY_MISMATCH", "运行计划的流程版本已不匹配。")
        for item in snapshot["assets"]:
            self.workspace.check_deadline()
            row = connection.execute(
                "SELECT v.asset_id,a.kind,v.content_hash,v.byte_length,v.media_type "
                "FROM asset_version v JOIN asset a ON a.id=v.asset_id WHERE v.id=?",
                (item["assetVersion"],),
            ).fetchone()
            if row is None or tuple(row) != (
                item["assetId"],
                item["kind"],
                item["sha256"],
                item["length"],
                item["mediaType"],
            ):
                raise DeskWeaveError("DEPENDENCY_MISMATCH", "运行计划的资产版本已不匹配。")

    def create(self, plan: RunPlan, request_id: str, *, authorize):
        with self.workspace.budget(None):
            snapshot = plan.value(checkpoint=self.workspace.check_deadline)
            if snapshot["workspaceId"] != self.workspace.id:
                raise DeskWeaveError("WORKSPACE_MISMATCH", "运行计划属于另一工作空间。")
            authorization = snapshot["authorization"]
            authorize(snapshot)
            with self.workspace.transaction() as connection:
                row = find_receipt(connection, authorization["clientId"], request_id)
                replay = replay_receipt(
                    row, "run.start", plan.request_hash, self.workspace.check_deadline
                )
                if replay is not None:
                    if (row["authorization_ref"], row["authorization_version"]) != (
                        authorization["ref"],
                        authorization["version"],
                    ):
                        raise DeskWeaveError("ACCESS_DENIED", "当前授权不能读取此运行回执。")
                    return replay
            self._verify_content(snapshot)
            snapshot_hash, _ = self.blobs.put(io.BytesIO(plan.content), maximum=33_554_432)
            state_hash = self.workspace._put_object(snapshot["initialState"])
            run_id, frame_id, created = uuid.uuid4().hex, uuid.uuid4().hex, utc_time()
            event_hash = self.workspace._put_object({"state": "queued", "controlVersion": 0})
            result = {"accepted": True, "runId": run_id, "state": "queued"}
            root = snapshot["root"]
            with self.workspace.transaction() as connection:
                authorize(snapshot)
                self._verify_versions(connection, snapshot)
                connection.execute(
                    "INSERT INTO run(id,workflow_id,revision_id,snapshot_hash,owner_client_id,"
                    "authorization_ref,authorization_version,state,control_version,data_version,"
                    "last_seq,root_frame_id,detached,cleanup,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'queued',0,0,1,?,?,'notRequired',?,?)",
                    (
                        run_id,
                        root["workflowId"],
                        root["revision"],
                        snapshot_hash,
                        authorization["clientId"],
                        authorization["ref"],
                        authorization["version"],
                        frame_id,
                        int(snapshot["detached"]),
                        created,
                        created,
                    ),
                )
                connection.execute(
                    "INSERT INTO frame(id,run_id,parent_id,workflow_id,revision_id,state,node_id,"
                    "state_hash) VALUES(?,?,NULL,?,?,'queued',?,?)",
                    (
                        frame_id,
                        run_id,
                        root["workflowId"],
                        root["revision"],
                        root["entry"],
                        state_hash,
                    ),
                )
                for item in snapshot["workflows"]:
                    self.workspace.check_deadline()
                    connection.execute(
                        "INSERT INTO run_workflow_ref VALUES(?,?)", (run_id, item["revision"])
                    )
                for item in snapshot["assets"]:
                    self.workspace.check_deadline()
                    connection.execute(
                        "INSERT INTO run_asset_ref VALUES(?,?)", (run_id, item["assetVersion"])
                    )
                connection.execute(
                    "INSERT INTO run_event VALUES(?,1,'run.state.changed',?,?)",
                    (run_id, event_hash, created),
                )
                write_receipt(
                    connection,
                    client_id=authorization["clientId"],
                    request_id=request_id,
                    method="run.start",
                    subject_kind="run",
                    subject_id=run_id,
                    payload_hash=plan.request_hash,
                    result=result,
                    created_at=created,
                    authorization_ref=authorization["ref"],
                    authorization_version=authorization["version"],
                )
                authorize(snapshot)
            return result

    def read(self, run_id: str):
        with self.workspace.budget(None):
            with self.workspace.transaction() as connection:
                row = connection.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise DeskWeaveError("RESOURCE_NOT_FOUND", "运行不存在。")
            snapshot = self.workspace._read_object(row["snapshot_hash"])
            try:
                validate_snapshot(snapshot, checkpoint=self.workspace.check_deadline)
                if (
                    snapshot["workspaceId"] != self.workspace.id
                    or snapshot["root"]["workflowId"] != row["workflow_id"]
                    or snapshot["root"]["revision"] != row["revision_id"]
                    or snapshot["authorization"]
                    != {
                        "clientId": row["owner_client_id"],
                        "ref": row["authorization_ref"],
                        "version": row["authorization_version"],
                    }
                ):
                    raise DeskWeaveError("INVALID_ARGUMENT", "运行快照与运行记录不一致。")
            except DeskWeaveError as error:
                if error.code != "INVALID_ARGUMENT":
                    raise
                raise DeskWeaveError("STORAGE_CORRUPT", "运行快照内容损坏。") from error
            return {
                "runId": row["id"],
                "state": row["state"],
                "snapshot": snapshot,
                "snapshotHash": row["snapshot_hash"],
                "rootFrameId": row["root_frame_id"],
                "controlVersion": row["control_version"],
                "dataVersion": row["data_version"],
                "lastSeq": str(row["last_seq"]),
            }
