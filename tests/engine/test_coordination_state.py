from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from deskweave.adapters.workspace import Workspace
from deskweave.coordination.authority import HostBinding, HostRegistration, StartupPlan
from deskweave.coordination.client import BindingOwner
from deskweave.coordination.server import ControlState, SharedRuntime
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.process import _wire as wire
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.native import Installation, InstalledFile
from deskweave.transport.native._common import Budget
from deskweave.transport.protocol import ConnectionPolicy, PeerIdentity


class SharedStateCase(unittest.TestCase):
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
        self.server, self.parent = self.peer(100), self.peer(101)
        self.authority = self.stack.enter_context(
            ConnectionAuthority(self.engine, self.server.identity)
        )
        self.policy = ConnectionPolicy({"workflow.read", "workflow.write"})
        self.host_binding = HostBinding(
            "fixture-authority",
            "root-host",
            ("volume", "file"),
            self.engine.workspace.id,
            self.installation.digest,
            self.parent.identity,
            self.policy,
            220.0,
            True,
        )
        plan = StartupPlan(
            "fixture-authority",
            ("volume", "file"),
            self.engine.workspace.id,
            self.installation.digest,
            (HostRegistration(self.host_binding, self.installation, b"x" * 32),),
        )
        listener = SimpleNamespace(local=self.server, address="business", close=lambda: None)
        host_listener = SimpleNamespace(address="control", close=lambda: None)
        with patch("deskweave.coordination.server.NativeListener", return_value=host_listener):
            self.runtime = SharedRuntime(
                listener,
                self.authority,
                self.parent,
                max_states=12,
                max_bytes=16384,
                identity=SimpleNamespace(key="fixture", object=("volume", "file")),
                runtime_directory=None,
                plan=plan,
                config={"deadline": time.monotonic() + 30, "idleGrace": 0.1},
            )
        self.stack.callback(self.runtime.registry.close)
        self.runtime.published.set()
        self.registry = self.runtime.registry

    def peer(self, pid, installation=None):
        installation = installation or self.installation
        return SimpleNamespace(
            identity=PeerIdentity(
                "windows",
                "fixture-user",
                "fixture-session",
                pid,
                "start",
                installation.digest,
                "instance",
            ),
            installation=installation,
            check=lambda checkpoint: checkpoint(),
            _check_process=lambda: None,
            close=Mock(),
        )

    def client(self, peer=None, owner=None):
        peer, owner = peer or self.parent, owner or self.runtime.root
        with patch("deskweave.process._worker.ProcessBinding", return_value=peer):
            credential = self.registry.add(
                peer.identity, peer.installation, self.policy, 100, Budget(5)
            )
        owner.clients.add(credential.client_id)
        self.runtime.owners[credential.client_id] = owner.binding.grant_id
        return credential

    def host(self, pid, *, parent=None, manager=False):
        peer = self.peer(pid)
        with patch("deskweave.process._worker.ProcessBinding", return_value=peer):
            credential = self.registry.add(
                peer.identity, peer.installation, self.policy, 100, Budget(5)
            )
        binding = replace(
            self.host_binding,
            grant_id="host-" + str(pid),
            peer=peer.identity,
            manager=manager,
            expires_at=200.0,
        )
        record = self.runtime.remember_host(binding, b"y" * 32, credential, parent)
        return record, peer

    def state(self, host=None):
        return ControlState(host or self.runtime.root, Mock())

    def dispatch(self, state, method, params):
        return self.runtime.dispatch_control(state, method, params, Budget(5))

    def begin(self, state, peer, *, host=False):
        params = {
            "peer": peer.identity.as_dict(),
            "installation": wire.manifest_header(peer.installation),
            "policy": self.policy.as_dict(),
            "lifetime": 100,
        }
        if host:
            params["manager"] = False
        return self.dispatch(state, "host.register.begin" if host else "register.begin", params)

    def test_all_dependent_permissions_expire_before_first_close(self):
        child, _ = self.host(102, parent=self.runtime.root.binding.grant_id)
        credentials = (self.client(), self.client(owner=child))
        other = self.authority.authorize(self.policy, lifetime=100)
        checked = []

        def close():
            for credential in credentials:
                with self.assertRaises(DeskWeaveError):
                    self.authority.describe(credential)
            self.assertEqual(self.authority.describe(other).policy, self.policy)
            checked.append(True)

        channel = Mock(close=close)
        self.runtime.root.channels.add(channel)
        self.runtime.revoke_host(self.runtime.root.binding.grant_id)
        self.assertEqual(checked, [True])
        self.assertEqual(self.runtime.hosts, {})
        self.assertEqual(self.runtime.owners, {})
        self.assertEqual(self.registry.grants, {})

    def test_revoke_failure_keeps_cleanup_owned_and_recovers_quota(self):
        peer = self.peer(102)
        credential = self.client(peer)
        connection = self.authority.accept(peer.identity)
        self.authority._connections[connection].client_id = credential.client_id
        original = connection.close
        with patch.object(connection, "close", side_effect=OSError("temporary close")):
            with self.assertRaises(BaseExceptionGroup):
                self.runtime.revoke_host(self.runtime.root.binding.grant_id)
            self.assertNotIn(credential.client_id, self.registry.grants)
            self.assertEqual(self.runtime.owners, {})
            self.assertTrue(self.registry.retry_authority)
            self.assertIn(peer.identity.process_id, self.registry.peers)
            peer.close.assert_not_called()
        original()
        self.assertEqual(self.registry.prune(), [])
        self.assertNotIn(peer.identity.process_id, self.registry.peers)
        peer.close.assert_called_once()
        self.assertEqual(self.registry.states, 4)

    def test_parent_process_departure_keeps_child_grant_and_expiry(self):
        child, _ = self.host(102, parent=self.runtime.root.binding.grant_id)
        self.parent._check_process = Mock(
            side_effect=DeskWeaveError("AUTHENTICATION_FAILED", "exited")
        )
        self.assertEqual(self.runtime.sweep_hosts(), [])
        self.assertTrue(self.runtime.root.departed)
        self.runtime.checked_host(child)
        self.assertEqual(self.runtime.pending_count, 1)
        self.assertNotIn(self.parent.identity.process_id, self.registry.peers)
        self.clock[0] = 200.0
        self.assertEqual(self.runtime.sweep_hosts(), [])
        self.assertEqual(self.runtime.hosts, {})
        self.assertEqual(self.runtime.pending_count, 0)

    def test_manager_cannot_revoke_ancestor_or_other_tree(self):
        child, _ = self.host(102, parent=self.runtime.root.binding.grant_id, manager=True)
        state = self.state(child)
        with self.assertRaises(DeskWeaveError) as caught:
            self.dispatch(state, "host.revoke", {"grantId": self.runtime.root.binding.grant_id})
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        self.runtime.checked_host(self.runtime.root)
        self.dispatch(state, "host.revoke", {"grantId": child.binding.grant_id})
        self.assertTrue(state.stop)
        self.runtime.checked_host(self.runtime.root)

    def test_interleaved_manifests_reserve_global_capacity_and_release_after_error(self):
        first = self.peer(102, replace(self.installation, macos_requirement="first"))
        second = self.peer(103, replace(self.installation, macos_requirement="second"))
        left, right = self.state(), self.state()
        self.assertTrue(self.begin(left, first)["needFiles"])
        self.assertTrue(self.begin(right, second)["needFiles"])
        self.assertEqual(self.runtime.pending_states, 4)
        self.dispatch(
            left, "register.files", {"files": [wire.file_value(first.installation.components[0])]}
        )
        committed_bytes = self.runtime.pending_bytes
        self.assertGreater(committed_bytes, 0)
        with patch.object(self.registry, "max_bytes", self.registry.bytes + committed_bytes):
            with self.assertRaises(DeskWeaveError) as caught:
                self.dispatch(
                    right,
                    "register.files",
                    {"files": [wire.file_value(second.installation.components[0])]},
                )
        self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")
        self.assertIsNone(right.pending)
        self.assertEqual(self.runtime.pending_states, 2)
        self.assertEqual(self.runtime.pending_bytes, left.reserved_bytes)
        with patch("deskweave.process._worker.ProcessBinding", return_value=first):
            result = self.dispatch(left, "register.commit", {})
        self.assertIn(wire.credential(result).client_id, self.registry.grants)
        self.assertEqual(
            (
                self.runtime.pending_states,
                self.runtime.pending_bytes,
                self.runtime.pending_metadata,
            ),
            (0, 0, 0),
        )
        self.assertTrue(self.begin(right, second)["needFiles"])
        self.runtime.clear_state(right)
        self.runtime.clear_state(right)
        self.assertEqual(
            (
                self.runtime.pending_states,
                self.runtime.pending_bytes,
                self.runtime.pending_metadata,
            ),
            (0, 0, 0),
        )

    def test_host_record_failure_rolls_back_registration(self):
        before = set(self.registry.grants)
        peer, state = self.peer(102), self.state()
        self.begin(state, peer, host=True)
        with (
            patch("deskweave.process._worker.ProcessBinding", return_value=peer),
            patch.object(self.runtime, "remember_host", side_effect=ValueError("host metadata")),
        ):
            with self.assertRaises(ValueError):
                self.dispatch(state, "host.register.commit", {})
        self.assertEqual(set(self.registry.grants), before)
        self.assertEqual(self.registry.states, 4)
        self.assertEqual(self.runtime.pending_hosts, 0)
        self.assertEqual(self.runtime.pending_metadata, 0)
        peer.close.assert_called_once()

    def test_control_close_failure_keeps_admission_until_retry(self):
        from deskweave.process._worker import Admission

        admission = Admission(self.registry)
        self.assertIsNotNone(admission.get(self.parent.identity.process_id))
        channel = Mock()
        channel.close.side_effect = [OSError("temporary close"), None]
        self.runtime.host_capacity.acquire()
        self.runtime.host_channels.add(channel)
        self.runtime.root.channels.add(channel)
        self.runtime.closing_channels.add(channel)
        self.runtime.failed_closes[channel] = (self.runtime.root, admission)
        self.assertEqual(len(self.runtime.retry_controls()), 1)
        self.assertEqual(self.registry.active_users, 1)
        self.assertIn(channel, self.runtime.host_channels)
        self.assertEqual(self.runtime.retry_controls(), [])
        self.assertEqual(self.registry.active_users, 0)
        self.assertNotIn(channel, self.runtime.host_channels)
        self.assertNotIn(channel, self.runtime.root.channels)

    def test_absolute_grant_deadline_is_checked_inside_mutation(self):
        before = set(self.authority._clients)
        for deadline in (99.0, 100.0):
            with self.subTest(deadline=deadline), self.assertRaises(DeskWeaveError):
                self.authority.authorize(self.policy, lifetime=60, not_after=deadline)
        self.assertEqual(set(self.authority._clients), before)
        credential = self.authority.authorize(self.policy, lifetime=60, not_after=110.0)
        self.assertEqual(self.authority.describe(credential).expires_at, 110.0)
        self.clock[0] = 109.0
        version = self.authority.revise(
            credential, self.policy, expected_version=1, lifetime=3600, not_after=110.0
        )
        self.assertEqual(version, 2)
        self.assertEqual(self.authority.describe(credential).expires_at, 110.0)
        self.clock[0] = 110.0
        with self.assertRaises(DeskWeaveError):
            self.authority.revise(
                credential, self.policy, expected_version=2, lifetime=3600, not_after=110.0
            )

    def test_last_activity_and_new_admission_share_exit_barrier(self):
        peer = self.registry.acquire_peer(self.parent.identity.process_id)
        self.assertFalse(self.registry.stop_when_idle())
        self.registry.release_peer(peer)
        self.assertTrue(self.registry.stop_when_idle())
        self.assertIsNone(self.registry.acquire_peer(self.parent.identity.process_id))

    def test_business_close_failure_retains_activity_and_capacity(self):
        from deskweave.process._worker import Admission

        admission = Admission(self.registry)
        self.assertIsNotNone(admission.get(self.parent.identity.process_id))
        channel = Mock()
        channel.close.side_effect = [OSError("temporary business close"), None]
        self.runtime.capacity.acquire()
        self.assertEqual(len(self.runtime.finish_connection(channel, admission)), 1)
        self.assertEqual(self.registry.active_users, 1)
        self.assertFalse(self.registry.stop_when_idle())
        self.assertEqual(self.runtime.retry_connections(), [])
        self.assertEqual(self.registry.active_users, 0)
        self.assertEqual(self.runtime.failed_channels, {})

    def test_revoke_retry_is_idempotent_and_other_host_remains_protected(self):
        owned = self.client()
        child, _ = self.host(102, parent=self.runtime.root.binding.grant_id)
        other = self.client(owner=child)
        state = self.state()
        params = {"credential": wire.credential_value(owned)}
        self.assertEqual(self.dispatch(state, "revoke", params), {})
        self.assertEqual(self.dispatch(state, "revoke", params), {})
        with self.assertRaises(DeskWeaveError) as malformed:
            self.dispatch(state, "revoke", {**params, "extra": True})
        self.assertEqual(malformed.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(DeskWeaveError) as caught:
            self.dispatch(state, "revoke", {"credential": wire.credential_value(other)})
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        self.assertEqual(self.authority.describe(other).policy, self.policy)

    def test_pid_reuse_after_parent_exit_gets_new_binding(self):
        replacement = self.peer(self.parent.identity.process_id)
        replacement.identity = replace(replacement.identity, process_started="new-instance")
        with patch("deskweave.process._worker.ProcessBinding") as constructor:
            with self.assertRaises(DeskWeaveError) as caught:
                self.registry.add(
                    replacement.identity, self.installation, self.policy, 60, Budget(5)
                )
            self.assertEqual(caught.exception.code, "AUTHENTICATION_FAILED")
            constructor.assert_not_called()
        self.runtime.checked_host(self.runtime.root)
        self.runtime.depart_host(self.runtime.root)
        with patch(
            "deskweave.process._worker.ProcessBinding", return_value=replacement
        ) as constructor:
            credential = self.registry.add(
                replacement.identity, self.installation, self.policy, 60, Budget(5)
            )
            constructor.assert_called_once()
        self.assertEqual(
            self.registry.selected(credential).peer.binding.identity, replacement.identity
        )
        self.assertEqual(self.registry.states, 6)
        self.registry.revoke(credential)
        replacement.close.assert_called_once()
        self.assertEqual(self.registry.states, 4)


class BindingOwnerCase(unittest.TestCase):
    def test_shared_binding_closes_after_last_channel_and_retries_failure(self):
        binding = Mock()
        binding.close.side_effect = [OSError("close failed"), None]
        owner = BindingOwner(binding)
        _, first = owner.acquire()
        _, second = owner.acquire()
        first()
        owner.close()
        binding.close.assert_not_called()
        with self.assertRaises(OSError):
            second()
        self.assertEqual(owner.references, 0)
        self.assertFalse(owner.closed)
        second()
        self.assertTrue(owner.closed)
        self.assertEqual(binding.close.call_count, 2)


class OutboundResourceCase(unittest.TestCase):
    def test_failed_connection_is_retained_until_host_close_succeeds(self):
        from deskweave.coordination.client import AttachedControl
        from deskweave.transport.native._common import InitializationError

        for stage in ("native", "handshake"):
            with self.subTest(stage=stage):
                peer = Mock()
                owner = BindingOwner(peer)
                _, release = owner.acquire()
                control_channel = Mock(close=release)
                control = AttachedControl(Mock(), owner, control_channel, "endpoint", "workspace")
                channel = Mock()
                channel.close.side_effect = OSError("close failed")
                try:
                    with (
                        patch.object(control, "issue", return_value=Mock()),
                        patch("deskweave.process.control.connect", return_value=channel) as connect,
                        patch(
                            "deskweave.process.control.NativeClient",
                            side_effect=ValueError("handshake failed"),
                        ),
                    ):
                        if stage == "native":
                            connect.side_effect = InitializationError(
                                channel, [ValueError("peer failed"), OSError("close failed")]
                            )
                        with self.assertRaises(InitializationError):
                            control.connect(Mock(), Mock())
                    self.assertEqual(len(control._failed_connections), 1)
                    with self.assertRaises(BaseExceptionGroup):
                        control.close()
                    self.assertEqual(owner.references, 1)
                    peer.close.assert_not_called()
                    channel.close.side_effect = None
                    control.close()
                    self.assertEqual(control._failed_connections, set())
                    self.assertEqual(owner.references, 0)
                    self.assertTrue(owner.closed)
                    peer.close.assert_called_once()
                finally:
                    channel.close.side_effect = None
                    control.close()

    def test_host_close_cancels_inflight_connection_initialization(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from deskweave.coordination.client import AttachedControl

        peer = Mock()
        owner = BindingOwner(peer)
        _, release = owner.acquire()
        control = AttachedControl(Mock(), owner, Mock(close=release), "endpoint", "workspace")
        entered = threading.Event()

        def connect(*_args, cancel, **_kwargs):
            entered.set()
            while not cancel.wait(0.025):
                pass
            raise DeskWeaveError("CANCELLED", "connection cancelled")

        try:
            with (
                patch.object(control, "issue", return_value=Mock()),
                patch("deskweave.process.control.connect", connect),
                ThreadPoolExecutor() as pool,
            ):
                pending = pool.submit(control.connect, Mock(), Mock())
                self.assertTrue(entered.wait(5))
                control.close()
                with self.assertRaises(DeskWeaveError) as caught:
                    pending.result(timeout=5)
                self.assertEqual(caught.exception.code, "CANCELLED")
            self.assertEqual(owner.references, 0)
            self.assertTrue(owner.closed)
            self.assertEqual(control._failed_connections, set())
        finally:
            control.close()

    def test_native_connect_returns_failed_raw_resource(self):
        from deskweave.transport.native import ProcessBinding
        from deskweave.transport.native import channel as native
        from deskweave.transport.native._common import InitializationError

        peer = object.__new__(ProcessBinding)
        peer.check = Mock()
        raw = Mock()
        raw.close.side_effect = OSError("raw close failed")
        with (
            patch.object(native, "local_binding"),
            patch.object(native.backend, "connect", return_value=raw),
            patch.object(native, "NativeChannel", side_effect=ValueError("peer invalid")),
        ):
            with self.assertRaises(InitializationError) as caught:
                native.connect("endpoint", Mock(), peer)
        self.assertIs(caught.exception.resource, raw)
        raw.close.side_effect = None
        caught.exception.resource.close()


if __name__ == "__main__":
    unittest.main()
