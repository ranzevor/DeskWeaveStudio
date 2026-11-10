from __future__ import annotations

import os
import queue
import secrets
import subprocess
import threading
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.transport.native import ProcessBinding, connect
from deskweave.transport.native._common import Budget, cleanup

from . import _wire as wire
from .control import ControlSession, remaining
from .launch import ENTRY, PythonLaunch


class EngineProcess(ControlSession):
    def __init__(
        self,
        launch: PythonLaunch,
        local: ProcessBinding,
        workspace: Path,
        *,
        create=False,
        directory=None,
        max_connections=64,
        max_clients=256,
        max_component_states=100000,
        max_manifest_bytes=16777216,
        timeout=30.0,
        cancel=None,
        _coordination=None,
    ):
        if type(launch) is not PythonLaunch or type(local) is not ProcessBinding:
            raise TypeError("执行进程需要受信启动配置与启动器登记。")
        if local.identity.process_id != os.getpid():
            raise ValueError("启动器登记必须属于当前进程。")
        if type(create) is not bool:
            raise ValueError("工作空间创建选项无效。")
        for value, maximum in (
            (max_connections, 64),
            (max_clients, 256),
            (max_component_states, 1000000),
            (max_manifest_bytes, 67108864),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("进程服务容量无效。")
        self.launch, self.local = launch, local
        self._lock = threading.Lock()
        self._initialize_connections()
        self._stopped = threading.Event()
        self._exited = threading.Event()
        self._diagnostic_lock = threading.Lock()
        self._diagnostic = bytearray()
        self._failures = []
        self._sequence = -1
        self._control = None
        self._peer = None
        self._process = None
        self._heartbeat = None
        self._watcher = None
        self._stderr_reader = None
        self._closed = False
        budget = Budget(timeout, cancel)
        launch.verify(budget.check_time)
        local.check(budget.check_time)
        epoch = secrets.token_urlsafe(24)
        startup_threads = []
        ready = queue.Queue(maxsize=1)
        try:
            self._process = subprocess.Popen(
                launch.command(),
                cwd=ENTRY.parent,
                env=launch.environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr, name="deskweave-diagnostics", daemon=True
            )
            self._stderr_reader.start()
            while True:
                budget.check_time()
                if self._process.poll() is not None:
                    raise DeskWeaveError("ENGINE_START_FAILED", "执行服务在就绪前退出。")
                try:
                    self._peer = ProcessBinding(
                        self.pid,
                        launch.installation,
                        instance_epoch=epoch,
                        timeout=remaining(budget),
                        cancel=cancel,
                    )
                    break
                except DeskWeaveError as error:
                    if error.code != "AUTHENTICATION_FAILED":
                        raise
                    threading.Event().wait(budget.check_time())
            configuration = {
                "version": wire.VERSION,
                "workspace": str(Path(workspace).resolve()),
                "create": create,
                "parent": local.identity.as_dict(),
                "engineEpoch": epoch,
                "engineInstallation": wire.manifest_header(launch.installation),
                "parentInstallation": wire.manifest_header(local.installation),
                "directory": str(Path(directory).resolve(strict=True)) if directory else None,
                "maxConnections": max_connections,
                "maxClients": max_clients,
                "maxComponentStates": max_component_states,
                "maxManifestBytes": max_manifest_bytes,
            }

            if _coordination is not None:
                from deskweave.coordination.startup import header, write_plan

                configuration["coordination"] = header(
                    _coordination.plan,
                    _coordination.identity,
                    _coordination.runtime,
                    _coordination.idle_grace,
                    budget,
                )

            def send():
                try:
                    wire.write_frame(
                        self._process.stdin,
                        {"importPaths": [str(path) for path in launch.import_paths]},
                    )
                    wire.write_frame(self._process.stdin, configuration)
                    for installation in (launch.installation, local.installation):
                        for chunk in wire.chunks(installation.components):
                            budget.check_time()
                            wire.write_frame(self._process.stdin, chunk)
                    if _coordination is not None:
                        write_plan(
                            self._process.stdin,
                            _coordination.plan,
                            (launch.installation, local.installation),
                            budget,
                        )
                except BaseException as error:
                    with self._diagnostic_lock:
                        self._startup_write_error = error
                finally:
                    self._process.stdin.close()

            def receive():
                try:
                    ready.put((True, wire.read_frame(self._process.stdout)))
                except BaseException as error:
                    ready.put((False, error))
                finally:
                    self._process.stdout.close()

            for name, function in (("bootstrap-write", send), ("bootstrap-read", receive)):
                thread = threading.Thread(target=function, name=f"deskweave-{name}", daemon=True)
                thread.start()
                startup_threads.append(thread)
            while True:
                try:
                    succeeded, value = ready.get(timeout=budget.check_time())
                    break
                except queue.Empty:
                    continue
            if not succeeded:
                raise DeskWeaveError(
                    "ENGINE_START_FAILED", "执行服务在启动交换完成前退出。"
                ) from value
            if value.get("ok") is False:
                self._remote_error(value["error"])
            wire.fields(value, ("ok", "version", "peer", "control", "address", "workspace"))
            if value["ok"] is not True or value["version"] != wire.VERSION:
                raise DeskWeaveError("PROTOCOL_UNSUPPORTED", "执行服务启动协议不兼容。")
            if wire.peer_value(value["peer"]) != self._peer.identity:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "执行服务就绪身份不匹配。")
            self.workspace, self.address = value["workspace"], value["address"]
            self._control = connect(
                value["control"], local, self._peer, timeout=remaining(budget), cancel=cancel
            )
            response = self._exchange("hello", {"version": wire.VERSION}, budget)
            if _coordination is None:
                if response != {"workspace": self.workspace}:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "执行服务工作空间不匹配。")
            else:
                wire.fields(response, ("workspace", "control", "authority"))
                if (
                    response["workspace"] != self.workspace
                    or response["authority"] != _coordination.plan.authority
                ):
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "执行服务工作空间不匹配。")
                self.host_address = response["control"]
            for thread in startup_threads:
                thread.join(remaining(budget))
                if thread.is_alive():
                    raise DeskWeaveError("DEADLINE_EXCEEDED", "启动通道尚未关闭。")
            self._heartbeat = threading.Thread(
                target=self._keep_alive, name="deskweave-control", daemon=True
            )
            self._watcher = threading.Thread(
                target=self._reap, name="deskweave-process", daemon=True
            )
            self._heartbeat.start()
            self._watcher.start()
        except BaseException as error:
            self._stopped.set()
            actions = []
            if self._control is not None:
                actions.append(self._control.close)
            if self._process is not None:
                if self._process.poll() is None:
                    self._process.kill()
                self._process.wait(timeout=10)
                for thread in startup_threads:
                    thread.join(10)
                if self._stderr_reader is not None and self._stderr_reader.ident is not None:
                    self._stderr_reader.join(10)
                for thread in (self._heartbeat, self._watcher):
                    if thread is not None and thread.ident is not None:
                        thread.join(10)
                actions.extend(
                    stream.close
                    for stream in (self._process.stdin, self._process.stdout, self._process.stderr)
                    if stream is not None
                )
            if self._peer is not None:
                actions.append(self._peer.close)
            try:
                cleanup(*actions)
            except BaseException as closing:
                raise BaseExceptionGroup("执行服务启动与清理失败。", [error, closing]) from None
            raise

    @property
    def pid(self):
        return self._process.pid

    @property
    def peer(self):
        return self._peer.identity

    @property
    def diagnostics(self):
        with self._diagnostic_lock:
            return bytes(self._diagnostic).decode("utf-8", errors="replace")

    def _read_stderr(self):
        try:
            while chunk := self._process.stderr.read(8192):
                with self._diagnostic_lock:
                    self._diagnostic.extend(chunk)
                    if len(self._diagnostic) > 65536:
                        del self._diagnostic[:-65536]
        except (OSError, ValueError):
            pass

    def _reap(self):
        try:
            self._process.wait()
            cleanup(
                self.close,
                self._heartbeat.join,
                self._stderr_reader.join,
                self._process.stderr.close,
                self._peer.close,
            )
        except BaseException as error:
            self._failures.append(error)
        finally:
            self._exited.set()

    def poll(self):
        return self._process.poll()

    def wait(self, timeout=30.0):
        budget = Budget(timeout)
        while not self._exited.wait(budget.check_time()):
            pass
        if self._failures:
            raise BaseExceptionGroup("执行进程控制或回收失败。", list(self._failures))
        return self._process.returncode
