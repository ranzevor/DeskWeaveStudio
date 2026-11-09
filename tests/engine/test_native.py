from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import deskweave
from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.connection import ClientConnection
from deskweave.transport.native import (
    Installation,
    InstalledFile,
    NativeClient,
    NativeListener,
    ProcessBinding,
    connect,
    current_executable,
    serve_connection,
)
from deskweave.transport.native._common import Budget
from deskweave.transport.protocol import ConnectionPolicy, ConnectionTicket


def installed(path):
    path = Path(path).resolve(strict=True)
    return InstalledFile(path, hashlib.sha256(path.read_bytes()).hexdigest())


def installation():
    executable = installed(current_executable())
    components = [
        installed(path)
        for path in Path(deskweave.__file__).parent.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json"}
    ]
    components.append(installed(Path(__file__).with_name("native_helper.py")))
    requirement = None
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/codesign", "-d", "-r-", str(executable.path)],
            text=True,
            capture_output=True,
            check=True,
        )
        requirement = (result.stdout + result.stderr).split("designated => ", 1)[1].splitlines()[0]
    return Installation(executable, tuple(components), requirement)


class NativeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installation = installation()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.local = self.stack.enter_context(
            ProcessBinding(os.getpid(), self.installation, instance_epoch="client-test")
        )
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="dw-test-"))

    def pair(self):
        listener = self.stack.enter_context(NativeListener(self.local, directory=self.directory))
        client = self.stack.enter_context(connect(listener.address, self.local, self.local))
        server = self.stack.enter_context(listener.accept({os.getpid(): self.local}))
        return client, server, listener

    def worker(self, function):
        results = queue.Queue()

        def run():
            try:
                results.put((True, function()))
            except BaseException as error:
                results.put((False, error))

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(lambda: thread.join(5))
        return results, thread

    def error(self, code, function):
        with self.assertRaises(DeskWeaveError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_current_executable_is_independent_of_login_session(self):
        from deskweave.transport.native import identity

        platform = identity.platform
        if sys.platform == "linux":
            target, attribute = platform.Process, "_session"
        else:
            target = platform
            attribute = "token_identity" if sys.platform == "win32" else "local_session"
        with patch.object(
            target, attribute, side_effect=DeskWeaveError("AUTHENTICATION_FAILED", "session")
        ):
            self.assertEqual(current_executable(), self.installation.executable.path)
            self.error(
                "AUTHENTICATION_FAILED",
                lambda: ProcessBinding(os.getpid(), self.installation, instance_epoch="session"),
            )

    def test_kernel_peer_and_unicode_frame(self):
        client, server, _listener = self.pair()
        self.assertEqual(server.peer.process_id, os.getpid())
        message = {"value": "全显示屏 / workflow", "nested": [True, 0, None]}
        client.send(message)
        self.assertEqual(server.receive(), message)
        server.send(message)
        self.assertEqual(client.receive(), message)

    def test_connection_failures_close_read_and_write_channels(self):
        for operation in ("read", "write"):
            for failure in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                with self.subTest(operation=operation, failure=failure):
                    client, server, _listener = self.pair()
                    with patch.object(client._raw, operation, side_effect=failure("peer closed")):
                        self.error(
                            "TRANSPORT_CLOSED",
                            client.receive
                            if operation == "read"
                            else lambda client=client: client.send({}),
                        )
                    self.error("TRANSPORT_CLOSED", lambda client=client: client.send({}))
                    self.error("TRANSPORT_CLOSED", server.receive)

    @unittest.skipUnless(sys.platform == "linux", "Linux process snapshots")
    def test_process_exit_during_snapshot_rejects_identity(self):
        from deskweave.transport.native import _linux

        with _linux.Process(os.getpid()) as process:
            for failure in (FileNotFoundError, ProcessLookupError):
                with (
                    self.subTest(failure=failure),
                    patch.object(Path, "resolve", side_effect=failure("process exited")),
                ):
                    self.error("AUTHENTICATION_FAILED", process.snapshot)
            self.assertEqual(process.snapshot(), process.initial)

    @unittest.skipUnless(sys.platform == "win32", "Windows process snapshots")
    def test_process_exit_during_windows_snapshot_rejects_identity(self):
        import ctypes

        from deskweave.transport.native import _windows

        with _windows.Process(os.getpid()) as process:
            with patch.object(_windows, "process_executable", side_effect=ctypes.WinError(5)):
                self.error("AUTHENTICATION_FAILED", process.snapshot)
            with patch.object(_windows, "WaitForSingleObject", side_effect=(258, 0)):
                self.error("AUTHENTICATION_FAILED", process.snapshot)
            with patch.object(_windows, "WaitForSingleObject", return_value=0xFFFFFFFF):
                ctypes.set_last_error(6)
                with self.assertRaises(OSError) as caught:
                    process.snapshot()
                self.assertEqual(caught.exception.winerror, 6)
            with patch.object(_windows, "process_executable", side_effect=ctypes.WinError(8)):
                with self.assertRaises(OSError) as caught:
                    process.snapshot()
                self.assertEqual(caught.exception.winerror, 8)
            self.assertEqual(process.snapshot(), process.initial)

    def test_multiple_connections_keep_listener_owned(self):
        first, server, listener = self.pair()
        with connect(listener.address, self.local, self.local) as second:
            with listener.accept({os.getpid(): self.local}) as other:
                first.send({"client": 1})
                second.send({"client": 2})
                self.assertEqual(server.receive(), {"client": 1})
                self.assertEqual(other.receive(), {"client": 2})

    def test_unregistered_peer_rejected_before_frame(self):
        listener = self.stack.enter_context(NativeListener(self.local, directory=self.directory))
        with connect(listener.address, self.local, self.local):
            self.error("AUTHENTICATION_FAILED", lambda: listener.accept({}))

    def test_installation_mismatch_and_replacement(self):
        wrong = replace(
            self.installation, executable=replace(self.installation.executable, sha256="0" * 64)
        )
        self.error(
            "AUTHENTICATION_FAILED",
            lambda: ProcessBinding(os.getpid(), wrong, instance_epoch="bad"),
        )
        source = Path(self.directory) / "component.py"
        source.write_text("one", encoding="utf-8")
        selected = replace(self.installation, components=(installed(source),))
        with ProcessBinding(os.getpid(), selected, instance_epoch="test") as binding:
            source.write_text("two", encoding="utf-8")
            self.error("AUTHENTICATION_FAILED", binding.check)

    def test_installation_same_size_rewrite_with_preserved_mtime(self):
        source = Path(self.directory) / "component.py"
        source.write_bytes(b"one")
        selected = replace(self.installation, components=(installed(source),))
        with ProcessBinding(os.getpid(), selected, instance_epoch="test") as binding:
            state = source.stat()
            source.write_bytes(b"two")
            os.utime(source, ns=(state.st_atime_ns, state.st_mtime_ns))
            self.assertEqual(source.stat().st_size, state.st_size)
            self.assertEqual(source.stat().st_mtime_ns, state.st_mtime_ns)
            self.error("AUTHENTICATION_FAILED", binding.check)

    def test_large_installation_manifest_digest_is_not_a_frame(self):
        import rfc8785

        root = Path(self.directory) / "manifest"
        root.mkdir()
        digest = hashlib.sha256(b"").hexdigest()
        entries = []
        for index in range(10000):
            path = root / f"component-{index:05d}.py"
            path.touch()
            entries.append(InstalledFile(path, digest))
        selected = replace(self.installation, components=tuple(entries))
        value = {
            "components": sorted([str(item.path), item.sha256] for item in entries),
            "executable": [str(selected.executable.path), selected.executable.sha256],
            "macosRequirement": selected.macos_requirement,
        }
        encoded = rfc8785.dumps(value)
        self.assertGreater(len(encoded), MAX_FRAME)
        self.assertEqual(selected.digest, hashlib.sha256(encoded).hexdigest())

    def test_complete_frame_before_peer_close(self):
        client, server, _ = self.pair()
        message = {"value": "final frame"}
        client.send(message)
        client.close()
        self.assertEqual(server.receive(), message)
        self.error("TRANSPORT_CLOSED", server.receive)

    def test_partial_frame_and_eof(self):
        client, server, _ = self.pair()
        client._raw.write(struct.pack("<I", 10) + b"{}", Budget(2))
        client.close()
        self.error("TRANSPORT_TRUNCATED", server.receive)

    def test_partial_header_and_clean_eof(self):
        for data, code in ((b"\x01", "TRANSPORT_TRUNCATED"), (b"", "TRANSPORT_CLOSED")):
            with self.subTest(data=data):
                client, server, _ = self.pair()
                if data:
                    client._raw.write(data, Budget(2))
                client.close()
                self.error(code, server.receive)

    def test_oversize_prefix_rejected_without_payload(self):
        client, server, _ = self.pair()
        client._raw.write(struct.pack("<I", MAX_FRAME + 1), Budget(2))
        self.error("LIMIT_EXCEEDED", server.receive)

    def test_negotiated_frame_budget(self):
        client, server, _ = self.pair()
        server.max_frame_bytes = 1024
        client._raw.write(struct.pack("<I", 1025), Budget(2))
        self.error("LIMIT_EXCEEDED", server.receive)

    def test_timeout_covers_fragmented_frame(self):
        client, server, _ = self.pair()
        client._raw.write(struct.pack("<I", 20) + b"{", Budget(2))
        self.error("DEADLINE_EXCEEDED", lambda: server.receive(timeout=0.1))
        self.error("TRANSPORT_CLOSED", server.receive)

    def test_cancel_pending_read_and_concurrent_close(self):
        client, server, _ = self.pair()
        cancelled = threading.Event()
        entered = threading.Event()
        original = server._raw.read

        def read(size, budget):
            entered.set()
            return original(size, budget)

        with patch.object(server._raw, "read", read):
            results, worker = self.worker(lambda: server.receive(timeout=5, cancel=cancelled))
            self.assertTrue(entered.wait(3))
            cancelled.set()
            ok, error = results.get(timeout=3)
            self.assertFalse(ok)
            self.assertIsInstance(error, DeskWeaveError)
            self.assertEqual(error.code, "CANCELLED")
            worker.join(3)
            self.assertFalse(worker.is_alive())
        server.close()
        server.close()
        client.close()

    def test_close_wakes_pending_read(self):
        _client, server, _ = self.pair()
        entered = threading.Event()
        original = server._raw.read

        def read(size, budget):
            entered.set()
            return original(size, budget)

        with patch.object(server._raw, "read", read):
            results, worker = self.worker(lambda: server.receive(timeout=5))
            self.assertTrue(entered.wait(3))
            server.close()
            ok, _error = results.get(timeout=3)
            self.assertFalse(ok)
            worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_backpressure_write_has_deadline(self):
        client, _server, _ = self.pair()
        self.error(
            "DEADLINE_EXCEEDED", lambda: client.send({"data": "x" * (MAX_FRAME - 100)}, timeout=0.1)
        )

    def test_accept_cancel_and_reconnect(self):
        listener = self.stack.enter_context(NativeListener(self.local, directory=self.directory))
        cancel = threading.Event()
        cancel.set()
        self.error("CANCELLED", lambda: listener.accept({}, cancel=cancel))
        with connect(listener.address, self.local, self.local):
            with listener.accept({os.getpid(): self.local}):
                pass

    def test_deadline_includes_frame_lock_wait(self):
        client, _server, _ = self.pair()
        client._write_lock.acquire()
        try:
            self.error("DEADLINE_EXCEEDED", lambda: client.send({}, timeout=0.1))
        finally:
            client._write_lock.release()

    def authority(self, *, max_frame_bytes=MAX_FRAME):
        engine = Engine(Workspace(Path(self.directory) / "direct-workspace", create=True))
        self.stack.callback(engine.close)
        return self.stack.enter_context(
            ConnectionAuthority(engine, self.local.identity, max_frame_bytes=max_frame_bytes)
        )

    def test_slow_identity_checks_allow_checkpoint_progress(self):
        _client, server, _ = self.pair()
        authority = self.authority()
        now = [100.0]
        verified = []
        cancelled = threading.Event()

        def check(checkpoint):
            now[0] += 0.25
            checkpoint()
            verified.append(now[0])

        def accept(_peer, *, check_peer, close_transport):
            for _ in range(100):
                check_peer()
            self.assertEqual(len(verified), 1)
            now[0] = verified[-1] + 0.05
            check_peer()
            self.assertEqual(len(verified), 1)
            now[0] = verified[-1] + 0.15
            check_peer()
            self.assertEqual(len(verified), 2)
            cancelled.set()
            check_peer()

        with (
            patch("time.monotonic", lambda: now[0]),
            patch.object(server, "check", check),
            patch.object(authority, "accept", accept),
        ):
            self.error("CANCELLED", lambda: serve_connection(server, authority, cancel=cancelled))
        self.error("TRANSPORT_CLOSED", server.check)

    def test_service_initial_frame_limit(self):
        client, server, _ = self.pair()
        authority = self.authority(max_frame_bytes=1024)
        client._raw.write(struct.pack("<I", 1025), Budget(2))
        self.error("LIMIT_EXCEEDED", lambda: serve_connection(server, authority))
        self.assertEqual(authority._connections, {})

    def test_maintenance_start_failure_closes_connection(self):
        _client, server, _ = self.pair()
        authority = self.authority()
        with patch("threading.Thread.start", side_effect=RuntimeError("thread capacity")):
            with self.assertRaisesRegex(RuntimeError, "thread capacity"):
                serve_connection(server, authority)
        self.assertEqual(authority._connections, {})
        self.assertEqual(authority._closing, set())
        self.error("TRANSPORT_CLOSED", server.check)

    def test_client_constructor_failure_closes_channel(self):
        client, _server, _ = self.pair()
        authority = self.authority()
        credential = authority.authorize(ConnectionPolicy({"workflow.read"}), lifetime=30)
        ticket = authority.issue(credential, self.local.identity)
        ticket = replace(ticket, server_peer=replace(ticket.server_peer, instance_epoch="changed"))
        self.error("AUTHENTICATION_FAILED", lambda: NativeClient(client, ticket))
        self.error("TRANSPORT_CLOSED", client.check)

    def test_frame_error_preserves_cleanup_error(self):
        _client, server, _ = self.pair()
        with patch.object(
            server._raw, "read", side_effect=DeskWeaveError("TRANSPORT_TRUNCATED", "partial")
        ):
            with patch.object(server._raw, "close", side_effect=OSError("cleanup")):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    server.receive()
        self.assertEqual(caught.exception.exceptions[0].code, "TRANSPORT_TRUNCATED")
        self.assertIsInstance(caught.exception.exceptions[1], OSError)

    def test_installation_hash_observes_cancellation(self):
        from deskweave.transport.native.identity import digest_file

        cancel = threading.Event()
        cancel.set()
        budget = Budget(2, cancel)
        self.error(
            "CANCELLED", lambda: digest_file(self.installation.executable.path, budget.check_time)
        )

    @unittest.skipIf(sys.platform == "win32", "Unix socket 清理")
    def test_unix_unlink_failure_can_retry_close(self):
        listener = self.stack.enter_context(NativeListener(self.local, directory=self.directory))
        path = Path(listener.address)
        with patch("os.unlink", side_effect=PermissionError("temporary")):
            with self.assertRaises(PermissionError):
                listener.close()
        listener.close()
        self.assertFalse(path.parent.exists())

    def memory_client_input(self):
        authority = self.authority()
        credential = authority.authorize(ConnectionPolicy({"workflow.read"}), lifetime=30)
        peer = self.local.identity
        ticket = authority.issue(credential, peer)
        server = self.stack.enter_context(authority.accept(peer))

        class Channel:
            local = peer
            max_frame_bytes = MAX_FRAME
            is_closed = False
            request = None

            def send(self, value, **_options):
                self.request = value
                self.pending = server.receive(value)

            def receive(self, **_options):
                return self.pending

            def close(self):
                self.is_closed = True
                server.close()

        channel = Channel()
        channel.peer = peer
        return channel, ticket

    def test_client_request_budget_subtracts_queue_time(self):
        channel, ticket = self.memory_client_input()
        now = [100.0]
        with patch("time.monotonic", lambda: now[0]):
            with NativeClient(channel, ticket) as client:

                class QueueLock:
                    def acquire(self, *, timeout):
                        now[0] += 9.0
                        return True

                    def release(self):
                        pass

                client._lock = QueueLock()
                client.call(
                    "request.status", {"requestId": "original"}, request_id="query", timeout=10
                )
                self.assertEqual(channel.request["deadlineMs"], 1000)

    def test_client_ready_validation_observes_deadline(self):
        channel, ticket = self.memory_client_input()
        now = [100.0]
        original = ClientConnection.receive

        def receive(connection, value):
            result = original(connection, value)
            if value["kind"] == "connection.ready":
                now[0] += 2
            return result

        with (
            patch("time.monotonic", lambda: now[0]),
            patch.object(ClientConnection, "receive", receive),
        ):
            self.error("DEADLINE_EXCEEDED", lambda: NativeClient(channel, ticket, timeout=1))
        self.assertTrue(channel.is_closed)

    def test_client_response_validation_observes_deadline(self):
        channel, ticket = self.memory_client_input()
        now = [100.0]
        with patch("time.monotonic", lambda: now[0]):
            with NativeClient(channel, ticket) as client:
                original = client.connection.response

                def response(value, *, request):
                    result = original(value, request=request)
                    now[0] += 2
                    return result

                with patch.object(client.connection, "response", response):
                    self.error(
                        "DEADLINE_EXCEEDED",
                        lambda: client.call(
                            "request.status",
                            {"requestId": "original"},
                            request_id="query",
                            timeout=1,
                        ),
                    )
        self.assertTrue(channel.is_closed)

    def start_engine(self):
        bootstrap = (
            "import sys,json,runpy; sys.path=json.loads(sys.stdin.readline()); "
            "runpy.run_module('tests.engine.native_helper',run_name='__main__')"
        )
        process = subprocess.Popen(
            [sys._base_executable, "-X", "utf8", "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            close_fds=True,
        )

        reader = None

        def close():
            if process.poll() is None:
                try:
                    process.stdin.write('{"action":"stop"}\n')
                    process.stdin.flush()
                    process.wait(timeout=10)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
            if reader is not None:
                reader.join(5)
            stderr = process.stderr.read()
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            self.assertEqual(process.returncode, 0, stderr)

        self.addCleanup(close)
        config = {
            "executable": [
                str(self.installation.executable.path),
                self.installation.executable.sha256,
            ],
            "components": [[str(item.path), item.sha256] for item in self.installation.components],
            "requirement": self.installation.macos_requirement,
            "parent": os.getpid(),
            "workspace": str(Path(self.directory) / "workspace"),
            "directory": self.directory,
        }
        process.stdin.write(json.dumps(sys.path) + "\n" + json.dumps(config) + "\n")
        process.stdin.flush()
        messages = queue.Queue()

        def read():
            for line in process.stdout:
                messages.put(json.loads(line))
            messages.put(None)

        reader = threading.Thread(target=read)
        reader.start()
        ready = messages.get(timeout=15)
        self.assertIsNotNone(ready)
        self.assertEqual(ready["pid"], process.pid)
        peer = self.stack.enter_context(
            ProcessBinding(process.pid, self.installation, instance_epoch="engine-test")
        )
        return process, messages, ready["address"], peer

    def send_control(self, process, action):
        process.stdin.write(json.dumps({"action": action}) + "\n")
        process.stdin.flush()

    def open_engine(self, process, messages, address, peer):
        self.send_control(process, "accept")
        channel = self.stack.enter_context(connect(address, self.local, peer, timeout=10))
        data = messages.get(timeout=10)
        self.assertIsNotNone(data)
        ticket = ConnectionTicket(
            data["ticket"],
            base64.b64decode(data["secret"]),
            data["client"],
            data["version"],
            data["workspace"],
            self.local.identity,
            peer.identity,
        )
        return self.stack.enter_context(NativeClient(channel, ticket))

    def test_real_process_save_reconnect_status(self):
        engine = self.start_engine()
        client = self.open_engine(*engine)
        definition = json.loads(
            (Path(__file__).parents[2] / "examples/workflows/hello.json").read_text(
                encoding="utf-8"
            )
        )
        saved = client.call(
            "workflow.save",
            {
                "workflowId": definition["id"],
                "draftVersion": 1,
                "baseRevision": None,
                "definition": definition,
                "dependencies": {},
            },
            request_id="native-save",
        )
        self.assertTrue(saved["ok"], saved)
        client.close()
        recovered_client = self.open_engine(*engine)
        recovered = recovered_client.call(
            "request.status", {"requestId": "native-save"}, request_id="native-status"
        )
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["result"]["result"], saved["result"])
        recovered_client.close()
        self.send_control(engine[0], "join")
        result = engine[1].get(timeout=10)
        self.assertEqual(result["active"], 0)
        self.assertNotIn("ExceptionGroup", result["errors"])

    def test_fake_server_pid_rejected(self):
        engine = self.start_engine()
        listener = self.stack.enter_context(NativeListener(self.local, directory=self.directory))
        self.error(
            "AUTHENTICATION_FAILED", lambda: connect(listener.address, self.local, engine[3])
        )

    def test_revoke_closes_native_io(self):
        engine = self.start_engine()
        client = self.open_engine(*engine)
        self.send_control(engine[0], "revoke")
        self.assertEqual(engine[1].get(timeout=10), {"revoked": True})
        self.error("TRANSPORT_CLOSED", lambda: client.channel.receive(timeout=3))
        self.send_control(engine[0], "join")
        self.assertEqual(engine[1].get(timeout=10)["active"], 0)

    def test_expiry_reclaims_blocked_native_read(self):
        engine = self.start_engine()
        client = self.open_engine(*engine)
        self.send_control(engine[0], "expire")
        self.assertEqual(engine[1].get(timeout=10), {"expired": True})
        self.error("TRANSPORT_CLOSED", lambda: client.channel.receive(timeout=3))
        self.send_control(engine[0], "join")
        self.assertEqual(engine[1].get(timeout=10)["active"], 0)

    def test_exited_process_binding_rejected(self):
        engine = self.start_engine()
        self.send_control(engine[0], "stop")
        engine[0].wait(timeout=10)
        self.error("AUTHENTICATION_FAILED", engine[3].check)

    @unittest.skipIf(sys.platform == "win32", "Unix socket 权限")
    def test_unix_endpoint_permissions_and_cleanup(self):
        listener = NativeListener(self.local, directory=self.directory)
        path = Path(listener.address)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        listener.close()
        self.assertFalse(path.parent.exists())


if __name__ == "__main__":
    unittest.main()
