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

#[cfg(target_os = "macos")]
pub(crate) fn claim_scope(scope: &str) -> io::Result<ScopeClaim> {
    if scope.is_empty()
        || scope.len() > 100
        || !scope
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let name = macos_scope::CfString::create(&format!("deskweave.agent.v1.{scope}"))?;
    for attempt in 0..50 {
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
        if !port.is_null() {
            if should_free != 0 {
                unsafe {
                    macos_scope::CFRelease(port.cast_const());
                }
                return Err(io::ErrorKind::AlreadyExists.into());
            }
            return Ok(ScopeClaim { port });
        }
        let remote = unsafe { macos_scope::CFMessagePortCreateRemote(std::ptr::null(), name.raw) };
        if !remote.is_null() {
            let live = unsafe { macos_scope::CFMessagePortIsValid(remote) } != 0;
            unsafe {
                macos_scope::CFRelease(remote.cast_const());
            }
            if live {
                return Err(io::ErrorKind::AlreadyExists.into());
            }
        }
        if attempt < 49 {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
    Err(io::ErrorKind::AlreadyExists.into())
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
