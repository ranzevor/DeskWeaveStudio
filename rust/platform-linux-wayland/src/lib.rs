use std::sync::Mutex;

use deskweave_platform_api::{
    BackendKind, CapabilitySnapshot, CaptureSource, CredentialStore, DesktopSession,
    DisplayTopology, EmergencyControl, FeatureReason, FeatureSet, FeatureStatus, InputClock,
    InputEmitter, InputRecorder, MAX_SAFE_INT, NextAction, PlatformArch, PlatformError, PlatformOs,
    TargetBinding, TopologyCoverage, TrajectoryVersion, UnimplementedPorts,
};

pub const BACKEND_VERSION: &str = "0.1.0";

/// Explicit or automatic selection between `linux-wayland-portal` and `linux-wayland-compositor`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum WaylandBackendMode {
    /// Selects `linux-wayland-compositor` when a verified compositor integration socket is present,
    /// otherwise selects `linux-wayland-portal`.
    #[default]
    Auto,
    /// Forces the Portal backend (`linux-wayland-portal`).
    Portal,
    /// Forces the compositor-specific backend (`linux-wayland-compositor`).
    Compositor,
}

impl WaylandBackendMode {
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "auto" => Some(Self::Auto),
            "portal" => Some(Self::Portal),
            "compositor" => Some(Self::Compositor),
            _ => None,
        }
    }

    pub fn from_env() -> Self {
        std::env::var("DESKWEAVE_WAYLAND_BACKEND")
            .ok()
            .and_then(|value| Self::parse(&value))
            .unwrap_or_default()
    }
}

