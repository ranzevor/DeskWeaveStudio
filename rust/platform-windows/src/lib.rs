use std::sync::Mutex;

use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureSet, FeatureStatus, InputClock, InputEmitter,
    InputRecorder, MAX_SAFE_INT, PlatformArch, PlatformError, PlatformOs, TargetBinding,
    TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsSessionFacts {
    pub interactive_session: bool,
    pub desktop_name: Option<String>,
}

impl WindowsSessionFacts {
    pub fn probe() -> Self {
        #[cfg(windows)]
        {
            probe_windows_session()
        }
        #[cfg(not(windows))]
        {
            Self {
                interactive_session: false,
                desktop_name: None,
            }
        }
    }
}

#[cfg(windows)]
fn probe_windows_session() -> WindowsSessionFacts {
    use std::ffi::c_void;

    const UOI_FLAGS: u32 = 1;
    const UOI_NAME: u32 = 2;
    const WSF_VISIBLE: u32 = 0x0001;
    const DESKTOP_READOBJECTS: u32 = 0x0001;

    #[repr(C)]
    struct UserObjectFlags {
        inherit: i32,
        reserved: i32,
        flags: u32,
    }

    #[link(name = "user32")]
    unsafe extern "system" {
        fn GetProcessWindowStation() -> *mut c_void;
        fn OpenInputDesktop(flags: u32, inherit: i32, desired_access: u32) -> *mut c_void;
        fn CloseDesktop(desktop: *mut c_void) -> i32;
        fn GetUserObjectInformationW(
            obj: *mut c_void,
            index: u32,
            info: *mut c_void,
            length: u32,
            needed: *mut u32,
        ) -> i32;
    }

    // Read-only query on the current process window station; never closes the returned handle.
    let winsta = unsafe { GetProcessWindowStation() };
    let winsta_visible = if winsta.is_null() {
        false
    } else {
        let mut flags = UserObjectFlags {
            inherit: 0,
            reserved: 0,
            flags: 0,
        };
        let mut needed = 0_u32;
        let ok = unsafe {
            GetUserObjectInformationW(
                winsta,
                UOI_FLAGS,
                (&raw mut flags).cast(),
                size_of::<UserObjectFlags>() as u32,
                &raw mut needed,
            )
        };
        ok != 0 && (flags.flags & WSF_VISIBLE) != 0
    };

    if !winsta_visible {
        return WindowsSessionFacts {
            interactive_session: false,
            desktop_name: None,
        };
    }

    // Read-only check of the active input desktop; fails on UAC/Winlogon secure desktop or locked/disconnected sessions.
    let desktop = unsafe { OpenInputDesktop(0, 0, DESKTOP_READOBJECTS) };
    if desktop.is_null() {
        return WindowsSessionFacts {
            interactive_session: false,
            desktop_name: None,
        };
    }

    let mut name_buf = [0_u16; 128];
    let mut needed = 0_u32;
    let ok = unsafe {
        GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            name_buf.as_mut_ptr().cast(),
            (name_buf.len() * size_of::<u16>()) as u32,
            &raw mut needed,
        )
    };
    unsafe {
        let _ = CloseDesktop(desktop);
    }

    let desktop_name = if ok != 0 {
        let len = name_buf
            .iter()
            .position(|&ch| ch == 0)
            .unwrap_or(name_buf.len());
        Some(String::from_utf16_lossy(&name_buf[..len]))
    } else {
        Some(String::new())
    };

    WindowsSessionFacts {
        interactive_session: true,
        desktop_name,
    }
}

pub fn build_capabilities(
    arch: PlatformArch,
    facts: &WindowsSessionFacts,
    generation: u64,
) -> CapabilitySnapshot {
    let status = if facts.interactive_session {
        FeatureStatus::unimplemented()
    } else {
        FeatureStatus::session_unavailable()
    };
    CapabilitySnapshot {
        os: PlatformOs::Windows,
        arch,
        backend: BackendKind::WindowsWin32,
        backend_version: BACKEND_VERSION.to_owned(),
        generation,
        topology_version: None,
        topology_coverage: TopologyCoverage::Unknown,
        trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
        features: FeatureSet::all(status),
    }
}

struct TrackedWindowsState {
    last_facts: Option<WindowsSessionFacts>,
    generation: u64,
    invalidated: bool,
}

type WindowsProbeFn = Box<dyn Fn() -> WindowsSessionFacts + Send + Sync>;

pub struct WindowsBackend {
    arch: PlatformArch,
    probe: WindowsProbeFn,
    state: Mutex<TrackedWindowsState>,
    ports: UnimplementedPorts,
}

impl WindowsBackend {
    pub fn detect() -> Self {
        Self::with_probe(PlatformArch::current(), WindowsSessionFacts::probe)
    }

    pub fn with_probe(
        arch: PlatformArch,
        probe: impl Fn() -> WindowsSessionFacts + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedWindowsState {
                last_facts: None,
                generation: 1,
                invalidated: false,
            }),
            ports: UnimplementedPorts::new(PlatformOs::Windows),
        }
    }

    pub fn from_facts(arch: PlatformArch, facts: WindowsSessionFacts) -> Self {
        Self::with_probe(arch, move || facts.clone())
    }

    /// Marks the current capability snapshot stale so the next observation increments generation.
    pub fn invalidate(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.invalidated = true;
    }

    fn refresh(&self) -> (WindowsSessionFacts, u64) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let facts = (self.probe)();
        match &state.last_facts {
            None => {
                state.last_facts = Some(facts.clone());
                state.invalidated = false;
            }
            Some(prev) if *prev != facts || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_facts = Some(facts.clone());
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (facts, state.generation)
    }
}

