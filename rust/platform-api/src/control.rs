use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::capabilities::{
    MAX_SAFE_INT, deserialize_optional_non_null, deserialize_required_nullable,
};
use crate::error::PlatformError;
use crate::jcs::jcs_sha256_hex;
use crate::ports::{CleanupOutcome, HeldInput, NativeInputAction, TargetKind};
use crate::topology::{
    DisplayLocalTopology, DisplayPoint, DisplayTopology, LocalDisplay, MAX_COORD_F64,
    MAX_COORD_I64, MIN_COORD_I64, PhysicalPoint, PhysicalTopology, Rotation, valid_id,
};
use crate::trajectory::{MAX_AT_US, PointerButton};

pub const MAX_SEGMENT_EVENTS: u64 = 1_000_000;
pub const DEFAULT_PRESS_DURATION_MS: u64 = 50;
pub const MAX_PRESS_DURATION_MS: u64 = 10_000;
pub const SMOOTH_STEP_INTERVAL_MS: u64 = 8;
pub const MIN_SMOOTH_STEP_INTERVAL_MS: u64 = 4;
const SMOOTH_STEP_TARGET_PX: f64 = 8.0;
pub const MAX_SMOOTH_STEPS: u64 = 2_000;

fn is_lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn valid_node_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    let Some(first) = bytes.next() else {
        return false;
    };
    if !first.is_ascii_alphabetic() || value.len() > 64 {
        return false;
    }
    bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

fn is_leap_year(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

fn days_in_month(year: i64, month: u32) -> Option<u32> {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => Some(31),
        4 | 6 | 9 | 11 => Some(30),
        2 => Some(if is_leap_year(year) { 29 } else { 28 }),
        _ => None,
    }
}

fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let y = year - i64::from(month <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u32;
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + i64::from(doe) - 719_468
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = i64::from(yoe) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (y + i64::from(m <= 2), m, d)
}

/// Parses a strict `YYYY-MM-DDTHH:MM:SSZ` UTC timestamp into Unix epoch seconds.
pub fn parse_utc_timestamp(value: &str) -> Result<i64, PlatformError> {
    let b = value.as_bytes();
    if b.len() != 20
        || b[4] != b'-'
        || b[7] != b'-'
        || b[10] != b'T'
        || b[13] != b':'
        || b[16] != b':'
        || b[19] != b'Z'
        || ![0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18]
            .into_iter()
            .all(|idx| b[idx].is_ascii_digit())
    {
        return Err(PlatformError::invalid_argument("UTC 时间格式无效。"));
    }
    let year: i64 = value[0..4]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 年份无效。"))?;
    let month: u32 = value[5..7]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 月份无效。"))?;
    let day: u32 = value[8..10]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 日期无效。"))?;
    let hour: u32 = value[11..13]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 小时无效。"))?;
    let minute: u32 = value[14..16]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 分钟无效。"))?;
    let second: u32 = value[17..19]
        .parse()
        .map_err(|_| PlatformError::invalid_argument("UTC 秒数无效。"))?;

    let Some(max_day) = days_in_month(year, month) else {
        return Err(PlatformError::invalid_argument("UTC 月份超出范围。"));
    };
    if day == 0 || day > max_day || hour > 23 || minute > 59 || second > 59 {
        return Err(PlatformError::invalid_argument("UTC 日历日期无效。"));
    }
    let days = days_from_civil(year, month, day);
    let secs = days * 86_400 + i64::from(hour) * 3_600 + i64::from(minute) * 60 + i64::from(second);
    Ok(secs)
}