/// Read-only facts gathered from Linux login session metadata and real display/portal sockets.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LinuxSessionFacts {
    pub logind_session_type: Option<String>,
    pub xdg_session_type: Option<String>,
    pub wayland_display: Option<String>,
    pub wayland_socket_present: bool,
    pub portal_available: bool,
    pub x11_display: Option<String>,
    pub x11_socket_present: bool,
    pub wayland_backend_mode: WaylandBackendMode,
    pub compositor_endpoint_present: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SelectedLinuxBackend {
    WaylandPortal {
        active: bool,
        portal_available: bool,
    },
    WaylandCompositor {
        active: bool,
    },
    X11 {
        active: bool,
    },
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
/// 2. Within Wayland, `linux-wayland-compositor` is chosen when `WaylandBackendMode::Compositor`
///    is requested or when `WaylandBackendMode::Auto` observes a verified compositor integration
///    socket; otherwise `linux-wayland-portal` is selected with its live Portal bus state.
/// 3. Explicit `tty` / `unspecified` sessions without an active Wayland socket are inactive,
///    even if a stale `DISPLAY` environment variable is present.
/// 4. X11 is selected only when session metadata indicates `x11` (and no active Wayland socket).
pub fn select_linux_session(facts: &LinuxSessionFacts) -> SelectedLinuxBackend {
    let session_type = normalized_type(facts.logind_session_type.as_deref())
        .or_else(|| normalized_type(facts.xdg_session_type.as_deref()));

    let wayland_selected = matches!(session_type, Some("wayland")) || facts.wayland_socket_present;
    if wayland_selected {
        let active = facts.wayland_socket_present;
        let use_compositor = match facts.wayland_backend_mode {
            WaylandBackendMode::Compositor => true,
            WaylandBackendMode::Portal => false,
            WaylandBackendMode::Auto => facts.compositor_endpoint_present,
        };
        return if use_compositor {
            SelectedLinuxBackend::WaylandCompositor { active }
        } else {
            SelectedLinuxBackend::WaylandPortal {
                active,
                portal_available: facts.portal_available,
            }
        };
    }

    if matches!(session_type, Some("x11")) {
        return SelectedLinuxBackend::X11 {
            active: facts.x11_socket_present,
        };
    }

    SelectedLinuxBackend::Inactive
}

/// Parses `TYPE=` from a systemd logind session file ONLY when `UID=` matches `expected_uid`.
pub fn parse_systemd_session_type(body: &str, expected_uid: u32) -> Option<String> {
    let mut uid_matched = false;
    let mut session_type = None;
    for line in body.lines() {
        if let Some(uid_raw) = line.strip_prefix("UID=") {
            if uid_raw.trim().parse::<u32>().ok() == Some(expected_uid) {
                uid_matched = true;
            }
        } else if let Some(type_raw) = line.strip_prefix("TYPE=") {
            let trimmed = type_raw.trim();
            if !trimmed.is_empty() {
                session_type = Some(trimmed.to_ascii_lowercase());
            }
        }
    }
    if uid_matched { session_type } else { None }
}

/// Validates a relative `WAYLAND_DISPLAY` socket name (single normal filename, no traversal).
pub fn is_safe_relative_wayland_display(display: &str) -> bool {
    !display.is_empty()
        && display.len() <= 255
        && display != "."
        && display != ".."
        && !display.bytes().any(|b| matches!(b, b'/' | b'\\' | 0))
}

/// Extracts an absolute filesystem Unix socket path from `DBUS_SESSION_BUS_ADDRESS`
/// (e.g. `unix:path=/run/user/1000/bus,guid=...`). Rejects percent-encoded escapes and non-absolute paths.
pub fn parse_dbus_unix_socket_path(address: &str) -> Option<&str> {
    for entry in address.split(';') {
        let Some(rest) = entry.trim().strip_prefix("unix:") else {
            continue;
        };
        for pair in rest.split(',') {
            if let Some(path) = pair.trim().strip_prefix("path=")
                && path.starts_with('/')
                && !path.is_empty()
                && path.len() <= 4096
                && !path.contains('%')
                && !path.bytes().any(|b| b == 0)
            {
                return Some(path);
            }
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn is_private_owned_dir(path: &std::path::Path, expected_uid: u32) -> bool {
    use std::fs;
    use std::os::unix::fs::MetadataExt;

    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return false;
    }
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            meta.file_type().is_dir()
                && !meta.file_type().is_symlink()
                && meta.uid() == expected_uid
                && (meta.mode() & 0o077) == 0
        }
        Err(_) => false,
    }
}

#[cfg(target_os = "linux")]
fn verify_wayland_socket(display: &str, runtime_dir: Option<&str>, expected_uid: u32) -> bool {
    use std::fs;
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    use std::path::{Component, Path, PathBuf};

    if display.is_empty() || display.bytes().any(|b| b == 0) {
        return false;
    }
    let path = if Path::new(display).is_absolute() {
        let candidate = PathBuf::from(display);
        if candidate
            .components()
            .any(|c| matches!(c, Component::ParentDir))
        {
            return false;
        }
        let Some(parent) = candidate.parent() else {
            return false;
        };
        if !is_private_owned_dir(parent, expected_uid) {
            return false;
        }
        candidate
    } else {
        if !is_safe_relative_wayland_display(display) {
            return false;
        }
        let Some(runtime) = runtime_dir else {
            return false;
        };
        let runtime_path = Path::new(runtime);
        if !is_private_owned_dir(runtime_path, expected_uid) {
            return false;
        }
        runtime_path.join(display)
    };
    fs::symlink_metadata(path)
        .map(|meta| {
            meta.file_type().is_socket()
                && !meta.file_type().is_symlink()
                && meta.uid() == expected_uid
        })
        .unwrap_or(false)
}

#[cfg(target_os = "linux")]
pub fn probe_linux_session_facts_with_mode(
    override_mode: Option<WaylandBackendMode>,
) -> LinuxSessionFacts {
    use std::env;
    use std::fs;
    use std::os::unix::fs::MetadataExt;

    let read_env = |key: &str| {
        env::var(key)
            .ok()
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty())
    };

    let current_euid = fs::metadata("/proc/self").ok().map(|meta| meta.uid());

    // Prefer kernel-authoritative `/proc/self/sessionid` over environment `XDG_SESSION_ID`.
    let session_id = fs::read_to_string("/proc/self/sessionid")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty() && s != "0" && s != "4294967295")
        .or_else(|| read_env("XDG_SESSION_ID"));

    let logind_session_type = current_euid.and_then(|euid| {
        session_id
            .as_deref()
            .filter(|id| {
                !id.is_empty()
                    && id.len() <= 64
                    && id
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
            })
            .and_then(|id| fs::read_to_string(format!("/run/systemd/sessions/{id}")).ok())
            .and_then(|body| parse_systemd_session_type(&body, euid))
    });

    let xdg_session_type = read_env("XDG_SESSION_TYPE").map(|v| v.to_ascii_lowercase());
    let wayland_display = read_env("WAYLAND_DISPLAY");
    let xdg_runtime_dir = read_env("XDG_RUNTIME_DIR");
    let wayland_socket_present = current_euid.is_some_and(|euid| {
        wayland_display
            .as_deref()
            .is_some_and(|disp| verify_wayland_socket(disp, xdg_runtime_dir.as_deref(), euid))
    });

    let dbus_address = read_env("DBUS_SESSION_BUS_ADDRESS");
    let portal_available = current_euid.is_some_and(|euid| {
        let dbus_path_ok = dbus_address
            .as_deref()
            .and_then(parse_dbus_unix_socket_path)
            .is_some_and(|path| verify_wayland_socket(path, None, euid));
        let runtime_bus_ok = verify_wayland_socket("bus", xdg_runtime_dir.as_deref(), euid);
        dbus_path_ok || runtime_bus_ok
    });

    let compositor_socket = read_env("DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET");
    let compositor_endpoint_present = current_euid.is_some_and(|euid| {
        compositor_socket
            .as_deref()
            .is_some_and(|sock| verify_wayland_socket(sock, xdg_runtime_dir.as_deref(), euid))
    });

    let x11_display = read_env("DISPLAY");
    let x11_socket_present = x11_display
        .as_deref()
        .is_some_and(deskweave_platform_linux_x11::x11_socket_exists);

    let wayland_backend_mode = override_mode.unwrap_or_else(WaylandBackendMode::from_env);

    LinuxSessionFacts {
        logind_session_type,
        xdg_session_type,
        wayland_display,
        wayland_socket_present,
        portal_available,
        x11_display,
        x11_socket_present,
        wayland_backend_mode,
        compositor_endpoint_present,
    }
}

