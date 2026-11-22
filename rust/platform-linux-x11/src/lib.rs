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

/// Stable kernel identity for a verified local X11 Unix socket endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct X11SocketIdentity {
    pub dev: u64,
    pub ino: u64,
    pub ctime: i64,
    pub ctime_nsec: i64,
}

/// Probes the local X11 Unix socket `/tmp/.X11-unix/X<n>` on Linux and returns its stable
/// filesystem identity when present as a non-symlink Unix socket.
pub fn x11_socket_identity(display: &str) -> Option<X11SocketIdentity> {
    #[cfg(target_os = "linux")]
    {
        use std::fs;
        use std::os::unix::fs::{FileTypeExt, MetadataExt};
        use std::path::PathBuf;

        let num = parse_local_display_number(display)?;
        let path = PathBuf::from(format!("/tmp/.X11-unix/X{num}"));
        fs::symlink_metadata(path)
            .ok()
            .filter(|meta| meta.file_type().is_socket() && !meta.file_type().is_symlink())
            .map(|meta| X11SocketIdentity {
                dev: meta.dev(),
                ino: meta.ino(),
                ctime: meta.ctime(),
                ctime_nsec: meta.ctime_nsec(),
            })
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = display;
        None
    }
}

/// Checks whether the local X11 Unix socket `/tmp/.X11-unix/X<n>` exists on Linux.
pub fn x11_socket_exists(display: &str) -> bool {
    x11_socket_identity(display).is_some()
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ObservedX11State {
    active: bool,
    endpoint: Option<X11SocketIdentity>,
}

struct TrackedX11State {
    last_state: Option<ObservedX11State>,
    generation: u64,
    invalidated: bool,
}

type X11ProbeFn = Box<dyn Fn() -> ObservedX11State + Send + Sync>;

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
        Self::from_observed_probe(arch, move || ObservedX11State {
            active: probe(),
            endpoint: None,
        })
    }

    pub fn with_endpoint_probe(
        arch: PlatformArch,
        probe: impl Fn() -> Option<X11SocketIdentity> + Send + Sync + 'static,
    ) -> Self {
        Self::from_observed_probe(arch, move || {
            let endpoint = probe();
            ObservedX11State {
                active: endpoint.is_some(),
                endpoint,
            }
        })
    }

    fn from_observed_probe(
        arch: PlatformArch,
        probe: impl Fn() -> ObservedX11State + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedX11State {
                last_state: None,
                generation: 1,
                invalidated: false,
            }),
            ports: UnimplementedPorts::new(PlatformOs::Linux),
        }
    }

    pub fn invalidate(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.invalidated = true;
    }

    fn refresh(&self) -> (bool, u64) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let observed = (self.probe)();
        match state.last_state {
            None => {
                state.last_state = Some(observed);
                state.invalidated = false;
            }
            Some(prev) if prev != observed || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_state = Some(observed);
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (observed.active, state.generation)
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

    #[test]
    fn x11_backend_bumps_generation_on_same_path_endpoint_replacement() {
        use std::sync::{Arc, Mutex};

        let endpoint = Arc::new(Mutex::new(Some(X11SocketIdentity {
            dev: 1,
            ino: 100,
            ctime: 1_700_000_000,
            ctime_nsec: 10,
        })));
        let reader = Arc::clone(&endpoint);
        let backend = LinuxX11Backend::with_endpoint_probe(PlatformArch::X64, move || {
            *reader.lock().unwrap()
        });

        assert_eq!(backend.capabilities().generation, 1);
        assert_eq!(backend.capabilities().generation, 1);

        // Atomic replacement on the same display path without ever observing absence.
        *endpoint.lock().unwrap() = Some(X11SocketIdentity {
            dev: 1,
            ino: 101,
            ctime: 1_700_000_001,
            ctime_nsec: 20,
        });
        let replaced = backend.capabilities();
        assert_eq!(replaced.generation, 2);
        assert_eq!(
            replaced.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        assert_eq!(backend.capabilities().generation, 2);
    }
}
