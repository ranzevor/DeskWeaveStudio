use std::fs::File;
use std::io;

#[cfg(unix)]
use std::os::fd::{AsFd, AsRawFd};
#[cfg(unix)]
use std::os::unix::net::UnixStream;
#[cfg(windows)]
use std::os::windows::{fs::OpenOptionsExt, io::AsHandle};

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
pub(crate) fn claim_scope(scope: &str) -> io::Result<std::os::windows::io::OwnedHandle> {
    use std::os::windows::io::{FromRawHandle, OwnedHandle, RawHandle};
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

#[cfg(unix)]
pub(crate) fn connect(address: &str, expected_pid: u32) -> io::Result<UnixStream> {
    if !std::path::Path::new(address).is_absolute() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let channel = UnixStream::connect(address)?;
    let (pid, uid) = peer(&channel)?;
    if pid != expected_pid || uid != unsafe { libc::geteuid() } {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(channel)
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
fn peer(channel: &UnixStream) -> io::Result<(u32, u32)> {
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
    Ok((token[5], token[1]))
}

#[cfg(windows)]
pub(crate) fn connect(address: &str, expected_pid: u32) -> io::Result<File> {
    use std::os::windows::io::{AsRawHandle, RawHandle};

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetNamedPipeServerProcessId(pipe: RawHandle, pid: *mut u32) -> i32;
    }

    const SECURITY_IDENTIFICATION: u32 = 0x0001_0000;
    if !address.starts_with(r"\\.\pipe\") {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let channel = File::options()
        .read(true)
        .write(true)
        .security_qos_flags(SECURITY_IDENTIFICATION)
        .open(address)?;
    let mut pid = 0;
    if unsafe { GetNamedPipeServerProcessId(channel.as_raw_handle(), &mut pid) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if pid != expected_pid {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(channel)
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
}