#[cfg(not(target_os = "linux"))]
pub fn probe_linux_session_facts_with_mode(
    override_mode: Option<WaylandBackendMode>,
) -> LinuxSessionFacts {
    LinuxSessionFacts {
        wayland_backend_mode: override_mode.unwrap_or_else(WaylandBackendMode::from_env),
        ..LinuxSessionFacts::default()
    }
}

pub fn probe_linux_session_facts() -> LinuxSessionFacts {
    probe_linux_session_facts_with_mode(None)
}

pub fn build_portal_capabilities(
    arch: PlatformArch,
    session_active: bool,
    portal_available: bool,
    generation: u64,
) -> CapabilitySnapshot {
    let features = if !session_active {
        FeatureSet::all(FeatureStatus::session_unavailable())
    } else {
        let no_passive =
            FeatureStatus::unsupported(FeatureReason::NoPassiveCapture, NextAction::SwitchSession);
        let portal_feature = if portal_available {
            FeatureStatus::unimplemented()
        } else {
            FeatureStatus::session_unavailable()
        };
        FeatureSet {
            capture_display: portal_feature,
            capture_window: portal_feature,
            record_pointer_absolute: no_passive,
            record_pointer_relative: no_passive,
            record_keyboard: no_passive,
            record_all_displays: no_passive,
            input_pointer_absolute: portal_feature,
            input_pointer_relative: portal_feature,
            input_keyboard: portal_feature,
            input_unicode_text: portal_feature,
            control_global_shortcut: portal_feature,
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

struct TrackedPortalState {
    last_state: Option<(bool, bool)>,
    generation: u64,
    invalidated: bool,
}

type PortalProbeFn = Box<dyn Fn() -> (bool, bool) + Send + Sync>;

pub struct LinuxWaylandPortalBackend {
    arch: PlatformArch,
    probe: PortalProbeFn,
    state: Mutex<TrackedPortalState>,
    ports: UnimplementedPorts,
}

impl LinuxWaylandPortalBackend {
    pub fn new(arch: PlatformArch, session_active: bool, portal_available: bool) -> Self {
        Self::with_probe(arch, move || (session_active, portal_available))
    }

    pub fn with_probe(
        arch: PlatformArch,
        probe: impl Fn() -> (bool, bool) + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedPortalState {
                last_state: None,
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

    fn refresh(&self) -> (bool, bool, u64) {
        let current = (self.probe)();
        let mut state = self.state.lock().unwrap();
        match state.last_state {
            None => {
                state.last_state = Some(current);
                state.invalidated = false;
            }
            Some(prev) if prev != current || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_state = Some(current);
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (current.0, current.1, state.generation)
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
        let (session_active, portal_available, generation) = self.refresh();
        build_portal_capabilities(self.arch, session_active, portal_available, generation)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        let (session_active, portal_available, _) = self.refresh();
        if !session_active {
            Err(PlatformError::session_unavailable(
                "当前进程不在活动 Wayland 图形会话中。",
            ))
        } else if !portal_available {
            Err(PlatformError::session_unavailable(
                "Wayland Portal 会话总线当前不可用。",
            ))
        } else {
            Err(PlatformError::capability_not_implemented(
                "Wayland Portal 显示拓扑枚举尚未实现。",
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

struct TrackedCompositorState {
    last_active: Option<bool>,
    generation: u64,
    invalidated: bool,
}

type CompositorProbeFn = Box<dyn Fn() -> bool + Send + Sync>;

pub struct LinuxWaylandCompositorBackend {
    arch: PlatformArch,
    probe: CompositorProbeFn,
    state: Mutex<TrackedCompositorState>,
    ports: UnimplementedPorts,
}

impl LinuxWaylandCompositorBackend {
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
            state: Mutex::new(TrackedCompositorState {
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
        let (session_active, generation) = self.refresh();
        build_compositor_capabilities(self.arch, session_active, generation)
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        let (session_active, _) = self.refresh();
        if session_active {
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

struct TrackedLinuxSessionState {
    last_selection: Option<SelectedLinuxBackend>,
    generation: u64,
    invalidated: bool,
}

type LinuxFactsProbeFn = Box<dyn Fn() -> LinuxSessionFacts + Send + Sync>;

/// Dynamic Linux `DesktopSession` that re-probes session/socket/portal/compositor facts on every
/// query and increments capability `generation` whenever backend selection or availability changes.
pub struct LinuxDesktopSession {
    arch: PlatformArch,
    probe: LinuxFactsProbeFn,
    state: Mutex<TrackedLinuxSessionState>,
    ports: UnimplementedPorts,
}

impl LinuxDesktopSession {
    pub fn detect(override_mode: Option<WaylandBackendMode>) -> Self {
        Self::with_probe(PlatformArch::current(), move || {
            probe_linux_session_facts_with_mode(override_mode)
        })
    }

    pub fn with_probe(
        arch: PlatformArch,
        probe: impl Fn() -> LinuxSessionFacts + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedLinuxSessionState {
                last_selection: None,
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

    fn refresh(&self) -> (SelectedLinuxBackend, u64) {
        let facts = (self.probe)();
        let selection = select_linux_session(&facts);
        let mut state = self.state.lock().unwrap();
        match state.last_selection {
            None => {
                state.last_selection = Some(selection);
                state.invalidated = false;
            }
            Some(prev) if prev != selection || state.invalidated => {
                state.generation = state.generation.saturating_add(1).min(MAX_SAFE_INT);
                state.last_selection = Some(selection);
                state.invalidated = false;
            }
            Some(_) => {}
        }
        (selection, state.generation)
    }
}

impl DesktopSession for LinuxDesktopSession {
    fn os(&self) -> PlatformOs {
        PlatformOs::Linux
    }

    fn arch(&self) -> PlatformArch {
        self.arch
    }

    fn backend(&self) -> BackendKind {
        match self.refresh().0 {
            SelectedLinuxBackend::WaylandPortal { .. } | SelectedLinuxBackend::Inactive => {
                BackendKind::LinuxWaylandPortal
            }
            SelectedLinuxBackend::WaylandCompositor { .. } => BackendKind::LinuxWaylandCompositor,
            SelectedLinuxBackend::X11 { .. } => BackendKind::LinuxX11,
        }
    }

    fn backend_version(&self) -> &str {
        BACKEND_VERSION
    }

    fn capabilities(&self) -> CapabilitySnapshot {
        let (selection, generation) = self.refresh();
        match selection {
            SelectedLinuxBackend::WaylandPortal {
                active,
                portal_available,
            } => build_portal_capabilities(self.arch, active, portal_available, generation),
            SelectedLinuxBackend::WaylandCompositor { active } => {
                build_compositor_capabilities(self.arch, active, generation)
            }
            SelectedLinuxBackend::X11 { active } => {
                deskweave_platform_linux_x11::build_capabilities(self.arch, active, generation)
            }
            SelectedLinuxBackend::Inactive => {
                build_portal_capabilities(self.arch, false, false, generation)
            }
        }
    }

    fn topology(&self) -> Result<DisplayTopology, PlatformError> {
        match self.refresh().0 {
            SelectedLinuxBackend::WaylandPortal {
                active: true,
                portal_available: true,
            } => Err(PlatformError::capability_not_implemented(
                "Wayland Portal 显示拓扑枚举尚未实现。",
            )),
            SelectedLinuxBackend::WaylandPortal {
                active: true,
                portal_available: false,
            } => Err(PlatformError::session_unavailable(
                "Wayland Portal 会话总线当前不可用。",
            )),
            SelectedLinuxBackend::WaylandPortal { active: false, .. }
            | SelectedLinuxBackend::Inactive => Err(PlatformError::session_unavailable(
                "当前进程不在活动 Linux 图形会话中。",
            )),
            SelectedLinuxBackend::WaylandCompositor { active: true } => {
                Err(PlatformError::capability_not_implemented(
                    "Wayland 合成器专用显示拓扑枚举尚未实现。",
                ))
            }
            SelectedLinuxBackend::WaylandCompositor { active: false } => Err(
                PlatformError::session_unavailable("当前进程不在活动 Wayland 图形会话中。"),
            ),
            SelectedLinuxBackend::X11 { active: true } => Err(
                PlatformError::capability_not_implemented("Linux X11 显示拓扑枚举尚未实现。"),
            ),
            SelectedLinuxBackend::X11 { active: false } => Err(PlatformError::session_unavailable(
                "当前进程不在活动 X11 图形会话中。",
            )),
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
    fn linux_session_selector_respects_wayland_xwayland_x11_compositor_and_tty_boundaries() {
        // Pure Wayland (auto without compositor integration -> Portal)
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                portal_available: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal {
                active: true,
                portal_available: true,
            }
        );

        // XWayland (both Wayland socket and DISPLAY present): MUST choose Wayland, never X11!
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                portal_available: true,
                x11_display: Some(":0".to_owned()),
                x11_socket_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal {
                active: true,
                portal_available: true,
            }
        );

        // Explicit Compositor backend selection on Wayland
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                wayland_backend_mode: WaylandBackendMode::Compositor,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandCompositor { active: true }
        );

        // Auto mode selects Compositor when verified compositor integration socket is present
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                wayland_backend_mode: WaylandBackendMode::Auto,
                compositor_endpoint_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandCompositor { active: true }
        );

        // Explicit Portal mode overrides compositor_endpoint_present
        assert_eq!(
            select_linux_session(&LinuxSessionFacts {
                logind_session_type: Some("wayland".to_owned()),
                wayland_display: Some("wayland-0".to_owned()),
                wayland_socket_present: true,
                portal_available: true,
                wayland_backend_mode: WaylandBackendMode::Portal,
                compositor_endpoint_present: true,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal {
                active: true,
                portal_available: true,
            }
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
                portal_available: false,
                ..Default::default()
            }),
            SelectedLinuxBackend::WaylandPortal {
                active: false,
                portal_available: false,
            }
        );
    }

    #[test]
    fn portal_and_compositor_capabilities_are_valid_and_honest() {
        let portal = LinuxWaylandPortalBackend::new(PlatformArch::X64, true, true);
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

        // Portal bus closed while Wayland active -> Portal features degrade to SessionUnavailable.
        let closed_portal = LinuxWaylandPortalBackend::new(PlatformArch::X64, true, false);
        let closed_caps = closed_portal.capabilities();
        closed_caps.validate().unwrap();
        assert_eq!(
            closed_caps.features.capture_display.state,
            FeatureState::SessionUnavailable
        );
        assert_eq!(
            closed_caps.features.record_all_displays.state,
            FeatureState::Unsupported
        );
        assert_eq!(
            closed_portal.topology().unwrap_err().code,
            "SESSION_UNAVAILABLE"
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

    #[test]
    fn linux_desktop_session_refreshes_on_portal_close_compositor_restart_and_backend_switch() {
        let facts = Arc::new(Mutex::new(LinuxSessionFacts {
            logind_session_type: Some("wayland".to_owned()),
            wayland_display: Some("wayland-0".to_owned()),
            wayland_socket_present: true,
            portal_available: true,
            wayland_backend_mode: WaylandBackendMode::Auto,
            ..Default::default()
        }));
        let reader = Arc::clone(&facts);
        let session = LinuxDesktopSession::with_probe(PlatformArch::X64, move || {
            reader.lock().unwrap().clone()
        });

        // 1. Active Wayland Portal -> generation 1.
        let c1 = session.capabilities();
        c1.validate().unwrap();
        assert_eq!(c1.backend, BackendKind::LinuxWaylandPortal);
        assert_eq!(c1.generation, 1);
        assert_eq!(
            c1.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        assert_eq!(
            c1.features.record_all_displays.state,
            FeatureState::Unsupported
        );
        assert_eq!(session.capabilities().generation, 1);

        // 2. Portal bus closes -> Portal capabilities degrade and generation bumps to 2.
        facts.lock().unwrap().portal_available = false;
        let c2 = session.capabilities();
        c2.validate().unwrap();
        assert_eq!(c2.backend, BackendKind::LinuxWaylandPortal);
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        assert_eq!(session.topology().unwrap_err().code, "SESSION_UNAVAILABLE");

        // 3. Switch to Wayland Compositor backend -> backend identity changes, record.* is Unimplemented, generation 3.
        {
            let mut f = facts.lock().unwrap();
            f.wayland_backend_mode = WaylandBackendMode::Compositor;
        }
        let c3 = session.capabilities();
        c3.validate().unwrap();
        assert_eq!(c3.backend, BackendKind::LinuxWaylandCompositor);
        assert_eq!(c3.generation, 3);
        assert_eq!(
            c3.features.record_all_displays.state,
            FeatureState::Unimplemented
        );
        assert_eq!(
            session.topology().unwrap_err().code,
            "CAPABILITY_NOT_IMPLEMENTED"
        );

        // 4. Wayland compositor crashes/restarts (socket gone) -> SessionUnavailable, generation 4.
        facts.lock().unwrap().wayland_socket_present = false;
        let c4 = session.capabilities();
        c4.validate().unwrap();
        assert_eq!(c4.backend, BackendKind::LinuxWaylandCompositor);
        assert_eq!(c4.generation, 4);
        assert_eq!(
            c4.features.record_all_displays.state,
            FeatureState::SessionUnavailable
        );

        // 5. Explicit invalidation bumps generation to 5.
        session.invalidate();
        assert_eq!(session.capabilities().generation, 5);
    }

    #[test]
    fn systemd_session_parser_dbus_parser_mode_parser_and_wayland_display_guardrails() {
        assert_eq!(
            parse_systemd_session_type("UID=1000\nTYPE=wayland\n", 1000),
            Some("wayland".to_owned())
        );
        assert_eq!(
            parse_systemd_session_type("UID=1001\nTYPE=wayland\n", 1000),
            None
        );
        assert_eq!(parse_systemd_session_type("TYPE=wayland\n", 1000), None);

        assert_eq!(
            WaylandBackendMode::parse("auto"),
            Some(WaylandBackendMode::Auto)
        );
        assert_eq!(
            WaylandBackendMode::parse("PORTAL"),
            Some(WaylandBackendMode::Portal)
        );
        assert_eq!(
            WaylandBackendMode::parse("compositor"),
            Some(WaylandBackendMode::Compositor)
        );
        assert_eq!(WaylandBackendMode::parse("x11"), None);

        assert_eq!(
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus,guid=deadbeef"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(
            parse_dbus_unix_socket_path("tcp:host=127.0.0.1;unix:path=/run/user/1000/bus"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(parse_dbus_unix_socket_path("unix:abstract=/tmp/dbus"), None);
        assert_eq!(
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus%00x"),
            None
        );
        assert_eq!(parse_dbus_unix_socket_path("unix:path=relative/bus"), None);

        assert!(is_safe_relative_wayland_display("wayland-0"));
        assert!(!is_safe_relative_wayland_display(""));
        assert!(!is_safe_relative_wayland_display("."));
        assert!(!is_safe_relative_wayland_display(".."));
        assert!(!is_safe_relative_wayland_display("../dbus"));
        assert!(!is_safe_relative_wayland_display("sub/wayland-0"));
    }
}
