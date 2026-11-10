from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from deskweave.errors import DeskWeaveError
from deskweave.json import digest, dumps, loads


def utc_time(timestamp: float | None = None) -> str:
    value = datetime.now(UTC) if timestamp is None else datetime.fromtimestamp(timestamp, UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class Workspace:
    def __init__(
        self,
        path: Path,
        *,
        create: bool = False,
        name: str = "工作空间",
        _identity: str | None = None,
    ):
        if _identity is not None and (type(_identity) is not str or not 1 <= len(_identity) <= 128):
            raise ValueError("工作空间身份无效。")
        self.path = path.resolve()
        self._mutex = threading.RLock()
        self._closed = False
        self._deadline: float | None = None
        self._clock = time.monotonic
        self._authorize: Callable[[], None] | None = None
        self._progress = None
        self._connection: sqlite3.Connection | None = None
        self._lock_file = None
        if create:
            self.path.mkdir(parents=True, exist_ok=True)
        if not self.path.is_dir():
            raise DeskWeaveError("WORKSPACE_NOT_FOUND", "工作空间目录不存在。")
        descriptor = self.path / "workspace.json"
        if not descriptor.exists() and not create:
            raise DeskWeaveError("WORKSPACE_NOT_FOUND", "目录中没有工作空间。")
        try:
            lock_path = self.path / ".engine.lock"
            if lock_path.is_symlink() or descriptor.is_symlink():
                raise DeskWeaveError("RESOURCE_DENIED", "工作空间元数据不能使用符号链接。")
            self._lock_file = lock_path.open("a+b")
            if lock_path.stat().st_size == 0:
                self._lock_file.write(b"0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise DeskWeaveError(
                    "WORKSPACE_BUSY", "工作空间已由另一个 Engine 打开。"
                ) from error
            if not descriptor.exists():
                if any(
                    item.name not in {".engine.lock", ".engine-start.lock"}
                    for item in self.path.iterdir()
                ):
                    raise DeskWeaveError("INVALID_ARGUMENT", "新工作空间需要空目录。")
                if not 1 <= len(name.strip()) <= 120:
                    raise DeskWeaveError("INVALID_ARGUMENT", "工作空间名称长度应为 1～120。")
                self._atomic_write(
                    descriptor,
                    dumps(
                        {
                            "formatVersion": "1.0",
                            "id": _identity or uuid.uuid4().hex,
                            "name": name.strip(),
                            "createdAt": utc_time(),
                        }
                    ),
                )
            if descriptor.stat().st_size > 1048576:
                raise DeskWeaveError("STORAGE_CORRUPT", "工作空间元数据超过大小限制。")
            metadata = loads(descriptor.read_bytes())
            if type(metadata) is not dict or metadata.get("formatVersion") != "1.0":
                raise DeskWeaveError("SCHEMA_UNSUPPORTED", "工作空间格式不受支持。")
            if type(metadata.get("id")) is not str or not 1 <= len(metadata["id"]) <= 128:
                raise DeskWeaveError("INVALID_ARGUMENT", "工作空间身份无效。")
            self.id = metadata["id"]
            if _identity is not None and self.id != _identity:
                raise DeskWeaveError("RESOURCE_DENIED", "工作空间身份已改变。")
            for directory in ("data", "objects/sha256", "staging", "recovery", "exports"):
                location = self.path / directory
                if not location.resolve().is_relative_to(self.path):
                    raise DeskWeaveError("RESOURCE_DENIED", "工作空间目录指向了外部位置。")
                location.mkdir(parents=True, exist_ok=True)
            if (self.path / "data/workspace.db").is_symlink():
                raise DeskWeaveError("RESOURCE_DENIED", "工作空间数据库不能使用符号链接。")
            connection = sqlite3.connect(
                self.path / "data/workspace.db",
                timeout=2,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1, 2, 3):
                raise DeskWeaveError("SCHEMA_UNSUPPORTED", "工作空间数据库版本不受支持。")
            connection.execute("PRAGMA synchronous=FULL")
            self._initialize_storage()
        except BaseException:
            self.close()
            raise

    def _initialize_storage(self) -> None:
        with self.transaction() as connection:
            connection.create_function(
                "fold",
                1,
                lambda value: unicodedata.normalize("NFC", value).casefold(),
                deterministic=True,
            )
            statements = [
                "CREATE TABLE IF NOT EXISTS workflow (id TEXT PRIMARY KEY, name"
                " TEXT NOT NULL, head_revision TEXT NOT NULL, updated_at TEXT "
                "NOT NULL, sequence INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS revision (sequence INTEGER PRIMARY "
                "KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, workflow_id TEXT "
                "NOT NULL, parent_id TEXT, name TEXT NOT NULL, definition_hash "
                "TEXT NOT NULL, dependencies_hash TEXT NOT NULL, created_at "
                "TEXT NOT NULL, name_key TEXT NOT NULL DEFAULT '', valid_until "
                "INTEGER)",
                "CREATE INDEX IF NOT EXISTS revision_workflow ON revision(workflow_id, sequence)",
                "CREATE TABLE IF NOT EXISTS command_receipt (client_id TEXT NOT"
                " NULL, request_id TEXT NOT NULL, method TEXT NOT NULL, "
                "workflow_id TEXT NOT NULL, payload_hash TEXT NOT NULL, result "
                "TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY "
                "KEY(client_id, request_id))",
            ]
            for statement in statements:
                connection.execute(statement)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(revision)")}
            if "name_key" not in columns:
                connection.execute(
                    "ALTER TABLE revision ADD COLUMN name_key TEXT NOT NULL DEFAULT ''"
                )
                connection.execute("UPDATE revision SET name_key=fold(name)")
            if "valid_until" not in columns:
                connection.execute("ALTER TABLE revision ADD COLUMN valid_until INTEGER")
                connection.execute(
                    "WITH bounds AS MATERIALIZED (SELECT sequence, LEAD(sequence) OVER "
                    "(PARTITION BY workflow_id ORDER BY sequence) AS end_sequence FROM revision) "
                    "UPDATE revision SET valid_until=(SELECT end_sequence FROM "
                    "bounds WHERE bounds.sequence=revision.sequence)"
                )
            for statement in (
                "CREATE INDEX IF NOT EXISTS revision_catalog_name_asc ON "
                "revision(name_key ASC, workflow_id ASC, sequence DESC)",
                "CREATE INDEX IF NOT EXISTS revision_catalog_name_desc ON "
                "revision(name_key DESC, workflow_id ASC, sequence DESC)",
                "DROP TABLE IF EXISTS query_cursor",
                "DROP TABLE IF EXISTS query_member",
                "DROP TABLE IF EXISTS query_snapshot",
                "CREATE TABLE query_snapshot (id TEXT PRIMARY KEY, owner TEXT "
                "NOT NULL, authorization_version INTEGER NOT NULL, binding TEXT"
                " NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,"
                " source TEXT NOT NULL, extra TEXT NOT NULL)",
                "CREATE TABLE query_cursor (token TEXT PRIMARY KEY, snapshot_id"
                " TEXT NOT NULL REFERENCES query_snapshot(id) ON DELETE "
                "CASCADE, position TEXT NOT NULL, UNIQUE(snapshot_id, "
                "position))",
                "CREATE TABLE IF NOT EXISTS asset (id TEXT PRIMARY KEY, kind TEXT NOT NULL "
                "CHECK(kind IN ('image','trajectory')), name TEXT NOT NULL, "
                "current_version TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS asset_name_claim (namespace TEXT NOT NULL, "
                "name_nfc TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES asset(id), "
                "PRIMARY KEY(namespace,name_nfc), UNIQUE(asset_id))",
                "CREATE TABLE IF NOT EXISTS asset_version (sequence INTEGER PRIMARY KEY "
                "AUTOINCREMENT, id TEXT UNIQUE NOT NULL, asset_id TEXT NOT NULL REFERENCES "
                "asset(id), content_hash TEXT NOT NULL, byte_length INTEGER NOT NULL, "
                "media_type TEXT NOT NULL, metadata TEXT NOT NULL, name TEXT NOT NULL, "
                "name_key TEXT NOT NULL, created_at TEXT NOT NULL, valid_until INTEGER)",
                "CREATE INDEX IF NOT EXISTS asset_version_asset ON "
                "asset_version(asset_id,sequence)",
                "CREATE INDEX IF NOT EXISTS asset_catalog_name_asc ON "
                "asset_version(name_key ASC,asset_id ASC,sequence DESC)",
                "CREATE INDEX IF NOT EXISTS asset_catalog_name_desc ON "
                "asset_version(name_key DESC,asset_id ASC,sequence DESC)",
                "CREATE TABLE IF NOT EXISTS revision_asset_ref (revision_id TEXT NOT NULL "
                "REFERENCES revision(id), asset_version TEXT NOT NULL "
                "REFERENCES asset_version(id), "
                "PRIMARY KEY(revision_id,asset_version))",
                "CREATE INDEX IF NOT EXISTS asset_ref_target ON revision_asset_ref(asset_version)",
                "CREATE TABLE IF NOT EXISTS revision_workflow_ref (revision_id TEXT NOT NULL "
                "REFERENCES revision(id), target_revision TEXT NOT NULL REFERENCES revision(id), "
                "PRIMARY KEY(revision_id,target_revision))",
                "CREATE INDEX IF NOT EXISTS workflow_ref_target ON "
                "revision_workflow_ref(target_revision)",
                "CREATE TABLE IF NOT EXISTS blob_chunk (content_hash TEXT NOT NULL, "
                "position INTEGER NOT NULL, sha256 TEXT NOT NULL, "
                "PRIMARY KEY(content_hash,position))",
                "PRAGMA user_version=3",
            ):
                connection.execute(statement)

    @staticmethod
    def catalog_rows(connection, source, position, limit):
        asset = source.get("kind") == "asset"
        table = "asset_version" if asset else "revision"
        identity = "asset_id" if asset else "workflow_id"
        index_prefix = "asset_catalog" if asset else "revision_catalog"
        where = ["sequence<=?", "(valid_until IS NULL OR valid_until>?)", "instr(name_key,?)>0"]
        arguments = [source["watermark"], source["watermark"], source["nameContains"]]
        if not asset and source["workflowIds"] is not None:
            where.append("workflow_id IN (SELECT value FROM json_each(?))")
            arguments.append(dumps(source["workflowIds"]).decode())
        index = ""
        if source["order"] == "updatedDesc":
            order = "sequence DESC"
            if position is not None:
                where.append("sequence<?")
                arguments.append(position)
        elif source["order"] == "nameAsc":
            index = f" INDEXED BY {index_prefix}_name_asc"
            order = f"name_key ASC, {identity} ASC"
            if position is not None:
                where.append(f"(name_key,{identity})>(?,?)")
                arguments.extend(position)
        else:
            index = f" INDEXED BY {index_prefix}_name_desc"
            order = f"name_key DESC, {identity} ASC"
            if position is not None:
                where.append(f"name_key<=? AND (name_key<? OR {identity}>?)")
                arguments.extend((position[0], position[0], position[1]))
        rows = connection.execute(
            f"SELECT sequence,{identity},name,id,name_key FROM {table}"
            + index
            + " WHERE "
            + " AND ".join(where)
            + " ORDER BY "
            + order
            + " LIMIT ?",
            (*arguments, limit),
        )
        for row in rows:
            key = (
                row["sequence"]
                if source["order"] == "updatedDesc"
                else [row["name_key"], row[identity]]
            )
            yield key, {"id": row[identity], "name": row["name"], "version": row["id"]}

    @staticmethod
    def revision_rows(connection, source, position, limit):
        rows = connection.execute(
            "SELECT * FROM revision WHERE workflow_id=? AND sequence<=? AND sequence<? "
            "ORDER BY sequence DESC LIMIT ?",
            (
                source["workflowId"],
                source["watermark"],
                source["watermark"] + 1 if position is None else position,
                limit,
            ),
        )
        for row in rows:
            yield (
                row["sequence"],
                {
                    "revision": row["id"],
                    "parentRevision": row["parent_id"],
                    "name": row["name"],
                    "createdAt": row["created_at"],
                    "definitionHash": row["definition_hash"],
                },
            )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        with NamedTemporaryFile(dir=path.parent, prefix=".writing-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def closed(self) -> bool:
        return self._closed

    def check_deadline(self) -> None:
        if self._authorize is not None:
            self._authorize()
        if self._deadline is not None and self._clock() >= self._deadline:
            raise DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。", retry="afterQuery")

    @contextmanager
    def budget(self, deadline: float | None, clock=time.monotonic, *, authorize=None):
        with self._mutex:
            if self._closed or self._connection is None:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            previous = self._deadline, self._clock, self._authorize, self._progress
            outer_deadline, outer_clock, outer_authorize, outer_progress = previous

            def authorization():
                if outer_authorize is not None:
                    outer_authorize()
                if authorize is not None:
                    authorize()
                if outer_deadline is not None and outer_clock() >= outer_deadline:
                    raise DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。", retry="afterQuery")

            interrupted = None

            def progress():
                nonlocal interrupted
                try:
                    self.check_deadline()
                except BaseException as error:
                    interrupted = error
                    return 1
                return 0

            self._deadline, self._clock = deadline, clock
            self._authorize, self._progress = authorization, progress
            connection = self._connection
            try:
                self.check_deadline()
                wait_ms = (
                    2000
                    if deadline is None
                    else max(1, min(2000, int((deadline - clock()) * 1000)))
                )
                connection.execute(f"PRAGMA busy_timeout={wait_ms}")
                connection.set_progress_handler(progress, 1000)
                yield
                self.check_deadline()
            except sqlite3.Error as error:
                if interrupted is not None:
                    raise interrupted from error
                self.check_deadline()
                raise
            finally:
                connection.set_progress_handler(None, 0)
                self._deadline, self._clock, self._authorize, self._progress = previous
                wait_ms = (
                    2000
                    if outer_deadline is None
                    else max(1, min(2000, int((outer_deadline - outer_clock()) * 1000)))
                )
                connection.execute(f"PRAGMA busy_timeout={wait_ms}")
                connection.set_progress_handler(outer_progress, 1000 if outer_progress else 0)

    @contextmanager
    def transaction(self, *, deadline: float | None = None, clock=time.monotonic, authorize=None):
        with self._mutex:
            if self._closed or self._connection is None:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            connection = self._connection
            savepoint = f"txn_{uuid.uuid4().hex}" if connection.in_transaction else None
            budget = (
                self.budget(deadline, clock, authorize=authorize)
                if deadline is not None or authorize is not None
                else nullcontext()
            )
            started = False
            try:
                with budget:
                    self.check_deadline()
                    connection.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
                    started = True
                    yield connection
                    self.check_deadline()
                # 完成提交检查后，事务收尾不再被 SQLite 进度回调中断。
                connection.set_progress_handler(None, 0)
                if savepoint:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    connection.commit()
            except BaseException:
                connection.set_progress_handler(None, 0)
                if started and connection.in_transaction:
                    if savepoint:
                        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    else:
                        connection.rollback()
                raise
            finally:
                connection.set_progress_handler(self._progress, 1000 if self._progress else 0)

    def _put_object(self, value: object) -> str:
        self.check_deadline()
        data = dumps(value, max_bytes=33_554_432, checkpoint=self.check_deadline)
        identity = hashlib.sha256(data).hexdigest()
        path = self.path / "objects/sha256" / identity
        if path.is_symlink() or not path.resolve().is_relative_to(self.path):
            raise DeskWeaveError("RESOURCE_DENIED", "工作空间对象路径越界。")
        if path.exists():
            if path.stat().st_size != len(data) or path.read_bytes() != data:
                raise DeskWeaveError("STORAGE_CORRUPT", "工作空间对象内容损坏。")
        else:
            self._atomic_write(path, data)
        return identity

    def _read_object(self, identity: str):
        if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
            raise DeskWeaveError("STORAGE_CORRUPT", "工作空间对象引用无效。")
        path = self.path / "objects/sha256" / identity
        if path.is_symlink() or not path.resolve().is_relative_to(self.path):
            raise DeskWeaveError("RESOURCE_DENIED", "工作空间对象路径越界。")
        try:
            if path.stat().st_size > 33554432:
                raise DeskWeaveError("STORAGE_CORRUPT", "工作空间对象超过大小限制。")
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise DeskWeaveError("STORAGE_CORRUPT", "流程引用的内容缺失。") from error
        if hashlib.sha256(data).hexdigest() != identity:
            raise DeskWeaveError("STORAGE_CORRUPT", "流程引用的内容损坏。")
        return loads(data, max_bytes=33_554_432, checkpoint=self.check_deadline)

    def save_receipt(self, client: str, request_id: str, params: dict) -> dict | None:
        payload_hash = digest(
            {"method": "workflow.save", "params": params}, checkpoint=self.check_deadline
        )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_hash,result FROM command_receipt "
                "WHERE client_id=? AND request_id=?",
                (client, request_id),
            ).fetchone()
            if row is None:
                return None
            if row["payload_hash"] != payload_hash:
                raise DeskWeaveError("REQUEST_CONFLICT", "请求标识已用于另一项修改。")
            return loads(row["result"], checkpoint=self.check_deadline)

    def save(
        self,
        client: str,
        request_id: str,
        params: dict,
        *,
        request_params: dict | None = None,
        authorize_workflow=lambda _identity: None,
        authorize_asset=lambda: None,
    ) -> dict:
        payload_hash = digest(
            {
                "method": "workflow.save",
                "params": params if request_params is None else request_params,
            },
            checkpoint=self.check_deadline,
        )
        with self._mutex:
            with self.transaction() as connection:
                receipt = connection.execute(
                    "SELECT * FROM command_receipt WHERE client_id=? AND request_id=?",
                    (client, request_id),
                ).fetchone()
                if receipt:
                    if receipt["payload_hash"] != payload_hash:
                        raise DeskWeaveError("REQUEST_CONFLICT", "请求标识已用于另一项修改。")
                    return loads(receipt["result"], checkpoint=self.check_deadline)
                current = connection.execute(
                    "SELECT head_revision FROM workflow WHERE id=?", (params["workflowId"],)
                ).fetchone()
                head = current["head_revision"] if current else None
                if head != params["baseRevision"]:
                    raise DeskWeaveError(
                        "REVISION_CONFLICT",
                        "流程已更新，请读取当前版本后保存。",
                        retry="afterQuery",
                    )
            from deskweave.adapters.dependencies import Dependencies

            dependencies = Dependencies(self, authorize_workflow, authorize_asset).bind(
                params["definition"], params["dependencies"]
            )
            definition_hash = self._put_object(params["definition"])
            dependencies_hash = self._put_object(dependencies)
            revision, created = uuid.uuid4().hex, utc_time()
            result = {
                "workflowId": params["workflowId"],
                "revision": revision,
                "draftVersion": params["draftVersion"],
                "definitionHash": definition_hash,
            }
            with self.transaction() as connection:
                row = connection.execute(
                    "INSERT INTO revision(id,workflow_id,parent_id,name,definition_hash,"
                    "dependencies_hash,created_at,name_key) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        revision,
                        params["workflowId"],
                        head,
                        params["definition"]["name"],
                        definition_hash,
                        dependencies_hash,
                        created,
                        unicodedata.normalize("NFC", params["definition"]["name"]).casefold(),
                    ),
                )
                for binding in dependencies.values():
                    if binding["kind"] == "workflow":
                        connection.execute(
                            "INSERT OR IGNORE INTO revision_workflow_ref VALUES(?,?)",
                            (revision, binding["revision"]),
                        )
                    else:
                        connection.execute(
                            "INSERT OR IGNORE INTO revision_asset_ref VALUES(?,?)",
                            (revision, binding["assetVersion"]),
                        )
                if head is not None:
                    connection.execute(
                        "UPDATE revision SET valid_until=? WHERE id=?", (row.lastrowid, head)
                    )
                connection.execute(
                    "INSERT INTO workflow VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name,head_revision=excluded.head_revision,"
                    "updated_at=excluded.updated_at,sequence=excluded.sequence",
                    (
                        params["workflowId"],
                        params["definition"]["name"],
                        revision,
                        created,
                        row.lastrowid,
                    ),
                )
                connection.execute(
                    "INSERT INTO command_receipt VALUES(?,?,?,?,?,?,?)",
                    (
                        client,
                        request_id,
                        "workflow.save",
                        params["workflowId"],
                        payload_hash,
                        dumps(result).decode(),
                        created,
                    ),
                )
            return result

    def read(self, workflow_id: str, revision: str) -> dict:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM revision WHERE workflow_id=? AND id=?", (workflow_id, revision)
            ).fetchone()
        if row is None:
            raise DeskWeaveError("RESOURCE_NOT_FOUND", "流程版本不存在。")
        return {
            "workflowId": workflow_id,
            "revision": revision,
            "definition": self._read_object(row["definition_hash"]),
            "dependencies": self._read_object(row["dependencies_hash"]),
            "definitionHash": row["definition_hash"],
        }

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._connection.close()
            if self._lock_file is not None:
                self._lock_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
