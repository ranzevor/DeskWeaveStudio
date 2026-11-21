use std::sync::Arc;

use deskweave_platform_api::DesktopSession;

pub(crate) fn current_session() -> Arc<dyn DesktopSession> {
    #[cfg(windows)]
    {
        Arc::new(deskweave_platform_windows::WindowsBackend::detect())
    }
    #[cfg(target_os = "macos")]
    {
        Arc::new(deskweave_platform_macos::MacOsBackend::detect())
    }
    #[cfg(target_os = "linux")]
    {
        use deskweave_platform_api::PlatformArch;
        use deskweave_platform_linux_wayland::{
            LinuxWaylandCompositorBackend, LinuxWaylandPortalBackend, SelectedLinuxBackend,
            probe_linux_session_facts, select_linux_session,
        };
        use deskweave_platform_linux_x11::LinuxX11Backend;

        let arch = PlatformArch::current();
        let facts = probe_linux_session_facts();
        match select_linux_session(&facts) {
            SelectedLinuxBackend::WaylandPortal { active } => {
                Arc::new(LinuxWaylandPortalBackend::new(arch, active))
            }
            SelectedLinuxBackend::WaylandCompositor { active } => {
                Arc::new(LinuxWaylandCompositorBackend::new(arch, active))
            }
            SelectedLinuxBackend::X11 { active } => Arc::new(LinuxX11Backend::new(arch, active)),
            SelectedLinuxBackend::Inactive => Arc::new(LinuxWaylandPortalBackend::new(arch, false)),
        }
    }
    #[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
    {
        compile_error!("unsupported target OS for deskweave-agent");
    }
}
