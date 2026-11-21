from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from deskweave.coordination.directory import Directory, StartupLock, backend, runtime_directory
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from deskweave.process._wire import write_frame
from deskweave.process.control import remaining
from deskweave.transport.native import Installation, NativeListener, ProcessBinding
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
_MAX_FRAME = 4096


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


def _check_owner(runtime, expected):
    runtime.check()
    with runtime.open(_OWNER) as current:
        if backend.file_identity(current) != expected:
            raise DeskWeaveError("RESOURCE_DENIED", "桌面 Agent 所有权对象已改变。")


def _proof(secret, role, engine_pid, agent_pid, epoch):
    identity = struct.pack("<II", engine_pid, agent_pid) + epoch.encode("ascii")
    return hmac.digest(secret, b"deskweave/desktop-agent/v1\0" + role + b"\0" + identity, "sha256")


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

    def registration(self, engine, peer, owner, runtime, nonce, budget):
        registered = self.check_engine(engine, budget)
        peer.check(budget.check_time)
        self._check_namespaces(peer.identity)
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


class AgentProcess(Resource):
    def __init__(
        self,
        local: ProcessBinding,
        installation: Installation,
        authority: AgentAuthority,
        *,
        timeout=10,
        cancel=None,
    ):
        if type(installation) is not Installation or type(authority) is not AgentAuthority:
            raise TypeError("启动 Desktop Agent 需要受信安装清单和启动授权方。")
        self.local = local
        self.installation = installation
        self.authority = authority
        self.epoch = secrets.token_hex(32)
        self._process = self._peer = self._listener = self._channel = None
        self._runtime_dir = self._owner = None
        self._registration_dir = self._signature = self._owner_identity = None
        self._io = threading.RLock()
        self._stopping = threading.Event()
        budget = Budget(timeout, cancel)
        try:
            local_binding(local, budget.check_time)
            self._initialize_resource("_runtime_dir", authority.open_runtime, local, budget)
            with StartupLock(self._runtime_dir, budget, name=_STARTUP):
                try:
                    self._initialize_resource(
                        "_owner", self._runtime_dir.open, _OWNER, write=True, create=True
                    )
                    self._owner_identity = backend.file_identity(self._owner)
                    _check_owner(self._runtime_dir, self._owner_identity)
                    authority.check_owner(local, self._owner, budget)
                    if not backend.acquire(self._owner):
                        raise DeskWeaveError("DESKTOP_BUSY", "当前登录会话已有 Desktop Agent。")
                    backend.release(self._owner)
                    self._launch(budget)
                    _check_owner(self._runtime_dir, self._owner_identity)
                    self._close_resource("_owner")
                    budget.check()
                except BaseException as error:
                    try:
                        self._close_started()
                    except BaseException as closing:
                        raise InitializationError(self, [error, closing]) from None
                    raise
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def _initialize_resource(self, name, factory, *args, **kwargs):
        try:
            setattr(self, name, factory(*args, **kwargs))
        except InitializationError as error:
            setattr(self, name, error.resource)
            raise

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
            write_frame(
                process.stdin,
                {
                    "version": 1,
                    "enginePid": engine.process_id,
                    "agentEpoch": self.epoch,
                    "address": self._listener.address,
                    "secret": list(secret),
                },
            )
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
        if (
            ready.keys() != {"kind", "agentEpoch", "proof"}
            or ready["kind"] not in ("agent.ready", "agent.busy")
            or ready["agentEpoch"] != self.epoch
        ):
            raise rejected()
        role = b"agent" if ready["kind"] == "agent.ready" else b"busy"
        if not hmac.compare_digest(
            _bytes(ready["proof"], 32),
            _proof(secret, role, engine.process_id, process.pid, self.epoch),
        ):
            raise rejected()
        if ready["kind"] == "agent.busy":
            raise DeskWeaveError("DESKTOP_BUSY", "当前登录会话已有 Desktop Agent。")

    def _spawn(self, path, image):
        environment = {
            name: os.environ[name]
            for name in ("SystemRoot", "WINDIR")
            if sys.platform == "win32" and name in os.environ
        }
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
        process = self._process
        return not self._stopping.is_set() and process is not None and process.poll() is None

    def probe(self, *, timeout=10, cancel=None):
        if self._stopping.is_set():
            raise closed()
        budget = Budget(timeout, Cancellation(cancel, self._stopping))
        budget.acquire(self._io)
        try:
            _check_owner(self._runtime_dir, self._owner_identity)
            self._channel.send({"kind": "agent.ping", "agentEpoch": self.epoch}, budget=budget)
            response = self._channel.receive(budget=budget)
            _check_owner(self._runtime_dir, self._owner_identity)
            if response != {"kind": "agent.pong", "agentEpoch": self.epoch}:
                raise rejected()
            return self.epoch
        except BaseException as error:
            self._abort(error)
            raise
        finally:
            self._io.release()

    def _close_resource(self, name):
        resource = getattr(self, name)
        if resource is not None:
            resource.close()
            setattr(self, name, None)

    def _reap(self):
        process = self._process
        if process is None:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._process = None

    def close(self):
        self._stopping.set()
        with self._io:
            cleanup(self._close_started, lambda: self._close_resource("_runtime_dir"))

    def _close_started(self):
        cleanup(
            lambda: self._close_resource("_signature"),
            lambda: self._close_resource("_registration_dir"),
            lambda: self._close_resource("_channel"),
            lambda: self._close_resource("_listener"),
            self._reap,
            lambda: self._close_resource("_peer"),
            lambda: self._close_resource("_owner"),
        )


def start_agent(engine, local, installation, authority, *, timeout=10, cancel=None):
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
                ):
                    raise rejected()
                current.probe(timeout=remaining(budget), cancel=budget.cancel)
                return current
            current.close()
            engine._desktop_agent = None
        try:
            engine._desktop_agent = AgentProcess(
                local, installation, authority, timeout=remaining(budget), cancel=budget.cancel
            )
        except InitializationError as error:
            if type(error.resource) is AgentProcess:
                engine._desktop_agent = error.resource
            raise
        return engine._desktop_agent
    finally:
        engine._desktop_lock.release()
