use std::collections::HashSet;
use std::fmt;

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};

use crate::capabilities::{BackendKind, MAX_SAFE_INT, deserialize_required_nullable};
use crate::error::PlatformError;

pub const MIN_COORD_I64: i64 = -2_147_483_648;
pub const MAX_COORD_I64: i64 = 2_147_483_647;
pub const MIN_COORD_F64: f64 = -2_147_483_648.0;
pub const MAX_COORD_F64: f64 = 2_147_483_647.0;
pub const MIN_LAYOUT_EXTENT: f64 = 1e-6;
pub const MAX_MATRIX_COMPONENT: f64 = 1_000_000_000_000.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Rotation {
    Deg0,
    Deg90,
    Deg180,
    Deg270,
}

impl Rotation {
    pub const fn degrees(self) -> u16 {
        match self {
            Self::Deg0 => 0,
            Self::Deg90 => 90,
            Self::Deg180 => 180,
            Self::Deg270 => 270,
        }
    }
}

impl Serialize for Rotation {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_u16(self.degrees())
    }
}

impl<'de> Deserialize<'de> for Rotation {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct RotationVisitor;

        impl de::Visitor<'_> for RotationVisitor {
            type Value = Rotation;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("rotation integer 0, 90, 180, or 270")
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                match value {
                    0 => Ok(Rotation::Deg0),
                    90 => Ok(Rotation::Deg90),
                    180 => Ok(Rotation::Deg180),
                    270 => Ok(Rotation::Deg270),
                    other => Err(E::custom(format!("invalid rotation: {other}"))),
                }
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                match value {
                    0 => Ok(Rotation::Deg0),
                    90 => Ok(Rotation::Deg90),
                    180 => Ok(Rotation::Deg180),
                    270 => Ok(Rotation::Deg270),
                    other => Err(E::custom(format!("invalid rotation: {other}"))),
                }
            }
        }

        deserializer.deserialize_any(RotationVisitor)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum NativeSpace {
    #[serde(rename = "windowsPhysicalPixels")]
    WindowsPhysicalPixels,
    #[serde(rename = "quartzPoints")]
    QuartzPoints,
    #[serde(rename = "x11RootPixels")]
    X11RootPixels,
    #[serde(rename = "eiRegions")]
    EiRegions,
}

