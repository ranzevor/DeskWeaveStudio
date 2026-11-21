from __future__ import annotations

import os
import secrets
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from deskweave import agent
from deskweave.adapters.workspace import Workspace
from deskweave.domain.platform import (
    current_platform_tag,
    native_to_pixel,
    pixel_to_native,
    validate_desktop_capabilities,
    validate_desktop_topology,
    validate_record_start,
    validate_recording_source,
    validate_replay,
    validate_trajectory,
)
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import loads
from deskweave.transport.native import ProcessBinding
from tests.engine.test_agent import _BINARY, _SIGNER, _agent_installation, _SignatureRequest
from tests.engine.test_native import installation

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _ROOT / "examples" / "platform"


def _load_example(name: str):
    return loads((_EXAMPLES / name).read_bytes())


class PlatformContractTests(unittest.TestCase):
    def test_affine_transform_round_trips_and_rejects_singular_matrices(self):
        matrix = [0.5, 0.0, 0.0, 0.5, -1920.0, 100.0]
        nx, ny = pixel_to_native(matrix, 80.0, 200.0)
        self.assertAlmostEqual(nx, -1880.0, places=9)
        self.assertAlmostEqual(ny, 200.0, places=9)
        px, py = native_to_pixel(matrix, nx, ny)
        self.assertAlmostEqual(px, 80.0, places=9)
        self.assertAlmostEqual(py, 200.0, places=9)

        rotated = [0.0, 1.0, -1.0, 0.0, 1080.0, 0.0]
        rx, ry = pixel_to_native(rotated, 12.5, 34.25)
        bx, by = native_to_pixel(rotated, rx, ry)
        self.assertAlmostEqual(bx, 12.5, places=9)
        self.assertAlmostEqual(by, 34.25, places=9)

        with self.assertRaises(DeskWeaveError) as caught:
            pixel_to_native([1.0, 2.0, 2.0, 4.0, 0.0, 0.0], 1.0, 2.0)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        with self.assertRaises(DeskWeaveError) as caught:
            native_to_pixel([float("nan"), 0.0, 0.0, 1.0, 0.0, 0.0], 1.0, 2.0)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_golden_capability_examples(self):
        for valid in (
            "windows-capabilities.json",
            "macos-capabilities.json",
            "x11-capabilities.json",
            "wayland-capabilities.json",
            "macos-permissions.json",
            "compositor-unimplemented.json",
        ):
            with self.subTest(valid=valid):
                validate_desktop_capabilities(_load_example(valid))

        for invalid in (
            "invalid-os-backend.json",
            "invalid-partial-all-displays.json",
            "invalid-portal-passive-recording.json",
            "invalid-available-permission.json",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(DeskWeaveError):
                    validate_desktop_capabilities(_load_example(invalid))

    def test_golden_topology_source_and_trajectory_examples(self):
        validate_desktop_topology(_load_example("macos-topology.json"))
        with self.assertRaises(DeskWeaveError):
            validate_desktop_topology(_load_example("invalid-singular-transform.json"))

        validate_recording_source(_load_example("macos-source.json"))
        with self.assertRaises(DeskWeaveError):
            validate_recording_source(_load_example("invalid-selected-all-source.json"))

        for valid in (
            "macos-cross-display.json",
            "fractional-relative.json",
            "physical-v11.json",
        ):
            with self.subTest(valid=valid):
                validate_trajectory(_load_example(valid))

        for invalid in (
            "invalid-display-missing-id.json",
            "invalid-display-outside.json",
            "invalid-legacy-display.json",
            "invalid-display-physical-mode.json",
            "invalid-relative-unit.json",
            "invalid-v10-fractional.json",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(DeskWeaveError):
                    validate_trajectory(_load_example(invalid))

    def test_golden_replay_and_record_start_examples(self):
        for mapping_file, context_file, ok in (
            ("replay-original.json", "context-original.json", True),
            ("replay-cross-backend.json", "context-cross-backend.json", True),
            ("invalid-original-backend.json", "context-invalid-original-backend.json", False),
            ("invalid-relative-backend.json", "context-invalid-relative-backend.json", False),
            ("invalid-missing-input-map.json", "context-missing-input-map.json", False),
        ):
            with self.subTest(mapping=mapping_file):
                mapping = _load_example(mapping_file)
                context = _load_example(context_file)
                if ok:
                    validate_replay(mapping, context)
                else:
                    with self.assertRaises(DeskWeaveError):
                        validate_replay(mapping, context)

        for req_file, context_file, ok in (
            ("record-start-display.json", "context-record-start-display.json", True),
            ("invalid-record-permission.json", "context-invalid-record-permission.json", False),
            (
                "invalid-record-wayland-coverage.json",
                "context-invalid-record-wayland-coverage.json",
                False,
            ),
        ):
            with self.subTest(request=req_file):
                request = _load_example(req_file)
                context = _load_example(context_file)
                if ok:
                    validate_record_start(request, context)
                else:
                    with self.assertRaises(DeskWeaveError):
                        validate_record_start(request, context)

    def test_system_capabilities_without_agent_omits_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Workspace(Path(directory) / "ws", create=True))
            try:
                self.assertIn("system.capabilities", engine.methods)
                plain = engine.open_session("plain-client", set())
                capable = engine.open_session(
                    "cap-client",
                    set(),
                    capabilities={"platform-capabilities-v1"},
                )
                for sess in (plain, capable):
                    response = engine.dispatch(
                        sess.id,
                        {
                            "protocolVersion": "1.0",
                            "kind": "request",
                            "id": "req-" + sess.client_id,
                            "session": sess.id,
                            "workspace": engine.workspace.id,
                            "method": "system.capabilities",
                            "deadlineMs": 5000,
                            "params": {},
                        },
                    )
                    self.assertTrue(response["ok"], response)
                    result = response["result"]
                    self.assertEqual(result["platform"], current_platform_tag())
                    self.assertIn("engine", result["componentVersions"])
                    self.assertNotIn("agent", result["componentVersions"])
                    self.assertNotIn("desktop", result)
                    self.assertIn("time.wait", result["actions"])
                    self.assertGreater(result["limits"]["maxFrameBytes"], 0)
            finally:
                engine.close()


class PlatformAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not _BINARY.is_file() or not _SIGNER.is_file():
            if os.environ.get("DESKWEAVE_AGENT_TEST_REQUIRED") == "1":
                self.fail("缺少 Rust Desktop Agent 测试构建产物。")
            self.skipTest("尚未构建 Rust Desktop Agent 测试二进制。")
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.runtime = self.root / "agent-runtime"
        self.engine_installation = installation()
        self.local = self.stack.enter_context(
            ProcessBinding(os.getpid(), self.engine_installation, instance_epoch="engine-platform")
        )
        self.agent_installation = _agent_installation()
        self.authority = agent.AgentAuthority((self.local,), _SignatureRequest)
        self.stack.enter_context(patch.object(agent, "_runtime", self._runtime))

    def tearDown(self):
        self.stack.close()

    def _runtime(self, local):
        from deskweave.coordination.directory import runtime_directory

        return runtime_directory(local, self.runtime)

    def _call_engine(self, engine, session, method, params):
        return engine.dispatch(
            session.id,
            {
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-" + secrets.token_hex(6),
                "session": session.id,
                "workspace": engine.workspace.id,
                "method": method,
                "deadlineMs": 5000,
                "params": params,
            },
        )

    def test_system_capabilities_forwards_live_agent_capabilities_when_negotiated(self):
        engine_a = Engine(Workspace(self.root / "ws-a", create=True))
        engine_b = Engine(Workspace(self.root / "ws-b", create=True))
        authority_b = agent.AgentAuthority((self.local,), _SignatureRequest)
        try:
            first = engine_a.start_desktop_agent(
                self.local, self.agent_installation, self.authority, idle_grace=0.2
            )
            second = engine_b.start_desktop_agent(
                self.local, self.agent_installation, authority_b, idle_grace=0.2
            )
            self.assertEqual(first.pid, second.pid)
            self.assertNotEqual(first.session, second.session)

            # Without platform-capabilities-v1 negotiation: desktop field MUST be omitted.
            legacy_sess = engine_a.open_session("legacy", set(), capabilities={"contentRefsV1"})
            legacy_resp = self._call_engine(engine_a, legacy_sess, "system.capabilities", {})
            self.assertTrue(legacy_resp["ok"], legacy_resp)
            self.assertNotIn("desktop", legacy_resp["result"])

            # With platform-capabilities-v1 negotiation: live desktop summary is forwarded.
            cap_sess_a = engine_a.open_session(
                "studio-a", set(), capabilities={"contentRefsV1", "platform-capabilities-v1"}
            )
            cap_sess_b = engine_b.open_session(
                "studio-b", set(), capabilities={"platform-capabilities-v1"}
            )
            resp_a = self._call_engine(engine_a, cap_sess_a, "system.capabilities", {})
            resp_b = self._call_engine(engine_b, cap_sess_b, "system.capabilities", {})
            self.assertTrue(resp_a["ok"], resp_a)
            self.assertTrue(resp_b["ok"], resp_b)

            desktop_a = resp_a["result"]["desktop"]
            desktop_b = resp_b["result"]["desktop"]
            validate_desktop_capabilities(desktop_a)
            validate_desktop_capabilities(desktop_b)
            self.assertEqual(desktop_a["backend"], desktop_b["backend"])
            expected_os = (
                "windows"
                if sys.platform == "win32"
                else "macos"
                if sys.platform == "darwin"
                else "linux"
            )
            self.assertEqual(desktop_a["os"], expected_os)
            self.assertEqual(
                resp_a["result"]["componentVersions"]["agent"], desktop_a["backendVersion"]
            )
            # M1 native backends MUST NEVER forge "available" for unimplemented drivers.
            for name, status in desktop_a["features"].items():
                self.assertNotEqual(status["state"], "available", f"{name} unexpectedly available")
        finally:
            engine_a.close()
            engine_b.close()

    def test_agent_rpc_errors_and_envelope_isolation_preserve_live_sessions(self):
        engine_a = Engine(Workspace(self.root / "ws-a", create=True))
        engine_b = Engine(Workspace(self.root / "ws-b", create=True))
        authority_b = agent.AgentAuthority((self.local,), _SignatureRequest)
        try:
            first = engine_a.start_desktop_agent(
                self.local, self.agent_installation, self.authority, idle_grace=0.2
            )
            second = engine_b.start_desktop_agent(
                self.local, self.agent_installation, authority_b, idle_grace=0.2
            )

            # desktop.topology returns honest error in M1 without dropping connection.
            with self.assertRaises(DeskWeaveError) as topo_err:
                first.desktop_topology()
            self.assertIn(
                topo_err.exception.code, {"CAPABILITY_NOT_IMPLEMENTED", "SESSION_UNAVAILABLE"}
            )

            # Unimplemented Agent method returns METHOD_UNAVAILABLE without dropping connection.
            with self.assertRaises(DeskWeaveError) as method_err:
                first.call("lease.acquire", {})
            self.assertEqual(method_err.exception.code, "METHOD_UNAVAILABLE")

            # Extra params on desktop.capabilities returns INVALID_ARGUMENT cleanly.
            with self.assertRaises(DeskWeaveError) as param_err:
                first.call("desktop.capabilities", {"unexpected": 1})
            self.assertEqual(param_err.exception.code, "INVALID_ARGUMENT")

            # Raw cross-workspace spoofed request gets ACCESS_DENIED; healthy Engine B unaffected.
            first._channel.send(
                {
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "spoof-1",
                    "session": first.session,
                    "workspace": engine_b.workspace.id,
                    "method": "desktop.capabilities",
                    "deadlineMs": 5000,
                    "params": {},
                }
            )
            spoof_resp = first._channel.receive(timeout=5)
            self.assertFalse(spoof_resp["ok"])
            self.assertEqual(spoof_resp["error"]["code"], "ACCESS_DENIED")

            # Unsupported protocolVersion gets PROTOCOL_UNSUPPORTED cleanly.
            first._channel.send(
                {
                    "protocolVersion": "2.0",
                    "kind": "request",
                    "id": "ver-1",
                    "session": first.session,
                    "workspace": engine_a.workspace.id,
                    "method": "desktop.capabilities",
                    "deadlineMs": 5000,
                    "params": {},
                }
            )
            ver_resp = first._channel.receive(timeout=5)
            self.assertFalse(ver_resp["ok"])
            self.assertEqual(ver_resp["error"]["code"], "PROTOCOL_UNSUPPORTED")

            # Both sessions remain completely healthy after all structured errors.
            self.assertEqual(first.probe(), first.epoch)
            self.assertEqual(second.probe(), second.epoch)
            validate_desktop_capabilities(first.desktop_capabilities())
            validate_desktop_capabilities(second.desktop_capabilities())
        finally:
            engine_a.close()
            engine_b.close()


if __name__ == "__main__":
    unittest.main()
