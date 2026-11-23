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

/// Performs a bounded non-blocking `AF_UNIX` `SOCK_STREAM` liveness check on Linux.
///
/// Returns `true` only when a live listening peer exists on `path` (`connect` succeeds immediately,
/// returns `EAGAIN` due to a full listen backlog on an active `TCP_LISTEN` socket, or finishes
/// `EINPROGRESS` with `SO_ERROR == 0` within the bounded 50ms timeout). Stale socket files left
/// behind by crashed/closed servers return `ECONNREFUSED` -> `false`.
#[cfg(target_os = "linux")]
pub fn probe_unix_stream_listening(path: &std::path::Path) -> bool {
    use std::ffi::{c_int, c_short, c_ulong, c_ushort, c_void};
    use std::io::{Error, ErrorKind};
    use std::os::unix::ffi::OsStrExt;
    use std::time::{Duration, Instant};

    const AF_UNIX: c_int = 1;
    const SOCK_STREAM: c_int = 1;
    const SOCK_NONBLOCK: c_int = 0o0004000;
    const SOCK_CLOEXEC: c_int = 0o2000000;
    const SOL_SOCKET: c_int = 1;
    const SO_ERROR: c_int = 4;
    const POLLOUT: c_short = 0x0004;
    const EINPROGRESS: i32 = 115;
    const PROBE_TIMEOUT: Duration = Duration::from_millis(50);

    #[repr(C)]
    struct SockAddrUn {
        sun_family: c_ushort,
        sun_path: [u8; 108],
    }

    #[repr(C)]
    struct PollFd {
        fd: c_int,
        events: c_short,
        revents: c_short,
    }

    unsafe extern "C" {
        fn socket(domain: c_int, ty: c_int, protocol: c_int) -> c_int;
        fn connect(sockfd: c_int, addr: *const c_void, addrlen: u32) -> c_int;
        fn poll(fds: *mut PollFd, nfds: c_ulong, timeout: c_int) -> c_int;
        fn getsockopt(
            sockfd: c_int,
            level: c_int,
            optname: c_int,
            optval: *mut c_void,
            optlen: *mut u32,
        ) -> c_int;
        fn close(fd: c_int) -> c_int;
    }

    struct FdGuard(c_int);
    impl Drop for FdGuard {
        fn drop(&mut self) {
            // SAFETY: `self.0` is an owned socket file descriptor returned by `socket()`.
            unsafe {
                let _ = close(self.0);
            }
        }
    }

    let bytes = path.as_os_str().as_bytes();
    if bytes.is_empty() || bytes.len() >= 108 || bytes.contains(&0) {
        return false;
    }

    // SAFETY: Creating a non-blocking close-on-exec AF_UNIX stream socket.
    let fd = unsafe { socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return false;
    }
    let _guard = FdGuard(fd);

    let mut addr = SockAddrUn {
        sun_family: AF_UNIX as c_ushort,
        sun_path: [0u8; 108],
    };
    addr.sun_path[..bytes.len()].copy_from_slice(bytes);
    let addr_len = (std::mem::offset_of!(SockAddrUn, sun_path) + bytes.len() + 1) as u32;
    let deadline = Instant::now() + PROBE_TIMEOUT;

    loop {
        // SAFETY: `fd` is a valid socket descriptor and `addr` is valid for `addr_len` bytes.
        let rc = unsafe { connect(fd, (&raw const addr).cast::<c_void>(), addr_len) };
        if rc == 0 {
            return true;
        }
        let err = Error::last_os_error();
        match err.kind() {
            // On Linux AF_UNIX SOCK_STREAM, non-blocking connect returns EAGAIN only when a live
            // listening socket exists on that inode and its accept backlog is currently full.
            ErrorKind::WouldBlock => return true,
            ErrorKind::Interrupted => {
                if Instant::now() >= deadline {
                    return false;
                }
                continue;
            }
            _ if err.raw_os_error() == Some(EINPROGRESS) => loop {
                let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                    return false;
                };
                let wait_ms = remaining.as_millis().clamp(1, 50) as c_int;
                let mut pfd = PollFd {
                    fd,
                    events: POLLOUT,
                    revents: 0,
                };
                // SAFETY: `pfd` points to 1 valid `PollFd` entry.
                let ready = unsafe { poll(&raw mut pfd, 1, wait_ms) };
                if ready < 0 {
                    if Error::last_os_error().kind() == ErrorKind::Interrupted {
                        continue;
                    }
                    return false;
                }
                if ready == 0 {
                    return false;
                }
                let mut so_error: c_int = 0;
                let mut opt_len = std::mem::size_of::<c_int>() as u32;
                // SAFETY: `so_error` and `opt_len` are valid writable pointers.
                let opt_rc = unsafe {
                    getsockopt(
                        fd,
                        SOL_SOCKET,
                        SO_ERROR,
                        (&raw mut so_error).cast::<c_void>(),
                        &raw mut opt_len,
                    )
                };
                return opt_rc == 0 && so_error == 0;
            },
            _ => return false,
        }
    }
}

