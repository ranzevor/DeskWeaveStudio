from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from deskweave import agent
from deskweave.adapters.workspace import Workspace
from deskweave.domain.execution_control import (
    compute_segment_payload_hash,
    resume_request_errors,
    validate_lease_acquire_result,
    validate_lease_release_result,
    validate_lease_snapshot,
    validate_pause_snapshot,
    validate_resume_plan,
    validate_resume_request,
    validate_segment_result,
    validate_target_bind_result,
)
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import digest, loads
from deskweave.transport.native import ProcessBinding
from tests.engine.test_agent import _BINARY, _SIGNER, _agent_installation, _SignatureRequest
from tests.engine.test_native import installation

_ROOT = Path(__file__).resolve().parents[2]
_CONTROL_EXAMPLES = _ROOT / "examples" / "control"
_MESSAGE_EXAMPLES = _ROOT / "examples" / "messages"
_INVALID_EXAMPLES = _ROOT / "examples" / "invalid"


def _load_control(name: str) -> dict:
    data = loads((_CONTROL_EXAMPLES / name).read_bytes())
    assert isinstance(data, dict)
    return data


class ExecutionControlContractTests(unittest.TestCase):
    def test_golden_pause_and_lease_snapshot_examples(self):
        for valid_pause in (
            "pause-held.json",
            "pause-idle.json",
            "pause-between-held-multitarget.json",
        ):
            with self.subTest(valid_pause=valid_pause):
                validate_pause_snapshot(_load_control(valid_pause))

        for invalid_pause in (
            "invalid-cursor-overflow.json",
            "invalid-held-digest.json",
            "invalid-pause-target-outside.json",
            "invalid-pause-target-null.json",
        ):
            with self.subTest(invalid_pause=invalid_pause):
                with self.assertRaises(DeskWeaveError) as caught:
                    validate_pause_snapshot(_load_control(invalid_pause))
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        for valid_lease in (
            "lease-paused.json",
            "lease-uncertain.json",
            "lease-resumed.json",
        ):
            with self.subTest(valid_lease=valid_lease):
                validate_lease_snapshot(_load_control(valid_lease))

        for invalid_lease in (
            "invalid-paused-cleanup.json",
            "invalid-lease-generation.json",
        ):
            with self.subTest(invalid_lease=invalid_lease):
                with self.assertRaises(DeskWeaveError) as caught:
                    validate_lease_snapshot(_load_control(invalid_lease))
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_golden_resume_plan_and_request_examples(self):
        for plan_file, ctx_file, expect_ok in (
            ("resume-plan-held.json", "context-held.json", True),
            ("resume-plan-idle.json", "context-idle.json", True),
            ("resume-between-held-multitarget.json", "context-between.json", True),
            ("invalid-plan-hash.json", "context-held.json", False),
            ("invalid-plan-expired.json", "context-expired.json", False),
            ("invalid-plan-epoch.json", "context-epoch.json", False),
            ("invalid-plan-generation.json", "context-generation.json", False),
            ("invalid-plan-consumed.json", "context-consumed.json", False),
            ("invalid-resume-target-substituted.json", "context-between.json", False),
            ("invalid-resume-target-outside.json", "context-between.json", False),
        ):
            with self.subTest(plan_file=plan_file):
                plan = _load_control(plan_file)
                ctx = _load_control(ctx_file)
                if expect_ok:
                    validate_resume_plan(plan, ctx)
                else:
                    with self.assertRaises(DeskWeaveError) as caught:
                        validate_resume_plan(plan, ctx)
                    self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        plan_held = _load_control("resume-plan-held.json")
        ctx_held = _load_control("context-held.json")
        req_ctx = {
            **ctx_held,
            "resumePlan": plan_held,
            "registeredGrant": "confirm-resume-demo",
            "clientKind": "trustedHost",
        }
        valid_req = {
            "protocolVersion": "1.0",
            "kind": "request",
            "id": "req-resume-1",
            "session": "sess-1",
            "workspace": "ws-1",
            "method": "run.resume",
            "deadlineMs": 5000,
            "params": {
                "runId": plan_held["runId"],
                "expectedControlVersion": plan_held["controlVersion"],
                "resumePlanId": plan_held["planId"],
                "confirmationGrant": "confirm-resume-demo",
            },
        }
        validate_resume_request(valid_req, req_ctx)

        untrusted_ctx = {**req_ctx, "clientKind": "mcp"}
        with self.assertRaises(DeskWeaveError) as caught:
            validate_resume_request(valid_req, untrusted_ctx)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_segment_payload_hash_matches_golden_example_and_result_validators(self):
        seg_msg = loads((_MESSAGE_EXAMPLES / "agent-segment.json").read_bytes())
        assert isinstance(seg_msg, dict)
        params = seg_msg["params"]
        computed = compute_segment_payload_hash(params["targetToken"], params["operation"])
        self.assertEqual(computed, params["payloadHash"])
        self.assertEqual(
            computed,
            "f565b15740b77e732ab635467f04723d37164597e7ffec20424171ff77f24622",
        )

        tref = {"resourceId": "res-1", "sha256": "a" * 64}
        traj_op = {
            "id": "play",
            "title": "回放轨迹",
            "action": "input.trajectory",
            "params": {"target": "main", "trajectory": "stroke"},
            "on": {},
        }
        self.assertEqual(
            compute_segment_payload_hash("target-demo", traj_op, tref),
            digest(
                {
                    "targetToken": "target-demo",
                    "operation": traj_op,
                    "trajectorySha256": "a" * 64,
                }
            ),
        )

        validate_target_bind_result(
            {
                "targetToken": "target-1",
                "displayName": "Window 1",
                "kind": "window",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        )
        validate_lease_acquire_result(
            {
                "leaseId": "lease-1",
                "runId": "run-1",
                "generation": 1,
                "agentEpoch": "a" * 64,
                "expiresInMs": 2000,
            }
        )
        validate_lease_release_result({"generation": 2, "cleanup": "notRequired"})
        validate_segment_result(
            {
                "segmentId": "seg-1",
                "state": "sent",
                "cleanup": "notRequired",
                "agentEpoch": "a" * 64,
            }
        )

    def test_resume_confirmation_and_context_validation_negative_cases(self):
        plan_held = _load_control("resume-plan-held.json")
        ctx_held = _load_control("context-held.json")
        base_ctx = {
            **ctx_held,
            "resumePlan": plan_held,
            "registeredGrant": "confirm-resume-demo",
            "clientKind": "trustedHost",
        }
        base_req = {
            "protocolVersion": "1.0",
            "kind": "request",
            "id": "req-resume-neg",
            "session": "sess-1",
            "workspace": "ws-1",
            "method": "run.resume",
            "deadlineMs": 5000,
            "params": {
                "runId": plan_held["runId"],
                "expectedControlVersion": plan_held["controlVersion"],
                "resumePlanId": plan_held["planId"],
                "confirmationGrant": "confirm-resume-demo",
            },
        }

        ctx_none_grant = {**base_ctx, "registeredGrant": None}
        req_none_grant = {
            **base_req,
            "params": {**base_req["params"], "confirmationGrant": None},
        }
        # schema rejects None for confirmationGrant, and omission must also fail cleanly
        req_no_grant = {
            **base_req,
            "params": {
                "runId": plan_held["runId"],
                "expectedControlVersion": plan_held["controlVersion"],
                "resumePlanId": plan_held["planId"],
            },
        }
        for bad_req, bad_ctx in (
            (req_none_grant, ctx_none_grant),
            (base_req, ctx_none_grant),
            (req_no_grant, ctx_none_grant),
            (req_no_grant, {k: v for k, v in base_ctx.items() if k != "registeredGrant"}),
            (base_req, {k: v for k, v in base_ctx.items() if k != "registeredGrant"}),
            (base_req, {k: v for k, v in base_ctx.items() if k != "clientKind"}),
        ):
            with self.subTest(bad_req=bad_req["params"], has_reg="registeredGrant" in bad_ctx):
                with self.assertRaises(DeskWeaveError) as caught:
                    validate_resume_request(bad_req, bad_ctx)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        self.assertTrue(resume_request_errors(req_none_grant, ctx_none_grant))

        for field, bad_val in (
            ("controlVersion", True),
            ("currentTargets", "bad"),
            ("sameOriginalRequest", 1),
            ("clientKind", 123),
        ):
            with self.subTest(field=field, bad_val=bad_val):
                bad_ctx = {**base_ctx, field: bad_val}
                with self.assertRaises(DeskWeaveError) as caught:
                    validate_resume_plan(plan_held, bad_ctx)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_malformed_and_naive_timestamps_and_trajectory_ref_raise_deskweave_error(self):
        pause_held = _load_control("pause-held.json")
        plan_held = _load_control("resume-plan-held.json")
        ctx_held = _load_control("context-held.json")

        for bad_ts in (
            "not-a-time",
            "2026-10-29T02:00:00",
            "2026-02-30T00:00:00Z",
            "+2026-10-29T02:00:00Z",
        ):
            with self.subTest(bad_ts=bad_ts):
                bad_pause = {**pause_held, "createdAt": bad_ts}
                with self.assertRaises(DeskWeaveError) as c1:
                    validate_pause_snapshot(bad_pause)
                self.assertEqual(c1.exception.code, "INVALID_ARGUMENT")

                bad_plan = {**plan_held, "expiresAt": bad_ts}
                with self.assertRaises(DeskWeaveError) as c2:
                    validate_resume_plan(bad_plan, ctx_held)
                self.assertEqual(c2.exception.code, "INVALID_ARGUMENT")

                bad_ctx = {**ctx_held, "preparedAt": bad_ts}
                with self.assertRaises(DeskWeaveError) as c3:
                    validate_resume_plan(plan_held, bad_ctx)
                self.assertEqual(c3.exception.code, "INVALID_ARGUMENT")

                with self.assertRaises(DeskWeaveError) as c4:
                    validate_target_bind_result(
                        {
                            "targetToken": "target-1",
                            "displayName": "Window 1",
                            "kind": "window",
                            "expiresAt": bad_ts,
                        }
                    )
                self.assertEqual(c4.exception.code, "INVALID_ARGUMENT")

        traj_op = {
            "id": "play",
            "title": "回放轨迹",
            "action": "input.trajectory",
            "params": {"target": "main", "trajectory": "stroke"},
            "on": {},
        }
        for bad_ref in (
            {"resourceId": "", "sha256": "a" * 64},
            {"resourceId": 123, "sha256": "a" * 64},
            {"resourceId": "res\x001", "sha256": "a" * 64},
            {"resourceId": "res-1", "sha256": "A" * 64},
            {"resourceId": "res-1", "sha256": "a" * 63},
        ):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaises(DeskWeaveError) as caught:
                    compute_segment_payload_hash("target-demo", traj_op, bad_ref)
                self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")


class DesktopLeaseAgentIntegrationTests(unittest.TestCase):
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
            ProcessBinding(os.getpid(), self.engine_installation, instance_epoch="engine-lease")
        )
        self.agent_installation = _agent_installation()
        self.stack.enter_context(patch.object(agent, "_runtime", self._runtime))
        scope = hashlib.sha256(os.fsencode(self.runtime)).hexdigest()
        self.stack.enter_context(patch.object(agent, "_scope", return_value=scope))
        self.authority = agent.AgentAuthority((self.local,), _SignatureRequest)

    def _runtime(self, local):
        from deskweave.coordination.directory import runtime_directory

        return runtime_directory(local, self.runtime)

    def test_cross_process_lease_exclusivity_pause_resume_segment_and_disconnect_release(self):
        engine_a = Engine(Workspace(self.root / "ws-a", create=True))
        engine_b = Engine(Workspace(self.root / "ws-b", create=True))
        authority_b = agent.AgentAuthority((self.local,), _SignatureRequest)
        process = None
        try:
            first = engine_a.start_desktop_agent(
                self.local, self.agent_installation, self.authority, idle_grace=0.25
            )
            process = first._process
            second = engine_b.start_desktop_agent(
                self.local, self.agent_installation, authority_b, idle_grace=0.25
            )
            self.assertEqual(first.pid, second.pid)

            # 1. target.bind binds synthetic test targets and enforces one-time selectionGrant.
            bound_a = first.target_bind("test-grant-win-a")
            self.assertEqual(bound_a["targetToken"], "target-win-a")
            self.assertEqual(bound_a["kind"], "window")

            with self.assertRaises(DeskWeaveError) as dup_grant:
                first.target_bind("test-grant-win-a")
            self.assertEqual(dup_grant.exception.code, "RESOURCE_DENIED")

            bound_b = second.target_bind("test-grant-win-b")
            self.assertEqual(bound_b["targetToken"], "target-win-b")

            # 2. Engine A acquires exclusive desktop lease; Engine B wait_ms=0 gets DESKTOP_BUSY.
            acq_a = first.lease_acquire("run-a", ["target-win-a"], wait_ms=0)
            lease_a = acq_a["leaseId"]
            gen_a = acq_a["generation"]
            self.assertEqual(acq_a["expiresInMs"], 2000)
            self.assertEqual(acq_a["agentEpoch"], first.epoch)

            with self.assertRaises(DeskWeaveError) as busy_err:
                second.lease_acquire("run-b", ["target-win-b"], wait_ms=0)
            self.assertEqual(busy_err.exception.code, "DESKTOP_BUSY")
            self.assertEqual(busy_err.exception.retry, "afterQuery")

            # Cross-workspace lease.status or lease.renew is rejected with ACCESS_DENIED.
            with self.assertRaises(DeskWeaveError) as cross_status:
                second.lease_status(lease_a)
            self.assertEqual(cross_status.exception.code, "ACCESS_DENIED")

            # 3. Engine A renews lease and submits smooth pointer.move, chord, and keyboard.down.
            renewed = first.lease_renew(lease_a, gen_a)
            self.assertEqual(renewed["leaseId"], lease_a)
            self.assertEqual(renewed["generation"], gen_a)

            op_move = {
                "id": "move-cursor",
                "title": "平滑移动指针",
                "action": "pointer.move",
                "params": {
                    "target": "main",
                    "position": {
                        "kind": "point",
                        "space": "desktopPhysical",
                        "x": 240,
                        "y": 180,
                    },
                    "durationMs": 32,
                },
                "on": {},
            }
            seg_move = first.segment_submit(lease_a, gen_a, "seg-move-1", "target-win-a", op_move)
            self.assertEqual(seg_move["state"], "sent")

            op_chord = {
                "id": "save-shortcut",
                "title": "发送保存快捷键",
                "action": "keyboard.chord",
                "params": {
                    "target": "main",
                    "keys": ["ControlLeft", "KeyS"],
                    "pressDurationMs": 20,
                },
                "on": {},
            }
            seg_chord = first.segment_submit(
                lease_a, gen_a, "seg-chord-1", "target-win-a", op_chord
            )
            self.assertEqual(seg_chord["state"], "sent")

            # Residual $expr in segment.submit is rejected with INVALID_ARGUMENT.
            invalid_expr = loads((_INVALID_EXAMPLES / "agent-expression.json").read_bytes())
            assert isinstance(invalid_expr, dict)
            bad_op = invalid_expr["params"]["operation"]
            with self.assertRaises(DeskWeaveError) as expr_err:
                first.segment_submit(lease_a, gen_a, "seg-bad-expr", "target-win-a", bad_op)
            self.assertEqual(expr_err.exception.code, "INVALID_ARGUMENT")

            # Hold ShiftLeft across nodes; test idempotent replay & conflict on segment RPCs.
            op_down = {
                "id": "hold-shift",
                "title": "按住 ShiftLeft",
                "action": "keyboard.down",
                "params": {"target": "main", "key": "ShiftLeft"},
                "on": {},
            }
            down_hash = compute_segment_payload_hash("target-win-a", op_down)
            seg_down = first.segment_submit(
                lease_a,
                gen_a,
                "seg-down-1",
                "target-win-a",
                op_down,
                payload_hash=down_hash,
            )
            self.assertEqual(seg_down["state"], "sent")

            seg_replay = first.segment_submit(
                lease_a,
                gen_a,
                "seg-down-1",
                "target-win-a",
                op_down,
                payload_hash=down_hash,
            )
            self.assertEqual(seg_replay, seg_down)
            self.assertEqual(first.segment_status("seg-down-1", down_hash), seg_down)

            with self.assertRaises(DeskWeaveError) as seg_conflict:
                first.segment_submit(lease_a, gen_a, "seg-down-1", "target-win-a", op_chord)
            self.assertEqual(seg_conflict.exception.code, "REQUEST_CONFLICT")

            with self.assertRaises(DeskWeaveError) as status_conflict:
                first.segment_status("seg-down-1", "0" * 64)
            self.assertEqual(status_conflict.exception.code, "REQUEST_CONFLICT")

            # 4. Pause lease -> releases held ShiftLeft and produces RFC 8785 JCS checkpoint.
            paused = first.lease_pause(lease_a, gen_a)
            self.assertEqual(paused["state"], "paused")
            self.assertEqual(paused["cleanup"], "succeeded")
            pause_snap = paused["pauseSnapshot"]
            self.assertIsNotNone(pause_snap)
            validate_pause_snapshot(pause_snap)
            self.assertEqual(pause_snap["heldInputs"], [{"kind": "key", "key": "ShiftLeft"}])
            self.assertEqual(pause_snap["pointer"], {"x": 240, "y": 180})
            self.assertEqual(pause_snap["inputTargetToken"], "target-win-a")

            # Arbitrary unregistered confirmationGrant must be rejected with RESOURCE_DENIED.
            pause_id = pause_snap["pauseId"]
            plan_hash = "e" * 64
            with self.assertRaises(DeskWeaveError) as confirm_req:
                first.lease_resume(lease_a, gen_a, pause_id, "attempt-1", plan_hash)
            self.assertEqual(confirm_req.exception.code, "LOCAL_CONFIRMATION_REQUIRED")

            with self.assertRaises(DeskWeaveError) as confirm_forged:
                first.lease_resume(
                    lease_a,
                    gen_a,
                    pause_id,
                    "attempt-1",
                    plan_hash,
                    confirmation_grant="arbitrary-grant",
                )
            self.assertEqual(confirm_forged.exception.code, "RESOURCE_DENIED")

            # Resuming with bound confirmationGrant restores held ShiftLeft and returns to active.
            bound_grant = f"test-confirm:{pause_id}:{pause_snap['checkpointHash']}:{plan_hash}"
            resumed = first.lease_resume(
                lease_a,
                gen_a,
                pause_id,
                "attempt-1",
                plan_hash,
                confirmation_grant=bound_grant,
            )
            self.assertEqual(resumed["state"], "active")
            self.assertEqual(resumed["lastResumeAttemptId"], "attempt-1")

            # Idempotent resumeAttemptId replay returns same snapshot; different hash conflicts.
            resumed_replay = first.lease_resume(
                lease_a,
                gen_a,
                pause_id,
                "attempt-1",
                plan_hash,
                confirmation_grant=bound_grant,
            )
            self.assertEqual(resumed_replay, resumed)

            with self.assertRaises(DeskWeaveError) as resume_conflict:
                first.lease_resume(
                    lease_a,
                    gen_a,
                    pause_id,
                    "attempt-1",
                    "d" * 64,
                    confirmation_grant=bound_grant,
                )
            self.assertEqual(resume_conflict.exception.code, "REQUEST_CONFLICT")

            # 5. FIFO wait queue + Engine A disconnect auto-revocation (Q10 + Q19).
            waiter_result: dict[str, object] = {}

            def _acquire_b():
                try:
                    waiter_result["lease"] = second.lease_acquire(
                        "run-b", ["target-win-b"], wait_ms=3000
                    )
                except BaseException as exc:
                    waiter_result["error"] = exc

            waiter_thread = threading.Thread(target=_acquire_b)
            waiter_thread.start()
            time.sleep(0.08)
            engine_a.close()
            waiter_thread.join(timeout=5)
            self.assertNotIn("error", waiter_result, waiter_result.get("error"))
            acq_b = waiter_result["lease"]
            assert isinstance(acq_b, dict)
            self.assertGreater(acq_b["generation"], gen_a)

            rel_b = second.lease_release(acq_b["leaseId"], acq_b["generation"], reason="finished")
            self.assertEqual(rel_b["cleanup"], "notRequired")
            self.assertGreater(rel_b["generation"], acq_b["generation"])
        finally:
            engine_a.close()
            engine_b.close()
            if process is not None:
                self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
