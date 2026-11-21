use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureReason, FeatureSet, FeatureState, FeatureStatus,
    InputClock, InputEmitter, InputRecorder, NextAction, PlatformArch, PlatformError, PlatformOs,
    TargetBinding, TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MacOsSessionFacts {
    pub graphical_session: bool,
}

impl MacOsSessionFacts {
    pub fn probe() -> Self {
        #[cfg(target_os = "macos")]
        {
            Self {
                graphical_session: probe_graphical_session(),
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
fn probe_graphical_session() -> bool {
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
        return false;
    }
    unsafe { CFRelease(dict) };
    true
}

/// Pure permission projection helper for testing macOS TCC capability separation
/// (screen recording, input monitoring, and accessibility).
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
    permissions: Option<MacOsPermissionFacts>,
    generation: u64,
) -> CapabilitySnapshot {
    let features = if !facts.graphical_session {
        FeatureSet::all(FeatureStatus::session_unavailable())
    } else if let Some(perms) = permissions {
        let capture = project_permission(perms.screen_recording);
        let record = project_permission(perms.input_monitoring);
        let input = project_permission(perms.accessibility);
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
    } else {
        FeatureSet::all(FeatureStatus::unimplemented())
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

pub struct MacOsBackend {
    arch: PlatformArch,
    facts: MacOsSessionFacts,
    ports: UnimplementedPorts,
}

impl MacOsBackend {
    pub fn detect() -> Self {
        Self::from_facts(PlatformArch::current(), MacOsSessionFacts::probe())
    }

    pub fn from_facts(arch: PlatformArch, facts: MacOsSessionFacts) -> Self {
        Self {
            arch,
            facts,
            ports: UnimplementedPorts::new(PlatformOs::Macos),
        }
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
        build_capabilities(self.arch, self.facts, None, 1)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        if self.facts.graphical_session {
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
    use super::*;

    #[test]
    fn macos_capabilities_separate_tcc_domains_and_never_forge_available_in_m1() {
        let caps = build_capabilities(
            PlatformArch::Arm64,
            MacOsSessionFacts {
                graphical_session: true,
            },
            Some(MacOsPermissionFacts {
                screen_recording: PermissionGrant::Missing,
                input_monitoring: PermissionGrant::Denied,
                accessibility: PermissionGrant::Granted,
            }),
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

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_live_probe_produces_valid_snapshot() {
        let backend = MacOsBackend::detect();
        backend.capabilities().validate().unwrap();
    }
}
