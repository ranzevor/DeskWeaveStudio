use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::capabilities::{BackendKind, CapabilitySnapshot, PlatformArch, PlatformOs};
use crate::error::PlatformError;
use crate::ports::{
    CaptureRoi, CaptureSource, CapturedFrame, CleanupOutcome, CredentialStore, DesktopSession,
    EmergencyControl, EmergencyHotkey, EmergencyState, HeldInput, InputClock, InputEmitter,
    InputRecorder, NativeInputAction, RecordedBatch, RecordingConfig, RecordingHandle,
    SubmissionOutcome, TargetBinding, TargetDescriptor, TargetKind, TargetStatus, TargetToken,
};
use crate::topology::{DisplayTopology, PhysicalPoint};

/// Deterministic manual clock for unit and integration tests.
pub struct FakeClock {
    now_us: AtomicU64,
}

impl FakeClock {
    pub fn new(initial_us: u64) -> Self {
        Self {
            now_us: AtomicU64::new(initial_us),
        }
    }

    pub fn advance_us(&self, delta_us: u64) -> u64 {
        self.now_us.fetch_add(delta_us, Ordering::AcqRel) + delta_us
    }
}

impl InputClock for FakeClock {
    fn now_us(&self) -> u64 {
        self.now_us.load(Ordering::Acquire)
    }
}

struct FakeState {
    capabilities: CapabilitySnapshot,
    topology: Result<DisplayTopology, PlatformError>,
    emergency_state: EmergencyState,
    emergency_generation: u64,
    hotkey_ready: bool,
    submission_outcome: SubmissionOutcome,
    cleanup_outcome: CleanupOutcome,
    pointer: PhysicalPoint,
    targets: HashMap<String, (TargetDescriptor, TargetStatus)>,
    grants: HashMap<String, TargetDescriptor>,
    submitted: Vec<(TargetToken, NativeInputAction)>,
    released: Vec<Vec<HeldInput>>,
    secrets: HashMap<(String, String), Vec<u8>>,
}

/// In-memory controllable `DesktopSession` test double with zero OS side effects.
pub struct FakeDesktopSession {
    clock: FakeClock,
    backend_version: String,
    state: Mutex<FakeState>,
}

impl FakeDesktopSession {
    pub fn new(
        capabilities: CapabilitySnapshot,
        topology: Result<DisplayTopology, PlatformError>,
    ) -> Self {
        let backend_version = capabilities.backend_version.clone();
        Self {
            clock: FakeClock::new(0),
            backend_version,
            state: Mutex::new(FakeState {
                capabilities,
                topology,
                emergency_state: EmergencyState::Armed,
                emergency_generation: 0,
                hotkey_ready: true,
                submission_outcome: SubmissionOutcome::Submitted,
                cleanup_outcome: CleanupOutcome::Succeeded,
                pointer: PhysicalPoint { x: 0, y: 0 },
                targets: HashMap::new(),
                grants: HashMap::new(),
                submitted: Vec::new(),
                released: Vec::new(),
                secrets: HashMap::new(),
            }),
        }
    }

    pub fn fake_clock(&self) -> &FakeClock {
        &self.clock
    }

    pub fn set_capabilities(&self, capabilities: CapabilitySnapshot) {
        self.state.lock().unwrap().capabilities = capabilities;
    }

    pub fn set_topology(&self, topology: Result<DisplayTopology, PlatformError>) {
        self.state.lock().unwrap().topology = topology;
    }

    pub fn set_submission_outcome(&self, outcome: SubmissionOutcome) {
        self.state.lock().unwrap().submission_outcome = outcome;
    }

    pub fn set_cleanup_outcome(&self, outcome: CleanupOutcome) {
        self.state.lock().unwrap().cleanup_outcome = outcome;
    }

    pub fn set_hotkey_ready(&self, ready: bool) {
        self.state.lock().unwrap().hotkey_ready = ready;
    }

    pub fn set_pointer(&self, point: PhysicalPoint) {
        self.state.lock().unwrap().pointer = point;
    }

    pub fn pointer(&self) -> PhysicalPoint {
        self.state.lock().unwrap().pointer
    }

    pub fn register_grant(&self, selection_grant: impl Into<String>, descriptor: TargetDescriptor) {
        let mut state = self.state.lock().unwrap();
        state.targets.insert(
            descriptor.token.0.clone(),
            (descriptor.clone(), TargetStatus::Active),
        );
        state.grants.insert(selection_grant.into(), descriptor);
    }

