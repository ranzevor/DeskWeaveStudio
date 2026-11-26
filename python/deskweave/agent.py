from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from deskweave.coordination.directory import Directory, StartupLock, backend, runtime_directory
from deskweave.coordination.startup import InvalidDiscovery, check_idle
from deskweave.domain.execution_control import (
    compute_segment_payload_hash,
    validate_lease_acquire_result,
    validate_lease_release_result,
    validate_lease_snapshot,
    validate_segment_result,
    validate_target_bind_result,
)
from deskweave.domain.platform import validate_desktop_capabilities, validate_desktop_topology
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME as _MAX_CONTROL_FRAME
from deskweave.json import dumps, loads
from deskweave.process import _wire as wire
from deskweave.process._wire import write_frame
from deskweave.process.control import remaining
from deskweave.transport.native import Installation, NativeListener, ProcessBinding, connect
from deskweave.transport.native._common import (
    Budget,
    Cancellation,
    InitializationError,
    Resource,
    cleanup,
    closed,
    rejected,
)
from deskweave.transport.native.channel import local_binding
from deskweave.transport.native.identity import digest_file, stamp

_OWNER = ".agent-owner.lock"
_STARTUP = ".agent-start.lock"
_DISCOVERY = "agent.json"
_MAX_FRAME = 4096
_DISCOVERY_LIMIT = 16384
_MAX_SAFE_INT = 9_007_199_254_740_991
_MAX_IGNORED_IDS = 256


class _PendingWaiter:
    __slots__ = ("done", "error", "frame")

    def __init__(self):
        self.done = False
        self.frame = None
        self.error = None


def _is_local_x11_display(value: str) -> bool:
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= 64:
        return False
    for prefix in ("unix/:", "unix:", ":"):
        if trimmed.startswith(prefix):
            rest = trimmed[len(prefix) :]
            break
    else:
        return False
    num, sep, screen = rest.partition(".")
    if not num or not num.isascii() or not num.isdigit():
        return False
    if sep and (not screen or not screen.isascii() or not screen.isdigit()):
        return False
    return int(num) <= 0xFFFFFFFF


