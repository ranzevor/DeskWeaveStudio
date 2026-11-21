use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureSet, FeatureStatus, InputClock, InputEmitter,
    InputRecorder, PlatformArch, PlatformError, PlatformOs, TargetBinding, TopologyCoverage,
    TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowsSessionFacts {
    pub interactive_session: bool,
}

impl WindowsSessionFacts {
    pub fn probe() -> Self {
        #[cfg(windows)]
        {
            Self {
                interactive_session: probe_interactive_window_station(),
            }
        }
        #[cfg(not(windows))]
        {
            Self {
                interactive_session: false,
            }
        }
    }
}

#[cfg(windows)]
fn probe_interactive_window_station() -> bool {
    use std::ffi::c_void;

    const UOI_FLAGS: u32 = 1;
    const WSF_VISIBLE: u32 = 0x0001;

    #[repr(C)]
    struct UserObjectFlags {
        inherit: i32,
        reserved: i32,
        flags: u32,
    }

    #[link(name = "user32")]
    unsafe extern "system" {
        fn GetProcessWindowStation() -> *mut c_void;
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
    if winsta.is_null() {
        return false;
    }
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
}

pub fn build_capabilities(
    arch: PlatformArch,
    facts: WindowsSessionFacts,
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

pub struct WindowsBackend {
    arch: PlatformArch,
    facts: WindowsSessionFacts,
    ports: UnimplementedPorts,
}

impl WindowsBackend {
    pub fn detect() -> Self {
        Self::from_facts(PlatformArch::current(), WindowsSessionFacts::probe())
    }

    pub fn from_facts(arch: PlatformArch, facts: WindowsSessionFacts) -> Self {
        Self {
            arch,
            facts,
            ports: UnimplementedPorts::new(PlatformOs::Windows),
        }
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
        build_capabilities(self.arch, self.facts, 1)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        if self.facts.interactive_session {
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
    use super::*;
    use deskweave_platform_api::FeatureState;

    #[test]
    fn windows_capabilities_are_valid_and_never_forge_available_in_m1() {
        let interactive = WindowsBackend::from_facts(
            PlatformArch::X64,
            WindowsSessionFacts {
                interactive_session: true,
            },
        );
        let caps = interactive.capabilities();
        caps.validate().unwrap();
        assert_eq!(caps.os, PlatformOs::Windows);
        assert_eq!(caps.backend, BackendKind::WindowsWin32);
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

    #[cfg(windows)]
    #[test]
    fn windows_live_probe_produces_valid_snapshot() {
        let backend = WindowsBackend::detect();
        backend.capabilities().validate().unwrap();
    }
}
