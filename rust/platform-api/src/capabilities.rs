use std::collections::HashSet;

use serde::{Deserialize, Deserializer, Serialize};

use crate::error::PlatformError;

pub const MAX_SAFE_INT: u64 = 9_007_199_254_740_991;

/// Deserializes a JSON field that is REQUIRED to be present, yet allowed to be `null`.
pub(crate) fn deserialize_required_nullable<'de, D, T>(
    deserializer: D,
) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

/// Deserializes a JSON field that may be OMITTED, yet when present MUST NOT be `null`.
pub(crate) fn deserialize_optional_non_null<'de, D, T>(
    deserializer: D,
) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PlatformOs {
    Windows,
    Macos,
    Linux,
}

impl PlatformOs {
    pub fn current() -> Self {
        #[cfg(windows)]
        {
            Self::Windows
        }
        #[cfg(target_os = "macos")]
        {
            Self::Macos
        }
        #[cfg(target_os = "linux")]
        {
            Self::Linux
        }
        #[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
        {
            Self::Linux
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Windows => "windows",
            Self::Macos => "macos",
            Self::Linux => "linux",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PlatformArch {
    X64,
    Arm64,
}

impl PlatformArch {
    pub fn current() -> Self {
        #[cfg(target_arch = "aarch64")]
        {
            Self::Arm64
        }
        #[cfg(not(target_arch = "aarch64"))]
        {
            Self::X64
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::X64 => "x64",
            Self::Arm64 => "arm64",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BackendKind {
    #[serde(rename = "windows-win32")]
    WindowsWin32,
    #[serde(rename = "macos-quartz")]
    MacosQuartz,
    #[serde(rename = "linux-x11")]
    LinuxX11,
    #[serde(rename = "linux-wayland-portal")]
    LinuxWaylandPortal,
    #[serde(rename = "linux-wayland-compositor")]
    LinuxWaylandCompositor,
}

impl BackendKind {
    pub fn expected_os(self) -> PlatformOs {
        match self {
            Self::WindowsWin32 => PlatformOs::Windows,
            Self::MacosQuartz => PlatformOs::Macos,
            Self::LinuxX11 | Self::LinuxWaylandPortal | Self::LinuxWaylandCompositor => {
                PlatformOs::Linux
            }
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::WindowsWin32 => "windows-win32",
            Self::MacosQuartz => "macos-quartz",
            Self::LinuxX11 => "linux-x11",
            Self::LinuxWaylandPortal => "linux-wayland-portal",
            Self::LinuxWaylandCompositor => "linux-wayland-compositor",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TopologyCoverage {
    All,
    Selected,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TrajectoryVersion {
    #[serde(rename = "1.0")]
    V1_0,
    #[serde(rename = "1.1")]
    V1_1,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum FeatureState {
    #[serde(rename = "available")]
    Available,
    #[serde(rename = "permissionRequired")]
    PermissionRequired,
    #[serde(rename = "denied")]
    Denied,
    #[serde(rename = "unsupported")]
    Unsupported,
    #[serde(rename = "unimplemented")]
    Unimplemented,
    #[serde(rename = "sessionUnavailable")]
    SessionUnavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum FeatureReason {
    #[serde(rename = "NONE")]
    None,
    #[serde(rename = "PERMISSION_MISSING")]
    PermissionMissing,
    #[serde(rename = "PERMISSION_DENIED")]
    PermissionDenied,
    #[serde(rename = "NO_PASSIVE_CAPTURE")]
    NoPassiveCapture,
    #[serde(rename = "BACKEND_UNAVAILABLE")]
    BackendUnavailable,
    #[serde(rename = "SESSION_INACTIVE")]
    SessionInactive,
    #[serde(rename = "SELECTION_REQUIRED")]
    SelectionRequired,
    #[serde(rename = "CAPABILITY_NOT_IMPLEMENTED")]
    CapabilityNotImplemented,
    #[serde(rename = "PROTOCOL_MISSING")]
    ProtocolMissing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum NextAction {
    #[serde(rename = "none")]
    None,
    #[serde(rename = "grantPermission")]
    GrantPermission,
    #[serde(rename = "selectSource")]
    SelectSource,
    #[serde(rename = "switchSession")]
    SwitchSession,
    #[serde(rename = "updateComponent")]
    UpdateComponent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FeatureStatus {
    pub state: FeatureState,
    pub reason: FeatureReason,
    pub next_action: NextAction,
}

impl FeatureStatus {
    pub const fn available() -> Self {
        Self {
            state: FeatureState::Available,
            reason: FeatureReason::None,
            next_action: NextAction::None,
        }
    }

    pub const fn unimplemented() -> Self {
        Self {
            state: FeatureState::Unimplemented,
            reason: FeatureReason::CapabilityNotImplemented,
            next_action: NextAction::UpdateComponent,
        }
    }

    pub const fn session_unavailable() -> Self {
        Self {
            state: FeatureState::SessionUnavailable,
            reason: FeatureReason::SessionInactive,
            next_action: NextAction::SwitchSession,
        }
    }

    pub const fn unsupported(reason: FeatureReason, next_action: NextAction) -> Self {
        Self {
            state: FeatureState::Unsupported,
            reason,
            next_action,
        }
    }

    pub const fn permission_required(reason: FeatureReason, next_action: NextAction) -> Self {
        Self {
            state: FeatureState::PermissionRequired,
            reason,
            next_action,
        }
    }

    pub const fn denied(next_action: NextAction) -> Self {
        Self {
            state: FeatureState::Denied,
            reason: FeatureReason::PermissionDenied,
            next_action,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureSet {
    #[serde(rename = "capture.display")]
    pub capture_display: FeatureStatus,
    #[serde(rename = "capture.window")]
    pub capture_window: FeatureStatus,
    #[serde(rename = "record.pointerAbsolute")]
    pub record_pointer_absolute: FeatureStatus,
    #[serde(rename = "record.pointerRelative")]
    pub record_pointer_relative: FeatureStatus,
    #[serde(rename = "record.keyboard")]
    pub record_keyboard: FeatureStatus,
    #[serde(rename = "record.allDisplays")]
    pub record_all_displays: FeatureStatus,
    #[serde(rename = "input.pointerAbsolute")]
    pub input_pointer_absolute: FeatureStatus,
    #[serde(rename = "input.pointerRelative")]
    pub input_pointer_relative: FeatureStatus,
    #[serde(rename = "input.keyboard")]
    pub input_keyboard: FeatureStatus,
    #[serde(rename = "input.unicodeText")]
    pub input_unicode_text: FeatureStatus,
    #[serde(rename = "control.globalShortcut")]
    pub control_global_shortcut: FeatureStatus,
    #[serde(rename = "control.focusTarget")]
    pub control_focus_target: FeatureStatus,
}

impl FeatureSet {
    pub const fn all(status: FeatureStatus) -> Self {
        Self {
            capture_display: status,
            capture_window: status,
            record_pointer_absolute: status,
            record_pointer_relative: status,
            record_keyboard: status,
            record_all_displays: status,
            input_pointer_absolute: status,
            input_pointer_relative: status,
            input_keyboard: status,
            input_unicode_text: status,
            control_global_shortcut: status,
            control_focus_target: status,
        }
    }

    pub fn entries(&self) -> [(&'static str, FeatureStatus); 12] {
        [
            ("capture.display", self.capture_display),
            ("capture.window", self.capture_window),
            ("record.pointerAbsolute", self.record_pointer_absolute),
            ("record.pointerRelative", self.record_pointer_relative),
            ("record.keyboard", self.record_keyboard),
            ("record.allDisplays", self.record_all_displays),
            ("input.pointerAbsolute", self.input_pointer_absolute),
            ("input.pointerRelative", self.input_pointer_relative),
            ("input.keyboard", self.input_keyboard),
            ("input.unicodeText", self.input_unicode_text),
            ("control.globalShortcut", self.control_global_shortcut),
            ("control.focusTarget", self.control_focus_target),
        ]
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CapabilitySnapshot {
    pub os: PlatformOs,
    pub arch: PlatformArch,
    pub backend: BackendKind,
    pub backend_version: String,
    pub generation: u64,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub topology_version: Option<u64>,
    pub topology_coverage: TopologyCoverage,
    pub trajectory_versions: Vec<TrajectoryVersion>,
    pub features: FeatureSet,
}

impl CapabilitySnapshot {
    pub fn validate(&self) -> Result<(), PlatformError> {
        let version_chars = self.backend_version.chars().count();
        if !(1..=80).contains(&version_chars) {
            return Err(PlatformError::invalid_argument(
                "backendVersion 长度应为 1～80。",
            ));
        }
        if self.generation > MAX_SAFE_INT {
            return Err(PlatformError::invalid_argument(
                "能力代次超出安全整数范围。",
            ));
        }
        if let Some(topology_version) = self.topology_version
            && topology_version > MAX_SAFE_INT
        {
            return Err(PlatformError::invalid_argument(
                "拓扑版本超出安全整数范围。",
            ));
        }
        if !(1..=2).contains(&self.trajectory_versions.len()) {
            return Err(PlatformError::invalid_argument(
                "trajectoryVersions 数量应为 1～2。",
            ));
        }
        let mut seen = HashSet::with_capacity(self.trajectory_versions.len());
        for version in &self.trajectory_versions {
            if !seen.insert(*version) {
                return Err(PlatformError::invalid_argument(
                    "trajectoryVersions 存在重复项。",
                ));
            }
        }
        if self.os != self.backend.expected_os() {
            return Err(PlatformError::invalid_argument("系统与桌面后端不匹配。"));
        }
        for (_, item) in self.features.entries() {
            if item.state == FeatureState::Available
                && (item.reason != FeatureReason::None || item.next_action != NextAction::None)
            {
                return Err(PlatformError::invalid_argument(
                    "可用能力不能同时要求授权或恢复。",
                ));
            }
            if item.state != FeatureState::Available && item.reason == FeatureReason::None {
                return Err(PlatformError::invalid_argument("不可用能力缺少具体原因。"));
            }
            if item.state == FeatureState::PermissionRequired
                && !matches!(
                    item.next_action,
                    NextAction::GrantPermission | NextAction::SelectSource
                )
            {
                return Err(PlatformError::invalid_argument("授权状态缺少对应操作。"));
            }
        }
        if self.features.record_all_displays.state == FeatureState::Available {
            if self.topology_coverage != TopologyCoverage::All || self.topology_version.is_none() {
                return Err(PlatformError::invalid_argument(
                    "全部显示屏录制能力缺少完整拓扑。",
                ));
            }
            if self.features.record_pointer_absolute.state != FeatureState::Available
                || self.features.record_keyboard.state != FeatureState::Available
            {
                return Err(PlatformError::invalid_argument(
                    "全部显示屏录制缺少位置或键盘事件。",
                ));
            }
        }
        if self.backend == BackendKind::LinuxWaylandPortal
            && [
                self.features.record_pointer_absolute,
                self.features.record_pointer_relative,
                self.features.record_keyboard,
                self.features.record_all_displays,
            ]
            .iter()
            .any(|item| item.state == FeatureState::Available)
        {
            return Err(PlatformError::invalid_argument(
                "Portal 输入捕获不能作为普通桌面的被动录制。",
            ));
        }
        Ok(())
    }
}