#[cfg(not(target_os = "linux"))]
pub fn probe_unix_stream_listening(path: &std::path::Path) -> bool {
    let _ = path;
    false
}

/// Probes a specific local X11 Unix socket path on Linux, verifying it is a non-symlink Unix
/// socket with an active listening peer and returning its stable filesystem identity.
pub fn probe_x11_socket_path(path: &std::path::Path) -> Option<X11SocketIdentity> {
    #[cfg(target_os = "linux")]
    {
        use std::fs;
        use std::os::unix::fs::{FileTypeExt, MetadataExt};

        let read_identity = || {
            fs::symlink_metadata(path)
                .ok()
                .filter(|meta| meta.file_type().is_socket() && !meta.file_type().is_symlink())
                .map(|meta| X11SocketIdentity {
                    dev: meta.dev(),
                    ino: meta.ino(),
                    ctime: meta.ctime(),
                    ctime_nsec: meta.ctime_nsec(),
                })
        };
        for _ in 0..2 {
            let before = read_identity()?;
            if !probe_unix_stream_listening(path) {
                return None;
            }
            let after = read_identity()?;
            if before == after {
                return Some(after);
            }
        }
        None
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        None
    }
}

/// Probes the local X11 Unix socket `/tmp/.X11-unix/X<n>` on Linux and returns its stable
/// filesystem identity when present as a non-symlink Unix socket with an active listener.
pub fn x11_socket_identity(display: &str) -> Option<X11SocketIdentity> {
    let num = parse_local_display_number(display)?;
    let path = std::path::PathBuf::from(format!("/tmp/.X11-unix/X{num}"));
    probe_x11_socket_path(&path)
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

    #[cfg(target_os = "linux")]
    #[test]
    fn x11_live_socket_probe_detects_closed_listener_with_retained_file_and_replacement() {
        use std::fs;
        use std::os::unix::fs::{FileTypeExt, symlink};
        use std::os::unix::net::UnixListener;
        use std::path::PathBuf;
        use std::time::{SystemTime, UNIX_EPOCH};

        struct TempDirGuard(PathBuf);
        impl Drop for TempDirGuard {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("dw-x11-{}-{stamp}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let _cleanup = TempDirGuard(dir.clone());

        let sock_path = dir.join("X99");
        let staged_path = dir.join("X99.new");
        let link_path = dir.join("X99.link");

        let listener1 = UnixListener::bind(&sock_path).unwrap();
        symlink(&sock_path, &link_path).unwrap();
        // Symlinks to live sockets must still be rejected.
        assert!(probe_x11_socket_path(&link_path).is_none());

        let probe_path = sock_path.clone();
        let backend = LinuxX11Backend::with_endpoint_probe(PlatformArch::X64, move || {
            probe_x11_socket_path(&probe_path)
        });

        // Probe repeatedly beyond UnixListener's default listen(128) backlog without calling
        // accept() to verify non-blocking EAGAIN handling never deadlocks or drops liveness.
        for _ in 0..140 {
            let caps = backend.capabilities();
            assert_eq!(caps.generation, 1);
            assert_eq!(
                caps.features.input_keyboard.state,
                FeatureState::Unimplemented
            );
        }

        // Atomic same-path replacement bumps generation without intermediate absence.
        let listener2 = UnixListener::bind(&staged_path).unwrap();
        fs::rename(&staged_path, &sock_path).unwrap();
        drop(listener1);
        let replaced = backend.capabilities();
        assert_eq!(replaced.generation, 2);
        assert_eq!(
            replaced.features.input_keyboard.state,
            FeatureState::Unimplemented
        );

        // Close listener WITHOUT unlinking the socket file node -> must detect dead listener,
        // degrade capabilities to SessionUnavailable, and bump generation to 3.
        drop(listener2);
        assert!(sock_path.exists());
        assert!(
            fs::symlink_metadata(&sock_path)
                .unwrap()
                .file_type()
                .is_socket()
        );
        let stale = backend.capabilities();
        assert_eq!(stale.generation, 3);
        assert_eq!(
            stale.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        assert_eq!(backend.topology().unwrap_err().code, "SESSION_UNAVAILABLE");
    }
}
