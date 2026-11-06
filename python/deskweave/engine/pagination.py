from __future__ import annotations

import secrets
import time
import uuid

from deskweave.adapters.workspace import Workspace, utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads


class Pages:
    def __init__(self, workspace: Workspace, clock=time.time):
        self.workspace = workspace
        self.clock = clock

    def begin(
        self,
        owner: str,
        version: int,
        binding: str,
        rows,
        extra: dict,
        *,
        expires_at: float | None = None,
    ) -> str:
        now = self.clock()
        identity = uuid.uuid4().hex
        expires_at = min(now + 300, expires_at) if expires_at is not None else now + 300
        with self.workspace.transaction() as connection:
            connection.execute("DELETE FROM query_snapshot WHERE expires_at<=?", (now,))
            count, owned = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(owner=?),0) FROM query_snapshot", (owner,)
            ).fetchone()
            if count >= 32 or owned >= 8:
                raise DeskWeaveError("LIMIT_EXCEEDED", "查询数量已满，请关闭已有查询。")
            connection.execute(
                "INSERT INTO query_snapshot VALUES(?,?,?,?,?,?,?)",
                (identity, owner, version, binding, now, expires_at, dumps(extra).decode()),
            )
            if isinstance(rows, tuple):
                sql, arguments = rows
                connection.execute(
                    "INSERT INTO query_member(snapshot_id,ordinal,value) "
                    "SELECT ?,ROW_NUMBER() OVER (),value FROM (" + sql + ")",
                    (identity, *arguments),
                )
            else:
                connection.executemany(
                    "INSERT INTO query_member VALUES(?,?,?)",
                    ((identity, index, dumps(row).decode()) for index, row in enumerate(rows, 1)),
                )
            count = connection.execute("SELECT COUNT(*) FROM query_member").fetchone()[0]
            if count > 1_000_000:
                raise DeskWeaveError("LIMIT_EXCEEDED", "查询成员数量超过工作空间限制。")
        return identity

    def read(
        self,
        owner: str,
        version: int,
        binding: str,
        page_size: int,
        *,
        cursor: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict:
        with self.workspace.transaction() as connection:
            offset = 0
            if cursor is not None:
                position = connection.execute(
                    "SELECT * FROM query_cursor WHERE token=?", (cursor,)
                ).fetchone()
                if position is None:
                    raise DeskWeaveError("CURSOR_EXPIRED", "查询已失效，请重新查询。")
                snapshot_id, offset = position["snapshot_id"], position["ordinal"]
            snapshot = connection.execute(
                "SELECT * FROM query_snapshot WHERE id=?", (snapshot_id,)
            ).fetchone()
            if (
                snapshot is None
                or snapshot["owner"] != owner
                or snapshot["authorization_version"] != version
                or snapshot["binding"] != binding
                or snapshot["expires_at"] <= self.clock()
            ):
                raise DeskWeaveError("CURSOR_EXPIRED", "查询已失效，请重新查询。")
            rows = connection.execute(
                "SELECT ordinal,value FROM query_member WHERE snapshot_id=? AND ordinal>? "
                "ORDER BY ordinal LIMIT ?",
                (snapshot_id, offset, page_size + 1),
            ).fetchall()
            next_cursor = None
            if len(rows) > page_size:
                offset = rows[page_size - 1]["ordinal"]
                position = connection.execute(
                    "SELECT token FROM query_cursor WHERE snapshot_id=? AND ordinal=?",
                    (snapshot_id, offset),
                ).fetchone()
                next_cursor = position[0] if position else secrets.token_urlsafe(24)
                if position is None:
                    connection.execute(
                        "INSERT INTO query_cursor VALUES(?,?,?)", (next_cursor, snapshot_id, offset)
                    )
            return {
                "snapshotId": snapshot_id,
                "snapshotAt": utc_time(snapshot["created_at"]),
                "expiresAt": utc_time(snapshot["expires_at"]),
                "items": [loads(row["value"]) for row in rows[:page_size]],
                "nextCursor": next_cursor,
                "extra": loads(snapshot["extra"]),
            }

    def close(self, owner: str, snapshot_id: str) -> dict:
        with self.workspace.transaction() as connection:
            connection.execute(
                "DELETE FROM query_snapshot WHERE id=? AND owner=?", (snapshot_id, owner)
            )
        return {"closed": True}

    def release(self, owner: str) -> None:
        with self.workspace.transaction() as connection:
            connection.execute("DELETE FROM query_snapshot WHERE owner=?", (owner,))

    def active_collections(self) -> set[str]:
        with self.workspace.transaction() as connection:
            connection.execute("DELETE FROM query_snapshot WHERE expires_at<=?", (self.clock(),))
            return {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT json_extract(extra,'$.collectionId') FROM query_snapshot "
                    "WHERE json_extract(extra,'$.collectionId') IS NOT NULL"
                )
            }

    def release_collection(self, owner: str, collection_id: str) -> None:
        with self.workspace.transaction() as connection:
            connection.execute(
                "DELETE FROM query_snapshot WHERE owner=? "
                "AND json_extract(extra,'$.collectionId')=?",
                (owner, collection_id),
            )
