from __future__ import annotations

import hashlib
import multiprocessing
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deskweave import agent
from deskweave.adapters.workspace import Workspace
from deskweave.coordination.directory import runtime_directory
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads
from deskweave.process._wire import write_frame
from deskweave.transport.native import Installation, NativeChannel, NativeListener, ProcessBinding
from deskweave.transport.native._common import Budget, InitializationError
from tests.engine.test_native import installation, installed

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "target" / "agent-fixture" / "debug"
_SUFFIX = ".exe" if sys.platform == "win32" else ""
_BINARY = _FIXTURE / ("deskweave-agent" + _SUFFIX)
_SIGNER = _FIXTURE / "examples" / ("agent-test-authority" + _SUFFIX)


def _sign(claim, budget):
    return subprocess.run(
        [str(_SIGNER)],
        input=claim,
        capture_output=True,
        check=True,
        timeout=agent.remaining(budget),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    ).stdout


class _SignatureRequest:
    def __init__(self, claim, budget):
        budget.check_time()
        self.process = subprocess.Popen(
            [str(_SIGNER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            with self.process.stdin:
                self.process.stdin.write(claim)
        except BaseException as error:
            try:
                self.close()
            except BaseException as closing:
                raise InitializationError(self, [error, closing]) from None
            raise

    def poll(self):
        code = self.process.poll()
        if code is None:
            return None
        return self.process.stdout.read(65) if code == 0 else b""

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()


def _agent_installation():
    image = installed(_BINARY)
    requirement = None
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/codesign", "-d", "-r-", str(image.path)],
            text=True,
            capture_output=True,
            check=True,
        )
        requirement = (result.stdout + result.stderr).split("designated => ", 1)[1].splitlines()[0]
    schema = installed(_ROOT / "contracts" / "agent.schema.json")
    return Installation(image, (schema,), requirement)


def _contender(root, start, finish, results, workspace_id="contender-ws", idle_grace=0.15):
    scope = hashlib.sha256(os.fsencode(root)).hexdigest()
    with (
        ProcessBinding(os.getpid(), installation(), instance_epoch="contender") as local,
        patch.object(agent, "_runtime", side_effect=lambda _: runtime_directory(local, root)),
        patch.object(agent, "_scope", return_value=scope),
    ):
        peer = _agent_installation()
        if not start.wait(20):
            raise TimeoutError("未收到启动信号。")
        try:
            authority = agent.AgentAuthority((local,), _SignatureRequest)
            with agent.AgentProcess(
                local, peer, authority, workspace_id=workspace_id, idle_grace=idle_grace
            ) as instance:
                epoch = instance.probe()
                results.put(("ready", (instance.pid, epoch, instance.created)))
                if not finish.wait(30):
                    raise TimeoutError("未收到关闭信号。")
        except DeskWeaveError as error:
            results.put((error.code, None))


class DesktopAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (_BINARY.is_file() and _SIGNER.is_file()):
            message = "请先构建 Desktop Agent 和启动授权测试程序。"
            if os.environ.get("DESKWEAVE_AGENT_TEST_REQUIRED") == "1":
                raise RuntimeError(message)
            raise unittest.SkipTest(message)
        cls.host_installation = installation()
        cls.agent_installation = _agent_installation()

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="dw-agent-")))
        self.runtime = self.root / "runtime"
        self.local = self.stack.enter_context(
            ProcessBinding(os.getpid(), self.host_installation, instance_epoch="engine-test")
        )
        self.stack.enter_context(
            patch.object(
                agent, "_runtime", side_effect=lambda _: runtime_directory(self.local, self.runtime)
            )
        )
        scope = hashlib.sha256(os.fsencode(self.runtime)).hexdigest()
        self.stack.enter_context(patch.object(agent, "_scope", return_value=scope))
        self.authority = agent.AgentAuthority((self.local,), _SignatureRequest)

    def _start(
        self,
        *,
        installation=None,
        authority=None,
        cancel=None,
        workspace_id="workspace",
        idle_grace=0.15,
    ):
        return agent.AgentProcess(
            self.local,
            installation if installation is not None else self.agent_installation,
            authority if authority is not None else self.authority,
            workspace_id=workspace_id,
            cancel=cancel,
            idle_grace=idle_grace,
        )

    def _start_with_engine(self, engine, *, authority=None, idle_grace=0.15):
        return engine.start_desktop_agent(
            self.local,
            self.agent_installation,
            authority if authority is not None else self.authority,
            idle_grace=idle_grace,
        )

    def _read_error(self, code, function):
        with self.assertRaises(DeskWeaveError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_engine_connects_reuses_and_reaps_agent(self):
        engine = Engine(Workspace(self.root / "workspace", create=True))
        try:
            self.assertNotIn("agent.ping", engine.methods)
            instance = self._start_with_engine(engine)
            process = instance._process
            self.assertTrue(instance.created)
            self.assertEqual(instance.workspace_id, engine.workspace.id)
            self.assertNotEqual(instance.pid, os.getpid())
            self.assertEqual(instance.probe(), instance.epoch)
            self.assertIs(instance, self._start_with_engine(engine))
        finally:
            engine.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self._read_error("TRANSPORT_CLOSED", instance.probe)

    def test_concurrent_engines_share_single_agent(self):
        context = multiprocessing.get_context("spawn")
        start, finish, results = context.Event(), context.Event(), context.Queue()
        contenders = [
            context.Process(
                target=_contender,
                args=(self.runtime, start, finish, results, f"ws-{index}", 0.15),
            )
            for index in range(2)
        ]
        with runtime_directory(self.local, self.runtime):
            pass
        active = []
        exit_codes = []
        try:
            for contender in contenders:
                contender.start()
                active.append(contender)
            start.set()
            records = [results.get(timeout=25), results.get(timeout=25)]
            self.assertEqual([record[0] for record in records], ["ready", "ready"])
            pids = {record[1][0] for record in records}
            epochs = {record[1][1] for record in records}
            created_flags = [record[1][2] for record in records]
            self.assertEqual(len(pids), 1)
            self.assertEqual(len(epochs), 1)
            self.assertCountEqual(created_flags, [True, False])
        finally:
            finish.set()
            start.set()
            for contender in active:
                contender.join(8)
                if contender.is_alive():
                    contender.kill()
                    contender.join(5)
                exit_codes.append(contender.exitcode)
                if contender.exitcode is not None:
                    contender.close()
            results.close()
            results.join_thread()
        self.assertEqual(exit_codes, [0, 0])
        with self._start() as instance:
            self.assertEqual(instance.probe(), instance.epoch)

    def test_shared_agent_survives_launcher_exit_and_exits_after_idle_grace(self):
        engine_a = Engine(Workspace(self.root / "workspace-a", create=True))
        engine_b = Engine(Workspace(self.root / "workspace-b", create=True))
        authority_b = agent.AgentAuthority((self.local,), _SignatureRequest)
        try:
            first = self._start_with_engine(engine_a, idle_grace=0.15)
            process = first._process
            second = self._start_with_engine(engine_b, authority=authority_b, idle_grace=0.15)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(second.pid, first.pid)
            self.assertEqual(second.epoch, first.epoch)
            self.assertEqual(second.workspace_id, engine_b.workspace.id)
            engine_a.close()
            threading.Event().wait(0.35)
            self.assertIsNone(process.poll())
            self.assertEqual(second.probe(), first.epoch)
        finally:
            engine_a.close()
            engine_b.close()
        self.assertEqual(process.wait(timeout=5), 0)

    def test_reattach_within_idle_grace_cancels_exit(self):
        engine_a = Engine(Workspace(self.root / "workspace-a", create=True))
        engine_b = Engine(Workspace(self.root / "workspace-b", create=True))
        try:
            first = self._start_with_engine(engine_a, idle_grace=2.0)
            process = first._process
            original_pid, original_epoch = first.pid, first.epoch
            engine_a.close()
            threading.Event().wait(0.2)
            self.assertIsNone(process.poll())
            second = self._start_with_engine(engine_b, idle_grace=2.0)
            self.assertFalse(second.created)
            self.assertEqual(second.pid, original_pid)
            self.assertEqual(second.epoch, original_epoch)
            threading.Event().wait(2.1)
            self.assertIsNone(process.poll())
            self.assertEqual(second.probe(), original_epoch)
        finally:
            engine_a.close()
            engine_b.close()
        self.assertEqual(process.wait(timeout=8), 0)

    def test_disconnect_and_restart_reject_old_epoch(self):
        with self._start() as first:
            old_epoch = first.probe()
            first._process.kill()
            first._process.wait(timeout=5)
            with self._start() as second:
                process = second._process
                self.assertNotEqual(second.probe(), old_epoch)
                with self.assertRaises(DeskWeaveError) as caught:
                    first.probe()
                self.assertIn(caught.exception.code, {"AUTHENTICATION_FAILED", "TRANSPORT_CLOSED"})
                with self._start(workspace_id="ws-healthy") as healthy:
                    self.assertEqual(healthy.pid, second.pid)
                    second._channel.send({"kind": "agent.ping", "agentEpoch": old_epoch})
                    self._read_error("TRANSPORT_CLOSED", lambda: second._channel.receive(timeout=5))
                    self.assertEqual(healthy.probe(), second.epoch)
            self.assertEqual(process.wait(timeout=5), 0)
        with self._start() as third:
            self.assertEqual(third.probe(), third.epoch)

    def _launch_probe(self, engine_pid, accept):
        with (
            runtime_directory(self.local, self.runtime) as runtime,
            runtime.open(agent._OWNER, write=True, create=True) as owner,
            NativeListener(self.local) as listener,
        ):
            process = subprocess.Popen(
                [str(_BINARY)],
                stdin=subprocess.PIPE,
                stdout=owner,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            epoch = secrets.token_hex(32)
            secret = secrets.token_bytes(32)
            try:
                with process.stdin:
                    write_frame(
                        process.stdin,
                        {
                            "version": 1,
                            "enginePid": engine_pid,
                            "agentEpoch": epoch,
                            "address": listener.address,
                            "secret": list(secret),
                        },
                    )
                accept(listener, process, epoch, owner, runtime, secret)
                self.assertNotEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertTrue(agent.backend.acquire(owner))
            agent.backend.release(owner)

    def test_agent_rejects_endpoint_with_wrong_native_pid(self):
        def accept(listener, process, _epoch, _owner, _runtime, _secret):
            try:
                with listener._raw.accept(Budget(5)):
                    self.assertNotEqual(process.wait(timeout=5), 0)
            except DeskWeaveError as error:
                self.assertEqual(error.code, "TRANSPORT_CLOSED")

        self._launch_probe(os.getpid() + 1, accept)

    def _assert_bootstrap_rejected(self, rewrite):
        def accept(listener, process, epoch, owner, runtime, secret):
            with (
                ProcessBinding(
                    process.pid, self.agent_installation, instance_epoch=epoch
                ) as binding,
                listener.accept({process.pid: binding}) as channel,
            ):
                challenge = channel.receive(timeout=5)
                budget = Budget(5)
                claim = self.authority.registration(
                    self.local, binding, owner, runtime, bytes(challenge["nonce"]), budget
                )
                hello = {
                    "kind": "agent.hello",
                    "proof": list(agent._proof(secret, b"engine", os.getpid(), process.pid, epoch)),
                    "registration": claim.decode("utf-8"),
                    "signature": list(_sign(claim, budget)),
                }
                rewrite(hello)
                channel.send(hello)
                self.assertNotEqual(process.wait(timeout=5), 0)

        self._launch_probe(os.getpid(), accept)

    def test_agent_rejects_incorrect_bootstrap_proof(self):
        self._assert_bootstrap_rejected(lambda hello: hello.update(proof=[0] * 32))

    def test_agent_rejects_correct_proof_without_trusted_registration(self):
        self._assert_bootstrap_rejected(lambda hello: hello.update(signature=[0] * 64))

    def test_agent_rejects_signed_registration_for_another_owner(self):
        def rewrite(hello):
            claim = loads(hello["registration"])
            claim["owner"] = ["0", "0"]
            encoded = dumps(claim)
            hello.update(
                registration=encoded.decode("utf-8"), signature=list(_sign(encoded, Budget(5)))
            )

        self._assert_bootstrap_rejected(rewrite)

    def test_engine_rejects_incorrect_agent_proof_and_reaps_process(self):
        original_receive = NativeChannel.receive
        original_spawn = agent.AgentProcess._spawn
        children = []

        def receive(channel, **kwargs):
            value = original_receive(channel, **kwargs)
            if value.get("kind") == "agent.ready":
                value["proof"][0] ^= 1
            return value

        def spawn(instance, *args):
            process = original_spawn(instance, *args)
            children.append(process)
            return process

        with (
            patch.object(NativeChannel, "receive", receive),
            patch.object(agent.AgentProcess, "_spawn", spawn),
        ):
            self._read_error("AUTHENTICATION_FAILED", self._start)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        with self._start() as instance:
            self.assertEqual(instance.probe(), instance.epoch)

    def test_changed_installation_and_cancelled_start_do_not_spawn(self):
        changed = replace(
            self.agent_installation,
            executable=replace(self.agent_installation.executable, sha256="0" * 64),
        )
        cancel = threading.Event()
        cancel.set()
        with patch.object(agent.subprocess, "Popen") as spawn:
            changed_component = replace(
                self.agent_installation,
                components=(replace(self.agent_installation.components[0], sha256="0" * 64),),
            )
            for invalid in (changed, changed_component):
                self._read_error(
                    "AUTHENTICATION_FAILED",
                    lambda invalid=invalid: self._start(installation=invalid),
                )
            self._read_error("CANCELLED", lambda: self._start(cancel=cancel))
            self._read_error(
                "AUTHENTICATION_FAILED",
                lambda: self._start(authority=agent.AgentAuthority((), _SignatureRequest)),
            )
            spawn.assert_not_called()

    def test_listener_installation_scan_uses_startup_cancel(self):
        cancel = threading.Event()

        def scan(checkpoint):
            cancel.set()
            checkpoint()
            self.fail("监听器扫描没有使用启动取消信号。")

        def listener(local, **kwargs):
            with patch.object(local, "check", scan):
                return NativeListener(local, **kwargs)

        with (
            patch.object(agent, "NativeListener", listener),
            patch.object(agent.subprocess, "Popen") as spawn,
        ):
            self._read_error("CANCELLED", lambda: self._start(cancel=cancel))
            spawn.assert_not_called()

    def test_engine_retries_pending_launch_resource_on_close(self):
        pending = Mock()
        pending.close.side_effect = [OSError("first"), OSError("second"), None]
        engine = Engine(Workspace(self.root / "workspace", create=True))
        try:
            with patch.object(
                agent, "ProcessBinding", side_effect=InitializationError(pending, [OSError("open")])
            ):
                with self.assertRaises(InitializationError):
                    self._start_with_engine(engine)
            self.assertEqual(pending.close.call_count, 2)
        finally:
            engine.close()
        self.assertEqual(pending.close.call_count, 3)

    def test_engine_retries_registration_directory_on_close(self):
        resources = []
        closing = []

        def runtime(_local):
            directory = runtime_directory(self.local, self.runtime)
            resources.append(directory)
            if len(resources) == 2:
                original_close = directory.close

                def close():
                    closing.append(None)
                    if len(closing) <= 3:
                        raise OSError("registration directory temporarily unavailable")
                    original_close()

                directory.close = close
            return directory

        engine = Engine(Workspace(self.root / "workspace", create=True))
        try:
            with patch.object(agent, "_runtime", runtime):
                with self.assertRaises(InitializationError):
                    self._start_with_engine(engine)
            self.assertEqual(len(closing), 3)
        finally:
            engine.close()
        self.assertEqual(len(closing), 4)

    def test_engine_close_cancels_pending_signature(self):
        submitted = threading.Event()
        request = Mock()
        request.poll.return_value = None

        def submit(_claim, _budget):
            submitted.set()
            return request

        engine = Engine(Workspace(self.root / "workspace", create=True))
        authority = agent.AgentAuthority((self.local,), submit)
        with ThreadPoolExecutor(max_workers=2) as pool:
            starting = pool.submit(
                engine.start_desktop_agent, self.local, self.agent_installation, authority
            )
            try:
                self.assertTrue(submitted.wait(5))
            finally:
                pool.submit(engine.close).result(timeout=5)
            self._read_error("CANCELLED", lambda: starting.result(timeout=5))
        request.close.assert_called_once()

    def test_occupied_native_scope_returns_busy_even_with_another_file(self):
        with self._start() as first:
            alternate_runtime = self.root / "alternate-runtime"
            other = agent.AgentAuthority((self.local,), _SignatureRequest)
            with patch.object(
                agent,
                "_runtime",
                side_effect=lambda _: runtime_directory(self.local, alternate_runtime),
            ):
                self._read_error("DESKTOP_BUSY", lambda: self._start(authority=other))
            self.assertEqual(first.probe(), first.epoch)

    def test_installation_update_excludes_startup(self):
        with (
            self.authority.installation_update(self.local),
            patch.object(agent.subprocess, "Popen") as spawn,
        ):
            with (
                runtime_directory(self.local, self.runtime) as runtime,
                runtime.open(agent._STARTUP, write=True) as startup_lock,
            ):
                self.assertFalse(agent.backend.acquire(startup_lock))
            self._read_error(
                "DEADLINE_EXCEEDED",
                lambda: agent.AgentProcess(
                    self.local, self.agent_installation, self.authority, timeout=0.5
                ),
            )
            spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Linux 原生命名空间场景。")
    def test_linux_registration_rejects_another_namespace(self):
        with patch.object(agent, "_linux_namespaces", return_value=(("1", "2"), ("3", "4"))):
            self._read_error("AUTHENTICATION_FAILED", self._start)

    def _assert_contender_busy(self):
        context = multiprocessing.get_context("spawn")
        start, finish, results = context.Event(), context.Event(), context.Queue()
        start.set()
        contender = context.Process(target=_contender, args=(self.runtime, start, finish, results))
        try:
            contender.start()
            self.assertEqual(results.get(timeout=25), ("DESKTOP_BUSY", None))
        finally:
            finish.set()
            contender.join(8)
            if contender.is_alive():
                contender.kill()
                contender.join(5)
            exit_code = contender.exitcode
            if exit_code is not None:
                contender.close()
            results.close()
            results.join_thread()
        self.assertEqual(exit_code, 0)

    @unittest.skipIf(sys.platform == "win32", "POSIX 所有权文件替换场景。")
    def test_replaced_owner_cannot_be_registered_or_probed(self):
        with self._start() as first:
            path = self.runtime / agent._OWNER
            path.rename(self.runtime / "original.lock")
            with (
                runtime_directory(self.local, self.runtime) as runtime,
                runtime.open(agent._OWNER, write=True, create=True),
            ):
                pass
            with patch.object(agent.subprocess, "Popen") as spawn:
                self._read_error("RESOURCE_DENIED", self._start)
                spawn.assert_not_called()
            self._assert_contender_busy()
            self._read_error("RESOURCE_DENIED", first.probe)
            self.assertFalse(first.running)

    @unittest.skipIf(sys.platform == "win32", "POSIX 运行目录替换场景。")
    def test_replaced_runtime_directory_still_blocks_second_process(self):
        with self._start() as first:
            process = first._process
            self.runtime.rename(self.root / "moved-runtime")
            with (
                runtime_directory(self.local, self.runtime) as runtime,
                runtime.open(agent._OWNER, write=True, create=True),
            ):
                pass
            self._assert_contender_busy()
            self._read_error("RESOURCE_DENIED", first.probe)
            self.assertFalse(first.running)
        self.assertEqual(process.wait(timeout=5), 0)
        other = agent.AgentAuthority((self.local,), _SignatureRequest)
        with self._start(authority=other) as second:
            self.assertEqual(second.probe(), second.epoch)

    def test_tampered_discovery_and_incompatible_protocol_do_not_spawn_duplicate(self):
        with runtime_directory(self.local, self.runtime) as runtime:
            runtime.write(agent._DISCOVERY, dumps({"version": "2.0"}))
        with self._start() as recovered:
            self.assertTrue(recovered.created)
            self.assertEqual(recovered.probe(), recovered.epoch)
            other = agent.AgentAuthority((self.local,), _SignatureRequest)
            with runtime_directory(self.local, self.runtime) as runtime:
                original_bytes = runtime.read(agent._DISCOVERY)
                runtime.write(agent._DISCOVERY, dumps({"version": "2.0"}))
            with patch.object(agent.subprocess, "Popen") as spawn:
                self._read_error("PROTOCOL_UNSUPPORTED", lambda: self._start(authority=other))
                spawn.assert_not_called()
            with runtime_directory(self.local, self.runtime) as runtime:
                forged = loads(original_bytes)
                forged["peer"] = self.local.identity.as_dict()
                forged["peer"]["instanceEpoch"] = forged["agentEpoch"]
                runtime.write(agent._DISCOVERY, dumps(forged))
            with patch.object(agent.subprocess, "Popen") as spawn:
                self._read_error("AUTHENTICATION_FAILED", lambda: self._start(authority=other))
                spawn.assert_not_called()
            with runtime_directory(self.local, self.runtime) as runtime:
                runtime.write(agent._DISCOVERY, original_bytes)
            with self._start(authority=other, workspace_id="ws-attach-ok") as attached:
                self.assertFalse(attached.created)
                self.assertEqual(attached.pid, recovered.pid)
                self.assertEqual(attached.probe(), recovered.epoch)

    @unittest.skipUnless(sys.platform == "win32", "Windows 登录 SID 命名空间场景。")
    def test_windows_reused_session_number_does_not_reuse_acl_directory(self):
        identity = SimpleNamespace(user_id="fixture-user", os_session_id="3:0:1")
        original = SimpleNamespace(identity=identity, process=SimpleNamespace(logon_sid="logon-a"))
        renewed = SimpleNamespace(identity=identity, process=SimpleNamespace(logon_sid="logon-b"))
        self.assertNotEqual(agent._runtime_key(original), agent._runtime_key(renewed))
