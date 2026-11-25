use std::sync::Mutex;
use std::time::{Duration, Instant};

use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureReason, FeatureSet, FeatureState, FeatureStatus,
    InputClock, InputEmitter, InputRecorder, MAX_SAFE_INT, NextAction, PlatformArch, PlatformError,
    PlatformOs, TargetBinding, TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";
const LIVE_SESSION_PROBE_TTL: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacOsSessionFacts {
    pub graphical_session: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PermissionGrant {
    Granted,
    Missing,
    Denied,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacOsPermissionFacts {
    pub screen_recording: PermissionGrant,
    pub input_monitoring: PermissionGrant,
    pub accessibility: PermissionGrant,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacOsRawProbe {
    pub graphical_session: bool,
    pub screen_recording: bool,
    pub input_monitoring: bool,
    pub accessibility: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacOsProbeState {
    pub session: MacOsSessionFacts,
    pub permissions: MacOsPermissionFacts,
}

/// Reconciles read-only macOS preflight booleans with previously observed grants so
/// runtime permission revocations surface as `Denied` rather than `Missing`.
#[derive(Debug, Default)]
pub struct MacOsPermissionTracker {
    screen_recording_ever_granted: bool,
    input_monitoring_ever_granted: bool,
    accessibility_ever_granted: bool,
}

impl MacOsPermissionTracker {
    pub fn new() -> Self {
        Self::default()
    }

    fn reconcile(ever_granted: &mut bool, granted_now: bool) -> PermissionGrant {
        if granted_now {
            *ever_granted = true;
            PermissionGrant::Granted
        } else if *ever_granted {
            PermissionGrant::Denied
        } else {
            PermissionGrant::Missing
        }
    }

    pub fn observe(&mut self, raw: MacOsRawProbe) -> MacOsProbeState {
        if !raw.graphical_session {
            return MacOsProbeState {
                session: MacOsSessionFacts {
                    graphical_session: false,
                },
                permissions: MacOsPermissionFacts {
                    screen_recording: if self.screen_recording_ever_granted {
                        PermissionGrant::Denied
                    } else {
                        PermissionGrant::Missing
                    },
                    input_monitoring: if self.input_monitoring_ever_granted {
                        PermissionGrant::Denied
                    } else {
                        PermissionGrant::Missing
                    },
                    accessibility: if self.accessibility_ever_granted {
                        PermissionGrant::Denied
                    } else {
                        PermissionGrant::Missing
                    },
                },
            };
        }
        MacOsProbeState {
            session: MacOsSessionFacts {
                graphical_session: true,
            },
            permissions: MacOsPermissionFacts {
                screen_recording: Self::reconcile(
                    &mut self.screen_recording_ever_granted,
                    raw.screen_recording,
                ),
                input_monitoring: Self::reconcile(
                    &mut self.input_monitoring_ever_granted,
                    raw.input_monitoring,
                ),
                accessibility: Self::reconcile(
                    &mut self.accessibility_ever_granted,
                    raw.accessibility,
                ),
            },
        }
    }
}

impl MacOsRawProbe {
    pub fn probe() -> Self {
        #[cfg(target_os = "macos")]
        {
            probe_macos_raw()
        }
        #[cfg(not(target_os = "macos"))]
        {
            Self {
                graphical_session: false,
                screen_recording: false,
                input_monitoring: false,
                accessibility: false,
            }
        }
    }
}

impl MacOsSessionFacts {
    pub fn probe() -> Self {
        #[cfg(target_os = "macos")]
        {
            Self {
                graphical_session: probe_macos_graphical_session(),
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            Self {
                graphical_session: false,
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn probe_macos_graphical_session() -> bool {
    use std::ffi::c_void;

    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGSessionCopyCurrentDictionary() -> *const c_void;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        fn CFRelease(value: *const c_void);
    }

    // Read-only WindowServer session query; never triggers any TCC privacy prompt.
    let dict = unsafe { CGSessionCopyCurrentDictionary() };
    if dict.is_null() {
        false
    } else {
        unsafe { CFRelease(dict) };
        true
    }
}

#[cfg(target_os = "macos")]
fn probe_macos_raw() -> MacOsRawProbe {
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGPreflightScreenCaptureAccess() -> u8;
        fn CGPreflightListenEventAccess() -> u8;
    }

    #[link(name = "ApplicationServices", kind = "framework")]
    unsafe extern "C" {
        fn AXIsProcessTrusted() -> u8;
    }

    if !probe_macos_graphical_session() {
        return MacOsRawProbe {
            graphical_session: false,
            screen_recording: false,
            input_monitoring: false,
            accessibility: false,
        };
    }

    // All three TCC preflights are strictly read-only and never open a system consent dialog.
    let screen_recording = unsafe { CGPreflightScreenCaptureAccess() != 0 };
    let input_monitoring = unsafe { CGPreflightListenEventAccess() != 0 };
    let accessibility = unsafe { AXIsProcessTrusted() != 0 };
    MacOsRawProbe {
        graphical_session: true,
        screen_recording,
        input_monitoring,
        accessibility,
    }
}

pub fn project_permission(grant: PermissionGrant) -> FeatureStatus {
    match grant {
        PermissionGrant::Granted => FeatureStatus::unimplemented(),
        PermissionGrant::Missing => FeatureStatus::permission_required(
            FeatureReason::PermissionMissing,
            NextAction::GrantPermission,
        ),
        PermissionGrant::Denied => FeatureStatus::denied(NextAction::GrantPermission),
    }
}

pub fn build_capabilities(
    arch: PlatformArch,
    facts: MacOsSessionFacts,
    permissions: MacOsPermissionFacts,
    generation: u64,
) -> CapabilitySnapshot {
    let features = if !facts.graphical_session {
        FeatureSet::all(FeatureStatus::session_unavailable())
    } else {
        let capture = project_permission(permissions.screen_recording);
        let record = project_permission(permissions.input_monitoring);
        let input = project_permission(permissions.accessibility);
        let all_displays = if record.state == FeatureState::Unimplemented {
            FeatureStatus::unimplemented()
        } else {
            record
        };
        FeatureSet {
            capture_display: capture,
            capture_window: capture,
            record_pointer_absolute: record,
            record_pointer_relative: record,
            record_keyboard: record,
            record_all_displays: all_displays,
            input_pointer_absolute: input,
            input_pointer_relative: input,
            input_keyboard: input,
            input_unicode_text: input,
            control_global_shortcut: input,
            control_focus_target: input,
        }
    };
    CapabilitySnapshot {
        os: PlatformOs::Macos,
        arch,
        backend: BackendKind::MacosQuartz,
        backend_version: BACKEND_VERSION.to_owned(),
        generation,
        topology_version: None,
        topology_coverage: TopologyCoverage::Unknown,
        trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
        features,
    }
}

struct TrackedMacOsState {
    last_state: Option<MacOsProbeState>,
    generation: u64,
    invalidated: bool,
}

type MacOsProbeFn = Box<dyn Fn() -> MacOsProbeState + Send + Sync>;
type MacOsSessionProbeFn = Box<dyn Fn() -> bool + Send + Sync>;

struct SessionProbeCache {
    ttl: Option<Duration>,
    entry: Mutex<Option<(bool, Instant)>>,
}

impl SessionProbeCache {
    fn disabled() -> Self {
        Self {
            ttl: None,
            entry: Mutex::new(None),
        }
    }

    fn with_ttl(ttl: Duration) -> Self {
        Self {
            ttl: Some(ttl),
            entry: Mutex::new(None),
        }
    }

    fn clear(&self) {
        *self
            .entry
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    fn record(&self, graphical_session: bool) {
        if self.ttl.is_some() {
            *self
                .entry
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) =
                Some((graphical_session, Instant::now()));
        }
    }

    fn get_or_probe(&self, probe: &MacOsSessionProbeFn) -> bool {
        let Some(ttl) = self.ttl else {
            return probe();
        };
        {
            let cache = self
                .entry
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if let Some((cached, at)) = *cache
                && at.elapsed() < ttl
            {
                return cached;
            }
        }
        let live = probe();
        *self
            .entry
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some((live, Instant::now()));
        live
    }
}

pub struct MacOsBackend {
    arch: PlatformArch,
    probe: MacOsProbeFn,
    session_probe: Option<MacOsSessionProbeFn>,
    session_cache: SessionProbeCache,
    state: Mutex<TrackedMacOsState>,
    ports: UnimplementedPorts,
}

impl MacOsBackend {
    pub fn detect() -> Self {
        let tracker = Mutex::new(MacOsPermissionTracker::new());
        let mut backend = Self::with_probe(PlatformArch::current(), move || {
            let raw = MacOsRawProbe::probe();
            tracker
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .observe(raw)
        });
        backend.session_probe = Some(Box::new(|| MacOsSessionFacts::probe().graphical_session));
        backend.session_cache = SessionProbeCache::with_ttl(LIVE_SESSION_PROBE_TTL);
        backend
    }

    pub fn with_probe(
        arch: PlatformArch,
        probe: impl Fn() -> MacOsProbeState + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            session_probe: None,
            session_cache: SessionProbeCache::disabled(),
            state: Mutex::new(TrackedMacOsState {
                last_state: None,
                generation: 1,
                invalidated: false,
            }),
            ports: UnimplementedPorts::new(PlatformOs::Macos),
        }
    }

    #[cfg(test)]
    fn with_session_probe(
        arch: PlatformArch,
        probe: impl Fn() -> MacOsProbeState + Send + Sync + 'static,
        session_probe: impl Fn() -> bool + Send + Sync + 'static,
    ) -> Self {
        let mut backend = Self::with_probe(arch, probe);
        backend.session_probe = Some(Box::new(session_probe));
        backend
    }

    #[cfg(test)]
    fn with_cached_session_probe(
        arch: PlatformArch,
        probe: impl Fn() -> MacOsProbeState + Send + Sync + 'static,
        session_probe: impl Fn() -> bool + Send + Sync + 'static,
        ttl: Duration,
    ) -> Self {
        let mut backend = Self::with_session_probe(arch, probe, session_probe);
        backend.session_cache = SessionProbeCache::with_ttl(ttl);
        backend
    }

    pub fn with_raw_probe(
        arch: PlatformArch,
        probe: impl Fn() -> MacOsRawProbe + Send + Sync + 'static,
    ) -> Self {
        let tracker = Mutex::new(MacOsPermissionTracker::new());
        Self::with_probe(arch, move || {
            let raw = probe();
            tracker
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .observe(raw)
        })
    }

    pub fn from_facts(
        arch: PlatformArch,
        facts: MacOsSessionFacts,
        permissions: MacOsPermissionFacts,
    ) -> Self {
        let state = MacOsProbeState {
            session: facts,
            permissions,
        };
        Self::with_probe(arch, move || state)
    }

    /// Marks the current capability snapshot stale so the next observation increments generation.
    pub fn invalidate(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.invalidated = true;
        self.session_cache.clear();
    }

    fn refresh(&self) -> (MacOsProbeState, u64) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let probed = (self.probe)();
        self.session_cache.record(probed.session.graphical_session);
        match state.last_state {
            None => {
                state.last_state = Some(probed);
                state.invalidated = false;
            }
            Some(prev) if prev != probed || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_state = Some(probed);
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (probed, state.generation)
    }
}

impl DesktopSession for MacOsBackend {
    fn os(&self) -> PlatformOs {
        PlatformOs::Macos
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        BackendKind::MacosQuartz
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        let (probed, generation) = self.refresh();
        build_capabilities(self.arch, probed.session, probed.permissions, generation)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        let graphical_session = match &self.session_probe {
            Some(probe) => self.session_cache.get_or_probe(probe),
            None => self.refresh().0.session.graphical_session,
        };
        if graphical_session {
            Err(PlatformError::capability_not_implemented(
                "macOS 原生显示拓扑枚举尚未实现。",
            ))
        } else {
            Err(PlatformError::session_unavailable(
                "当前进程不在活动 macOS 图形会话中。",
            ))
        }
    }

    fn clock(&self) -> &dyn InputClock {
        self.ports.clock()
    }

    fn targets(&self) -> &dyn TargetBinding {
        &self.ports
    }

    fn capture(&self) -> &dyn CaptureSource {
        &self.ports
    }

    fn recorder(&self) -> &dyn InputRecorder {
        &self.ports
    }

    fn emitter(&self) -> &dyn InputEmitter {
        &self.ports
    }

    fn emergency(&self) -> &dyn EmergencyControl {
        &self.ports
    }

    fn credentials(&self) -> &dyn CredentialStore {
        &self.ports
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;

    #[test]
    fn macos_capabilities_separate_tcc_domains_and_never_forge_available_in_m1() {
        let caps = build_capabilities(
            PlatformArch::Arm64,
            MacOsSessionFacts {
                graphical_session: true,
            },
            MacOsPermissionFacts {
                screen_recording: PermissionGrant::Missing,
                input_monitoring: PermissionGrant::Denied,
                accessibility: PermissionGrant::Granted,
            },
            2,
        );
        caps.validate().unwrap();
        assert_eq!(
            caps.features.capture_display.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(
            caps.features.record_all_displays.state,
            FeatureState::Denied
        );
        assert_eq!(
            caps.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        let headless = MacOsBackend::from_facts(
            PlatformArch::Arm64,
            MacOsSessionFacts {
                graphical_session: false,
            },
            MacOsPermissionFacts {
                screen_recording: PermissionGrant::Granted,
                input_monitoring: PermissionGrant::Granted,
                accessibility: PermissionGrant::Granted,
            },
        );
        let h_caps = headless.capabilities();
        h_caps.validate().unwrap();
        assert!(
            h_caps
                .features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::SessionUnavailable)
        );
        assert_eq!(headless.topology().unwrap_err().code, "SESSION_UNAVAILABLE");
    }

    #[test]
    fn macos_backend_tracks_live_tcc_grant_revocation_and_session_transitions() {
        let raw = Arc::new(Mutex::new(MacOsRawProbe {
            graphical_session: true,
            screen_recording: false,
            input_monitoring: false,
            accessibility: false,
        }));
        let reader = Arc::clone(&raw);
        let backend =
            MacOsBackend::with_raw_probe(PlatformArch::Arm64, move || *reader.lock().unwrap());

        // Step 1: All three TCC permissions initially missing -> permissionRequired, generation 1.
        let c1 = backend.capabilities();
        c1.validate().unwrap();
        assert_eq!(c1.generation, 1);
        assert_eq!(
            c1.features.capture_display.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(
            c1.features.record_keyboard.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(
            c1.features.input_keyboard.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(backend.capabilities().generation, 1);

        // Step 2: Grant Input Monitoring & Accessibility while Screen Recording remains missing.
        *raw.lock().unwrap() = MacOsRawProbe {
            graphical_session: true,
            screen_recording: false,
            input_monitoring: true,
            accessibility: true,
        };
        let c2 = backend.capabilities();
        c2.validate().unwrap();
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.capture_display.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(
            c2.features.record_all_displays.state,
            FeatureState::Unimplemented
        );
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        // Step 3: Revoke Accessibility -> surfaces as Denied while Input Monitoring stays granted (Unimplemented).
        *raw.lock().unwrap() = MacOsRawProbe {
            graphical_session: true,
            screen_recording: false,
            input_monitoring: true,
            accessibility: false,
        };
        let c3 = backend.capabilities();
        c3.validate().unwrap();
        assert_eq!(c3.generation, 3);
        assert_eq!(
            c3.features.capture_display.state,
            FeatureState::PermissionRequired
        );
        assert_eq!(
            c3.features.record_keyboard.state,
            FeatureState::Unimplemented
        );
        assert_eq!(c3.features.input_keyboard.state, FeatureState::Denied);
        assert_eq!(
            c3.features.input_keyboard.reason,
            FeatureReason::PermissionDenied
        );

        // Step 4: Leave graphical session -> all SessionUnavailable, generation bumps to 4.
        *raw.lock().unwrap() = MacOsRawProbe {
            graphical_session: false,
            screen_recording: false,
            input_monitoring: false,
            accessibility: false,
        };
        let c4 = backend.capabilities();
        c4.validate().unwrap();
        assert_eq!(c4.generation, 4);
        assert!(
            c4.features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::SessionUnavailable)
        );
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");

        // Step 5: Explicit invalidation bumps generation.
        backend.invalidate();
        assert_eq!(backend.capabilities().generation, 5);
    }

    #[test]
    fn macos_backend_serializes_concurrent_raw_probes_and_recovers_from_poison() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        use std::sync::{Arc, Mutex, mpsc};
        use std::thread;

        let raw = Arc::new(Mutex::new(MacOsRawProbe {
            graphical_session: true,
            screen_recording: true,
            input_monitoring: true,
            accessibility: true,
        }));
        let in_probe = Arc::new(AtomicBool::new(false));
        let calls = Arc::new(AtomicUsize::new(0));
        let panic_next = Arc::new(AtomicBool::new(false));
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let release_rx = Arc::new(Mutex::new(release_rx));

        let raw_p = Arc::clone(&raw);
        let in_probe_p = Arc::clone(&in_probe);
        let calls_p = Arc::clone(&calls);
        let panic_p = Arc::clone(&panic_next);
        let release_p = Arc::clone(&release_rx);
        let backend = Arc::new(MacOsBackend::with_raw_probe(
            PlatformArch::Arm64,
            move || {
                assert!(
                    !in_probe_p.swap(true, Ordering::SeqCst),
                    "concurrent macOS TCC probe overlapped outside lock"
                );
                struct Guard(Arc<AtomicBool>);
                impl Drop for Guard {
                    fn drop(&mut self) {
                        self.0.store(false, Ordering::SeqCst);
                    }
                }
                let _guard = Guard(Arc::clone(&in_probe_p));

                if panic_p.swap(false, Ordering::SeqCst) {
                    panic!("simulated transient TCC probe failure");
                }

                let call_no = calls_p.fetch_add(1, Ordering::SeqCst);
                let snapshot = *raw_p.lock().unwrap();
                if call_no == 0 {
                    entered_tx.send(()).unwrap();
                    release_p.lock().unwrap().recv().unwrap();
                }
                snapshot
            },
        ));

        let b1 = Arc::clone(&backend);
        let t1 = thread::spawn(move || b1.capabilities());
        entered_rx.recv().unwrap();

        // Revoke Accessibility while Thread 1 holds the state lock inside the initial granted probe.
        raw.lock().unwrap().accessibility = false;
        let b2 = Arc::clone(&backend);
        let t2 = thread::spawn(move || b2.capabilities());

        release_tx.send(()).unwrap();
        let c1 = t1.join().unwrap();
        let c2 = t2.join().unwrap();

        assert_eq!(c1.generation, 1);
        assert_eq!(
            c1.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        // Because Thread 1 strictly serialized before Thread 2, tracker observed Granted -> Denied!
        assert_eq!(c2.generation, 2);
        assert_eq!(c2.features.input_keyboard.state, FeatureState::Denied);
        assert_eq!(
            c2.features.input_keyboard.reason,
            FeatureReason::PermissionDenied
        );
        assert_eq!(backend.capabilities().generation, 2);

        // Poison recovery preserves ever-granted Denied state and recovers cleanly.
        panic_next.store(true, Ordering::SeqCst);
        assert!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.capabilities()))
                .is_err()
        );
        backend.invalidate();
        let recovered = backend.capabilities();
        assert_eq!(recovered.generation, 3);
        assert_eq!(
            recovered.features.input_keyboard.state,
            FeatureState::Denied
        );
    }

    #[test]
    fn macos_topology_uses_lightweight_session_probe_without_tcc_calls() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        let full_probe_calls = Arc::new(AtomicUsize::new(0));
        let graphical_session = Arc::new(AtomicBool::new(true));

        let full_probe_calls_p = Arc::clone(&full_probe_calls);
        let graphical_p = Arc::clone(&graphical_session);
        let graphical_s = Arc::clone(&graphical_session);
        let backend = MacOsBackend::with_session_probe(
            PlatformArch::Arm64,
            move || {
                full_probe_calls_p.fetch_add(1, Ordering::SeqCst);
                MacOsProbeState {
                    session: MacOsSessionFacts {
                        graphical_session: graphical_p.load(Ordering::SeqCst),
                    },
                    permissions: MacOsPermissionFacts {
                        screen_recording: PermissionGrant::Missing,
                        input_monitoring: PermissionGrant::Missing,
                        accessibility: PermissionGrant::Missing,
                    },
                }
            },
            move || graphical_s.load(Ordering::SeqCst),
        );

        assert_eq!(
            backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );
        graphical_session.store(false, Ordering::SeqCst);
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");
        graphical_session.store(true, Ordering::SeqCst);
        assert_eq!(
            backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );
        assert_eq!(full_probe_calls.load(Ordering::SeqCst), 0);

        let caps = backend.capabilities();
        caps.validate().unwrap();
        assert_eq!(full_probe_calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn macos_live_session_cache_reuses_within_ttl_and_clears_on_invalidate() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        let session_probe_calls = Arc::new(AtomicUsize::new(0));
        let graphical_session = Arc::new(AtomicBool::new(true));

        let graphical_p = Arc::clone(&graphical_session);
        let calls_s = Arc::clone(&session_probe_calls);
        let graphical_s = Arc::clone(&graphical_session);
        let backend = MacOsBackend::with_cached_session_probe(
            PlatformArch::Arm64,
            move || MacOsProbeState {
                session: MacOsSessionFacts {
                    graphical_session: graphical_p.load(Ordering::SeqCst),
                },
                permissions: MacOsPermissionFacts {
                    screen_recording: PermissionGrant::Missing,
                    input_monitoring: PermissionGrant::Missing,
                    accessibility: PermissionGrant::Missing,
                },
            },
            move || {
                calls_s.fetch_add(1, Ordering::SeqCst);
                graphical_s.load(Ordering::SeqCst)
            },
            Duration::from_millis(200),
        );

        assert_eq!(
            backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );
        assert_eq!(
            backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );
        assert_eq!(session_probe_calls.load(Ordering::SeqCst), 1);

        graphical_session.store(false, Ordering::SeqCst);
        backend.invalidate();
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");
        assert_eq!(session_probe_calls.load(Ordering::SeqCst), 2);

        let short_calls = Arc::new(AtomicUsize::new(0));
        let short_session = Arc::new(AtomicBool::new(true));
        let short_p = Arc::clone(&short_session);
        let short_calls_s = Arc::clone(&short_calls);
        let short_s = Arc::clone(&short_session);
        let short_backend = MacOsBackend::with_cached_session_probe(
            PlatformArch::Arm64,
            move || MacOsProbeState {
                session: MacOsSessionFacts {
                    graphical_session: short_p.load(Ordering::SeqCst),
                },
                permissions: MacOsPermissionFacts {
                    screen_recording: PermissionGrant::Missing,
                    input_monitoring: PermissionGrant::Missing,
                    accessibility: PermissionGrant::Missing,
                },
            },
            move || {
                short_calls_s.fetch_add(1, Ordering::SeqCst);
                short_s.load(Ordering::SeqCst)
            },
            Duration::from_millis(15),
        );

        assert_eq!(
            short_backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );
        assert_eq!(short_calls.load(Ordering::SeqCst), 1);

        short_session.store(false, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(25));
        assert_eq!(
            short_backend.topology().unwrap_err().code,
            "SESSION_UNAVAILABLE"
        );
        assert_eq!(short_calls.load(Ordering::SeqCst), 2);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_live_probe_produces_valid_snapshot() {
        let backend = MacOsBackend::detect();
        let caps = backend.capabilities();
        caps.validate().unwrap();
        // Live macOS runner must never forge available in M1.
        assert!(
            caps.features
                .entries()
                .iter()
                .all(|(_, s)| s.state != FeatureState::Available)
        );
        assert!(matches!(
            backend.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED" | "SESSION_UNAVAILABLE"
        ));
    }
}