    pub fn register_target(&self, descriptor: TargetDescriptor, status: TargetStatus) {
        self.state
            .lock()
            .unwrap()
            .targets
            .insert(descriptor.token.0.clone(), (descriptor, status));
    }

    pub fn set_target_status(&self, token: &str, status: TargetStatus) {
        let mut state = self.state.lock().unwrap();
        if let Some(entry) = state.targets.get_mut(token) {
            entry.1 = status;
        }
    }

    pub fn set_target_version(&self, token: &str, version: impl Into<String>) {
        let mut state = self.state.lock().unwrap();
        if let Some(entry) = state.targets.get_mut(token) {
            entry.0.target_version = version.into();
        }
    }

    pub fn submitted_actions(&self) -> Vec<(TargetToken, NativeInputAction)> {
        self.state.lock().unwrap().submitted.clone()
    }

    pub fn released_batches(&self) -> Vec<Vec<HeldInput>> {
        self.state.lock().unwrap().released.clone()
    }
}

impl DesktopSession for FakeDesktopSession {
    fn os(&self) -> PlatformOs {
        self.state.lock().unwrap().capabilities.os
    }

    fn arch(&self) -> PlatformArch {
        self.state.lock().unwrap().capabilities.arch
    }

    fn backend(&self) -> BackendKind {
        self.state.lock().unwrap().capabilities.backend
    }

    fn backend_version(&self) -> &str {
        &self.backend_version
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        self.state.lock().unwrap().capabilities.clone()
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        self.state.lock().unwrap().topology.clone()
    }

    fn clock(&self) -> &dyn InputClock {
        &self.clock
    }

    fn targets(&self) -> &dyn TargetBinding {
        self
    }

    fn capture(&self) -> &dyn CaptureSource {
        self
    }

    fn recorder(&self) -> &dyn InputRecorder {
        self
    }

    fn emitter(&self) -> &dyn InputEmitter {
        self
    }

    fn emergency(&self) -> &dyn EmergencyControl {
        self
    }

    fn credentials(&self) -> &dyn CredentialStore {
        self
    }
}

impl TargetBinding for FakeDesktopSession {
    fn bind_selection(&self, selection_grant: &str) -> Result<TargetDescriptor, PlatformError> {
        if selection_grant.is_empty() {
            return Err(PlatformError::invalid_argument("选择授权无效。"));
        }
        let mut state = self.state.lock().unwrap();
        if let Some(desc) = state.grants.remove(selection_grant) {
            state
                .targets
                .entry(desc.token.0.clone())
                .or_insert_with(|| (desc.clone(), TargetStatus::Active));
            return Ok(desc);
        }
        let desc = TargetDescriptor {
            token: TargetToken(format!("fake-target-{selection_grant}")),
            display_name: "Fake Target".to_owned(),
            kind: TargetKind::Desktop,
            target_version: "1".to_owned(),
            expires_at: "2099-01-01T00:00:00Z".to_owned(),
        };
        state
            .targets
            .entry(desc.token.0.clone())
            .or_insert_with(|| (desc.clone(), TargetStatus::Active));
        Ok(desc)
    }

    fn verify_target(&self, token: &TargetToken) -> Result<TargetStatus, PlatformError> {
        let state = self.state.lock().unwrap();
        if let Some((_, status)) = state.targets.get(&token.0) {
            return Ok(*status);
        }
        if token.0.starts_with("fake-target-") {
            Ok(TargetStatus::Active)
        } else {
            Ok(TargetStatus::Lost)
        }
    }

    fn focus_target(&self, token: &TargetToken) -> Result<(), PlatformError> {
        let mut state = self.state.lock().unwrap();
        if let Some((_, status)) = state.targets.get_mut(&token.0) {
            return match *status {
                TargetStatus::Active | TargetStatus::Unfocused => {
                    *status = TargetStatus::Active;
                    Ok(())
                }
                TargetStatus::Lost | TargetStatus::Expired => {
                    Err(PlatformError::target_lost("目标已失效或不存在。"))
                }
            };
        }
        if token.0.starts_with("fake-target-") {
            Ok(())
        } else {
            Err(PlatformError::target_lost("目标不存在。"))
        }
    }

    fn target_version(&self, token: &TargetToken) -> Result<Option<String>, PlatformError> {
        let state = self.state.lock().unwrap();
        Ok(state
            .targets
            .get(&token.0)
            .map(|(desc, _)| desc.target_version.clone()))
    }
}

