from __future__ import annotations

import base64
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.engine.process import development_launch

from deskweave.adapters.workspace import Workspace
from deskweave.coordination import HostGrant, StartupAuthority, WorkspaceCoordinator
from deskweave.coordination.directory import (
    Directory,
    StartupLock,
    WorkspaceIdentity,
    runtime_directory,
)
from deskweave.coordination.startup import Discovery
from deskweave.errors import DeskWeaveError
from deskweave.process import _wire as wire
from deskweave.process.control import ControlSession
from deskweave.transport.native import NativeListener, ProcessBinding, connect
from deskweave.transport.native._common import Budget
from deskweave.transport.native.identity import platform
from deskweave.transport.protocol import ConnectionPolicy


class DirectoryCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="dw-coord-")))
        process = self.stack.enter_context(platform.Process(os.getpid()))
        self.local = SimpleNamespace(
            process=process,
            identity=SimpleNamespace(user_id=process.initial[0], os_session_id=process.initial[1]),
        )

    def test_atomic_records_and_instance_selected_cleanup(self):
        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        self.assertIsNone(directory.read("entry.json"))
        directory.write("entry.json", b'{"epoch":"first"}')
        directory.write("entry.json", b'{"epoch":"second"}')
        self.assertEqual(directory.read("entry.json"), b'{"epoch":"second"}')
        self.assertFalse(directory.remove("entry.json", matches=lambda value: b"first" in value))
        self.assertTrue(directory.remove("entry.json", matches=lambda value: b"second" in value))
        self.assertIsNone(directory.read("entry.json"))
        self.assertEqual(list(directory.path.iterdir()), [])

    def test_atomic_record_replacement_preserves_open_reader(self):
        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        directory.write("entry.json", b'{"epoch":"first"}')
        with directory.open("entry.json") as reader:
            directory.write("entry.json", b'{"epoch":"second"}')
            self.assertEqual(reader.read(), b'{"epoch":"first"}')
            self.assertEqual(directory.read("entry.json"), b'{"epoch":"second"}')
        self.assertEqual(directory.read("entry.json"), b'{"epoch":"second"}')

    def test_lock_contention_cancellation_and_release(self):
        directory = self.stack.enter_context(Directory(self.root))
        first = self.stack.enter_context(StartupLock(directory, Budget(2)))
        with self.assertRaises(DeskWeaveError) as caught:
            StartupLock(directory, Budget(0.03))
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(DeskWeaveError) as caught:
            StartupLock(directory, Budget(1, cancelled))
        self.assertEqual(caught.exception.code, "CANCELLED")
        first.close()
        with StartupLock(directory, Budget(1)):
            self.assertTrue((self.root / ".engine-start.lock").exists())
        self.assertTrue((self.root / ".engine-start.lock").exists())

    def test_metadata_limits_and_invalid_names(self):
        directory = self.stack.enter_context(Directory(self.root))
        directory.write("large.json", b"a" * 16384)
        self.assertEqual(len(directory.read("large.json")), 16384)
        with self.assertRaises(ValueError):
            directory.write("large.json", b"b" * 16385)
        (self.root / "oversized.json").write_bytes(b"a" * 16385)
        with self.assertRaises(DeskWeaveError) as caught:
            directory.read("oversized.json")
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
        for name in ("../outside", "..", ".", "file:stream", "file/name", "file\\name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                directory.read(name)

    def test_new_workspace_identity_is_bound_to_directory(self):
        selected = self.stack.enter_context(WorkspaceIdentity(self.root / "workspace", create=True))
        alias = self.stack.enter_context(WorkspaceIdentity(selected.path / ".", create=True))
        self.assertEqual(selected.object, alias.object)
        self.assertEqual(selected.key, alias.key)
        self.assertIsNone(selected.workspace)
        with selected.lock(Budget(2)):
            selected.directory.write("workspace.json", b'{"formatVersion":"1.0","id":"created"}')
        self.assertEqual(selected.check("created"), "created")
        with self.assertRaises(DeskWeaveError) as caught:
            selected.check("different")
        self.assertEqual(caught.exception.code, "RESOURCE_DENIED")

    def test_hard_link_metadata_is_rejected(self):
        directory = self.stack.enter_context(Directory(self.root))
        source = self.root / "source.json"
        source.write_bytes(b"original")
        os.link(source, self.root / "entry.json")
        with self.assertRaises(DeskWeaveError) as caught:
            directory.read("entry.json")
        self.assertEqual(caught.exception.code, "RESOURCE_DENIED")
        self.assertEqual(source.read_bytes(), b"original")

    def test_revalidation_retains_failed_close_and_checks_principal(self):
        from unittest.mock import Mock

        from deskweave.coordination.directory import backend

        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        current = Mock(identity=directory.identity)
        current.close.side_effect = OSError("temporary directory close")
        with patch.object(backend, "Directory", return_value=current) as constructor:
            with self.assertRaises(OSError):
                directory.check()
            self.assertIs(constructor.call_args.kwargs["principal"], self.local)
            with self.assertRaises(OSError):
                directory.check()
            self.assertEqual(constructor.call_count, 1)
            self.assertIn(current, directory._failed)
            current.close.side_effect = None
        directory.check()
        self.assertEqual(directory._failed, set())

    @unittest.skipIf(os.name == "nt", "POSIX directory modes")
    def test_runtime_permission_change_is_rejected(self):
        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        directory.path.chmod(0o755)
        try:
            with self.assertRaises(DeskWeaveError) as caught:
                directory.write("entry.json", b"{}")
            self.assertEqual(caught.exception.code, "RESOURCE_DENIED")
            self.assertFalse((directory.path / "entry.json").exists())
        finally:
            directory.path.chmod(0o700)

    def test_failed_temporary_unlink_is_retried_without_file_accumulation(self):
        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        with (
            patch.object(directory._raw, "replace", side_effect=OSError("replace failed")),
            patch.object(directory._raw, "unlink", side_effect=OSError("unlink failed")),
        ):
            with self.assertRaises(BaseExceptionGroup):
                directory.write("entry.json", b"first")
            self.assertEqual(len(directory._pending_removals), 1)
            with self.assertRaises(OSError):
                directory.write("entry.json", b"second")
            self.assertEqual(len(directory._pending_removals), 1)
            self.assertEqual(len(list(directory.path.glob(".tmp-*"))), 1)
        directory.write("entry.json", b"recovered")
        self.assertEqual(directory.read("entry.json"), b"recovered")
        self.assertEqual(directory._pending_removals, {})
        self.assertEqual(list(directory.path.glob(".tmp-*")), [])

    def test_close_retries_temporary_removal_before_directory_handle(self):
        directory = self.stack.enter_context(runtime_directory(self.local, self.root / "runtime"))
        with (
            patch.object(directory._raw, "replace", side_effect=OSError("replace failed")),
            patch.object(directory._raw, "unlink", side_effect=OSError("unlink failed")),
        ):
            with self.assertRaises(BaseExceptionGroup):
                directory.write("entry.json", b"first")
            with self.assertRaises(BaseExceptionGroup):
                directory.close()
            self.assertEqual(len(directory._pending_removals), 1)
            self.assertEqual(len(directory._streams), 1)
        directory.close()
        self.assertEqual(directory._pending_removals, {})
        self.assertEqual(list(directory.path.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX runtime base validation")
    def test_default_runtime_base_close_failure_transfers_resource(self):
        from deskweave.coordination.directory import backend
        from deskweave.transport.native._common import InitializationError

        self.root.chmod(0o700)
        original = Directory.close
        held = []

        def close(directory):
            held.append(directory)
            raise OSError("base directory close failed")

        with (
            patch.object(backend, "runtime_base", return_value=self.root),
            patch.object(Directory, "close", close),
        ):
            with self.assertRaises(InitializationError) as caught:
                runtime_directory(self.local)
        self.assertIs(caught.exception.resource, held[0])
        original(caught.exception.resource)

    @unittest.skipIf(os.name == "nt", "POSIX descriptor ownership")
    def test_close_error_never_retries_reused_descriptor(self):
        import errno

        from deskweave.coordination import _posix

        other = self.root / "other"
        other.write_bytes(b"other")
        for kind in ("file", "directory"):
            for failure in (errno.EINTR, errno.EIO):
                with self.subTest(kind=kind, failure=failure):
                    owned = (
                        _posix.File(os.open(other, os.O_RDONLY))
                        if kind == "file"
                        else _posix.Directory(self.root)
                    )
                    old = owned.fd
                    original = os.close
                    reused = []

                    def close(fd, original=original, reused=reused, failure=failure):
                        original(fd)
                        replacement = os.open(other, os.O_RDONLY)
                        if replacement != fd:
                            os.dup2(replacement, fd)
                            original(replacement)
                        reused.append(fd)
                        raise OSError(failure, "reported after release")

                    try:
                        with patch.object(os, "close", close):
                            with self.assertRaises(OSError):
                                owned.close()
                            self.assertIsNone(owned.fd)
                            owned.close()
                        self.assertEqual(reused, [old])
                        self.assertEqual(os.fstat(old).st_ino, other.stat().st_ino)
                    finally:
                        original(old)


class CoordinationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = Path(__file__).with_name("coordination_helper.py")
        cls.launch = development_launch((cls.helper,))

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="dw-coord-")))
        self.local = self.stack.enter_context(
            ProcessBinding(
                os.getpid(),
                self.launch.installation,
                instance_epoch="coordination-host",
            )
        )
        self.identity = self.stack.enter_context(
            WorkspaceIdentity(self.root / "workspace", create=True)
        )
        self.runtime = self.root / "runtime"
        self.policy = ConnectionPolicy({"workflow.read", "workflow.write"})
        self.authority = StartupAuthority(self.identity, self.launch.installation)
        self.grant = self.authority.register_host(
            self.local, self.policy, lifetime=120, manager=True
        )
        self.coordinator = self.coordinator_for()

    def coordinator_for(self, **options):
        return self.stack.enter_context(
            WorkspaceCoordinator(
                self.launch,
                self.local,
                directory=self.runtime,
                idle_grace=0.3,
                **options,
            )
        )

    def start(self, *, plan=None, coordinator=None, **options):
        session = (coordinator or self.coordinator).open(
            self.identity,
            self.grant,
            plan=plan or self.authority.seal(),
            create=True,
            **options,
        )
        if session.created:
            process = session.control

            def finish():
                session.close()
                try:
                    self.assertEqual(process.wait(15), 0, process.diagnostics)
                finally:
                    if process.poll() is None:
                        process._process.kill()
                        process.wait(10)

            self.stack.callback(finish)
        return session

    def query(self, session):
        with session.connect() as client:
            result = client.call("request.status", {"requestId": "missing"}, request_id="query")
            self.assertTrue(result["ok"], result)

    def error(self, code, operation):
        with self.assertRaises(DeskWeaveError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def helper_host(self, epoch):
        script = (
            "import sys, runpy; sys.path[0:0] = "
            + repr([str(p) for p in self.launch.import_paths])
            + "; runpy.run_path("
            + repr(str(self.helper))
            + ", run_name='__main__')"
        )
        child = subprocess.Popen(
            [str(self.launch.launcher.path), "-I", "-S", "-X", "utf8", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        messages = queue.Queue()

        def read():
            for line in child.stdout:
                messages.put(json.loads(line))
            messages.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def send(value):
            child.stdin.write(json.dumps(value) + "\n")
            child.stdin.flush()

        def receive():
            value = messages.get(timeout=40)
            if value is None:
                self.fail("host exited: " + child.stderr.read())
            return value

        def finish():
            if child.poll() is None:
                send({"action": "stop"})
            try:
                child.wait(15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(10)
            reader.join(5)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

        self.stack.callback(finish)
        send(
            {
                "installation": wire.manifest_header(self.launch.installation),
                "files": [wire.file_value(value) for value in self.launch.installation.components],
                "launcher": wire.file_value(self.launch.launcher),
                "paths": [str(path) for path in self.launch.import_paths],
                "workspace": str(self.identity.path),
                "runtime": str(self.runtime),
                "epoch": epoch,
            }
        )
        identity = wire.peer_value(receive())
        binding = self.stack.enter_context(
            ProcessBinding(
                child.pid,
                self.launch.installation,
                instance_epoch=epoch,
            )
        )
        self.assertEqual(identity, binding.identity)
        return SimpleNamespace(process=child, send=send, receive=receive, binding=binding)

    @staticmethod
    def open_command(grant, plan=None, **options):
        return {
            "action": "open",
            "grant": grant.as_dict(),
            "hosts": [
                {
                    "binding": item.binding.as_dict(),
                    "key": base64.b64encode(item.verification_key).decode("ascii"),
                }
                for item in plan.hosts
            ]
            if plan is not None
            else None,
            **options,
        }

    def stop_instance(self, peer):
        process = None
        try:
            process = platform.Process(peer.process_id)
            if process.initial[2] == peer.process_started:
                os.kill(peer.process_id, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        process.snapshot()
                    except (DeskWeaveError, ProcessLookupError):
                        break
                    time.sleep(0.025)
        except (DeskWeaveError, ProcessLookupError, FileNotFoundError):
            pass
        except OSError as error:
            if getattr(error, "winerror", None) != 87:
                raise
        finally:
            if process is not None:
                process.close()

    def test_save_reconnect_shared_instance_and_idle_release(self):
        first = self.start()
        second = self.coordinator.open(self.identity, self.grant, create=True)
        self.stack.callback(second.close)
        self.assertEqual(first.peer, second.peer)
        self.assertEqual(first.workspace, self.grant.binding.workspace)
        self.error("WORKSPACE_BUSY", lambda: Workspace(self.identity.path))
        definition = json.loads(
            (Path(__file__).parents[2] / "examples/workflows/hello.json").read_text(
                encoding="utf-8"
            )
        )
        with first.connect() as client:
            saved = client.call(
                "workflow.save",
                {
                    "workflowId": definition["id"],
                    "draftVersion": 1,
                    "baseRevision": None,
                    "definition": definition,
                    "dependencies": {},
                },
                request_id="shared-save",
            )
            self.assertTrue(saved["ok"], saved)
        first.close()
        with second.connect() as client:
            restored = client.call(
                "request.status", {"requestId": "shared-save"}, request_id="restore"
            )
            self.assertEqual(restored["result"]["result"], saved["result"])
            second.close()
            self.assertTrue(
                client.call("request.status", {"requestId": "missing"}, request_id="held")["ok"]
            )
        self.assertEqual(first.control.wait(15), 0, first.control.diagnostics)
        with Workspace(self.identity.path) as workspace:
            self.assertEqual(workspace.id, self.grant.binding.workspace)
        self.assertIsNone(Discovery(self.identity, self.coordinator.runtime).read())

    def test_concurrent_hosts_share_winner_after_starter_exits(self):
        first = self.helper_host("first")
        second = self.helper_host("second")
        grants = [
            self.authority.register_host(host.binding, self.policy, lifetime=120, manager=True)
            for host in (first, second)
        ]
        plan = self.authority.seal()
        for host, grant in zip((first, second), grants, strict=True):
            host.send(self.open_command(grant, plan))
        opened = [host.receive() for host in (first, second)]
        self.assertEqual(opened[0]["peer"], opened[1]["peer"])
        self.assertEqual(opened[0]["workspace"], opened[1]["workspace"])
        self.assertEqual(sum(item["created"] for item in opened), 1)
        peer = wire.peer_value(opened[0]["peer"])
        self.stack.callback(self.stop_instance, peer)
        for host in (first, second):
            host.send({"action": "query"})
            self.assertTrue(host.receive()["ok"])
        winner, survivor = (first, second) if opened[0]["created"] else (second, first)
        winner.process.kill()
        winner.process.wait(10)
        survivor.send({"action": "query"})
        self.assertTrue(survivor.receive()["ok"])
        survivor.send({"action": "stop"})
        self.assertEqual(survivor.process.wait(15), 0)

    def test_published_candidate_survives_starter_exit_before_receipt(self):
        host = self.helper_host("publish-exit")
        grant = self.authority.register_host(host.binding, self.policy, lifetime=120, manager=True)
        plan = self.authority.seal()
        host.send(self.open_command(grant, plan, exitAfterPublish=True))
        self.assertEqual(host.process.wait(30), 0)
        record = Discovery(self.identity, self.coordinator.runtime).read()
        self.assertIsNotNone(record)
        self.stack.callback(self.stop_instance, record["peer"])
        session = self.coordinator.open(self.identity, self.grant, plan=plan, create=True)
        self.stack.callback(session.close)
        self.assertEqual(session.peer, record["peer"])
        self.query(session)

    def test_failed_first_publication_retries_same_workspace_identity(self):
        plan = self.authority.seal()

        def fail_publication(_discovery, _workspace, peer, *_args):
            os.kill(peer.process_id, signal.SIGTERM)
            raise OSError("publication failed")

        with patch.object(Discovery, "publish", fail_publication):
            with self.assertRaises(OSError):
                self.coordinator.open(self.identity, self.grant, plan=plan, create=True)
        self.assertEqual(self.identity.read_workspace(), plan.workspace)
        session = self.start(plan=plan)
        self.assertEqual(session.workspace, plan.workspace)
        self.query(session)

    def test_corrupt_discovery_recovers_only_after_writer_releases(self):
        discovery = Discovery(self.identity, self.coordinator.runtime)
        self.coordinator.runtime.write(discovery.name, b"broken")
        session = self.start()
        self.query(session)
        self.coordinator.runtime.write(discovery.name, b"broken")
        self.error(
            "DEADLINE_EXCEEDED",
            lambda: self.coordinator.open(
                self.identity,
                self.grant,
                plan=self.authority.seal(),
                create=True,
                timeout=0.15,
            ),
        )
        self.assertIsNone(session.control.poll())
        self.query(session)
        discovery.publish(
            session.workspace,
            session.peer,
            session.control.host_address,
            session.control.address,
            self.grant.binding.authority,
        )

    def test_cancelled_waiter_preserves_running_instance(self):
        first = self.start()
        cancel = threading.Event()
        cancel.set()
        self.error(
            "CANCELLED",
            lambda: self.coordinator.open(self.identity, self.grant, create=True, cancel=cancel),
        )
        self.query(first)

    def test_close_cancels_pending_open_and_releases_runtime(self):
        first = self.start()
        other = self.coordinator_for()
        entered = threading.Event()
        original = other.attach

        def blocked(record, grant, budget):
            entered.set()
            while not budget.cancel.is_set():
                budget.cancel.wait(0.025)
            return original(record, grant, budget)

        with patch.object(other, "attach", blocked), ThreadPoolExecutor() as pool:
            future = pool.submit(other.open, self.identity, self.grant, create=True)
            self.assertTrue(entered.wait(10))
            other.close()
            self.error("CANCELLED", future.result)
        self.assertEqual(other.pending, 0)
        self.assertTrue(other.runtime._closed)
        self.query(first)

    def test_manager_policy_is_ceiling_for_delegation_and_revision(self):
        authority = StartupAuthority(self.identity, self.launch.installation)
        readonly = ConnectionPolicy({"workflow.read"})
        self.grant = authority.register_host(self.local, readonly, lifetime=60, manager=True)
        session = self.start(plan=authority.seal())
        self.error("ACCESS_DENIED", lambda: session.authorize(self.local, self.policy, lifetime=60))
        credential = session.authorize(self.local, readonly, lifetime=60)
        self.error(
            "ACCESS_DENIED",
            lambda: session.revise(credential, self.policy, expected_version=1, lifetime=60),
        )
        self.error(
            "ACCESS_DENIED", lambda: session.register_host(self.local, self.policy, lifetime=60)
        )
        self.query(session)

    def test_copied_grant_and_same_interpreter_do_not_authorize_other_process(self):
        session = self.start()
        host = self.helper_host("unregistered")
        fake = HostGrant(replace(self.grant.binding, peer=host.binding.identity), self.grant.secret)
        host.send(self.open_command(fake))
        self.assertNotEqual(host.process.wait(40), 0)
        self.query(session)

    def test_invalid_authentication_and_method_leave_service_healthy(self):
        session = self.start()
        record = Discovery(self.identity, self.coordinator.runtime).read()
        with ProcessBinding(
            session.peer.process_id,
            self.launch.installation,
            instance_epoch=session.peer.instance_epoch,
        ) as peer:
            with connect(record["control"], self.local, peer) as channel:
                channel.send(
                    {
                        "kind": "host.hello",
                        "authority": self.grant.binding.authority,
                        "grantId": [],
                        "nonce": "a" * 64,
                    }
                )
                with self.assertRaises(DeskWeaveError):
                    channel.receive()
            with connect(record["control"], self.local, peer) as channel:
                channel.send(
                    {
                        "kind": "host.hello",
                        "authority": self.grant.binding.authority,
                        "grantId": self.grant.binding.grant_id,
                        "nonce": "b" * 64,
                    }
                )
                self.assertEqual(channel.receive()["kind"], "host.challenge")
                channel.send({"kind": "host.authenticate", "proof": "中文"})
                with self.assertRaises(DeskWeaveError):
                    channel.receive()
        self.error("INVALID_ARGUMENT", lambda: session.control._call([], {}))
        self.query(session)

    def test_lost_publication_receipt_keeps_shared_process(self):
        original = ControlSession._call
        created = []

        def call(control, method, params, **options):
            result = original(control, method, params, **options)
            if method == "host.publish":
                created.append(control)
                raise DeskWeaveError("CANCELLED", "publication receipt lost")
            return result

        with patch.object(ControlSession, "_call", call):
            self.error(
                "CANCELLED",
                lambda: self.coordinator.open(
                    self.identity, self.grant, plan=self.authority.seal(), create=True
                ),
            )
        (process,) = created
        self.stack.callback(self.stop_instance, process.peer)
        session = self.coordinator.open(self.identity, self.grant, create=True)
        self.stack.callback(session.close)
        self.assertEqual(session.peer, process.peer)
        self.query(session)

    def test_registered_host_exit_reclaims_pending_reference(self):
        session = self.start()
        host = self.helper_host("pending-death")
        session.register_host(host.binding, ConnectionPolicy({"workflow.read"}), lifetime=60)
        self.assertEqual(session.status()["pendingHosts"], 1)
        host.process.kill()
        host.process.wait(10)
        deadline = time.monotonic() + 5
        while True:
            status = session.status()
            if status["hosts"] == 1 and status["pendingHosts"] == 0:
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.025)
        self.query(session)

    def test_runtime_delegation_can_attach_and_is_revocable(self):
        session = self.start()
        host = self.helper_host("delegated")
        grant = session.register_host(
            host.binding, ConnectionPolicy({"workflow.read"}), lifetime=60
        )
        host.send(self.open_command(grant))
        self.assertEqual(wire.peer_value(host.receive()["peer"]), session.peer)
        host.send({"action": "query"})
        self.assertTrue(host.receive()["ok"])
        session.revoke_host(grant.binding.grant_id)
        host.send({"action": "query"})
        self.assertNotEqual(host.process.wait(15), 0)
        self.query(session)

    def test_stale_instance_and_directory_alias_reuse_workspace(self):
        first = self.start()
        discovery = Discovery(self.identity, self.coordinator.runtime)
        record = self.coordinator.runtime.read(discovery.name)
        first.close()
        self.assertEqual(first.control.wait(15), 0)
        self.coordinator.runtime.write(discovery.name, record)
        if sys.platform == "win32":
            alias_path = Path(str(self.identity.path).swapcase())
        else:
            alias_path = self.root / "alias"
            alias_path.symlink_to(self.identity.path, target_is_directory=True)
        with WorkspaceIdentity(alias_path) as alias:
            self.assertEqual(alias.object, self.identity.object)
            second = self.coordinator.open(alias, self.grant, plan=self.authority.seal())
        try:
            self.assertNotEqual(first.peer, second.peer)
            self.assertEqual(first.workspace, second.workspace)
            self.query(second)
        finally:
            second.close()
            self.assertEqual(second.control.wait(15), 0)

    def test_directory_failure_after_attach_closes_acquired_control(self):
        from deskweave.coordination.client import AttachedControl

        first = self.start()
        controls = []
        original = WorkspaceIdentity.check

        def construct(*args):
            value = AttachedControl(*args)
            controls.append(value)
            return value

        def check(identity, expected=None, *, allow_missing=False):
            if expected == first.workspace and not allow_missing:
                raise PermissionError("directory revalidation failed")
            return original(identity, expected, allow_missing=allow_missing)

        with (
            patch("deskweave.coordination.client.AttachedControl", construct),
            patch.object(WorkspaceIdentity, "check", check),
        ):
            with self.assertRaises(PermissionError):
                self.coordinator.open(self.identity, self.grant, create=True)
        (control,) = controls
        self.assertTrue(control._closed)
        self.assertTrue(control.owner.closed)
        self.assertFalse(control._heartbeat.is_alive())
        self.assertEqual(len(self.coordinator.sessions), 1)
        with self.coordinator.open(self.identity, self.grant, create=True) as recovered:
            self.query(recovered)

    def test_host_registration_rejection_keeps_control_session(self):
        session = self.start()
        host = self.helper_host("registration-exit")
        original = session.control._exchange

        def exchange(method, params, budget):
            if method == "host.register.commit":
                host.process.kill()
                host.process.wait(10)
            return original(method, params, budget)

        with patch.object(session.control, "_exchange", exchange):
            self.error(
                "AUTHENTICATION_FAILED",
                lambda: session.register_host(host.binding, self.policy, lifetime=60),
            )
        self.assertFalse(session.control._closed)
        self.query(session)

    def test_invalid_idle_grace_is_rejected_before_runtime_allocation(self):
        target = self.root / "invalid-runtime"
        for value in (True, None, -1, 0.01, 301, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkspaceCoordinator(self.launch, self.local, directory=target, idle_grace=value)
        self.assertFalse(target.exists())

    def test_false_endpoint_rejected_before_host_authentication(self):
        session = self.start()
        discovery = Discovery(self.identity, self.coordinator.runtime)
        record = self.coordinator.runtime.read(discovery.name)
        with NativeListener(self.local) as fake, ThreadPoolExecutor() as pool:
            discovery.publish(
                session.workspace,
                session.peer,
                fake.address,
                session.control.address,
                self.grant.binding.authority,
            )

            def listen():
                try:
                    with fake.accept(
                        {self.local.identity.process_id: self.local}, timeout=10
                    ) as channel:
                        return channel.receive(timeout=10)
                except DeskWeaveError:
                    return None

            received = pool.submit(listen)
            try:
                self.error(
                    "AUTHENTICATION_FAILED",
                    lambda: self.coordinator.open(
                        self.identity, self.grant, create=True, timeout=10
                    ),
                )
                self.assertIsNone(received.result(timeout=15))
            finally:
                self.coordinator.runtime.write(discovery.name, record)
        self.query(session)


if __name__ == "__main__":
    unittest.main()
