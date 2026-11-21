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
}