impl NativeSpace {
    pub const fn for_backend(backend: BackendKind) -> Self {
        match backend {
            BackendKind::WindowsWin32 => Self::WindowsPhysicalPixels,
            BackendKind::MacosQuartz => Self::QuartzPoints,
            BackendKind::LinuxX11 => Self::X11RootPixels,
            BackendKind::LinuxWaylandPortal | BackendKind::LinuxWaylandCompositor => {
                Self::EiRegions
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum RelativeUnit {
    #[serde(rename = "deviceCounts")]
    DeviceCounts,
    #[serde(rename = "nativePoints")]
    NativePoints,
    #[serde(rename = "x11Valuators")]
    X11Valuators,
    #[serde(rename = "libeiUnits")]
    LibeiUnits,
    #[serde(rename = "none")]
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InputProfile {
    pub backend: BackendKind,
    pub relative_unit: RelativeUnit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhysicalPoint {
    pub x: i64,
    pub y: i64,
}

impl PhysicalPoint {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !(MIN_COORD_I64..=MAX_COORD_I64).contains(&self.x)
            || !(MIN_COORD_I64..=MAX_COORD_I64).contains(&self.y)
        {
            return Err(PlatformError::invalid_argument("物理坐标超出范围。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhysicalRect {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

impl PhysicalRect {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !(MIN_COORD_I64..=MAX_COORD_I64).contains(&self.x)
            || !(MIN_COORD_I64..=MAX_COORD_I64).contains(&self.y)
            || !(1..=MAX_COORD_I64).contains(&self.width)
            || !(1..=MAX_COORD_I64).contains(&self.height)
        {
            return Err(PlatformError::invalid_argument("物理矩形超出范围。"));
        }
        Ok(())
    }

    pub fn contains_f64(&self, x: f64, y: f64) -> bool {
        let left = self.x as f64;
        let top = self.y as f64;
        let right = (self.x + self.width) as f64;
        let bottom = (self.y + self.height) as f64;
        x >= left && x < right && y >= top && y < bottom
    }

    pub fn contains_point(&self, point: PhysicalPoint) -> bool {
        point.x >= self.x
            && point.x < self.x + self.width
            && point.y >= self.y
            && point.y < self.y + self.height
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PixelSize {
    pub width: i64,
    pub height: i64,
}

impl PixelSize {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !(1..=MAX_COORD_I64).contains(&self.width) || !(1..=MAX_COORD_I64).contains(&self.height)
        {
            return Err(PlatformError::invalid_argument("像素尺寸超出范围。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LayoutRect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl LayoutRect {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !self.x.is_finite()
            || !self.y.is_finite()
            || !self.width.is_finite()
            || !self.height.is_finite()
            || !(MIN_COORD_F64..=MAX_COORD_F64).contains(&self.x)
            || !(MIN_COORD_F64..=MAX_COORD_F64).contains(&self.y)
            || !(MIN_LAYOUT_EXTENT..=MAX_COORD_F64).contains(&self.width)
            || !(MIN_LAYOUT_EXTENT..=MAX_COORD_F64).contains(&self.height)
        {
            return Err(PlatformError::invalid_argument("布局矩形超出范围。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DisplayPoint {
    pub display_id: String,
    pub x: f64,
    pub y: f64,
}

impl DisplayPoint {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.display_id)
            || !self.x.is_finite()
            || !self.y.is_finite()
            || !(0.0..=MAX_COORD_F64).contains(&self.x)
            || !(0.0..=MAX_COORD_F64).contains(&self.y)
        {
            return Err(PlatformError::invalid_argument("屏内坐标超出范围。"));
        }
        Ok(())
    }
}

/// 2D affine matrix `[a, b, c, d, tx, ty]`:
/// `nativeX = a * x + c * y + tx`, `nativeY = b * x + d * y + ty`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct AffineTransform2D(pub [f64; 6]);

impl AffineTransform2D {
    pub fn determinant(&self) -> f64 {
        let [a, b, c, d, _, _] = self.0;
        a * d - b * c
    }

    pub fn validate(&self) -> Result<(), PlatformError> {
        for component in self.0 {
            if !component.is_finite()
                || !(-MAX_MATRIX_COMPONENT..=MAX_MATRIX_COMPONENT).contains(&component)
            {
                return Err(PlatformError::invalid_argument(
                    "像素到原生坐标变换分量无效。",
                ));
            }
        }
        let det = self.determinant();
        if !det.is_finite() || det == 0.0 {
            return Err(PlatformError::invalid_argument(
                "像素到原生坐标的变换不可逆。",
            ));
        }
        Ok(())
    }

    pub fn pixel_to_native(&self, x: f64, y: f64) -> Result<(f64, f64), PlatformError> {
        self.validate()?;
        if !x.is_finite() || !y.is_finite() {
            return Err(PlatformError::invalid_argument("坐标必须为有限数值。"));
        }
        let [a, b, c, d, tx, ty] = self.0;
        let nx = a * x + c * y + tx;
        let ny = b * x + d * y + ty;
        if !nx.is_finite() || !ny.is_finite() {
            return Err(PlatformError::invalid_argument("原生坐标变换溢出。"));
        }
        Ok((nx, ny))
    }

    pub fn native_to_pixel(&self, nx: f64, ny: f64) -> Result<(f64, f64), PlatformError> {
        self.validate()?;
        if !nx.is_finite() || !ny.is_finite() {
            return Err(PlatformError::invalid_argument("坐标必须为有限数值。"));
        }
        let [a, b, c, d, tx, ty] = self.0;
        let det = a * d - b * c;
        let dx = nx - tx;
        let dy = ny - ty;
        let x = (d * dx - c * dy) / det;
        let y = (-b * dx + a * dy) / det;
        if !x.is_finite() || !y.is_finite() {
            return Err(PlatformError::invalid_argument("逆向像素坐标变换溢出。"));
        }
        Ok((x, y))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LocalTopologyCoverage {
    All,
    Selected,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PhysicalDisplay {
    pub display_id: String,
    pub name: String,
    pub output_names: Vec<String>,
    pub bounds: PhysicalRect,
    pub rotation: Rotation,
    pub dpi_x: f64,
    pub dpi_y: f64,
    pub primary: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PhysicalTopology {
    pub version: u64,
    pub virtual_bounds: PhysicalRect,
    pub displays: Vec<PhysicalDisplay>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LocalDisplay {
    pub display_id: String,
    pub name: String,
    pub output_names: Vec<String>,
    pub pixel_size: PixelSize,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub layout_bounds: Option<LayoutRect>,
    pub rotation: Rotation,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub primary: Option<bool>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub pixel_to_native: Option<AffineTransform2D>,
}

impl LocalDisplay {
    pub fn contains_pixel(&self, x: f64, y: f64) -> bool {
        x.is_finite()
            && y.is_finite()
            && x >= 0.0
            && x < self.pixel_size.width as f64
            && y >= 0.0
            && y < self.pixel_size.height as f64
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DisplayLocalTopology {
    pub version: u64,
    pub native_space: NativeSpace,
    pub coverage: LocalTopologyCoverage,
    pub displays: Vec<LocalDisplay>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "coordinateSpace")]
pub enum DisplayTopology {
    #[serde(rename = "desktopPhysicalPixels")]
    Physical(PhysicalTopology),
    #[serde(rename = "displayLocalPixels")]
    DisplayLocal(DisplayLocalTopology),
}

pub(crate) fn valid_id(value: &str) -> bool {
    let chars = value.chars().count();
    (1..=256).contains(&chars)
}

fn validate_output_names(names: &[String]) -> Result<(), PlatformError> {
    if !(1..=16).contains(&names.len()) {
        return Err(PlatformError::invalid_argument(
            "outputNames 数量应为 1～16。",
        ));
    }
    let mut seen = HashSet::with_capacity(names.len());
    for name in names {
        if !valid_id(name) || !seen.insert(name.as_str()) {
            return Err(PlatformError::invalid_argument(
                "outputNames 存在空值、超长或重复项。",
            ));
        }
    }
    Ok(())
}

impl PhysicalTopology {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if self.version > MAX_SAFE_INT {
            return Err(PlatformError::invalid_argument(
                "拓扑版本超出安全整数范围。",
            ));
        }
        self.virtual_bounds.validate()?;
        if !(1..=64).contains(&self.displays.len()) {
            return Err(PlatformError::invalid_argument("显示区域数量应为 1～64。"));
        }
        let mut ids = HashSet::with_capacity(self.displays.len());
        let mut rects = HashSet::with_capacity(self.displays.len());
        let mut primaries = 0_usize;
        let mut left = i64::MAX;
        let mut top = i64::MAX;
        let mut right = i64::MIN;
        let mut bottom = i64::MIN;

        for display in &self.displays {
            if !valid_id(&display.display_id) || !valid_id(&display.name) {
                return Err(PlatformError::invalid_argument("显示屏标识或名称无效。"));
            }
            if !ids.insert(display.display_id.as_str()) {
                return Err(PlatformError::invalid_argument("显示屏身份重复。"));
            }
            validate_output_names(&display.output_names)?;
            display.bounds.validate()?;
            if !display.dpi_x.is_finite()
                || !display.dpi_y.is_finite()
                || !(48.0..=768.0).contains(&display.dpi_x)
                || !(48.0..=768.0).contains(&display.dpi_y)
            {
                return Err(PlatformError::invalid_argument("DPI 数值超出允许范围。"));
            }
            if display.primary {
                primaries += 1;
            }
            if !rects.insert(display.bounds) {
                return Err(PlatformError::invalid_argument(
                    "镜像输出须合并到同一逻辑显示区域。",
                ));
            }
            let r = display.bounds.x + display.bounds.width;
            let b = display.bounds.y + display.bounds.height;
            left = left.min(display.bounds.x);
            top = top.min(display.bounds.y);
            right = right.max(r);
            bottom = bottom.max(b);
        }

        if primaries != 1 {
            return Err(PlatformError::invalid_argument("显示拓扑需要一个主屏。"));
        }
        if right > 2_147_483_648
            || bottom > 2_147_483_648
            || right - left > MAX_COORD_I64
            || bottom - top > MAX_COORD_I64
        {
            return Err(PlatformError::invalid_argument("显示拓扑超出坐标范围。"));
        }
        let expected = PhysicalRect {
            x: left,
            y: top,
            width: right - left,
            height: bottom - top,
        };
        if self.virtual_bounds != expected {
            return Err(PlatformError::invalid_argument(
                "虚拟桌面范围与显示区域不一致。",
            ));
        }
        Ok(())
    }
}

impl DisplayLocalTopology {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if self.version > MAX_SAFE_INT {
            return Err(PlatformError::invalid_argument(
                "拓扑版本超出安全整数范围。",
            ));
        }
        if !(1..=64).contains(&self.displays.len()) {
            return Err(PlatformError::invalid_argument("显示区域数量应为 1～64。"));
        }
        let mut ids = HashSet::with_capacity(self.displays.len());
        let mut primaries = 0_usize;
        for display in &self.displays {
            if !valid_id(&display.display_id) || !valid_id(&display.name) {
                return Err(PlatformError::invalid_argument("显示屏标识或名称无效。"));
            }
            if !ids.insert(display.display_id.as_str()) {
                return Err(PlatformError::invalid_argument("显示屏身份重复。"));
            }
            validate_output_names(&display.output_names)?;
            display.pixel_size.validate()?;
            if let Some(bounds) = display.layout_bounds {
                bounds.validate()?;
            }
            if display.primary == Some(true) {
                primaries += 1;
            }
            if let Some(matrix) = display.pixel_to_native {
                matrix.validate()?;
            }
        }
        if primaries > 1 {
            return Err(PlatformError::invalid_argument("主显示区域重复。"));
        }
        Ok(())
    }
}

impl DisplayTopology {
    pub fn version(&self) -> u64 {
        match self {
            Self::Physical(t) => t.version,
            Self::DisplayLocal(t) => t.version,
        }
    }

    pub fn validate(&self) -> Result<(), PlatformError> {
        match self {
            Self::Physical(t) => t.validate(),
            Self::DisplayLocal(t) => t.validate(),
        }
    }

    pub fn same_layout_ignoring_version(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Physical(a), Self::Physical(b)) => {
                a.virtual_bounds == b.virtual_bounds && a.displays == b.displays
            }
            (Self::DisplayLocal(a), Self::DisplayLocal(b)) => {
                a.native_space == b.native_space
                    && a.coverage == b.coverage
                    && a.displays == b.displays
            }
            _ => false,
        }
    }
}
