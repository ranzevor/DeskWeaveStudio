use std::sync::Arc;

use deskweave_platform_api::DesktopSession;

pub(crate) fn current_session(wayland_backend: Option<&str>) -> Arc<dyn DesktopSession> {
    #[cfg(windows)]
    {
        let _ = wayland_backend;
        Arc::new(deskweave_platform_windows::WindowsBackend::detect())
    }
    #[cfg(target_os = "macos")]
    {
        let _ = wayland_backend;
        Arc::new(deskweave_platform_macos::MacOsBackend::detect())
    }
    #[cfg(target_os = "linux")]
    {
        use deskweave_platform_linux_wayland::{LinuxDesktopSession, WaylandBackendMode};

        let override_mode = wayland_backend.and_then(WaylandBackendMode::parse);
        Arc::new(LinuxDesktopSession::detect(override_mode))
    }
    #[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
    {
        compile_error!("unsupported target OS for deskweave-agent");
    }
}
