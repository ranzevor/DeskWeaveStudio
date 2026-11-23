use std::time::Instant;

use serde::{Deserialize, Serialize};

use crate::capabilities::{BackendKind, CapabilitySnapshot, PlatformArch, PlatformOs};
use crate::error::PlatformError;
use crate::topology::{DisplayPoint, DisplayTopology, PhysicalPoint, PhysicalRect};
use crate::trajectory::{CoordinateMode, PointerButton, TrajectoryEvent};

/// Opaque target capability token issued by trusted native selection/binding.
/// Shared core never holds raw OS handles (HWND / CGEventRef / XID / PipeWire / EIS fd).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TargetToken(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TargetKind {
    Window,
    Display,
    Region,
    Desktop,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TargetDescriptor {
    pub token: TargetToken,
    pub display_name: String,
    pub kind: TargetKind,
    pub target_version: String,
    pub expires_at: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetStatus {
    Active,
    Unfocused,
    Lost,
    Expired,
}

/// Port 4: Opaque target binding & foreground/validity verification.
pub trait TargetBinding: Send + Sync {
    fn bind_selection(&self, selection_grant: &str) -> Result<TargetDescriptor, PlatformError>;
    fn verify_target(&self, token: &TargetToken) -> Result<TargetStatus, PlatformError>;
    fn focus_target(&self, token: &TargetToken) -> Result<(), PlatformError>;
    fn target_version(&self, _token: &TargetToken) -> Result<Option<String>, PlatformError> {
        Ok(None)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct CaptureRoi {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapturedFrame {
    pub media_type: String,
    pub bytes: Vec<u8>,
    pub origin: PhysicalPoint,
    pub width: u32,
    pub height: u32,
    pub target_version: String,
    pub topology_version: u64,
}

/// Port 5: Screen/window capture source.
pub trait CaptureSource: Send + Sync {
    fn capture_frame(
        &self,
        token: &TargetToken,
        roi: Option<CaptureRoi>,
    ) -> Result<CapturedFrame, PlatformError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordingConfig {
    pub target_token: TargetToken,
    pub coordinate_mode: CoordinateMode,
    pub include_pointer_motion: bool,
    pub expected_topology_version: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct RecordingHandle(pub String);

#[derive(Debug, Clone, PartialEq)]
pub struct RecordedBatch {
    pub events: Vec<TrajectoryEvent>,
    pub interrupted_reason: Option<&'static str>,
}

/// Port 6: Native passive input recorder.
pub trait InputRecorder: Send + Sync {
    fn start_recording(&self, config: &RecordingConfig) -> Result<RecordingHandle, PlatformError>;
    fn drain_events(&self, handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError>;
    fn stop_recording(&self, handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError>;
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum HeldInput {
    Key { key: String },
    Button { button: PointerButton },
}

impl HeldInput {
    pub fn sort_key(&self) -> (&'static str, &str) {
        match self {
            Self::Button { button } => ("button", button.as_str()),
            Self::Key { key } => ("key", key.as_str()),
        }
    }

    pub fn validate(&self) -> Result<(), PlatformError> {
        match self {
            Self::Button { .. } => Ok(()),
            Self::Key { key } => {
                let chars = key.chars().count();
                if !(1..=32).contains(&chars) || key.bytes().any(|b| b == 0) {
                    return Err(PlatformError::invalid_argument("按键名称长度应为 1～32。"));
                }
                Ok(())
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum NativeInputAction {
    PointerMovePhysical(PhysicalPoint),
    PointerMoveDisplay(DisplayPoint),
    PointerDelta { dx: f64, dy: f64 },
    PointerButtonDown(PointerButton),
    PointerButtonUp(PointerButton),
    PointerScroll { dx: f64, dy: f64 },
    KeyDown(String),
    KeyUp(String),
    UnicodeText(String),
}

/// Native submission outcome: distinguishes confirmed dispatch, clear refusal, and ambiguous state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubmissionOutcome {
    Submitted,
    Rejected,
    Uncertain,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum CleanupOutcome {
    Pending,
    Succeeded,
    Failed,
    NotRequired,
}

/// Port 7: Native single-event input emitter & held-input release boundary.
pub trait InputEmitter: Send + Sync {
    fn submit_event(
        &self,
        token: &TargetToken,
        action: &NativeInputAction,
    ) -> Result<SubmissionOutcome, PlatformError>;

    fn release_held(&self, held: &[HeldInput]) -> Result<CleanupOutcome, PlatformError>;
}

/// Port 8: Shared monotonic microsecond clock for lease heartbeats, deadlines, and playback.
pub trait InputClock: Send + Sync {
    fn now_us(&self) -> u64;
}

/// Default process-local monotonic clock anchored at creation time.
pub struct MonotonicClock {
    origin: Instant,
}

impl MonotonicClock {
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for MonotonicClock {
    fn default() -> Self {
        Self::new()
    }
}

impl InputClock for MonotonicClock {
    fn now_us(&self) -> u64 {
        let elapsed = self.origin.elapsed().as_micros();
        u64::try_from(elapsed).unwrap_or(u64::MAX)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EmergencyHotkey {
    pub chord: String,
}

impl EmergencyHotkey {
    pub fn default_for(os: PlatformOs) -> Self {
        match os {
            PlatformOs::Macos => Self {
                chord: "Ctrl+Alt+F12".to_owned(),
            },
            PlatformOs::Windows | PlatformOs::Linux => Self {
                chord: "Ctrl+Alt+Pause".to_owned(),
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EmergencyState {
    Armed,
    Locked,
}

/// Port 9: Independent emergency stop control & native global hotkey boundary.
pub trait EmergencyControl: Send + Sync {
    fn state(&self) -> EmergencyState;
    fn generation(&self) -> u64;
    fn hotkey_ready(&self) -> bool;
    fn register_hotkey(&self, hotkey: &EmergencyHotkey) -> Result<(), PlatformError>;
    fn trigger_lock(&self) -> Result<u64, PlatformError>;
    fn rearm(&self, expected_generation: u64) -> Result<u64, PlatformError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialErrorKind {
    Unavailable,
    Locked,
    NotFound,
    AccessDenied,
}

/// Port 10: Platform secret store boundary (never falls back to plaintext files).
pub trait CredentialStore: Send + Sync {
    fn read_secret(&self, service: &str, account: &str) -> Result<Vec<u8>, PlatformError>;
    fn write_secret(
        &self,
        service: &str,
        account: &str,
        secret: &[u8],
    ) -> Result<(), PlatformError>;
    fn delete_secret(&self, service: &str, account: &str) -> Result<(), PlatformError>;
}

/// Port 1: Root desktop session facade combining capability/topology inspection with all sub-ports.
pub trait DesktopSession: Send + Sync {
    fn os(&self) -> PlatformOs;
    fn arch(&self) -> PlatformArch;
    fn backend(&self) -> BackendKind;
    fn backend_version(&self) -> &str;
    fn capabilities(&self) -> CapabilitySnapshot;
    fn topology(&self) -> Result<DisplayTopology, PlatformError>;
    fn clock(&self) -> &dyn InputClock;
    fn targets(&self) -> &dyn TargetBinding;
    fn capture(&self) -> &dyn CaptureSource;
    fn recorder(&self) -> &dyn InputRecorder;
    fn emitter(&self) -> &dyn InputEmitter;
    fn emergency(&self) -> &dyn EmergencyControl;
    fn credentials(&self) -> &dyn CredentialStore;
}

/// Shared unimplemented sub-port adapter used by M1 platform backends before M3 native drivers.
pub struct UnimplementedPorts {
    os: PlatformOs,
    clock: MonotonicClock,
}

impl UnimplementedPorts {
    pub fn new(os: PlatformOs) -> Self {
        Self {
            os,
            clock: MonotonicClock::new(),
        }
    }

    pub fn clock(&self) -> &dyn InputClock {
        &self.clock
    }

    pub fn default_hotkey(&self) -> EmergencyHotkey {
        EmergencyHotkey::default_for(self.os)
    }
}

impl TargetBinding for UnimplementedPorts {
    fn bind_selection(&self, _selection_grant: &str) -> Result<TargetDescriptor, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生目标绑定尚未实现。",
        ))
    }

    fn verify_target(&self, _token: &TargetToken) -> Result<TargetStatus, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生目标核验尚未实现。",
        ))
    }

    fn focus_target(&self, _token: &TargetToken) -> Result<(), PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生目标激活尚未实现。",
        ))
    }
}

impl CaptureSource for UnimplementedPorts {
    fn capture_frame(
        &self,
        _token: &TargetToken,
        _roi: Option<CaptureRoi>,
    ) -> Result<CapturedFrame, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生屏幕采集尚未实现。",
        ))
    }
}

impl InputRecorder for UnimplementedPorts {
    fn start_recording(&self, _config: &RecordingConfig) -> Result<RecordingHandle, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生输入录制尚未实现。",
        ))
    }

    fn drain_events(&self, _handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生输入录制尚未实现。",
        ))
    }

    fn stop_recording(&self, _handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生输入录制尚未实现。",
        ))
    }
}

impl InputEmitter for UnimplementedPorts {
    fn submit_event(
        &self,
        _token: &TargetToken,
        _action: &NativeInputAction,
    ) -> Result<SubmissionOutcome, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生输入发送尚未实现。",
        ))
    }

    fn release_held(&self, held: &[HeldInput]) -> Result<CleanupOutcome, PlatformError> {
        if held.is_empty() {
            Ok(CleanupOutcome::NotRequired)
        } else {
            Err(PlatformError::capability_not_implemented(
                "原生输入释放尚未实现。",
            ))
        }
    }
}