def _check_wayland_backend(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("Wayland 后端选择无效。")
    normalized = value.strip().lower()
    if normalized not in {"auto", "portal", "compositor"}:
        raise ValueError("Wayland 后端选择必须为 auto、portal 或 compositor。")
    return normalized


def _is_safe_socket_spec(value: str) -> bool:
    return (
        1 <= len(value) <= 255
        and "\x00" not in value
        and value not in {".", ".."}
        and (
            (value.startswith("/") and ".." not in value.split("/") and "\\" not in value)
            or ("/" not in value and "\\" not in value)
        )
    )


_UNSUPPORTED_DBUS_ADDRESS = "unsupported:"
_UNIX_EXCLUSIVE_LOCATOR_KEYS = frozenset({"abstract", "tmpdir", "dir", "runtime"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_valid_dbus_key(key: str) -> bool:
    return bool(key) and key.isascii() and all(ch.isalnum() or ch in "_-" for ch in key)


def _is_dbus_optionally_escaped_char(ch: str) -> bool:
    # D-Bus Server Addresses optionally-escaped byte set `[-0-9A-Za-z_/.\\*]` minus `\` for
    # path-separator safety; all other bytes (including space and non-ASCII) must be %-escaped.
    return ch.isascii() and (ch.isalnum() or ch in "-_/.*")


def _is_safe_dbus_unix_path_value(item: str) -> bool:
    return (
        bool(item)
        and item.startswith("/")
        and ".." not in item.split("/")
        and all(_is_dbus_optionally_escaped_char(ch) for ch in item)
    )


def _is_safe_dbus_param_value(item: str) -> bool:
    if not item:
        return False
    idx = 0
    length = len(item)
    while idx < length:
        ch = item[idx]
        if _is_dbus_optionally_escaped_char(ch):
            idx += 1
            continue
        if ch == "%" and idx + 2 < length:
            hi, lo = item[idx + 1], item[idx + 2]
            if hi in _HEX_DIGITS and lo in _HEX_DIGITS and not (hi == "0" and lo == "0"):
                idx += 3
                continue
        return False
    return True


def _parse_dbus_entry(entry: str) -> tuple[bool, bool]:
    """Validates a single D-Bus address entry.

    Returns `(well_formed, is_safe_unix_path_entry)`. Rejects mutually exclusive `unix:` locator
    keys (`path` combined with `abstract`/`tmpdir`/`dir`/`runtime`), duplicate `path=` keys,
    unescaped bytes outside the D-Bus optionally-escaped set (such as spaces), path traversal,
    percent escapes in `unix:path=`, backslashes, and malformed key/value pairs.
    """
    transport, sep, rest = entry.partition(":")
    if (
        not sep
        or not 1 <= len(transport) <= 32
        or not transport.isascii()
        or not all(ch.isalnum() or ch in "+-" for ch in transport)
    ):
        return False, False
    if not rest:
        # Standard D-Bus allows parameterless non-unix transports (e.g. `autolaunch:`, `systemd:`);
        # `unix:` always requires an explicit locator parameter.
        return transport != "unix", False

    seen_path = False
    seen_other_locator = False
    for pair in rest.split(","):
        key, eq, item = pair.partition("=")
        if not eq or not _is_valid_dbus_key(key) or not item:
            return False, False
        if transport == "unix" and key == "path":
            if seen_path or not _is_safe_dbus_unix_path_value(item):
                return False, False
            seen_path = True
        else:
            if not _is_safe_dbus_param_value(item):
                return False, False
            if transport == "unix" and key in _UNIX_EXCLUSIVE_LOCATOR_KEYS:
                seen_other_locator = True
    if transport == "unix" and seen_path and seen_other_locator:
        return False, False
    return True, transport == "unix" and seen_path


def _sanitize_dbus_session_bus_address(value: str) -> str:
    """Sanitizes an explicitly configured DBUS_SESSION_BUS_ADDRESS without dropping it.

    Extracts and emits safe `unix:path=...` entries (allowing valid `;`-separated multi-address
    lists, optional single trailing `;`, and well-formed fallback transports like `autolaunch:`
    alongside at least one `unix:path=...` entry, which are stripped from the emitted value) and
    replaces unsupported, non-unix-only, or malformed explicit addresses (such as `autolaunch:`
    alone, `unix:abstract=...`, `tcp:...`, control characters, mutually exclusive `unix:` keys,
    or unsafe paths) with an inert sentinel so the agent never silently falls back to
    `$XDG_RUNTIME_DIR/bus`.
    """
    if not 1 <= len(value) <= 4096 or any(not ch.isprintable() for ch in value):
        return _UNSUPPORTED_DBUS_ADDRESS
    body = value[:-1] if value.endswith(";") else value
    if not body:
        return _UNSUPPORTED_DBUS_ADDRESS
    entries = body.split(";")
    if len(entries) > 16 or any(not entry for entry in entries):
        return _UNSUPPORTED_DBUS_ADDRESS
    safe_unix_entries: list[str] = []
    for entry in entries:
        well_formed, is_safe_unix = _parse_dbus_entry(entry)
        if not well_formed:
            return _UNSUPPORTED_DBUS_ADDRESS
        if is_safe_unix:
            safe_unix_entries.append(entry)
    if not safe_unix_entries:
        return _UNSUPPORTED_DBUS_ADDRESS
    return ";".join(safe_unix_entries)


def _sanitize_linux_session_env(environ) -> dict[str, str]:
    result: dict[str, str] = {}
    session_type = environ.get("XDG_SESSION_TYPE")
    if (
        type(session_type) is str
        and (trimmed := session_type.strip())
        and len(trimmed) <= 32
        and trimmed.isascii()
        and trimmed.isalnum()
    ):
        result["XDG_SESSION_TYPE"] = trimmed
    session_id = environ.get("XDG_SESSION_ID")
    if (
        type(session_id) is str
        and (trimmed := session_id.strip())
        and len(trimmed) <= 64
        and trimmed.isascii()
        and all(ch.isalnum() or ch in "-_" for ch in trimmed)
    ):
        result["XDG_SESSION_ID"] = trimmed
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if (
        type(runtime_dir) is str
        and (trimmed := runtime_dir.strip())
        and 1 <= len(trimmed) <= 4096
        and "\x00" not in trimmed
        and trimmed.startswith("/")
        and ".." not in trimmed.split("/")
    ):
        result["XDG_RUNTIME_DIR"] = trimmed
    wayland = environ.get("WAYLAND_DISPLAY")
    if type(wayland) is str and (trimmed := wayland.strip()) and _is_safe_socket_spec(trimmed):
        result["WAYLAND_DISPLAY"] = trimmed
    compositor_socket = environ.get("DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET")
    if (
        type(compositor_socket) is str
        and (trimmed := compositor_socket.strip())
        and _is_safe_socket_spec(trimmed)
    ):
        result["DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET"] = trimmed
    wayland_backend = environ.get("DESKWEAVE_WAYLAND_BACKEND")
    if type(wayland_backend) is str and (trimmed := wayland_backend.strip().lower()) in {
        "auto",
        "portal",
        "compositor",
    }:
        result["DESKWEAVE_WAYLAND_BACKEND"] = trimmed
    dbus_addr = environ.get("DBUS_SESSION_BUS_ADDRESS")
    if type(dbus_addr) is str and dbus_addr != "":
        result["DBUS_SESSION_BUS_ADDRESS"] = _sanitize_dbus_session_bus_address(dbus_addr)
    display = environ.get("DISPLAY")
    if type(display) is str and "\x00" not in display and _is_local_x11_display(display):
        result["DISPLAY"] = display.strip()
    return result


def _scope(local):
    identity = local.identity
    session = identity.os_session_id
    if sys.platform == "win32":
        session = session.partition(":")[0]
    return hashlib.sha256(dumps([identity.user_id, session])).hexdigest()


def _runtime(local):
    if sys.platform == "linux":
        base = Path("/tmp") / f"deskweave-agent-{os.geteuid()}"
        backend.make_private(base, local)
    else:
        base = backend.runtime_base()
    if sys.platform != "win32":
        with Directory(base, principal=local):
            pass
    return runtime_directory(local, base / ("dw-agent-" + _runtime_key(local)))


def _runtime_key(local):
    identity = local.identity
    session = local.process.logon_sid if sys.platform == "win32" else identity.os_session_id
    return hashlib.sha256(dumps([identity.user_id, session])).hexdigest()[:24]


def _check_workspace_id(workspace_id):
    if type(workspace_id) is not str or not 1 <= len(workspace_id) <= 128 or "\x00" in workspace_id:
        raise ValueError("工作空间身份无效。")
    return workspace_id


def _is_hex(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(letter in "0123456789abcdef" for letter in value)
    )


def _is_session_id(value):
    return (
        type(value) is str
        and len(value) == 32
        and all(letter in "0123456789abcdef" for letter in value)
    )


def _check_owner(runtime, expected):
    runtime.check()
    with runtime.open(_OWNER) as current:
        if backend.file_identity(current) != expected:
            raise DeskWeaveError("RESOURCE_DENIED", "桌面 Agent 所有权对象已改变。")


def _proof(secret, role, engine_pid, agent_pid, epoch, *, address=None, session=None):
    identity = struct.pack("<II", engine_pid, agent_pid) + epoch.encode("ascii")
    if address is not None:
        identity += b"\0" + address.encode("utf-8")
    if session is not None:
        identity += b"\0" + session.encode("ascii")
    return hmac.digest(secret, b"deskweave/desktop-agent/v1\0" + role + b"\0" + identity, "sha256")


def _attach_ready_proof(proof_key, engine_pid, agent_pid, workspace_id, epoch, nonce, session):
    payload = (
        b"deskweave/desktop-agent/attach-ready/v1\0"
        + struct.pack("<II", engine_pid, agent_pid)
        + workspace_id.encode("utf-8")
        + b"\0"
        + epoch.encode("ascii")
        + b"\0"
        + nonce
        + b"\0"
        + session.encode("ascii")
    )
    return hmac.digest(proof_key, payload, "sha256")


def _bytes(value, size):
    if (
        type(value) is not list
        or len(value) != size
        or any(type(part) is not int or not 0 <= part <= 255 for part in value)
    ):
        raise rejected()
    return bytes(value)


def _open_image(path):
    if sys.platform == "win32":
        import msvcrt

        from deskweave.transport.native import _windows as native

        handle = native.CreateFile(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == native.INVALID:
            raise rejected()
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            native.checked(native.CloseHandle(handle))
            raise
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    return os.fdopen(descriptor, "rb", buffering=0)


class AgentDiscovery:
    def __init__(self, runtime, scope):
        self.runtime = runtime
        self.scope = scope

    def read(self):
        try:
            data = self.runtime.read(_DISCOVERY, limit=_DISCOVERY_LIMIT)
        except DeskWeaveError as error:
            if error.code == "LIMIT_EXCEEDED":
                raise InvalidDiscovery() from error
            raise
        if data is None:
            return None
        try:
            value = loads(data)
        except (DeskWeaveError, ValueError, TypeError) as error:
            raise InvalidDiscovery() from error
        if type(value) is not dict:
            raise InvalidDiscovery()
        version = value.get("version")
        if type(version) is str and version != "1.0":
            raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "Desktop Agent 发现记录协议不兼容。")
        try:
            wire.fields(value, ("version", "scope", "agentEpoch", "address", "peer", "owner"))
            if value["version"] != "1.0" or value["scope"] != self.scope:
                raise InvalidDiscovery()
            if not _is_hex(value["agentEpoch"]):
                raise InvalidDiscovery()
            address = value["address"]
            if type(address) is not str or not 1 <= len(address) <= 4096 or "\x00" in address:
                raise InvalidDiscovery()
            owner = value["owner"]
            if (
                type(owner) is not list
                or len(owner) != 2
                or any(type(item) is not str or not 1 <= len(item) <= 128 for item in owner)
            ):
                raise InvalidDiscovery()
            peer = wire.peer_value(value["peer"])
            if peer.instance_epoch != value["agentEpoch"]:
                raise InvalidDiscovery()
        except (DeskWeaveError, ValueError, TypeError, KeyError) as error:
            raise InvalidDiscovery() from error
        return {
            "version": "1.0",
            "scope": self.scope,
            "agentEpoch": value["agentEpoch"],
            "address": address,
            "peer": peer,
            "owner": tuple(owner),
        }

    def publish(self, epoch, address, peer, owner_identity):
        value = {
            "version": "1.0",
            "scope": self.scope,
            "agentEpoch": epoch,
            "address": address,
            "peer": peer.as_dict(),
            "owner": list(owner_identity),
        }
        self.runtime.write(_DISCOVERY, dumps(value, max_bytes=_DISCOVERY_LIMIT))

    def remove(self, expected=None):
        def matches(data):
            if expected is None:
                return True
            try:
                value = loads(data)
            except (DeskWeaveError, ValueError, TypeError):
                return True
            return type(value) is not dict or (
                value.get("agentEpoch") == expected["agentEpoch"]
                and value.get("peer") == expected["peer"].as_dict()
            )

        return self.runtime.remove(_DISCOVERY, matches=matches)


class AgentAuthority:
    def __init__(self, engines, submit_signature):
        self._engines = {binding.identity.process_id: binding for binding in engines}
        self.submit_signature = submit_signature
        self._owners = {}
        self._lock = threading.Lock()
        self._namespaces = _linux_namespaces(os.getpid()) if sys.platform == "linux" else None

    def _check_namespaces(self, identity):
        if (
            self._namespaces is not None
            and _linux_namespaces(identity.process_id) != self._namespaces
        ):
            raise rejected()

    def check_engine(self, engine, budget):
        registered = self._engines.get(engine.identity.process_id)
        if registered is None or registered.identity != engine.identity:
            raise rejected()
        registered.check(budget.check_time)
        self._check_namespaces(registered.identity)
        return registered

    def check_owner(self, engine, owner, budget):
        scope = _scope(engine)
        budget.acquire(self._lock)
        try:
            self._match_owner(scope, backend.file_identity(owner))
        finally:
            self._lock.release()

    def _match_owner(self, scope, identity):
        if self._owners.get(scope, identity) != identity:
            raise DeskWeaveError("RESOURCE_DENIED", "桌面 Agent 所有权登记已改变。")

    def open_runtime(self, engine, budget):
        return _runtime(self.check_engine(engine, budget))

    @contextmanager
    def installation_update(self, engine, *, timeout=10, cancel=None):
        budget = Budget(timeout, cancel)
        with self.open_runtime(engine, budget) as runtime:
            with StartupLock(runtime, budget, name=_STARTUP):
                yield

    def registration(
        self, engine, peer, owner, runtime, nonce, budget, *, workspace_id="workspace"
    ):
        registered = self.check_engine(engine, budget)
        peer.check(budget.check_time)
        self._check_namespaces(peer.identity)
        workspace_id = _check_workspace_id(workspace_id)
        scope = _scope(registered)
        owner_identity = backend.file_identity(owner)
        budget.acquire(self._lock)
        try:
            self._match_owner(scope, owner_identity)
            _check_owner(runtime, owner_identity)
            claim = dumps(
                {
                    "kind": "deskweave/agent-registration/v1",
                    "enginePid": registered.identity.process_id,
                    "agentPid": peer.identity.process_id,
                    "agentEpoch": peer.identity.instance_epoch,
                    "workspaceId": workspace_id,
                    "owner": list(owner_identity),
                    "scope": scope,
                    "nonce": list(nonce),
                }
            )
            budget.check()
            self._owners[scope] = owner_identity
            return claim
        finally:
            self._lock.release()

    def attach_registration(
        self, engine, peer, owner, runtime, workspace_id, nonce, proof_key_hash, budget
    ):
        registered = self.check_engine(engine, budget)
        peer.check(budget.check_time)
        self._check_namespaces(peer.identity)
        if (
            registered.identity.platform,
            registered.identity.user_id,
            registered.identity.os_session_id,
        ) != (
            peer.identity.platform,
            peer.identity.user_id,
            peer.identity.os_session_id,
        ):
            raise rejected()
        workspace_id = _check_workspace_id(workspace_id)
        if not _is_hex(proof_key_hash):
            raise ValueError("接入证明摘要无效。")
        scope = _scope(registered)
        owner_identity = backend.file_identity(owner)
        budget.acquire(self._lock)
        try:
            self._match_owner(scope, owner_identity)
            _check_owner(runtime, owner_identity)
            claim = dumps(
                {
                    "kind": "deskweave/agent-attach/v1",
                    "enginePid": registered.identity.process_id,
                    "agentPid": peer.identity.process_id,
                    "agentEpoch": peer.identity.instance_epoch,
                    "workspaceId": workspace_id,
                    "owner": list(owner_identity),
                    "scope": scope,
                    "nonce": list(nonce),
                    "proofKeyHash": proof_key_hash,
                }
            )
            budget.check()
            self._owners[scope] = owner_identity
            return claim
        finally:
            self._lock.release()

    def confirmation_claim(
        self,
        engine,
        peer,
        owner_identity,
        workspace_id,
        nonce,
        *,
        lease_id,
        pause_id,
        generation,
        checkpoint_hash,
        resume_plan_hash,
        restore_target_token=None,
        pointer_to_restore=None,
        ttl_ms=60_000,
        budget=None,
    ):
        budget = budget or Budget(5)
        registered = self.check_engine(engine, budget)
        peer.check(budget.check_time)
        self._check_namespaces(peer.identity)
        if (
            registered.identity.platform,
            registered.identity.user_id,
            registered.identity.os_session_id,
        ) != (
            peer.identity.platform,
            peer.identity.user_id,
            peer.identity.os_session_id,
        ):
            raise rejected()
        workspace_id = _check_workspace_id(workspace_id)
        if (
            type(owner_identity) not in (list, tuple)
            or len(owner_identity) != 2
            or any(type(item) is not str or not 1 <= len(item) <= 128 for item in owner_identity)
        ):
            raise ValueError("桌面 Agent 所有权身份无效。")
        owner_tuple = tuple(owner_identity)
        if type(nonce) not in (bytes, bytearray) or len(nonce) != 32:
            raise ValueError("确认声明挑战 nonce 必须为 32 字节。")
        for field_name, ident in (("leaseId", lease_id), ("pauseId", pause_id)):
            if type(ident) is not str or not 1 <= len(ident) <= 256 or "\x00" in ident:
                raise ValueError(f"字段 {field_name} 无效。")
        if type(generation) is not int or not 0 <= generation <= _MAX_SAFE_INT:
            raise ValueError("字段 generation 无效。")
        if not _is_hex(checkpoint_hash) or not _is_hex(resume_plan_hash):
            raise ValueError("确认声明摘要字段必须为 64 位小写十六进制字符串。")
        if restore_target_token is not None and (
            type(restore_target_token) is not str
            or not 1 <= len(restore_target_token) <= 256
            or "\x00" in restore_target_token
        ):
            raise ValueError("字段 restoreTargetToken 无效。")
        if pointer_to_restore is not None and type(pointer_to_restore) is not dict:
            raise ValueError("字段 pointerToRestore 必须为对象或 null。")
        if type(ttl_ms) is not int or not 1 <= ttl_ms <= 60_000:
            raise ValueError("字段 ttlMs 必须在 1..=60000 之间。")
        scope = _scope(registered)
        budget.acquire(self._lock)
        try:
            self._match_owner(scope, owner_tuple)
            claim = dumps(
                {
                    "kind": "deskweave/agent-confirmation/v1",
                    "agentPid": peer.identity.process_id,
                    "agentEpoch": peer.identity.instance_epoch,
                    "owner": list(owner_tuple),
                    "scope": scope,
                    "workspaceId": workspace_id,
                    "purpose": "lease.resume",
                    "nonce": list(nonce),
                    "leaseId": lease_id,
                    "pauseId": pause_id,
                    "generation": generation,
                    "checkpointHash": checkpoint_hash,
                    "resumePlanHash": resume_plan_hash,
                    "restoreTargetToken": restore_target_token,
                    "pointerToRestore": pointer_to_restore,
                    "ttlMs": ttl_ms,
                },
                max_bytes=_MAX_FRAME,
                checkpoint=budget.check,
            )
            budget.check()
            self._owners[scope] = owner_tuple
            return claim
        finally:
            self._lock.release()


def _linux_namespaces(pid):
    try:
        return tuple(backend.identity(os.stat(f"/proc/{pid}/ns/{name}")) for name in ("mnt", "net"))
    except OSError as error:
        raise rejected() from error


def _signature_result(request, budget):
    while True:
        delay = budget.check_time()
        signature = request.poll()
        if signature is not None:
            if type(signature) is not bytes or len(signature) != 64:
                raise rejected()
            budget.check()
            return signature
        if budget.cancel is None:
            time.sleep(delay)
        else:
            budget.cancel.wait(delay)


def _watch_process(process):
    try:
        process.wait()
    except OSError:
        pass


class AgentProcess(Resource):
    def __init__(
        self,
        local: ProcessBinding,
        installation: Installation,
        authority: AgentAuthority,
        *,
        workspace_id: str = "workspace",
        timeout=10,
        cancel=None,
        idle_grace=2.0,
        wayland_backend: str | None = None,
    ):
        if type(installation) is not Installation or type(authority) is not AgentAuthority:
            raise TypeError("启动 Desktop Agent 需要受信安装清单和启动授权方。")
        _check_workspace_id(workspace_id)
        check_idle(idle_grace)
        self.wayland_backend = _check_wayland_backend(wayland_backend)
        self.local = local
        self.installation = installation
        self.authority = authority
        self.workspace_id = workspace_id
        self.idle_grace = idle_grace
        self.epoch = None
        self.session = None
        self.created = False
        self._published = False
        self._process = self._peer = self._listener = self._channel = None
        self._runtime_dir = self._owner = None
        self._registration_dir = self._signature = self._owner_identity = None
        self._io = threading.RLock()
        self._stopping = threading.Event()
        budget = Budget(timeout, cancel)
        try:
            local_binding(local, budget.check_time)
            self._initialize_resource("_runtime_dir", authority.open_runtime, local, budget)
            scope = _scope(local)
            discovery = AgentDiscovery(self._runtime_dir, scope)
            wake = cancel or threading.Event()
            while True:
                budget.check_time()
                record, protocol_error = self._read_discovery(discovery)
                failure = protocol_error
                if record is not None:
                    try:
                        attached = self._attach(record, scope, budget)
                    except (DeskWeaveError, OSError, ValueError) as error:
                        try:
                            self._close_started()
                        except BaseException as closing:
                            raise InitializationError(self, [error, closing]) from None
                        failure = error
                        if isinstance(error, DeskWeaveError) and error.code in {
                            "CANCELLED",
                            "DEADLINE_EXCEEDED",
                            "PROTOCOL_UNSUPPORTED",
                            "SCHEMA_UNSUPPORTED",
                            "RESOURCE_DENIED",
                        }:
                            raise
                    else:
                        if attached:
                            budget.check()
                            break
                        self._close_started()
                with StartupLock(self._runtime_dir, budget, name=_STARTUP):
                    try:
                        current, current_protocol_error = self._read_discovery(discovery)
                        if current != record or (
                            current_protocol_error is not None and protocol_error is None
                        ):
                            continue
                        self._initialize_resource(
                            "_owner", self._runtime_dir.open, _OWNER, write=True, create=True
                        )
                        self._owner_identity = backend.file_identity(self._owner)
                        _check_owner(self._runtime_dir, self._owner_identity)
                        authority.check_owner(local, self._owner, budget)
                        owner_busy = not backend.acquire(self._owner)
                        if not owner_busy:
                            backend.release(self._owner)
                        if owner_busy:
                            self._close_resource("_owner")
                            if current_protocol_error is not None:
                                raise current_protocol_error
                            if (
                                failure is not None
                                and isinstance(failure, DeskWeaveError)
                                and failure.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED"}
                            ):
                                raise failure
                            launched = False
                        else:
                            if current is not None or current_protocol_error is not None:
                                discovery.remove(current)
                            self.epoch = secrets.token_hex(32)
                            address = self._launch(budget)
                            _check_owner(self._runtime_dir, self._owner_identity)
                            discovery.publish(
                                self.epoch, address, self._peer.identity, self._owner_identity
                            )
                            self._published = True
                            self.created = True
                            threading.Thread(
                                target=_watch_process,
                                args=(self._process,),
                                name="deskweave-agent-reap",
                                daemon=True,
                            ).start()
                            self._close_resource("_owner")
                            budget.check()
                            launched = True
                    except BaseException as error:
                        try:
                            self._close_started()
                        except BaseException as closing:
                            raise InitializationError(self, [error, closing]) from None
                        raise
                if launched:
                    break
                wake.wait(min(0.05, budget.check_time()))
        except BaseException as error:
            self._abort_initialization(error)
            raise

    @staticmethod
    def _read_discovery(discovery):
        try:
            return discovery.read(), None
        except InvalidDiscovery:
            return None, None
        except DeskWeaveError as error:
            if error.code == "PROTOCOL_UNSUPPORTED":
                return None, error
            raise

    def _initialize_resource(self, name, factory, *args, **kwargs):
        try:
            setattr(self, name, factory(*args, **kwargs))
        except InitializationError as error:
            setattr(self, name, error.resource)
            raise

    def _attach(self, record, scope, budget):
        try:
            self._initialize_resource("_owner", self._runtime_dir.open, _OWNER)
        except FileNotFoundError:
            return False
        owner_identity = backend.file_identity(self._owner)
        self.authority.check_owner(self.local, self._owner, budget)
        if owner_identity != record["owner"]:
            return False
        self._initialize_resource(
            "_peer",
            ProcessBinding,
            record["peer"].process_id,
            self.installation,
            instance_epoch=record["agentEpoch"],
            timeout=remaining(budget),
            cancel=budget.cancel,
        )
        if self._peer.identity != record["peer"]:
            raise rejected()
        self.local.check(budget.check_time)
        self._initialize_resource(
            "_channel",
            connect,
            record["address"],
            self.local,
            self._peer,
            timeout=remaining(budget),
            cancel=budget.cancel,
        )
        self._channel.max_frame_bytes = _MAX_FRAME
        challenge = self._channel.receive(budget=budget)
        if (
            challenge.get("kind") == "agent.attach.challenge"
            and type(challenge.get("version")) is int
            and challenge["version"] != 1
        ):
            raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "Desktop Agent 接入协议版本不受支持。")
        if (
            challenge.keys() != {"kind", "version", "agentEpoch", "scope", "nonce"}
            or challenge["kind"] != "agent.attach.challenge"
            or type(challenge["version"]) is not int
            or challenge["version"] != 1
        ):
            raise rejected()
        if challenge["agentEpoch"] != record["agentEpoch"] or challenge["scope"] != scope:
            raise rejected()
        nonce = _bytes(challenge["nonce"], 32)
        proof_key = secrets.token_bytes(32)
        proof_key_hash = hashlib.sha256(proof_key).hexdigest()
        self._initialize_resource(
            "_registration_dir", self.authority.open_runtime, self.local, budget
        )
        claim = self.authority.attach_registration(
            self.local,
            self._peer,
            self._owner,
            self._registration_dir,
            self.workspace_id,
            nonce,
            proof_key_hash,
            budget,
        )
        self._close_resource("_registration_dir")
        self._initialize_resource("_signature", self.authority.submit_signature, claim, budget)
        signature = _signature_result(self._signature, budget)
        self._close_resource("_signature")
        self._channel.send(
            {
                "kind": "agent.attach.hello",
                "version": 1,
                "proofKey": list(proof_key),
                "registration": claim.decode("utf-8"),
                "signature": list(signature),
            },
            budget=budget,
        )
        ready = self._channel.receive(budget=budget)
        if (
            ready.keys() != {"kind", "agentEpoch", "workspaceId", "session", "proof"}
            or ready["kind"] != "agent.attach.ready"
            or ready["agentEpoch"] != record["agentEpoch"]
            or ready["workspaceId"] != self.workspace_id
            or not _is_session_id(ready["session"])
        ):
            raise rejected()
        expected = _attach_ready_proof(
            proof_key,
            self.local.identity.process_id,
            self._peer.identity.process_id,
            self.workspace_id,
            record["agentEpoch"],
            nonce,
            ready["session"],
        )
        if not hmac.compare_digest(_bytes(ready["proof"], 32), expected):
            raise rejected()
        _check_owner(self._runtime_dir, owner_identity)
        self._close_resource("_owner")
        self._owner_identity = owner_identity
        self._channel.max_frame_bytes = _MAX_CONTROL_FRAME
        self.epoch = record["agentEpoch"]
        self.session = ready["session"]
        self.created = False
        return True

    def _launch(self, budget):
        executable = self.installation.executable
        for entry in self.installation.components:
            if digest_file(entry.path, budget.check_time)[0] != entry.sha256:
                raise rejected()
        self._initialize_resource("_listener", NativeListener, self.local, budget=budget)
        secret = secrets.token_bytes(32)
        engine = self.local.identity
        with _open_image(executable.path) as image:
            digest, observed = digest_file(executable.path, budget.check_time)
            if digest != executable.sha256 or observed != stamp(os.fstat(image.fileno())):
                raise rejected()
            budget.check()
            process = self._spawn(executable.path, image)
        with process.stdin:
            self._initialize_resource(
                "_peer",
                ProcessBinding,
                process.pid,
                self.installation,
                instance_epoch=self.epoch,
                timeout=remaining(budget),
                cancel=budget.cancel,
            )
            agent = self._peer.identity
            if (engine.platform, engine.user_id, engine.os_session_id) != (
                agent.platform,
                agent.user_id,
                agent.os_session_id,
            ):
                raise rejected()
            self.local.check(budget.check_time)
            bootstrap = {
                "version": 1,
                "enginePid": engine.process_id,
                "agentEpoch": self.epoch,
                "address": self._listener.address,
                "secret": list(secret),
                "idleGraceMs": round(self.idle_grace * 1000),
            }
            if self.wayland_backend is not None:
                bootstrap["waylandBackend"] = self.wayland_backend
            write_frame(process.stdin, bootstrap)
        self._initialize_resource(
            "_channel",
            self._listener.accept,
            {process.pid: self._peer},
            timeout=remaining(budget),
            cancel=budget.cancel,
        )
        self._close_resource("_listener")
        self._channel.max_frame_bytes = _MAX_FRAME
        challenge = self._channel.receive(budget=budget)
        if challenge.keys() != {"kind", "nonce"} or challenge["kind"] != "agent.challenge":
            raise rejected()
        self._initialize_resource(
            "_registration_dir", self.authority.open_runtime, self.local, budget
        )
        claim = self.authority.registration(
            self.local,
            self._peer,
            self._owner,
            self._registration_dir,
            _bytes(challenge["nonce"], 32),
            budget,
            workspace_id=self.workspace_id,
        )
        self._close_resource("_registration_dir")
        self._initialize_resource("_signature", self.authority.submit_signature, claim, budget)
        signature = _signature_result(self._signature, budget)
        self._close_resource("_signature")
        self._channel.send(
            {
                "kind": "agent.hello",
                "proof": list(
                    _proof(secret, b"engine", engine.process_id, process.pid, self.epoch)
                ),
                "registration": claim.decode("utf-8"),
                "signature": list(signature),
            },
            budget=budget,
        )
        ready = self._channel.receive(budget=budget)
        kind = ready.get("kind")
        if kind == "agent.busy":
            if (
                ready.keys() != {"kind", "agentEpoch", "proof"}
                or ready["agentEpoch"] != self.epoch
                or not hmac.compare_digest(
                    _bytes(ready["proof"], 32),
                    _proof(secret, b"busy", engine.process_id, process.pid, self.epoch),
                )
            ):
                raise rejected()
            raise DeskWeaveError("DESKTOP_BUSY", "当前登录会话已有 Desktop Agent。")
        if (
            ready.keys() != {"kind", "agentEpoch", "address", "session", "proof"}
            or kind != "agent.ready"
            or ready["agentEpoch"] != self.epoch
            or not _is_session_id(ready["session"])
        ):
            raise rejected()
        address = ready["address"]
        if type(address) is not str or not 1 <= len(address) <= 4096 or "\x00" in address:
            raise rejected()
        if not hmac.compare_digest(
            _bytes(ready["proof"], 32),
            _proof(
                secret,
                b"agent",
                engine.process_id,
                process.pid,
                self.epoch,
                address=address,
                session=ready["session"],
            ),
        ):
            raise rejected()
        self.session = ready["session"]
        self._channel.max_frame_bytes = _MAX_CONTROL_FRAME
        return address

    def _spawn(self, path, image):
        environment = {}
        if sys.platform == "win32":
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]
        elif sys.platform == "linux":
            environment.update(_sanitize_linux_session_env(os.environ))
        launch = {}
        if sys.platform == "linux":
            descriptor = image.fileno()
            launch = {"executable": f"/proc/self/fd/{descriptor}", "pass_fds": (descriptor,)}
        self._process = subprocess.Popen(
            [os.fspath(path)],
            cwd=path.parent,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=self._owner,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            **launch,
        )
        return self._process

    @property
    def pid(self):
        return self._peer.identity.process_id if self._peer is not None else None

    @property
    def running(self):
        if (
            self._stopping.is_set()
            or getattr(self, "_fatal_error", None) is not None
            or self._channel is None
            or self._peer is None
        ):
            return False
        if self._process is not None and self._process.poll() is not None:
            return False
        try:
            self._peer._check_process()
        except BaseException:
            return False
        return True

    def _ensure_mux(self):
        with self._io:
            if not hasattr(self, "_mux_lock"):
                self._send_lock = threading.Lock()
                self._mux_lock = threading.Lock()
                self._mux_cond = threading.Condition(self._mux_lock)
                self._pending: dict[str, _PendingWaiter] = {}
                self._orphaned_rpc_ids: deque[str] = deque()
                self._orphaned_rpc_set: set[str] = set()
                self._orphaned_control: dict[str, int] = {}
                self._reader_thread: threading.Thread | None = None
                self._fatal_error: BaseException | None = None
                self._ping_lock = threading.Lock()
                self._confirm_lock = threading.Lock()

    def _has_outstanding_replies_locked(self) -> bool:
        return (
            bool(self._pending)
            or bool(self._orphaned_rpc_set)
            or any(count > 0 for count in self._orphaned_control.values())
        )

    def _mark_orphaned_locked(self, key: str, *, is_control: bool) -> None:
        if is_control:
            self._orphaned_control[key] = self._orphaned_control.get(key, 0) + 1
            return
        if key in self._orphaned_rpc_set:
            return
        while len(self._orphaned_rpc_ids) >= _MAX_IGNORED_IDS:
            evicted = self._orphaned_rpc_ids.popleft()
            self._orphaned_rpc_set.discard(evicted)
        self._orphaned_rpc_ids.append(key)
        self._orphaned_rpc_set.add(key)

    def _drain_orphaned_control(self, keys: tuple[str, ...], budget: Budget) -> None:
        fatal_to_abort: BaseException | None = None
        with self._mux_lock:
            while any(self._orphaned_control.get(key, 0) > 0 for key in keys):
                if self._stopping.is_set() or self._channel is None:
                    raise closed()
                if self._fatal_error is not None:
                    fatal_to_abort = self._fatal_error
                    break
                delay = budget.check_time()
                self._mux_cond.wait(delay)
        if fatal_to_abort is not None:
            if not self._stopping.is_set():
                self._abort(fatal_to_abort)
            raise fatal_to_abort

    def _fail_all_waiters_locked(self, error: BaseException) -> None:
        for waiter in self._pending.values():
            if not waiter.done:
                waiter.error = error
                waiter.done = True
        self._pending.clear()
        self._orphaned_rpc_ids.clear()
        self._orphaned_rpc_set.clear()
        self._orphaned_control.clear()
        self._mux_cond.notify_all()

    def _route_incoming_locked(self, response: object) -> None:
        if type(response) is not dict:
            raise rejected()
        kind = response.get("kind")
        if kind == "response":
            reply_to = response.get("replyTo")
            if type(reply_to) is not str:
                raise rejected()
            waiter = self._pending.pop(reply_to, None)
            if waiter is not None:
                waiter.frame = response
                waiter.done = True
                return
            if reply_to in self._orphaned_rpc_set:
                self._orphaned_rpc_set.discard(reply_to)
                try:
                    self._orphaned_rpc_ids.remove(reply_to)
                except ValueError:
                    pass
                return
            raise rejected()
        if kind == "agent.pong":
            key = "__pong__"
        elif kind == "agent.confirmation.challenge":
            key = "__confirm_challenge__"
        elif kind == "agent.confirmation.registered":
            key = "__confirm_register__"
        elif kind == "agent.confirmation.error":
            if (
                self._orphaned_control.get("__confirm_register__", 0) > 0
                or "__confirm_register__" in self._pending
            ):
                key = "__confirm_register__"
            elif (
                self._orphaned_control.get("__confirm_challenge__", 0) > 0
                or "__confirm_challenge__" in self._pending
            ):
                key = "__confirm_challenge__"
            else:
                raise rejected()
        else:
            raise rejected()
        orphaned_count = self._orphaned_control.get(key, 0)
        if orphaned_count > 0:
            if orphaned_count == 1:
                self._orphaned_control.pop(key, None)
            else:
                self._orphaned_control[key] = orphaned_count - 1
            return
        waiter = self._pending.pop(key, None)
        if waiter is None:
            raise rejected()
        waiter.frame = response
        waiter.done = True

    def _ensure_reader_locked(self, channel, runtime_dir) -> None:
        if self._reader_thread is None:
            thread = threading.Thread(
                target=self._reader_loop,
                args=(channel, runtime_dir),
                name="deskweave-agent-mux-reader",
                daemon=True,
            )
            self._reader_thread = thread
            thread.start()

    def _reader_loop(self, channel, runtime_dir) -> None:
        while True:
            with self._mux_lock:
                if self._stopping.is_set() or not self._has_outstanding_replies_locked():
                    self._reader_thread = None
                    self._mux_cond.notify_all()
                    return
            try:
                read_budget = Budget(86_400, Cancellation(None, self._stopping))
                response = channel.receive(budget=read_budget)
                _check_owner(runtime_dir, self._owner_identity)
            except BaseException as error:
                recorded: BaseException = closed() if self._stopping.is_set() else error
                with self._mux_lock:
                    if self._fatal_error is None:
                        self._fatal_error = recorded
                    self._fail_all_waiters_locked(self._fatal_error)
                    self._reader_thread = None
                    self._mux_cond.notify_all()
                try:
                    channel.close()
                except BaseException:
                    pass
                return
            with self._mux_lock:
                try:
                    self._route_incoming_locked(response)
                except BaseException as error:
                    recorded = closed() if self._stopping.is_set() else error
                    if self._fatal_error is None:
                        self._fatal_error = recorded
                    self._fail_all_waiters_locked(self._fatal_error)
                    self._reader_thread = None
                    self._mux_cond.notify_all()
                    break
                if not self._has_outstanding_replies_locked():
                    self._reader_thread = None
                    self._mux_cond.notify_all()
                    return
                self._mux_cond.notify_all()
        try:
            channel.close()
        except BaseException:
            pass

    @staticmethod
    def _deadline_ms(budget: Budget) -> int:
        deadline_seconds = min(remaining(budget), 86_400)
        return max(1, min(86_400_000, math.ceil(deadline_seconds * 1000)))

    def _exchange_frame(
        self,
        key: str,
        outgoing: dict,
        *,
        budget: Budget,
        is_control: bool = False,
        refresh_deadline: bool = False,
    ) -> dict:
        self._ensure_mux()
        waiter = _PendingWaiter()
        pending_raise: BaseException | None = None
        send_error_to_abort: BaseException | None = None
        budget.acquire(self._send_lock)
        try:
            with self._mux_lock:
                channel = self._channel
                runtime_dir = self._runtime_dir
                if self._stopping.is_set() or channel is None or runtime_dir is None:
                    raise closed()
                if self._fatal_error is not None:
                    pending_raise = self._fatal_error
                    if not self._stopping.is_set():
                        send_error_to_abort = self._fatal_error
                else:
                    budget.check_time()
                    if key in self._pending:
                        raise DeskWeaveError("REQUEST_CONFLICT", "并发请求标识符冲突。")
            if pending_raise is None:
                sent = False
                registered = False
                try:
                    _check_owner(runtime_dir, self._owner_identity)
                    with self._mux_lock:
                        if self._stopping.is_set():
                            raise closed()
                        if self._fatal_error is not None:
                            raise self._fatal_error
                        rem_seconds = remaining(budget)
                        if refresh_deadline:
                            outgoing["deadlineMs"] = max(
                                1, min(86_400_000, math.ceil(min(rem_seconds, 86_400) * 1000))
                            )
                        if key in self._pending:
                            raise DeskWeaveError("REQUEST_CONFLICT", "并发请求标识符冲突。")
                        self._pending[key] = waiter
                        registered = True
                    send_budget = Budget(rem_seconds, Cancellation(None, self._stopping))
                    channel.send(outgoing, budget=send_budget)
                    sent = True
                    with self._mux_lock:
                        if self._stopping.is_set():
                            if self._pending.pop(key, None) is not None:
                                self._mark_orphaned_locked(key, is_control=is_control)
                            self._mux_cond.notify_all()
                            raise closed()
                        if self._fatal_error is not None:
                            if self._pending.pop(key, None) is not None:
                                self._mark_orphaned_locked(key, is_control=is_control)
                            self._mux_cond.notify_all()
                            raise self._fatal_error
                        self._ensure_reader_locked(channel, runtime_dir)
                except BaseException as error:
                    is_pre_send_budget_error = (
                        not registered
                        and isinstance(error, DeskWeaveError)
                        and error.code in {"DEADLINE_EXCEEDED", "CANCELLED"}
                    )
                    if is_pre_send_budget_error:
                        pending_raise = error
                    else:
                        with self._mux_lock:
                            if not sent:
                                self._pending.pop(key, None)
                            recorded = closed() if self._stopping.is_set() else error
                            if self._fatal_error is None:
                                self._fatal_error = recorded
                            self._mux_cond.notify_all()
                        pending_raise = recorded
                        if not self._stopping.is_set():
                            send_error_to_abort = recorded
        finally:
            self._send_lock.release()
        if send_error_to_abort is not None and not self._stopping.is_set():
            self._abort(send_error_to_abort)
        if pending_raise is not None:
            raise pending_raise

        fatal_to_abort: BaseException | None = None
        with self._mux_lock:
            while not waiter.done:
                if self._stopping.is_set():
                    if self._pending.pop(key, None) is not None:
                        self._mark_orphaned_locked(key, is_control=is_control)
                    self._mux_cond.notify_all()
                    raise closed()
                try:
                    delay = budget.check_time()
                except BaseException:
                    if self._pending.pop(key, None) is not None:
                        self._mark_orphaned_locked(key, is_control=is_control)
                    self._mux_cond.notify_all()
                    raise
                self._mux_cond.wait(delay)

            if waiter.error is not None:
                fatal_to_abort = closed() if self._stopping.is_set() else waiter.error
            else:
                assert waiter.frame is not None
                return waiter.frame

        if fatal_to_abort is not None and not self._stopping.is_set():
            self._abort(fatal_to_abort)
        raise fatal_to_abort

    @staticmethod
    def _validate_confirmation_error(frame: dict) -> DeskWeaveError | None:
        if (
            frame.keys() != {"kind", "code", "message", "retry"}
            or type(frame["code"]) is not str
            or not 1 <= len(frame["code"]) <= 80
            or type(frame["message"]) is not str
            or not 1 <= len(frame["message"]) <= 1000
            or frame["retry"] not in {"never", "sameRequest", "afterQuery"}
        ):
            return None
        return DeskWeaveError(frame["code"], frame["message"], retry=frame["retry"])

    def probe(self, *, timeout=10, cancel=None):
        if self._stopping.is_set():
            raise closed()
        self._ensure_mux()
        budget = Budget(timeout, Cancellation(cancel, self._stopping))
        budget.acquire(self._ping_lock)
        try:
            self._drain_orphaned_control(("__pong__",), budget)
            response = self._exchange_frame(
                "__pong__",
                {"kind": "agent.ping", "agentEpoch": self.epoch},
                budget=budget,
                is_control=True,
            )
            if response != {"kind": "agent.pong", "agentEpoch": self.epoch}:
                self._abort(rejected())
                raise rejected()
            return self.epoch
        finally:
            self._ping_lock.release()

    def register_resume_confirmation(
        self,
        *,
        lease_id: str,
        pause_id: str,
        generation: int,
        checkpoint_hash: str,
        resume_plan_hash: str,
        restore_target_token: str | None = None,
        pointer_to_restore: dict | None = None,
        ttl_ms: int = 60_000,
        timeout=10,
        cancel=None,
    ) -> str:
        if (
            self._stopping.is_set()
            or self.session is None
            or self.epoch is None
            or self._peer is None
        ):
            raise closed()
        self._ensure_mux()
        budget = Budget(timeout, Cancellation(cancel, self._stopping))
        budget.acquire(self._confirm_lock)
        try:
            self._drain_orphaned_control(("__confirm_challenge__", "__confirm_register__"), budget)
            challenge = self._exchange_frame(
                "__confirm_challenge__",
                {"kind": "agent.confirmation.challenge.request"},
                budget=budget,
                is_control=True,
            )
            if type(challenge) is not dict:
                err = rejected()
                self._abort(err)
                raise err
            if challenge.get("kind") == "agent.confirmation.error":
                parsed_err = self._validate_confirmation_error(challenge)
                if parsed_err is None:
                    err = rejected()
                    self._abort(err)
                    raise err
                raise parsed_err
            if (
                challenge.keys() != {"kind", "version", "agentEpoch", "nonce"}
                or challenge["kind"] != "agent.confirmation.challenge"
                or type(challenge["version"]) is not int
                or challenge["version"] != 1
                or challenge["agentEpoch"] != self.epoch
            ):
                err = rejected()
                self._abort(err)
                raise err
            try:
                nonce = _bytes(challenge["nonce"], 32)
            except BaseException as error:
                self._abort(error)
                raise
            claim = self.authority.confirmation_claim(
                self.local,
                self._peer,
                self._owner_identity,
                self.workspace_id,
                nonce,
                lease_id=lease_id,
                pause_id=pause_id,
                generation=generation,
                checkpoint_hash=checkpoint_hash,
                resume_plan_hash=resume_plan_hash,
                restore_target_token=restore_target_token,
                pointer_to_restore=pointer_to_restore,
                ttl_ms=ttl_ms,
                budget=budget,
            )
            sig_req = self.authority.submit_signature(claim, budget)
            try:
                signature = _signature_result(sig_req, budget)
            finally:
                sig_req.close()
            reply = self._exchange_frame(
                "__confirm_register__",
                {
                    "kind": "agent.confirmation.register",
                    "version": 1,
                    "claim": claim.decode("utf-8"),
                    "signature": list(signature),
                },
                budget=budget,
                is_control=True,
            )
            if type(reply) is not dict:
                err = rejected()
                self._abort(err)
                raise err
            kind = reply.get("kind")
            if kind == "agent.confirmation.registered":
                grant = reply.get("confirmationGrant")
                exp_ms = reply.get("expiresInMs")
                if (
                    reply.keys() != {"kind", "confirmationGrant", "expiresInMs"}
                    or not _is_hex(grant)
                    or type(exp_ms) is not int
                    or exp_ms < 1
                ):
                    err = rejected()
                    self._abort(err)
                    raise err
                return grant
            if kind == "agent.confirmation.error":
                parsed_err = self._validate_confirmation_error(reply)
                if parsed_err is None:
                    err = rejected()
                    self._abort(err)
                    raise err
                raise parsed_err
            err = rejected()
            self._abort(err)
            raise err
        finally:
            self._confirm_lock.release()

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout=10,
        cancel=None,
        validator=None,
    ):
        if type(method) is not str or not 1 <= len(method) <= 128 or "\x00" in method:
            raise ValueError("Agent 方法名无效。")
        payload = {} if params is None else params
        if type(payload) is not dict:
            raise TypeError("Agent 请求参数必须为对象。")
        if self._stopping.is_set() or self.session is None:
            raise closed()
        budget = Budget(timeout, Cancellation(cancel, self._stopping))
        request_id = secrets.token_hex(16)
        deadline_ms = self._deadline_ms(budget)
        request = {
            "protocolVersion": "1.0",
            "kind": "request",
            "id": request_id,
            "session": self.session,
            "workspace": self.workspace_id,
            "method": method,
            "deadlineMs": deadline_ms,
            "params": payload,
        }
        try:
            dumps(request, max_bytes=_MAX_CONTROL_FRAME, checkpoint=budget.check_time)
        except (TypeError, ValueError) as error:
            raise DeskWeaveError("INVALID_ARGUMENT", "Agent 请求参数无法编码。") from error

        response = self._exchange_frame(
            request_id,
            request,
            budget=budget,
            refresh_deadline=True,
        )
        try:
            if (
                type(response) is not dict
                or response.get("protocolVersion") != "1.0"
                or response.get("kind") != "response"
                or response.get("replyTo") != request_id
                or type(response.get("ok")) is not bool
            ):
                raise rejected()
            if response["ok"]:
                if response.keys() != {"protocolVersion", "kind", "replyTo", "ok", "result"}:
                    raise rejected()
                result = response["result"]
                if validator is not None:
                    try:
                        return validator(result, checkpoint=budget.check_time)
                    except DeskWeaveError as error:
                        if error.code != "INVALID_ARGUMENT":
                            raise
                        raise DeskWeaveError(
                            "INTERNAL_ERROR", "Desktop Agent 返回的平台数据不符合契约。"
                        ) from error
                return result
            if response.keys() != {"protocolVersion", "kind", "replyTo", "ok", "error"}:
                raise rejected()
            body = response["error"]
            if (
                type(body) is not dict
                or not {"code", "message", "retry"}
                <= body.keys()
                <= {"code", "message", "retry", "details"}
                or type(body["code"]) is not str
                or not 1 <= len(body["code"]) <= 80
                or type(body["message"]) is not str
                or not 1 <= len(body["message"]) <= 1000
                or body["retry"] not in {"never", "sameRequest", "afterQuery"}
            ):
                raise rejected()
            if "details" in body:
                if type(body["details"]) is not dict:
                    raise rejected()
                details = body["details"]
            else:
                details = None
        except DeskWeaveError as error:
            if error.code in {"AUTHENTICATION_FAILED", "INTERNAL_ERROR"}:
                self._abort(error)
            raise
        except BaseException as error:
            self._abort(error)
            raise
        raise DeskWeaveError(body["code"], body["message"], retry=body["retry"], details=details)

    def desktop_capabilities(self, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "desktop.capabilities",
            {},
            timeout=timeout,
            cancel=cancel,
            validator=validate_desktop_capabilities,
        )

    def desktop_topology(self, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "desktop.topology",
            {},
            timeout=timeout,
            cancel=cancel,
            validator=validate_desktop_topology,
        )

    def target_bind(self, selection_grant: str, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "target.bind",
            {"selectionGrant": selection_grant},
            timeout=timeout,
            cancel=cancel,
            validator=validate_target_bind_result,
        )

    def lease_acquire(
        self,
        run_id: str,
        targets: list[str],
        *,
        wait_ms: int = 0,
        interrupt_policy: str | None = None,
        timeout=10,
        cancel=None,
    ) -> dict:
        params: dict[str, object] = {
            "runId": run_id,
            "targets": list(targets) if isinstance(targets, (list, tuple)) else targets,
            "waitMs": wait_ms,
        }
        if interrupt_policy is not None:
            params["interruptPolicy"] = interrupt_policy
        return self.call(
            "lease.acquire",
            params,
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_acquire_result,
        )

    def lease_renew(self, lease_id: str, generation: int, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "lease.renew",
            {"leaseId": lease_id, "generation": generation},
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_acquire_result,
        )

    def lease_release(
        self,
        lease_id: str,
        generation: int,
        reason: str = "finished",
        *,
        timeout=10,
        cancel=None,
    ) -> dict:
        return self.call(
            "lease.release",
            {"leaseId": lease_id, "generation": generation, "reason": reason},
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_release_result,
        )

    def lease_pause(self, lease_id: str, generation: int, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "lease.pause",
            {"leaseId": lease_id, "generation": generation},
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_snapshot,
        )

    def lease_status(self, lease_id: str, *, timeout=10, cancel=None) -> dict:
        return self.call(
            "lease.status",
            {"leaseId": lease_id},
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_snapshot,
        )

    def lease_resume(
        self,
        lease_id: str,
        generation: int,
        pause_id: str,
        resume_attempt_id: str,
        resume_plan_hash: str,
        *,
        confirmation_grant: str | None = None,
        timeout=10,
        cancel=None,
    ) -> dict:
        params: dict[str, object] = {
            "leaseId": lease_id,
            "generation": generation,
            "pauseId": pause_id,
            "resumeAttemptId": resume_attempt_id,
            "resumePlanHash": resume_plan_hash,
        }
        if confirmation_grant is not None:
            params["confirmationGrant"] = confirmation_grant
        return self.call(
            "lease.resume",
            params,
            timeout=timeout,
            cancel=cancel,
            validator=validate_lease_snapshot,
        )

    def segment_submit(
        self,
        lease_id: str,
        generation: int,
        segment_id: str,
        target_token: str,
        operation: dict,
        *,
        payload_hash: str | None = None,
        trajectory_ref: dict | None = None,
        timeout=10,
        cancel=None,
    ) -> dict:
        computed_hash = (
            payload_hash
            if payload_hash is not None
            else compute_segment_payload_hash(target_token, operation, trajectory_ref)
        )
        params: dict[str, object] = {
            "leaseId": lease_id,
            "generation": generation,
            "segmentId": segment_id,
            "payloadHash": computed_hash,
            "targetToken": target_token,
            "operation": operation,
        }
        if trajectory_ref is not None:
            params["trajectoryRef"] = trajectory_ref
        return self.call(
            "segment.submit",
            params,
            timeout=timeout,
            cancel=cancel,
            validator=validate_segment_result,
        )

    def segment_status(
        self,
        segment_id: str,
        payload_hash: str,
        *,
        timeout=10,
        cancel=None,
    ) -> dict:
        return self.call(
            "segment.status",
            {"segmentId": segment_id, "payloadHash": payload_hash},
            timeout=timeout,
            cancel=cancel,
            validator=validate_segment_result,
        )

    def _close_resource(self, name):
        resource = getattr(self, name)
        if resource is not None:
            resource.close()
            setattr(self, name, None)

    def _reap(self):
        process = self._process
        if process is None:
            return
        if self._published:
            self._process = None
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._process = None

    def _join_reader(self):
        if not hasattr(self, "_mux_lock"):
            return
        with self._mux_lock:
            thread = self._reader_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._mux_lock:
            self._reader_thread = None

    def _wait_idle_sender(self):
        if hasattr(self, "_send_lock"):
            with self._send_lock:
                pass

    def close(self):
        self._stopping.set()
        with self._io:
            if hasattr(self, "_mux_cond"):
                with self._mux_lock:
                    if self._fatal_error is None:
                        self._fatal_error = closed()
                    self._fail_all_waiters_locked(self._fatal_error)
            cleanup(
                self._close_started,
                self._wait_idle_sender,
                lambda: self._close_resource("_runtime_dir"),
            )

    def _close_started(self):
        cleanup(
            lambda: self._close_resource("_signature"),
            lambda: self._close_resource("_registration_dir"),
            lambda: self._close_resource("_channel"),
            lambda: self._close_resource("_listener"),
            self._join_reader,
            self._reap,
            lambda: self._close_resource("_peer"),
            lambda: self._close_resource("_owner"),
        )


def start_agent(
    engine,
    local,
    installation,
    authority,
    *,
    timeout=10,
    cancel=None,
    idle_grace=2.0,
    wayland_backend=None,
):
    check_idle(idle_grace)
    checked_wayland = _check_wayland_backend(wayland_backend)
    budget = Budget(timeout, Cancellation(cancel, engine._desktop_stopping))
    budget.acquire(engine._desktop_lock)
    try:
        current = engine._desktop_agent
        if current is not None:
            if current.running:
                if (
                    current.local is not local
                    or current.installation != installation
                    or current.authority is not authority
                    or current.wayland_backend != checked_wayland
                ):
                    raise rejected()
                current.probe(timeout=remaining(budget), cancel=budget.cancel)
                return current
            current.close()
            engine._desktop_agent = None
        try:
            engine._desktop_agent = AgentProcess(
                local,
                installation,
                authority,
                workspace_id=engine.workspace.id,
                timeout=remaining(budget),
                cancel=budget.cancel,
                idle_grace=idle_grace,
                wayland_backend=checked_wayland,
            )
        except InitializationError as error:
            if type(error.resource) is AgentProcess:
                engine._desktop_agent = error.resource
            raise
        return engine._desktop_agent
    finally:
        engine._desktop_lock.release()
