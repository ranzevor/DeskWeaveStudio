use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

use crate::capabilities::{
    CapabilitySnapshot, FeatureState, MAX_SAFE_INT, TrajectoryVersion,
    deserialize_optional_non_null, deserialize_required_nullable,
};
use crate::error::PlatformError;
use crate::topology::{
    DisplayLocalTopology, DisplayPoint, DisplayTopology, InputProfile, LocalTopologyCoverage,
    MAX_COORD_F64, MIN_COORD_F64, NativeSpace, PhysicalPoint, PhysicalTopology, RelativeUnit,
    valid_id,
};

pub const MAX_AT_US: u64 = 86_400_000_000;
pub const MAX_EVENTS: usize = 100_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CoordinateMode {
    #[serde(rename = "physical")]
    Physical,
    #[serde(rename = "relative")]
    Relative,
    #[serde(rename = "event")]
    Event,
    #[serde(rename = "display")]
    Display,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum RecordingScope {
    #[serde(rename = "allDisplays")]
    AllDisplays,
    #[serde(rename = "display")]
    Display,
    #[serde(rename = "window")]
    Window,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PhysicalSource {
    pub scope: RecordingScope,
    pub topology: PhysicalTopologyWrapper,
    pub display_ids: Vec<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub initial_pointer: Option<PhysicalPoint>,
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_null",
        skip_serializing_if = "Option::is_none"
    )]
    pub input_profile: Option<InputProfile>,
}

/// Wrapper enforcing `coordinateSpace == "desktopPhysicalPixels"` inside `PhysicalSource`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "coordinateSpace")]
pub enum PhysicalTopologyWrapper {
    #[serde(rename = "desktopPhysicalPixels")]
    Physical(PhysicalTopology),
}