/// Formats Unix epoch seconds into `YYYY-MM-DDTHH:MM:SSZ`.
pub fn format_utc_timestamp(unix_seconds: i64) -> String {
    let days = unix_seconds.div_euclid(86_400);
    let sod = unix_seconds.rem_euclid(86_400) as u32;
    let (year, month, day) = civil_from_days(days);
    let hour = sod / 3_600;
    let minute = (sod % 3_600) / 60;
    let second = sod % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum InterruptPolicy {
    #[default]
    Pause,
    Ignore,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LeaseReleaseReason {
    Finished,
    Cancelled,
    Yielded,
    Fault,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LeaseState {
    Active,
    Pausing,
    Paused,
    Released,
    Uncertain,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SegmentState {
    Accepted,
    Running,
    Sent,
    Rejected,
    Uncertain,
    Paused,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TargetState {
    pub target_token: String,
    pub target_version: String,
    pub topology_version: String,
}

impl TargetState {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.target_token)
            || !valid_id(&self.target_version)
            || !valid_id(&self.topology_version)
        {
            return Err(PlatformError::invalid_argument("目标状态标识无效。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SegmentCursor {
    pub segment_id: String,
    pub payload_hash: String,
    pub event_count: u64,
    pub next_event_index: u64,
    pub active_elapsed_us: u64,
    pub remaining_delay_us: u64,
}

impl SegmentCursor {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.segment_id) || !is_lower_hex_64(&self.payload_hash) {
            return Err(PlatformError::invalid_argument("暂停游标身份或摘要无效。"));
        }
        if self.event_count > MAX_SEGMENT_EVENTS || self.next_event_index > MAX_SEGMENT_EVENTS {
            return Err(PlatformError::invalid_argument("暂停游标事件数量超限。"));
        }
        if self.next_event_index > self.event_count {
            return Err(PlatformError::invalid_argument("暂停游标超过源事件总数。"));
        }
        if self.active_elapsed_us > MAX_AT_US || self.remaining_delay_us > MAX_AT_US {
            return Err(PlatformError::invalid_argument("暂停游标计时超出范围。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum PausePoint {
    Physical(PhysicalPoint),
    Display(DisplayPoint),
}

impl PausePoint {
    pub fn validate(&self) -> Result<(), PlatformError> {
        match self {
            Self::Physical(pt) => pt.validate(),
            Self::Display(pt) => pt.validate(),
        }
    }
}

/// Canonical sorting and deduplication check for `heldInputs`.
pub fn sort_held_inputs(inputs: &mut [HeldInput]) {
    inputs.sort_by(|left, right| left.sort_key().cmp(&right.sort_key()));
}

/// Canonical sorting for `targets` by `targetToken`.
pub fn sort_target_states(targets: &mut [TargetState]) {
    targets.sort_by(|left, right| left.target_token.cmp(&right.target_token));
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct PauseSnapshot {
    pub pause_id: String,
    pub lease_id: String,
    pub agent_epoch: String,
    pub generation: u64,
    pub created_at: String,
    pub expires_at: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub cursor: Option<SegmentCursor>,
    pub held_inputs: Vec<HeldInput>,
    pub pointer: PausePoint,
    pub targets: Vec<TargetState>,
    pub checkpoint_hash: String,
    pub held_inputs_hash: String,
    pub target_state_hash: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub input_target_token: Option<String>,
}

impl PauseSnapshot {
    #[allow(clippy::too_many_arguments)]
    pub fn build(
        pause_id: String,
        lease_id: String,
        agent_epoch: String,
        generation: u64,
        created_at: String,
        expires_at: String,
        cursor: Option<SegmentCursor>,
        mut held_inputs: Vec<HeldInput>,
        pointer: PausePoint,
        mut targets: Vec<TargetState>,
        input_target_token: Option<String>,
    ) -> Result<Self, PlatformError> {
        sort_held_inputs(&mut held_inputs);
        sort_target_states(&mut targets);
        let held_inputs_hash = jcs_sha256_hex(&held_inputs)?;
        let target_state_hash = jcs_sha256_hex(&targets)?;
        let checkpoint_payload = json!({
            "pauseId": pause_id,
            "leaseId": lease_id,
            "agentEpoch": agent_epoch,
            "generation": generation,
            "cursor": cursor,
            "heldInputsHash": held_inputs_hash,
            "pointer": pointer,
            "targetStateHash": target_state_hash,
            "inputTargetToken": input_target_token,
        });
        let checkpoint_hash = jcs_sha256_hex(&checkpoint_payload)?;
        let snapshot = Self {
            pause_id,
            lease_id,
            agent_epoch,
            generation,
            created_at,
            expires_at,
            cursor,
            held_inputs,
            pointer,
            targets,
            checkpoint_hash,
            held_inputs_hash,
            target_state_hash,
            input_target_token,
        };
        snapshot.validate()?;
        Ok(snapshot)
    }

    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.pause_id)
            || !valid_id(&self.lease_id)
            || !valid_id(&self.agent_epoch)
            || self.generation > MAX_SAFE_INT
        {
            return Err(PlatformError::invalid_argument(
                "暂停检查点身份或代次无效。",
            ));
        }
        let created = parse_utc_timestamp(&self.created_at)?;
        let expires = parse_utc_timestamp(&self.expires_at)?;
        if created >= expires {
            return Err(PlatformError::invalid_argument(
                "暂停期限没有覆盖创建时间。",
            ));
        }
        if let Some(ref cursor) = self.cursor {
            cursor.validate()?;
        }
        if self.held_inputs.len() > 256 {
            return Err(PlatformError::invalid_argument("暂停持有集合数量超限。"));
        }
        let mut seen_held = HashSet::with_capacity(self.held_inputs.len());
        for (idx, item) in self.held_inputs.iter().enumerate() {
            item.validate()?;
            if !seen_held.insert(item) {
                return Err(PlatformError::invalid_argument("暂停持有集合存在重复项。"));
            }
            if idx > 0 && self.held_inputs[idx - 1].sort_key() > item.sort_key() {
                return Err(PlatformError::invalid_argument("暂停持有集合顺序不稳定。"));
            }
        }
        self.pointer.validate()?;
        if !(1..=32).contains(&self.targets.len()) {
            return Err(PlatformError::invalid_argument("暂停目标数量应为 1～32。"));
        }
        let mut seen_targets = HashSet::with_capacity(self.targets.len());
        for (idx, target) in self.targets.iter().enumerate() {
            target.validate()?;
            if !seen_targets.insert(target.target_token.as_str()) {
                return Err(PlatformError::invalid_argument("暂停目标重复。"));
            }
            if idx > 0 && self.targets[idx - 1].target_token > target.target_token {
                return Err(PlatformError::invalid_argument("暂停目标顺序不稳定。"));
            }
        }
        if !is_lower_hex_64(&self.held_inputs_hash)
            || !is_lower_hex_64(&self.target_state_hash)
            || !is_lower_hex_64(&self.checkpoint_hash)
        {
            return Err(PlatformError::invalid_argument("暂停检查点摘要格式无效。"));
        }
        if jcs_sha256_hex(&self.held_inputs)? != self.held_inputs_hash
            || jcs_sha256_hex(&self.targets)? != self.target_state_hash
        {
            return Err(PlatformError::invalid_argument("暂停集合摘要不符。"));
        }
        if let Some(ref input_target) = self.input_target_token {
            if !valid_id(input_target) || !seen_targets.contains(input_target.as_str()) {
                return Err(PlatformError::invalid_argument(
                    "暂停输入目标不在授权目标集合。",
                ));
            }
        } else if self.cursor.is_some() || !self.held_inputs.is_empty() {
            return Err(PlatformError::invalid_argument("暂停输入缺少唯一目标。"));
        }
        let checkpoint_payload = json!({
            "pauseId": self.pause_id,
            "leaseId": self.lease_id,
            "agentEpoch": self.agent_epoch,
            "generation": self.generation,
            "cursor": self.cursor,
            "heldInputsHash": self.held_inputs_hash,
            "pointer": self.pointer,
            "targetStateHash": self.target_state_hash,
            "inputTargetToken": self.input_target_token,
        });
        if jcs_sha256_hex(&checkpoint_payload)? != self.checkpoint_hash {
            return Err(PlatformError::invalid_argument("暂停检查点摘要不符。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LeaseSnapshot {
    pub lease_id: String,
    pub generation: u64,
    pub agent_epoch: String,
    pub state: LeaseState,
    pub cleanup: CleanupOutcome,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub pause_snapshot: Option<PauseSnapshot>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub last_resume_attempt_id: Option<String>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub reason: Option<String>,
}

impl LeaseSnapshot {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.lease_id)
            || !valid_id(&self.agent_epoch)
            || self.generation > MAX_SAFE_INT
        {
            return Err(PlatformError::invalid_argument("租约快照身份或代次无效。"));
        }
        if let Some(ref attempt_id) = self.last_resume_attempt_id
            && !valid_id(attempt_id)
        {
            return Err(PlatformError::invalid_argument("恢复尝试标识无效。"));
        }
        if let Some(ref reason) = self.reason {
            let chars = reason.chars().count();
            if !(1..=500).contains(&chars) || reason.bytes().any(|b| b == 0) {
                return Err(PlatformError::invalid_argument("租约原因说明长度无效。"));
            }
        }
        if self.state == LeaseState::Paused {
            let Some(ref pause) = self.pause_snapshot else {
                return Err(PlatformError::invalid_argument(
                    "暂停状态必须携带暂停检查点。",
                ));
            };
            if !matches!(
                self.cleanup,
                CleanupOutcome::Succeeded | CleanupOutcome::NotRequired
            ) {
                return Err(PlatformError::invalid_argument(
                    "暂停状态的输入清理必须已完成或无须清理。",
                ));
            }
            pause.validate()?;
            if pause.lease_id != self.lease_id
                || pause.agent_epoch != self.agent_epoch
                || pause.generation != self.generation
            {
                return Err(PlatformError::invalid_argument(
                    "租约与暂停检查点身份不符。",
                ));
            }
        } else if self.pause_snapshot.is_some() {
            return Err(PlatformError::invalid_argument(
                "非暂停状态不得携带暂停检查点。",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ResumePlan {
    pub plan_id: String,
    pub run_id: String,
    pub control_version: u64,
    pub pause_id: String,
    pub agent_epoch: String,
    pub generation: u64,
    pub expires_at: String,
    pub held_inputs: Vec<HeldInput>,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub pointer_to_restore: Option<PausePoint>,
    pub requires_confirmation: bool,
    pub plan_hash: String,
    #[serde(deserialize_with = "deserialize_required_nullable")]
    pub restore_target_token: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResumeContext {
    pub run_id: String,
    pub control_version: u64,
    pub pause_snapshot: PauseSnapshot,
    pub prepared_at: String,
    pub now: String,
    pub current_agent_epoch: String,
    pub current_generation: u64,
    pub current_targets: Vec<TargetState>,
    pub plan_state: String,
}

impl ResumePlan {
    pub fn validate(&self, context: &ResumeContext) -> Result<(), PlatformError> {
        if !valid_id(&self.plan_id)
            || !valid_id(&self.run_id)
            || !valid_id(&self.pause_id)
            || !valid_id(&self.agent_epoch)
            || self.control_version > MAX_SAFE_INT
            || self.generation > MAX_SAFE_INT
            || !is_lower_hex_64(&self.plan_hash)
        {
            return Err(PlatformError::invalid_argument("继续计划字段无效。"));
        }
        let pause = &context.pause_snapshot;
        pause.validate()?;
        if self.pause_id != pause.pause_id
            || self.agent_epoch != pause.agent_epoch
            || self.generation != pause.generation
        {
            return Err(PlatformError::invalid_argument(
                "继续计划与实时检查点不符。",
            ));
        }
        if self.run_id != context.run_id || self.control_version != context.control_version {
            return Err(PlatformError::invalid_argument(
                "继续计划与运行控制版本不符。",
            ));
        }
        if self.held_inputs != pause.held_inputs {
            return Err(PlatformError::invalid_argument("继续计划持有集合改变。"));
        }
        if let Some(ref ptr) = self.pointer_to_restore {
            ptr.validate()?;
            if *ptr != pause.pointer {
                return Err(PlatformError::invalid_argument("继续指针目的地不符。"));
            }
        }
        let target_tokens: HashSet<&str> = pause
            .targets
            .iter()
            .map(|t| t.target_token.as_str())
            .collect();
        if let Some(ref restore_tok) = self.restore_target_token {
            if Some(restore_tok.as_str()) != pause.input_target_token.as_deref()
                || !target_tokens.contains(restore_tok.as_str())
            {
                return Err(PlatformError::invalid_argument(
                    "继续恢复目标没有绑定暂停输入目标。",
                ));
            }
        } else if !self.held_inputs.is_empty() || self.pointer_to_restore.is_some() {
            return Err(PlatformError::invalid_argument("继续恢复输入缺少目标。"));
        }
        let needs_confirm = !self.held_inputs.is_empty()
            || self.pointer_to_restore.is_some()
            || self.restore_target_token.is_some();
        if self.requires_confirmation != needs_confirm {
            return Err(PlatformError::invalid_argument("继续确认要求不符。"));
        }
        let expires = parse_utc_timestamp(&self.expires_at)?;
        let prepared = parse_utc_timestamp(&context.prepared_at)?;
        let pause_expires = parse_utc_timestamp(&pause.expires_at)?;
        let now = parse_utc_timestamp(&context.now)?;
        if !(prepared < expires && expires <= pause_expires) || (expires - prepared) > 60 {
            return Err(PlatformError::invalid_argument("继续计划有效期越界。"));
        }
        if now >= expires {
            return Err(PlatformError::invalid_argument("继续计划已过期。"));
        }
        if context.current_agent_epoch != pause.agent_epoch
            || context.current_generation != pause.generation
        {
            return Err(PlatformError::invalid_argument("原生代次已经改变。"));
        }
        if context.current_targets != pause.targets {
            return Err(PlatformError::invalid_argument("目标或拓扑已经改变。"));
        }
        if context.plan_state != "prepared" {
            return Err(PlatformError::invalid_argument("继续计划已经消耗或取消。"));
        }
        let plan_payload = json!({
            "runId": self.run_id,
            "controlVersion": self.control_version,
            "pauseId": self.pause_id,
            "agentEpoch": self.agent_epoch,
            "generation": self.generation,
            "checkpointHash": pause.checkpoint_hash,
            "heldInputs": self.held_inputs,
            "pointerToRestore": self.pointer_to_restore,
            "restoreTargetToken": self.restore_target_token,
            "expiresAt": self.expires_at,
        });
        if jcs_sha256_hex(&plan_payload)? != self.plan_hash {
            return Err(PlatformError::invalid_argument("继续计划摘要不符。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LeaseAcquireResult {
    pub lease_id: String,
    pub run_id: String,
    pub generation: u64,
    pub agent_epoch: String,
    pub expires_in_ms: u64,
}

impl LeaseAcquireResult {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.lease_id)
            || !valid_id(&self.run_id)
            || !valid_id(&self.agent_epoch)
            || self.generation > MAX_SAFE_INT
            || !(1..=10_000).contains(&self.expires_in_ms)
        {
            return Err(PlatformError::invalid_argument("租约结果字段无效。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LeaseReleaseResult {
    pub generation: u64,
    pub cleanup: CleanupOutcome,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TargetBindResult {
    pub target_token: String,
    pub display_name: String,
    pub kind: TargetKind,
    pub expires_at: String,
    #[serde(
        default,
        deserialize_with = "deserialize_optional_non_null",
        skip_serializing_if = "Option::is_none"
    )]
    pub topology: Option<DisplayTopology>,
}

impl TargetBindResult {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.target_token) || !valid_id(&self.display_name) {
            return Err(PlatformError::invalid_argument("目标绑定身份或名称无效。"));
        }
        parse_utc_timestamp(&self.expires_at)?;
        if self.kind == TargetKind::Desktop && self.topology.is_none() {
            return Err(PlatformError::invalid_argument(
                "全桌面目标绑定必须包含显示拓扑。",
            ));
        }
        if let Some(ref topo) = self.topology {
            topo.validate()?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct TrajectoryRef {
    pub resource_id: String,
    pub sha256: String,
}

impl TrajectoryRef {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.resource_id) || !is_lower_hex_64(&self.sha256) {
            return Err(PlatformError::invalid_argument("轨迹资源引用格式无效。"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SegmentResult {
    pub segment_id: String,
    pub state: SegmentState,
    pub cleanup: CleanupOutcome,
    pub agent_epoch: String,
    #[serde(
        default,
        deserialize_with = "deserialize_required_nullable",
        skip_serializing_if = "Option::is_none"
    )]
    pub pause_id: Option<String>,
}

impl SegmentResult {
    pub fn validate(&self) -> Result<(), PlatformError> {
        if !valid_id(&self.segment_id) || !valid_id(&self.agent_epoch) {
            return Err(PlatformError::invalid_argument("输入段结果身份无效。"));
        }
        if self.state == SegmentState::Paused {
            let Some(ref pause_id) = self.pause_id else {
                return Err(PlatformError::invalid_argument(
                    "暂停输入段必须返回 pauseId。",
                ));
            };
            if !valid_id(pause_id)
                || !matches!(
                    self.cleanup,
                    CleanupOutcome::Succeeded | CleanupOutcome::NotRequired
                )
            {
                return Err(PlatformError::invalid_argument(
                    "暂停输入段的 pauseId 或清理状态无效。",
                ));
            }
        } else if self.pause_id.is_some() {
            return Err(PlatformError::invalid_argument(
                "非暂停输入段不得携带非空 pauseId。",
            ));
        }
        Ok(())
    }
}

/// Checks recursively that a resolved JSON value contains no `$expr` or `$literal` keys.
pub fn reject_unresolved_expressions(value: &Value) -> Result<(), PlatformError> {
    match value {
        Value::Object(map) => {
            if map.contains_key("$expr") || map.contains_key("$literal") {
                return Err(PlatformError::invalid_argument(
                    "Agent 输入参数不得包含未求值表达式。",
                ));
            }
            for child in map.values() {
                reject_unresolved_expressions(child)?;
            }
            Ok(())
        }
        Value::Array(items) => {
            for item in items {
                reject_unresolved_expressions(item)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

/// Computes the RFC 8785 JCS SHA-256 `payloadHash` for `segment.submit`.
pub fn compute_segment_payload_hash(
    target_token: &str,
    operation: &Value,
    trajectory_ref: Option<&TrajectoryRef>,
) -> Result<String, PlatformError> {
    let mut map = Map::new();
    map.insert(
        "targetToken".to_owned(),
        Value::String(target_token.to_owned()),
    );
    map.insert("operation".to_owned(), operation.clone());
    if let Some(tref) = trajectory_ref {
        map.insert(
            "trajectorySha256".to_owned(),
            Value::String(tref.sha256.clone()),
        );
    }
    jcs_sha256_hex(&Value::Object(map))
}

#[derive(Debug, Clone, PartialEq)]
pub enum ResolvedPosition {
    Physical(PhysicalPoint),
    Display(DisplayPoint),
}

impl ResolvedPosition {
    pub fn parse(value: &Value) -> Result<Self, PlatformError> {
        let Some(obj) = value.as_object() else {
            return Err(PlatformError::invalid_argument("指针位置必须为对象。"));
        };
        let Some(kind) = obj.get("kind").and_then(Value::as_str) else {
            return Err(PlatformError::invalid_argument("指针位置缺少 kind。"));
        };
        if kind != "point" {
            return Err(PlatformError::invalid_argument(
                "Agent 输入段位置必须为已求值坐标点。",
            ));
        }
        let Some(space) = obj.get("space").and_then(Value::as_str) else {
            return Err(PlatformError::invalid_argument("指针位置缺少 space。"));
        };
        match space {
            "desktopPhysical" => {
                if obj.len() != 4 {
                    return Err(PlatformError::invalid_argument("物理坐标字段数量无效。"));
                }
                let x = parse_i64_in(obj.get("x"), MIN_COORD_I64, MAX_COORD_I64)?;
                let y = parse_i64_in(obj.get("y"), MIN_COORD_I64, MAX_COORD_I64)?;
                Ok(Self::Physical(PhysicalPoint { x, y }))
            }
            "displayPixels" => {
                if obj.len() != 5 {
                    return Err(PlatformError::invalid_argument("屏内坐标字段数量无效。"));
                }
                let Some(display_id) = obj
                    .get("displayId")
                    .and_then(Value::as_str)
                    .filter(|s| valid_id(s))
                else {
                    return Err(PlatformError::invalid_argument("屏内坐标 displayId 无效。"));
                };
                let x = parse_f64_in(obj.get("x"), 0.0, MAX_COORD_F64)?;
                let y = parse_f64_in(obj.get("y"), 0.0, MAX_COORD_F64)?;
                Ok(Self::Display(DisplayPoint {
                    display_id: display_id.to_owned(),
                    x,
                    y,
                }))
            }
            _ => Err(PlatformError::invalid_argument("未知坐标空间。")),
        }
    }

    pub fn to_move_action(&self) -> NativeInputAction {
        match self {
            Self::Physical(pt) => NativeInputAction::PointerMovePhysical(*pt),
            Self::Display(pt) => NativeInputAction::PointerMoveDisplay(pt.clone()),
        }
    }

    pub fn from_pause_point(point: &PausePoint) -> Self {
        match point {
            PausePoint::Physical(pt) => Self::Physical(*pt),
            PausePoint::Display(pt) => Self::Display(pt.clone()),
        }
    }
}

fn parse_i64_in(val: Option<&Value>, min: i64, max: i64) -> Result<i64, PlatformError> {
    let Some(num) = val.and_then(Value::as_i64) else {
        return Err(PlatformError::invalid_argument("整数参数类型或范围无效。"));
    };
    if !(min..=max).contains(&num) {
        return Err(PlatformError::invalid_argument("整数参数超出范围。"));
    }
    Ok(num)
}

fn parse_u64_in(val: Option<&Value>, min: u64, max: u64) -> Result<u64, PlatformError> {
    let Some(num) = val.and_then(Value::as_u64) else {
        return Err(PlatformError::invalid_argument(
            "非负整数参数类型或范围无效。",
        ));
    };
    if !(min..=max).contains(&num) {
        return Err(PlatformError::invalid_argument("非负整数参数超出范围。"));
    }
    Ok(num)
}

fn parse_f64_in(val: Option<&Value>, min: f64, max: f64) -> Result<f64, PlatformError> {
    let Some(num) = val.and_then(Value::as_f64) else {
        return Err(PlatformError::invalid_argument("数值参数类型无效。"));
    };
    if !num.is_finite() || !(min..=max).contains(&num) {
        return Err(PlatformError::invalid_argument("数值参数超出范围。"));
    }
    Ok(num)
}

fn parse_button(val: Option<&Value>) -> Result<PointerButton, PlatformError> {
    match val.and_then(Value::as_str) {
        Some("left") => Ok(PointerButton::Left),
        Some("right") => Ok(PointerButton::Right),
        Some("middle") => Ok(PointerButton::Middle),
        Some("x1") => Ok(PointerButton::X1),
        Some("x2") => Ok(PointerButton::X2),
        _ => Err(PlatformError::invalid_argument("鼠标按钮名称无效。")),
    }
}

/// Evaluates the quintic minimum-jerk interpolation curve `s(u) = 10u³ - 15u⁴ + 6u⁵` for `u` in `[0, 1]`.
pub fn quintic_progress(u: f64) -> f64 {
    let clamped = u.clamp(0.0, 1.0);
    let u2 = clamped * clamped;
    let u3 = u2 * clamped;
    u3 * (10.0 - 15.0 * clamped + 6.0 * u2)
}

#[derive(Debug, Clone, PartialEq)]
pub struct AtomicInputEvent {
    pub delay_before_us: u64,
    pub action: NativeInputAction,
}

fn smooth_step_count(duration_ms: u64, distance: f64) -> u64 {
    let by_time = (duration_ms / SMOOTH_STEP_INTERVAL_MS).max(2);
    let by_space = ((distance / SMOOTH_STEP_TARGET_PX).ceil() as u64).max(2);
    let rate_cap = (duration_ms / MIN_SMOOTH_STEP_INTERVAL_MS).max(2);
    by_time
        .max(by_space.min(250))
        .min(rate_cap)
        .min(MAX_SMOOTH_STEPS)
}

fn interpolate_physical_moves(
    p0: PhysicalPoint,
    p1: PhysicalPoint,
    duration_ms: u64,
) -> Vec<AtomicInputEvent> {
    let dx = (p1.x - p0.x) as f64;
    let dy = (p1.y - p0.y) as f64;
    let steps = smooth_step_count(duration_ms, dx.hypot(dy));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let point = if i == steps {
            p1
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            PhysicalPoint {
                x: p0.x + (dx * s).round() as i64,
                y: p0.y + (dy * s).round() as i64,
            }
        };
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action: NativeInputAction::PointerMovePhysical(point),
        });
    }
    events
}

fn interpolate_same_display_moves(
    d0: &DisplayPoint,
    d1: &DisplayPoint,
    duration_ms: u64,
) -> Vec<AtomicInputEvent> {
    let dx = d1.x - d0.x;
    let dy = d1.y - d0.y;
    let steps = smooth_step_count(duration_ms, dx.hypot(dy));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let point = if i == steps {
            d1.clone()
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            DisplayPoint {
                display_id: d1.display_id.clone(),
                x: d0.x + dx * s,
                y: d0.y + dy * s,
            }
        };
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action: NativeInputAction::PointerMoveDisplay(point),
        });
    }
    events
}

fn map_native_to_display(
    display: &LocalDisplay,
    nx: f64,
    ny: f64,
) -> Result<Option<DisplayPoint>, PlatformError> {
    let Some(transform) = display.pixel_to_native else {
        return Ok(None);
    };
    let (raw_x, raw_y) = transform.native_to_pixel(nx, ny)?;
    let px = if raw_x == 0.0 { 0.0 } else { raw_x };
    let py = if raw_y == 0.0 { 0.0 } else { raw_y };
    if display.contains_pixel(px, py) && px <= MAX_COORD_F64 && py <= MAX_COORD_F64 {
        Ok(Some(DisplayPoint {
            display_id: display.display_id.clone(),
            x: px,
            y: py,
        }))
    } else {
        Ok(None)
    }
}

fn interpolate_cross_display_local(
    d0: &DisplayPoint,
    d1: &DisplayPoint,
    duration_ms: u64,
    local: &DisplayLocalTopology,
) -> Result<Vec<AtomicInputEvent>, PlatformError> {
    let find_display = |pt: &DisplayPoint| -> Result<&LocalDisplay, PlatformError> {
        let disp = local
            .displays
            .iter()
            .find(|d| d.display_id == pt.display_id)
            .ok_or_else(|| {
                PlatformError::invalid_argument("跨屏移动引用的 displayId 不在当前拓扑中。")
            })?;
        if !disp.contains_pixel(pt.x, pt.y) {
            return Err(PlatformError::invalid_argument(
                "跨屏移动坐标超出显示屏像素范围。",
            ));
        }
        if disp.pixel_to_native.is_none() {
            return Err(PlatformError::invalid_argument(
                "跨屏移动显示屏缺少 pixelToNative 坐标变换。",
            ));
        }
        Ok(disp)
    };
    let disp0 = find_display(d0)?;
    let disp1 = find_display(d1)?;
    let (nx0, ny0) = disp0
        .pixel_to_native
        .expect("verified above")
        .pixel_to_native(d0.x, d0.y)?;
    let (nx1, ny1) = disp1
        .pixel_to_native
        .expect("verified above")
        .pixel_to_native(d1.x, d1.y)?;

    let dx = nx1 - nx0;
    let dy = ny1 - ny0;
    let steps = smooth_step_count(duration_ms, dx.hypot(dy));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let point = if i == steps {
            d1.clone()
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            let nx = nx0 + dx * s;
            let ny = ny0 + dy * s;
            let mut mapped = map_native_to_display(disp0, nx, ny)?;
            if mapped.is_none() {
                mapped = map_native_to_display(disp1, nx, ny)?;
            }
            if mapped.is_none() {
                for candidate in &local.displays {
                    if candidate.display_id != disp0.display_id
                        && candidate.display_id != disp1.display_id
                        && let Some(pt) = map_native_to_display(candidate, nx, ny)?
                    {
                        mapped = Some(pt);
                        break;
                    }
                }
            }
            mapped.ok_or_else(|| {
                PlatformError::invalid_argument(
                    "跨屏平滑移动中间点超出可用显示区域或缺少坐标变换。",
                )
            })?
        };
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action: NativeInputAction::PointerMoveDisplay(point),
        });
    }
    Ok(events)
}

fn display_point_to_physical(
    pt: &DisplayPoint,
    phys: &PhysicalTopology,
) -> Result<PhysicalPoint, PlatformError> {
    let disp = phys
        .displays
        .iter()
        .find(|d| d.display_id == pt.display_id)
        .ok_or_else(|| {
            PlatformError::invalid_argument("屏内坐标引用的 displayId 不在物理拓扑中。")
        })?;
    if disp.rotation != Rotation::Deg0 {
        return Err(PlatformError::invalid_argument(
            "带旋转角度的显示屏不支持与全局物理坐标混合平滑插值。",
        ));
    }
    if pt.x < 0.0
        || pt.x >= disp.bounds.width as f64
        || pt.y < 0.0
        || pt.y >= disp.bounds.height as f64
    {
        return Err(PlatformError::invalid_argument(
            "屏内坐标超出对应显示区域范围。",
        ));
    }
    let max_x = disp.bounds.x + disp.bounds.width - 1;
    let max_y = disp.bounds.y + disp.bounds.height - 1;
    Ok(PhysicalPoint {
        x: (disp.bounds.x + pt.x.round() as i64).clamp(disp.bounds.x, max_x),
        y: (disp.bounds.y + pt.y.round() as i64).clamp(disp.bounds.y, max_y),
    })
}

fn interpolate_cross_display_physical(
    d0: &DisplayPoint,
    d1: &DisplayPoint,
    duration_ms: u64,
    phys: &PhysicalTopology,
) -> Result<Vec<AtomicInputEvent>, PlatformError> {
    let p0 = display_point_to_physical(d0, phys)?;
    let p1 = display_point_to_physical(d1, phys)?;
    let dx = (p1.x - p0.x) as f64;
    let dy = (p1.y - p0.y) as f64;
    let steps = smooth_step_count(duration_ms, dx.hypot(dy));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let point = if i == steps {
            d1.clone()
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            let pt = PhysicalPoint {
                x: p0.x + (dx * s).round() as i64,
                y: p0.y + (dy * s).round() as i64,
            };
            let disp = phys
                .displays
                .iter()
                .find(|d| d.rotation == Rotation::Deg0 && d.bounds.contains_point(pt))
                .ok_or_else(|| {
                    PlatformError::invalid_argument("跨屏平滑移动中间点超出可用显示区域。")
                })?;
            DisplayPoint {
                display_id: disp.display_id.clone(),
                x: (pt.x - disp.bounds.x) as f64,
                y: (pt.y - disp.bounds.y) as f64,
            }
        };
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action: NativeInputAction::PointerMoveDisplay(point),
        });
    }
    Ok(events)
}

fn interpolate_mixed_space(
    start: &ResolvedPosition,
    end: &ResolvedPosition,
    duration_ms: u64,
    topology: Option<&DisplayTopology>,
) -> Result<Vec<AtomicInputEvent>, PlatformError> {
    let Some(DisplayTopology::Physical(phys)) = topology else {
        return Err(PlatformError::invalid_argument(
            "正时长混合坐标空间移动需要物理显示拓扑支持。",
        ));
    };
    let to_phys = |pos: &ResolvedPosition| -> Result<PhysicalPoint, PlatformError> {
        match pos {
            ResolvedPosition::Physical(pt) => Ok(*pt),
            ResolvedPosition::Display(d) => display_point_to_physical(d, phys),
        }
    };
    let p0 = to_phys(start)?;
    let p1 = to_phys(end)?;
    let dx = (p1.x - p0.x) as f64;
    let dy = (p1.y - p0.y) as f64;
    let steps = smooth_step_count(duration_ms, dx.hypot(dy));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let action = if i == steps {
            end.to_move_action()
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            let pt = PhysicalPoint {
                x: p0.x + (dx * s).round() as i64,
                y: p0.y + (dy * s).round() as i64,
            };
            if !phys.displays.iter().any(|d| d.bounds.contains_point(pt)) {
                return Err(PlatformError::invalid_argument(
                    "混合坐标平滑移动中间点超出可用显示区域。",
                ));
            }
            NativeInputAction::PointerMovePhysical(pt)
        };
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action,
        });
    }
    Ok(events)
}

fn generate_smooth_moves(
    start: &ResolvedPosition,
    end: &ResolvedPosition,
    duration_ms: u64,
    topology: Option<&DisplayTopology>,
) -> Result<Vec<AtomicInputEvent>, PlatformError> {
    if duration_ms == 0 {
        return Ok(vec![AtomicInputEvent {
            delay_before_us: 0,
            action: end.to_move_action(),
        }]);
    }
    match (start, end) {
        (ResolvedPosition::Physical(p0), ResolvedPosition::Physical(p1)) => {
            Ok(interpolate_physical_moves(*p0, *p1, duration_ms))
        }
        (ResolvedPosition::Display(d0), ResolvedPosition::Display(d1))
            if d0.display_id == d1.display_id =>
        {
            Ok(interpolate_same_display_moves(d0, d1, duration_ms))
        }
        (ResolvedPosition::Display(d0), ResolvedPosition::Display(d1)) => match topology {
            Some(DisplayTopology::DisplayLocal(local)) => {
                interpolate_cross_display_local(d0, d1, duration_ms, local)
            }
            Some(DisplayTopology::Physical(phys)) => {
                interpolate_cross_display_physical(d0, d1, duration_ms, phys)
            }
            None => Err(PlatformError::invalid_argument(
                "正时长跨屏指针移动缺少有效的显示拓扑映射。",
            )),
        },
        _ => interpolate_mixed_space(start, end, duration_ms, topology),
    }
}

fn generate_smooth_deltas(dx: i64, dy: i64, duration_ms: u64) -> Vec<AtomicInputEvent> {
    if duration_ms == 0 {
        return vec![AtomicInputEvent {
            delay_before_us: 0,
            action: NativeInputAction::PointerDelta {
                dx: dx as f64,
                dy: dy as f64,
            },
        }];
    }
    let steps = smooth_step_count(duration_ms, (dx as f64).hypot(dy as f64));
    let total_us = duration_ms * 1_000;
    let mut events = Vec::with_capacity(steps as usize);
    let mut prev_us = 0_u64;
    let mut sent_x = 0_i64;
    let mut sent_y = 0_i64;
    for i in 1..=steps {
        let target_us = (total_us * i) / steps;
        let delay = target_us - prev_us;
        prev_us = target_us;
        let (target_x, target_y) = if i == steps {
            (dx, dy)
        } else {
            let s = quintic_progress(i as f64 / steps as f64);
            (
                (dx as f64 * s).round() as i64,
                (dy as f64 * s).round() as i64,
            )
        };
        let step_dx = target_x - sent_x;
        let step_dy = target_y - sent_y;
        sent_x = target_x;
        sent_y = target_y;
        events.push(AtomicInputEvent {
            delay_before_us: delay,
            action: NativeInputAction::PointerDelta {
                dx: step_dx as f64,
                dy: step_dy as f64,
            },
        });
    }
    events
}

/// Validates a resolved `segment.submit` `operation` and expands it into ordered atomic native events.
pub fn expand_input_operation(
    operation: &Value,
    trajectory_ref: Option<&TrajectoryRef>,
    current_pointer: Option<&PausePoint>,
    topology: Option<&DisplayTopology>,
) -> Result<Vec<AtomicInputEvent>, PlatformError> {
    reject_unresolved_expressions(operation)?;
    let Some(obj) = operation.as_object() else {
        return Err(PlatformError::invalid_argument("operation 必须为对象。"));
    };
    if obj.len() != 5 || obj.contains_key("store") {
        return Err(PlatformError::invalid_argument(
            "operation 字段不完整或包含非法 store。",
        ));
    }
    let Some(node_id) = obj.get("id").and_then(Value::as_str) else {
        return Err(PlatformError::invalid_argument("operation.id 无效。"));
    };
    if !valid_node_id(node_id) {
        return Err(PlatformError::invalid_argument("operation.id 格式无效。"));
    }
    let Some(title) = obj.get("title").and_then(Value::as_str) else {
        return Err(PlatformError::invalid_argument("operation.title 无效。"));
    };
    let title_chars = title.chars().count();
    if !(1..=120).contains(&title_chars) || title.bytes().any(|b| b == 0) {
        return Err(PlatformError::invalid_argument(
            "operation.title 长度应为 1～120。",
        ));
    }
    let Some(on_map) = obj.get("on").and_then(Value::as_object) else {
        return Err(PlatformError::invalid_argument("operation.on 必须为对象。"));
    };
    if !on_map.is_empty() {
        return Err(PlatformError::invalid_argument(
            "Agent 输入段 operation.on 必须为空对象。",
        ));
    }
    let Some(action) = obj.get("action").and_then(Value::as_str) else {
        return Err(PlatformError::invalid_argument("operation.action 无效。"));
    };
    let Some(params) = obj.get("params").and_then(Value::as_object) else {
        return Err(PlatformError::invalid_argument(
            "operation.params 必须为对象。",
        ));
    };
    if params.get("target").and_then(Value::as_str) != Some("main") {
        return Err(PlatformError::invalid_argument(
            "Agent 输入段 params.target 必须固定为 main。",
        ));
    }
    if (action == "input.trajectory") != trajectory_ref.is_some() {
        return Err(PlatformError::invalid_argument(
            "trajectoryRef 仅用于且必须用于 input.trajectory 动作。",
        ));
    }

    match action {
        "pointer.move" => {
            if params.len() != 3 {
                return Err(PlatformError::invalid_argument("pointer.move 参数无效。"));
            }
            let pos = ResolvedPosition::parse(
                params
                    .get("position")
                    .ok_or_else(|| PlatformError::invalid_argument("缺少 position。"))?,
            )?;
            let duration_ms = parse_u64_in(params.get("durationMs"), 0, 86_400_000)?;
            if duration_ms == 0 {
                return Ok(vec![AtomicInputEvent {
                    delay_before_us: 0,
                    action: pos.to_move_action(),
                }]);
            }
            let current = current_pointer.ok_or_else(|| {
                PlatformError::invalid_argument("正时长指针移动缺少已核实的起始指针位置。")
            })?;
            let start = ResolvedPosition::from_pause_point(current);
            generate_smooth_moves(&start, &pos, duration_ms, topology)
        }
        "pointer.delta" => {
            if params.len() != 4 {
                return Err(PlatformError::invalid_argument("pointer.delta 参数无效。"));
            }
            let dx = parse_i64_in(params.get("dx"), MIN_COORD_I64, MAX_COORD_I64)?;
            let dy = parse_i64_in(params.get("dy"), MIN_COORD_I64, MAX_COORD_I64)?;
            let duration_ms = parse_u64_in(params.get("durationMs"), 0, 86_400_000)?;
            Ok(generate_smooth_deltas(dx, dy, duration_ms))
        }
        "pointer.click" => {
            if !(5..=6).contains(&params.len())
                || (params.len() == 6 && !params.contains_key("pressDurationMs"))
            {
                return Err(PlatformError::invalid_argument("pointer.click 参数无效。"));
            }
            let pos = ResolvedPosition::parse(
                params
                    .get("position")
                    .ok_or_else(|| PlatformError::invalid_argument("缺少 position。"))?,
            )?;
            let button = parse_button(params.get("button"))?;
            let clicks = parse_u64_in(params.get("clicks"), 1, 10)?;
            let interval_ms = parse_u64_in(params.get("intervalMs"), 0, 86_400_000)?;
            let press_ms = match params.get("pressDurationMs") {
                Some(v) => parse_u64_in(Some(v), 0, MAX_PRESS_DURATION_MS)?,
                None => DEFAULT_PRESS_DURATION_MS,
            };
            let mut events = Vec::with_capacity(1 + (clicks as usize) * 2);
            events.push(AtomicInputEvent {
                delay_before_us: 0,
                action: pos.to_move_action(),
            });
            for idx in 0..clicks {
                events.push(AtomicInputEvent {
                    delay_before_us: if idx == 0 { 0 } else { interval_ms * 1_000 },
                    action: NativeInputAction::PointerButtonDown(button),
                });
                events.push(AtomicInputEvent {
                    delay_before_us: press_ms * 1_000,
                    action: NativeInputAction::PointerButtonUp(button),
                });
            }
            Ok(events)
        }
        "pointer.drag" => {
            if params.len() != 5 {
                return Err(PlatformError::invalid_argument("pointer.drag 参数无效。"));
            }
            let from = ResolvedPosition::parse(
                params
                    .get("from")
                    .ok_or_else(|| PlatformError::invalid_argument("缺少 from。"))?,
            )?;
            let to = ResolvedPosition::parse(
                params
                    .get("to")
                    .ok_or_else(|| PlatformError::invalid_argument("缺少 to。"))?,
            )?;
            let button = parse_button(params.get("button"))?;
            let duration_ms = parse_u64_in(params.get("durationMs"), 0, 86_400_000)?;
            let move_steps = generate_smooth_moves(&from, &to, duration_ms, topology)?;
            let mut events = Vec::with_capacity(move_steps.len() + 3);
            events.push(AtomicInputEvent {
                delay_before_us: 0,
                action: from.to_move_action(),
            });
            events.push(AtomicInputEvent {
                delay_before_us: 0,
                action: NativeInputAction::PointerButtonDown(button),
            });
            events.extend(move_steps);
            events.push(AtomicInputEvent {
                delay_before_us: 0,
                action: NativeInputAction::PointerButtonUp(button),
            });
            Ok(events)
        }
        "pointer.scroll" => {
            if params.len() != 3 {
                return Err(PlatformError::invalid_argument("pointer.scroll 参数无效。"));
            }
            let dx = parse_f64_in(params.get("dx"), -10_000.0, 10_000.0)?;
            let dy = parse_f64_in(params.get("dy"), -10_000.0, 10_000.0)?;
            Ok(vec![AtomicInputEvent {
                delay_before_us: 0,
                action: NativeInputAction::PointerScroll { dx, dy },
            }])
        }
        "pointer.button" => {
            if params.len() != 3 {
                return Err(PlatformError::invalid_argument("pointer.button 参数无效。"));
            }
            let button = parse_button(params.get("button"))?;
            let action = match params.get("state").and_then(Value::as_str) {
                Some("down") => NativeInputAction::PointerButtonDown(button),
                Some("up") => NativeInputAction::PointerButtonUp(button),
                _ => {
                    return Err(PlatformError::invalid_argument(
                        "按钮状态必须为 down 或 up。",
                    ));
                }
            };
            Ok(vec![AtomicInputEvent {
                delay_before_us: 0,
                action,
            }])
        }
        "keyboard.down" | "keyboard.up" => {
            if params.len() != 2 {
                return Err(PlatformError::invalid_argument("键盘按键参数无效。"));
            }
            let Some(key) = params.get("key").and_then(Value::as_str) else {
                return Err(PlatformError::invalid_argument("缺少 key。"));
            };
            let key_chars = key.chars().count();
            if !(1..=32).contains(&key_chars) || key.bytes().any(|b| b == 0) {
                return Err(PlatformError::invalid_argument("按键名称长度应为 1～32。"));
            }
            let action = if action == "keyboard.down" {
                NativeInputAction::KeyDown(key.to_owned())
            } else {
                NativeInputAction::KeyUp(key.to_owned())
            };
            Ok(vec![AtomicInputEvent {
                delay_before_us: 0,
                action,
            }])
        }
        "keyboard.chord" => {
            if params.len() != 3 {
                return Err(PlatformError::invalid_argument("keyboard.chord 参数无效。"));
            }
            let Some(keys_arr) = params.get("keys").and_then(Value::as_array) else {
                return Err(PlatformError::invalid_argument(
                    "keyboard.chord 缺少 keys。",
                ));
            };
            if !(1..=8).contains(&keys_arr.len()) {
                return Err(PlatformError::invalid_argument("组合键数量应为 1～8。"));
            }
            let press_ms = parse_u64_in(params.get("pressDurationMs"), 0, MAX_PRESS_DURATION_MS)?;
            let mut keys = Vec::with_capacity(keys_arr.len());
            let mut seen = HashSet::with_capacity(keys_arr.len());
            for item in keys_arr {
                let Some(k) = item.as_str() else {
                    return Err(PlatformError::invalid_argument("组合键名称必须为文本。"));
                };
                let chars = k.chars().count();
                if !(1..=32).contains(&chars) || k.bytes().any(|b| b == 0) || !seen.insert(k) {
                    return Err(PlatformError::invalid_argument(
                        "组合键包含非法或重复按键。",
                    ));
                }
                keys.push(k.to_owned());
            }
            let mut events = Vec::with_capacity(keys.len() * 2);
            for k in &keys {
                events.push(AtomicInputEvent {
                    delay_before_us: 0,
                    action: NativeInputAction::KeyDown(k.clone()),
                });
            }
            for (idx, k) in keys.iter().rev().enumerate() {
                events.push(AtomicInputEvent {
                    delay_before_us: if idx == 0 { press_ms * 1_000 } else { 0 },
                    action: NativeInputAction::KeyUp(k.clone()),
                });
            }
            Ok(events)
        }
        "keyboard.text" => {
            if params.len() != 3 {
                return Err(PlatformError::invalid_argument("keyboard.text 参数无效。"));
            }
            let Some(text) = params.get("text").and_then(Value::as_str) else {
                return Err(PlatformError::invalid_argument("keyboard.text 缺少 text。"));
            };
            if text.chars().count() > 262_144 || text.bytes().any(|b| b == 0) {
                return Err(PlatformError::invalid_argument(
                    "输入文本长度超限或包含空字节。",
                ));
            }
            let interval_ms = parse_u64_in(params.get("intervalMs"), 0, 1_000)?;
            let mut events = Vec::new();
            for (idx, ch) in text.chars().enumerate() {
                events.push(AtomicInputEvent {
                    delay_before_us: if idx == 0 { 0 } else { interval_ms * 1_000 },
                    action: NativeInputAction::UnicodeText(ch.to_string()),
                });
            }
            Ok(events)
        }
        "input.trajectory" => {
            let Some(tref) = trajectory_ref else {
                return Err(PlatformError::invalid_argument(
                    "input.trajectory 缺少 trajectoryRef。",
                ));
            };
            tref.validate()?;
            let Some(traj_key) = params.get("trajectory").and_then(Value::as_str) else {
                return Err(PlatformError::invalid_argument("缺少 trajectory 键。"));
            };
            if !valid_node_id(traj_key) {
                return Err(PlatformError::invalid_argument("trajectory 键格式无效。"));
            }
            if !(2..=3).contains(&params.len())
                || (params.len() == 3 && !params.contains_key("displayMapping"))
            {
                return Err(PlatformError::invalid_argument(
                    "input.trajectory 参数无效。",
                ));
            }
            Err(PlatformError::capability_not_implemented(
                "轨迹资源封存与回放尚未接入。",
            ))
        }
        _ => Err(PlatformError::invalid_argument("未知输入动作。")),
    }
}
