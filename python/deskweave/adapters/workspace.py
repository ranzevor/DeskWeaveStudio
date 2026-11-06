from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from deskweave.errors import DeskWeaveError
from deskweave.json import digest, dumps, loads


def utc_time(timestamp: float | None = None) -> str:
    value = datetime.now(UTC) if timestamp is None else datetime.fromtimestamp(timestamp, UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class Workspace:
    def __init__(self, path: Path, *, create: bool = False, name: str = "工作空间"):
        self.path = path.resolve()
        self._mutex = threading.RLock()
        self._closed = False
        self._deadline: float | None = None
        self._clock = time.monotonic
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
                if any(item.name != ".engine.lock" for item in self.path.iterdir()):
                    raise DeskWeaveError("INVALID_ARGUMENT", "新工作空间需要空目录。")
                if not 1 <= len(name.strip()) <= 120:
                    raise DeskWeaveError("INVALID_ARGUMENT", "工作空间名称长度应为 1～120。")
                self._atomic_write(
                    descriptor,
                    dumps(
                        {
                            "formatVersion": "1.0",
                            "id": uuid.uuid4().hex,
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
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise DeskWeaveError("SCHEMA_UNSUPPORTED", "工作空间数据库版本不受支持。")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS workflow (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, head_revision TEXT NOT NULL,
                    updated_at TEXT NOT NULL, sequence INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS revision (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    workflow_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
                    definition_hash TEXT NOT NULL, dependencies_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS revision_workflow ON revision(workflow_id, sequence);
                CREATE TABLE IF NOT EXISTS command_receipt (
                    client_id TEXT NOT NULL, request_id TEXT NOT NULL, method TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, payload_hash TEXT NOT NULL, result TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(client_id, request_id));
                CREATE TABLE IF NOT EXISTS query_snapshot (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    authorization_version INTEGER NOT NULL,
                    binding TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    extra TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS query_member (
                    snapshot_id TEXT NOT NULL REFERENCES query_snapshot(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, ordinal));
                CREATE TABLE IF NOT EXISTS query_cursor (
                    token TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL
                    REFERENCES query_snapshot(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, UNIQUE(snapshot_id, ordinal));
                PRAGMA user_version=1;
                DELETE FROM query_snapshot;
            """)
        except BaseException:
            self.close()
            raise

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
        if self._deadline is not None and self._clock() >= self._deadline:
            raise DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。", retry="afterQuery")

    @contextmanager
    def budget(self, deadline: float, clock=time.monotonic):
        with self._mutex:
            if self._closed or self._connection is None:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            previous = self._deadline, self._clock
            self._deadline, self._clock = deadline, clock
            connection = self._connection
            try:
                self.check_deadline()
                wait_ms = max(1, min(2000, int((deadline - clock()) * 1000)))
                connection.execute(f"PRAGMA busy_timeout={wait_ms}")
                connection.set_progress_handler(lambda: int(clock() >= deadline), 1000)
                yield
                self.check_deadline()
            except sqlite3.Error:
                self.check_deadline()
                raise
            finally:
                connection.set_progress_handler(None, 0)
                connection.execute("PRAGMA busy_timeout=2000")
                self._deadline, self._clock = previous

    @contextmanager
    def transaction(self):
        with self._mutex:
            if self._closed or self._connection is None:
                raise DeskWeaveError("WORKSPACE_CLOSED", "工作空间已关闭。")
            connection = self._connection
            try:
                self.check_deadline()
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                self.check_deadline()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _put_object(self, value: object) -> str:
        self.check_deadline()
        data = dumps(value, max_bytes=33_554_432)
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
        return loads(data, max_bytes=33_554_432)

    def save(self, client: str, request_id: str, params: dict) -> dict:
        payload_hash = digest({"method": "workflow.save", "params": params})
        with self._mutex:
            with self.transaction() as connection:
                receipt = connection.execute(
                    "SELECT * FROM command_receipt WHERE client_id=? AND request_id=?",
                    (client, request_id),
                ).fetchone()
                if receipt:
                    if receipt["payload_hash"] != payload_hash:
                        raise DeskWeaveError("REQUEST_CONFLICT", "请求标识已用于另一项修改。")
                    return loads(receipt["result"])
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
            definition_hash = self._put_object(params["definition"])
            dependencies_hash = self._put_object(params["dependencies"])
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
                    "dependencies_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        revision,
                        params["workflowId"],
                        head,
                        params["definition"]["name"],
                        definition_hash,
                        dependencies_hash,
                        created,
                    ),
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
