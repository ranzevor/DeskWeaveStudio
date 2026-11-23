pub mod capabilities;
pub mod control;
pub mod error;
pub mod fake;
pub mod jcs;
pub mod ports;
pub mod topology;
pub mod trajectory;

pub use capabilities::{
    BackendKind, CapabilitySnapshot, FeatureReason, FeatureSet, FeatureState, FeatureStatus,
    MAX_SAFE_INT, NextAction, PlatformArch, PlatformOs, TopologyCoverage, TrajectoryVersion,
};
pub use control::{
    AtomicInputEvent, DEFAULT_PRESS_DURATION_MS, InterruptPolicy, LeaseAcquireResult,
    LeaseReleaseReason, LeaseReleaseResult, LeaseSnapshot, LeaseState, MAX_PRESS_DURATION_MS,
    MAX_SEGMENT_EVENTS, PausePoint, PauseSnapshot, ResolvedPosition, ResumeContext, ResumePlan,
    SegmentCursor, SegmentResult, SegmentState, TargetBindResult, TargetState, TrajectoryRef,
    compute_segment_payload_hash, expand_input_operation, format_utc_timestamp,
    parse_utc_timestamp, quintic_progress, reject_unresolved_expressions, sort_held_inputs,
    sort_target_states,
};
pub use error::PlatformError;
pub use fake::{FakeClock, FakeDesktopSession};
pub use jcs::{canonical_json_bytes, format_jcs_f64, jcs_sha256_hex, sha256_bytes, sha256_hex};
pub use ports::{
    CaptureRoi, CaptureSource, CapturedFrame, CleanupOutcome, CredentialErrorKind, CredentialStore,
    DesktopSession, EmergencyControl, EmergencyHotkey, EmergencyState, HeldInput, InputClock,
    InputEmitter, InputRecorder, MonotonicClock, NativeInputAction, RecordedBatch, RecordingConfig,
    RecordingHandle, SubmissionOutcome, TargetBinding, TargetDescriptor, TargetKind, TargetStatus,
    TargetToken, UnimplementedPorts, roi_inside_bounds,
};
pub use topology::{
    AffineTransform2D, DisplayLocalTopology, DisplayPoint, DisplayTopology, InputProfile,
    LayoutRect, LocalDisplay, LocalTopologyCoverage, MAX_COORD_F64, MAX_COORD_I64, MIN_COORD_I64,
    NativeSpace, PhysicalDisplay, PhysicalPoint, PhysicalRect, PhysicalTopology, PixelSize,
    RelativeUnit, Rotation,
};
pub use trajectory::{
    ButtonState, CoordinateMode, DisplayLocalTopologyWrapper, DisplayMapping, DisplayPair,
    DisplaySource, PhysicalSource, PhysicalTopologyWrapper, PointerButton, RecordStartContext,
    RecordStartRequest, RecordingScope, RecordingSource, ReplayContext, TrajectoryDocument,
    TrajectoryEvent, validate_record_start, validate_replay,
};

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::PathBuf;

    use super::*;

    fn example_path(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples/platform")
            .join(name)
    }

    fn load_json<T: serde::de::DeserializeOwned>(name: &str) -> Result<T, serde_json::Error> {
        let bytes = fs::read(example_path(name)).expect("example file must exist");
        serde_json::from_slice(&bytes)
    }

    #[test]
    fn affine_transform_round_trips_and_rejects_singular_matrices() {
        let retina_offset = AffineTransform2D([0.5, 0.0, 0.0, 0.5, -1920.0, 100.0]);
        retina_offset.validate().unwrap();
        let (nx, ny) = retina_offset.pixel_to_native(80.0, 200.0).unwrap();
        assert!((nx - -1880.0).abs() < 1e-9);
        assert!((ny - 200.0).abs() < 1e-9);
        let (px, py) = retina_offset.native_to_pixel(nx, ny).unwrap();
        assert!((px - 80.0).abs() < 1e-9);
        assert!((py - 200.0).abs() < 1e-9);

        let rotated = AffineTransform2D([0.0, 1.0, -1.0, 0.0, 1080.0, 0.0]);
        let (rx, ry) = rotated.pixel_to_native(12.5, 34.25).unwrap();
        let (back_x, back_y) = rotated.native_to_pixel(rx, ry).unwrap();
        assert!((back_x - 12.5).abs() < 1e-9);
        assert!((back_y - 34.25).abs() < 1e-9);

        assert!(
            AffineTransform2D([1.0, 2.0, 2.0, 4.0, 0.0, 0.0])
                .validate()
                .is_err()
        );
        assert!(
            AffineTransform2D([f64::NAN, 0.0, 0.0, 1.0, 0.0, 0.0])
                .validate()
                .is_err()
        );
    }

    #[test]
    fn golden_capability_examples_match_expected_outcomes() {
        for valid in [
            "windows-capabilities.json",
            "macos-capabilities.json",
            "x11-capabilities.json",
            "wayland-capabilities.json",
            "macos-permissions.json",
            "compositor-unimplemented.json",
        ] {
            let snapshot: CapabilitySnapshot =
                load_json(valid).unwrap_or_else(|err| panic!("failed to parse {valid}: {err}"));
            snapshot
                .validate()
                .unwrap_or_else(|err| panic!("expected {valid} valid, got {err}"));
        }

        for invalid in [
            "invalid-os-backend.json",
            "invalid-partial-all-displays.json",
            "invalid-portal-passive-recording.json",
            "invalid-available-permission.json",
        ] {
            let snapshot: CapabilitySnapshot =
                load_json(invalid).unwrap_or_else(|err| panic!("failed to parse {invalid}: {err}"));
            assert!(
                snapshot.validate().is_err(),
                "expected {invalid} to fail validation"
            );
        }
    }

    #[test]
    fn golden_topology_and_source_examples_match_expected_outcomes() {
        let valid_topo: DisplayTopology = load_json("macos-topology.json").unwrap();
        valid_topo.validate().unwrap();

        let singular_topo: DisplayTopology = load_json("invalid-singular-transform.json").unwrap();
        assert!(singular_topo.validate().is_err());

        let valid_source: RecordingSource = load_json("macos-source.json").unwrap();
        valid_source.validate().unwrap();

        let bad_source: RecordingSource = load_json("invalid-selected-all-source.json").unwrap();
        assert!(bad_source.validate().is_err());
    }

    #[test]
    fn golden_trajectory_examples_match_expected_outcomes() {
        for valid in [
            "macos-cross-display.json",
            "fractional-relative.json",
            "physical-v11.json",
        ] {
            let doc: TrajectoryDocument =
                load_json(valid).unwrap_or_else(|err| panic!("failed to parse {valid}: {err}"));
            doc.validate()
                .unwrap_or_else(|err| panic!("expected {valid} valid, got {err}"));
        }

        for invalid in [
            "invalid-display-missing-id.json",
            "invalid-display-outside.json",
            "invalid-legacy-display.json",
            "invalid-display-physical-mode.json",
            "invalid-relative-unit.json",
            "invalid-v10-fractional.json",
        ] {
            let outcome = load_json::<TrajectoryDocument>(invalid).and_then(|doc| {
                doc.validate()
                    .map_err(|err| serde::de::Error::custom(err.to_string()))
            });
            assert!(outcome.is_err(), "expected {invalid} to fail validation");
        }
    }

    #[test]
    fn golden_replay_and_record_start_examples_match_expected_outcomes() {
        for (mapping_file, ctx_file, expect_ok) in [
            ("replay-original.json", "context-original.json", true),
            (
                "replay-cross-backend.json",
                "context-cross-backend.json",
                true,
            ),
            (
                "invalid-original-backend.json",
                "context-invalid-original-backend.json",
                false,
            ),
            (
                "invalid-relative-backend.json",
                "context-invalid-relative-backend.json",
                false,
            ),
            (
                "invalid-missing-input-map.json",
                "context-missing-input-map.json",
                false,
            ),
        ] {
            let mapping: DisplayMapping = load_json(mapping_file).unwrap();
            let context: ReplayContext = load_json(ctx_file).unwrap();
            let result = validate_replay(&mapping, &context);
            assert_eq!(
                result.is_ok(),
                expect_ok,
                "unexpected replay validation for {mapping_file}: {result:?}"
            );
        }

        for (req_file, ctx_file, expect_ok) in [
            (
                "record-start-display.json",
                "context-record-start-display.json",
                true,
            ),
            (
                "invalid-record-permission.json",
                "context-invalid-record-permission.json",
                false,
            ),
            (
                "invalid-record-wayland-coverage.json",
                "context-invalid-record-wayland-coverage.json",
                false,
            ),
        ] {
            let request: RecordStartRequest = load_json(req_file).unwrap();
            let context: RecordStartContext = load_json(ctx_file).unwrap();
            let result = validate_record_start(&request, &context);
            assert_eq!(
                result.is_ok(),
                expect_ok,
                "unexpected record.start validation for {req_file}: {result:?}"
            );
        }
    }

    #[test]
    fn fake_desktop_session_exercises_all_ports() {
        let caps: CapabilitySnapshot = load_json("macos-capabilities.json").unwrap();
        let topo: DisplayTopology = load_json("macos-topology.json").unwrap();
        let fake = FakeDesktopSession::new(caps.clone(), Ok(topo.clone()));

        assert_eq!(fake.os(), PlatformOs::Macos);
        assert_eq!(fake.backend(), BackendKind::MacosQuartz);
        assert_eq!(fake.capabilities(), caps);
        assert_eq!(fake.topology().unwrap(), topo);

        assert_eq!(fake.clock().now_us(), 0);
        assert_eq!(fake.fake_clock().advance_us(15_000), 15_000);
        assert_eq!(fake.clock().now_us(), 15_000);

        let desc = fake.targets().bind_selection("sel-1").unwrap();
        assert_eq!(
            fake.targets().verify_target(&desc.token).unwrap(),
            TargetStatus::Active
        );
        assert_eq!(
            fake.emitter()
                .submit_event(&desc.token, &NativeInputAction::KeyDown("a".to_owned()))
                .unwrap(),
            SubmissionOutcome::Submitted
        );
        assert_eq!(fake.submitted_actions().len(), 1);

        let gen1 = fake.emergency().trigger_lock().unwrap();
        assert_eq!(fake.emergency().state(), EmergencyState::Locked);
        assert_eq!(
            fake.emitter()
                .submit_event(&desc.token, &NativeInputAction::KeyUp("a".to_owned()))
                .unwrap(),
            SubmissionOutcome::Rejected
        );
        assert!(fake.emergency().rearm(gen1 + 1).is_err());
        assert_eq!(fake.emergency().rearm(gen1).unwrap(), gen1 + 1);
        assert_eq!(fake.emergency().state(), EmergencyState::Armed);

        fake.credentials()
            .write_secret("svc", "acct", b"top")
            .unwrap();
        assert_eq!(
            fake.credentials().read_secret("svc", "acct").unwrap(),
            b"top"
        );
        fake.credentials().delete_secret("svc", "acct").unwrap();
        assert!(fake.credentials().read_secret("svc", "acct").is_err());
    }

    #[test]
    fn required_nullable_optional_non_null_scroll_v10_and_roi_overflow_are_enforced() {
        // Required-nullable fields MUST fail if omitted, succeed if explicit null.
        let mut caps: serde_json::Value = load_json("compositor-unimplemented.json").unwrap();
        assert!(serde_json::from_value::<CapabilitySnapshot>(caps.clone()).is_ok());
        caps.as_object_mut().unwrap().remove("topologyVersion");
        assert!(serde_json::from_value::<CapabilitySnapshot>(caps).is_err());

        let mut topo: serde_json::Value = load_json("macos-topology.json").unwrap();
        topo["displays"][0]
            .as_object_mut()
            .unwrap()
            .remove("layoutBounds");
        assert!(serde_json::from_value::<DisplayTopology>(topo).is_err());

        let mut source: serde_json::Value = load_json("macos-source.json").unwrap();
        source.as_object_mut().unwrap().remove("initialPointer");
        assert!(serde_json::from_value::<RecordingSource>(source).is_err());

        // Optional-non-null fields MUST fail when explicit null is passed.
        let null_source = serde_json::json!({
            "formatVersion": "1.0",
            "coordinateMode": "physical",
            "source": null,
            "events": [{"kind": "pointer.move", "atUs": 0, "x": 1, "y": 2}]
        });
        assert!(serde_json::from_value::<TrajectoryDocument>(null_source).is_err());

        let null_display_id = serde_json::json!({
            "formatVersion": "1.1",
            "coordinateMode": "physical",
            "events": [{"kind": "pointer.move", "atUs": 0, "x": 1, "y": 2, "displayId": null}]
        });
        assert!(serde_json::from_value::<TrajectoryDocument>(null_display_id).is_err());

        // Trajectory 1.0 rejects fractional pointer.scroll dx/dy.
        let scroll_v10: TrajectoryDocument = serde_json::from_value(serde_json::json!({
            "formatVersion": "1.0",
            "coordinateMode": "physical",
            "events": [{"kind": "pointer.scroll", "atUs": 0, "dx": 0.5, "dy": 0}]
        }))
        .unwrap();
        assert!(scroll_v10.validate().is_err());

        // roi_inside_bounds handles extreme i64 values without arithmetic overflow.
        let bounds = PhysicalRect {
            x: 0,
            y: 0,
            width: 1920,
            height: 1080,
        };
        assert!(!roi_inside_bounds(
            CaptureRoi {
                x: i64::MAX,
                y: 0,
                width: 10,
                height: 10,
            },
            bounds
        ));
    }

    #[test]
    fn rotation_deserializes_integer_valued_floats_and_rejects_fractional() {
        for (raw, expected) in [
            ("0", Rotation::Deg0),
            ("0.0", Rotation::Deg0),
            ("-0.0", Rotation::Deg0),
            ("90", Rotation::Deg90),
            ("90.0", Rotation::Deg90),
            ("180.0", Rotation::Deg180),
            ("270.0", Rotation::Deg270),
        ] {
            let parsed: Rotation = serde_json::from_str(raw)
                .unwrap_or_else(|err| panic!("expected rotation {raw} valid, got {err}"));
            assert_eq!(parsed, expected);
        }

        for invalid in ["90.5", "-90.0", "360.0", "1.0", "1e20"] {
            assert!(
                serde_json::from_str::<Rotation>(invalid).is_err(),
                "expected rotation {invalid} to fail"
            );
        }

        // Valid float rotation inside a full LocalDisplay topology document.
        let mut topo: serde_json::Value = load_json("macos-topology.json").unwrap();
        topo["displays"][0]["rotation"] = serde_json::json!(90.0);
        let parsed: DisplayTopology = serde_json::from_value(topo).unwrap();
        parsed.validate().unwrap();
    }

    #[test]
    fn record_start_enforces_bidirectional_topology_mode_and_v11_admission() {
        let local_ctx: RecordStartContext = load_json("context-record-start-display.json").unwrap();
        let base_req: RecordStartRequest = load_json("record-start-display.json").unwrap();

        // displayLocalPixels + physical -> rejected.
        let mut req = base_req.clone();
        req.coordinate_mode = CoordinateMode::Physical;
        assert_eq!(
            validate_record_start(&req, &local_ctx).unwrap_err().code,
            "INVALID_ARGUMENT"
        );

        // displayLocalPixels + event / relative / display with 1.1 -> valid neighbor cases.
        for mode in [
            CoordinateMode::Display,
            CoordinateMode::Event,
            CoordinateMode::Relative,
        ] {
            let mut r = base_req.clone();
            r.coordinate_mode = mode;
            validate_record_start(&r, &local_ctx)
                .unwrap_or_else(|err| panic!("expected displayLocalPixels + {mode:?} ok: {err}"));
        }

        // displayLocalPixels with only trajectory 1.0 -> rejected for display, event, AND relative!
        let mut local_v10_ctx = local_ctx.clone();
        local_v10_ctx
            .capabilities
            .as_mut()
            .unwrap()
            .trajectory_versions = vec![TrajectoryVersion::V1_0];
        for mode in [
            CoordinateMode::Display,
            CoordinateMode::Event,
            CoordinateMode::Relative,
        ] {
            let mut r = base_req.clone();
            r.coordinate_mode = mode;
            assert_eq!(
                validate_record_start(&r, &local_v10_ctx).unwrap_err().code,
                "SCHEMA_UNSUPPORTED",
                "expected displayLocalPixels + {mode:?} without 1.1 to fail"
            );
        }

        // desktopPhysicalPixels context with trajectory 1.0 only.
        let physical_topo = DisplayTopology::Physical(PhysicalTopology {
            version: 7,
            virtual_bounds: PhysicalRect {
                x: 0,
                y: 0,
                width: 1920,
                height: 1080,
            },
            displays: vec![PhysicalDisplay {
                display_id: "main".to_owned(),
                name: "主屏".to_owned(),
                output_names: vec!["DP-1".to_owned()],
                bounds: PhysicalRect {
                    x: 0,
                    y: 0,
                    width: 1920,
                    height: 1080,
                },
                rotation: Rotation::Deg0,
                dpi_x: 96.0,
                dpi_y: 96.0,
                primary: true,
            }],
        });
        let mut physical_caps = local_ctx.capabilities.clone().unwrap();
        physical_caps.os = PlatformOs::Windows;
        physical_caps.backend = BackendKind::WindowsWin32;
        physical_caps.trajectory_versions = vec![TrajectoryVersion::V1_0];
        let physical_ctx = RecordStartContext {
            target_token: base_req.target_token.clone(),
            target_kind: "desktop".to_owned(),
            topology: physical_topo,
            permissions: vec!["record".to_owned()],
            capabilities: Some(physical_caps),
        };

        // desktopPhysicalPixels + display -> rejected!
        let mut req_display = base_req.clone();
        req_display.coordinate_mode = CoordinateMode::Display;
        assert_eq!(
            validate_record_start(&req_display, &physical_ctx)
                .unwrap_err()
                .code,
            "INVALID_ARGUMENT"
        );

        // desktopPhysicalPixels + physical / event / relative with only 1.0 -> valid neighbor cases!
        for mode in [
            CoordinateMode::Physical,
            CoordinateMode::Event,
            CoordinateMode::Relative,
        ] {
            let mut r = base_req.clone();
            r.coordinate_mode = mode;
            validate_record_start(&r, &physical_ctx).unwrap_or_else(|err| {
                panic!("expected desktopPhysicalPixels + {mode:?} with 1.0 ok: {err}")
            });
        }
    }

    #[test]
    fn display_local_window_source_requires_full_available_display_set() {
        let valid_all: RecordingSource = load_json("macos-source.json").unwrap();
        let RecordingSource::Display(base) = valid_all else {
            panic!("expected display local source");
        };

        // Window scope with all displays in topology -> valid (even if topology coverage is Selected).
        let mut window_full = base.clone();
        window_full.scope = RecordingScope::Window;
        window_full.validate().unwrap();

        let mut window_selected_cov = window_full.clone();
        window_selected_cov.topology =
            DisplayLocalTopologyWrapper::DisplayLocal(DisplayLocalTopology {
                coverage: LocalTopologyCoverage::Selected,
                ..window_full.topology.inner().clone()
            });
        window_selected_cov.validate().unwrap();

        // Window scope dropping one display from topology -> rejected!
        let mut window_partial = window_full.clone();
        window_partial.display_ids = vec!["main".to_owned()];
        assert_eq!(
            window_partial.validate().unwrap_err().code,
            "INVALID_ARGUMENT"
        );

        // Display scope with single display -> valid neighbor case!
        let mut single_display = base;
        single_display.scope = RecordingScope::Display;
        single_display.display_ids = vec!["main".to_owned()];
        single_display.validate().unwrap();
    }

    fn repo_example_path(rel: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples")
            .join(rel)
    }

    fn load_repo_json<T: serde::de::DeserializeOwned>(rel: &str) -> Result<T, serde_json::Error> {
        let bytes = fs::read(repo_example_path(rel)).expect("repo example file must exist");
        serde_json::from_slice(&bytes)
    }

    #[test]
    fn jcs_encoding_and_segment_payload_hash_match_rfc8785_and_golden_examples() {
        assert_eq!(
            jcs_sha256_hex(&Vec::<HeldInput>::new()).unwrap(),
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
        );
        assert_eq!(format_jcs_f64(-0.0).unwrap(), "0");
        assert_eq!(format_jcs_f64(1e-6).unwrap(), "0.000001");
        assert_eq!(format_jcs_f64(1e-7).unwrap(), "1e-7");
        assert_eq!(format_jcs_f64(1e20).unwrap(), "100000000000000000000");
        assert_eq!(format_jcs_f64(1e21).unwrap(), "1e+21");

        // UTF-16 key sorting: U+E000 (BMP, UTF-16 0xE000) must sort AFTER U+10000 (surrogate 0xD800 0xDC00).
        let obj = serde_json::json!({
            "\u{E000}": 1,
            "\u{10000}": 2,
            "a": 3
        });
        let encoded = String::from_utf8(canonical_json_bytes(&obj).unwrap()).unwrap();
        assert_eq!(encoded, "{\"a\":3,\"\u{10000}\":2,\"\u{E000}\":1}");

        let seg_req: serde_json::Value = load_repo_json("messages/agent-segment.json").unwrap();
        let params = &seg_req["params"];
        let expected_hash = params["payloadHash"].as_str().unwrap();
        let computed = compute_segment_payload_hash(
            params["targetToken"].as_str().unwrap(),
            &params["operation"],
            None,
        )
        .unwrap();
        assert_eq!(computed, expected_hash);

        let bad_expr: serde_json::Value = load_repo_json("invalid/agent-expression.json").unwrap();
        assert!(
            expand_input_operation(
                &bad_expr["params"]["operation"],
                None,
                &PausePoint::Physical(PhysicalPoint { x: 0, y: 0 }),
            )
            .is_err()
        );

        assert_eq!(
            parse_utc_timestamp("+999-+1-+1T+0:+0:+0Z")
                .unwrap_err()
                .code,
            "INVALID_ARGUMENT"
        );
        assert!(parse_utc_timestamp("2026-10-29T10:15:00Z").is_ok());
    }

    #[test]
    fn golden_execution_control_examples_match_expected_outcomes() {
        for valid_pause in [
            "control/pause-held.json",
            "control/pause-idle.json",
            "control/pause-between-held-multitarget.json",
        ] {
            let snap: PauseSnapshot = load_repo_json(valid_pause).unwrap();
            snap.validate()
                .unwrap_or_else(|err| panic!("expected {valid_pause} valid, got {err}"));
        }

        for invalid_pause in [
            "control/invalid-cursor-overflow.json",
            "control/invalid-held-digest.json",
            "control/invalid-pause-target-outside.json",
            "control/invalid-pause-target-null.json",
        ] {
            let outcome = load_repo_json::<PauseSnapshot>(invalid_pause).and_then(|snap| {
                snap.validate()
                    .map_err(|err| serde::de::Error::custom(err.to_string()))
            });
            assert!(
                outcome.is_err(),
                "expected {invalid_pause} to fail validation"
            );
        }

        for valid_lease in [
            "control/lease-paused.json",
            "control/lease-uncertain.json",
            "control/lease-resumed.json",
        ] {
            let snap: LeaseSnapshot = load_repo_json(valid_lease).unwrap();
            snap.validate()
                .unwrap_or_else(|err| panic!("expected {valid_lease} valid, got {err}"));
        }

        for invalid_lease in [
            "control/invalid-paused-cleanup.json",
            "control/invalid-lease-generation.json",
        ] {
            let outcome = load_repo_json::<LeaseSnapshot>(invalid_lease).and_then(|snap| {
                snap.validate()
                    .map_err(|err| serde::de::Error::custom(err.to_string()))
            });
            assert!(
                outcome.is_err(),
                "expected {invalid_lease} to fail validation"
            );
        }

        for (plan_file, ctx_file, expect_ok) in [
            (
                "control/resume-plan-held.json",
                "control/context-held.json",
                true,
            ),
            (
                "control/resume-plan-idle.json",
                "control/context-idle.json",
                true,
            ),
            (
                "control/resume-between-held-multitarget.json",
                "control/context-between.json",
                true,
            ),
            (
                "control/invalid-plan-hash.json",
                "control/context-held.json",
                false,
            ),
            (
                "control/invalid-plan-expired.json",
                "control/context-expired.json",
                false,
            ),
            (
                "control/invalid-plan-epoch.json",
                "control/context-epoch.json",
                false,
            ),
            (
                "control/invalid-plan-generation.json",
                "control/context-generation.json",
                false,
            ),
            (
                "control/invalid-plan-consumed.json",
                "control/context-consumed.json",
                false,
            ),
            (
                "control/invalid-resume-target-substituted.json",
                "control/context-between.json",
                false,
            ),
            (
                "control/invalid-resume-target-outside.json",
                "control/context-between.json",
                false,
            ),
        ] {
            let plan: ResumePlan = load_repo_json(plan_file).unwrap();
            let ctx: ResumeContext = load_repo_json(ctx_file).unwrap();
            assert_eq!(
                plan.validate(&ctx).is_ok(),
                expect_ok,
                "unexpected validation outcome for {plan_file}"
            );
        }
    }

    #[test]
    fn input_operation_expansion_preserves_smooth_motion_deltas_and_chord_lifecycles() {
        // Positive-duration small & negative relative delta conservation (Q53).
        let delta_op = serde_json::json!({
            "id": "relMove",
            "title": "小负相对增量运动",
            "action": "pointer.delta",
            "params": {
                "target": "main",
                "dx": -3,
                "dy": 5,
                "durationMs": 40
            },
            "on": {}
        });
        let delta_events = expand_input_operation(
            &delta_op,
            None,
            &PausePoint::Physical(PhysicalPoint { x: 100, y: 100 }),
        )
        .unwrap();
        assert!(delta_events.len() >= 5);
        let mut sum_dx = 0_i64;
        let mut sum_dy = 0_i64;
        let mut sum_delay = 0_u64;
        for ev in &delta_events {
            sum_delay += ev.delay_before_us;
            let NativeInputAction::PointerDelta { dx, dy } = ev.action else {
                panic!("expected PointerDelta");
            };
            sum_dx += dx.round() as i64;
            sum_dy += dy.round() as i64;
        }
        assert_eq!(sum_dx, -3);
        assert_eq!(sum_dy, 5);
        assert_eq!(sum_delay, 40_000);

        // Positive-duration drag lifecycle: move(from) -> button_down -> smooth moves -> button_up(to).
        let drag_op = serde_json::json!({
            "id": "dragLine",
            "title": "平滑拖拽",
            "action": "pointer.drag",
            "params": {
                "target": "main",
                "from": {"kind": "point", "space": "desktopPhysical", "x": 10, "y": 20},
                "to": {"kind": "point", "space": "desktopPhysical", "x": 110, "y": 220},
                "button": "left",
                "durationMs": 64
            },
            "on": {}
        });
        let drag_events = expand_input_operation(
            &drag_op,
            None,
            &PausePoint::Physical(PhysicalPoint { x: 0, y: 0 }),
        )
        .unwrap();
        assert!(drag_events.len() >= 10);
        assert_eq!(
            drag_events[0].action,
            NativeInputAction::PointerMovePhysical(PhysicalPoint { x: 10, y: 20 })
        );
        assert_eq!(
            drag_events[1].action,
            NativeInputAction::PointerButtonDown(PointerButton::Left)
        );
        assert_eq!(
            drag_events[drag_events.len() - 2].action,
            NativeInputAction::PointerMovePhysical(PhysicalPoint { x: 110, y: 220 })
        );
        assert_eq!(
            drag_events[drag_events.len() - 1].action,
            NativeInputAction::PointerButtonUp(PointerButton::Left)
        );

        // Positive-duration displayPixels move interpolates smoothly from PausePoint::Display.
        let disp_move_op = serde_json::json!({
            "id": "dispMove",
            "title": "屏内平滑移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "displayPixels", "displayId": "main", "x": 110.0, "y": 220.0},
                "durationMs": 40
            },
            "on": {}
        });
        let disp_events = expand_input_operation(
            &disp_move_op,
            None,
            &PausePoint::Display(DisplayPoint {
                display_id: "main".to_owned(),
                x: 10.0,
                y: 20.0,
            }),
        )
        .unwrap();
        assert!(disp_events.len() >= 5);
        let disp_delay: u64 = disp_events.iter().map(|e| e.delay_before_us).sum();
        assert_eq!(disp_delay, 40_000);
        assert_eq!(
            disp_events.last().unwrap().action,
            NativeInputAction::PointerMoveDisplay(DisplayPoint {
                display_id: "main".to_owned(),
                x: 110.0,
                y: 220.0,
            })
        );

        // Keyboard chord forward press, hold pressDurationMs, reverse release (Q29).
        let chord_op = serde_json::json!({
            "id": "copyChord",
            "title": "组合键复制",
            "action": "keyboard.chord",
            "params": {
                "target": "main",
                "keys": ["Control", "Shift", "C"],
                "pressDurationMs": 80
            },
            "on": {}
        });
        let chord_events = expand_input_operation(
            &chord_op,
            None,
            &PausePoint::Physical(PhysicalPoint { x: 0, y: 0 }),
        )
        .unwrap();
        assert_eq!(
            chord_events,
            vec![
                AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyDown("Control".to_owned()),
                },
                AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyDown("Shift".to_owned()),
                },
                AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyDown("C".to_owned()),
                },
                AtomicInputEvent {
                    delay_before_us: 80_000,
                    action: NativeInputAction::KeyUp("C".to_owned()),
                },
                AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyUp("Shift".to_owned()),
                },
                AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyUp("Control".to_owned()),
                },
            ]
        );
    }
}
