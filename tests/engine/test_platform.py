from __future__ import annotations

import copy
import hashlib
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
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

        with self.assertRaises(DeskWeaveError) as caught:
            pixel_to_native({"a": 1, "b": 0, "c": 0, "d": 1, "tx": 0, "ty": 0}, 1.0, 2.0)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        with self.assertRaises(DeskWeaveError) as caught:
            pixel_to_native([1e13, 0.0, 0.0, 1.0, 0.0, 0.0], 1.0, 2.0)
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

    def test_validate_replay_and_record_start_enforce_context_schemas(self):
        mapping = _load_example("replay-original.json")
        bad_replay_ctx = copy.deepcopy(_load_example("context-original.json"))
        bad_replay_ctx["trajectory"] = _load_example("invalid-v10-fractional.json")
        with self.assertRaises(DeskWeaveError) as caught:
            validate_replay(mapping, bad_replay_ctx)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        bad_profile_ctx = copy.deepcopy(_load_example("context-original.json"))
        bad_profile_ctx["inputProfile"] = {"backend": "unknown-backend", "relativeUnit": "none"}
        with self.assertRaises(DeskWeaveError) as caught:
            validate_replay(mapping, bad_profile_ctx)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        request = _load_example("record-start-display.json")
        bad_record_ctx = copy.deepcopy(_load_example("context-record-start-display.json"))
        bad_record_ctx["capabilities"]["backendVersion"] = ""
        with self.assertRaises(DeskWeaveError) as caught:
            validate_record_start(request, bad_record_ctx)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        bad_record_keys = copy.deepcopy(_load_example("context-record-start-display.json"))
        del bad_record_keys["permissions"]
        with self.assertRaises(DeskWeaveError) as caught:
            validate_record_start(request, bad_record_keys)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_linux_session_env_sanitizer_filters_untrusted_values(self):
        clean = agent._sanitize_linux_session_env(
            {
                "XDG_SESSION_TYPE": "wayland",
                "XDG_SESSION_ID": "c12_a-b",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "WAYLAND_DISPLAY": "wayland-0",
                "DESKWEAVE_WAYLAND_BACKEND": "Compositor",
                "DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET": "dw-compositor-0",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus,guid=abc123",
                "DISPLAY": "unix/:1.0",
            }
        )
        self.assertEqual(
            clean,
            {
                "XDG_SESSION_TYPE": "wayland",
                "XDG_SESSION_ID": "c12_a-b",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "WAYLAND_DISPLAY": "wayland-0",
                "DESKWEAVE_WAYLAND_BACKEND": "compositor",
                "DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET": "dw-compositor-0",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus,guid=abc123",
                "DISPLAY": "unix/:1.0",
            },
        )

        x11_clean = agent._sanitize_linux_session_env({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
        self.assertEqual(x11_clean, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

        # Valid multi-address D-Bus lists preserve safe unix:path entries in order.
        multi_dbus = agent._sanitize_linux_session_env(
            {
                "DBUS_SESSION_BUS_ADDRESS": (
                    "unix:path=/run/user/1000/a;unix:path=/run/user/1000/b,guid=1234"
                )
            }
        )
        self.assertEqual(
            multi_dbus,
            {
                "DBUS_SESSION_BUS_ADDRESS": (
                    "unix:path=/run/user/1000/a;unix:path=/run/user/1000/b,guid=1234"
                )
            },
        )
        mixed_multi = agent._sanitize_linux_session_env(
            {
                "DBUS_SESSION_BUS_ADDRESS": (
                    "unix:abstract=/tmp/dbus-1;unix:path=/run/user/1000/fallback"
                )
            }
        )
        self.assertEqual(
            mixed_multi,
            {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/fallback"},
        )

        # Unset or empty DBUS_SESSION_BUS_ADDRESS remains unset (allows $XDG_RUNTIME_DIR/bus).
        self.assertEqual(agent._sanitize_linux_session_env({"DBUS_SESSION_BUS_ADDRESS": ""}), {})

        # Explicit unsupported or unsafe DBUS_SESSION_BUS_ADDRESS preserves explicit-set state
        # via an inert sentinel so the agent never silently falls back to $XDG_RUNTIME_DIR/bus.
        dirty = agent._sanitize_linux_session_env(
            {
                "XDG_SESSION_TYPE": "wayland;id",
                "XDG_SESSION_ID": "../1",
                "XDG_RUNTIME_DIR": "/run/../tmp",
                "WAYLAND_DISPLAY": "../dbus",
                "DESKWEAVE_WAYLAND_BACKEND": "x11",
                "DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET": "../comp",
                "DBUS_SESSION_BUS_ADDRESS": "tcp:host=127.0.0.1,port=1234",
                "DISPLAY": "127.0.0.1:0",
            }
        )
        self.assertEqual(dirty, {"DBUS_SESSION_BUS_ADDRESS": "unsupported:"})

        dirty_screen = agent._sanitize_linux_session_env(
            {
                "XDG_SESSION_TYPE": "x11/bad",
                "WAYLAND_DISPLAY": "sub/wayland-0",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/../tmp/bus",
                "DISPLAY": ":0.garbage",
            }
        )
        self.assertEqual(dirty_screen, {"DBUS_SESSION_BUS_ADDRESS": "unsupported:"})

        for explicit_unsupported in (
            "unix:abstract=/tmp/dbus-abc",
            "unix:abstract=/tmp/a;tcp:host=127.0.0.1,port=1234",
            "unix:path=/run/user/1000/a,path=/run/user/1000/b",
            "unix:path=/run/user/1000/\x07bus",
            "unix:path=/run/user/1000/good;unix:path=/run/../evil",
            "   ",
        ):
            with self.subTest(dbus=explicit_unsupported):
                sanitized = agent._sanitize_linux_session_env(
                    {"DBUS_SESSION_BUS_ADDRESS": explicit_unsupported}
                )
                self.assertEqual(sanitized, {"DBUS_SESSION_BUS_ADDRESS": "unsupported:"})

    def test_record_start_bidirectional_topology_mode_v11_window_source_and_float_rotation(self):
        # 1. Float integer rotation (90.0) accepted, fractional (90.5) rejected.
        topo_ok = copy.deepcopy(_load_example("macos-topology.json"))
        topo_ok["displays"][0]["rotation"] = 90.0
        validate_desktop_topology(topo_ok)

        topo_bad = copy.deepcopy(_load_example("macos-topology.json"))
        topo_bad["displays"][0]["rotation"] = 90.5
        with self.assertRaises(DeskWeaveError) as caught:
            validate_desktop_topology(topo_bad)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        # 2. Display-local window source requires full available display set in topology.
        window_full = copy.deepcopy(_load_example("macos-source.json"))
        window_full["scope"] = "window"
        validate_recording_source(window_full)

        window_selected_cov = copy.deepcopy(window_full)
        window_selected_cov["topology"]["coverage"] = "selected"
        validate_recording_source(window_selected_cov)

        window_partial = copy.deepcopy(window_full)
        window_partial["displayIds"] = ["main"]
        with self.assertRaises(DeskWeaveError) as caught:
            validate_recording_source(window_partial)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        # Valid neighbor: single display scope selects one display.
        single_display = copy.deepcopy(_load_example("macos-source.json"))
        single_display["scope"] = "display"
        single_display["displayIds"] = ["main"]
        validate_recording_source(single_display)

        # 3. Bidirectional topology <-> coordinateMode and trajectory 1.1 admission matrix.
        local_ctx = copy.deepcopy(_load_example("context-record-start-display.json"))
        base_req = copy.deepcopy(_load_example("record-start-display.json"))

        # displayLocalPixels + physical -> rejected.
        req_phys = {**base_req, "coordinateMode": "physical"}
        with self.assertRaises(DeskWeaveError):
            validate_record_start(req_phys, local_ctx)

        # displayLocalPixels + display / event / relative with 1.1 -> valid neighbor cases.
        for mode in ("display", "event", "relative"):
            validate_record_start({**base_req, "coordinateMode": mode}, local_ctx)

        # displayLocalPixels without 1.1 -> rejected for display, event, AND relative!
        local_v10_ctx = copy.deepcopy(local_ctx)
        local_v10_ctx["capabilities"]["trajectoryVersions"] = ["1.0"]
        for mode in ("display", "event", "relative"):
            with self.subTest(local_v10_mode=mode):
                with self.assertRaises(DeskWeaveError):
                    validate_record_start({**base_req, "coordinateMode": mode}, local_v10_ctx)

        # desktopPhysicalPixels context with trajectory 1.0 only.
        physical_caps = copy.deepcopy(local_ctx["capabilities"])
        physical_caps["os"] = "windows"
        physical_caps["backend"] = "windows-win32"
        physical_caps["trajectoryVersions"] = ["1.0"]
        physical_ctx = {
            "targetToken": base_req["targetToken"],
            "targetKind": "desktop",
            "permissions": ["record"],
            "capabilities": physical_caps,
            "topology": {
                "version": 7,
                "coordinateSpace": "desktopPhysicalPixels",
                "virtualBounds": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "displays": [
                    {
                        "displayId": "main",
                        "name": "主屏",
                        "outputNames": ["DP-1"],
                        "bounds": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                        "rotation": 0,
                        "dpiX": 96,
                        "dpiY": 96,
                        "primary": True,
                    }
                ],
            },
        }

        # desktopPhysicalPixels + display -> rejected!
        with self.assertRaises(DeskWeaveError):
            validate_record_start({**base_req, "coordinateMode": "display"}, physical_ctx)

        # desktopPhysicalPixels + physical / event / relative with 1.0 only -> valid neighbor cases!
        for mode in ("physical", "event", "relative"):
            with self.subTest(physical_v10_mode=mode):
                validate_record_start({**base_req, "coordinateMode": mode}, physical_ctx)

    def test_system_capabilities_bounds_desktop_lock_wait_by_deadline_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Workspace(Path(directory) / "ws", create=True))
            try:
                sess_short = engine.open_session(
                    "lock-short", set(), capabilities={"platform-capabilities-v1"}
                )
                sess_revoked = engine.open_session(
                    "lock-revoked", set(), capabilities={"platform-capabilities-v1"}
                )

                # Hold _desktop_lock (simulating slow start_desktop_agent) for 300ms.
                locked = threading.Event()
                unlock = threading.Event()

                def _hold_lock():
                    with engine._desktop_lock:
                        locked.set()
                        unlock.wait(2)

                holder = threading.Thread(target=_hold_lock, daemon=True)
                holder.start()
                self.assertTrue(locked.wait(2))
                try:
                    # 1. Caller with 40ms deadline must time out promptly, not wait 300ms.
                    started = time.monotonic()
                    short_resp = engine.dispatch(
                        sess_short.id,
                        {
                            "protocolVersion": "1.0",
                            "kind": "request",
                            "id": "req-lock-short",
                            "session": sess_short.id,
                            "workspace": engine.workspace.id,
                            "method": "system.capabilities",
                            "deadlineMs": 40,
                            "params": {},
                        },
                    )
                    elapsed = time.monotonic() - started
                    self.assertFalse(short_resp["ok"])
                    self.assertEqual(short_resp["error"]["code"], "DEADLINE_EXCEEDED")
                    self.assertLess(elapsed, 0.25)

                    # 2. Revoking a caller blocked on _desktop_lock fails while lock stays held.
                    revoked_out = {}
                    entered_query = threading.Event()
                    orig_query = engine._query_desktop_capabilities

                    def _watched_query(deadline, authorize):
                        entered_query.set()
                        return orig_query(deadline, authorize)

                    engine._query_desktop_capabilities = _watched_query

                    def _run_revoked():
                        revoked_out["resp"] = engine.dispatch(
                            sess_revoked.id,
                            {
                                "protocolVersion": "1.0",
                                "kind": "request",
                                "id": "req-lock-revoked",
                                "session": sess_revoked.id,
                                "workspace": engine.workspace.id,
                                "method": "system.capabilities",
                                "deadlineMs": 5000,
                                "params": {},
                            },
                        )

                    worker = threading.Thread(target=_run_revoked, daemon=True)
                    worker.start()
                    self.assertTrue(entered_query.wait(2))
                    engine.revoke_client("lock-revoked")
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(revoked_out["resp"]["ok"])
                    self.assertEqual(revoked_out["resp"]["error"]["code"], "ACCESS_DENIED")
                finally:
                    unlock.set()
                    holder.join(timeout=2)
            finally:
                engine.close()

    def test_system_capabilities_isolates_client_timeout_revocation_and_long_backend_version(self):
        entered = threading.Event()
        release = threading.Event()
        long_version = "v" * 72
        caps = copy.deepcopy(_load_example("windows-capabilities.json"))
        caps["backendVersion"] = long_version

        class _StubAgent:
            def __init__(self):
                self.running = True
                self.closed = False
                self.calls = 0

            def desktop_capabilities(self, *, timeout=10, cancel=None):
                self.calls += 1
                entered.set()
                if cancel is not None:
                    while not release.is_set():
                        if cancel.wait(0.01):
                            raise DeskWeaveError("CANCELLED", "cancelled")
                else:
                    release.wait(timeout)
                return copy.deepcopy(caps)

            def close(self):
                self.closed = True
                self.running = False

        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Workspace(Path(directory) / "ws", create=True))
            stub = _StubAgent()
            engine._desktop_agent = stub
            try:
                sess_short = engine.open_session(
                    "short-client", set(), capabilities={"platform-capabilities-v1"}
                )
                sess_revoked = engine.open_session(
                    "revoked-client", set(), capabilities={"platform-capabilities-v1"}
                )
                sess_good = engine.open_session(
                    "good-client", set(), capabilities={"platform-capabilities-v1"}
                )

                # Short-deadline caller times out without closing shared Agent.
                short_resp = engine.dispatch(
                    sess_short.id,
                    {
                        "protocolVersion": "1.0",
                        "kind": "request",
                        "id": "req-short",
                        "session": sess_short.id,
                        "workspace": engine.workspace.id,
                        "method": "system.capabilities",
                        "deadlineMs": 250,
                        "params": {},
                    },
                )
                self.assertFalse(short_resp["ok"])
                self.assertEqual(short_resp["error"]["code"], "DEADLINE_EXCEEDED")
                self.assertTrue(entered.wait(2))
                self.assertTrue(stub.running)
                self.assertFalse(stub.closed)

                in_flight = engine._desktop_caps_future
                self.assertIsNotNone(in_flight)
                waiter_entered = threading.Event()
                orig_result = in_flight.result

                def _watched_result(timeout=None):
                    waiter_entered.set()
                    return orig_result(timeout=timeout)

                in_flight.result = _watched_result

                # Revoking a waiting client fails only that client promptly; Agent stays alive.
                revoked_out = {}

                def _run_revoked():
                    revoked_out["resp"] = engine.dispatch(
                        sess_revoked.id,
                        {
                            "protocolVersion": "1.0",
                            "kind": "request",
                            "id": "req-revoked",
                            "session": sess_revoked.id,
                            "workspace": engine.workspace.id,
                            "method": "system.capabilities",
                            "deadlineMs": 5000,
                            "params": {},
                        },
                    )

                worker = threading.Thread(target=_run_revoked, daemon=True)
                worker.start()
                self.assertTrue(waiter_entered.wait(2))
                engine.revoke_client("revoked-client")
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(revoked_out["resp"]["ok"])
                self.assertEqual(revoked_out["resp"]["error"]["code"], "ACCESS_DENIED")
                self.assertTrue(stub.running)
                self.assertFalse(stub.closed)

                # Attach healthy caller to the same in-flight query before releasing it.
                waiter_entered.clear()
                good_out = {}

                def _run_good():
                    good_out["resp"] = engine.dispatch(
                        sess_good.id,
                        {
                            "protocolVersion": "1.0",
                            "kind": "request",
                            "id": "req-good",
                            "session": sess_good.id,
                            "workspace": engine.workspace.id,
                            "method": "system.capabilities",
                            "deadlineMs": 5000,
                            "params": {},
                        },
                    )

                good_worker = threading.Thread(target=_run_good, daemon=True)
                good_worker.start()
                self.assertTrue(waiter_entered.wait(2))
                release.set()
                good_worker.join(timeout=5)
                self.assertFalse(good_worker.is_alive())
                good_resp = good_out["resp"]
                self.assertTrue(good_resp["ok"], good_resp)
                self.assertEqual(good_resp["result"]["desktop"]["backendVersion"], long_version)
                self.assertNotIn("agent", good_resp["result"]["componentVersions"])
                self.assertEqual(stub.calls, 1)

                # If Thread.start fails, _desktop_caps_future MUST be cleared for future callers.
                with (
                    patch.object(
                        threading.Thread, "start", side_effect=RuntimeError("thread exhausted")
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    engine.dispatch(
                        sess_good.id,
                        {
                            "protocolVersion": "1.0",
                            "kind": "request",
                            "id": "req-thread-fail",
                            "session": sess_good.id,
                            "workspace": engine.workspace.id,
                            "method": "system.capabilities",
                            "deadlineMs": 5000,
                            "params": {},
                        },
                    )
                self.assertIsNone(engine._desktop_caps_future)
                self.assertIsNone(engine._desktop_caps_agent)
                recovered = engine.dispatch(
                    sess_good.id,
                    {
                        "protocolVersion": "1.0",
                        "kind": "request",
                        "id": "req-recovered",
                        "session": sess_good.id,
                        "workspace": engine.workspace.id,
                        "method": "system.capabilities",
                        "deadlineMs": 5000,
                        "params": {},
                    },
                )
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(stub.calls, 2)
            finally:
                release.set()
                engine.close()

    def test_agent_call_validates_upstream_error_and_payload_atomically(self):
        class _FakeChannel:
            def __init__(self, reply_fn):
                self._reply_fn = reply_fn
                self.sent = None
                self.closed = False

            def send(self, value, *, budget):
                self.sent = value

            def receive(self, *, budget):
                return self._reply_fn(self.sent["id"])

            def close(self):
                self.closed = True

        def _make_fake_agent(reply_fn):
            proc = object.__new__(agent.AgentProcess)
            proc.session = "0" * 32
            proc.workspace_id = "ws-test"
            proc._stopping = threading.Event()
            proc._io = threading.RLock()
            proc._runtime_dir = object()
            proc._owner_identity = ("dev", "ino")
            proc._channel = _FakeChannel(reply_fn)
            proc._signature = proc._registration_dir = proc._listener = None
            proc._process = proc._peer = proc._owner = None
            proc._published = False
            proc._abort = lambda _error: proc._channel.close()
            return proc

        # Null details in error body violates message.schema.json -> rejected & channel closed.
        null_details = _make_fake_agent(
            lambda rid: {
                "protocolVersion": "1.0",
                "kind": "response",
                "replyTo": rid,
                "ok": False,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "bad",
                    "retry": "never",
                    "details": None,
                },
            }
        )
        with (
            patch.object(agent, "_check_owner", return_value=None),
            self.assertRaises(DeskWeaveError) as caught,
        ):
            null_details.call("desktop.capabilities", {})
        self.assertEqual(caught.exception.code, "AUTHENTICATION_FAILED")
        self.assertTrue(null_details._channel.closed)

        # Invalid capabilities payload from upstream -> INTERNAL_ERROR & channel closed.
        bad_caps = _make_fake_agent(
            lambda rid: {
                "protocolVersion": "1.0",
                "kind": "response",
                "replyTo": rid,
                "ok": True,
                "result": {"os": "windows"},
            }
        )
        with (
            patch.object(agent, "_check_owner", return_value=None),
            self.assertRaises(DeskWeaveError) as caught,
        ):
            bad_caps.desktop_capabilities()
        self.assertEqual(caught.exception.code, "INTERNAL_ERROR")
        self.assertTrue(bad_caps._channel.closed)


class PlatformAgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not _BINARY.is_file() or not _SIGNER.is_file():
            if os.environ.get("DESKWEAVE_AGENT_TEST_REQUIRED") == "1":
                self.fail("缺少 Rust Desktop Agent 测试构建产物。")
            self.skipTest("尚未构建 Rust Desktop Agent 测试二进制。")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.runtime = self.root / "agent-runtime"
        self.engine_installation = installation()
        self.local = self.stack.enter_context(
            ProcessBinding(os.getpid(), self.engine_installation, instance_epoch="engine-platform")
        )
        self.agent_installation = _agent_installation()
        self.stack.enter_context(patch.object(agent, "_runtime", self._runtime))
        scope = hashlib.sha256(os.fsencode(self.runtime)).hexdigest()
        self.stack.enter_context(patch.object(agent, "_scope", return_value=scope))
        self.authority = agent.AgentAuthority((self.local,), _SignatureRequest)

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
        process = None
        try:
            first = engine_a.start_desktop_agent(
                self.local, self.agent_installation, self.authority, idle_grace=0.2
            )
            process = first._process
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
            if process is not None:
                self.assertEqual(process.wait(timeout=5), 0)

    def test_agent_rpc_errors_and_envelope_isolation_preserve_live_sessions(self):
        engine_a = Engine(Workspace(self.root / "ws-a", create=True))
        engine_b = Engine(Workspace(self.root / "ws-b", create=True))
        authority_b = agent.AgentAuthority((self.local,), _SignatureRequest)
        process = None
        try:
            first = engine_a.start_desktop_agent(
                self.local, self.agent_installation, self.authority, idle_grace=0.2
            )
            process = first._process
            second = engine_b.start_desktop_agent(
                self.local, self.agent_installation, authority_b, idle_grace=0.2
            )

            # Local bad/oversized params MUST fail before channel I/O, keeping Agent alive.
            with self.assertRaises(DeskWeaveError) as oversized:
                first.call("desktop.capabilities", {"pad": "x" * 1_100_000})
            self.assertEqual(oversized.exception.code, "LIMIT_EXCEEDED")
            with self.assertRaises(DeskWeaveError) as bad_param:
                first.call("desktop.capabilities", {"bad": object()})
            self.assertEqual(bad_param.exception.code, "INVALID_ARGUMENT")
            self.assertTrue(first.running)

            # Huge finite timeout clamps cleanly without OverflowError.
            validate_desktop_capabilities(first.desktop_capabilities(timeout=1e308))

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
            if process is not None:
                self.assertEqual(process.wait(timeout=5), 0)

    @unittest.skipUnless(sys.platform == "linux", "Linux Wayland/Portal live transitions")
    def test_linux_live_wayland_compositor_portal_selection_and_dynamic_generation(self):
        with tempfile.TemporaryDirectory(prefix="dw-wl-") as raw_dir:
            priv_dir = Path(raw_dir)
            os.chmod(priv_dir, 0o700)
            wayland_path = priv_dir / "wayland-test-0"
            runtime_bus_path = priv_dir / "bus"
            custom_bus_path = priv_dir / "custom-bus"
            compositor_sock_path = priv_dir / "dw-compositor-0"

            def _bind_unix(path: Path) -> socket.socket:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(str(path))
                sock.listen(1)
                return sock

            def _atomic_replace_unix(target: Path, old_sock: socket.socket) -> socket.socket:
                staged = target.parent / f".{target.name}.new-{secrets.token_hex(4)}"
                new_sock = _bind_unix(staged)
                try:
                    os.replace(staged, target)
                except Exception:
                    new_sock.close()
                    if staged.exists():
                        staged.unlink()
                    raise
                old_sock.close()
                return new_sock

            @contextmanager
            def _patched_session_env(overrides: dict[str, str]):
                with patch.dict(os.environ, {}, clear=False):
                    for key in (
                        "DISPLAY",
                        "XDG_SESSION_ID",
                        "XDG_SESSION_TYPE",
                        "XDG_RUNTIME_DIR",
                        "WAYLAND_DISPLAY",
                        "DBUS_SESSION_BUS_ADDRESS",
                        "DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET",
                        "DESKWEAVE_WAYLAND_BACKEND",
                    ):
                        os.environ.pop(key, None)
                    os.environ.update(overrides)
                    yield

            base_env = {
                "XDG_SESSION_TYPE": "wayland",
                "XDG_RUNTIME_DIR": str(priv_dir),
                "WAYLAND_DISPLAY": wayland_path.name,
            }

            # Case 1: Wayland Compositor selection + atomic same-path socket replacement (never
            # observed absent) + socket loss/recovery increments generation.
            wl_sock = _bind_unix(wayland_path)
            wl_rebound = None
            engine_comp = Engine(Workspace(self.root / "ws-comp", create=True))
            proc_comp = None
            try:
                with _patched_session_env(base_env):
                    proc_handle = engine_comp.start_desktop_agent(
                        self.local,
                        self.agent_installation,
                        self.authority,
                        idle_grace=0.2,
                        wayland_backend="compositor",
                    )
                proc_comp = proc_handle._process
                c1 = proc_handle.desktop_capabilities()
                self.assertEqual(c1["backend"], "linux-wayland-compositor")
                self.assertEqual(c1["generation"], 1)
                self.assertEqual(c1["features"]["record.allDisplays"]["state"], "unimplemented")

                # Atomic same-path replacement: target stays present continuously, yet generation
                # must bump because the socket endpoint identity changed.
                wl_sock = _atomic_replace_unix(wayland_path, wl_sock)
                c_replaced = proc_handle.desktop_capabilities()
                self.assertEqual(c_replaced["backend"], "linux-wayland-compositor")
                self.assertEqual(c_replaced["generation"], 2)
                self.assertEqual(
                    c_replaced["features"]["record.allDisplays"]["state"], "unimplemented"
                )
                self.assertEqual(proc_handle.desktop_capabilities()["generation"], 2)

                # Simulate compositor crash/restart by removing live Wayland socket.
                wl_sock.close()
                wayland_path.unlink()
                c2 = proc_handle.desktop_capabilities()
                self.assertEqual(c2["generation"], 3)
                self.assertEqual(
                    c2["features"]["record.allDisplays"]["state"], "sessionUnavailable"
                )

                # Re-bind Wayland socket -> recovers to active compositor and bumps generation.
                wl_rebound = _bind_unix(wayland_path)
                c3 = proc_handle.desktop_capabilities()
                self.assertEqual(c3["backend"], "linux-wayland-compositor")
                self.assertEqual(c3["generation"], 4)
                self.assertEqual(c3["features"]["record.allDisplays"]["state"], "unimplemented")
            finally:
                engine_comp.close()
                if proc_comp is not None:
                    self.assertEqual(proc_comp.wait(timeout=5), 0)
                wl_sock.close()
                if wl_rebound is not None:
                    wl_rebound.close()
                if wayland_path.exists():
                    wayland_path.unlink()

            # Case 2: Explicit DBUS_SESSION_BUS_ADDRESS atomic replacement + close while
            # $XDG_RUNTIME_DIR/bus stays alive -> never falls back to runtime bus.
            with _bind_unix(runtime_bus_path):
                wl_sock2 = _bind_unix(wayland_path)
                custom_bus_sock = _bind_unix(custom_bus_path)
                engine_portal = Engine(Workspace(self.root / "ws-portal", create=True))
                authority_portal = agent.AgentAuthority((self.local,), _SignatureRequest)
                proc_portal = None
                try:
                    portal_env = {
                        **base_env,
                        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={custom_bus_path}",
                    }
                    with _patched_session_env(portal_env):
                        proc_handle = engine_portal.start_desktop_agent(
                            self.local,
                            self.agent_installation,
                            authority_portal,
                            idle_grace=0.2,
                            wayland_backend="portal",
                        )
                    proc_portal = proc_handle._process
                    p1 = proc_handle.desktop_capabilities()
                    self.assertEqual(p1["backend"], "linux-wayland-portal")
                    self.assertEqual(p1["generation"], 1)
                    self.assertEqual(p1["features"]["record.allDisplays"]["state"], "unsupported")
                    self.assertEqual(p1["features"]["input.keyboard"]["state"], "unimplemented")

                    # Atomic same-path replacement of custom D-Bus socket -> generation bumps to 2
                    # without any intermediate absence.
                    custom_bus_sock = _atomic_replace_unix(custom_bus_path, custom_bus_sock)
                    p_replaced = proc_handle.desktop_capabilities()
                    self.assertEqual(p_replaced["backend"], "linux-wayland-portal")
                    self.assertEqual(p_replaced["generation"], 2)
                    self.assertEqual(
                        p_replaced["features"]["input.keyboard"]["state"], "unimplemented"
                    )

                    # Close ONLY the explicit custom session bus; default runtime bus stays alive.
                    custom_bus_sock.close()
                    custom_bus_path.unlink()
                    p2 = proc_handle.desktop_capabilities()
                    self.assertEqual(p2["backend"], "linux-wayland-portal")
                    self.assertEqual(p2["generation"], 3)
                    self.assertEqual(p2["features"]["record.allDisplays"]["state"], "unsupported")
                    self.assertEqual(
                        p2["features"]["input.keyboard"]["state"], "sessionUnavailable"
                    )
                finally:
                    engine_portal.close()
                    if proc_portal is not None:
                        self.assertEqual(proc_portal.wait(timeout=5), 0)
                    wl_sock2.close()
                    custom_bus_sock.close()
                    if wayland_path.exists():
                        wayland_path.unlink()
                    if custom_bus_path.exists():
                        custom_bus_path.unlink()

                # Case 3: Auto mode atomic compositor-socket replacement + toggle to Portal.
                wl_sock3 = _bind_unix(wayland_path)
                comp_sock = _bind_unix(compositor_sock_path)
                comp_sock_rebound = None
                engine_auto = Engine(Workspace(self.root / "ws-auto", create=True))
                authority_auto = agent.AgentAuthority((self.local,), _SignatureRequest)
                proc_auto = None
                try:
                    auto_env = {
                        **base_env,
                        "DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET": compositor_sock_path.name,
                    }
                    with _patched_session_env(auto_env):
                        proc_handle = engine_auto.start_desktop_agent(
                            self.local,
                            self.agent_installation,
                            authority_auto,
                            idle_grace=0.2,
                            wayland_backend="auto",
                        )
                    proc_auto = proc_handle._process
                    a1 = proc_handle.desktop_capabilities()
                    self.assertEqual(a1["backend"], "linux-wayland-compositor")
                    self.assertEqual(a1["generation"], 1)
                    self.assertEqual(a1["features"]["record.allDisplays"]["state"], "unimplemented")

                    # Atomic same-path replacement of compositor integration socket -> gen 2.
                    comp_sock = _atomic_replace_unix(compositor_sock_path, comp_sock)
                    a_replaced = proc_handle.desktop_capabilities()
                    self.assertEqual(a_replaced["backend"], "linux-wayland-compositor")
                    self.assertEqual(a_replaced["generation"], 2)
                    self.assertEqual(
                        a_replaced["features"]["record.allDisplays"]["state"], "unimplemented"
                    )

                    # Removing compositor socket falls back to active Portal via runtime bus.
                    comp_sock.close()
                    compositor_sock_path.unlink()
                    a2 = proc_handle.desktop_capabilities()
                    self.assertEqual(a2["backend"], "linux-wayland-portal")
                    self.assertEqual(a2["generation"], 3)
                    self.assertEqual(a2["features"]["record.allDisplays"]["state"], "unsupported")
                    self.assertEqual(a2["features"]["input.keyboard"]["state"], "unimplemented")

                    # Re-creating compositor socket switches back to compositor and bumps gen.
                    comp_sock_rebound = _bind_unix(compositor_sock_path)
                    a3 = proc_handle.desktop_capabilities()
                    self.assertEqual(a3["backend"], "linux-wayland-compositor")
                    self.assertEqual(a3["generation"], 4)
                    self.assertEqual(a3["features"]["record.allDisplays"]["state"], "unimplemented")
                finally:
                    engine_auto.close()
                    if proc_auto is not None:
                        self.assertEqual(proc_auto.wait(timeout=5), 0)
                    wl_sock3.close()
                    comp_sock.close()
                    if comp_sock_rebound is not None:
                        comp_sock_rebound.close()
                    if wayland_path.exists():
                        wayland_path.unlink()
                    if compositor_sock_path.exists():
                        compositor_sock_path.unlink()

                # Case 4: Explicit unsupported / abstract / multi-address D-Bus while
                # $XDG_RUNTIME_DIR/bus is alive -> must NEVER fall back to runtime bus.
                for idx, bad_dbus in enumerate(
                    (
                        "unix:abstract=/tmp/deskweave-nonexistent-bus",
                        "unix:abstract=/tmp/dw-abs;tcp:host=127.0.0.1,port=1234",
                    )
                ):
                    wl_sock_abs = _bind_unix(wayland_path)
                    engine_abs = Engine(Workspace(self.root / f"ws-dbus-abs-{idx}", create=True))
                    authority_abs = agent.AgentAuthority((self.local,), _SignatureRequest)
                    proc_abs = None
                    try:
                        with _patched_session_env(
                            {**base_env, "DBUS_SESSION_BUS_ADDRESS": bad_dbus}
                        ):
                            proc_handle = engine_abs.start_desktop_agent(
                                self.local,
                                self.agent_installation,
                                authority_abs,
                                idle_grace=0.2,
                                wayland_backend="portal",
                            )
                        proc_abs = proc_handle._process
                        caps = proc_handle.desktop_capabilities()
                        self.assertEqual(caps["backend"], "linux-wayland-portal")
                        self.assertEqual(
                            caps["features"]["record.allDisplays"]["state"], "unsupported"
                        )
                        self.assertEqual(
                            caps["features"]["input.keyboard"]["state"], "sessionUnavailable"
                        )
                    finally:
                        engine_abs.close()
                        if proc_abs is not None:
                            self.assertEqual(proc_abs.wait(timeout=5), 0)
                        wl_sock_abs.close()
                        if wayland_path.exists():
                            wayland_path.unlink()

                # Case 5: Valid multi-address DBUS_SESSION_BUS_ADDRESS (missing first, live second)
                # resolves second socket, tracks its replacement, and degrades without falling back
                # to live $XDG_RUNTIME_DIR/bus when closed.
                wl_sock_multi = _bind_unix(wayland_path)
                multi_bus_sock = _bind_unix(custom_bus_path)
                missing_bus_path = priv_dir / "missing-bus"
                engine_multi = Engine(Workspace(self.root / "ws-dbus-multi", create=True))
                authority_multi = agent.AgentAuthority((self.local,), _SignatureRequest)
                proc_multi = None
                try:
                    multi_env = {
                        **base_env,
                        "DBUS_SESSION_BUS_ADDRESS": (
                            f"unix:path={missing_bus_path};unix:path={custom_bus_path}"
                        ),
                    }
                    with _patched_session_env(multi_env):
                        proc_handle = engine_multi.start_desktop_agent(
                            self.local,
                            self.agent_installation,
                            authority_multi,
                            idle_grace=0.2,
                            wayland_backend="portal",
                        )
                    proc_multi = proc_handle._process
                    m1 = proc_handle.desktop_capabilities()
                    self.assertEqual(m1["backend"], "linux-wayland-portal")
                    self.assertEqual(m1["generation"], 1)
                    self.assertEqual(m1["features"]["input.keyboard"]["state"], "unimplemented")

                    multi_bus_sock = _atomic_replace_unix(custom_bus_path, multi_bus_sock)
                    m2 = proc_handle.desktop_capabilities()
                    self.assertEqual(m2["generation"], 2)
                    self.assertEqual(m2["features"]["input.keyboard"]["state"], "unimplemented")

                    multi_bus_sock.close()
                    custom_bus_path.unlink()
                    m3 = proc_handle.desktop_capabilities()
                    self.assertEqual(m3["generation"], 3)
                    self.assertEqual(
                        m3["features"]["input.keyboard"]["state"], "sessionUnavailable"
                    )
                finally:
                    engine_multi.close()
                    if proc_multi is not None:
                        self.assertEqual(proc_multi.wait(timeout=5), 0)
                    wl_sock_multi.close()
                    multi_bus_sock.close()
                    if wayland_path.exists():
                        wayland_path.unlink()
                    if custom_bus_path.exists():
                        custom_bus_path.unlink()


if __name__ == "__main__":
    unittest.main()
