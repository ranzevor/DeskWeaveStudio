use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureReason, FeatureSet, FeatureStatus, InputClock,
    InputEmitter, InputRecorder, NextAction, PlatformArch, PlatformError, PlatformOs,
    TargetBinding, TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

/// Read-only facts gathered from Linux login session metadata and real display sockets.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LinuxSessionFacts {
    pub logind_session_type: Option<String>,
    pub xdg_session_type: Option<String>,
    pub wayland_display: Option<String>,
    pub wayland_socket_present: bool,
    pub x11_display: Option<String>,
    pub x11_socket_present: bool,
    pub compositor_backend_requested: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectedLinuxBackend {
    WaylandPortal { active: bool },
    WaylandCompositor { active: bool },
    X11 { active: bool },
    Inactive,
}

fn normalized_type(value: Option<&str>) -> Option<&str> {
    let trimmed = value?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

/// Selects the real Linux desktop backend without trusting `DISPLAY` alone.
///
/// Precedence rules (aligned with `docs/development/engineering.md` and `docs/architecture/platforms.md`):
/// 1. Real Wayland session (`logind`/`XDG_SESSION_TYPE == wayland` or live Wayland socket) takes
///    precedence even when `DISPLAY` is also set (XWayland).
/// 2. Explicit `tty` / `unspecified` sessions without an active Wayland socket are inactive,
///    even if a stale `DISPLAY` environment variable is present.
/// 3. X11 is selected only when session metadata indicates `x11` (and no active Wayland socket).
///    If `x11` is declared but its socket is unreachable, X11 is returned with `active: false`.
pub fn select_linux_session(facts: &LinuxSessionFacts) -> SelectedLinuxBackend {
    let session_type = normalized_type(facts.logind_session_type.as_deref())
        .or_else(|| normalized_type(facts.xdg_session_type.as_deref()));

    let wayland_selected = matches!(session_type, Some("wayland")) || facts.wayland_socket_present;
    if wayland_selected {
        let active = facts.wayland_socket_present;
        return if facts.compositor_backend_requested {
            SelectedLinuxBackend::WaylandCompositor { active }
        } else {
            SelectedLinuxBackend::WaylandPortal { active }
        };
    }

    if matches!(session_type, Some("x11")) {
        return SelectedLinuxBackend::X11 {
            active: facts.x11_socket_present,
        };
    }

    SelectedLinuxBackend::Inactive
}

#[cfg(target_os = "linux")]
pub fn probe_linux_session_facts() -> LinuxSessionFacts {
    use std::env;
    use std::fs;
    use std::os::unix::fs::FileTypeExt;
    use std::path::{Path, PathBuf};

    let read_env = |key: &str| {
        env::var(key)
            .ok()
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
    };

    let session_id = read_env("XDG_SESSION_ID").or_else(|| {
        fs::read_to_string("/proc/self/sessionid")
            .ok()
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty() && s != "0" && s != "4294967295")
    });

    let logind_session_type = session_id
        .as_deref()
        .filter(|id| {
            !id.is_empty()
                && id.len() <= 64
                && id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
        })
        .and_then(|id| fs::read_to_string(format!("/run/systemd/sessions/{id}")).ok())
        .and_then(|body| {
            body.lines().find_map(|line| {
                line.strip_prefix("TYPE=")
                    .map(|val| val.trim().to_ascii_lowercase())
            })
        });

    let xdg_session_type = read_env("XDG_SESSION_TYPE").map(|v| v.to_ascii_lowercase());
    let wayland_display = read_env("WAYLAND_DISPLAY");
    let xdg_runtime_dir = read_env("XDG_RUNTIME_DIR");
    let wayland_socket_present = wayland_display.as_deref().is_some_and(|disp| {
        let path = if Path::new(disp).is_absolute() {
            PathBuf::from(disp)
        } else if let Some(runtime) = xdg_runtime_dir.as_deref() {
            Path::new(runtime).join(disp)
        } else {
            return false;
        };
        fs::symlink_metadata(path)
            .map(|meta| meta.file_type().is_socket())
            .unwrap_or(false)
    });

    let x11_display = read_env("DISPLAY");
    let x11_socket_present = x11_display
        .as_deref()
        .is_some_and(deskweave_platform_linux_x11::x11_socket_exists);

    LinuxSessionFacts {
        logind_session_type,
        xdg_session_type,
        wayland_display,
        wayland_socket_present,
        x11_display,
        x11_socket_present,
        compositor_backend_requested: false,
    }
}

#[cfg(not(target_os = "linux"))]
pub fn probe_linux_session_facts() -> LinuxSessionFacts {
    LinuxSessionFacts::default()
}

pub fn build_portal_capabilities(
    arch: PlatformArch,
    session_active: bool,
    generation: u64,
) -> CapabilitySnapshot {
    let features = if !session_active {
        FeatureSet::all(FeatureStatus::session_unavailable())
    } else {
        let no_passive =
            FeatureStatus::unsupported(FeatureReason::NoPassiveCapture, NextAction::SwitchSession);
        let unimplemented = FeatureStatus::unimplemented();
        FeatureSet {
            capture_display: unimplemented,
            capture_window: unimplemented,
            record_pointer_absolute: no_passive,
            record_pointer_relative: no_passive,
            record_keyboard: no_passive,
            record_all_displays: no_passive,
            input_pointer_absolute: unimplemented,
            input_pointer_relative: unimplemented,
            input_keyboard: unimplemented,
            input_unicode_text: unimplemented,
            control_global_shortcut: unimplemented,
            control_focus_target: FeatureStatus::unsupported(
                FeatureReason::ProtocolMissing,
                NextAction::None,
            ),
        }
    };
    CapabilitySnapshot {
        os: PlatformOs::Linux,
        arch,
        backend: BackendKind::LinuxWaylandPortal,
        backend_version: BACKEND_VERSION.to_owned(),
        generation,
        topology_version: None,
        topology_coverage: TopologyCoverage::Unknown,
        trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
        features,
    }
}