impl CaptureSource for FakeDesktopSession {
    fn capture_frame(
        &self,
        token: &TargetToken,
        _roi: Option<CaptureRoi>,
    ) -> Result<CapturedFrame, PlatformError> {
        if !token.0.starts_with("fake-target-") {
            return Err(PlatformError::target_lost("目标不存在。"));
        }
        let topology_version = self
            .state
            .lock()
            .unwrap()
            .capabilities
            .topology_version
            .unwrap_or(0);
        Ok(CapturedFrame {
            media_type: "image/png".to_owned(),
            bytes: Vec::new(),
            origin: PhysicalPoint { x: 0, y: 0 },
            width: 1,
            height: 1,
            target_version: "1".to_owned(),
            topology_version,
        })
    }
}

impl InputRecorder for FakeDesktopSession {
    fn start_recording(&self, _config: &RecordingConfig) -> Result<RecordingHandle, PlatformError> {
        Ok(RecordingHandle("fake-recording-1".to_owned()))
    }

    fn drain_events(&self, _handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError> {
        Ok(RecordedBatch {
            events: Vec::new(),
            interrupted_reason: None,
        })
    }

    fn stop_recording(&self, _handle: &RecordingHandle) -> Result<RecordedBatch, PlatformError> {
        Ok(RecordedBatch {
            events: Vec::new(),
            interrupted_reason: None,
        })
    }
}

impl InputEmitter for FakeDesktopSession {
    fn submit_event(
        &self,
        token: &TargetToken,
        action: &NativeInputAction,
    ) -> Result<SubmissionOutcome, PlatformError> {
        let mut state = self.state.lock().unwrap();
        if state.emergency_state == EmergencyState::Locked {
            return Ok(SubmissionOutcome::Rejected);
        }
        if state.submission_outcome == SubmissionOutcome::Submitted {
            match action {
                NativeInputAction::PointerMovePhysical(pt) => {
                    state.pointer = *pt;
                }
                NativeInputAction::PointerDelta { dx, dy } => {
                    state.pointer.x = state.pointer.x.saturating_add(dx.round() as i64);
                    state.pointer.y = state.pointer.y.saturating_add(dy.round() as i64);
                }
                _ => {}
            }
        }
        state.submitted.push((token.clone(), action.clone()));
        Ok(state.submission_outcome)
    }

    fn release_held(&self, held: &[HeldInput]) -> Result<CleanupOutcome, PlatformError> {
        if held.is_empty() {
            return Ok(CleanupOutcome::NotRequired);
        }
        let mut state = self.state.lock().unwrap();
        let outcome = state.cleanup_outcome;
        state.released.push(held.to_vec());
        Ok(outcome)
    }
}

impl EmergencyControl for FakeDesktopSession {
    fn state(&self) -> EmergencyState {
        self.state.lock().unwrap().emergency_state
    }

    fn generation(&self) -> u64 {
        self.state.lock().unwrap().emergency_generation
    }

    fn hotkey_ready(&self) -> bool {
        self.state.lock().unwrap().hotkey_ready
    }

    fn register_hotkey(&self, _hotkey: &EmergencyHotkey) -> Result<(), PlatformError> {
        self.state.lock().unwrap().hotkey_ready = true;
        Ok(())
    }

    fn trigger_lock(&self) -> Result<u64, PlatformError> {
        let mut state = self.state.lock().unwrap();
        state.emergency_state = EmergencyState::Locked;
        state.emergency_generation += 1;
        Ok(state.emergency_generation)
    }

    fn rearm(&self, expected_generation: u64) -> Result<u64, PlatformError> {
        let mut state = self.state.lock().unwrap();
        if state.emergency_generation != expected_generation {
            return Err(PlatformError::invalid_argument("急停代次不匹配。"));
        }
        state.emergency_state = EmergencyState::Armed;
        state.emergency_generation += 1;
        Ok(state.emergency_generation)
    }
}

impl CredentialStore for FakeDesktopSession {
    fn read_secret(&self, service: &str, account: &str) -> Result<Vec<u8>, PlatformError> {
        self.state
            .lock()
            .unwrap()
            .secrets
            .get(&(service.to_owned(), account.to_owned()))
            .cloned()
            .ok_or_else(|| PlatformError::credential_unavailable("未找到凭据。"))
    }

    fn write_secret(
        &self,
        service: &str,
        account: &str,
        secret: &[u8],
    ) -> Result<(), PlatformError> {
        self.state
            .lock()
            .unwrap()
            .secrets
            .insert((service.to_owned(), account.to_owned()), secret.to_vec());
        Ok(())
    }

    fn delete_secret(&self, service: &str, account: &str) -> Result<(), PlatformError> {
        self.state
            .lock()
            .unwrap()
            .secrets
            .remove(&(service.to_owned(), account.to_owned()));
        Ok(())
    }
}
