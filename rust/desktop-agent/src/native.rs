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
pub(crate) fn claim_scope(scope: &str) -> io::Result<File> {
    use std::ffi::OsStr;
    use std::fs::{self, DirBuilder};
    use std::os::unix::{
        ffi::OsStrExt,
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    };
    use std::path::PathBuf;

    const CS_DARWIN_USER_TEMP_DIR: libc::c_int = 65537;
    if scope.is_empty()
        || scope.len() > 128
        || !scope
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(io::ErrorKind::InvalidInput.into());
    }
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
    let uid = unsafe { libc::geteuid() };
    let directory = PathBuf::from(OsStr::from_bytes(&buffer[..length]))
        .join(format!("dw-agent-scope-v1-{uid}"));
    match DirBuilder::new().mode(0o700).create(&directory) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error),
    }
    let dir = File::options()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(&directory)?;
    let dir_info = dir.metadata()?;
    let dir_path_info = fs::symlink_metadata(&directory)?;
    if !dir_info.is_dir()
        || dir_info.uid() != uid
        || (dir_info.mode() & 0o077) != 0
        || (dir_info.dev(), dir_info.ino()) != (dir_path_info.dev(), dir_path_info.ino())
    {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    let path = directory.join(format!("{scope}.lock"));
    for _ in 0..4 {
        let file = File::options()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
        if let Err(error) = file.try_lock().map_err(io::Error::from) {
            if error.kind() == io::ErrorKind::WouldBlock {
                return Err(io::ErrorKind::AlreadyExists.into());
            }
            return Err(error);
        }
        let info = file.metadata()?;
        let current = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if !info.is_file() || info.uid() != uid || (info.mode() & 0o077) != 0 || info.nlink() != 1 {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        if (info.dev(), info.ino()) == (current.dev(), current.ino()) {
            return Ok(file);
        }
    }
    Err(io::ErrorKind::PermissionDenied.into())
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