pub fn build_compositor_capabilities(
    arch: PlatformArch,
    session_active: bool,
    generation: u64,
) -> CapabilitySnapshot {
    let status = if session_active {
        FeatureStatus::unimplemented()
    } else {
        FeatureStatus::session_unavailable()
    };
    CapabilitySnapshot {
        os: PlatformOs::Linux,
        arch,
        backend: BackendKind::LinuxWaylandCompositor,
        backend_version: BACKEND_VERSION.to_owned(),
        generation,
        topology_version: None,
        topology_coverage: TopologyCoverage::Unknown,
        trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
        features: FeatureSet::all(status),
    }
}

pub struct LinuxWaylandPortalBackend {
    arch: PlatformArch,
    session_active: bool,
    ports: UnimplementedPorts,
}

impl LinuxWaylandPortalBackend {
    pub fn new(arch: PlatformArch, session_active: bool) -> Self {
        Self {
            arch,
            session_active,
            ports: UnimplementedPorts::new(PlatformOs::Linux),
        }
    }
}

impl DesktopSession for LinuxWaylandPortalBackend {
    fn os(&self) -> PlatformOs {
        PlatformOs::Linux
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        BackendKind::LinuxWaylandPortal
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        build_portal_capabilities(self.arch, self.session_active, 1)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        if self.session_active {
            Err(PlatformError::capability_not_implemented(
                "Wayland Portal 显示拓扑枚举尚未实现。",
            ))
        } else {
            Err(PlatformError::session_unavailable(
                "当前进程不在活动 Wayland 图形会话中。",
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

pub struct LinuxWaylandCompositorBackend {
    arch: PlatformArch,
    session_active: bool,
    ports: UnimplementedPorts,
}

impl LinuxWaylandCompositorBackend {
    pub fn new(arch: PlatformArch, session_active: bool) -> Self {
        Self {
            arch,
            session_active,
            ports: UnimplementedPorts::new(PlatformOs::Linux),
        }
    }
}

impl DesktopSession for LinuxWaylandCompositorBackend {
    fn os(&self) -> PlatformOs {
        PlatformOs::Linux
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        BackendKind::LinuxWaylandCompositor
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        build_compositor_capabilities(self.arch, self.session_active, 1)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        if self.session_active {
            Err(PlatformError::capability_not_implemented(
                "Wayland 合成器专用显示拓扑枚举尚未实现。",
            ))
        } else {
            Err(PlatformError::session_unavailable(
                "当前进程不在活动 Wayland 图形会话中。",
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
    fn linux_session_selector_respects_wayland_xwayland_x11_and_tty_boundaries() {
        // Pure Wayland
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal { active: true }
        );

        // XWayland (both Wayland socket and DISPLAY present): MUST choose Wayland, never X11!
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                x11_display: Some(":0".to_owned()),
                x11_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal { active: true }
        );

        // Compositor backend requested on Wayland
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                compositor_backend_requested: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandCompositor { active: true }
        );

        // Pure X11 with real socket
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("x11".to_owned()),
                x11_display: Some(":0".to_owned()),
                x11_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::X11 { active: true }
        );

        // Stray DISPLAY in TTY/headless session must NOT select X11
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("tty".to_owned()),
                x11_display: Some(":0".to_owned()),
                x11_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::Inactive
        );

        // Missing session type with only DISPLAY must NOT select X11
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                x11_display: Some(":0".to_owned()),
                x11_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::Inactive
        );

        // Declared Wayland without live socket -> inactive Wayland
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                xdg_session_type: Some("wayland".to_owned()),
                wayland_socket_present: false,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal { active: false }
        );
    }

    #[test]
    fn portal_and_compositor_capabilities_are_valid_and_honest() {
        let portal = LinuxWaylandPortalBackend::new(PlatformArch::X64, true);
        let pcaps = portal.capabilities();
        pcaps.validate().unwrap();
        for status in [
            pcaps.features.record_pointer_absolute,
            pcaps.features.record_pointer_relative,
            pcaps.features.record_keyboard,
            pcaps.features.record_all_displays,
        ] {
            assert_eq!(status.state, FeatureState::Unsupported);
            assert_eq!(status.reason, FeatureReason::NoPassiveCapture);
            assert_eq!(status.next_action, NextAction::SwitchSession);
        }
        assert_eq!(
            pcaps.features.control_focus_target.state,
            FeatureState::Unsupported
        );
        assert_eq!(
            pcaps.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        let compositor = LinuxWaylandCompositorBackend::new(PlatformArch::Arm64, true);
        let ccaps = compositor.capabilities();
        ccaps.validate().unwrap();
        assert!(
            ccaps
                .features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::Unimplemented)
        );
    }
}
