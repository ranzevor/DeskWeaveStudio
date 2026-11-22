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

/// Stable kernel identity for a verified local Unix socket endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SocketEndpointId {
    pub dev: u64,
    pub ino: u64,
    pub ctime: i64,
    pub ctime_nsec: i64,
}

/// Read-only facts gathered from Linux login session metadata and real display/portal sockets.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LinuxSessionFacts {
    pub logind_session_type: Option<String>,
    pub xdg_session_type: Option<String>,
    pub wayland_display: Option<String>,
    pub wayland_socket_present: bool,
    pub wayland_endpoint: Option<SocketEndpointId>,
    pub portal_available: bool,
    pub portal_endpoint: Option<SocketEndpointId>,
    pub x11_display: Option<String>,
    pub x11_socket_present: bool,
    pub x11_endpoint: Option<SocketEndpointId>,
    pub wayland_backend_mode: WaylandBackendMode,
    pub compositor_endpoint_present: bool,
    pub compositor_endpoint: Option<SocketEndpointId>,
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

    let wayland_active = facts.wayland_socket_present || facts.wayland_endpoint.is_some();
    let portal_available = facts.portal_available || facts.portal_endpoint.is_some();
    let compositor_present =
        facts.compositor_endpoint_present || facts.compositor_endpoint.is_some();
    let x11_active = facts.x11_socket_present || facts.x11_endpoint.is_some();

    let wayland_selected = matches!(session_type, Some("wayland")) || wayland_active;
    if wayland_selected {
        let use_compositor = match facts.wayland_backend_mode {
            WaylandBackendMode::Compositor => true,
            WaylandBackendMode::Portal => false,
            WaylandBackendMode::Auto => compositor_present,
        };
        return if use_compositor {
            SelectedLinuxBackend::WaylandCompositor {
                active: wayland_active,
            }
        } else {
            SelectedLinuxBackend::WaylandPortal {
                active: wayland_active,
                portal_available,
            }
        };
    }

    if matches!(session_type, Some("x11")) {
        return SelectedLinuxBackend::X11 { active: x11_active };
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

fn is_valid_dbus_key(key: &str) -> bool {
    !key.is_empty()
        && key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}

fn is_safe_dbus_param_value(item: &str) -> bool {
    !item.is_empty()
        && item
            .bytes()
            .all(|b| (0x21..=0x7E).contains(&b) && b != b'\\')
}

/// Extracts all safe absolute filesystem Unix socket paths from `DBUS_SESSION_BUS_ADDRESS`
/// (e.g. `unix:path=/run/user/1000/bus,guid=...;unix:path=/run/user/1000/bus2`).
///
/// Mirrors the Python launcher sanitizer and fails closed (`Vec::new()`) if any entry is malformed,
/// contains control characters, uses mutually exclusive `unix:` locator keys (`path` combined with
/// `abstract`/`tmpdir`/`dir`/`runtime`), has duplicate `path=` keys, or contains percent escapes,
/// backslashes, or `..` traversal. Accepts a single trailing `;` and parameterless non-unix
/// fallback transports (such as `autolaunch:`).
pub fn parse_dbus_unix_socket_paths(address: &str) -> Vec<&str> {
    if address.is_empty() || address.len() > 4096 || address.chars().any(char::is_control) {
        return Vec::new();
    }
    let body = address.strip_suffix(';').unwrap_or(address);
    if body.is_empty() {
        return Vec::new();
    }
    let mut paths = Vec::new();
    let mut entry_count = 0usize;
    for entry in body.split(';') {
        entry_count += 1;
        if entry.is_empty() || entry_count > 16 {
            return Vec::new();
        }
        let Some((transport, rest)) = entry.split_once(':') else {
            return Vec::new();
        };
        if transport.is_empty()
            || transport.len() > 32
            || !transport
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-'))
        {
            return Vec::new();
        }
        if rest.is_empty() {
            if transport == "unix" {
                return Vec::new();
            }
            continue;
        }
        let mut entry_path: Option<&str> = None;
        let mut seen_other_locator = false;
        for pair in rest.split(',') {
            let Some((key, item)) = pair.split_once('=') else {
                return Vec::new();
            };
            if !is_valid_dbus_key(key) || item.is_empty() {
                return Vec::new();
            }
            if transport == "unix" && key == "path" {
                if entry_path.is_some()
                    || !item.starts_with('/')
                    || item.contains('%')
                    || item.contains('\\')
                    || item.split('/').any(|seg| seg == "..")
                {
                    return Vec::new();
                }
                entry_path = Some(item);
            } else {
                if !is_safe_dbus_param_value(item) {
                    return Vec::new();
                }
                if transport == "unix" && matches!(key, "abstract" | "tmpdir" | "dir" | "runtime") {
                    seen_other_locator = true;
                }
            }
        }
        if transport == "unix" && entry_path.is_some() && seen_other_locator {
            return Vec::new();
        }
        if let Some(path) = entry_path {
            paths.push(path);
        }
    }
    paths
}

/// Extracts the first safe absolute filesystem Unix socket path from `DBUS_SESSION_BUS_ADDRESS`.
pub fn parse_dbus_unix_socket_path(address: &str) -> Option<&str> {
    parse_dbus_unix_socket_paths(address).into_iter().next()
}

/// Resolves the active D-Bus session bus socket endpoint for Wayland Portal.
///
/// Mirrors standard D-Bus client semantics: when `DBUS_SESSION_BUS_ADDRESS` is explicitly set
/// (any non-empty string, including unsupported addresses or sentinel markers), only safe
/// `unix:path=...` entries in that explicit address list are probed in order (never silently
/// falling back to `$XDG_RUNTIME_DIR/bus`); `$XDG_RUNTIME_DIR/bus` is consulted only when
/// `DBUS_SESSION_BUS_ADDRESS` is unset (`None`).
pub fn resolve_portal_bus_endpoint<T>(
    dbus_address: Option<&str>,
    check_explicit_path: impl FnMut(&str) -> Option<T>,
    check_runtime_bus: impl FnOnce() -> Option<T>,
) -> Option<T> {
    match dbus_address {
        Some(addr) => parse_dbus_unix_socket_paths(addr)
            .into_iter()
            .find_map(check_explicit_path),
        None => check_runtime_bus(),
    }
}

/// Resolves whether a D-Bus session bus socket for Wayland Portal is available.
pub fn resolve_portal_bus_available(
    dbus_address: Option<&str>,
    mut check_explicit_path: impl FnMut(&str) -> bool,
    check_runtime_bus: impl FnOnce() -> bool,
) -> bool {
    resolve_portal_bus_endpoint(
        dbus_address,
        |path| check_explicit_path(path).then_some(()),
        || check_runtime_bus().then_some(()),
    )
    .is_some()
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
fn verify_wayland_socket(
    display: &str,
    runtime_dir: Option<&str>,
    expected_uid: u32,
) -> Option<SocketEndpointId> {
    use std::fs;
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    use std::path::{Component, Path, PathBuf};

    if display.is_empty() || display.bytes().any(|b| b == 0) {
        return None;
    }
    let path = if Path::new(display).is_absolute() {
        let candidate = PathBuf::from(display);
        if candidate
            .components()
            .any(|c| matches!(c, Component::ParentDir))
        {
            return None;
        }
        let parent = candidate.parent()?;
        if !is_private_owned_dir(parent, expected_uid) {
            return None;
        }
        candidate
    } else {
        if !is_safe_relative_wayland_display(display) {
            return None;
        }
        let runtime = runtime_dir?;
        let runtime_path = Path::new(runtime);
        if !is_private_owned_dir(runtime_path, expected_uid) {
            return None;
        }
        runtime_path.join(display)
    };
    fs::symlink_metadata(path)
        .ok()
        .filter(|meta| {
            meta.file_type().is_socket()
                && !meta.file_type().is_symlink()
                && meta.uid() == expected_uid
        })
        .map(|meta| SocketEndpointId {
            dev: meta.dev(),
            ino: meta.ino(),
            ctime: meta.ctime(),
            ctime_nsec: meta.ctime_nsec(),
        })
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
    let wayland_endpoint = current_euid.and_then(|euid| {
        wayland_display
            .as_deref()
            .and_then(|disp| verify_wayland_socket(disp, xdg_runtime_dir.as_deref(), euid))
    });

    // Preserve any non-empty DBUS_SESSION_BUS_ADDRESS verbatim (including whitespace-only or
    // sentinel values) so explicit configuration never silently falls back to $XDG_RUNTIME_DIR/bus.
    let dbus_address = env::var("DBUS_SESSION_BUS_ADDRESS")
        .ok()
        .filter(|v| !v.is_empty());
    let portal_endpoint = current_euid.and_then(|euid| {
        resolve_portal_bus_endpoint(
            dbus_address.as_deref(),
            |path| verify_wayland_socket(path, None, euid),
            || verify_wayland_socket("bus", xdg_runtime_dir.as_deref(), euid),
        )
    });

    let compositor_socket = read_env("DESKWEAVE_WAYLAND_COMPOSITOR_SOCKET");
    let compositor_endpoint = current_euid.and_then(|euid| {
        compositor_socket
            .as_deref()
            .and_then(|sock| verify_wayland_socket(sock, xdg_runtime_dir.as_deref(), euid))
    });

    let x11_display = read_env("DISPLAY");
    let x11_endpoint = x11_display
        .as_deref()
        .and_then(deskweave_platform_linux_x11::x11_socket_identity)
        .map(|id| SocketEndpointId {
            dev: id.dev,
            ino: id.ino,
            ctime: id.ctime,
            ctime_nsec: id.ctime_nsec,
        });

    let wayland_backend_mode = override_mode.unwrap_or_else(WaylandBackendMode::from_env);

    LinuxSessionFacts {
        logind_session_type,
        xdg_session_type,
        wayland_display,
        wayland_socket_present: wayland_endpoint.is_some(),
        wayland_endpoint,
        portal_available: portal_endpoint.is_some(),
        portal_endpoint,
        x11_display,
        x11_socket_present: x11_endpoint.is_some(),
        x11_endpoint,
        wayland_backend_mode,
        compositor_endpoint_present: compositor_endpoint.is_some(),
        compositor_endpoint,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ObservedPortalState {
    session_active: bool,
    portal_available: bool,
    wayland_endpoint: Option<SocketEndpointId>,
    portal_endpoint: Option<SocketEndpointId>,
}

struct TrackedPortalState {
    last_state: Option<ObservedPortalState>,
    generation: u64,
    invalidated: bool,
}

type PortalProbeFn = Box<dyn Fn() -> ObservedPortalState + Send + Sync>;

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
        Self::from_observed_probe(arch, move || {
            let (session_active, portal_available) = probe();
            ObservedPortalState {
                session_active,
                portal_available,
                wayland_endpoint: None,
                portal_endpoint: None,
            }
        })
    }

    pub fn with_endpoint_probe(
        arch: PlatformArch,
        probe: impl Fn() -> (Option<SocketEndpointId>, Option<SocketEndpointId>) + Send + Sync + 'static,
    ) -> Self {
        Self::from_observed_probe(arch, move || {
            let (wayland_endpoint, portal_endpoint) = probe();
            ObservedPortalState {
                session_active: wayland_endpoint.is_some(),
                portal_available: portal_endpoint.is_some(),
                wayland_endpoint,
                portal_endpoint,
            }
        })
    }

    fn from_observed_probe(
        arch: PlatformArch,
        probe: impl Fn() -> ObservedPortalState + Send + Sync + 'static,
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
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.invalidated = true;
    }

    fn refresh(&self) -> (bool, bool, u64) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let current = (self.probe)();
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
        (
            current.session_active,
            current.portal_available,
            state.generation,
        )
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ObservedCompositorState {
    active: bool,
    wayland_endpoint: Option<SocketEndpointId>,
    compositor_endpoint: Option<SocketEndpointId>,
}

struct TrackedCompositorState {
    last_state: Option<ObservedCompositorState>,
    generation: u64,
    invalidated: bool,
}

type CompositorProbeFn = Box<dyn Fn() -> ObservedCompositorState + Send + Sync>;

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
        Self::from_observed_probe(arch, move || ObservedCompositorState {
            active: probe(),
            wayland_endpoint: None,
            compositor_endpoint: None,
        })
    }

    pub fn with_endpoint_probe(
        arch: PlatformArch,
        probe: impl Fn() -> (Option<SocketEndpointId>, Option<SocketEndpointId>) + Send + Sync + 'static,
    ) -> Self {
        Self::from_observed_probe(arch, move || {
            let (wayland_endpoint, compositor_endpoint) = probe();
            ObservedCompositorState {
                active: wayland_endpoint.is_some(),
                wayland_endpoint,
                compositor_endpoint,
            }
        })
    }

    fn from_observed_probe(
        arch: PlatformArch,
        probe: impl Fn() -> ObservedCompositorState + Send + Sync + 'static,
    ) -> Self {
        Self {
            arch,
            probe: Box::new(probe),
            state: Mutex::new(TrackedCompositorState {
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
        let current = (self.probe)();
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
        (current.active, state.generation)
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct EffectiveLinuxSessionState {
    selection: SelectedLinuxBackend,
    wayland_endpoint: Option<SocketEndpointId>,
    compositor_endpoint: Option<SocketEndpointId>,
    portal_endpoint: Option<SocketEndpointId>,
    x11_endpoint: Option<SocketEndpointId>,
}

impl EffectiveLinuxSessionState {
    fn from_facts(selection: SelectedLinuxBackend, facts: &LinuxSessionFacts) -> Self {
        match selection {
            SelectedLinuxBackend::WaylandCompositor { .. } => Self {
                selection,
                wayland_endpoint: facts.wayland_endpoint,
                compositor_endpoint: facts.compositor_endpoint,
                portal_endpoint: None,
                x11_endpoint: None,
            },
            SelectedLinuxBackend::WaylandPortal { .. } => Self {
                selection,
                wayland_endpoint: facts.wayland_endpoint,
                compositor_endpoint: None,
                portal_endpoint: facts.portal_endpoint,
                x11_endpoint: None,
            },
            SelectedLinuxBackend::X11 { .. } => Self {
                selection,
                wayland_endpoint: None,
                compositor_endpoint: None,
                portal_endpoint: None,
                x11_endpoint: facts.x11_endpoint,
            },
            SelectedLinuxBackend::Inactive => Self {
                selection,
                wayland_endpoint: None,
                compositor_endpoint: None,
                portal_endpoint: None,
                x11_endpoint: None,
            },
        }
    }
}

struct TrackedLinuxSessionState {
    last_state: Option<EffectiveLinuxSessionState>,
    generation: u64,
    invalidated: bool,
}

type LinuxFactsProbeFn = Box<dyn Fn() -> LinuxSessionFacts + Send + Sync>;

/// Dynamic Linux `DesktopSession` that re-probes session/socket/portal/compositor facts on every
/// query and increments capability `generation` whenever backend selection, availability, or any
/// active socket endpoint identity changes.
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

    fn refresh(&self) -> (SelectedLinuxBackend, u64) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let facts = (self.probe)();
        let selection = select_linux_session(&facts);
        let observed = EffectiveLinuxSessionState::from_facts(selection, &facts);
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
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus;"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus;autolaunch:"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(
            parse_dbus_unix_socket_path("unix:path=/run/user/1000/bus,guid=deadbeef,foo_bar=1"),
            Some("/run/user/1000/bus")
        );
        assert_eq!(
            parse_dbus_unix_socket_path("tcp:host=127.0.0.1;unix:path=/run/user/1000/bus"),
            Some("/run/user/1000/bus")
        );
        for rejected in [
            "unix:abstract=/tmp/dbus",
            "autolaunch:",
            "unsupported:",
            ";",
            ";unix:path=/run/user/1000/bus",
            "unix:path=/run/user/1000/bus;;unix:path=/run/user/1000/b",
            "unix:path=/run/user/1000/a,path=/run/user/1000/b",
            "unix:path=/run/user/1000/bus,abstract=/tmp/foo",
            "unix:path=/run/user/1000/bus,tmpdir=/tmp",
            "unix:path=/run/user/1000/bus,dir=/tmp",
            "unix:path=/run/user/1000/bus,runtime=yes",
            "unix:path=/run/user/1000/bus,garbage",
            "unix:path=/run/user/1000/bus,",
            "unix:path=/run/user/1000/bus\r\n",
            "unix:path=/run/user/1000/bus\n,guid=123",
            "unix:path=/run/user/1000/a\n;unix:path=/run/user/1000/b",
            "unix:path=/run/user/1000/bus,guid=\x07",
            "unix:path=/run/user/1000/good;unix:path=/run/../evil",
            "unix:path=/run/../tmp/bus",
            "unix:path=/run/user/1000/bus%00x",
            "unix:path=relative/bus",
        ] {
            assert_eq!(
                parse_dbus_unix_socket_path(rejected),
                None,
                "expected D-Bus address {rejected:?} to be rejected"
            );
        }
        assert_eq!(
            parse_dbus_unix_socket_paths(
                "unix:abstract=/tmp/a;unix:path=/run/user/1000/first;unix:path=/run/user/1000/second;autolaunch:"
            ),
            vec!["/run/user/1000/first", "/run/user/1000/second"]
        );

        assert!(is_safe_relative_wayland_display("wayland-0"));
        assert!(!is_safe_relative_wayland_display(""));
        assert!(!is_safe_relative_wayland_display("."));
        assert!(!is_safe_relative_wayland_display(".."));
        assert!(!is_safe_relative_wayland_display("../dbus"));
        assert!(!is_safe_relative_wayland_display("sub/wayland-0"));

        // Explicit DBUS_SESSION_BUS_ADDRESS must not silently fall back to $XDG_RUNTIME_DIR/bus.
        assert!(!resolve_portal_bus_available(
            Some("unix:path=/run/user/1000/custom-bus"),
            |_| false,
            || panic!("must not fall back to runtime bus when explicit D-Bus address is set"),
        ));
        assert!(resolve_portal_bus_available(
            Some("unix:path=/run/user/1000/custom-bus"),
            |p| p == "/run/user/1000/custom-bus",
            || panic!("must not consult runtime bus when explicit D-Bus address is set"),
        ));
        assert_eq!(
            resolve_portal_bus_endpoint(
                Some("unix:path=/run/user/1000/dead;unix:path=/run/user/1000/live"),
                |p| (p == "/run/user/1000/live").then_some(SocketEndpointId {
                    dev: 1,
                    ino: 42,
                    ctime: 100,
                    ctime_nsec: 200,
                }),
                || panic!("must not fall back to runtime bus for multi-address D-Bus"),
            ),
            Some(SocketEndpointId {
                dev: 1,
                ino: 42,
                ctime: 100,
                ctime_nsec: 200,
            })
        );
        for unsupported in [
            "tcp:host=127.0.0.1",
            "unix:abstract=/tmp/dbus-abs",
            "unsupported:",
            "   ",
        ] {
            assert!(!resolve_portal_bus_available(
                Some(unsupported),
                |_| true,
                || panic!("explicit unsupported D-Bus address must not fall back to runtime bus"),
            ));
        }
        assert!(resolve_portal_bus_available(None, |_| false, || true));
        assert!(!resolve_portal_bus_available(None, |_| true, || false));
    }

    #[test]
    fn same_path_endpoint_replacement_bumps_generation_without_intermediate_absence() {
        use std::sync::{Arc, Mutex};

        let id = |ino: u64| SocketEndpointId {
            dev: 7,
            ino,
            ctime: 1_700_000_000 + ino as i64,
            ctime_nsec: 50,
        };

        // 1. LinuxDesktopSession: Portal mode replacing Wayland socket and D-Bus socket in place.
        let facts = Arc::new(Mutex::new(LinuxSessionFacts {
            logind_session_type: Some("wayland".to_owned()),
            wayland_display: Some("wayland-0".to_owned()),
            wayland_socket_present: true,
            wayland_endpoint: Some(id(10)),
            portal_available: true,
            portal_endpoint: Some(id(20)),
            wayland_backend_mode: WaylandBackendMode::Portal,
            ..LinuxSessionFacts::default()
        }));
        let reader = Arc::clone(&facts);
        let session = LinuxDesktopSession::with_probe(PlatformArch::X64, move || {
            reader.lock().unwrap().clone()
        });

        let c1 = session.capabilities();
        assert_eq!(c1.backend, BackendKind::LinuxWaylandPortal);
        assert_eq!(c1.generation, 1);
        assert_eq!(
            c1.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        assert_eq!(session.capabilities().generation, 1);

        // Replace Wayland socket in place (active stays true, never observed absent) -> gen 2.
        facts.lock().unwrap().wayland_endpoint = Some(id(11));
        let c2 = session.capabilities();
        assert_eq!(c2.backend, BackendKind::LinuxWaylandPortal);
        assert_eq!(c2.generation, 2);
        assert_eq!(
            c2.features.input_keyboard.state,
            FeatureState::Unimplemented
        );
        assert_eq!(session.capabilities().generation, 2);

        // Replace Portal D-Bus socket in place (portal_available stays true) -> gen 3.
        facts.lock().unwrap().portal_endpoint = Some(id(21));
        let c3 = session.capabilities();
        assert_eq!(c3.backend, BackendKind::LinuxWaylandPortal);
        assert_eq!(c3.generation, 3);
        assert_eq!(
            c3.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        // Switch to Auto + Compositor integration socket -> gen 4.
        {
            let mut f = facts.lock().unwrap();
            f.wayland_backend_mode = WaylandBackendMode::Auto;
            f.compositor_endpoint_present = true;
            f.compositor_endpoint = Some(id(30));
        }
        let c4 = session.capabilities();
        assert_eq!(c4.backend, BackendKind::LinuxWaylandCompositor);
        assert_eq!(c4.generation, 4);

        // Replace Compositor integration socket in place (active stays true) -> gen 5.
        facts.lock().unwrap().compositor_endpoint = Some(id(31));
        let c5 = session.capabilities();
        assert_eq!(c5.backend, BackendKind::LinuxWaylandCompositor);
        assert_eq!(c5.generation, 5);
        assert_eq!(
            c5.features.record_all_displays.state,
            FeatureState::Unimplemented
        );

        // 2. X11 session replacing X11 socket in place -> bumps generation without absence.
        let x11_facts = Arc::new(Mutex::new(LinuxSessionFacts {
            logind_session_type: Some("x11".to_owned()),
            x11_display: Some(":0".to_owned()),
            x11_socket_present: true,
            x11_endpoint: Some(id(40)),
            ..LinuxSessionFacts::default()
        }));
        let x11_reader = Arc::clone(&x11_facts);
        let x11_session = LinuxDesktopSession::with_probe(PlatformArch::X64, move || {
            x11_reader.lock().unwrap().clone()
        });
        assert_eq!(x11_session.capabilities().generation, 1);
        x11_facts.lock().unwrap().x11_endpoint = Some(id(41));
        assert_eq!(x11_session.capabilities().generation, 2);

        // 3. Standalone Portal & Compositor endpoint probes also bump generation on replacement.
        let portal_eps = Arc::new(Mutex::new((Some(id(50)), Some(id(60)))));
        let portal_reader = Arc::clone(&portal_eps);
        let portal = LinuxWaylandPortalBackend::with_endpoint_probe(PlatformArch::X64, move || {
            *portal_reader.lock().unwrap()
        });
        assert_eq!(portal.capabilities().generation, 1);
        portal_eps.lock().unwrap().1 = Some(id(61));
        assert_eq!(portal.capabilities().generation, 2);

        let comp_eps = Arc::new(Mutex::new((Some(id(70)), Some(id(80)))));
        let comp_reader = Arc::clone(&comp_eps);
        let comp =
            LinuxWaylandCompositorBackend::with_endpoint_probe(PlatformArch::X64, move || {
                *comp_reader.lock().unwrap()
            });
        assert_eq!(comp.capabilities().generation, 1);
        comp_eps.lock().unwrap().1 = Some(id(81));
        assert_eq!(comp.capabilities().generation, 2);
    }

    #[test]
    fn linux_desktop_session_serializes_concurrent_probes_and_recovers_from_poison() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        use std::sync::{Arc, Mutex, mpsc};
        use std::thread;

        let facts = Arc::new(Mutex::new(LinuxSessionFacts {
            logind_session_type: Some("wayland".to_owned()),
            wayland_display: Some("wayland-0".to_owned()),
            wayland_socket_present: true,
            portal_available: true,
            wayland_backend_mode: WaylandBackendMode::Portal,
            ..LinuxSessionFacts::default()
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
        let session = Arc::new(LinuxDesktopSession::with_probe(
            PlatformArch::X64,
            move || {
                assert!(
                    !in_probe_p.swap(true, Ordering::SeqCst),
                    "concurrent Linux probe overlapped outside lock"
                );
                struct Guard(Arc<AtomicBool>);
                impl Drop for Guard {
                    fn drop(&mut self) {
                        self.0.store(false, Ordering::SeqCst);
                    }
                }
                let _guard = Guard(Arc::clone(&in_probe_p));

                if panic_p.swap(false, Ordering::SeqCst) {
                    panic!("simulated transient Linux probe failure");
                }

                let call_no = calls_p.fetch_add(1, Ordering::SeqCst);
                let snapshot = facts_p.lock().unwrap().clone();
                if call_no == 0 {
                    entered_tx.send(()).unwrap();
                    release_p.lock().unwrap().recv().unwrap();
                }
                snapshot
            },
        ));

        let s1 = Arc::clone(&session);
        let t1 = thread::spawn(move || s1.capabilities());
        entered_rx.recv().unwrap();

        // Close Portal bus while Thread 1 holds the state lock inside the initial active probe.
        facts.lock().unwrap().portal_available = false;
        let s2 = Arc::clone(&session);
        let t2 = thread::spawn(move || s2.capabilities());

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
        assert_eq!(session.capabilities().generation, 2);

        // Poison recovery must preserve generation continuity and recover cleanly.
        panic_next.store(true, Ordering::SeqCst);
        assert!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.capabilities()))
                .is_err()
        );
        session.invalidate();
        let recovered = session.capabilities();
        assert_eq!(recovered.generation, 3);
        assert_eq!(
            recovered.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
    }
}
