pub mod capabilities;
pub mod error;
pub mod fake;
pub mod ports;
pub mod topology;
pub mod trajectory;

pub use capabilities::{
    BackendKind, CapabilitySnapshot, FeatureReason, FeatureSet, FeatureState, FeatureStatus,
    MAX_SAFE_INT, NextAction, PlatformArch, PlatformOs, TopologyCoverage, TrajectoryVersion,
};
pub use error::PlatformError;
pub use fake::{FakeClock, FakeDesktopSession};
pub use ports::{
    CaptureRoi, CaptureSource, CapturedFrame, CleanupOutcome, CredentialErrorKind, CredentialStore,
    DesktopSession, EmergencyControl, EmergencyHotkey, EmergencyState, HeldInput, InputClock,
    InputEmitter, InputRecorder, MonotonicClock, NativeInputAction, RecordedBatch, RecordingConfig,
    RecordingHandle, SubmissionOutcome, TargetBinding, TargetDescriptor, TargetKind, TargetStatus,
    TargetToken, UnimplementedPorts, roi_inside_bounds,
};
pub use topology::{
    AffineTransform2D, DisplayLocalTopology, DisplayPoint, DisplayTopology, InputProfile,
    LayoutRect, LocalDisplay, LocalTopologyCoverage, NativeSpace, PhysicalDisplay, PhysicalPoint,
    PhysicalRect, PhysicalTopology, PixelSize, RelativeUnit, Rotation,
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
}
