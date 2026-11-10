from __future__ import annotations

from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads


def find_receipt(connection, client_id, request_id):
    return connection.execute(
        "SELECT * FROM command_receipt WHERE client_id=? AND request_id=?",
        (client_id, request_id),
    ).fetchone()


def replay_receipt(row, method, payload_hash, checkpoint):
    if row is None:
        return None
    if row["method"] != method or row["payload_hash"] != payload_hash:
        raise DeskWeaveError("REQUEST_CONFLICT", "请求标识已用于另一项修改。")
    return loads(row["result"], checkpoint=checkpoint)


def write_receipt(
    connection,
    *,
    client_id,
    request_id,
    method,
    subject_kind,
    subject_id,
    payload_hash,
    result,
    created_at,
    authorization_ref=None,
    authorization_version=None,
):
    connection.execute(
        "INSERT INTO command_receipt(client_id,request_id,method,subject_kind,subject_id,"
        "payload_hash,result,created_at,authorization_ref,authorization_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            client_id,
            request_id,
            method,
            subject_kind,
            subject_id,
            payload_hash,
            dumps(result).decode(),
            created_at,
            authorization_ref,
            authorization_version,
        ),
    )