impl EmergencyControl for UnimplementedPorts {
    fn state(&self) -> EmergencyState {
        EmergencyState::Armed
    }

    fn generation(&self) -> u64 {
        0
    }

    fn hotkey_ready(&self) -> bool {
        false
    }

    fn register_hotkey(&self, _hotkey: &EmergencyHotkey) -> Result<(), PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生全局急停快捷键尚未实现。",
        ))
    }

    fn trigger_lock(&self) -> Result<u64, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生急停控制尚未实现。",
        ))
    }

    fn rearm(&self, _expected_generation: u64) -> Result<u64, PlatformError> {
        Err(PlatformError::capability_not_implemented(
            "原生急停解除尚未实现。",
        ))
    }
}

impl CredentialStore for UnimplementedPorts {
    fn read_secret(&self, _service: &str, _account: &str) -> Result<Vec<u8>, PlatformError> {
        Err(PlatformError::credential_unavailable(
            "系统凭据存储尚未接入。",
        ))
    }

    fn write_secret(
        &self,
        _service: &str,
        _account: &str,
        _secret: &[u8],
    ) -> Result<(), PlatformError> {
        Err(PlatformError::credential_unavailable(
            "系统凭据存储尚未接入。",
        ))
    }

    fn delete_secret(&self, _service: &str, _account: &str) -> Result<(), PlatformError> {
        Err(PlatformError::credential_unavailable(
            "系统凭据存储尚未接入。",
        ))
    }
}

/// Helper to keep `PhysicalRect` re-exported alongside capture ROI utilities.
pub fn roi_inside_bounds(roi: CaptureRoi, bounds: PhysicalRect) -> bool {
    roi.width >= 1
        && roi.height >= 1
        && bounds.width >= 1
        && bounds.height >= 1
        && i128::from(roi.x) >= i128::from(bounds.x)
        && i128::from(roi.y) >= i128::from(bounds.y)
        && i128::from(roi.x) + i128::from(roi.width)
            <= i128::from(bounds.x) + i128::from(bounds.width)
        && i128::from(roi.y) + i128::from(roi.height)
            <= i128::from(bounds.y) + i128::from(bounds.height)
}
