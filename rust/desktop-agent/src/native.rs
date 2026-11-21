use std::fs::File;
use std::io::{self, Read, Write};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

#[cfg(unix)]
use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd, RawFd};
#[cfg(unix)]
use std::os::unix::net::{UnixListener, UnixStream};
#[cfg(windows)]
use std::os::windows::{
    fs::OpenOptionsExt,
    io::{AsHandle, AsRawHandle, FromRawHandle, OwnedHandle, RawHandle},
};

pub(crate) fn owner_file() -> io::Result<File> {
    #[cfg(unix)]
    let handle = io::stdout().as_fd().try_clone_to_owned()?;
    #[cfg(windows)]
    let handle = io::stdout().as_handle().try_clone_to_owned()?;
    let file = File::from(handle);
    if !file.metadata()?.is_file() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    Ok(file)
}

#[cfg(unix)]
pub(crate) fn owner_identity(owner: &File) -> io::Result<[String; 2]> {
    use std::os::unix::fs::MetadataExt;

    let info = owner.metadata()?;
    Ok([info.dev().to_string(), info.ino().to_string()])
}

#[cfg(windows)]
pub(crate) fn owner_identity(owner: &File) -> io::Result<[String; 2]> {
    use std::ffi::c_void;
    use std::os::windows::io::{AsRawHandle, RawHandle};

    #[repr(C)]
    struct FileId {
        volume: u64,
        identity: [u8; 16],
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetFileInformationByHandleEx(
            file: RawHandle,
            class: i32,
            buffer: *mut c_void,
            size: u32,
        ) -> i32;
    }
    let mut info = FileId {
        volume: 0,
        identity: [0; 16],
    };
    if unsafe {
        GetFileInformationByHandleEx(
            owner.as_raw_handle(),
            18,
            (&raw mut info).cast(),
            size_of::<FileId>() as u32,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let identity = info
        .identity
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok([info.volume.to_string(), identity])
}

#[cfg(target_os = "linux")]
pub(crate) fn claim_scope(scope: &str) -> io::Result<std::os::unix::net::UnixDatagram> {
    use std::os::linux::net::SocketAddrExt;
    use std::os::unix::net::{SocketAddr, UnixDatagram};

    let name = SocketAddr::from_abstract_name(format!("deskweave-agent-v1-{scope}"))?;
    UnixDatagram::bind_addr(&name)
}

#[cfg(target_os = "macos")]
pub(crate) struct ScopeClaim {
    port: *mut std::ffi::c_void,
}

#[cfg(target_os = "macos")]
impl Drop for ScopeClaim {
    fn drop(&mut self) {
        unsafe {
            macos_scope::CFMessagePortInvalidate(self.port);
            macos_scope::CFRelease(self.port.cast_const());
        }
    }
}

#[cfg(target_os = "macos")]
mod macos_scope {
    use std::ffi::c_void;
    use std::io;

    pub(super) type CallBack = unsafe extern "C" fn(
        local: *mut c_void,
        msgid: i32,
        data: *const c_void,
        info: *mut c_void,
    ) -> *const c_void;

    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        pub(super) fn CFStringCreateWithBytes(
            alloc: *const c_void,
            bytes: *const u8,
            num_bytes: isize,
            encoding: u32,
            is_external_representation: u8,
        ) -> *const c_void;
        pub(super) fn CFMessagePortCreateLocal(
            allocator: *const c_void,
            name: *const c_void,
            callout: Option<CallBack>,
            context: *const c_void,
            should_free_info: *mut u8,
        ) -> *mut c_void;
        pub(super) fn CFMessagePortCreateRemote(
            allocator: *const c_void,
            name: *const c_void,
        ) -> *mut c_void;
        pub(super) fn CFMessagePortIsValid(port: *mut c_void) -> u8;
        pub(super) fn CFMessagePortInvalidate(port: *mut c_void);
        pub(super) fn CFRelease(cf: *const c_void);
    }

    pub(super) struct CfString {
        pub(super) raw: *const c_void,
    }

    impl CfString {
        pub(super) fn create(value: &str) -> io::Result<Self> {
            const CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
            let len = isize::try_from(value.len())
                .map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
            let raw = unsafe {
                CFStringCreateWithBytes(
                    std::ptr::null(),
                    value.as_ptr(),
                    len,
                    CF_STRING_ENCODING_UTF8,
                    0,
                )
            };
            if raw.is_null() {
                return Err(io::ErrorKind::OutOfMemory.into());
            }
            Ok(Self { raw })
        }
    }

    impl Drop for CfString {
        fn drop(&mut self) {
            unsafe {
                CFRelease(self.raw);
            }
        }
    }

    pub(super) unsafe extern "C" fn ignore_message(
        _local: *mut c_void,
        _msgid: i32,
        _data: *const c_void,
        _info: *mut c_void,
    ) -> *const c_void {
        std::ptr::null()
    }
}

#[cfg(any(target_os = "macos", test))]
mod macos_claim {
    use std::io;
    use std::time::Duration;

    pub(super) const ATTEMPTS: usize = 50;
    pub(super) const RETRY_DELAY: Duration = Duration::from_millis(10);

    pub(super) enum LocalPort<T> {
        Created(T),
        Duplicate(T),
        Unavailable,
    }

    pub(super) fn validate_scope(scope: &str) -> io::Result<String> {
        if scope.is_empty()
            || scope.len() > 100
            || !scope
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
        {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        Ok(format!("deskweave.agent.v1.{scope}"))
    }

    pub(super) fn retry_claim<T>(
        attempts: usize,
        retry_delay: Duration,
        mut create_local: impl FnMut() -> LocalPort<T>,
        mut release_duplicate: impl FnMut(T),
        mut remote_is_live: impl FnMut() -> bool,
        mut pause: impl FnMut(Duration),
    ) -> io::Result<T> {
        for attempt in 0..attempts {
            match create_local() {
                LocalPort::Created(port) => return Ok(port),
                LocalPort::Duplicate(port) => {
                    release_duplicate(port);
                    return Err(io::ErrorKind::AlreadyExists.into());
                }
                LocalPort::Unavailable => {
                    if remote_is_live() {
                        return Err(io::ErrorKind::AlreadyExists.into());
                    }
                    if attempt + 1 < attempts {
                        pause(retry_delay);
                    }
                }
            }
        }
        Err(io::Error::other(
            "CFMessagePortCreateLocal failed without an active owner",
        ))
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn claim_scope(scope: &str) -> io::Result<ScopeClaim> {
    let service = macos_claim::validate_scope(scope)?;
    let name = macos_scope::CfString::create(&service)?;
    let port = macos_claim::retry_claim(
        macos_claim::ATTEMPTS,
        macos_claim::RETRY_DELAY,
        || {
            let mut should_free: u8 = 0;
            let port = unsafe {
                macos_scope::CFMessagePortCreateLocal(
                    std::ptr::null(),
                    name.raw,
                    Some(macos_scope::ignore_message),
                    std::ptr::null(),
                    &mut should_free,
                )
            };
            if port.is_null() {
                macos_claim::LocalPort::Unavailable
            } else if should_free != 0 {
                macos_claim::LocalPort::Duplicate(port)
            } else {
                macos_claim::LocalPort::Created(port)
            }
        },
        |port| unsafe {
            macos_scope::CFRelease(port.cast_const());
        },
        || {
            let remote =
                unsafe { macos_scope::CFMessagePortCreateRemote(std::ptr::null(), name.raw) };
            if remote.is_null() {
                return false;
            }
            let live = unsafe { macos_scope::CFMessagePortIsValid(remote) } != 0;
            unsafe {
                macos_scope::CFRelease(remote.cast_const());
            }
            live
        },
        std::thread::sleep,
    )?;
    Ok(ScopeClaim { port })
}

#[cfg(windows)]
pub(crate) fn claim_scope(scope: &str) -> io::Result<OwnedHandle> {
    use std::ptr;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateMutexW(
            attributes: *const std::ffi::c_void,
            owner: i32,
            name: *const u16,
        ) -> RawHandle;
        fn GetLastError() -> u32;
        fn SetLastError(code: u32);
    }

    let name: Vec<_> = format!("Local\\DeskWeave.Agent.v1.{scope}\0")
        .encode_utf16()
        .collect();
    let (handle, error) = unsafe {
        SetLastError(0);
        let handle = CreateMutexW(ptr::null(), 0, name.as_ptr());
        (handle, GetLastError())
    };
    if handle.is_null() {
        return Err(io::Error::from_raw_os_error(error as i32));
    }
    let claim = unsafe { OwnedHandle::from_raw_handle(handle) };
    if error == 183 {
        return Err(io::ErrorKind::AlreadyExists.into());
    }
    Ok(claim)
}

fn random_hex(byte_len: usize) -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    let slice = bytes
        .get_mut(..byte_len)
        .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
    getrandom::fill(slice).map_err(|_| io::Error::other("getrandom failed"))?;
    let mut encoded = String::with_capacity(byte_len * 2);
    for &byte in slice.iter() {
        use std::fmt::Write as _;
        let _ = write!(&mut encoded, "{byte:02x}");
    }
    Ok(encoded)
}

pub(crate) trait SessionStream: Send {
    fn read_chunk(
        &mut self,
        buf: &mut [u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize>;

    fn write_chunk(
        &mut self,
        buf: &[u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize>;
}

pub(crate) trait SessionListener: Send {
    type Stream: SessionStream + 'static;

    fn accept(&mut self, stop: &StopSignal) -> io::Result<(Self::Stream, u32)>;
}

pub(crate) struct DeadlineIo<'a, S: SessionStream> {
    stream: &'a mut S,
    deadline: Option<Instant>,
    stop: &'a StopSignal,
}

impl<'a, S: SessionStream> DeadlineIo<'a, S> {
    pub(crate) fn new(stream: &'a mut S, deadline: Option<Instant>, stop: &'a StopSignal) -> Self {
        Self {
            stream,
            deadline,
            stop,
        }
    }
}

impl<S: SessionStream> Read for DeadlineIo<'_, S> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.stream.read_chunk(buf, self.deadline, self.stop)
    }
}

impl<S: SessionStream> Write for DeadlineIo<'_, S> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.stream.write_chunk(buf, self.deadline, self.stop)
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[cfg(unix)]
struct StopInner {
    stopped: AtomicBool,
    read: OwnedFd,
    write: OwnedFd,
}

#[cfg(windows)]
struct StopInner {
    stopped: AtomicBool,
    event: OwnedHandle,
}

#[derive(Clone)]
pub(crate) struct StopSignal {
    inner: Arc<StopInner>,
}

impl StopSignal {
    #[cfg(unix)]
    pub(crate) fn new() -> io::Result<Self> {
        let mut fds = [0_i32; 2];
        if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let read = unsafe { OwnedFd::from_raw_fd(fds[0]) };
        let write = unsafe { OwnedFd::from_raw_fd(fds[1]) };
        for fd in [read.as_raw_fd(), write.as_raw_fd()] {
            if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } != 0
                || unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } != 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(Self {
            inner: Arc::new(StopInner {
                stopped: AtomicBool::new(false),
                read,
                write,
            }),
        })
    }

    #[cfg(windows)]
    pub(crate) fn new() -> io::Result<Self> {
        let event = win_ipc::create_event(true)?;
        Ok(Self {
            inner: Arc::new(StopInner {
                stopped: AtomicBool::new(false),
                event,
            }),
        })
    }

    pub(crate) fn trigger(&self) {
        if !self.inner.stopped.swap(true, Ordering::AcqRel) {
            #[cfg(unix)]
            unsafe {
                let byte = 1_u8;
                loop {
                    let rc = libc::write(self.inner.write.as_raw_fd(), (&raw const byte).cast(), 1);
                    if rc >= 0 {
                        break;
                    }
                    if io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
                        break;
                    }
                }
            }
            #[cfg(windows)]
            win_ipc::set_event(&self.inner.event);
        }
    }

    pub(crate) fn is_triggered(&self) -> bool {
        self.inner.stopped.load(Ordering::Acquire)
    }

    #[cfg(unix)]
    fn raw_fd(&self) -> RawFd {
        self.inner.read.as_raw_fd()
    }

    #[cfg(windows)]
    fn raw_handle(&self) -> RawHandle {
        self.inner.event.as_raw_handle()
    }
}

#[cfg(unix)]
fn poll_fd(
    fd: RawFd,
    events: libc::c_short,
    stop: &StopSignal,
    deadline: Option<Instant>,
) -> io::Result<()> {
    loop {
        if stop.is_triggered() {
            return Err(io::ErrorKind::ConnectionAborted.into());
        }
        let timeout_ms = match deadline {
            None => -1,
            Some(limit) => {
                let now = Instant::now();
                if now >= limit {
                    return Err(io::ErrorKind::TimedOut.into());
                }
                let remaining = limit.duration_since(now).as_millis();
                (remaining.min(i32::MAX as u128) as i32).max(1)
            }
        };
        let mut fds = [
            libc::pollfd {
                fd,
                events,
                revents: 0,
            },
            libc::pollfd {
                fd: stop.raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        let rc = unsafe { libc::poll(fds.as_mut_ptr(), 2, timeout_ms) };
        if rc < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if rc == 0 {
            return Err(io::ErrorKind::TimedOut.into());
        }
        if (fds[1].revents & libc::POLLIN) != 0 || stop.is_triggered() {
            return Err(io::ErrorKind::ConnectionAborted.into());
        }
        return Ok(());
    }
}

#[cfg(unix)]
pub(crate) struct NativeStream {
    stream: UnixStream,
}

#[cfg(unix)]
impl NativeStream {
    fn from_unix(stream: UnixStream) -> io::Result<Self> {
        stream.set_nonblocking(true)?;
        Ok(Self { stream })
    }
}

#[cfg(unix)]
impl SessionStream for NativeStream {
    fn read_chunk(
        &mut self,
        buf: &mut [u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        loop {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            match self.stream.read(buf) {
                Ok(count) => return Ok(count),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    poll_fd(self.stream.as_raw_fd(), libc::POLLIN, stop, deadline)?;
                }
                Err(error) => return Err(error),
            }
        }
    }

    fn write_chunk(
        &mut self,
        buf: &[u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        loop {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            match self.stream.write(buf) {
                Ok(count) => return Ok(count),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    poll_fd(self.stream.as_raw_fd(), libc::POLLOUT, stop, deadline)?;
                }
                Err(error) => return Err(error),
            }
        }
    }
}

#[cfg(unix)]
pub(crate) fn connect(address: &str, expected_pid: u32) -> io::Result<NativeStream> {
    if !std::path::Path::new(address).is_absolute() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let channel = UnixStream::connect(address)?;
    let (pid, uid) = peer(&channel)?;
    if pid != expected_pid || uid != unsafe { libc::geteuid() } {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    NativeStream::from_unix(channel)
}

#[cfg(target_os = "linux")]
fn peer(channel: &UnixStream) -> io::Result<(u32, u32)> {
    let mut credential = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut size = size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            channel.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&raw mut credential).cast(),
            &mut size,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if size as usize != size_of::<libc::ucred>() || credential.pid <= 0 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok((credential.pid as u32, credential.uid))
}

#[cfg(target_os = "macos")]
fn peer_token(channel: &UnixStream) -> io::Result<[u32; 8]> {
    const SOL_LOCAL: libc::c_int = 0;
    const LOCAL_PEERTOKEN: libc::c_int = 6;
    let mut token = [0_u32; 8];
    let mut size = size_of_val(&token) as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            channel.as_raw_fd(),
            SOL_LOCAL,
            LOCAL_PEERTOKEN,
            token.as_mut_ptr().cast(),
            &mut size,
        )
    };
    if result != 0 {
        return Err(io::Error::last_os_error());
    }
    if size as usize != size_of_val(&token) || token[5] == 0 {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(token)
}

#[cfg(target_os = "macos")]
fn own_audit_session() -> io::Result<u32> {
    let (first, _second) = UnixStream::pair()?;
    Ok(peer_token(&first)?[6])
}

#[cfg(target_os = "macos")]
fn peer(channel: &UnixStream) -> io::Result<(u32, u32)> {
    let token = peer_token(channel)?;
    if token[6] != own_audit_session()? {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok((token[5], token[1]))
}

#[cfg(unix)]
fn stat_id(st: &libc::stat) -> (u64, u64) {
    #[allow(clippy::unnecessary_cast)]
    {
        (st.st_dev as u64, st.st_ino as u64)
    }
}

#[cfg(target_os = "macos")]
fn unix_socket_base(_uid: u32) -> io::Result<std::path::PathBuf> {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::path::PathBuf;

    const CS_DARWIN_USER_TEMP_DIR: libc::c_int = 65537;
    let mut buffer = [0_u8; 4096];
    let size = unsafe {
        libc::confstr(
            CS_DARWIN_USER_TEMP_DIR,
            buffer.as_mut_ptr().cast(),
            buffer.len(),
        )
    };
    if size == 0 || size > buffer.len() {
        return Err(io::Error::last_os_error());
    }
    let length = buffer[..size]
        .iter()
        .position(|&byte| byte == 0)
        .unwrap_or(size);
    if length == 0 {
        return Err(io::ErrorKind::NotFound.into());
    }
    Ok(PathBuf::from(OsStr::from_bytes(&buffer[..length])))
}

#[cfg(target_os = "linux")]
fn unix_socket_base(uid: u32) -> io::Result<std::path::PathBuf> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::PathBuf;

    let run_user = PathBuf::from(format!("/run/user/{uid}"));
    if let Ok(dir) = File::options()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(&run_user)
        && let Ok(meta) = dir.metadata()
        && meta.is_dir()
        && meta.uid() == uid
        && (meta.mode() & 0o077) == 0
    {
        return Ok(run_user);
    }
    Ok(PathBuf::from("/tmp"))
}

#[cfg(unix)]
pub(crate) struct Listener {
    listener: Option<UnixListener>,
    dir_file: Option<File>,
    dir_path: std::path::PathBuf,
    sock_path: std::path::PathBuf,
    address: String,
    dir_id: (u64, u64),
    sock_id: (u64, u64),
    uid: u32,
}

#[cfg(unix)]
impl Listener {
    pub(crate) fn bind() -> io::Result<Self> {
        use std::fs::{self, DirBuilder, Permissions};
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::{
            DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt,
        };

        #[cfg(target_os = "macos")]
        const MAX_SUN_PATH: usize = 104;
        #[cfg(not(target_os = "macos"))]
        const MAX_SUN_PATH: usize = 108;

        let uid = unsafe { libc::geteuid() };
        let base = unix_socket_base(uid)?;
        let mut created_dir = None;
        for _ in 0..8 {
            let candidate = base.join(format!("dw-ag-{}", random_hex(8)?));
            match DirBuilder::new().mode(0o700).create(&candidate) {
                Ok(()) => {
                    created_dir = Some(candidate);
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        let dir_path = created_dir.ok_or_else(|| io::Error::from(io::ErrorKind::AlreadyExists))?;
        let sock_path = dir_path.join("a.sock");
        let setup = (|| -> io::Result<Self> {
            let dir_file = File::options()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
                .open(&dir_path)?;
            let dir_meta = dir_file.metadata()?;
            let path_meta = fs::symlink_metadata(&dir_path)?;
            if !dir_meta.is_dir()
                || dir_meta.uid() != uid
                || (dir_meta.mode() & 0o077) != 0
                || (dir_meta.dev(), dir_meta.ino()) != (path_meta.dev(), path_meta.ino())
            {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            if sock_path.as_os_str().as_bytes().len() >= MAX_SUN_PATH {
                return Err(io::ErrorKind::InvalidInput.into());
            }
            let address = sock_path
                .to_str()
                .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?
                .to_owned();
            let listener = UnixListener::bind(&sock_path)?;
            fs::set_permissions(&sock_path, Permissions::from_mode(0o600))?;
            let sock_meta = fs::symlink_metadata(&sock_path)?;
            if !sock_meta.file_type().is_socket()
                || sock_meta.uid() != uid
                || (sock_meta.mode() & 0o077) != 0
            {
                return Err(io::ErrorKind::PermissionDenied.into());
            }
            listener.set_nonblocking(true)?;
            Ok(Self {
                listener: Some(listener),
                dir_file: Some(dir_file),
                dir_path: dir_path.clone(),
                sock_path: sock_path.clone(),
                address,
                dir_id: (dir_meta.dev(), dir_meta.ino()),
                sock_id: (sock_meta.dev(), sock_meta.ino()),
                uid,
            })
        })();
        if setup.is_err() {
            let _ = fs::remove_file(&sock_path);
            let _ = fs::remove_dir(&dir_path);
        }
        setup
    }

    pub(crate) fn address(&self) -> &str {
        &self.address
    }

    fn check_paths(&self) -> io::Result<()> {
        use std::fs;
        use std::os::unix::fs::{FileTypeExt, MetadataExt};

        let dir_meta = fs::symlink_metadata(&self.dir_path)?;
        let sock_meta = fs::symlink_metadata(&self.sock_path)?;
        if !dir_meta.is_dir()
            || dir_meta.uid() != self.uid
            || (dir_meta.mode() & 0o077) != 0
            || (dir_meta.dev(), dir_meta.ino()) != self.dir_id
            || !sock_meta.file_type().is_socket()
            || sock_meta.uid() != self.uid
            || (sock_meta.mode() & 0o077) != 0
            || (sock_meta.dev(), sock_meta.ino()) != self.sock_id
        {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        Ok(())
    }
}

#[cfg(unix)]
fn is_transient_accept_error(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::Interrupted
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
    ) || error.raw_os_error() == Some(libc::EPROTO)
}

#[cfg(unix)]
impl SessionListener for Listener {
    type Stream = NativeStream;

    fn accept(&mut self, stop: &StopSignal) -> io::Result<(NativeStream, u32)> {
        let listener = self
            .listener
            .as_ref()
            .ok_or_else(|| io::Error::from(io::ErrorKind::NotConnected))?;
        loop {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            self.check_paths()?;
            match listener.accept() {
                Ok((stream, _)) => match peer(&stream) {
                    Ok((pid, uid)) if uid == self.uid => match NativeStream::from_unix(stream) {
                        Ok(native) => return Ok((native, pid)),
                        Err(_) => continue,
                    },
                    Ok(_) | Err(_) => continue,
                },
                Err(error) if is_transient_accept_error(&error) => continue,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    poll_fd(listener.as_raw_fd(), libc::POLLIN, stop, None)?;
                }
                Err(error) => return Err(error),
            }
        }
    }
}

#[cfg(unix)]
impl Drop for Listener {
    fn drop(&mut self) {
        use std::fs;
        use std::mem::MaybeUninit;
        use std::os::unix::fs::MetadataExt;

        self.listener.take();
        if let Some(dir_file) = self.dir_file.take() {
            let mut st = MaybeUninit::<libc::stat>::zeroed();
            let rc = unsafe {
                libc::fstatat(
                    dir_file.as_raw_fd(),
                    c"a.sock".as_ptr(),
                    st.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            };
            if rc == 0 {
                let st = unsafe { st.assume_init() };
                if stat_id(&st) == self.sock_id {
                    unsafe {
                        let _ = libc::unlinkat(dir_file.as_raw_fd(), c"a.sock".as_ptr(), 0);
                    }
                }
            }
            drop(dir_file);
        }
        if let Ok(meta) = fs::symlink_metadata(&self.dir_path)
            && (meta.dev(), meta.ino()) == self.dir_id
        {
            let _ = fs::remove_dir(&self.dir_path);
        }
    }
}

#[cfg(windows)]
mod win_ipc {
    use std::ffi::c_void;
    use std::io;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
    use std::ptr;
    use std::time::Instant;

    use super::StopSignal;

    const ERROR_BROKEN_PIPE: u32 = 109;
    const ERROR_INSUFFICIENT_BUFFER: u32 = 122;
    const ERROR_NO_DATA: u32 = 232;
    const ERROR_PIPE_CONNECTED: u32 = 535;
    const ERROR_OPERATION_ABORTED: u32 = 995;
    const ERROR_IO_PENDING: u32 = 997;
    const WAIT_OBJECT_0: u32 = 0;
    const WAIT_OBJECT_1: u32 = 1;
    const WAIT_TIMEOUT: u32 = 258;
    const INFINITE: u32 = 0xFFFF_FFFF;
    const TOKEN_QUERY: u32 = 0x0008;
    const TOKEN_GROUPS_CLASS: u32 = 2;
    const SE_GROUP_LOGON_ID: u32 = 0xC000_0000;
    const SDDL_REVISION_1: u32 = 1;
    const PIPE_ACCESS_DUPLEX: u32 = 0x0000_0003;
    const FILE_FLAG_FIRST_PIPE_INSTANCE: u32 = 0x0008_0000;
    const FILE_FLAG_OVERLAPPED: u32 = 0x4000_0000;
    const PIPE_REJECT_REMOTE_CLIENTS: u32 = 0x0000_0008;

    #[repr(C)]
    struct Overlapped {
        internal: usize,
        internal_high: usize,
        offset: u32,
        offset_high: u32,
        h_event: RawHandle,
    }

    #[repr(C)]
    struct SecurityAttributes {
        n_length: u32,
        lp_security_descriptor: *mut c_void,
        b_inherit_handle: i32,
    }

    #[repr(C)]
    struct SidAndAttributes {
        sid: *mut c_void,
        attributes: u32,
    }

    #[repr(C)]
    struct TokenGroupsHeader {
        group_count: u32,
        groups: [SidAndAttributes; 1],
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetCurrentProcess() -> RawHandle;
        fn CreateEventW(
            attrs: *const c_void,
            manual_reset: i32,
            initial_state: i32,
            name: *const u16,
        ) -> RawHandle;
        fn SetEvent(event: RawHandle) -> i32;
        fn CreateNamedPipeW(
            name: *const u16,
            open_mode: u32,
            pipe_mode: u32,
            max_instances: u32,
            out_buffer_size: u32,
            in_buffer_size: u32,
            default_timeout: u32,
            security_attributes: *const SecurityAttributes,
        ) -> RawHandle;
        fn ConnectNamedPipe(pipe: RawHandle, overlapped: *mut Overlapped) -> i32;
        fn ReadFile(
            file: RawHandle,
            buffer: *mut c_void,
            to_read: u32,
            read: *mut u32,
            overlapped: *mut Overlapped,
        ) -> i32;
        fn WriteFile(
            file: RawHandle,
            buffer: *const c_void,
            to_write: u32,
            written: *mut u32,
            overlapped: *mut Overlapped,
        ) -> i32;
        fn GetOverlappedResult(
            file: RawHandle,
            overlapped: *mut Overlapped,
            transferred: *mut u32,
            wait: i32,
        ) -> i32;
        fn CancelIoEx(file: RawHandle, overlapped: *mut Overlapped) -> i32;
        fn WaitForMultipleObjects(
            count: u32,
            handles: *const RawHandle,
            wait_all: i32,
            milliseconds: u32,
        ) -> u32;
        fn GetNamedPipeClientProcessId(pipe: RawHandle, pid: *mut u32) -> i32;
        fn GetNamedPipeServerProcessId(pipe: RawHandle, pid: *mut u32) -> i32;
        fn LocalFree(mem: *mut c_void) -> *mut c_void;
        fn GetLastError() -> u32;
    }

    #[link(name = "advapi32")]
    unsafe extern "system" {
        fn OpenProcessToken(process: RawHandle, desired_access: u32, token: *mut RawHandle) -> i32;
        fn GetTokenInformation(
            token: RawHandle,
            class: u32,
            info: *mut c_void,
            length: u32,
            return_length: *mut u32,
        ) -> i32;
        fn ConvertSidToStringSidW(sid: *mut c_void, string_sid: *mut *mut u16) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl: *const u16,
            revision: u32,
            sd: *mut *mut c_void,
            sd_size: *mut u32,
        ) -> i32;
    }

    struct LocalMem(*mut c_void);

    impl Drop for LocalMem {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    LocalFree(self.0);
                }
            }
        }
    }

    pub(super) fn create_event(manual_reset: bool) -> io::Result<OwnedHandle> {
        let handle = unsafe { CreateEventW(ptr::null(), i32::from(manual_reset), 0, ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }

    pub(super) fn set_event(event: &OwnedHandle) {
        unsafe {
            let _ = SetEvent(event.as_raw_handle());
        }
    }

    pub(super) fn current_logon_sid() -> io::Result<String> {
        let mut raw_token: RawHandle = ptr::null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw_token) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = unsafe { OwnedHandle::from_raw_handle(raw_token) };
        let mut needed = 0_u32;
        let rc = unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TOKEN_GROUPS_CLASS,
                ptr::null_mut(),
                0,
                &mut needed,
            )
        };
        if rc != 0 {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        let err = unsafe { GetLastError() };
        if err != ERROR_INSUFFICIENT_BUFFER || needed == 0 {
            return Err(io::Error::from_raw_os_error(err as i32));
        }
        let mut buffer = vec![0_u64; (needed as usize).div_ceil(size_of::<u64>())];
        if unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TOKEN_GROUPS_CLASS,
                buffer.as_mut_ptr().cast(),
                needed,
                &mut needed,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let total_bytes = buffer.len() * size_of::<u64>();
        if total_bytes < size_of::<u32>() {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        let group_count = unsafe { *buffer.as_ptr().cast::<u32>() } as usize;
        let groups_offset = std::mem::offset_of!(TokenGroupsHeader, groups);
        let required = groups_offset
            .checked_add(
                group_count
                    .checked_mul(size_of::<SidAndAttributes>())
                    .ok_or(io::ErrorKind::PermissionDenied)?,
            )
            .ok_or(io::ErrorKind::PermissionDenied)?;
        if required > total_bytes {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        let groups_ptr = unsafe {
            buffer
                .as_ptr()
                .cast::<u8>()
                .add(groups_offset)
                .cast::<SidAndAttributes>()
        };
        for index in 0..group_count {
            let entry = unsafe { &*groups_ptr.add(index) };
            if (entry.attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID {
                let mut sid_ptr: *mut u16 = ptr::null_mut();
                if unsafe { ConvertSidToStringSidW(entry.sid, &mut sid_ptr) } == 0
                    || sid_ptr.is_null()
                {
                    return Err(io::Error::last_os_error());
                }
                let _guard = LocalMem(sid_ptr.cast());
                let mut len = 0_usize;
                while len < 256 && unsafe { *sid_ptr.add(len) } != 0 {
                    len += 1;
                }
                if len == 0 || unsafe { *sid_ptr.add(len) } != 0 {
                    return Err(io::ErrorKind::InvalidData.into());
                }
                let wide = unsafe { std::slice::from_raw_parts(sid_ptr, len) };
                return String::from_utf16(wide)
                    .map_err(|_| io::Error::from(io::ErrorKind::InvalidData));
            }
        }
        Err(io::ErrorKind::PermissionDenied.into())
    }

    pub(super) fn create_pipe_instance(
        name_utf16: &[u16],
        sddl_utf16: &[u16],
        first: bool,
    ) -> io::Result<OwnedHandle> {
        let mut sd: *mut c_void = ptr::null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl_utf16.as_ptr(),
                SDDL_REVISION_1,
                &mut sd,
                ptr::null_mut(),
            )
        } == 0
            || sd.is_null()
        {
            return Err(io::Error::last_os_error());
        }
        let _sd_guard = LocalMem(sd);
        let attrs = SecurityAttributes {
            n_length: size_of::<SecurityAttributes>() as u32,
            lp_security_descriptor: sd,
            b_inherit_handle: 0,
        };
        let open_mode = PIPE_ACCESS_DUPLEX
            | FILE_FLAG_OVERLAPPED
            | if first {
                FILE_FLAG_FIRST_PIPE_INSTANCE
            } else {
                0
            };
        let handle = unsafe {
            CreateNamedPipeW(
                name_utf16.as_ptr(),
                open_mode,
                PIPE_REJECT_REMOTE_CLIENTS,
                255,
                65536,
                65536,
                0,
                &attrs,
            )
        };
        let invalid = ptr::without_provenance_mut::<c_void>(usize::MAX);
        if handle == invalid || handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }

    fn wait_ms_from_deadline(deadline: Option<Instant>) -> io::Result<u32> {
        match deadline {
            None => Ok(INFINITE),
            Some(limit) => {
                let now = Instant::now();
                if now >= limit {
                    return Err(io::ErrorKind::TimedOut.into());
                }
                let ms = limit.duration_since(now).as_millis();
                Ok((ms.min((INFINITE - 1) as u128) as u32).max(1))
            }
        }
    }

    fn map_pipe_error(code: u32) -> io::Error {
        match code {
            ERROR_BROKEN_PIPE | ERROR_NO_DATA => io::ErrorKind::BrokenPipe.into(),
            ERROR_OPERATION_ABORTED => io::ErrorKind::ConnectionAborted.into(),
            _ => io::Error::from_raw_os_error(code as i32),
        }
    }

    fn finish_pending(
        handle: RawHandle,
        overlapped: &mut Overlapped,
        io_event: &OwnedHandle,
        stop: &StopSignal,
        deadline: Option<Instant>,
        is_read: bool,
    ) -> io::Result<usize> {
        let wait_ms = match wait_ms_from_deadline(deadline) {
            Ok(ms) => ms,
            Err(timeout) => {
                unsafe {
                    let _ = CancelIoEx(handle, overlapped);
                    let mut transferred = 0_u32;
                    let _ = GetOverlappedResult(handle, overlapped, &mut transferred, 1);
                }
                return Err(timeout);
            }
        };
        let handles = [io_event.as_raw_handle(), stop.raw_handle()];
        let wait = unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, wait_ms) };
        if wait == WAIT_OBJECT_0 {
            let mut transferred = 0_u32;
            if unsafe { GetOverlappedResult(handle, overlapped, &mut transferred, 1) } == 0 {
                let err = unsafe { GetLastError() };
                if is_read && matches!(err, ERROR_BROKEN_PIPE | ERROR_NO_DATA) {
                    return Ok(0);
                }
                return Err(map_pipe_error(err));
            }
            return Ok(transferred as usize);
        }
        let wait_err = if wait != WAIT_OBJECT_1 && wait != WAIT_TIMEOUT {
            Some(io::Error::last_os_error())
        } else {
            None
        };
        unsafe {
            let _ = CancelIoEx(handle, overlapped);
            let mut transferred = 0_u32;
            let _ = GetOverlappedResult(handle, overlapped, &mut transferred, 1);
        }
        match wait {
            WAIT_OBJECT_1 => Err(io::ErrorKind::ConnectionAborted.into()),
            WAIT_TIMEOUT => Err(io::ErrorKind::TimedOut.into()),
            _ => Err(wait_err.unwrap_or_else(|| io::Error::from(io::ErrorKind::Other))),
        }
    }

    pub(super) fn connect_named_pipe(pipe: &OwnedHandle, stop: &StopSignal) -> io::Result<()> {
        if stop.is_triggered() {
            return Err(io::ErrorKind::ConnectionAborted.into());
        }
        let io_event = create_event(true)?;
        let mut overlapped = Overlapped {
            internal: 0,
            internal_high: 0,
            offset: 0,
            offset_high: 0,
            h_event: io_event.as_raw_handle(),
        };
        let rc = unsafe { ConnectNamedPipe(pipe.as_raw_handle(), &raw mut overlapped) };
        if rc != 0 {
            return Ok(());
        }
        let err = unsafe { GetLastError() };
        if err == ERROR_PIPE_CONNECTED {
            return Ok(());
        }
        if err != ERROR_IO_PENDING {
            return Err(map_pipe_error(err));
        }
        let _ = finish_pending(
            pipe.as_raw_handle(),
            &mut overlapped,
            &io_event,
            stop,
            None,
            false,
        )?;
        Ok(())
    }

    pub(super) fn read_overlapped(
        pipe: &OwnedHandle,
        buf: &mut [u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        if stop.is_triggered() {
            return Err(io::ErrorKind::ConnectionAborted.into());
        }
        if buf.is_empty() {
            return Ok(0);
        }
        let _ = wait_ms_from_deadline(deadline)?;
        let io_event = create_event(true)?;
        let mut overlapped = Overlapped {
            internal: 0,
            internal_high: 0,
            offset: 0,
            offset_high: 0,
            h_event: io_event.as_raw_handle(),
        };
        let to_read = buf.len().min(u32::MAX as usize) as u32;
        let rc = unsafe {
            ReadFile(
                pipe.as_raw_handle(),
                buf.as_mut_ptr().cast(),
                to_read,
                ptr::null_mut(),
                &raw mut overlapped,
            )
        };
        if rc != 0 {
            let mut transferred = 0_u32;
            if unsafe {
                GetOverlappedResult(
                    pipe.as_raw_handle(),
                    &raw mut overlapped,
                    &mut transferred,
                    1,
                )
            } == 0
            {
                let err = unsafe { GetLastError() };
                if matches!(err, ERROR_BROKEN_PIPE | ERROR_NO_DATA) {
                    return Ok(0);
                }
                return Err(map_pipe_error(err));
            }
            return Ok(transferred as usize);
        }
        let err = unsafe { GetLastError() };
        if matches!(err, ERROR_BROKEN_PIPE | ERROR_NO_DATA) {
            return Ok(0);
        }
        if err != ERROR_IO_PENDING {
            return Err(map_pipe_error(err));
        }
        finish_pending(
            pipe.as_raw_handle(),
            &mut overlapped,
            &io_event,
            stop,
            deadline,
            true,
        )
    }

    pub(super) fn write_overlapped(
        pipe: &OwnedHandle,
        buf: &[u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        if stop.is_triggered() {
            return Err(io::ErrorKind::ConnectionAborted.into());
        }
        if buf.is_empty() {
            return Ok(0);
        }
        let _ = wait_ms_from_deadline(deadline)?;
        let io_event = create_event(true)?;
        let mut overlapped = Overlapped {
            internal: 0,
            internal_high: 0,
            offset: 0,
            offset_high: 0,
            h_event: io_event.as_raw_handle(),
        };
        let to_write = buf.len().min(u32::MAX as usize) as u32;
        let rc = unsafe {
            WriteFile(
                pipe.as_raw_handle(),
                buf.as_ptr().cast(),
                to_write,
                ptr::null_mut(),
                &raw mut overlapped,
            )
        };
        if rc != 0 {
            let mut transferred = 0_u32;
            if unsafe {
                GetOverlappedResult(
                    pipe.as_raw_handle(),
                    &raw mut overlapped,
                    &mut transferred,
                    1,
                )
            } == 0
            {
                return Err(map_pipe_error(unsafe { GetLastError() }));
            }
            return Ok(transferred as usize);
        }
        let err = unsafe { GetLastError() };
        if err != ERROR_IO_PENDING {
            return Err(map_pipe_error(err));
        }
        finish_pending(
            pipe.as_raw_handle(),
            &mut overlapped,
            &io_event,
            stop,
            deadline,
            false,
        )
    }

    pub(super) fn pipe_client_pid(pipe: &OwnedHandle) -> io::Result<u32> {
        let mut pid = 0_u32;
        if unsafe { GetNamedPipeClientProcessId(pipe.as_raw_handle(), &mut pid) } == 0 || pid == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(pid)
    }

    pub(super) fn pipe_server_pid(pipe: RawHandle) -> io::Result<u32> {
        let mut pid = 0_u32;
        if unsafe { GetNamedPipeServerProcessId(pipe, &mut pid) } == 0 || pid == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(pid)
    }
}

#[cfg(windows)]
pub(crate) struct NativeStream {
    handle: OwnedHandle,
}

#[cfg(windows)]
impl NativeStream {
    fn from_handle(handle: OwnedHandle) -> Self {
        Self { handle }
    }
}

#[cfg(windows)]
impl SessionStream for NativeStream {
    fn read_chunk(
        &mut self,
        buf: &mut [u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        win_ipc::read_overlapped(&self.handle, buf, deadline, stop)
    }

    fn write_chunk(
        &mut self,
        buf: &[u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        win_ipc::write_overlapped(&self.handle, buf, deadline, stop)
    }
}

#[cfg(windows)]
pub(crate) fn connect(address: &str, expected_pid: u32) -> io::Result<NativeStream> {
    const SECURITY_IDENTIFICATION: u32 = 0x0001_0000;
    const FILE_FLAG_OVERLAPPED: u32 = 0x4000_0000;
    const ERROR_PIPE_BUSY: i32 = 231;

    if !address.starts_with(r"\\.\pipe\") || address[9..].contains('\\') {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let mut channel = None;
    for attempt in 0..200 {
        match File::options()
            .read(true)
            .write(true)
            .security_qos_flags(SECURITY_IDENTIFICATION)
            .custom_flags(FILE_FLAG_OVERLAPPED)
            .open(address)
        {
            Ok(file) => {
                channel = Some(file);
                break;
            }
            Err(error) if error.raw_os_error() == Some(ERROR_PIPE_BUSY) && attempt < 199 => {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(error) => return Err(error),
        }
    }
    let channel = channel.ok_or_else(|| io::Error::from(io::ErrorKind::TimedOut))?;
    let pid = win_ipc::pipe_server_pid(channel.as_raw_handle())?;
    if pid != expected_pid {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(NativeStream::from_handle(OwnedHandle::from(channel)))
}

#[cfg(windows)]
pub(crate) struct Listener {
    address: String,
    address_utf16: Vec<u16>,
    sddl_utf16: Vec<u16>,
    pending: Option<OwnedHandle>,
}

#[cfg(windows)]
impl Listener {
    pub(crate) fn bind() -> io::Result<Self> {
        let logon_sid = win_ipc::current_logon_sid()?;
        let sddl_utf16: Vec<u16> = format!("D:P(A;;GA;;;{logon_sid})\0")
            .encode_utf16()
            .collect();
        let address = format!(r"\\.\pipe\DeskWeaveStudio-agent-{}", random_hex(16)?);
        let address_utf16: Vec<u16> = format!("{address}\0").encode_utf16().collect();
        let first = win_ipc::create_pipe_instance(&address_utf16, &sddl_utf16, true)?;
        Ok(Self {
            address,
            address_utf16,
            sddl_utf16,
            pending: Some(first),
        })
    }

    pub(crate) fn address(&self) -> &str {
        &self.address
    }
}

#[cfg(windows)]
impl SessionListener for Listener {
    type Stream = NativeStream;

    fn accept(&mut self, stop: &StopSignal) -> io::Result<(NativeStream, u32)> {
        loop {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            let current = match self.pending.take() {
                Some(handle) => handle,
                None => {
                    win_ipc::create_pipe_instance(&self.address_utf16, &self.sddl_utf16, false)?
                }
            };
            match win_ipc::connect_named_pipe(&current, stop) {
                Ok(()) => {
                    self.pending = Some(win_ipc::create_pipe_instance(
                        &self.address_utf16,
                        &self.sddl_utf16,
                        false,
                    )?);
                    match win_ipc::pipe_client_pid(&current) {
                        Ok(pid) => return Ok((NativeStream::from_handle(current), pid)),
                        Err(_) => continue,
                    }
                }
                Err(error) if stop.is_triggered() => return Err(error),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::BrokenPipe
                            | io::ErrorKind::ConnectionAborted
                            | io::ErrorKind::ConnectionReset
                    ) =>
                {
                    self.pending = Some(win_ipc::create_pipe_instance(
                        &self.address_utf16,
                        &self.sddl_utf16,
                        false,
                    )?);
                    continue;
                }
                Err(error) => return Err(error),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::macos_claim::{ATTEMPTS, LocalPort, RETRY_DELAY, retry_claim, validate_scope};
    use std::io;

    #[test]
    fn macos_scope_validation_enforces_bootstrap_bounds() {
        assert_eq!(
            validate_scope("").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            validate_scope(&"a".repeat(101)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            validate_scope("bad/scope").unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let max_service = validate_scope(&"a".repeat(100)).unwrap();
        assert_eq!(max_service.len(), 119);
    }

    #[test]
    fn macos_claim_returns_already_exists_only_for_confirmed_ownership() {
        let mut released = None;
        let mut remote_checks = 0;
        let mut pauses = 0;
        let duplicate = retry_claim(
            ATTEMPTS,
            RETRY_DELAY,
            || LocalPort::Duplicate(7_usize),
            |port| released = Some(port),
            || {
                remote_checks += 1;
                false
            },
            |_| pauses += 1,
        );
        assert_eq!(duplicate.unwrap_err().kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(released, Some(7));
        assert_eq!(remote_checks, 0);
        assert_eq!(pauses, 0);

        let mut create_calls = 0;
        remote_checks = 0;
        pauses = 0;
        let occupied = retry_claim::<usize>(
            ATTEMPTS,
            RETRY_DELAY,
            || {
                create_calls += 1;
                LocalPort::Unavailable
            },
            |_| unreachable!(),
            || {
                remote_checks += 1;
                true
            },
            |_| pauses += 1,
        );
        assert_eq!(occupied.unwrap_err().kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(create_calls, 1);
        assert_eq!(remote_checks, 1);
        assert_eq!(pauses, 0);
    }

    #[test]
    fn macos_claim_retries_transient_teardown_until_created() {
        let mut attempt = 0;
        let mut remote_checks = 0;
        let mut delays = Vec::new();
        let port = retry_claim(
            ATTEMPTS,
            RETRY_DELAY,
            || {
                attempt += 1;
                if attempt < 3 {
                    LocalPort::Unavailable
                } else {
                    LocalPort::Created(42_usize)
                }
            },
            |_| unreachable!(),
            || {
                remote_checks += 1;
                false
            },
            |delay| delays.push(delay),
        )
        .unwrap();
        assert_eq!(port, 42);
        assert_eq!(attempt, 3);
        assert_eq!(remote_checks, 2);
        assert_eq!(delays, vec![RETRY_DELAY, RETRY_DELAY]);
    }

    #[test]
    fn macos_claim_returns_native_error_when_retries_exhaust_without_owner() {
        let mut attempts = 0;
        let mut remote_checks = 0;
        let mut delays = Vec::new();
        let error = retry_claim::<usize>(
            ATTEMPTS,
            RETRY_DELAY,
            || {
                attempts += 1;
                LocalPort::Unavailable
            },
            |_| unreachable!(),
            || {
                remote_checks += 1;
                false
            },
            |delay| delays.push(delay),
        )
        .unwrap_err();
        assert_eq!(attempts, ATTEMPTS);
        assert_eq!(remote_checks, ATTEMPTS);
        assert_eq!(delays.len(), ATTEMPTS - 1);
        assert_eq!(error.kind(), io::ErrorKind::Other);
        assert!(!matches!(
            error.kind(),
            io::ErrorKind::AlreadyExists | io::ErrorKind::AddrInUse
        ));
    }

    #[test]
    fn stop_signal_triggers_once_and_persists() {
        let stop = super::StopSignal::new().unwrap();
        assert!(!stop.is_triggered());
        stop.trigger();
        assert!(stop.is_triggered());
        stop.trigger();
        assert!(stop.is_triggered());
    }

    #[cfg(windows)]
    #[test]
    fn windows_current_logon_sid_is_present() {
        let sid = super::win_ipc::current_logon_sid().unwrap();
        assert!(sid.starts_with("S-1-5-5-"));
    }

    #[cfg(windows)]
    #[test]
    fn windows_listener_accepts_peer_recovers_from_abort_and_wakes_on_stop() {
        use super::{DeadlineIo, Listener, SessionListener, StopSignal, connect};
        use std::io::{Read, Write};
        use std::thread;
        use std::time::{Duration, Instant};

        let mut listener = Listener::bind().unwrap();
        let address = listener.address().to_owned();
        let aborted = connect(&address, std::process::id()).unwrap();
        drop(aborted);

        let stop = StopSignal::new().unwrap();
        let accept_stop = stop.clone();
        let server = thread::spawn(move || {
            let deadline = Some(Instant::now() + Duration::from_secs(2));
            loop {
                let (mut stream, pid) = listener.accept(&accept_stop).unwrap();
                assert_eq!(pid, std::process::id());
                let mut buf = [0_u8; 4];
                match DeadlineIo::new(&mut stream, deadline, &accept_stop).read_exact(&mut buf) {
                    Ok(()) => {
                        assert_eq!(&buf, b"ping");
                        DeadlineIo::new(&mut stream, deadline, &accept_stop)
                            .write_all(b"pong")
                            .unwrap();
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => continue,
                    Err(error) => panic!("unexpected server read error: {error}"),
                }
            }
            assert_eq!(
                listener.accept(&accept_stop).err().unwrap().kind(),
                io::ErrorKind::ConnectionAborted
            );
            drop(listener);
        });

        let mut client = connect(&address, std::process::id()).unwrap();
        let client_stop = StopSignal::new().unwrap();
        let deadline = Some(Instant::now() + Duration::from_secs(2));
        DeadlineIo::new(&mut client, deadline, &client_stop)
            .write_all(b"ping")
            .unwrap();
        let mut reply = [0_u8; 4];
        DeadlineIo::new(&mut client, deadline, &client_stop)
            .read_exact(&mut reply)
            .unwrap();
        assert_eq!(&reply, b"pong");
        drop(client);
        stop.trigger();
        server.join().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn unix_transient_accept_errors_are_classified() {
        assert!(super::is_transient_accept_error(&io::Error::from(
            io::ErrorKind::Interrupted
        )));
        assert!(super::is_transient_accept_error(&io::Error::from(
            io::ErrorKind::ConnectionAborted
        )));
        assert!(super::is_transient_accept_error(&io::Error::from(
            io::ErrorKind::ConnectionReset
        )));
        assert!(super::is_transient_accept_error(
            &io::Error::from_raw_os_error(libc::EPROTO)
        ));
        assert!(!super::is_transient_accept_error(&io::Error::from(
            io::ErrorKind::PermissionDenied
        )));
        assert!(!super::is_transient_accept_error(&io::Error::from(
            io::ErrorKind::InvalidInput
        )));
    }

    #[cfg(unix)]
    #[test]
    fn unix_listener_accepts_peer_wakes_on_stop_and_cleans_socket() {
        use super::{DeadlineIo, Listener, SessionListener, StopSignal, connect};
        use std::io::{Read, Write};
        use std::path::PathBuf;
        use std::thread;
        use std::time::{Duration, Instant};

        let mut listener = Listener::bind().unwrap();
        let address = listener.address().to_owned();
        let sock_path = PathBuf::from(&address);
        let dir_path = sock_path.parent().unwrap().to_path_buf();
        let aborted = connect(&address, std::process::id()).unwrap();
        drop(aborted);

        let stop = StopSignal::new().unwrap();
        let accept_stop = stop.clone();
        let server = thread::spawn(move || {
            let deadline = Some(Instant::now() + Duration::from_secs(2));
            loop {
                let (mut stream, pid) = listener.accept(&accept_stop).unwrap();
                assert_eq!(pid, std::process::id());
                let mut buf = [0_u8; 4];
                match DeadlineIo::new(&mut stream, deadline, &accept_stop).read_exact(&mut buf) {
                    Ok(()) => {
                        assert_eq!(&buf, b"ping");
                        DeadlineIo::new(&mut stream, deadline, &accept_stop)
                            .write_all(b"pong")
                            .unwrap();
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => continue,
                    Err(error) => panic!("unexpected server read error: {error}"),
                }
            }
            assert_eq!(
                listener.accept(&accept_stop).err().unwrap().kind(),
                io::ErrorKind::ConnectionAborted
            );
            drop(listener);
        });

        let mut client = connect(&address, std::process::id()).unwrap();
        let client_stop = StopSignal::new().unwrap();
        let deadline = Some(Instant::now() + Duration::from_secs(2));
        DeadlineIo::new(&mut client, deadline, &client_stop)
            .write_all(b"ping")
            .unwrap();
        let mut reply = [0_u8; 4];
        DeadlineIo::new(&mut client, deadline, &client_stop)
            .read_exact(&mut reply)
            .unwrap();
        assert_eq!(&reply, b"pong");
        stop.trigger();
        server.join().unwrap();
        assert!(!sock_path.exists());
        assert!(!dir_path.exists());
    }
}