impl PhysicalTopologyWrapper {
    pub fn inner(&self) -> &PhysicalTopology {
        let Self::Physical(inner) = self;
        inner
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DisplaySource {
    pub scope: RecordingScope,
    pub topology: DisplayLocalTopologyWrapper,
    pub display_ids: Vec<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub initial_pointer: Option<DisplayPoint>,
    pub input_profile: InputProfile,
}

/// Wrapper enforcing `coordinateSpace == "displayLocalPixels"` inside `DisplaySource`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "coordinateSpace")]
pub enum DisplayLocalTopologyWrapper {
    #[serde(rename = "displayLocalPixels")]
    DisplayLocal(DisplayLocalTopology),
}

impl DisplayLocalTopologyWrapper {
    pub fn inner(&self) -> &DisplayLocalTopology {
        let Self::DisplayLocal(inner) = self;
        inner
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RecordingSource {
    Physical(PhysicalSource),
    Display(DisplaySource),
}

impl RecordingSource {
    pub fn validate(&self) -> Result<(), PlatformError> {
        match self {
            Self::Physical(source) => source.validate(),
            Self::Display(source) => source.validate(),
        }
    }

    pub fn scope(&self) -> RecordingScope {
        match self {
            Self::Physical(s) => s.scope,
            Self::Display(s) => s.scope,
        }
    }

    pub fn input_profile(&self) -> Option<InputProfile> {
        match self {
            Self::Physical(s) => s.input_profile,
            Self::Display(s) => Some(s.input_profile),
        }
    }

    pub fn is_display_local(&self) -> bool {
        matches!(self, Self::Display(_))
    }

    pub fn as_topology(&self) -> DisplayTopology {
        match self {
            Self::Physical(s) => DisplayTopology::Physical(s.topology.inner().clone()),
            Self::Display(s) => DisplayTopology::DisplayLocal(s.topology.inner().clone()),
        }
    }
}

fn validate_display_id_list(ids: &[String]) -> Result<HashSet<&str>, PlatformError> {
    if !(1..=64).contains(&ids.len()) {
        return Err(PlatformError::invalid_argument(
            "displayIds 数量应为 1～64。",
        ));
    }
    let mut set = HashSet::with_capacity(ids.len());
    for id in ids {
        if !valid_id(id) || !set.insert(id.as_str()) {
            return Err(PlatformError::invalid_argument(
                "displayIds 存在非法或重复标识。",
            ));
        }
    }
    Ok(set)
}

impl PhysicalSource {
    pub fn validate(&self) -> Result<(), PlatformError> {
        let topology = self.topology.inner();
        topology.validate()?;
        let selected = validate_display_id_list(&self.display_ids)?;
        let all_ids: HashSet<&str> = topology
            .displays
            .iter()
            .map(|d| d.display_id.as_str())
            .collect();
        if !selected.is_subset(&all_ids) {
            return Err(PlatformError::invalid_argument(
                "来源引用了不存在的显示屏。",
            ));
        }
        if matches!(
            self.scope,
            RecordingScope::AllDisplays | RecordingScope::Window
        ) && selected != all_ids
        {
            return Err(PlatformError::invalid_argument("来源未覆盖固定显示集合。"));
        }
        if self.scope == RecordingScope::Display && selected.len() != 1 {
            return Err(PlatformError::invalid_argument(
                "单屏录制只能选择一个显示区域。",
            ));
        }
        if let Some(point) = self.initial_pointer {
            point.validate()?;
            let inside = topology
                .displays
                .iter()
                .filter(|d| selected.contains(d.display_id.as_str()))
                .any(|d| d.bounds.contains_point(point));
            if !inside {
                return Err(PlatformError::invalid_argument(
                    "起始指针位于显示区域之外。",
                ));
            }
        }
        Ok(())
    }
}

impl DisplaySource {
    pub fn validate(&self) -> Result<(), PlatformError> {
        let topology = self.topology.inner();
        topology.validate()?;
        let selected = validate_display_id_list(&self.display_ids)?;
        let all_ids: HashSet<&str> = topology
            .displays
            .iter()
            .map(|d| d.display_id.as_str())
            .collect();
        if !selected.is_subset(&all_ids) {
            return Err(PlatformError::invalid_argument(
                "来源引用了不存在的显示屏。",
            ));
        }
        if self.scope == RecordingScope::AllDisplays {
            if topology.coverage != LocalTopologyCoverage::All || selected != all_ids {
                return Err(PlatformError::invalid_argument(
                    "全部显示屏录制需要完整桌面覆盖。",
                ));
            }
            if topology
                .displays
                .iter()
                .any(|d| d.layout_bounds.is_none() || d.pixel_to_native.is_none())
            {
                return Err(PlatformError::invalid_argument(
                    "全部显示屏录制缺少完整位置映射。",
                ));
            }
        }
        if self.scope == RecordingScope::Window && selected != all_ids {
            return Err(PlatformError::invalid_argument(
                "窗口录制来源未覆盖当时可用显示集合。",
            ));
        }
        if self.scope == RecordingScope::Display && selected.len() != 1 {
            return Err(PlatformError::invalid_argument(
                "单屏录制只能选择一个显示区域。",
            ));
        }
        if let Some(point) = &self.initial_pointer {
            point.validate()?;
            let inside = topology
                .displays
                .iter()
                .find(|d| {
                    d.display_id == point.display_id && selected.contains(d.display_id.as_str())
                })
                .is_some_and(|d| d.contains_pixel(point.x, point.y));
            if !inside {
                return Err(PlatformError::invalid_argument(
                    "起始指针不在所选显示区域。",
                ));
            }
        }
        if NativeSpace::for_backend(self.input_profile.backend) != topology.native_space {
            return Err(PlatformError::invalid_argument(
                "来源后端与原生坐标空间不符。",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PointerButton {
    Left,
    Right,
    Middle,
    X1,
    X2,
}

impl PointerButton {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Left => "left",
            Self::Right => "right",
            Self::Middle => "middle",
            Self::X1 => "x1",
            Self::X2 => "x2",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ButtonState {
    Down,
    Up,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum TrajectoryEvent {
    #[serde(rename = "pointer.move", rename_all = "camelCase")]
    PointerMove {
        at_us: u64,
        x: f64,
        y: f64,
        #[serde(
            default,
            deserialize_with = "deserialize_optional_non_null",
            skip_serializing_if = "Option::is_none"
        )]
        display_id: Option<String>,
    },
    #[serde(rename = "pointer.delta", rename_all = "camelCase")]
    PointerDelta { at_us: u64, dx: f64, dy: f64 },
    #[serde(rename = "pointer.button", rename_all = "camelCase")]
    PointerButton {
        at_us: u64,
        button: PointerButton,
        state: ButtonState,
    },
    #[serde(rename = "pointer.scroll", rename_all = "camelCase")]
    PointerScroll { at_us: u64, dx: f64, dy: f64 },
    #[serde(rename = "key.down", rename_all = "camelCase")]
    KeyDown { at_us: u64, key: String },
    #[serde(rename = "key.up", rename_all = "camelCase")]
    KeyUp { at_us: u64, key: String },
    #[serde(rename = "text", rename_all = "camelCase")]
    Text { at_us: u64, text: String },
}

impl TrajectoryEvent {
    pub fn at_us(&self) -> u64 {
        match self {
            Self::PointerMove { at_us, .. }
            | Self::PointerDelta { at_us, .. }
            | Self::PointerButton { at_us, .. }
            | Self::PointerScroll { at_us, .. }
            | Self::KeyDown { at_us, .. }
            | Self::KeyUp { at_us, .. }
            | Self::Text { at_us, .. } => *at_us,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TrajectoryDocument {
    pub format_version: TrajectoryVersion,
    pub coordinate_mode: CoordinateMode,
    pub events: Vec<TrajectoryEvent>,
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_null",
        skip_serializing_if = "Option::is_none"
    )]
    pub source: Option<RecordingSource>,
}

fn valid_coord(v: f64) -> bool {
    v.is_finite() && (MIN_COORD_F64..=MAX_COORD_F64).contains(&v)
}

fn is_json_integer(v: f64) -> bool {
    v.is_finite() && v.fract() == 0.0
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
enum HeldKey {
    Key(String),
    Button(PointerButton),
}

impl TrajectoryDocument {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !(1..=MAX_EVENTS).contains(&self.events.len()) {
            return Err(PlatformError::invalid_argument("轨迹事件数量超出范围。"));
        }
        if let Some(source) = &self.source {
            source.validate()?;
        }
        if self.format_version == TrajectoryVersion::V1_0 {
            if self.coordinate_mode == CoordinateMode::Display {
                return Err(PlatformError::schema_unsupported(
                    "轨迹 1.0 不支持 display 坐标模式。",
                ));
            }
            if let Some(source) = &self.source {
                match source {
                    RecordingSource::Display(_) => {
                        return Err(PlatformError::schema_unsupported(
                            "轨迹 1.0 不支持屏内像素来源。",
                        ));
                    }
                    RecordingSource::Physical(physical) if physical.input_profile.is_some() => {
                        return Err(PlatformError::schema_unsupported(
                            "轨迹 1.0 不支持 inputProfile。",
                        ));
                    }
                    RecordingSource::Physical(_) => {}
                }
            }
        }

        let has_relative = self
            .events
            .iter()
            .any(|event| matches!(event, TrajectoryEvent::PointerDelta { .. }));
        let is_local = self
            .source
            .as_ref()
            .is_some_and(RecordingSource::is_display_local);

        if self.coordinate_mode == CoordinateMode::Display && (!is_local || has_relative) {
            return Err(PlatformError::invalid_argument(
                "屏内像素模式需要对应来源且不能包含相对位移。",
            ));
        }
        if is_local && self.coordinate_mode == CoordinateMode::Physical {
            return Err(PlatformError::invalid_argument(
                "屏内像素来源不能解释成全局物理坐标。",
            ));
        }
        if self.format_version == TrajectoryVersion::V1_1 && has_relative {
            let unit = self
                .source
                .as_ref()
                .and_then(RecordingSource::input_profile)
                .map(|profile| profile.relative_unit)
                .unwrap_or(RelativeUnit::None);
            if unit == RelativeUnit::None {
                return Err(PlatformError::invalid_argument("相对轨迹缺少来源单位。"));
            }
        }

        let mut last_at_us = 0_u64;
        let mut held = HashSet::<HeldKey>::new();
        for (index, event) in self.events.iter().enumerate() {
            let at_us = event.at_us();
            if at_us > MAX_AT_US {
                return Err(PlatformError::invalid_argument("轨迹时间超出范围。"));
            }
            if index > 0 && at_us < last_at_us {
                return Err(PlatformError::invalid_argument("轨迹时间逆序。"));
            }
            last_at_us = at_us;

            match event {
                TrajectoryEvent::PointerMove {
                    x, y, display_id, ..
                } => {
                    if self.coordinate_mode == CoordinateMode::Relative {
                        return Err(PlatformError::invalid_argument("坐标模式冲突。"));
                    }
                    if !valid_coord(*x) || !valid_coord(*y) {
                        return Err(PlatformError::invalid_argument("移动坐标超出范围。"));
                    }
                    if self.format_version == TrajectoryVersion::V1_0
                        && (display_id.is_some() || !is_json_integer(*x) || !is_json_integer(*y))
                    {
                        return Err(PlatformError::schema_unsupported(
                            "轨迹 1.0 不允许 displayId 或小数坐标。",
                        ));
                    }
                    if let Some(id) = display_id
                        && !valid_id(id)
                    {
                        return Err(PlatformError::invalid_argument("displayId 无效。"));
                    }
                    match &self.source {
                        Some(RecordingSource::Display(source)) => {
                            let Some(id) = display_id.as_deref() else {
                                return Err(PlatformError::invalid_argument(
                                    "轨迹屏内位置缺少显示身份。",
                                ));
                            };
                            let valid = source.display_ids.iter().any(|s| s == id)
                                && source
                                    .topology
                                    .inner()
                                    .displays
                                    .iter()
                                    .find(|d| d.display_id == id)
                                    .is_some_and(|d| d.contains_pixel(*x, *y));
                            if !valid {
                                return Err(PlatformError::invalid_argument(
                                    "轨迹屏内位置超出像素范围或不在所选显示区域。",
                                ));
                            }
                        }
                        Some(RecordingSource::Physical(source)) => {
                            if display_id.is_some() {
                                return Err(PlatformError::invalid_argument(
                                    "带显示身份的位置需要屏内像素来源。",
                                ));
                            }
                            let selected: HashSet<&str> =
                                source.display_ids.iter().map(String::as_str).collect();
                            let inside = source
                                .topology
                                .inner()
                                .displays
                                .iter()
                                .filter(|d| selected.contains(d.display_id.as_str()))
                                .any(|d| d.bounds.contains_f64(*x, *y));
                            if !inside {
                                return Err(PlatformError::invalid_argument(
                                    "轨迹位置落在显示区域之外。",
                                ));
                            }
                        }
                        None => {
                            if display_id.is_some() {
                                return Err(PlatformError::invalid_argument(
                                    "带显示身份的位置需要屏内像素来源。",
                                ));
                            }
                        }
                    }
                }
                TrajectoryEvent::PointerDelta { dx, dy, .. } => {
                    if self.coordinate_mode == CoordinateMode::Physical {
                        return Err(PlatformError::invalid_argument("坐标模式冲突。"));
                    }
                    if !valid_coord(*dx) || !valid_coord(*dy) {
                        return Err(PlatformError::invalid_argument("相对位移超出范围。"));
                    }
                    if self.format_version == TrajectoryVersion::V1_0
                        && (!is_json_integer(*dx) || !is_json_integer(*dy))
                    {
                        return Err(PlatformError::schema_unsupported(
                            "轨迹 1.0 不允许小数相对位移。",
                        ));
                    }
                }
                TrajectoryEvent::PointerButton { button, state, .. } => {
                    let key = HeldKey::Button(*button);
                    match state {
                        ButtonState::Down => {
                            if !held.insert(key) {
                                return Err(PlatformError::invalid_argument("重复按下鼠标按钮。"));
                            }
                        }
                        ButtonState::Up => {
                            if !held.remove(&key) {
                                return Err(PlatformError::invalid_argument(
                                    "释放了未按下的鼠标按钮。",
                                ));
                            }
                        }
                    }
                }
                TrajectoryEvent::PointerScroll { dx, dy, .. } => {
                    if !dx.is_finite()
                        || !dy.is_finite()
                        || !(-10_000.0..=10_000.0).contains(dx)
                        || !(-10_000.0..=10_000.0).contains(dy)
                    {
                        return Err(PlatformError::invalid_argument("滚轮增量超出范围。"));
                    }
                    if self.format_version == TrajectoryVersion::V1_0
                        && (!is_json_integer(*dx) || !is_json_integer(*dy))
                    {
                        return Err(PlatformError::schema_unsupported(
                            "轨迹 1.0 不允许小数滚轮增量。",
                        ));
                    }
                }
                TrajectoryEvent::KeyDown { key, .. } => {
                    let chars = key.chars().count();
                    if !(1..=32).contains(&chars) {
                        return Err(PlatformError::invalid_argument("按键标识无效。"));
                    }
                    if !held.insert(HeldKey::Key(key.clone())) {
                        return Err(PlatformError::invalid_argument("重复按下按键。"));
                    }
                }
                TrajectoryEvent::KeyUp { key, .. } => {
                    let chars = key.chars().count();
                    if !(1..=32).contains(&chars) {
                        return Err(PlatformError::invalid_argument("按键标识无效。"));
                    }
                    if !held.remove(&HeldKey::Key(key.clone())) {
                        return Err(PlatformError::invalid_argument("释放了未按下的按键。"));
                    }
                }
                TrajectoryEvent::Text { text, .. } => {
                    if text.chars().count() > 262_144 {
                        return Err(PlatformError::invalid_argument("文本事件超过长度限制。"));
                    }
                }
            }
        }
        if !held.is_empty() {
            return Err(PlatformError::invalid_argument("轨迹结束仍有输入按住。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DisplayPair {
    pub source_display_id: String,
    pub target_display_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", deny_unknown_fields)]
pub enum DisplayMapping {
    #[serde(rename = "original")]
    Original {},
    #[serde(rename = "displayLocal")]
    DisplayLocal { displays: Vec<DisplayPair> },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReplayContext {
    pub trajectory: TrajectoryDocument,
    pub topology: DisplayTopology,
    pub allowed_display_ids: Vec<String>,
    pub target_kind: String,
    #[serde(default)]
    pub input_profile: Option<InputProfile>,
}

pub fn validate_replay(
    mapping: &DisplayMapping,
    context: &ReplayContext,
) -> Result<(), PlatformError> {
    context.topology.validate()?;
    context.trajectory.validate()?;

    let source_local = context
        .trajectory
        .source
        .as_ref()
        .is_some_and(RecordingSource::is_display_local);
    let target_local = matches!(context.topology, DisplayTopology::DisplayLocal(_));

    let Some(source) = &context.trajectory.source else {
        if source_local || target_local {
            return Err(PlatformError::invalid_argument(
                "跨坐标空间回放需要轨迹来源。",
            ));
        }
        if !matches!(mapping, DisplayMapping::Original {}) {
            return Err(PlatformError::invalid_argument("映射需要轨迹来源布局。"));
        }
        return Ok(());
    };

    let has_initial_pointer = match source {
        RecordingSource::Physical(s) => s.initial_pointer.is_some(),
        RecordingSource::Display(s) => s.initial_pointer.is_some(),
    };
    if !has_initial_pointer {
        return Err(PlatformError::invalid_argument("请在轨迹编辑器设置起点。"));
    }
    if source.scope() == RecordingScope::AllDisplays && context.target_kind != "desktop" {
        return Err(PlatformError::invalid_argument(
            "全显示屏轨迹需要桌面输入目标。",
        ));
    }

    let relative = context
        .trajectory
        .events
        .iter()
        .any(|e| matches!(e, TrajectoryEvent::PointerDelta { .. }));

    let source_ids: HashSet<&str> = match source {
        RecordingSource::Physical(s) => s.display_ids.iter().map(String::as_str).collect(),
        RecordingSource::Display(s) => s.display_ids.iter().map(String::as_str).collect(),
    };
    let target_ids: HashSet<&str> = match &context.topology {
        DisplayTopology::Physical(t) => t.displays.iter().map(|d| d.display_id.as_str()).collect(),
        DisplayTopology::DisplayLocal(t) => {
            t.displays.iter().map(|d| d.display_id.as_str()).collect()
        }
    };
    let allowed: HashSet<&str> = context
        .allowed_display_ids
        .iter()
        .map(String::as_str)
        .collect();

    let pairs: HashMap<&str, &str> = match mapping {
        DisplayMapping::Original {} => {
            if source_local != target_local {
                return Err(PlatformError::invalid_argument(
                    "原布局回放不能改变坐标空间。",
                ));
            }
            if source_local || target_local {
                if source.input_profile() != context.input_profile {
                    return Err(PlatformError::invalid_argument(
                        "原布局回放不能改变输入后端。",
                    ));
                }
            } else if let Some(profile) = source.input_profile()
                && Some(profile) != context.input_profile
            {
                return Err(PlatformError::invalid_argument(
                    "原布局回放需要相同输入后端与单位。",
                ));
            }
            match (source, &context.topology) {
                (RecordingSource::Display(src), DisplayTopology::DisplayLocal(dst)) => {
                    let dst_map: HashMap<&str, _> = dst
                        .displays
                        .iter()
                        .map(|d| (d.display_id.as_str(), d))
                        .collect();
                    for item in &src.topology.inner().displays {
                        if !source_ids.contains(item.display_id.as_str()) {
                            continue;
                        }
                        let Some(target) = dst_map.get(item.display_id.as_str()) else {
                            return Err(PlatformError::invalid_argument(
                                "来源与当前显示布局不同。",
                            ));
                        };
                        if item.pixel_size != target.pixel_size
                            || item.layout_bounds != target.layout_bounds
                            || item.rotation != target.rotation
                            || item.pixel_to_native != target.pixel_to_native
                        {
                            return Err(PlatformError::invalid_argument(
                                "来源与当前显示布局不同。",
                            ));
                        }
                    }
                }
                (RecordingSource::Physical(src), DisplayTopology::Physical(dst)) => {
                    let dst_map: HashMap<&str, _> = dst
                        .displays
                        .iter()
                        .map(|d| (d.display_id.as_str(), d))
                        .collect();
                    for item in &src.topology.inner().displays {
                        if !source_ids.contains(item.display_id.as_str()) {
                            continue;
                        }
                        let Some(target) = dst_map.get(item.display_id.as_str()) else {
                            return Err(PlatformError::invalid_argument(
                                "来源与当前显示布局不同。",
                            ));
                        };
                        if item.bounds != target.bounds
                            || item.rotation != target.rotation
                            || item.dpi_x != target.dpi_x
                            || item.dpi_y != target.dpi_y
                        {
                            return Err(PlatformError::invalid_argument(
                                "来源与当前显示布局不同。",
                            ));
                        }
                    }
                }
                _ => {
                    return Err(PlatformError::invalid_argument(
                        "原布局回放不能改变坐标空间。",
                    ));
                }
            }
            source_ids.iter().map(|&id| (id, id)).collect()
        }
        DisplayMapping::DisplayLocal { displays } => {
            if !(1..=64).contains(&displays.len()) {
                return Err(PlatformError::invalid_argument("显示屏映射数量超出范围。"));
            }
            let mut map = HashMap::with_capacity(displays.len());
            let mut seen_targets = HashSet::with_capacity(displays.len());
            for row in displays {
                if !valid_id(&row.source_display_id) || !valid_id(&row.target_display_id) {
                    return Err(PlatformError::invalid_argument("显示屏映射标识无效。"));
                }
                if map
                    .insert(
                        row.source_display_id.as_str(),
                        row.target_display_id.as_str(),
                    )
                    .is_some()
                {
                    return Err(PlatformError::invalid_argument("显示屏映射来源重复。"));
                }
                if !seen_targets.insert(row.target_display_id.as_str()) {
                    return Err(PlatformError::invalid_argument(
                        "多个来源不能映射到同一显示区域。",
                    ));
                }
            }
            if map.keys().copied().collect::<HashSet<_>>() != source_ids {
                return Err(PlatformError::invalid_argument("显示屏映射缺失或多余。"));
            }
            map
        }
    };

    if relative {
        if !matches!(mapping, DisplayMapping::Original {}) {
            return Err(PlatformError::invalid_argument(
                "相对输入不能按屏内像素平移。",
            ));
        }
        if source_local || target_local {
            let unit = source
                .input_profile()
                .map(|p| p.relative_unit)
                .unwrap_or(RelativeUnit::None);
            if source.input_profile() != context.input_profile || unit == RelativeUnit::None {
                return Err(PlatformError::invalid_argument(
                    "相对输入需要相同后端、单位与原布局。",
                ));
            }
            if !source
                .as_topology()
                .same_layout_ignoring_version(&context.topology)
            {
                return Err(PlatformError::invalid_argument(
                    "相对输入需要完整原显示拓扑。",
                ));
            }
        } else {
            if let Some(profile) = source.input_profile()
                && Some(profile) != context.input_profile
            {
                return Err(PlatformError::invalid_argument(
                    "相对输入回放需要相同输入后端与单位。",
                ));
            }
            if let (RecordingSource::Physical(src), DisplayTopology::Physical(dst)) =
                (source, &context.topology)
            {
                let dst_map: HashMap<&str, _> = dst
                    .displays
                    .iter()
                    .map(|d| (d.display_id.as_str(), d))
                    .collect();
                if src.topology.inner().displays.len() != dst_map.len()
                    || src.topology.inner().displays.iter().any(|item| {
                        dst_map.get(item.display_id.as_str()).is_none_or(|t| {
                            item.bounds != t.bounds
                                || item.rotation != t.rotation
                                || item.dpi_x != t.dpi_x
                                || item.dpi_y != t.dpi_y
                        })
                    })
                {
                    return Err(PlatformError::invalid_argument("相对轨迹需要原显示布局。"));
                }
            }
        }
    }

    for target_id in pairs.values() {
        if !allowed.contains(target_id) || !target_ids.contains(target_id) {
            return Err(PlatformError::invalid_argument(
                "目标显示屏不存在或未获输入授权。",
            ));
        }
    }

    // Validate every absolute point (events + initialPointer) maps cleanly to target display.
    let mut points: Vec<(Option<&str>, f64, f64)> = context
        .trajectory
        .events
        .iter()
        .filter_map(|event| match event {
            TrajectoryEvent::PointerMove {
                x, y, display_id, ..
            } => Some((display_id.as_deref(), *x, *y)),
            _ => None,
        })
        .collect();
    match source {
        RecordingSource::Display(s) => {
            if let Some(p) = &s.initial_pointer {
                points.push((Some(p.display_id.as_str()), p.x, p.y));
            }
        }
        RecordingSource::Physical(s) => {
            if let Some(p) = s.initial_pointer {
                points.push((None, p.x as f64, p.y as f64));
            }
        }
    }

    for (display_id, px, py) in points {
        let (src_id, src_rot, local_x, local_y) = match source {
            RecordingSource::Display(s) => {
                let id = display_id
                    .ok_or_else(|| PlatformError::invalid_argument("来源位置缺少显示身份。"))?;
                let item = s
                    .topology
                    .inner()
                    .displays
                    .iter()
                    .find(|d| d.display_id == id && source_ids.contains(id))
                    .ok_or_else(|| {
                        PlatformError::invalid_argument("来源位置不能唯一对应显示区域。")
                    })?;
                (item.display_id.as_str(), item.rotation, px, py)
            }
            RecordingSource::Physical(s) => {
                let matches: Vec<_> = s
                    .topology
                    .inner()
                    .displays
                    .iter()
                    .filter(|d| {
                        source_ids.contains(d.display_id.as_str()) && d.bounds.contains_f64(px, py)
                    })
                    .collect();
                if matches.len() != 1 {
                    return Err(PlatformError::invalid_argument(
                        "来源位置不能唯一对应显示区域。",
                    ));
                }
                let item = matches[0];
                (
                    item.display_id.as_str(),
                    item.rotation,
                    px - item.bounds.x as f64,
                    py - item.bounds.y as f64,
                )
            }
        };

        let target_id = pairs[src_id];
        match &context.topology {
            DisplayTopology::DisplayLocal(dst) => {
                let target = dst
                    .displays
                    .iter()
                    .find(|d| d.display_id == target_id)
                    .ok_or_else(|| PlatformError::invalid_argument("目标显示屏不存在。"))?;
                if src_rot != target.rotation || !target.contains_pixel(local_x, local_y) {
                    return Err(PlatformError::invalid_argument(
                        "屏内映射方向不同或实际位置越界。",
                    ));
                }
                if target.pixel_to_native.is_none() {
                    return Err(PlatformError::invalid_argument(
                        "目标缺少原生输入坐标映射。",
                    ));
                }
            }
            DisplayTopology::Physical(dst) => {
                let target = dst
                    .displays
                    .iter()
                    .find(|d| d.display_id == target_id)
                    .ok_or_else(|| PlatformError::invalid_argument("目标显示屏不存在。"))?;
                if src_rot != target.rotation
                    || !(0.0..target.bounds.width as f64).contains(&local_x)
                    || !(0.0..target.bounds.height as f64).contains(&local_y)
                {
                    return Err(PlatformError::invalid_argument(
                        "映射屏幕方向不同或实际轨迹位置超出映射目标。",
                    ));
                }
            }
        }
    }

    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RecordStartRequest {
    pub target_token: String,
    pub coordinate_mode: CoordinateMode,
    pub include_pointer_motion: bool,
    pub expected_topology_version: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RecordStartContext {
    pub target_token: String,
    pub target_kind: String,
    pub topology: DisplayTopology,
    pub permissions: Vec<String>,
    #[serde(default)]
    pub capabilities: Option<CapabilitySnapshot>,
}

pub fn validate_record_start(
    request: &RecordStartRequest,
    context: &RecordStartContext,
) -> Result<(), PlatformError> {
    if request.expected_topology_version > MAX_SAFE_INT || !valid_id(&request.target_token) {
        return Err(PlatformError::invalid_argument("录制启动参数无效。"));
    }
    context.topology.validate()?;
    if request.target_token != context.target_token
        || !context.permissions.iter().any(|p| p == "record")
    {
        return Err(PlatformError::permission_denied("目标未授予此录制用途。"));
    }
    if request.expected_topology_version != context.topology.version() {
        return Err(PlatformError::invalid_argument("显示拓扑已变化。"));
    }
    if !matches!(
        context.target_kind.as_str(),
        "desktop" | "display" | "window"
    ) {
        return Err(PlatformError::invalid_argument("此目标不支持录制。"));
    }
    let is_local_topology = matches!(context.topology, DisplayTopology::DisplayLocal(_));
    if is_local_topology && request.coordinate_mode == CoordinateMode::Physical {
        return Err(PlatformError::invalid_argument(
            "当前拓扑需要屏内像素坐标。",
        ));
    }
    if !is_local_topology && request.coordinate_mode == CoordinateMode::Display {
        return Err(PlatformError::invalid_argument(
            "全局物理拓扑不能使用屏内像素坐标模式。",
        ));
    }
    if let Some(capabilities) = &context.capabilities {
        capabilities.validate()?;
        let pointer_ok = if request.coordinate_mode == CoordinateMode::Relative {
            capabilities.features.record_pointer_relative.state == FeatureState::Available
        } else {
            capabilities.features.record_pointer_absolute.state == FeatureState::Available
        };
        let keyboard_ok = capabilities.features.record_keyboard.state == FeatureState::Available;
        let desktop_ok = context.target_kind != "desktop"
            || capabilities.features.record_all_displays.state == FeatureState::Available;
        if !pointer_ok || !keyboard_ok || !desktop_ok {
            return Err(PlatformError::permission_denied(
                "当前会话缺少此范围的录制能力。",
            ));
        }
        let requires_v11 = is_local_topology || request.coordinate_mode == CoordinateMode::Display;
        if requires_v11
            && !capabilities
                .trajectory_versions
                .contains(&TrajectoryVersion::V1_1)
        {
            return Err(PlatformError::schema_unsupported(
                "当前后端未协商屏内坐标轨迹版本。",
            ));
        }
    }
    Ok(())
}
