use std::sync::Mutex;

use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureSet, FeatureStatus, InputClock, InputEmitter,
    InputRecorder, MAX_SAFE_INT, PlatformArch, PlatformError, PlatformOs, TargetBinding,
    TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

/// Parses a local X11 display number from `DISPLAY` (e.g. `:0`, `:0.0`, `unix:1`, `unix/:2.0`).
/// Remote TCP displays like `host:0` return `None`.
pub fn parse_local_display_number(display: &str) -> Option<u32> {
    let trimmed = display.trim();
    let rest = trimmed
        .strip_prefix(':')
        .or_else(|| trimmed.strip_prefix("unix/:"))
        .or_else(|| trimmed.strip_prefix("unix:"))?;
    let (num_part, screen_part) = match rest.split_once('.') {
        Some((num, screen)) => (num, Some(screen)),
        None => (rest, None),
    };
    if num_part.is_empty() || !num_part.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    if let Some(screen) = screen_part
        && (screen.is_empty() || !screen.bytes().all(|b| b.is_ascii_digit()))
    {
        return None;
    }
    num_part.parse::<u32>().ok()
}

/// Checks whether the local X11 Unix socket `/tmp/.X11-unix/X<n>` exists on Linux.
pub fn x11_socket_exists(display: &str) -> bool {
    #[cfg(target_os = "linux")]
    {
        use std::fs;
        use std::os::unix::fs::FileTypeExt;
        use std::path::PathBuf;

        let Some(num) = parse_local_display_number(display) else {
            return false;
        };
        let path = PathBuf::from(format!("/tmp/.X11-unix/X{num}"));
        fs::symlink_metadata(path)
            .map(|meta| meta.file_type().is_socket())
            .unwrap_or(false)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = display;
        false
    }
}

pub fn build_capabilities(
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
        backend: BackendKind::LinuxX11,
        backend_version: BACKEND_VERSION.to_owned(),
        generation,
        topology_version: None,
        topology_coverage: TopologyCoverage::Unknown,
        trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
        features: FeatureSet::all(status),
    }
}

struct TrackedX11State {
    last_active: Option<bool>,
    generation: u64,
    invalidated: bool,
}

type X11ProbeFn = Box<dyn Fn() -> bool + Send + Sync>;

pub struct LinuxX11Backend {
    arch: PlatformArch,
    probe: X11ProbeFn,
    state: Mutex<TrackedX11State>,
    ports: UnimplementedPorts,
}

impl LinuxX11Backend {
    pub fn new(arch: PlatformArch, session_active: bool) -> Self {
        Self::with_probe(arch, move || session_active)
    }

    pub fn with_probe(
        arch: PlatformArch,
        probe: impl Fn() -> bool + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedX11State {
                last_active: None,
                generation: 1,
                invalidated: false,
            }),
            ports: UnimplementedPorts::new(PlatformOs::Linux),
        }
    }

    pub fn invalidate(&self) {
        let mut state = self.state.lock().unwrap();
        state.invalidated = true;
    }

    fn refresh(&self) -> (bool, u64) {
        let active = (self.probe)();
        let mut state = self.state.lock().unwrap();
        match state.last_active {
            None => {
                state.last_active = Some(active);
                state.invalidated = false;
            }
            Some(prev) if prev != active || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_active = Some(active);
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (active, state.generation)
    }
}

impl DesktopSession for LinuxX11Backend {
    fn os(&self) -> PlatformOs {
        PlatformOs::Linux
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        BackendKind::LinuxX11
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        let (active, generation) = self.refresh();
        build_capabilities(self.arch, active, generation)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        let (active, _) = self.refresh();
        if active {
            Err(PlatformError::capability_not_implemented(
                "Linux X11 显示拓扑枚举尚未实现。",
            ))
        } else {
            Err(PlatformError::session_unavailable(
                "当前进程不在活动 X11 图形会话中。",
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
    use std::sync::atomic::{AtomicBool, Ordering};

    use super::*;
    use deskweave_platform_api::FeatureState;

    #[test]
    fn display_parser_handles_local_and_rejects_remote_tcp() {
        assert_eq!(parse_local_display_number(":0"), Some(0));
        assert_eq!(parse_local_display_number(":1.0"), Some(1));
        assert_eq!(parse_local_display_number("unix:2"), Some(2));
        assert_eq!(parse_local_display_number("unix/:10.0"), Some(10));
        assert_eq!(parse_local_display_number("127.0.0.1:0"), None);
        assert_eq!(parse_local_display_number("example.com:0.0"), None);
        assert_eq!(parse_local_display_number(":0.garbage"), None);
        assert_eq!(parse_local_display_number(":0."), None);
        assert_eq!(parse_local_display_number(":0.0.1"), None);
        assert_eq!(parse_local_display_number(""), None);
    }

    #[test]
    fn x11_capabilities_are_valid_and_honest() {
        let active = LinuxX11Backend::new(PlatformArch::X64, true);
        let caps = active.capabilities();
        caps.validate().unwrap();
        assert_eq!(caps.generation, 1);
        assert!(
            caps.features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::Unimplemented)
        );
        assert_eq!(
            active.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );

        let inactive = LinuxX11Backend::new(PlatformArch::Arm64, false);
        let icaps = inactive.capabilities();
        icaps.validate().unwrap();
        assert!(
            icaps
                .features
                .entries()
                .iter()
                .all(|(_, s)| s.state == FeatureState::SessionUnavailable)
        );
        assert_eq!(inactive.topology().unwrap_err().code, "SESSION_UNAVAILABLE");
    }

    #[test]
    fn x11_backend_refreshes_and_bumps_generation_on_session_change() {
        let active = Arc::new(AtomicBool::new(true));
        let reader = Arc::clone(&active);
        let backend =
            LinuxX11Backend::with_probe(PlatformArch::X64, move || reader.load(Ordering::Acquire));

        assert_eq!(backend.capabilities().generation, 1);
        assert_eq!(backend.capabilities().generation, 1);

        active.store(false, Ordering::Release);
        let c2 = backend.capabilities();
        c2.validate().unwrap();
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");

        active.store(true, Ordering::Release);
        let c3 = backend.capabilities();
        c3.validate().unwrap();
        assert_eq!(c3.generation, 3);
        assert_eq!(
            c3.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        backend.invalidate();
        assert_eq!(backend.capabilities().generation, 4);
    }
}
