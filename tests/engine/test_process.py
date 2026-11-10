from __future__ import annotations

import io
import json
import os
import queue
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.engine.process import development_launch

from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.process import EngineProcess
from deskweave.process import _wire as wire
from deskweave.process._worker import Registry, Runtime
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.native import (
    Installation,
    InstalledFile,
    NativeClient,
    ProcessBinding,
    connect,
)
from deskweave.transport.native._common import Budget
from deskweave.transport.protocol import ConnectionPolicy, PeerIdentity


class ProcessCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launch = development_launch((Path(__file__).with_name("process_helper.py"),))

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(
            self.stack.enter_context(tempfile.TemporaryDirectory(prefix="dw-process-test-"))
        )
        self.local = self.stack.enter_context(
            ProcessBinding(
                os.getpid(), self.launch.installation, instance_epoch="process-test-host"
            )
        )
        self.workspace = self.directory / "workspace"
        self.crashes = set()

    def start(self, **options):
        process = EngineProcess(self.launch, self.local, self.workspace, create=True, **options)

        def wait():
            code = process.wait(15)
            if process.pid in self.crashes:
                self.assertNotEqual(code, 0)
            else:
                self.assertEqual(code, 0, process.diagnostics)

        self.stack.callback(wait)
        self.stack.callback(process.close)
        return process

    def client(self, process, credential=None):
        credential = credential or process.authorize(
            self.local, ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=60
        )
        return self.stack.enter_context(process.connect(credential, self.local)), credential

    def waiting_process(self):
        candidate = subprocess.Popen(
            [
                str(self.launch.launcher.path),
                "-I",
                "-S",
                "-X",
                "utf8",
                "-c",
                "import sys; print('ready', flush=True); sys.stdin.read()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        ready = queue.Queue()
        reader = threading.Thread(
            target=lambda: ready.put(candidate.stdout.readline()), daemon=True
        )
        reader.start()

        def finish():
            if candidate.poll() is None:
                candidate.kill()
            candidate.wait(10)
            reader.join(5)
            candidate.stdin.close()
            candidate.stdout.close()

        self.stack.callback(finish)
        self.assertEqual(ready.get(timeout=10).strip(), b"ready")
        return candidate

    def error(self, code, operation):
        with self.assertRaises(DeskWeaveError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def query(self, client, identity="query"):
        result = client.call("request.status", {"requestId": "not-saved"}, request_id=identity)
        self.assertTrue(result["ok"])
        return result

    def test_save_reconnect_and_workspace_ownership(self):
        process = self.start()
        self.assertNotEqual(process.pid, os.getpid())
        self.error("WORKSPACE_BUSY", lambda: Workspace(self.workspace))
        client, credential = self.client(process)
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
            request_id="owned-save",
        )
        self.assertTrue(saved["ok"])
        client.close()
        restored, _ = self.client(process, credential)
        recovered = restored.call(
            "request.status", {"requestId": "owned-save"}, request_id="recover"
        )
        self.assertTrue(recovered["ok"])
        self.assertEqual(saved["result"], recovered["result"]["result"])
        restored.close()
        process.close()
        self.assertEqual(process.wait(15), 0, process.diagnostics)
        with Workspace(self.workspace) as opened:
            self.assertEqual(opened.id, process.workspace)

    def test_duplicate_candidate_does_not_stop_existing_engine(self):
        process = self.start()
        client, _ = self.client(process)
        self.error(
            "WORKSPACE_BUSY",
            lambda: EngineProcess(self.launch, self.local, self.workspace / ".", timeout=30),
        )
        self.query(client)
        self.assertIsNone(process.poll())

    def test_controller_close_drains_after_last_client(self):
        process = self.start()
        client, _ = self.client(process)
        process.close()
        self.query(client)
        self.assertIsNone(process.poll())
        client.close()
        self.assertEqual(process.wait(15), 0, process.diagnostics)

    def test_revoke_and_revision_leave_other_client_active(self):
        process = self.start()
        first, credential = self.client(process)
        other, _ = self.client(process)
        self.assertEqual(
            process.revise(
                credential, ConnectionPolicy({"workflow.read"}), expected_version=1, lifetime=60
            ),
            2,
        )
        with self.assertRaises(DeskWeaveError):
            self.query(first)
        replacement, _ = self.client(process, credential)
        self.query(replacement)
        process.revoke(credential)
        process.revoke(credential)
        self.error("ACCESS_DENIED", lambda: process.issue(credential))
        self.query(other)

    def test_exiting_registration_does_not_drain_other_clients(self):
        process = self.start()
        client, _ = self.client(process)
        candidate = self.waiting_process()
        with ProcessBinding(
            candidate.pid, self.launch.installation, instance_epoch="departing-client"
        ) as peer:
            original = process._exchange

            def exchange(method, params, budget):
                if method == "register.commit":
                    candidate.kill()
                    candidate.wait(5)
                return original(method, params, budget)

            with patch.object(process, "_exchange", exchange):
                self.error(
                    "AUTHENTICATION_FAILED",
                    lambda: process.authorize(
                        peer, ConnectionPolicy({"workflow.read"}), lifetime=60
                    ),
                )
        self.query(client)
        self.assertEqual(process.status()["clients"], 1)
        replacement = process.authorize(
            self.local, ConnectionPolicy({"workflow.read"}), lifetime=60
        )
        with process.connect(replacement, self.local) as other:
            self.query(other)

    def test_manifest_file_loss_does_not_drain_other_clients(self):
        process = self.start()
        client, _ = self.client(process)
        before = process.status()
        candidate = self.waiting_process()
        source = self.directory / "extra-client-component.py"
        source.write_bytes(b"")
        item = InstalledFile(
            source, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        installation = replace(
            self.launch.installation, components=(*self.launch.installation.components, item)
        )
        with ProcessBinding(
            candidate.pid, installation, instance_epoch="missing-component"
        ) as peer:
            original = process._exchange

            def exchange(method, params, budget):
                if method == "register.files":
                    source.unlink(missing_ok=True)
                return original(method, params, budget)

            with patch.object(process, "_exchange", exchange):
                self.error(
                    "AUTHENTICATION_FAILED",
                    lambda: process.authorize(
                        peer, ConnectionPolicy({"workflow.read"}), lifetime=60
                    ),
                )
        self.query(client)
        after = process.status()
        for key in ("clients", "componentStates", "manifestBytes"):
            self.assertEqual(after[key], before[key])
        replacement = process.authorize(
            self.local, ConnectionPolicy({"workflow.read"}), lifetime=60
        )
        with process.connect(replacement, self.local) as other:
            self.query(other)

    def test_ticket_file_loss_does_not_drain_other_clients(self):
        process = self.start()
        client, _ = self.client(process)
        before = process.status()
        candidate = self.waiting_process()
        source = self.directory / "ticket-component.py"
        source.write_bytes(b"")
        item = InstalledFile(
            source, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        installation = replace(
            self.launch.installation, components=(*self.launch.installation.components, item)
        )
        with ProcessBinding(candidate.pid, installation, instance_epoch="ticket-client") as peer:
            credential = process.authorize(peer, ConnectionPolicy({"workflow.read"}), lifetime=60)
            self.assertGreater(process.status()["componentStates"], before["componentStates"])
            source.unlink()
            self.error("AUTHENTICATION_FAILED", lambda: process.issue(credential))
            self.query(client)
            self.assertEqual(process.status()["clients"], before["clients"] + 1)
            process.revoke(credential)
        after = process.status()
        for key in ("clients", "componentStates", "manifestBytes"):
            self.assertEqual(after[key], before[key])
        replacement = process.authorize(
            self.local, ConnectionPolicy({"workflow.read"}), lifetime=60
        )
        with process.connect(replacement, self.local) as other:
            self.query(other)

    def test_invalid_control_arguments_preserve_connection(self):
        process = self.start()
        client, credential = self.client(process)
        for index, lifetime in enumerate((float("nan"), float("inf"), b"invalid")):
            with self.subTest(lifetime=index):
                self.error(
                    "INVALID_ARGUMENT",
                    lambda lifetime=lifetime: process.authorize(
                        self.local, ConnectionPolicy({"workflow.read"}), lifetime=lifetime
                    ),
                )
                self.query(client, f"invalid-control-{index}")
                self.assertEqual(process.status()["clients"], 1)
        self.error(
            "INVALID_ARGUMENT",
            lambda: process.revise(
                credential,
                ConnectionPolicy({"workflow.read"}),
                expected_version=float("nan"),
                lifetime=60,
            ),
        )
        self.assertEqual(
            process.revise(
                credential, ConnectionPolicy({"workflow.read"}), expected_version=1, lifetime=60
            ),
            2,
        )
        with process.connect(credential, self.local) as replacement:
            self.query(replacement)

    def test_client_capacity_and_same_installation_reuse(self):
        process = self.start(max_clients=1)
        methods = []
        original = process._exchange

        def exchange(method, params, budget):
            methods.append(method)
            return original(method, params, budget)

        with patch.object(process, "_exchange", exchange):
            credential = process.authorize(
                self.local, ConnectionPolicy({"workflow.read"}), lifetime=60
            )
        self.assertEqual(methods, ["register.begin", "register.commit"])
        self.error(
            "LIMIT_EXCEEDED",
            lambda: process.authorize(self.local, ConnectionPolicy({"workflow.read"}), lifetime=60),
        )
        self.assertEqual(process.status()["clients"], 1)
        process.revoke(credential)
        self.assertEqual(process.status()["clients"], 0)
        process.authorize(self.local, ConnectionPolicy({"workflow.read"}), lifetime=60)

    def test_cancel_before_registration_keeps_control(self):
        process = self.start()
        cancel = threading.Event()
        original = self.local.check

        def check(checkpoint=lambda: None):
            cancel.set()
            original(checkpoint)

        with patch.object(self.local, "check", check):
            self.error(
                "CANCELLED",
                lambda: process.authorize(
                    self.local, ConnectionPolicy({"workflow.read"}), lifetime=60, cancel=cancel
                ),
            )
        self.assertEqual(process.status()["clients"], 0)
        client, _ = self.client(process)
        self.query(client)

    def test_native_channel_closes_if_budget_expires_before_transfer(self):
        process = self.start()
        credential = process.authorize(self.local, ConnectionPolicy({"workflow.read"}), lifetime=60)
        from deskweave.process import control as module

        original_connect, original_remaining = module.connect, module.remaining
        channels = []
        expired = [False]

        def connected(*args, **kwargs):
            channel = original_connect(*args, **kwargs)
            channels.append(channel)
            expired[0] = True
            return channel

        def remaining(budget):
            if expired[0]:
                raise DeskWeaveError("DEADLINE_EXCEEDED", "elapsed")
            return original_remaining(budget)

        with (
            patch.object(module, "connect", connected),
            patch.object(module, "remaining", remaining),
        ):
            self.error("DEADLINE_EXCEEDED", lambda: process.connect(credential, self.local))
        self.assertEqual(len(channels), 1)
        self.error("TRANSPORT_CLOSED", channels[0].check)
        self.assertEqual(process.status()["clients"], 1)

    def test_shadowed_application_module_never_executes(self):
        shadow = self.directory / "shadow"
        package = shadow / "deskweave"
        package.mkdir(parents=True)
        marker = self.directory / "executed"
        (package / "__init__.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')",
            encoding="utf-8",
        )
        launch = replace(self.launch, import_paths=(shadow, *self.launch.import_paths))
        self.error(
            "ENGINE_START_FAILED",
            lambda: EngineProcess(launch, self.local, self.workspace, create=True),
        )
        self.assertFalse(marker.exists())
        self.assertFalse(self.workspace.exists())

    def test_shadowed_dependency_never_executes(self):
        shadow = self.directory / "shadow"
        shadow.mkdir()
        marker = self.directory / "executed"
        (shadow / "jsonschema.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')",
            encoding="utf-8",
        )
        launch = replace(self.launch, import_paths=(shadow, *self.launch.import_paths))
        self.error(
            "ENGINE_START_FAILED",
            lambda: EngineProcess(launch, self.local, self.workspace, create=True),
        )
        self.assertFalse(marker.exists())

    def test_ambient_python_path_is_not_inherited(self):
        shadow = self.directory / "shadow"
        shadow.mkdir()
        marker = self.directory / "executed"
        (shadow / "sitecustomize.py").write_text(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected')",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"PYTHONPATH": str(shadow), "PYTHONSTARTUP": str(shadow / "sitecustomize.py")},
        ):
            process = self.start()
        self.assertFalse(marker.exists())
        self.assertEqual(process.status()["clients"], 0)

    def test_thread_start_failure_reaps_owned_candidate(self):
        original_start, original_popen = threading.Thread.start, subprocess.Popen
        children = []

        def popen(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        def start(thread):
            if thread.name == "deskweave-diagnostics":
                raise RuntimeError("thread allocation")
            return original_start(thread)

        with patch("subprocess.Popen", popen), patch.object(threading.Thread, "start", start):
            with self.assertRaisesRegex(RuntimeError, "thread allocation"):
                EngineProcess(self.launch, self.local, self.workspace, create=True)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.local.check()

    def test_start_cancellation_has_no_process_side_effect(self):
        cancel = threading.Event()
        cancel.set()
        with patch("subprocess.Popen") as spawn:
            self.error(
                "CANCELLED",
                lambda: EngineProcess(
                    self.launch, self.local, self.workspace, create=True, cancel=cancel
                ),
            )
        spawn.assert_not_called()
        self.assertFalse(self.workspace.exists())

    def test_parent_exit_preserves_another_process_client(self):
        bootstrap = (
            "import json,sys,runpy; sys.path=json.loads(sys.stdin.readline()); "
            "runpy.run_module('tests.engine.process_helper',run_name='__main__')"
        )
        owner = subprocess.Popen(
            [sys._base_executable, "-X", "utf8", "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            close_fds=True,
        )
        messages = queue.Queue()

        def read():
            for line in owner.stdout:
                messages.put(json.loads(line))
            messages.put(None)

        reading = threading.Thread(target=read, daemon=True)
        reading.start()

        def close_owner():
            if owner.poll() is None:
                owner.kill()
                owner.wait(10)
            reading.join(5)
            owner.stdin.close()
            owner.stdout.close()
            owner.stderr.close()

        self.stack.callback(close_owner)
        config = {
            "installation": wire.manifest_header(self.launch.installation),
            "files": [wire.file_value(item) for item in self.launch.installation.components],
            "launcher": wire.file_value(self.launch.launcher),
            "paths": [str(value) for value in self.launch.import_paths],
            "workspace": str(self.workspace),
        }
        owner.stdin.write(json.dumps(sys.path) + "\n" + json.dumps(config) + "\n")
        owner.stdin.flush()
        ready = messages.get(timeout=30)
        self.assertIsNotNone(ready)
        expected = wire.peer_value(ready["peer"])
        engine = self.stack.enter_context(
            ProcessBinding(
                ready["pid"], self.launch.installation, instance_epoch=expected.instance_epoch
            )
        )

        def wait_engine():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    engine.process.snapshot()
                except DeskWeaveError:
                    return
                time.sleep(0.025)
            engine.process.snapshot()
            os.kill(ready["pid"], signal.SIGTERM)
            self.fail("Engine did not drain after its last client closed")

        self.stack.callback(wait_engine)
        owner.stdin.write(
            json.dumps({"action": "client", "peer": self.local.identity.as_dict()}) + "\n"
        )
        owner.stdin.flush()
        selected = wire.ticket(messages.get(timeout=30))
        channel = connect(ready["address"], self.local, engine)
        client = self.stack.enter_context(NativeClient(channel, selected))
        owner.kill()
        owner.wait(10)
        self.query(client)
        client.close()

    def test_crashed_engine_reopens_committed_workflow(self):
        process = self.start()
        client, _ = self.client(process)
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
            request_id="before-crash",
        )
        self.assertTrue(saved["ok"])
        client.close()
        self.crashes.add(process.pid)
        process._process.kill()
        self.assertNotEqual(process.wait(15), 0)
        replacement = self.start()
        reader, _ = self.client(replacement)
        loaded = reader.call(
            "workflow.read",
            {
                "workflowId": definition["id"],
                "revision": saved["result"]["revision"],
            },
            request_id="after-crash",
        )
        self.assertTrue(loaded["ok"])
        self.assertEqual(loaded["result"]["definition"], definition)

    def test_initial_manifest_quota_rejected_before_ready(self):
        with self.assertRaises(DeskWeaveError):
            EngineProcess(
                self.launch, self.local, self.workspace, create=True, max_manifest_bytes=64
            )
        self.assertFalse(self.workspace.exists())

    def test_distinct_initial_manifests_fit_only_with_total_budget(self):
        extra = self.directory / "additional-component.py"
        extra.write_bytes(b"")
        item = InstalledFile(
            extra, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        installation = replace(
            self.launch.installation, components=(*self.launch.installation.components, item)
        )
        launch = replace(self.launch, installation=installation)
        limit = max(wire.manifest_size(installation), wire.manifest_size(self.local.installation))
        self.error(
            "LIMIT_EXCEEDED",
            lambda: EngineProcess(
                launch, self.local, self.workspace, create=True, max_manifest_bytes=limit
            ),
        )
        process = self.start()
        self.assertEqual(process.status()["clients"], 0)

    def test_exact_initial_metadata_budget_accepts_shared_manifest(self):
        process = self.start(max_manifest_bytes=wire.manifest_size(self.launch.installation))
        self.assertEqual(
            process.status()["manifestBytes"], wire.manifest_size(self.launch.installation)
        )


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        source = self.directory / "component.py"
        source.write_bytes(b"")
        item = InstalledFile(
            source, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        self.installation = Installation(item, (item,), "anchor apple")
        self.clock = [100.0]
        self.engine = Engine(
            Workspace(self.directory / "workspace", create=True), monotonic=lambda: self.clock[0]
        )
        self.stack.callback(self.engine.close)
        self.server = self.binding(100)
        self.parent = self.binding(101)
        self.authority = self.stack.enter_context(
            ConnectionAuthority(self.engine, self.server.identity)
        )
        self.registry = Registry(
            self.authority, (self.server, self.parent), max_states=8, max_bytes=16384
        )
        self.stack.callback(self.registry.close)
        self.policy = ConnectionPolicy({"workflow.read"})

    def binding(self, pid):
        identity = PeerIdentity(
            "windows",
            "fixture-user",
            "fixture-session",
            pid,
            "start",
            self.installation.digest,
            "instance",
        )
        return SimpleNamespace(
            identity=identity,
            installation=self.installation,
            check=lambda checkpoint: checkpoint(),
            close=lambda: None,
        )

    def register(self, binding=None, lifetime=60):
        binding = binding or self.parent
        with patch("deskweave.process._worker.ProcessBinding", return_value=binding):
            return self.registry.add(
                binding.identity, binding.installation, self.policy, lifetime, Budget(30)
            )

    def test_revision_expiring_before_snapshot_releases_old_registration(self):
        credential = self.register(lifetime=3600)
        original = self.authority.describe
        calls = [0]

        def describe(value):
            calls[0] += 1
            if calls[0] == 2:
                self.clock[0] += 2
            return original(value)

        with patch.object(self.authority, "describe", describe):
            with self.assertRaises(DeskWeaveError) as caught:
                self.registry.revise(credential, self.policy, 1, 1)
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        self.assertEqual(self.registry.grants, {})
        self.assertEqual(self.registry.peers, {})
        self.assertEqual(self.registry.states, 4)

    def test_short_initial_grant_failure_keeps_registration_capacity(self):
        original = self.authority.describe

        def describe(value):
            self.clock[0] += 2
            return original(value)

        binding = self.binding(102)
        closed = []
        binding.close = lambda: closed.append(True)
        with patch.object(self.authority, "describe", describe):
            with self.assertRaises(DeskWeaveError):
                self.register(binding, lifetime=1)
        self.assertEqual(closed, [True])
        self.assertEqual(self.registry.grants, {})
        self.assertEqual(self.registry.peers, {})
        self.assertEqual(self.registry.states, 4)

    def test_failed_registration_retains_binding_until_close_succeeds(self):
        original = self.authority.describe
        initial_bytes = self.registry.bytes
        installation = replace(self.installation, macos_requirement="anchor retained")
        digest = installation.digest
        for failure in ("expired", "identity"):
            with self.subTest(failure=failure):
                binding = self.binding(102)
                binding.installation = installation
                binding.identity = replace(binding.identity, installation_digest=digest)
                requested = (
                    replace(
                        binding.identity, instance_epoch="different", installation_digest="f" * 64
                    )
                    if failure == "identity"
                    else binding.identity
                )

                def close():
                    raise OSError("temporary registration close failure")

                binding.close = close

                def describe(value):
                    self.assertIsNone(self.registry.get(102))
                    self.assertIsNone(self.registry.acquire_peer(102))
                    self.clock[0] += 2
                    return original(value)

                with (
                    patch("deskweave.process._worker.ProcessBinding", return_value=binding),
                    patch.object(self.authority, "describe", describe),
                    self.assertRaises(BaseExceptionGroup) as caught,
                ):
                    self.registry.add(requested, installation, self.policy, 1, Budget(30))
                self.assertEqual(
                    caught.exception.exceptions[0].code,
                    "ACCESS_DENIED" if failure == "expired" else "AUTHENTICATION_FAILED",
                )
                self.assertEqual(self.registry.grants, {})
                self.assertEqual(self.authority._clients, {})
                self.assertEqual(self.registry.states, 6)
                self.assertEqual(
                    self.registry.bytes, initial_bytes + wire.manifest_size(installation)
                )
                self.assertIs(self.registry.peers[102].binding, binding)
                self.assertIn(102, self.registry.closing)
                self.assertIsNone(self.registry.get(102))
                self.assertIsNone(self.registry.acquire_peer(102))
                self.assertTrue(self.registry.prune())
                for limit, value in (("max_states", 6), ("max_bytes", self.registry.bytes)):
                    with self.subTest(limit=limit):
                        other = self.binding(103)
                        other.installation = replace(
                            self.installation, macos_requirement="anchor other"
                        )
                        other.identity = replace(
                            other.identity, installation_digest=other.installation.digest
                        )
                        with (
                            patch.object(self.registry, limit, value),
                            patch("deskweave.process._worker.ProcessBinding") as factory,
                            self.assertRaises(DeskWeaveError) as rejected,
                        ):
                            self.registry.add(
                                other.identity, other.installation, self.policy, 60, Budget(30)
                            )
                        self.assertEqual(rejected.exception.code, "LIMIT_EXCEEDED")
                        factory.assert_not_called()
                with patch.object(self.authority, "max_clients", 1):
                    with self.assertRaises(DeskWeaveError) as rejected:
                        self.register(self.binding(103))
                    self.assertEqual(rejected.exception.code, "LIMIT_EXCEEDED")
                binding.close = lambda: None
                self.assertEqual(self.registry.prune(), [])
                self.assertEqual(self.registry.states, 4)
                self.assertEqual(self.registry.bytes, initial_bytes)
                self.assertNotIn(102, self.registry.peers)
                self.assertNotIn(digest, self.registry.installations)
                credential = self.register(binding)
                self.registry.revoke(credential)
                self.assertEqual(self.registry.states, 4)
                self.assertEqual(self.registry.bytes, initial_bytes)

    def test_registration_quota_precedes_binding_allocation(self):
        binding = self.binding(102)
        binding.installation = replace(self.installation, macos_requirement="anchor larger")
        binding.identity = replace(
            binding.identity, installation_digest=binding.installation.digest
        )
        for limit, value in (("max_states", 4), ("max_bytes", self.registry.bytes)):
            with (
                self.subTest(limit=limit),
                patch.object(self.registry, limit, value),
                patch("deskweave.process._worker.ProcessBinding") as factory,
                self.assertRaises(DeskWeaveError) as caught,
            ):
                self.registry.add(
                    binding.identity, binding.installation, self.policy, 60, Budget(30)
                )
            self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
            factory.assert_not_called()
        self.assertEqual(self.registry.states, 4)
        self.assertEqual(self.registry.peers, {})

    def test_registration_preallocation_observes_cancel_and_deadline(self):
        installation = replace(self.installation, macos_requirement="anchor budget")
        identity = replace(self.binding(102).identity, installation_digest=installation.digest)
        original = Installation.calculate_digest
        initial_bytes = self.registry.bytes
        for stage in ("digest", "metadata"):
            for code in ("CANCELLED", "DEADLINE_EXCEEDED"):
                with self.subTest(stage=stage, code=code):
                    cancel = threading.Event()
                    budget = Budget(30, cancel)

                    def interrupt(code=code, budget=budget, cancel=cancel):
                        if code == "CANCELLED":
                            cancel.set()
                        else:
                            budget.deadline = time.monotonic() - 1

                    def calculate(value, checkpoint=lambda: None, interrupt=interrupt):
                        result = original(value, checkpoint)
                        interrupt()
                        return result

                    if stage == "digest":
                        interrupt()
                    with (
                        patch.object(
                            Installation,
                            "calculate_digest",
                            calculate if stage == "metadata" else original,
                        ),
                        patch("deskweave.process._worker.ProcessBinding") as factory,
                        self.assertRaises(DeskWeaveError) as caught,
                    ):
                        self.registry.add(identity, installation, self.policy, 60, budget)
                    self.assertEqual(caught.exception.code, code)
                    factory.assert_not_called()
                    self.assertEqual(self.registry.peers, {})
                    self.assertEqual(self.registry.states, 4)
                    self.assertEqual(self.registry.bytes, initial_bytes)

    def test_binding_initialization_failure_retains_open_process(self):
        from deskweave.transport.native import identity as native_identity
        from deskweave.transport.native._common import InitializationError

        installation = replace(self.installation, macos_requirement="anchor initial")
        selected = replace(self.binding(102).identity, installation_digest=installation.digest)
        initial = (
            selected.user_id,
            selected.os_session_id,
            selected.process_started,
            installation.executable.path,
        )
        before_bytes = self.registry.bytes
        for stage in ("snapshot", "installation"):
            with self.subTest(stage=stage):
                process = SimpleNamespace(initial=initial, snapshot=lambda: initial)

                def close():
                    raise OSError("initial reference close failure")

                process.close = close
                with (
                    patch.object(
                        native_identity.platform, "Process", return_value=process
                    ) as factory,
                    patch.object(
                        ProcessBinding,
                        "_verify_installation",
                        side_effect=DeskWeaveError("AUTHENTICATION_FAILED", "changed installation"),
                    ),
                ):
                    factory.platform = selected.platform
                    if stage == "snapshot":
                        factory.side_effect = InitializationError(
                            process, [PermissionError("initial snapshot"), OSError("initial close")]
                        )
                    with self.assertRaises(InitializationError) as caught:
                        self.registry.add(selected, installation, self.policy, 60, Budget(30))
                binding = caught.exception.resource
                self.assertIs(self.registry.peers[102].binding, binding)
                self.assertEqual(hasattr(binding, "identity"), stage == "installation")
                self.assertEqual(self.registry.states, 6)
                self.assertEqual(
                    self.registry.bytes, before_bytes + wire.manifest_size(installation)
                )
                self.assertIsNone(self.registry.get(102))
                self.assertIsNone(self.registry.acquire_peer(102))
                self.assertEqual(self.authority._clients, {})
                self.assertTrue(self.registry.prune())
                process.close = lambda: None
                self.assertEqual(self.registry.prune(), [])
                self.assertEqual(self.registry.peers, {})
                self.assertEqual(self.registry.states, 4)
                self.assertEqual(self.registry.bytes, before_bytes)

    def test_failed_binding_close_retains_ownership_and_quota(self):
        binding = self.binding(102)
        attempts = [0]

        def close():
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("temporary close failure")

        binding.close = close
        credential = self.register(binding)
        self.authority.revoke(credential)
        with self.assertRaises(BaseExceptionGroup):
            self.registry.forget(credential.client_id)
        self.assertEqual(self.registry.states, 6)
        self.assertIn(102, self.registry.closing)
        self.assertIsNone(self.registry.get(102))
        self.assertEqual(self.registry.retry_closing(), [])
        self.assertEqual(self.registry.states, 4)
        self.assertNotIn(102, self.registry.peers)

    def test_aggregate_state_budget_and_release(self):
        first = self.register(self.binding(102))
        self.register(self.binding(103))
        with self.assertRaises(DeskWeaveError) as caught:
            self.register(self.binding(104))
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
        self.authority.revoke(first)
        self.registry.forget(first.client_id)
        self.register(self.binding(104))
        self.assertEqual(self.registry.states, 8)
        self.assertEqual(len(self.registry.installations), 1)

    def test_revision_heap_remains_bounded(self):
        credential = self.register()
        for version in range(1, 100):
            self.registry.revise(credential, self.policy, version, 60 + version)
        self.assertLessEqual(len(self.registry.expirations), 34)

    def test_listener_close_failure_still_drains_and_cleans_authority(self):
        attempts = [0]

        def close():
            attempts[0] += 1
            if attempts[0] == 1:
                raise OSError("temporary listener failure")

        def accept(_peers, *, cancel):
            cancel.wait(3)
            raise DeskWeaveError("CANCELLED", "stopped")

        listener = SimpleNamespace(local=self.server, close=close, accept=accept)
        runtime = Runtime(listener, self.authority, self.parent, max_states=8, max_bytes=16384)
        control = SimpleNamespace(
            receive=lambda **_kwargs: (_ for _ in ()).throw(
                DeskWeaveError("TRANSPORT_CLOSED", "closed")
            )
        )
        with self.assertRaises(BaseExceptionGroup):
            runtime.run(control)
        self.assertFalse(runtime.acceptor.is_alive())
        self.assertTrue(self.authority._closed)
        self.assertEqual(attempts[0], 2)

    def failing_connection(self, credential):
        authority = self.authority

        class Connection:
            attempts = 0

            def close(connection):
                connection.attempts += 1
                if connection.attempts == 1:
                    raise OSError("transport close")
                authority.finished_close(connection)

        connection = Connection()
        authority._connections[connection] = SimpleNamespace(
            client_id=credential.client_id, deadline=None
        )
        return connection

    def test_revision_cleanup_failure_still_updates_expiration(self):
        credential = self.register(lifetime=3600)
        connection = self.failing_connection(credential)
        with self.assertRaises(BaseExceptionGroup):
            self.registry.revise(credential, self.policy, 1, 1)
        self.assertEqual(self.registry.grants[credential.client_id].deadline, 101)
        self.clock[0] = 102
        self.registry.prune()
        self.assertEqual(self.registry.grants, {})
        self.assertEqual(self.registry.peers, {})
        self.assertGreaterEqual(connection.attempts, 2)

    def test_revoke_cleanup_failure_removes_grant_and_retries(self):
        credential = self.register(self.binding(102))
        connection = self.failing_connection(credential)
        with self.assertRaises(BaseExceptionGroup):
            self.registry.revoke(credential)
        self.assertEqual(self.registry.grants, {})
        self.assertEqual(self.registry.states, 6)
        self.registry.prune()
        self.assertEqual(self.registry.states, 4)
        self.assertEqual(self.registry.peers, {})
        self.assertGreaterEqual(connection.attempts, 2)

    def test_connection_held_binding_stays_charged_until_release(self):
        credential = self.register(self.binding(102))
        peer = self.registry.acquire_peer(102)
        self.registry.revoke(credential)
        self.assertEqual(self.registry.states, 6)
        self.registry.release_peer(peer)
        self.registry.prune()
        self.assertEqual(self.registry.states, 4)

    def test_sweep_failure_does_not_skip_worker_barrier(self):
        released, attempted, ended = threading.Event(), threading.Event(), threading.Event()

        def accept(_peers, *, cancel):
            cancel.wait(3)
            raise DeskWeaveError("CANCELLED", "stopped")

        listener = SimpleNamespace(local=self.server, close=lambda: None, accept=accept)
        runtime = Runtime(listener, self.authority, self.parent, max_states=8, max_bytes=16384)

        def work():
            released.wait(5)
            with runtime.condition:
                runtime.workers.discard(threading.current_thread())
                ended.set()
                runtime.condition.notify_all()

        worker = threading.Thread(target=work)
        runtime.workers.add(worker)
        worker.start()
        errors = []
        control = SimpleNamespace(
            receive=lambda **_kwargs: (_ for _ in ()).throw(
                DeskWeaveError("TRANSPORT_CLOSED", "closed")
            )
        )

        def prune():
            attempted.set()
            raise OSError("sweep")

        def run():
            try:
                runtime.run(control)
            except BaseException as error:
                errors.append(error)

        running = threading.Thread(target=run)
        try:
            with patch.object(runtime.registry, "prune", prune):
                running.start()
                self.assertTrue(attempted.wait(3))
                self.assertFalse(self.authority._closed)
                self.assertFalse(ended.is_set())
                released.set()
                running.join(5)
        finally:
            released.set()
            worker.join(5)
            running.join(5)
        self.assertFalse(running.is_alive())
        self.assertTrue(self.authority._closed)
        self.assertTrue(errors)

    def test_startup_rebuild_uses_configured_manifest_budget(self):
        from deskweave.process import _worker

        reader_type = wire.ManifestReader
        limits = []
        for maximum in (33554432, 67108864, wire.manifest_size(self.installation) - 1):
            with self.subTest(maximum=maximum):
                limits.clear()

                def reader(header, *, byte_limit=16777216):
                    limits.append(byte_limit)
                    return reader_type(header, byte_limit=byte_limit)

                config = {
                    "version": wire.VERSION,
                    "workspace": str(self.directory / "startup-workspace"),
                    "create": True,
                    "parent": self.parent.identity.as_dict(),
                    "engineEpoch": "startup-budget",
                    "engineInstallation": wire.manifest_header(self.installation),
                    "parentInstallation": wire.manifest_header(self.installation),
                    "directory": None,
                    "maxConnections": 1,
                    "maxClients": 1,
                    "maxComponentStates": 8,
                    "maxManifestBytes": maximum,
                }
                output = io.BytesIO()
                files = [wire.file_value(item) for item in self.installation.components]
                with (
                    patch.object(wire, "ManifestReader", reader),
                    patch.object(
                        _worker,
                        "ProcessBinding",
                        side_effect=DeskWeaveError("CANCELLED", "启动配置测试结束。"),
                    ),
                    patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO())),
                    patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
                    patch.object(sys, "stderr", io.StringIO()),
                ):
                    self.assertEqual(_worker.main(config, list(files), list(files)), 1)
                output.seek(0)
                result = wire.read_frame(output)
                accepted = maximum >= wire.manifest_size(self.installation)
                self.assertEqual(limits, [maximum] * (2 if accepted else 1))
                self.assertEqual(
                    result["error"]["code"], "CANCELLED" if accepted else "LIMIT_EXCEEDED"
                )
                self.assertFalse((self.directory / "startup-workspace").exists())

    def test_authority_description_validates_secret_and_expiry(self):
        from deskweave.transport.protocol import ClientCredential

        credential = self.register(lifetime=1)
        value = self.authority.describe(credential)
        self.assertEqual(value.client_id, credential.client_id)
        self.assertEqual(value.policy, self.policy)
        self.assertEqual(value.expires_at, 101)
        with self.assertRaises(DeskWeaveError):
            self.authority.describe(ClientCredential(credential.client_id, b"x" * 32))
        self.clock[0] = 101
        with self.assertRaises(DeskWeaveError):
            self.authority.describe(credential)


class BootstrapCase(unittest.TestCase):
    def test_truncation_and_frame_limit(self):
        for data in (b"", b"\x01", struct.pack("<I", 10) + b"{}"):
            with self.subTest(data=data), self.assertRaises(DeskWeaveError) as caught:
                wire.read_frame(io.BytesIO(data))
            self.assertEqual(caught.exception.code, "TRANSPORT_TRUNCATED")
        with self.assertRaises(DeskWeaveError) as caught:
            wire.read_frame(io.BytesIO(struct.pack("<I", 1048577)))
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")

    def test_manifest_path_access_failures_are_authentication_errors(self):
        with tempfile.TemporaryDirectory(prefix="dw-manifest-access-") as temporary:
            source = Path(temporary) / "component.py"
            source.write_bytes(b"")
            item = InstalledFile(
                source, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )
            value = wire.file_value(item)
            self.assertEqual(wire.parse_file(value), item)
            for error in (
                FileNotFoundError("removed file"),
                NotADirectoryError("replaced directory"),
                PermissionError("denied path"),
                OSError("unavailable path"),
                RuntimeError("symbolic link loop"),
            ):
                with (
                    self.subTest(error=type(error).__name__),
                    patch.object(Path, "resolve", side_effect=error),
                    self.assertRaises(DeskWeaveError) as caught,
                ):
                    wire.parse_file(value)
                self.assertEqual(caught.exception.code, "AUTHENTICATION_FAILED")
            with self.assertRaises(ValueError):
                wire.parse_file({**value, "sha256": "invalid"})

    def test_large_manifest_uses_bounded_frames(self):
        item = SimpleNamespace(
            path=Path("/application/" + "component-" * 10 + ".py"), sha256="a" * 64
        )
        count = total = 0
        for values in wire.chunks([item] * 10000):
            output = io.BytesIO()
            wire.write_frame(output, values)
            data = output.getvalue()
            self.assertLessEqual(len(data), 1048580)
            count += len(values)
            total += len(data)
        self.assertEqual(count, 10000)
        self.assertGreater(total, 1048576)

    def test_manifest_size_checks_budget_while_counting_components(self):
        item = SimpleNamespace(path=Path("/component.py"), sha256="a" * 64)
        installation = SimpleNamespace(
            executable=item, components=(item,) * 100, macos_requirement=None
        )
        original = wire.file_value
        for code in ("CANCELLED", "DEADLINE_EXCEEDED"):
            with self.subTest(code=code):
                cancel = threading.Event()
                budget = Budget(30, cancel)
                counted = []

                def file_value(value, budget=budget, cancel=cancel, code=code, counted=counted):
                    counted.append(value)
                    if len(counted) == 3:
                        if code == "CANCELLED":
                            cancel.set()
                        else:
                            budget.deadline = time.monotonic() - 1
                    return original(value)

                with patch.object(wire, "file_value", file_value):
                    with self.assertRaises(DeskWeaveError) as caught:
                        wire.manifest_size(installation, budget.check_time)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(counted), 3)

    def test_manifest_quota_counts_resolved_alias_paths(self):
        with tempfile.TemporaryDirectory(prefix="dw-alias-") as temporary:
            root = Path(temporary)
            target = root / ("long-directory-" * 8)
            target.mkdir()
            source = target / "component.py"
            source.write_bytes(b"")
            alias = root / "short"
            try:
                alias.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            header = {
                "executable": {"path": str(source), "sha256": digest},
                "componentCount": 1,
                "macosRequirement": None,
            }
            value = {"path": str(alias / "component.py"), "sha256": digest}
            limit = len(wire.dumps(header)) + len(wire.dumps(value)) + 1
            reader = wire.ManifestReader(header, byte_limit=limit)
            with self.assertRaises(DeskWeaveError) as caught:
                reader.append([value])
            self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
            self.assertEqual(reader.items, [])

    def test_verified_loader_pins_package_and_resource_paths(self):
        import hashlib
        import importlib.util

        from deskweave.process._entry import VerifiedFinder

        with tempfile.TemporaryDirectory(prefix="dw-import-") as temporary:
            root = Path(temporary)
            original, replacement = root / "original", root / "replacement"
            original.mkdir()
            replacement.mkdir()
            source = original / "__init__.py"
            source.write_text("value = 1\n", encoding="utf-8")
            (original / "value.txt").write_bytes(b"original")
            (replacement / "__init__.py").write_text("value = 2\n", encoding="utf-8")
            (replacement / "value.txt").write_bytes(b"replacement")
            alias = root / "fixture_package"
            try:
                alias.symlink_to(original, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            finder = VerifiedFinder(
                {source.resolve(): hashlib.sha256(source.read_bytes()).hexdigest()}, (), (root,)
            )
            with patch.object(sys, "path", [str(root)]):
                spec = finder.find_spec("fixture_package")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            alias.unlink()
            alias.symlink_to(replacement, target_is_directory=True)
            self.assertEqual(module.__file__, str(source.resolve()))
            self.assertEqual(list(module.__path__), [str(original.resolve())])
            with spec.loader.get_resource_reader("fixture_package").open_resource(
                "value.txt"
            ) as stream:
                self.assertEqual(stream.read(), b"original")


if __name__ == "__main__":
    unittest.main()