impl DesktopSession for WindowsBackend {
    fn os(&self) -> PlatformOs {
        PlatformOs::Windows
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        BackendKind::WindowsWin32
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        let (facts, generation) = self.refresh();
        build_capabilities(self.arch, &facts, generation)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        let (facts, _) = self.refresh();
        if facts.interactive_session {
            Err(PlatformError::capability_not_implemented(
                "Windows 原生显示拓扑枚举尚未实现。",
            ))
        } else {
            Err(PlatformError::session_unavailable(
                "当前进程不在可交互 Windows 桌面会话中。",
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
    use deskweave_platform_api::FeatureState;

    #[test]
    fn windows_capabilities_are_valid_and_never_forge_available_in_m1() {
        let interactive = WindowsBackend::from_facts(
            PlatformArch::X64,
            WindowsSessionFacts {
                interactive_session: true,
                desktop_name: Some("Default".to_owned()),
            },
        );
        let caps = interactive.capabilities();
        caps.validate().unwrap();
        assert_eq!(caps.os, PlatformOs::Windows);
        assert_eq!(caps.backend, BackendKind::WindowsWin32);
        assert_eq!(caps.generation, 1);
        assert!(
            caps.features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::Unimplemented)
        );
        assert_eq!(
            interactive.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );

        let headless = WindowsBackend::from_facts(
            PlatformArch::X64,
            WindowsSessionFacts {
                interactive_session: false,
                desktop_name: None,
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
    fn windows_backend_refreshes_on_desktop_switch_and_bumps_generation() {
        let current = Arc::new(Mutex::new(WindowsSessionFacts {
            interactive_session: true,
            desktop_name: Some("Default".to_owned()),
        }));
        let reader = Arc::clone(&current);
        let backend =
            WindowsBackend::with_probe(PlatformArch::X64, move || reader.lock().unwrap().clone());

        let c1 = backend.capabilities();
        assert_eq!(c1.generation, 1);
        assert_eq!(
            c1.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        // Stable state keeps generation unchanged.
        assert_eq!(backend.capabilities().generation, 1);

        // Switching to a secure/locked desktop makes session unavailable and bumps generation.
        *current.lock().unwrap() = WindowsSessionFacts {
            interactive_session: false,
            desktop_name: None,
        };
        let c2 = backend.capabilities();
        c2.validate().unwrap();
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");

        // Returning to interactive desktop restores unimplemented state and bumps generation again.
        *current.lock().unwrap() = WindowsSessionFacts {
            interactive_session: true,
            desktop_name: Some("Default".to_owned()),
        };
        let c3 = backend.capabilities();
        c3.validate().unwrap();
        assert_eq!(c3.generation, 3);
        assert_eq!(
            c3.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        // Switching between distinct interactive desktops bumps generation even when interactive stays true.
        *current.lock().unwrap() = WindowsSessionFacts {
            interactive_session: true,
            desktop_name: Some("DeskWeaveAlt".to_owned()),
        };
        assert_eq!(backend.capabilities().generation, 4);

        // Explicit invalidation bumps generation on next read.
        backend.invalidate();
        assert_eq!(backend.capabilities().generation, 5);
    }

    #[test]
    fn windows_backend_serializes_concurrent_probes_and_recovers_from_poison() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        use std::sync::{Arc, Mutex, mpsc};
        use std::thread;

        let facts = Arc::new(Mutex::new(WindowsSessionFacts {
            interactive_session: true,
            desktop_name: Some("Default".to_owned()),
        }));
        let in_probe = Arc::new(AtomicBool::new(false));
        let calls = Arc::new(AtomicUsize::new(0));
        let panic_next = Arc::new(AtomicBool::new(false));
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let release_rx = Arc::new(Mutex::new(release_rx));

        let facts_p = Arc::clone(&facts);
        let in_probe_p = Arc::clone(&in_probe);
        let calls_p = Arc::clone(&calls);
        let panic_p = Arc::clone(&panic_next);
        let release_p = Arc::clone(&release_rx);
        let backend = Arc::new(WindowsBackend::with_probe(PlatformArch::X64, move || {
            assert!(
                !in_probe_p.swap(true, Ordering::SeqCst),
                "concurrent probe overlapped outside lock"
            );
            struct Guard(Arc<AtomicBool>);
            impl Drop for Guard {
                fn drop(&mut self) {
                    self.0.store(false, Ordering::SeqCst);
                }
            }
            let _guard = Guard(Arc::clone(&in_probe_p));

            if panic_p.swap(false, Ordering::SeqCst) {
                panic!("simulated transient probe failure");
            }

            let call_no = calls_p.fetch_add(1, Ordering::SeqCst);
            let snapshot = facts_p.lock().unwrap().clone();
            if call_no == 0 {
                entered_tx.send(()).unwrap();
                release_p.lock().unwrap().recv().unwrap();
            }
            snapshot
        }));

        let b1 = Arc::clone(&backend);
        let t1 = thread::spawn(move || b1.capabilities());
        entered_rx.recv().unwrap();

        // Transition desktop state while Thread 1 is still inside probe holding the state lock.
        *facts.lock().unwrap() = WindowsSessionFacts {
            interactive_session: false,
            desktop_name: None,
        };
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
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        // No stale overwrite or spurious generation jump on subsequent read.
        let c3 = backend.capabilities();
        assert_eq!(c3.generation, 2);
        assert_eq!(
            c3.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );

        // Poison recovery: a panic inside probe must not permanently brick the backend.
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
            FeatureState::SessionUnavailable
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_live_probe_produces_valid_snapshot() {
        let backend = WindowsBackend::detect();
        backend.capabilities().validate().unwrap();
    }
}
