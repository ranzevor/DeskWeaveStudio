from __future__ import annotations

import secrets
import time
import uuid
from itertools import islice

from deskweave.adapters.workspace import Workspace, utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads

MAX_CURSOR_POSITIONS = 1_000_000


class Pages:
    def __init__(self, workspace: Workspace, clock=time.time):
        self.workspace = workspace
        self.clock = clock

    def _begin(self, connection, owner, version, binding, factory, expires_at):
        now = self.clock()
        expires = min(now + 300, expires_at) if expires_at is not None else now + 300
        if expires <= now:
            raise DeskWeaveError("CURSOR_EXPIRED", "结果集合已过期，请重新查询。")
        connection.execute("DELETE FROM query_snapshot WHERE expires_at<=?", (now,))
        count, owned = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(owner=?),0) FROM query_snapshot",
            (owner,),
        ).fetchone()
        if count >= 32 or owned >= 8:
            raise DeskWeaveError("LIMIT_EXCEEDED", "查询数量已满，请关闭已有查询。")
        source, extra = factory(connection)
        identity = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO query_snapshot VALUES(?,?,?,?,?,?,?,?)",
            (
                identity,
                owner,
                version,
                binding,
                now,
                expires,
                dumps(source).decode(),
                dumps(extra).decode(),
            ),
        )
        return connection.execute("SELECT * FROM query_snapshot WHERE id=?", (identity,)).fetchone()

    def read(
        self,
        owner: str,
        version: int,
        binding: str,
        page_size: int,
        *,
        factory,
        reader,
        project,
        max_bytes: int = 262144,
        cursor: str | None = None,
        expires_at: float | None = None,
    ) -> dict:
        page_size = int(page_size)
        with self.workspace.transaction() as connection:
            position = None
            if cursor is None:
                snapshot = self._begin(connection, owner, version, binding, factory, expires_at)
            else:
                marker = connection.execute(
                    "SELECT * FROM query_cursor WHERE token=?",
                    (cursor,),
                ).fetchone()
                if marker is None:
                    raise DeskWeaveError("CURSOR_EXPIRED", "查询已失效，请重新查询。")
                snapshot = connection.execute(
                    "SELECT * FROM query_snapshot WHERE id=?",
                    (marker["snapshot_id"],),
                ).fetchone()
                position = loads(marker["position"])
            if (
                snapshot is None
                or snapshot["owner"] != owner
                or snapshot["authorization_version"] != version
                or snapshot["binding"] != binding
                or snapshot["expires_at"] <= self.clock()
            ):
                raise DeskWeaveError("CURSOR_EXPIRED", "查询已失效，请重新查询。")
            source, extra = loads(snapshot["source"]), loads(snapshot["extra"])
            rows = list(islice(reader(connection, source, position, page_size + 1), page_size + 1))
            metadata = {
                "snapshotId": snapshot["id"],
                "snapshotAt": utc_time(snapshot["created_at"]),
                "expiresAt": utc_time(snapshot["expires_at"]),
                "extra": extra,
            }

            def render(items, next_cursor):
                return project({**metadata, "items": items, "nextCursor": next_cursor})

            token = secrets.token_urlsafe(24)
            accepted, used_bytes = [], 0
            for index, (_key, value) in enumerate(rows[:page_size]):
                self.workspace.check_deadline()
                more = index + 1 < len(rows)
                try:
                    size = len(
                        dumps(value, max_bytes=max_bytes, checkpoint=self.workspace.check_deadline)
                    )
                except DeskWeaveError as error:
                    if error.code != "LIMIT_EXCEEDED":
                        raise
                    if accepted:
                        break
                    raise DeskWeaveError(
                        "QUERY_ITEM_TOO_LARGE",
                        "查询成员超过页面大小限制。",
                        details={"fieldPath": "/items/0"},
                    ) from error
                base = len(dumps(render([], token if more else None), max_bytes=max_bytes))
                if base + used_bytes + size + len(accepted) > max_bytes:
                    if not accepted:
                        raise DeskWeaveError(
                            "QUERY_ITEM_TOO_LARGE",
                            "查询成员超过页面大小限制。",
                            details={"fieldPath": "/items/0"},
                        )
                    break
                used_bytes += size
                accepted.append(value)
            next_cursor = None
            if len(accepted) < len(rows):
                encoded = dumps(rows[len(accepted) - 1][0]).decode()
                previous = connection.execute(
                    "SELECT token FROM query_cursor WHERE snapshot_id=? AND position=?",
                    (snapshot["id"], encoded),
                ).fetchone()
                if previous:
                    next_cursor = previous[0]
                else:
                    if (
                        connection.execute("SELECT COUNT(*) FROM query_cursor").fetchone()[0]
                        >= MAX_CURSOR_POSITIONS
                    ):
                        raise DeskWeaveError("LIMIT_EXCEEDED", "游标位置数量已满，请关闭已有查询。")
                    next_cursor = token
                    connection.execute(
                        "INSERT INTO query_cursor VALUES(?,?,?)", (token, snapshot["id"], encoded)
                    )
            result = render(accepted, next_cursor)
            dumps(result, max_bytes=max_bytes, checkpoint=self.workspace.check_deadline)
            return result

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
                "DELETE FROM query_snapshot WHERE owner=? AND "
                "json_extract(extra,'$.collectionId')=?",
                (owner, collection_id),
            )
