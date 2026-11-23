use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use deskweave_platform_api::{
    AtomicInputEvent, CleanupOutcome, DesktopSession, EmergencyState, HeldInput, InterruptPolicy,
    LeaseAcquireResult, LeaseReleaseReason, LeaseReleaseResult, LeaseSnapshot, LeaseState,
    MAX_COORD_F64, MAX_COORD_I64, MAX_SEGMENT_EVENTS, MIN_COORD_I64, NativeInputAction, PausePoint,
    PauseSnapshot, PhysicalPoint, PlatformError, SegmentCursor, SegmentResult, SegmentState,
    SubmissionOutcome, TargetBindResult, TargetDescriptor, TargetKind, TargetState, TargetStatus,
    TargetToken, TrajectoryRef, compute_segment_payload_hash, expand_input_operation,
    format_utc_timestamp, parse_utc_timestamp, sort_held_inputs, sort_target_states,
};
use serde_json::{Map, Value};

use crate::server::SessionContext;

pub(crate) const LEASE_TTL_US: u64 = 2_000_000;
pub(crate) const LEASE_EXPIRES_IN_MS: u64 = 2_000;
pub(crate) const PAUSE_RETENTION_US: u64 = 30 * 60 * 1_000_000;
const MAX_WAIT_QUEUE: usize = 32;
const MAX_SAFE_INT: u64 = 9_007_199_254_740_991;
const MAX_HELD_INPUTS: usize = 256;
const MAX_BOUND_TARGETS: usize = 512;
const MAX_HISTORICAL_LEASES: usize = 512;
const MAX_RESUME_RECEIPTS: usize = 1024;
const MAX_SEGMENT_RECEIPTS: usize = 4096;
const MAX_CONSUMED_TOKENS: usize = 4096;
#[cfg(test)]
const MAX_REGISTERED_CONFIRMATIONS: usize = 256;

#[cfg_attr(not(test), allow(dead_code))]
#[derive(Clone)]
pub(crate) struct RegisteredConfirmation {
    pub(crate) lease_id: String,
    pub(crate) pause_id: String,
    pub(crate) agent_epoch: String,
    pub(crate) generation: u64,
    pub(crate) checkpoint_hash: String,
    pub(crate) resume_plan_hash: String,
    pub(crate) restore_target_token: Option<String>,
}

#[derive(Clone)]
struct BoundTarget {
    descriptor: TargetDescriptor,
    owner_workspace: String,
}

#[derive(Clone)]
struct WaitEntry {
    waiter_id: u64,
    session_id: String,
}

#[derive(Clone)]
struct InFlightSegment {
    pause_requested: bool,
}

#[derive(Clone)]
struct SuspendedSegment {
    receipt_key: (String, String),
    segment_id: String,
    payload_hash: String,
    target_token: String,
    events: Vec<AtomicInputEvent>,
    next_event_index: usize,
    remaining_delay_us: u64,
    active_elapsed_us: u64,
    dispatched_count: usize,
    pause_cleanup: CleanupOutcome,
}

#[derive(Clone)]
struct PendingPause {
    suspended_segment: Option<SuspendedSegment>,
}

#[derive(Clone)]
struct ActiveLease {
    lease_id: String,
    run_id: String,
    owner_session: String,
    owner_workspace: String,
    generation: u64,
    agent_epoch: String,
    targets: Vec<String>,
    _interrupt_policy: InterruptPolicy,
    state: LeaseState,
    cleanup: CleanupOutcome,
    held_inputs: Vec<HeldInput>,
    pointer: PausePoint,
    physical_pointer: PhysicalPoint,
    input_target_token: Option<String>,
    in_flight: Option<InFlightSegment>,
    release_waiters: usize,
    release_pending: bool,
    pending_pause: Option<PendingPause>,
    suspended_segment: Option<SuspendedSegment>,
    pause_snapshot: Option<PauseSnapshot>,
    last_resume_attempt_id: Option<String>,
    reason: Option<String>,
    expires_at_us: u64,
    pause_expires_at_us: Option<u64>,
}

impl ActiveLease {
    fn to_snapshot(&self) -> LeaseSnapshot {
        LeaseSnapshot {
            lease_id: self.lease_id.clone(),
            generation: self.generation,
            agent_epoch: self.agent_epoch.clone(),
            state: self.state,
            cleanup: self.cleanup,
            pause_snapshot: self.pause_snapshot.clone(),
            last_resume_attempt_id: self.last_resume_attempt_id.clone(),
            reason: self.reason.clone(),
        }
    }
}

#[derive(Clone)]
struct HistoricalLease {
    owner_workspace: String,
    snapshot: LeaseSnapshot,
}

#[derive(Clone, PartialEq, Eq)]
struct ResumeRequestKey {
    generation: u64,
    pause_id: String,
    resume_plan_hash: String,
    confirmation_grant: Option<String>,
}

#[derive(Clone)]
struct ResumeReceipt {
    lease_id: String,
    request: ResumeRequestKey,
    snapshot: LeaseSnapshot,
}

#[derive(Clone)]
struct SegmentReceipt {
    lease_id: String,
    generation: u64,
    payload_hash: String,
    result: SegmentResult,
}

struct CoordinatorState {
    generation: u64,
    next_lease_seq: u64,
    next_pause_seq: u64,
    next_waiter_id: u64,
    consumed_grants: BTreeSet<String>,
    consumed_grants_order: VecDeque<String>,
    consumed_confirmations: BTreeSet<String>,
    consumed_confirmations_order: VecDeque<String>,
    registered_confirmations: BTreeMap<String, RegisteredConfirmation>,
    bound_targets: BTreeMap<String, BoundTarget>,
    current_lease: Option<ActiveLease>,
    historical_leases: BTreeMap<String, HistoricalLease>,
    historical_leases_order: VecDeque<String>,
    wait_queue: VecDeque<WaitEntry>,
    resume_receipts: BTreeMap<(String, String, String), ResumeReceipt>,
    resume_receipts_order: VecDeque<(String, String, String)>,
    segment_receipts: BTreeMap<(String, String), SegmentReceipt>,
    segment_receipts_order: VecDeque<(String, String)>,
}

impl CoordinatorState {
    fn record_historical_lease(&mut self, lease: &ActiveLease) {
        let lease_id = lease.lease_id.clone();
        if !self.historical_leases.contains_key(&lease_id) {
            while self.historical_leases.len() >= MAX_HISTORICAL_LEASES {
                let Some(oldest) = self.historical_leases_order.pop_front() else {
                    break;
                };
                if self
                    .current_lease
                    .as_ref()
                    .is_some_and(|cur| cur.lease_id == oldest)
                {
                    self.historical_leases_order.push_back(oldest);
                    break;
                }
                self.historical_leases.remove(&oldest);
            }
            self.historical_leases_order.push_back(lease_id.clone());
        }
        self.historical_leases.insert(
            lease_id,
            HistoricalLease {
                owner_workspace: lease.owner_workspace.clone(),
                snapshot: lease.to_snapshot(),
            },
        );
    }

    fn record_consumed_grant(&mut self, grant: String) {
        if self.consumed_grants.insert(grant.clone()) {
            while self.consumed_grants.len() > MAX_CONSUMED_TOKENS {
                if let Some(oldest) = self.consumed_grants_order.pop_front() {
                    self.consumed_grants.remove(&oldest);
                } else {
                    break;
                }
            }
            self.consumed_grants_order.push_back(grant);
        }
    }

    fn record_consumed_confirmation(&mut self, grant: String) {
        self.registered_confirmations.remove(&grant);
        if self.consumed_confirmations.insert(grant.clone()) {
            while self.consumed_confirmations.len() > MAX_CONSUMED_TOKENS {
                if let Some(oldest) = self.consumed_confirmations_order.pop_front() {
                    self.consumed_confirmations.remove(&oldest);
                } else {
                    break;
                }
            }
            self.consumed_confirmations_order.push_back(grant);
        }
    }

    fn ensure_resume_receipt_capacity(
        &mut self,
        key: &(String, String, String),
    ) -> Result<(), PlatformError> {
        let active_lease_id = self.current_lease.as_ref().map(|cur| cur.lease_id.as_str());
        ensure_receipt_capacity(
            &mut self.resume_receipts,
            &mut self.resume_receipts_order,
            key,
            MAX_RESUME_RECEIPTS,
            active_lease_id,
            |r| r.lease_id.as_str(),
            "当前租约恢复收据数量达到上限。",
        )
    }

    fn record_resume_receipt(
        &mut self,
        key: (String, String, String),
        receipt: ResumeReceipt,
    ) -> Result<(), PlatformError> {
        if !self.resume_receipts.contains_key(&key) {
            self.ensure_resume_receipt_capacity(&key)?;
            self.resume_receipts_order.push_back(key.clone());
        }
        self.resume_receipts.insert(key, receipt);
        Ok(())
    }

    fn ensure_segment_receipt_capacity(
        &mut self,
        key: &(String, String),
    ) -> Result<(), PlatformError> {
        let active_lease_id = self.current_lease.as_ref().map(|cur| cur.lease_id.as_str());
        ensure_receipt_capacity(
            &mut self.segment_receipts,
            &mut self.segment_receipts_order,
            key,
            MAX_SEGMENT_RECEIPTS,
            active_lease_id,
            |r| r.lease_id.as_str(),
            "当前租约输入段收据数量达到上限。",
        )
    }

    fn record_segment_receipt(
        &mut self,
        key: (String, String),
        receipt: SegmentReceipt,
    ) -> Result<(), PlatformError> {
        if !self.segment_receipts.contains_key(&key) {
            self.ensure_segment_receipt_capacity(&key)?;
            self.segment_receipts_order.push_back(key.clone());
        }
        self.segment_receipts.insert(key, receipt);
        Ok(())
    }

    fn remove_segment_receipt(&mut self, key: &(String, String)) {
        if self.segment_receipts.remove(key).is_some() {
            self.segment_receipts_order.retain(|item| item != key);
        }
    }
}

fn ensure_receipt_capacity<K: Ord, V>(
    map: &mut BTreeMap<K, V>,
    order: &mut VecDeque<K>,
    key: &K,
    limit: usize,
    active_lease_id: Option<&str>,
    entry_lease_id: impl Fn(&V) -> &str,
    limit_message: &'static str,
) -> Result<(), PlatformError> {
    if map.contains_key(key) {
        return Ok(());
    }
    let total = map.len();
    if total >= limit {
        let mut evicted = false;
        for _ in 0..total {
            let Some(oldest_key) = order.pop_front() else {
                break;
            };
            let belongs_to_active = map
                .get(&oldest_key)
                .is_some_and(|entry| active_lease_id == Some(entry_lease_id(entry)));
            if belongs_to_active {
                order.push_back(oldest_key);
            } else {
                map.remove(&oldest_key);
                evicted = true;
                break;
            }
        }
        if !evicted {
            return Err(PlatformError::new("LIMIT_EXCEEDED", limit_message));
        }
    }
    Ok(())
}

pub(crate) struct LeaseCoordinator {
    state: Mutex<CoordinatorState>,
    wait_cv: Condvar,
}

impl LeaseCoordinator {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(CoordinatorState {
                generation: 0,
                next_lease_seq: 0,
                next_pause_seq: 0,
                next_waiter_id: 0,
                consumed_grants: BTreeSet::new(),
                consumed_grants_order: VecDeque::new(),
                consumed_confirmations: BTreeSet::new(),
                consumed_confirmations_order: VecDeque::new(),
                registered_confirmations: BTreeMap::new(),
                bound_targets: BTreeMap::new(),
                current_lease: None,
                historical_leases: BTreeMap::new(),
                historical_leases_order: VecDeque::new(),
                wait_queue: VecDeque::new(),
                resume_receipts: BTreeMap::new(),
                resume_receipts_order: VecDeque::new(),
                segment_receipts: BTreeMap::new(),
                segment_receipts_order: VecDeque::new(),
            }),
            wait_cv: Condvar::new(),
        }
    }

    #[cfg(test)]
    pub(crate) fn register_confirmation(
        &self,
        grant: impl Into<String>,
        record: RegisteredConfirmation,
    ) -> Result<(), PlatformError> {
        let mut state = self.state.lock().unwrap();
        let active_lease_id = state.current_lease.as_ref().map(|l| l.lease_id.clone());
        if state.registered_confirmations.len() >= MAX_REGISTERED_CONFIRMATIONS {
            state
                .registered_confirmations
                .retain(|_, v| active_lease_id.as_deref() == Some(v.lease_id.as_str()));
            if state.registered_confirmations.len() >= MAX_REGISTERED_CONFIRMATIONS {
                return Err(PlatformError::new(
                    "LIMIT_EXCEEDED",
                    "恢复确认授权表已达上限。",
                ));
            }
        }
        state.registered_confirmations.insert(grant.into(), record);
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn wait_queue_len(&self) -> usize {
        self.state.lock().unwrap().wait_queue.len()
    }

    fn refresh_lease_expiry_locked(
        &self,
        state: &mut CoordinatorState,
        desktop: &dyn DesktopSession,
    ) {
        let now_us = desktop.clock().now_us();
        let expired = match state.current_lease.as_ref() {
            Some(lease)
                if lease.state == LeaseState::Active
                    && lease.in_flight.is_none()
                    && !lease.release_pending =>
            {
                now_us >= lease.expires_at_us
            }
            Some(lease) if lease.state == LeaseState::Paused && !lease.release_pending => {
                now_us >= lease.expires_at_us
                    || lease
                        .pause_expires_at_us
                        .is_some_and(|limit| now_us >= limit)
            }
            Some(lease)
                if lease.state == LeaseState::Pausing
                    && lease.in_flight.is_none()
                    && !lease.release_pending =>
            {
                now_us >= lease.expires_at_us
            }
            _ => false,
        };

        if expired {
            revoke_current_lease_locked(state, desktop, "LEASE_EXPIRED");
            self.wait_cv.notify_all();
        }
    }

    pub(crate) fn on_session_closed(&self, session_id: &str, desktop: &dyn DesktopSession) {
        let mut state = self.state.lock().unwrap();
        state
            .wait_queue
            .retain(|entry| entry.session_id != session_id);
        let mut waited_lease: Option<(String, u64)> = None;
        while let Some(lease) = state.current_lease.as_mut()
            && lease.owner_session == session_id
            && lease.state != LeaseState::Uncertain
            && lease.in_flight.is_some()
        {
            if waited_lease
                .as_ref()
                .is_none_or(|(lid, lgen)| *lid != lease.lease_id || *lgen != lease.generation)
            {
                lease.release_waiters += 1;
                waited_lease = Some((lease.lease_id.clone(), lease.generation));
            }
            self.wait_cv.notify_all();
            let (guard, _) = self
                .wait_cv
                .wait_timeout(state, Duration::from_millis(10))
                .unwrap();
            state = guard;
        }
        if let Some((lid, lgen)) = waited_lease
            && let Some(lease) = state.current_lease.as_mut()
            && lease.lease_id == lid
            && lease.generation == lgen
        {
            lease.release_waiters = lease.release_waiters.saturating_sub(1);
        }
        let should_release = state.current_lease.as_ref().is_some_and(|lease| {
            lease.owner_session == session_id && lease.state != LeaseState::Uncertain
        });

        if should_release {
            revoke_current_lease_locked(&mut state, desktop, "SESSION_CLOSED");
        }
        self.wait_cv.notify_all();
    }

    pub(crate) fn handle_target_bind(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
    ) -> Result<TargetBindResult, PlatformError> {
        if params.len() != 1 || !params.contains_key("selectionGrant") {
            return Err(PlatformError::invalid_argument(
                "target.bind 参数必须仅包含 selectionGrant。",
            ));
        }
        let selection_grant = require_non_empty_string(params, "selectionGrant")?;

        let mut state = self.state.lock().unwrap();
        if state.consumed_grants.contains(&selection_grant) {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "目标选择授权已消耗或无效。",
            ));
        }

        let descriptor = bind_target_selection(desktop, &selection_grant)?;

        let now_secs = current_utc_secs();
        let exp_secs = parse_utc_timestamp(&descriptor.expires_at)?;
        if exp_secs <= now_secs {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "目标选择授权已超过有效期。",
            ));
        }

        if let Some(existing) = state.bound_targets.get(&descriptor.token.0)
            && existing.owner_workspace != ctx.workspace_id
        {
            let referenced_by_lease = state
                .current_lease
                .as_ref()
                .is_some_and(|lease| lease.targets.contains(&descriptor.token.0));
            let still_valid = parse_utc_timestamp(&existing.descriptor.expires_at)
                .ok()
                .is_some_and(|exp| exp > now_secs);
            if referenced_by_lease || still_valid {
                return Err(PlatformError::new(
                    "RESOURCE_DENIED",
                    "目标令牌已被其他工作空间占用。",
                ));
            }
        }

        if !state.bound_targets.contains_key(&descriptor.token.0)
            && state.bound_targets.len() >= MAX_BOUND_TARGETS
        {
            let active_targets: BTreeSet<String> = state
                .current_lease
                .as_ref()
                .map(|l| l.targets.iter().cloned().collect())
                .unwrap_or_default();
            state.bound_targets.retain(|tok, entry| {
                active_targets.contains(tok)
                    || parse_utc_timestamp(&entry.descriptor.expires_at)
                        .ok()
                        .is_some_and(|exp| exp > now_secs)
            });
            if state.bound_targets.len() >= MAX_BOUND_TARGETS {
                return Err(PlatformError::new(
                    "LIMIT_EXCEEDED",
                    "已绑定目标令牌数量超过上限。",
                ));
            }
        }

        let topology = if descriptor.kind == TargetKind::Desktop {
            let topo = desktop.topology()?;
            topo.validate()?;
            Some(topo)
        } else {
            None
        };

        let result = TargetBindResult {
            target_token: descriptor.token.0.clone(),
            display_name: descriptor.display_name.clone(),
            kind: descriptor.kind,
            expires_at: descriptor.expires_at.clone(),
            topology,
        };
        result.validate()?;

        state.record_consumed_grant(selection_grant);
        state.bound_targets.insert(
            descriptor.token.0.clone(),
            BoundTarget {
                descriptor,
                owner_workspace: ctx.workspace_id.clone(),
            },
        );
        Ok(result)
    }

    pub(crate) fn handle_lease_acquire(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        is_draining: impl Fn() -> bool,
        deadline_ms: u64,
    ) -> Result<LeaseAcquireResult, PlatformError> {
        for key in params.keys() {
            if !matches!(
                key.as_str(),
                "runId" | "targets" | "waitMs" | "interruptPolicy"
            ) {
                return Err(PlatformError::invalid_argument(format!(
                    "lease.acquire 包含未知参数字段：{key}"
                )));
            }
        }
        let run_id = require_non_empty_string(params, "runId")?;
        let wait_ms = require_u64_in_range(params, "waitMs", 0, 60_000)?;
        let interrupt_policy = match params.get("interruptPolicy") {
            None => InterruptPolicy::Pause,
            Some(Value::String(s)) if s == "pause" => InterruptPolicy::Pause,
            Some(Value::String(s)) if s == "ignore" => InterruptPolicy::Ignore,
            Some(_) => {
                return Err(PlatformError::invalid_argument(
                    "interruptPolicy 取值无效。",
                ));
            }
        };

        let Some(targets_val) = params.get("targets").and_then(Value::as_array) else {
            return Err(PlatformError::invalid_argument(
                "lease.acquire 的 targets 必须为数组。",
            ));
        };
        if targets_val.is_empty() || targets_val.len() > 32 {
            return Err(PlatformError::invalid_argument(
                "lease.acquire 的 targets 数量必须在 1..=32 之间。",
            ));
        }
        let mut targets = Vec::with_capacity(targets_val.len());
        let mut seen_targets = BTreeSet::new();
        for item in targets_val {
            let Some(token) = item.as_str() else {
                return Err(PlatformError::invalid_argument(
                    "targets 元素必须为字符串。",
                ));
            };
            validate_identifier(token, "targets")?;
            if !seen_targets.insert(token.to_owned()) {
                return Err(PlatformError::invalid_argument(
                    "lease.acquire 的 targets 不允许重复目标令牌。",
                ));
            }
            targets.push(token.to_owned());
        }

        if is_draining() {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "Agent 正在排空，拒绝新租约请求。",
            ));
        }
        if desktop.emergency().state() == EmergencyState::Locked {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "桌面处于急停锁定状态，无法获取租约。",
            ));
        }

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);
        verify_targets_bound_and_usable(&state, &ctx.workspace_id, &targets, desktop)?;

        if state
            .current_lease
            .as_ref()
            .is_some_and(|lease| lease.state == LeaseState::Uncertain)
        {
            return Err(PlatformError::new(
                "EFFECT_UNCERTAIN",
                "上一桌面租约输入清理未确认，拒绝授予新租约。",
            )
            .with_retry("afterQuery"));
        }

        if state.current_lease.is_some() || !state.wait_queue.is_empty() {
            if wait_ms == 0 {
                return Err(
                    PlatformError::new("DESKTOP_BUSY", "当前交互桌面已被其他运行占用。")
                        .with_retry("afterQuery"),
                );
            }
            if state.wait_queue.len() >= MAX_WAIT_QUEUE {
                return Err(
                    PlatformError::new("LIMIT_EXCEEDED", "桌面租约等待队列已达上限。")
                        .with_retry("afterQuery"),
                );
            }

            state.next_waiter_id += 1;
            let waiter_id = state.next_waiter_id;
            state.wait_queue.push_back(WaitEntry {
                waiter_id,
                session_id: ctx.session_id.clone(),
            });

            let wall_start = Instant::now();
            let wall_wait_budget = Duration::from_millis(wait_ms);
            let wall_deadline_budget = Duration::from_millis(deadline_ms);
            let clock_start_us = desktop.clock().now_us();
            let clock_wait_budget_us = wait_ms.saturating_mul(1_000);

            loop {
                let wall_elapsed = wall_start.elapsed();
                let clock_elapsed_us = desktop.clock().now_us().saturating_sub(clock_start_us);
                if wall_elapsed >= wall_deadline_budget {
                    state.wait_queue.retain(|w| w.waiter_id != waiter_id);
                    self.wait_cv.notify_all();
                    return Err(PlatformError::new(
                        "DEADLINE_EXCEEDED",
                        "等待桌面租约超出请求 deadlineMs 时限。",
                    )
                    .with_retry("afterQuery"));
                }
                if wall_elapsed >= wall_wait_budget || clock_elapsed_us >= clock_wait_budget_us {
                    state.wait_queue.retain(|w| w.waiter_id != waiter_id);
                    self.wait_cv.notify_all();
                    return Err(PlatformError::new(
                        "DESKTOP_BUSY",
                        "等待桌面租约超时，当前桌面仍被占用。",
                    )
                    .with_retry("afterQuery"));
                }
                if is_draining() {
                    state.wait_queue.retain(|w| w.waiter_id != waiter_id);
                    self.wait_cv.notify_all();
                    return Err(PlatformError::new(
                        "RESOURCE_DENIED",
                        "Agent 正在排空，取消租约等待。",
                    ));
                }
                if desktop.emergency().state() == EmergencyState::Locked {
                    state.wait_queue.retain(|w| w.waiter_id != waiter_id);
                    self.wait_cv.notify_all();
                    return Err(PlatformError::new(
                        "RESOURCE_DENIED",
                        "桌面进入急停锁定状态，取消租约等待。",
                    ));
                }

                self.refresh_lease_expiry_locked(&mut state, desktop);

                if state
                    .current_lease
                    .as_ref()
                    .is_some_and(|lease| lease.state == LeaseState::Uncertain)
                {
                    state.wait_queue.retain(|w| w.waiter_id != waiter_id);
                    self.wait_cv.notify_all();
                    return Err(PlatformError::new(
                        "EFFECT_UNCERTAIN",
                        "上一桌面租约输入清理未确认，拒绝授予新租约。",
                    )
                    .with_retry("afterQuery"));
                }

                let is_head = state
                    .wait_queue
                    .front()
                    .is_some_and(|front| front.waiter_id == waiter_id);
                if state.current_lease.is_none() && is_head {
                    state.wait_queue.pop_front();
                    break;
                }

                let remaining_wall = wall_wait_budget
                    .min(wall_deadline_budget)
                    .saturating_sub(wall_elapsed);
                let slice = remaining_wall.min(Duration::from_millis(10));
                let (guard, _) = self.wait_cv.wait_timeout(state, slice).unwrap();
                state = guard;
            }

            if let Err(err) =
                verify_targets_bound_and_usable(&state, &ctx.workspace_id, &targets, desktop)
            {
                self.wait_cv.notify_all();
                return Err(err);
            }
        }

        let now_us = desktop.clock().now_us();
        state.generation += 1;
        state.next_lease_seq += 1;
        let generation = state.generation;
        let lease_id = format!("lease-{}", state.next_lease_seq);
        let initial_pt = PhysicalPoint { x: 0, y: 0 };

        let lease = ActiveLease {
            lease_id: lease_id.clone(),
            run_id: run_id.clone(),
            owner_session: ctx.session_id.clone(),
            owner_workspace: ctx.workspace_id.clone(),
            generation,
            agent_epoch: ctx.agent_epoch.clone(),
            targets,
            _interrupt_policy: interrupt_policy,
            state: LeaseState::Active,
            cleanup: CleanupOutcome::NotRequired,
            held_inputs: Vec::new(),
            pointer: PausePoint::Physical(initial_pt),
            physical_pointer: initial_pt,
            input_target_token: None,
            in_flight: None,
            release_waiters: 0,
            release_pending: false,
            pending_pause: None,
            suspended_segment: None,
            pause_snapshot: None,
            last_resume_attempt_id: None,
            reason: None,
            expires_at_us: now_us.saturating_add(LEASE_TTL_US),
            pause_expires_at_us: None,
        };
        state.current_lease = Some(lease);

        let result = LeaseAcquireResult {
            lease_id,
            run_id,
            generation,
            agent_epoch: ctx.agent_epoch.clone(),
            expires_in_ms: LEASE_EXPIRES_IN_MS,
        };
        result.validate()?;
        Ok(result)
    }

    pub(crate) fn handle_lease_renew(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
    ) -> Result<LeaseAcquireResult, PlatformError> {
        if params.len() != 2
            || !params.contains_key("leaseId")
            || !params.contains_key("generation")
        {
            return Err(PlatformError::invalid_argument(
                "lease.renew 参数必须仅包含 leaseId 与 generation。",
            ));
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;
        let generation = require_u64_in_range(params, "generation", 0, MAX_SAFE_INT)?;

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);

        let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
        if lease.release_pending
            || !matches!(
                lease.state,
                LeaseState::Active | LeaseState::Paused | LeaseState::Pausing
            )
        {
            return Err(PlatformError::new(
                "LEASE_EXPIRED",
                "当前租约状态不允许续期。",
            ));
        }

        let now_us = desktop.clock().now_us();
        let candidate_exp = now_us.saturating_add(LEASE_TTL_US);
        let (new_exp, expires_in_ms) = match (lease.state, lease.pause_expires_at_us) {
            (LeaseState::Paused, Some(pause_limit)) => {
                if now_us >= pause_limit {
                    return Err(PlatformError::new(
                        "LEASE_EXPIRED",
                        "暂停租约保留期限已到期。",
                    ));
                }
                let clamped = candidate_exp.min(pause_limit);
                let rem_ms = clamped
                    .saturating_sub(now_us)
                    .div_ceil(1_000)
                    .clamp(1, LEASE_EXPIRES_IN_MS);
                (clamped, rem_ms)
            }
            _ => (candidate_exp, LEASE_EXPIRES_IN_MS),
        };
        lease.expires_at_us = new_exp;

        let result = LeaseAcquireResult {
            lease_id: lease.lease_id.clone(),
            run_id: lease.run_id.clone(),
            generation: lease.generation,
            agent_epoch: lease.agent_epoch.clone(),
            expires_in_ms,
        };
        result.validate()?;
        Ok(result)
    }

    pub(crate) fn handle_lease_release(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        deadline_ms: u64,
    ) -> Result<LeaseReleaseResult, PlatformError> {
        if params.len() != 3
            || !params.contains_key("leaseId")
            || !params.contains_key("generation")
            || !params.contains_key("reason")
        {
            return Err(PlatformError::invalid_argument(
                "lease.release 参数必须仅包含 leaseId、generation 与 reason。",
            ));
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;
        let generation = require_u64_in_range(params, "generation", 0, MAX_SAFE_INT)?;
        let reason_val = params
            .get("reason")
            .ok_or_else(|| PlatformError::invalid_argument("缺少 reason 参数。"))?;
        let reason: LeaseReleaseReason = serde_json::from_value(reason_val.clone())
            .map_err(|_| PlatformError::invalid_argument("lease.release 的 reason 取值无效。"))?;

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);

        let wait_start = Instant::now();
        let deadline_budget = Duration::from_millis(deadline_ms);
        let mut registered_waiter = false;
        let wait_res = loop {
            let need_wait =
                match require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx) {
                    Ok(lease) => {
                        if lease.state == LeaseState::Uncertain {
                            break Err(PlatformError::new(
                                "EFFECT_UNCERTAIN",
                                "桌面存在未确认输入状态，不能通过常规 lease.release 解锁。",
                            )
                            .with_retry("afterQuery"));
                        }
                        if lease.in_flight.is_some() {
                            if !registered_waiter {
                                lease.release_waiters += 1;
                                registered_waiter = true;
                            }
                            self.wait_cv.notify_all();
                            true
                        } else {
                            false
                        }
                    }
                    Err(err) => break Err(err),
                };
            if !need_wait {
                break Ok(());
            }
            let elapsed = wait_start.elapsed();
            if elapsed >= deadline_budget {
                break Err(PlatformError::new(
                    "DEADLINE_EXCEEDED",
                    "等待在途输入边界超时，拒绝提前释放桌面。",
                )
                .with_retry("afterQuery"));
            }
            let slice = deadline_budget
                .saturating_sub(elapsed)
                .min(Duration::from_millis(10));
            let (guard, _) = self.wait_cv.wait_timeout(state, slice).unwrap();
            state = guard;
        };
        if registered_waiter {
            if let Some(lease) = state.current_lease.as_mut()
                && lease.lease_id == lease_id
                && lease.generation == generation
            {
                lease.release_waiters = lease.release_waiters.saturating_sub(1);
            }
            self.wait_cv.notify_all();
        }
        wait_res?;

        {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            if lease.state == LeaseState::Uncertain {
                return Err(PlatformError::new(
                    "EFFECT_UNCERTAIN",
                    "桌面存在未确认输入状态，不能通过常规 lease.release 解锁。",
                )
                .with_retry("afterQuery"));
            }
        }
        let reason_str = match reason {
            LeaseReleaseReason::Finished => "finished",
            LeaseReleaseReason::Cancelled => "cancelled",
            LeaseReleaseReason::Yielded => "yielded",
            LeaseReleaseReason::Fault => "fault",
        };
        let (new_generation, cleanup) =
            revoke_current_lease_locked(&mut state, desktop, reason_str);
        self.wait_cv.notify_all();

        let result = LeaseReleaseResult {
            generation: new_generation,
            cleanup,
        };
        Ok(result)
    }

    pub(crate) fn handle_lease_pause(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        deadline_ms: u64,
    ) -> Result<LeaseSnapshot, PlatformError> {
        if params.len() != 2
            || !params.contains_key("leaseId")
            || !params.contains_key("generation")
        {
            return Err(PlatformError::invalid_argument(
                "lease.pause 参数必须仅包含 leaseId 与 generation。",
            ));
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;
        let generation = require_u64_in_range(params, "generation", 0, MAX_SAFE_INT)?;

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);

        let wait_start = Instant::now();
        let deadline_budget = Duration::from_millis(deadline_ms);

        loop {
            let need_wait = {
                let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
                if lease.release_pending || lease.release_waiters > 0 {
                    self.wait_cv.notify_all();
                    true
                } else if lease.in_flight.is_some() {
                    if lease.state == LeaseState::Active {
                        lease.state = LeaseState::Pausing;
                    }
                    if let Some(ref mut flight) = lease.in_flight {
                        flight.pause_requested = true;
                    }
                    self.wait_cv.notify_all();
                    true
                } else {
                    false
                }
            };

            if !need_wait {
                break;
            }

            let elapsed = wait_start.elapsed();
            if elapsed >= deadline_budget {
                let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
                let snap = lease.to_snapshot();
                snap.validate()?;
                return Ok(snap);
            }
            let slice = deadline_budget
                .saturating_sub(elapsed)
                .min(Duration::from_millis(10));
            let (guard, _) = self.wait_cv.wait_timeout(state, slice).unwrap();
            state = guard;
        }

        let is_already_settled = {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            matches!(lease.state, LeaseState::Paused | LeaseState::Uncertain)
                || (lease.state == LeaseState::Pausing && lease.pending_pause.is_some())
        };
        if is_already_settled {
            let lease = state.current_lease.as_ref().unwrap();
            let snap = lease.to_snapshot();
            snap.validate()?;
            return Ok(snap);
        }

        finalize_pause_locked(&mut state, &lease_id, generation, ctx, desktop, None, None)
            .map(|(snap, _)| snap)
    }

    pub(crate) fn handle_lease_status(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
    ) -> Result<LeaseSnapshot, PlatformError> {
        if params.len() != 1 || !params.contains_key("leaseId") {
            return Err(PlatformError::invalid_argument(
                "lease.status 参数必须仅包含 leaseId。",
            ));
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);

        if let Some(lease) = state.current_lease.as_ref()
            && lease.lease_id == lease_id
        {
            if lease.owner_workspace != ctx.workspace_id {
                return Err(PlatformError::new(
                    "ACCESS_DENIED",
                    "无权查询其他工作空间的桌面租约状态。",
                ));
            }
            let snapshot = lease.to_snapshot();
            snapshot.validate()?;
            return Ok(snapshot);
        }

        if let Some(hist) = state.historical_leases.get(&lease_id) {
            if hist.owner_workspace != ctx.workspace_id {
                return Err(PlatformError::new(
                    "ACCESS_DENIED",
                    "无权查询其他工作空间的桌面租约状态。",
                ));
            }
            hist.snapshot.validate()?;
            return Ok(hist.snapshot.clone());
        }

        Err(PlatformError::new(
            "LEASE_EXPIRED",
            "指定的桌面租约不存在或已过期。",
        ))
    }

    pub(crate) fn handle_lease_resume(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        deadline_ms: u64,
    ) -> Result<LeaseSnapshot, PlatformError> {
        for key in params.keys() {
            if !matches!(
                key.as_str(),
                "leaseId"
                    | "generation"
                    | "pauseId"
                    | "resumeAttemptId"
                    | "resumePlanHash"
                    | "confirmationGrant"
            ) {
                return Err(PlatformError::invalid_argument(format!(
                    "lease.resume 包含未知参数字段：{key}"
                )));
            }
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;
        let generation = require_u64_in_range(params, "generation", 0, MAX_SAFE_INT)?;
        let pause_id = require_non_empty_string(params, "pauseId")?;
        let resume_attempt_id = require_non_empty_string(params, "resumeAttemptId")?;
        let resume_plan_hash = require_sha256_hex(params, "resumePlanHash")?;
        let confirmation_grant = match params.get("confirmationGrant") {
            None => None,
            Some(Value::String(s)) => {
                validate_identifier(s, "confirmationGrant")?;
                Some(s.clone())
            }
            Some(_) => {
                return Err(PlatformError::invalid_argument(
                    "confirmationGrant 必须为非空字符串。",
                ));
            }
        };

        let request_key = ResumeRequestKey {
            generation,
            pause_id: pause_id.clone(),
            resume_plan_hash: resume_plan_hash.clone(),
            confirmation_grant: confirmation_grant.clone(),
        };

        let mut state = self.state.lock().unwrap();
        let receipt_lookup_key = (
            ctx.workspace_id.clone(),
            lease_id.clone(),
            resume_attempt_id.clone(),
        );
        if let Some(existing) = state.resume_receipts.get(&receipt_lookup_key) {
            if existing.request == request_key {
                return Ok(existing.snapshot.clone());
            }
            return Err(PlatformError::new(
                "REQUEST_CONFLICT",
                "相同 resumeAttemptId 的恢复参数与已有收据冲突。",
            ));
        }

        self.refresh_lease_expiry_locked(&mut state, desktop);

        if desktop.emergency().state() == EmergencyState::Locked {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "桌面处于急停锁定状态，无法继续运行。",
            ));
        }

        let (targets, pause_snapshot) = {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            if lease.state != LeaseState::Paused {
                return Err(PlatformError::invalid_argument(
                    "仅处于 paused 状态的租约允许执行 lease.resume。",
                ));
            }
            let Some(snap) = lease.pause_snapshot.clone() else {
                return Err(PlatformError::new(
                    "INTERNAL_ERROR",
                    "暂停租约缺少实时检查点。",
                ));
            };
            if snap.pause_id != pause_id {
                return Err(PlatformError::invalid_argument(
                    "pauseId 与当前租约暂停检查点不匹配。",
                ));
            }
            (lease.targets.clone(), snap)
        };

        verify_targets_bound_and_usable(&state, &ctx.workspace_id, &targets, desktop)?;

        let current_targets = build_target_states(&state, &targets, desktop)?;
        if current_targets != pause_snapshot.targets {
            return Err(PlatformError::target_lost(
                "目标版本或显示拓扑已变化，保持 paused 状态。",
            ));
        }

        let requires_confirmation =
            !pause_snapshot.held_inputs.is_empty() || pause_snapshot.cursor.is_some();
        if requires_confirmation && confirmation_grant.is_none() {
            return Err(PlatformError::new(
                "LOCAL_CONFIRMATION_REQUIRED",
                "恢复持有输入或段内游标需要提供可信本地 confirmationGrant。",
            ));
        }

        if let Some(grant) = &confirmation_grant {
            verify_confirmation_grant(
                &state,
                grant,
                &lease_id,
                generation,
                &pause_snapshot,
                &resume_plan_hash,
            )?;
        }

        state.ensure_resume_receipt_capacity(&receipt_lookup_key)?;

        let mut restored_held = Vec::new();
        if requires_confirmation {
            let restore_target = pause_snapshot.input_target_token.as_ref().ok_or_else(|| {
                PlatformError::invalid_argument("恢复持有输入时检查点必须包含 inputTargetToken。")
            })?;
            focus_target_checked(desktop, restore_target)?;
            let status = verify_target_checked(desktop, restore_target)?;
            if status != TargetStatus::Active {
                return Err(PlatformError::target_lost(
                    "恢复目标未能取得前台焦点，保持 paused 状态。",
                ));
            }

            let move_action = match &pause_snapshot.pointer {
                PausePoint::Physical(pt) => NativeInputAction::PointerMovePhysical(*pt),
                PausePoint::Display(pt) => NativeInputAction::PointerMoveDisplay(pt.clone()),
            };
            let move_outcome = submit_native_action(desktop, restore_target, &move_action);
            if !matches!(move_outcome, Ok(SubmissionOutcome::Submitted)) {
                return fail_resume_uncertain_locked(
                    &mut state,
                    ctx,
                    confirmation_grant,
                    receipt_lookup_key,
                    lease_id,
                    request_key,
                    (
                        CleanupOutcome::NotRequired,
                        restored_held,
                        resume_attempt_id,
                    ),
                );
            }

            for held in &pause_snapshot.held_inputs {
                let action = match held {
                    HeldInput::Key { key } => NativeInputAction::KeyDown(key.clone()),
                    HeldInput::Button { button } => NativeInputAction::PointerButtonDown(*button),
                };
                match submit_native_action(desktop, restore_target, &action) {
                    Ok(SubmissionOutcome::Submitted) => {
                        restored_held.push(held.clone());
                    }
                    Ok(SubmissionOutcome::Rejected | SubmissionOutcome::Uncertain) | Err(_) => {
                        let cleanup = release_held_inputs(desktop, &mut restored_held);
                        return fail_resume_uncertain_locked(
                            &mut state,
                            ctx,
                            confirmation_grant,
                            receipt_lookup_key,
                            lease_id,
                            request_key,
                            (cleanup, restored_held, resume_attempt_id),
                        );
                    }
                }
            }
        } else if let Some(restore_target) = &pause_snapshot.input_target_token {
            if confirmation_grant.is_some() {
                focus_target_checked(desktop, restore_target)?;
            }
            let status = verify_target_checked(desktop, restore_target)?;
            if status != TargetStatus::Active {
                return Err(PlatformError::target_lost("输入目标当前不在前台。"));
            }
        }

        if let Some(grant) = confirmation_grant {
            state.record_consumed_confirmation(grant);
        }

        let now_us = desktop.clock().now_us();
        let (suspended, snapshot) = {
            let lease = state.current_lease.as_mut().unwrap();
            lease.state = LeaseState::Active;
            lease.cleanup = CleanupOutcome::NotRequired;
            lease.held_inputs = restored_held;
            if let PausePoint::Physical(pt) = pause_snapshot.pointer {
                lease.physical_pointer = pt;
            }
            lease.pointer = pause_snapshot.pointer;
            lease.pause_snapshot = None;
            lease.pending_pause = None;
            lease.pause_expires_at_us = None;
            lease.last_resume_attempt_id = Some(resume_attempt_id);
            lease.reason = None;
            lease.expires_at_us = now_us.saturating_add(LEASE_TTL_US);
            let suspended = lease.suspended_segment.take();
            if suspended.is_some() {
                lease.in_flight = Some(InFlightSegment {
                    pause_requested: false,
                });
            }
            let snapshot = lease.to_snapshot();
            (suspended, snapshot)
        };

        snapshot.validate()?;
        state.record_resume_receipt(
            receipt_lookup_key.clone(),
            ResumeReceipt {
                lease_id: lease_id.clone(),
                request: request_key.clone(),
                snapshot: snapshot.clone(),
            },
        )?;

        let Some(seg) = suspended else {
            return Ok(snapshot);
        };

        let running_result = SegmentResult {
            segment_id: seg.segment_id.clone(),
            state: SegmentState::Running,
            cleanup: CleanupOutcome::NotRequired,
            agent_epoch: ctx.agent_epoch.clone(),
            pause_id: None,
        };
        running_result.validate()?;
        state.record_segment_receipt(
            seg.receipt_key.clone(),
            SegmentReceipt {
                lease_id: lease_id.clone(),
                generation,
                payload_hash: seg.payload_hash.clone(),
                result: running_result,
            },
        )?;

        let (mut state, cont_res) = self.drive_segment_events(
            state,
            ctx,
            desktop,
            SegmentRun {
                lease_id: lease_id.clone(),
                generation,
                receipt_key: seg.receipt_key,
                segment_id: seg.segment_id,
                payload_hash: seg.payload_hash,
                target_token: seg.target_token,
                events: seg.events,
                start_index: seg.next_event_index,
                first_delay_override: Some(seg.remaining_delay_us),
                prior_active_elapsed_us: seg.active_elapsed_us,
                dispatched_count: seg.dispatched_count,
                fresh_submit: false,
            },
            deadline_ms,
        );

        let final_snapshot = if let Some(lease) = state
            .current_lease
            .as_ref()
            .filter(|l| l.lease_id == lease_id)
        {
            lease.to_snapshot()
        } else if let Some(hist) = state.historical_leases.get(&lease_id) {
            hist.snapshot.clone()
        } else {
            return Err(cont_res.err().unwrap_or_else(|| {
                PlatformError::new("LEASE_EXPIRED", "继续运行期间桌面租约已失效。")
            }));
        };
        final_snapshot.validate()?;
        state.record_resume_receipt(
            receipt_lookup_key,
            ResumeReceipt {
                lease_id,
                request: request_key,
                snapshot: final_snapshot.clone(),
            },
        )?;
        Ok(final_snapshot)
    }

    pub(crate) fn handle_segment_submit(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        deadline_ms: u64,
    ) -> Result<SegmentResult, PlatformError> {
        for key in params.keys() {
            if !matches!(
                key.as_str(),
                "leaseId"
                    | "generation"
                    | "segmentId"
                    | "payloadHash"
                    | "targetToken"
                    | "operation"
                    | "trajectoryRef"
            ) {
                return Err(PlatformError::invalid_argument(format!(
                    "segment.submit 包含未知参数字段：{key}"
                )));
            }
        }
        let lease_id = require_non_empty_string(params, "leaseId")?;
        let generation = require_u64_in_range(params, "generation", 0, MAX_SAFE_INT)?;
        let segment_id = require_non_empty_string(params, "segmentId")?;
        let payload_hash = require_sha256_hex(params, "payloadHash")?;
        let target_token = require_non_empty_string(params, "targetToken")?;
        let operation = params
            .get("operation")
            .ok_or_else(|| PlatformError::invalid_argument("segment.submit 缺少 operation。"))?;

        let trajectory_ref: Option<TrajectoryRef> = match params.get("trajectoryRef") {
            None => None,
            Some(val) => {
                let parsed: TrajectoryRef = serde_json::from_value(val.clone()).map_err(|_| {
                    PlatformError::invalid_argument("segment.submit 的 trajectoryRef 结构无效。")
                })?;
                parsed.validate()?;
                Some(parsed)
            }
        };

        let expected_hash =
            compute_segment_payload_hash(&target_token, operation, trajectory_ref.as_ref())?;
        if expected_hash != payload_hash {
            return Err(PlatformError::invalid_argument(
                "segment.submit 的 payloadHash 与实际参数摘要不匹配。",
            ));
        }

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);

        let receipt_key = (ctx.workspace_id.clone(), segment_id.clone());
        if let Some(receipt) = state.segment_receipts.get(&receipt_key) {
            if receipt.lease_id == lease_id
                && receipt.generation == generation
                && receipt.payload_hash == payload_hash
            {
                return Ok(receipt.result.clone());
            }
            return Err(PlatformError::new(
                "REQUEST_CONFLICT",
                "相同 segmentId 的租约身份或参数摘要与已有收据冲突。",
            ));
        }

        {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            if lease.state != LeaseState::Active {
                return Err(PlatformError::new(
                    "LEASE_EXPIRED",
                    "当前租约非活动状态，拒绝派发输入段。",
                ));
            }
        }

        if desktop.emergency().state() == EmergencyState::Locked {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "桌面处于急停锁定状态，拒绝派发输入段。",
            ));
        }

        let current_pointer = {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            if lease.in_flight.is_some() || lease.release_pending || lease.release_waiters > 0 {
                return Err(PlatformError::new(
                    "DESKTOP_BUSY",
                    "当前租约已有输入段正在执行或释放。",
                )
                .with_retry("afterQuery"));
            }
            if !lease.targets.contains(&target_token) {
                return Err(PlatformError::new(
                    "RESOURCE_DENIED",
                    "输入段 targetToken 不在该租约已授权的目标集合内。",
                ));
            }
            let pointer = lease.pointer.clone();
            verify_target_expiry_locked(&state, &target_token)?;
            pointer
        };

        let target_status = verify_target_checked(desktop, &target_token)?;
        if target_status != TargetStatus::Active {
            return Err(PlatformError::target_lost("输入目标已失焦、失效或丢失。"));
        }

        let atomic_events =
            expand_input_operation(operation, trajectory_ref.as_ref(), &current_pointer)?;
        if atomic_events.len() as u64 > MAX_SEGMENT_EVENTS {
            return Err(PlatformError::new(
                "LIMIT_EXCEEDED",
                "输入段展开事件数量超过上限。",
            ));
        }

        state.ensure_segment_receipt_capacity(&receipt_key)?;

        let running_result = SegmentResult {
            segment_id: segment_id.clone(),
            state: SegmentState::Running,
            cleanup: CleanupOutcome::NotRequired,
            agent_epoch: ctx.agent_epoch.clone(),
            pause_id: None,
        };
        running_result.validate()?;
        state.record_segment_receipt(
            receipt_key.clone(),
            SegmentReceipt {
                lease_id: lease_id.clone(),
                generation,
                payload_hash: payload_hash.clone(),
                result: running_result,
            },
        )?;

        {
            let lease = require_active_owner_lease_mut(&mut state, &lease_id, generation, ctx)?;
            lease.in_flight = Some(InFlightSegment {
                pause_requested: false,
            });
        }

        let (_state, result) = self.drive_segment_events(
            state,
            ctx,
            desktop,
            SegmentRun {
                lease_id,
                generation,
                receipt_key,
                segment_id,
                payload_hash,
                target_token,
                events: atomic_events,
                start_index: 0,
                first_delay_override: None,
                prior_active_elapsed_us: 0,
                dispatched_count: 0,
                fresh_submit: true,
            },
            deadline_ms,
        );
        result
    }

    fn drive_segment_events<'a>(
        &'a self,
        mut state: MutexGuard<'a, CoordinatorState>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        run: SegmentRun,
        deadline_ms: u64,
    ) -> (
        MutexGuard<'a, CoordinatorState>,
        Result<SegmentResult, PlatformError>,
    ) {
        let rpc_wall_start = Instant::now();
        let rpc_deadline_budget = Duration::from_millis(deadline_ms);
        let seg_clock_start_us = desktop.clock().now_us();
        let mut dispatched_count = run.dispatched_count;

        let active_elapsed_now = |wall_start: &Instant, clock_start_us: u64| {
            let cur = desktop
                .clock()
                .now_us()
                .saturating_sub(clock_start_us)
                .max(u64::try_from(wall_start.elapsed().as_micros()).unwrap_or(u64::MAX));
            run.prior_active_elapsed_us
                .saturating_add(cur)
                .min(86_400_000_000)
        };

        for (idx, event) in run.events.iter().enumerate().skip(run.start_index) {
            let delay_us = if idx == run.start_index {
                run.first_delay_override.unwrap_or(event.delay_before_us)
            } else {
                event.delay_before_us
            };

            let wait_wall_start = Instant::now();
            let wait_clock_start_us = desktop.clock().now_us();
            loop {
                let (pause_req, release_req) =
                    {
                        let Some(lease) = state.current_lease.as_ref().filter(|l| {
                            l.lease_id == run.lease_id && l.generation == run.generation
                        }) else {
                            return (
                                state,
                                Err(PlatformError::new(
                                    "LEASE_EXPIRED",
                                    "输入段执行期间桌面租约已失效或撤销。",
                                )),
                            );
                        };
                        (
                            lease.in_flight.as_ref().is_some_and(|f| f.pause_requested),
                            lease.release_waiters > 0,
                        )
                    };

                let wall_elapsed_us =
                    u64::try_from(wait_wall_start.elapsed().as_micros()).unwrap_or(u64::MAX);
                let clock_elapsed_us = desktop.clock().now_us().saturating_sub(wait_clock_start_us);
                let step_elapsed_us = wall_elapsed_us.max(clock_elapsed_us);
                let remaining_delay_us = delay_us.saturating_sub(step_elapsed_us);

                if release_req {
                    let remaining_rpc =
                        rpc_deadline_budget.saturating_sub(rpc_wall_start.elapsed());
                    let active_elapsed_us = active_elapsed_now(&rpc_wall_start, seg_clock_start_us);
                    return self.abort_segment_on_release(
                        state,
                        ctx,
                        desktop,
                        &run,
                        dispatched_count,
                        idx,
                        remaining_delay_us,
                        active_elapsed_us,
                        remaining_rpc,
                    );
                }

                if pause_req {
                    let active_elapsed_us = active_elapsed_now(&rpc_wall_start, seg_clock_start_us);
                    let res = self.suspend_segment_on_pause_locked(
                        &mut state,
                        ctx,
                        desktop,
                        &run,
                        dispatched_count,
                        idx,
                        remaining_delay_us,
                        active_elapsed_us,
                    );
                    return (state, res);
                }

                if desktop.emergency().state() == EmergencyState::Locked {
                    if let Some(lease) = state.current_lease.as_mut() {
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                    }
                    self.wait_cv.notify_all();
                    if dispatched_count == 0 {
                        clear_or_reject_pending_receipt(&mut state, &run, ctx);
                        return (
                            state,
                            Err(PlatformError::new(
                                "RESOURCE_DENIED",
                                "桌面进入急停锁定状态，终止输入段。",
                            )),
                        );
                    }
                    let res = finish_uncertain_segment_locked(
                        &mut state,
                        desktop,
                        ctx,
                        run.receipt_key,
                        run.lease_id,
                        run.generation,
                        run.segment_id,
                        run.payload_hash,
                    );
                    return (state, res);
                }

                let rpc_elapsed = rpc_wall_start.elapsed();
                if rpc_elapsed >= rpc_deadline_budget {
                    if let Some(lease) = state.current_lease.as_mut() {
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                    }
                    self.wait_cv.notify_all();
                    if dispatched_count == 0 {
                        clear_or_reject_pending_receipt(&mut state, &run, ctx);
                        return (
                            state,
                            Err(
                                PlatformError::new("DEADLINE_EXCEEDED", "输入段执行超出时限。")
                                    .with_retry("afterQuery"),
                            ),
                        );
                    }
                    let res = finish_uncertain_segment_locked(
                        &mut state,
                        desktop,
                        ctx,
                        run.receipt_key,
                        run.lease_id,
                        run.generation,
                        run.segment_id,
                        run.payload_hash,
                    );
                    return (state, res);
                }

                if step_elapsed_us >= delay_us {
                    break;
                }

                let remaining_rpc = rpc_deadline_budget.saturating_sub(rpc_elapsed);
                let slice = Duration::from_micros(remaining_delay_us)
                    .min(remaining_rpc)
                    .min(Duration::from_millis(5));
                let (guard, _) = self.wait_cv.wait_timeout(state, slice).unwrap();
                state = guard;
            }

            {
                let target_check =
                    verify_target_expiry_locked(&state, &run.target_token).and_then(|()| {
                        match verify_target_checked(desktop, &run.target_token)? {
                            TargetStatus::Active => Ok(()),
                            _ => Err(PlatformError::target_lost("输入目标已失焦、失效或丢失。")),
                        }
                    });
                if let Err(err) = target_check {
                    let (pause_req, release_req, should_finalize_pausing) = {
                        let lease = state.current_lease.as_mut().unwrap();
                        let pause_req = lease.in_flight.as_ref().is_some_and(|f| f.pause_requested);
                        let release_req = lease.release_waiters > 0;
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                        let should_fin =
                            lease.state == LeaseState::Pausing && lease.pending_pause.is_none();
                        (pause_req, release_req, should_fin)
                    };
                    if dispatched_count == 0 {
                        clear_or_reject_pending_receipt(&mut state, &run, ctx);
                        if !release_req && (pause_req || should_finalize_pausing) {
                            finalize_or_uncertain_pausing_locked(
                                &mut state,
                                &run.lease_id,
                                run.generation,
                                ctx,
                                desktop,
                            );
                        }
                        self.wait_cv.notify_all();
                        return (state, Err(err));
                    }
                    let res = finish_uncertain_segment_locked(
                        &mut state,
                        desktop,
                        ctx,
                        run.receipt_key,
                        run.lease_id,
                        run.generation,
                        run.segment_id,
                        run.payload_hash,
                    );
                    self.wait_cv.notify_all();
                    return (state, res);
                }

                let would_exceed_held = {
                    let lease = state.current_lease.as_ref().unwrap();
                    match &event.action {
                        NativeInputAction::KeyDown(k) => {
                            let item = HeldInput::Key { key: k.clone() };
                            !lease.held_inputs.contains(&item)
                                && lease.held_inputs.len() >= MAX_HELD_INPUTS
                        }
                        NativeInputAction::PointerButtonDown(b) => {
                            let item = HeldInput::Button { button: *b };
                            !lease.held_inputs.contains(&item)
                                && lease.held_inputs.len() >= MAX_HELD_INPUTS
                        }
                        _ => false,
                    }
                };
                if would_exceed_held {
                    let (pause_req, release_req, should_finalize_pausing) = {
                        let lease = state.current_lease.as_mut().unwrap();
                        let pause_req = lease.in_flight.as_ref().is_some_and(|f| f.pause_requested);
                        let release_req = lease.release_waiters > 0;
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                        let should_fin =
                            lease.state == LeaseState::Pausing && lease.pending_pause.is_none();
                        (pause_req, release_req, should_fin)
                    };
                    if dispatched_count == 0 {
                        clear_or_reject_pending_receipt(&mut state, &run, ctx);
                        if !release_req && (pause_req || should_finalize_pausing) {
                            finalize_or_uncertain_pausing_locked(
                                &mut state,
                                &run.lease_id,
                                run.generation,
                                ctx,
                                desktop,
                            );
                        }
                        self.wait_cv.notify_all();
                        return (
                            state,
                            Err(PlatformError::new(
                                "LIMIT_EXCEEDED",
                                "同时按住的输入数量超过上限。",
                            )),
                        );
                    }
                    let res = finish_uncertain_segment_locked(
                        &mut state,
                        desktop,
                        ctx,
                        run.receipt_key,
                        run.lease_id,
                        run.generation,
                        run.segment_id,
                        run.payload_hash,
                    );
                    self.wait_cv.notify_all();
                    return (state, res);
                }
            }

            // Release coordinator lock around native event emission so concurrent pause/status/release never blocks.
            drop(state);
            let submit_outcome = submit_native_action(desktop, &run.target_token, &event.action);
            state = self.state.lock().unwrap();

            let lease_still_matches = state
                .current_lease
                .as_ref()
                .is_some_and(|l| l.lease_id == run.lease_id && l.generation == run.generation);
            if !lease_still_matches {
                return (
                    state,
                    Err(PlatformError::new(
                        "LEASE_EXPIRED",
                        "输入发送期间桌面租约已变更或撤销。",
                    )),
                );
            }

            match submit_outcome {
                Ok(SubmissionOutcome::Submitted) => {
                    dispatched_count += 1;
                    let lease = state.current_lease.as_mut().unwrap();
                    lease.input_target_token = Some(run.target_token.clone());
                    apply_action_to_lease_ledger(lease, &event.action);
                }
                Ok(SubmissionOutcome::Rejected) if dispatched_count == 0 => {
                    let (pause_req, release_req, should_finalize_pausing) = {
                        let lease = state.current_lease.as_mut().unwrap();
                        let pause_req = lease.in_flight.as_ref().is_some_and(|f| f.pause_requested);
                        let release_req = lease.release_waiters > 0;
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                        let should_fin =
                            lease.state == LeaseState::Pausing && lease.pending_pause.is_none();
                        (pause_req, release_req, should_fin)
                    };
                    let rec_res =
                        record_rejected_segment_locked(&mut state, &run, ctx, run.generation);
                    if !release_req && (pause_req || should_finalize_pausing) {
                        finalize_or_uncertain_pausing_locked(
                            &mut state,
                            &run.lease_id,
                            run.generation,
                            ctx,
                            desktop,
                        );
                    }
                    self.wait_cv.notify_all();
                    return (state, rec_res);
                }
                Err(err) if dispatched_count == 0 => {
                    let (pause_req, release_req, should_finalize_pausing) = {
                        let lease = state.current_lease.as_mut().unwrap();
                        let pause_req = lease.in_flight.as_ref().is_some_and(|f| f.pause_requested);
                        let release_req = lease.release_waiters > 0;
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                        let should_fin =
                            lease.state == LeaseState::Pausing && lease.pending_pause.is_none();
                        (pause_req, release_req, should_fin)
                    };
                    clear_or_reject_pending_receipt(&mut state, &run, ctx);
                    if !release_req && (pause_req || should_finalize_pausing) {
                        finalize_or_uncertain_pausing_locked(
                            &mut state,
                            &run.lease_id,
                            run.generation,
                            ctx,
                            desktop,
                        );
                    }
                    self.wait_cv.notify_all();
                    return (state, Err(err));
                }
                Ok(SubmissionOutcome::Rejected | SubmissionOutcome::Uncertain) | Err(_) => {
                    if let Some(lease) = state.current_lease.as_mut() {
                        lease.in_flight = None;
                        lease.suspended_segment = None;
                    }
                    let res = finish_uncertain_segment_locked(
                        &mut state,
                        desktop,
                        ctx,
                        run.receipt_key,
                        run.lease_id,
                        run.generation,
                        run.segment_id,
                        run.payload_hash,
                    );
                    self.wait_cv.notify_all();
                    return (state, res);
                }
            }
        }

        let now_us = desktop.clock().now_us();
        let (pause_req, release_req, should_finalize_pausing) = {
            let lease = state.current_lease.as_mut().unwrap();
            let pause_req = lease.in_flight.as_ref().is_some_and(|f| f.pause_requested);
            let release_req = lease.release_waiters > 0;
            lease.in_flight = None;
            lease.suspended_segment = None;
            lease.expires_at_us = now_us.saturating_add(LEASE_TTL_US);
            let should_fin = lease.state == LeaseState::Pausing && lease.pending_pause.is_none();
            (pause_req, release_req, should_fin)
        };

        let result = SegmentResult {
            segment_id: run.segment_id,
            state: SegmentState::Sent,
            cleanup: CleanupOutcome::NotRequired,
            agent_epoch: ctx.agent_epoch.clone(),
            pause_id: None,
        };
        if let Err(err) = result.validate() {
            self.wait_cv.notify_all();
            return (state, Err(err));
        }
        if let Err(err) = state.record_segment_receipt(
            run.receipt_key,
            SegmentReceipt {
                lease_id: run.lease_id.clone(),
                generation: run.generation,
                payload_hash: run.payload_hash,
                result: result.clone(),
            },
        ) {
            self.wait_cv.notify_all();
            return (state, Err(err));
        }

        if !release_req && (pause_req || should_finalize_pausing) {
            finalize_or_uncertain_pausing_locked(
                &mut state,
                &run.lease_id,
                run.generation,
                ctx,
                desktop,
            );
        }

        self.wait_cv.notify_all();
        (state, Ok(result))
    }

    #[allow(clippy::too_many_arguments)]
    fn suspend_segment_on_pause_locked(
        &self,
        state: &mut CoordinatorState,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        run: &SegmentRun,
        dispatched_count: usize,
        next_event_index: usize,
        remaining_delay_us: u64,
        active_elapsed_us: u64,
    ) -> Result<SegmentResult, PlatformError> {
        let cursor = SegmentCursor {
            segment_id: run.segment_id.clone(),
            payload_hash: run.payload_hash.clone(),
            event_count: run.events.len() as u64,
            next_event_index: next_event_index as u64,
            active_elapsed_us,
            remaining_delay_us,
        };
        let suspended = SuspendedSegment {
            receipt_key: run.receipt_key.clone(),
            segment_id: run.segment_id.clone(),
            payload_hash: run.payload_hash.clone(),
            target_token: run.target_token.clone(),
            events: run.events.clone(),
            next_event_index,
            remaining_delay_us,
            active_elapsed_us,
            dispatched_count,
            pause_cleanup: CleanupOutcome::NotRequired,
        };
        if let Some(lease) = state.current_lease.as_mut() {
            lease.in_flight = None;
        }
        let finalize_res = finalize_pause_locked(
            state,
            &run.lease_id,
            run.generation,
            ctx,
            desktop,
            Some(cursor),
            Some(suspended),
        );
        match finalize_res {
            Ok((_snap, Some(seg_result))) => {
                self.wait_cv.notify_all();
                Ok(seg_result)
            }
            Ok((snap, None)) => {
                self.wait_cv.notify_all();
                segment_result_from_pause_snapshot(&run.segment_id, &ctx.agent_epoch, &snap)
            }
            Err(err) => {
                if dispatched_count == 0 {
                    if let Some(lease) = state.current_lease.as_mut()
                        && lease.state == LeaseState::Pausing
                        && lease.pending_pause.is_none()
                    {
                        lease.state = LeaseState::Active;
                        lease.reason = None;
                    }
                    clear_or_reject_pending_receipt(state, run, ctx);
                    self.wait_cv.notify_all();
                    Err(err)
                } else {
                    let res = finish_uncertain_segment_locked(
                        state,
                        desktop,
                        ctx,
                        run.receipt_key.clone(),
                        run.lease_id.clone(),
                        run.generation,
                        run.segment_id.clone(),
                        run.payload_hash.clone(),
                    );
                    self.wait_cv.notify_all();
                    res
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn abort_segment_on_release<'a>(
        &'a self,
        mut state: MutexGuard<'a, CoordinatorState>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
        run: &SegmentRun,
        dispatched_count: usize,
        next_event_index: usize,
        remaining_delay_us: u64,
        active_elapsed_us: u64,
        wait_budget: Duration,
    ) -> (
        MutexGuard<'a, CoordinatorState>,
        Result<SegmentResult, PlatformError>,
    ) {
        if dispatched_count == 0 {
            if let Some(lease) = state
                .current_lease
                .as_mut()
                .filter(|l| l.lease_id == run.lease_id && l.generation == run.generation)
            {
                lease.in_flight = None;
                lease.release_pending = false;
                lease.suspended_segment = None;
            }
            clear_or_reject_pending_receipt(&mut state, run, ctx);
            self.wait_cv.notify_all();
            return (
                state,
                Err(PlatformError::new(
                    "LEASE_EXPIRED",
                    "输入段执行期间桌面租约被请求释放或关闭。",
                )),
            );
        }

        if let Some(lease) = state
            .current_lease
            .as_mut()
            .filter(|l| l.lease_id == run.lease_id && l.generation == run.generation)
        {
            lease.in_flight = None;
            lease.release_pending = true;
            lease.suspended_segment = Some(SuspendedSegment {
                receipt_key: run.receipt_key.clone(),
                segment_id: run.segment_id.clone(),
                payload_hash: run.payload_hash.clone(),
                target_token: run.target_token.clone(),
                events: run.events.clone(),
                next_event_index,
                remaining_delay_us,
                active_elapsed_us,
                dispatched_count,
                pause_cleanup: CleanupOutcome::NotRequired,
            });
        }
        self.wait_cv.notify_all();

        let wait_start = Instant::now();
        let max_wait = wait_budget.min(Duration::from_millis(500));
        while state.current_lease.as_ref().is_some_and(|l| {
            l.lease_id == run.lease_id && l.generation == run.generation && l.release_pending
        }) && wait_start.elapsed() < max_wait
        {
            let slice = max_wait
                .saturating_sub(wait_start.elapsed())
                .min(Duration::from_millis(5));
            let (guard, _) = self.wait_cv.wait_timeout(state, slice).unwrap();
            state = guard;
        }

        if let Some(res) = state
            .segment_receipts
            .get(&run.receipt_key)
            .filter(|receipt| {
                receipt.lease_id == run.lease_id
                    && receipt.generation == run.generation
                    && receipt.payload_hash == run.payload_hash
                    && receipt.result.state != SegmentState::Running
            })
            .map(|receipt| receipt.result.clone())
        {
            return (state, Ok(res));
        }

        if state
            .current_lease
            .as_ref()
            .is_some_and(|l| l.lease_id == run.lease_id && l.generation == run.generation)
        {
            if let Some(lease) = state.current_lease.as_mut() {
                lease.release_pending = false;
            }
            let res = finish_uncertain_segment_locked(
                &mut state,
                desktop,
                ctx,
                run.receipt_key.clone(),
                run.lease_id.clone(),
                run.generation,
                run.segment_id.clone(),
                run.payload_hash.clone(),
            );
            self.wait_cv.notify_all();
            return (state, res);
        }

        let uncertain_result = SegmentResult {
            segment_id: run.segment_id.clone(),
            state: SegmentState::Uncertain,
            cleanup: CleanupOutcome::Failed,
            agent_epoch: ctx.agent_epoch.clone(),
            pause_id: None,
        };
        let res = match uncertain_result.validate().and_then(|()| {
            state.record_segment_receipt(
                run.receipt_key.clone(),
                SegmentReceipt {
                    lease_id: run.lease_id.clone(),
                    generation: run.generation,
                    payload_hash: run.payload_hash.clone(),
                    result: uncertain_result.clone(),
                },
            )
        }) {
            Ok(()) => Ok(uncertain_result),
            Err(err) => Err(err),
        };
        self.wait_cv.notify_all();
        (state, res)
    }

    pub(crate) fn handle_segment_status(
        &self,
        params: &Map<String, Value>,
        ctx: &SessionContext,
        desktop: &dyn DesktopSession,
    ) -> Result<SegmentResult, PlatformError> {
        if params.len() != 2
            || !params.contains_key("segmentId")
            || !params.contains_key("payloadHash")
        {
            return Err(PlatformError::invalid_argument(
                "segment.status 参数必须仅包含 segmentId 与 payloadHash。",
            ));
        }
        let segment_id = require_non_empty_string(params, "segmentId")?;
        let payload_hash = require_sha256_hex(params, "payloadHash")?;

        let mut state = self.state.lock().unwrap();
        self.refresh_lease_expiry_locked(&mut state, desktop);
        let receipt_key = (ctx.workspace_id.clone(), segment_id);
        let Some(receipt) = state.segment_receipts.get(&receipt_key) else {
            return Err(PlatformError::invalid_argument(
                "未找到对应输入段的提交收据。",
            ));
        };
        if receipt.payload_hash != payload_hash {
            return Err(PlatformError::new(
                "REQUEST_CONFLICT",
                "segment.status 的 payloadHash 与已有收据冲突。",
            ));
        }
        Ok(receipt.result.clone())
    }
}

struct SegmentRun {
    lease_id: String,
    generation: u64,
    receipt_key: (String, String),
    segment_id: String,
    payload_hash: String,
    target_token: String,
    events: Vec<AtomicInputEvent>,
    start_index: usize,
    first_delay_override: Option<u64>,
    prior_active_elapsed_us: u64,
    dispatched_count: usize,
    fresh_submit: bool,
}

fn clear_or_reject_pending_receipt(
    state: &mut CoordinatorState,
    run: &SegmentRun,
    ctx: &SessionContext,
) {
    if run.fresh_submit {
        state.remove_segment_receipt(&run.receipt_key);
    } else {
        let _ = record_rejected_segment_locked(state, run, ctx, run.generation);
    }
}

fn record_rejected_segment_locked(
    state: &mut CoordinatorState,
    run: &SegmentRun,
    ctx: &SessionContext,
    generation: u64,
) -> Result<SegmentResult, PlatformError> {
    let rejected_result = SegmentResult {
        segment_id: run.segment_id.clone(),
        state: SegmentState::Rejected,
        cleanup: CleanupOutcome::NotRequired,
        agent_epoch: ctx.agent_epoch.clone(),
        pause_id: None,
    };
    rejected_result.validate()?;
    state.record_segment_receipt(
        run.receipt_key.clone(),
        SegmentReceipt {
            lease_id: run.lease_id.clone(),
            generation,
            payload_hash: run.payload_hash.clone(),
            result: rejected_result.clone(),
        },
    )?;
    Ok(rejected_result)
}

fn finalize_or_uncertain_pausing_locked(
    state: &mut CoordinatorState,
    lease_id: &str,
    generation: u64,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
) {
    if finalize_pause_locked(state, lease_id, generation, ctx, desktop, None, None).is_err()
        && let Some(lease) = state.current_lease.as_mut()
    {
        let cleanup = release_held_inputs(desktop, &mut lease.held_inputs);
        lease.state = LeaseState::Uncertain;
        lease.cleanup = cleanup;
        lease.pause_snapshot = None;
        lease.pending_pause = None;
        lease.suspended_segment = None;
        lease.pause_expires_at_us = None;
        lease.reason = Some("EFFECT_UNCERTAIN".to_owned());
    }
}

fn fail_resume_uncertain_locked(
    state: &mut CoordinatorState,
    ctx: &SessionContext,
    confirmation_grant: Option<String>,
    receipt_lookup_key: (String, String, String),
    lease_id: String,
    request_key: ResumeRequestKey,
    outcome: (CleanupOutcome, Vec<HeldInput>, String),
) -> Result<LeaseSnapshot, PlatformError> {
    let (cleanup, restored_held, resume_attempt_id) = outcome;
    if let Some(grant) = confirmation_grant {
        state.record_consumed_confirmation(grant);
    }
    let (suspended, lease_gen, snap) = {
        let lease = state.current_lease.as_mut().unwrap();
        let suspended = lease.suspended_segment.take();
        lease.held_inputs = restored_held;
        lease.state = LeaseState::Uncertain;
        lease.cleanup = cleanup;
        lease.pause_snapshot = None;
        lease.pending_pause = None;
        lease.pause_expires_at_us = None;
        lease.last_resume_attempt_id = Some(resume_attempt_id);
        lease.reason = Some("EFFECT_UNCERTAIN".to_owned());
        (suspended, lease.generation, lease.to_snapshot())
    };
    snap.validate()?;
    if let Some(seg) = suspended {
        let eff_cleanup = if cleanup != CleanupOutcome::NotRequired {
            cleanup
        } else {
            seg.pause_cleanup
        };
        let seg_res = SegmentResult {
            segment_id: seg.segment_id,
            state: SegmentState::Uncertain,
            cleanup: eff_cleanup,
            agent_epoch: ctx.agent_epoch.clone(),
            pause_id: None,
        };
        seg_res.validate()?;
        state.record_segment_receipt(
            seg.receipt_key,
            SegmentReceipt {
                lease_id: lease_id.clone(),
                generation: lease_gen,
                payload_hash: seg.payload_hash,
                result: seg_res,
            },
        )?;
    }
    state.record_resume_receipt(
        receipt_lookup_key,
        ResumeReceipt {
            lease_id,
            request: request_key,
            snapshot: snap.clone(),
        },
    )?;
    Ok(snap)
}

fn current_topology_version(desktop: &dyn DesktopSession) -> Result<String, PlatformError> {
    match desktop.topology() {
        Ok(t) => Ok(t.version().to_string()),
        #[cfg(feature = "test-authority")]
        Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => Ok(desktop
            .capabilities()
            .topology_version
            .map(|v| v.to_string())
            .unwrap_or_else(|| "1".to_owned())),
        Err(err) => Err(err),
    }
}

fn build_target_states(
    state: &CoordinatorState,
    targets: &[String],
    desktop: &dyn DesktopSession,
) -> Result<Vec<TargetState>, PlatformError> {
    let topology_version = current_topology_version(desktop)?;
    let mut target_states = Vec::with_capacity(targets.len());
    for token in targets {
        let bound = state
            .bound_targets
            .get(token)
            .ok_or_else(|| PlatformError::target_lost("租约绑定的目标令牌已不存在。"))?;
        let live_version = match desktop.targets().target_version(&bound.descriptor.token) {
            Ok(Some(v)) => v,
            Ok(None) => bound.descriptor.target_version.clone(),
            #[cfg(feature = "test-authority")]
            Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => {
                bound.descriptor.target_version.clone()
            }
            Err(err) => return Err(err),
        };
        target_states.push(TargetState {
            target_token: token.clone(),
            target_version: live_version,
            topology_version: topology_version.clone(),
        });
    }
    sort_target_states(&mut target_states);
    Ok(target_states)
}

fn finalize_pause_locked(
    state: &mut CoordinatorState,
    lease_id: &str,
    generation: u64,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
    cursor: Option<SegmentCursor>,
    mut suspended: Option<SuspendedSegment>,
) -> Result<(LeaseSnapshot, Option<SegmentResult>), PlatformError> {
    let (targets, pointer, input_target_token, mut held_before_release, lease_epoch, lease_id_str) = {
        let lease = require_active_owner_lease_mut(state, lease_id, generation, ctx)?;
        if !matches!(lease.state, LeaseState::Active | LeaseState::Pausing) {
            return Err(PlatformError::new(
                "LEASE_EXPIRED",
                "当前租约状态不允许暂停。",
            ));
        }
        (
            lease.targets.clone(),
            lease.pointer.clone(),
            lease.input_target_token.clone(),
            lease.held_inputs.clone(),
            lease.agent_epoch.clone(),
            lease.lease_id.clone(),
        )
    };

    sort_held_inputs(&mut held_before_release);
    let target_states = build_target_states(state, &targets, desktop)?;
    let next_pause_seq = state.next_pause_seq + 1;
    let pause_id = format!("pause-{next_pause_seq}");
    let (created_at, expires_at) = current_and_expiry_utc_timestamps(1_800);
    let snapshot_input_target = if cursor.is_some() {
        suspended
            .as_ref()
            .map(|s| s.target_token.clone())
            .or(input_target_token)
    } else {
        input_target_token
    };

    // Build and validate snapshot BEFORE mutating lease state or releasing held inputs so
    // any construction failure never wedges the lease in `Pausing`.
    let candidate_snapshot = PauseSnapshot::build(
        pause_id,
        lease_id_str,
        lease_epoch,
        generation,
        created_at,
        expires_at,
        cursor,
        held_before_release,
        pointer,
        target_states,
        snapshot_input_target,
    )?;

    state.next_pause_seq = next_pause_seq;
    let now_us = desktop.clock().now_us();
    let cleanup = {
        let lease = state.current_lease.as_mut().unwrap();
        release_held_inputs(desktop, &mut lease.held_inputs)
    };

    let seg_result = match cleanup {
        CleanupOutcome::Succeeded | CleanupOutcome::NotRequired => {
            {
                let lease = state.current_lease.as_mut().unwrap();
                lease.state = LeaseState::Paused;
                lease.cleanup = cleanup;
                lease.pause_snapshot = Some(candidate_snapshot.clone());
                lease.pending_pause = None;
                lease.pause_expires_at_us = Some(now_us.saturating_add(PAUSE_RETENTION_US));
                lease.expires_at_us = now_us.saturating_add(LEASE_TTL_US);
                lease.reason = None;
            }
            if let Some(mut seg) = suspended.take() {
                seg.pause_cleanup = cleanup;
                let seg_res = SegmentResult {
                    segment_id: seg.segment_id.clone(),
                    state: SegmentState::Paused,
                    cleanup,
                    agent_epoch: ctx.agent_epoch.clone(),
                    pause_id: Some(candidate_snapshot.pause_id),
                };
                seg_res.validate()?;
                state.record_segment_receipt(
                    seg.receipt_key.clone(),
                    SegmentReceipt {
                        lease_id: lease_id.to_owned(),
                        generation,
                        payload_hash: seg.payload_hash.clone(),
                        result: seg_res.clone(),
                    },
                )?;
                state.current_lease.as_mut().unwrap().suspended_segment = Some(seg);
                Some(seg_res)
            } else {
                state.current_lease.as_mut().unwrap().suspended_segment = None;
                None
            }
        }
        CleanupOutcome::Pending => {
            {
                let lease = state.current_lease.as_mut().unwrap();
                lease.state = LeaseState::Pausing;
                lease.cleanup = CleanupOutcome::Pending;
                lease.pause_snapshot = None;
                lease.pause_expires_at_us = None;
                lease.reason = Some("INPUT_RELEASE_PENDING".to_owned());
                lease.suspended_segment = None;
                lease.pending_pause = Some(PendingPause {
                    suspended_segment: suspended.clone(),
                });
            }
            if let Some(seg) = suspended {
                let seg_res = SegmentResult {
                    segment_id: seg.segment_id,
                    state: SegmentState::Running,
                    cleanup: CleanupOutcome::Pending,
                    agent_epoch: ctx.agent_epoch.clone(),
                    pause_id: None,
                };
                seg_res.validate()?;
                state.record_segment_receipt(
                    seg.receipt_key,
                    SegmentReceipt {
                        lease_id: lease_id.to_owned(),
                        generation,
                        payload_hash: seg.payload_hash,
                        result: seg_res.clone(),
                    },
                )?;
                Some(seg_res)
            } else {
                None
            }
        }
        CleanupOutcome::Failed => {
            {
                let lease = state.current_lease.as_mut().unwrap();
                lease.state = LeaseState::Uncertain;
                lease.cleanup = CleanupOutcome::Failed;
                lease.pause_snapshot = None;
                lease.pending_pause = None;
                lease.suspended_segment = None;
                lease.pause_expires_at_us = None;
                lease.reason = Some("INPUT_RELEASE_FAILED".to_owned());
            }
            if let Some(seg) = suspended {
                let seg_res = SegmentResult {
                    segment_id: seg.segment_id,
                    state: SegmentState::Uncertain,
                    cleanup: CleanupOutcome::Failed,
                    agent_epoch: ctx.agent_epoch.clone(),
                    pause_id: None,
                };
                seg_res.validate()?;
                state.record_segment_receipt(
                    seg.receipt_key,
                    SegmentReceipt {
                        lease_id: lease_id.to_owned(),
                        generation,
                        payload_hash: seg.payload_hash,
                        result: seg_res.clone(),
                    },
                )?;
                Some(seg_res)
            } else {
                None
            }
        }
    };

    let snapshot = state.current_lease.as_ref().unwrap().to_snapshot();
    snapshot.validate()?;
    Ok((snapshot, seg_result))
}

fn segment_result_from_pause_snapshot(
    segment_id: &str,
    agent_epoch: &str,
    lease_snap: &LeaseSnapshot,
) -> Result<SegmentResult, PlatformError> {
    let result = match lease_snap.state {
        LeaseState::Paused => SegmentResult {
            segment_id: segment_id.to_owned(),
            state: SegmentState::Paused,
            cleanup: lease_snap.cleanup,
            agent_epoch: agent_epoch.to_owned(),
            pause_id: lease_snap
                .pause_snapshot
                .as_ref()
                .map(|p| p.pause_id.clone()),
        },
        _ => SegmentResult {
            segment_id: segment_id.to_owned(),
            state: SegmentState::Uncertain,
            cleanup: lease_snap.cleanup,
            agent_epoch: agent_epoch.to_owned(),
            pause_id: None,
        },
    };
    result.validate()?;
    Ok(result)
}

#[allow(clippy::too_many_arguments)]
fn finish_uncertain_segment_locked(
    state: &mut CoordinatorState,
    desktop: &dyn DesktopSession,
    ctx: &SessionContext,
    receipt_key: (String, String),
    lease_id: String,
    generation: u64,
    segment_id: String,
    payload_hash: String,
) -> Result<SegmentResult, PlatformError> {
    let cleanup = {
        let lease = state.current_lease.as_mut().unwrap();
        lease.release_pending = false;
        let cleanup = release_held_inputs(desktop, &mut lease.held_inputs);
        lease.state = LeaseState::Uncertain;
        lease.cleanup = cleanup;
        lease.pause_snapshot = None;
        lease.pending_pause = None;
        lease.suspended_segment = None;
        lease.pause_expires_at_us = None;
        lease.reason = Some("EFFECT_UNCERTAIN".to_owned());
        cleanup
    };
    let uncertain_result = SegmentResult {
        segment_id,
        state: SegmentState::Uncertain,
        cleanup,
        agent_epoch: ctx.agent_epoch.clone(),
        pause_id: None,
    };
    uncertain_result.validate()?;
    state.record_segment_receipt(
        receipt_key,
        SegmentReceipt {
            lease_id,
            generation,
            payload_hash,
            result: uncertain_result.clone(),
        },
    )?;
    Ok(uncertain_result)
}

fn revoke_current_lease_locked(
    state: &mut CoordinatorState,
    desktop: &dyn DesktopSession,
    reason_str: &str,
) -> (u64, CleanupOutcome) {
    let mut lease = state.current_lease.take().unwrap();
    lease.in_flight = None;
    lease.release_pending = false;
    lease.release_waiters = 0;
    let unfinished = lease
        .suspended_segment
        .take()
        .or_else(|| lease.pending_pause.take().and_then(|p| p.suspended_segment));
    let old_generation = lease.generation;
    let cleanup = release_held_inputs(desktop, &mut lease.held_inputs);
    state.generation += 1;
    let new_generation = state.generation;
    lease.generation = new_generation;
    lease.cleanup = cleanup;
    lease.pause_snapshot = None;
    lease.pending_pause = None;
    lease.pause_expires_at_us = None;

    if let Some(seg) = unfinished {
        let result = if seg.dispatched_count > 0 {
            let eff_cleanup = if cleanup != CleanupOutcome::NotRequired {
                cleanup
            } else {
                seg.pause_cleanup
            };
            SegmentResult {
                segment_id: seg.segment_id,
                state: SegmentState::Uncertain,
                cleanup: eff_cleanup,
                agent_epoch: lease.agent_epoch.clone(),
                pause_id: None,
            }
        } else {
            SegmentResult {
                segment_id: seg.segment_id,
                state: SegmentState::Rejected,
                cleanup: CleanupOutcome::NotRequired,
                agent_epoch: lease.agent_epoch.clone(),
                pause_id: None,
            }
        };
        if result.validate().is_ok() {
            let _ = state.record_segment_receipt(
                seg.receipt_key,
                SegmentReceipt {
                    lease_id: lease.lease_id.clone(),
                    generation: old_generation,
                    payload_hash: seg.payload_hash,
                    result,
                },
            );
        }
    }

    if matches!(cleanup, CleanupOutcome::Failed | CleanupOutcome::Pending) {
        lease.state = LeaseState::Uncertain;
        lease.reason = Some(
            if cleanup == CleanupOutcome::Failed {
                "INPUT_RELEASE_FAILED"
            } else {
                "INPUT_RELEASE_PENDING"
            }
            .to_owned(),
        );
        state.record_historical_lease(&lease);
        state.current_lease = Some(lease);
    } else {
        lease.state = LeaseState::Released;
        lease.reason = Some(reason_str.to_owned());
        state.record_historical_lease(&lease);
    }
    (new_generation, cleanup)
}

fn verify_confirmation_grant(
    state: &CoordinatorState,
    grant: &str,
    lease_id: &str,
    generation: u64,
    pause_snapshot: &PauseSnapshot,
    resume_plan_hash: &str,
) -> Result<(), PlatformError> {
    if state.consumed_confirmations.contains(grant) {
        return Err(PlatformError::new(
            "RESOURCE_DENIED",
            "恢复确认授权已消耗或无效。",
        ));
    }
    if let Some(reg) = state.registered_confirmations.get(grant) {
        if reg.lease_id != lease_id
            || reg.pause_id != pause_snapshot.pause_id
            || reg.agent_epoch != pause_snapshot.agent_epoch
            || reg.generation != generation
            || reg.checkpoint_hash != pause_snapshot.checkpoint_hash
            || reg.resume_plan_hash != resume_plan_hash
            || reg.restore_target_token != pause_snapshot.input_target_token
        {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                "恢复确认授权与检查点或计划不匹配。",
            ));
        }
        return Ok(());
    }
    #[cfg(feature = "test-authority")]
    {
        let expected = format!(
            "test-confirm:{}:{}:{}",
            pause_snapshot.pause_id, pause_snapshot.checkpoint_hash, resume_plan_hash
        );
        if grant == expected {
            return Ok(());
        }
    }
    Err(PlatformError::new(
        "RESOURCE_DENIED",
        "恢复确认授权未登记或无效。",
    ))
}

fn apply_action_to_lease_ledger(lease: &mut ActiveLease, action: &NativeInputAction) {
    match action {
        NativeInputAction::PointerMovePhysical(pt) => {
            let clamped = PhysicalPoint {
                x: pt.x.clamp(MIN_COORD_I64, MAX_COORD_I64),
                y: pt.y.clamp(MIN_COORD_I64, MAX_COORD_I64),
            };
            lease.physical_pointer = clamped;
            lease.pointer = PausePoint::Physical(clamped);
        }
        NativeInputAction::PointerMoveDisplay(pt) => {
            let mut bounded = pt.clone();
            bounded.x = bounded.x.clamp(0.0, MAX_COORD_F64);
            bounded.y = bounded.y.clamp(0.0, MAX_COORD_F64);
            lease.pointer = PausePoint::Display(bounded);
        }
        NativeInputAction::PointerDelta { dx, dy } => {
            lease.physical_pointer.x = lease
                .physical_pointer
                .x
                .saturating_add(dx.round() as i64)
                .clamp(MIN_COORD_I64, MAX_COORD_I64);
            lease.physical_pointer.y = lease
                .physical_pointer
                .y
                .saturating_add(dy.round() as i64)
                .clamp(MIN_COORD_I64, MAX_COORD_I64);
            match &mut lease.pointer {
                PausePoint::Physical(pt) => {
                    *pt = lease.physical_pointer;
                }
                PausePoint::Display(pt) => {
                    pt.x = (pt.x + dx).clamp(0.0, MAX_COORD_F64);
                    pt.y = (pt.y + dy).clamp(0.0, MAX_COORD_F64);
                }
            }
        }
        NativeInputAction::PointerButtonDown(button) => {
            let item = HeldInput::Button { button: *button };
            if !lease.held_inputs.contains(&item) {
                lease.held_inputs.push(item);
                sort_held_inputs(&mut lease.held_inputs);
            }
        }
        NativeInputAction::PointerButtonUp(button) => {
            let item = HeldInput::Button { button: *button };
            lease.held_inputs.retain(|existing| existing != &item);
        }
        NativeInputAction::KeyDown(key) => {
            let item = HeldInput::Key { key: key.clone() };
            if !lease.held_inputs.contains(&item) {
                lease.held_inputs.push(item);
                sort_held_inputs(&mut lease.held_inputs);
            }
        }
        NativeInputAction::KeyUp(key) => {
            let item = HeldInput::Key { key: key.clone() };
            lease.held_inputs.retain(|existing| existing != &item);
        }
        NativeInputAction::UnicodeText(_) | NativeInputAction::PointerScroll { .. } => {}
    }
}

fn require_active_owner_lease_mut<'a>(
    state: &'a mut CoordinatorState,
    lease_id: &str,
    generation: u64,
    ctx: &SessionContext,
) -> Result<&'a mut ActiveLease, PlatformError> {
    if let Some(lease) = state.current_lease.as_mut()
        && lease.lease_id == lease_id
    {
        if lease.owner_workspace != ctx.workspace_id || lease.owner_session != ctx.session_id {
            return Err(PlatformError::new(
                "ACCESS_DENIED",
                "当前连接不是该桌面租约的拥有者。",
            ));
        }
        if lease.generation != generation {
            return Err(PlatformError::new(
                "LEASE_EXPIRED",
                "桌面租约代次不匹配或已过期。",
            ));
        }
        return Ok(lease);
    }

    if let Some(hist) = state.historical_leases.get(lease_id)
        && hist.owner_workspace != ctx.workspace_id
    {
        return Err(PlatformError::new(
            "ACCESS_DENIED",
            "无权操作其他工作空间的桌面租约。",
        ));
    }

    Err(PlatformError::new(
        "LEASE_EXPIRED",
        "指定的桌面租约不存在、已释放或已过期。",
    ))
}

fn verify_target_expiry_locked(state: &CoordinatorState, token: &str) -> Result<(), PlatformError> {
    let Some(bound) = state.bound_targets.get(token) else {
        return Err(PlatformError::new(
            "RESOURCE_DENIED",
            format!("目标令牌未绑定：{token}"),
        ));
    };
    let exp_secs = parse_utc_timestamp(&bound.descriptor.expires_at)?;
    if exp_secs <= current_utc_secs() {
        return Err(PlatformError::target_lost(format!(
            "目标授权已超过有效期：{token}"
        )));
    }
    Ok(())
}

fn verify_targets_bound_and_usable(
    state: &CoordinatorState,
    workspace_id: &str,
    targets: &[String],
    desktop: &dyn DesktopSession,
) -> Result<(), PlatformError> {
    let now_secs = current_utc_secs();
    for token in targets {
        let Some(bound) = state.bound_targets.get(token) else {
            return Err(PlatformError::new(
                "RESOURCE_DENIED",
                format!("目标令牌未绑定：{token}"),
            ));
        };
        if bound.owner_workspace != workspace_id {
            return Err(PlatformError::new(
                "ACCESS_DENIED",
                "目标令牌不属于当前工作空间。",
            ));
        }
        let exp_secs = parse_utc_timestamp(&bound.descriptor.expires_at)?;
        if exp_secs <= now_secs {
            return Err(PlatformError::target_lost(format!(
                "目标授权已超过有效期：{token}"
            )));
        }
        let status = verify_target_checked(desktop, token)?;
        if matches!(status, TargetStatus::Lost | TargetStatus::Expired) {
            return Err(PlatformError::target_lost(format!(
                "目标已失效或丢失：{token}"
            )));
        }
    }
    Ok(())
}

fn bind_target_selection(
    desktop: &dyn DesktopSession,
    selection_grant: &str,
) -> Result<TargetDescriptor, PlatformError> {
    match desktop.targets().bind_selection(selection_grant) {
        Ok(desc) => Ok(desc),
        #[cfg(feature = "test-authority")]
        Err(err)
            if err.code == "CAPABILITY_NOT_IMPLEMENTED"
                && selection_grant.starts_with("test-grant-") =>
        {
            let suffix = &selection_grant["test-grant-".len()..];
            if suffix.is_empty() {
                return Err(PlatformError::invalid_argument(
                    "测试目标授权标识后缀不能为空。",
                ));
            }
            let (kind, display_suffix) = if let Some(rest) = suffix.strip_prefix("desktop-") {
                (TargetKind::Desktop, rest)
            } else if let Some(rest) = suffix.strip_prefix("display-") {
                (TargetKind::Display, rest)
            } else if let Some(rest) = suffix.strip_prefix("region-") {
                (TargetKind::Region, rest)
            } else {
                (TargetKind::Window, suffix)
            };
            if display_suffix.is_empty() {
                return Err(PlatformError::invalid_argument(
                    "测试目标令牌后缀不能为空。",
                ));
            }
            // Preserve the full suffix in TargetToken so distinct kinds/grants cannot collide.
            Ok(TargetDescriptor {
                token: TargetToken(format!("target-{suffix}")),
                display_name: format!("Test Target ({display_suffix})"),
                kind,
                target_version: "1".to_owned(),
                expires_at: "2099-01-01T00:00:00Z".to_owned(),
            })
        }
        Err(err) => Err(err),
    }
}

fn verify_target_checked(
    desktop: &dyn DesktopSession,
    target_token: &str,
) -> Result<TargetStatus, PlatformError> {
    let token = TargetToken(target_token.to_owned());
    match desktop.targets().verify_target(&token) {
        Ok(status) => Ok(status),
        #[cfg(feature = "test-authority")]
        Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => Ok(TargetStatus::Active),
        Err(err) => Err(err),
    }
}

fn focus_target_checked(
    desktop: &dyn DesktopSession,
    target_token: &str,
) -> Result<(), PlatformError> {
    let token = TargetToken(target_token.to_owned());
    match desktop.targets().focus_target(&token) {
        Ok(()) => Ok(()),
        #[cfg(feature = "test-authority")]
        Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => Ok(()),
        Err(err) => Err(err),
    }
}

fn submit_native_action(
    desktop: &dyn DesktopSession,
    target_token: &str,
    action: &NativeInputAction,
) -> Result<SubmissionOutcome, PlatformError> {
    let token = TargetToken(target_token.to_owned());
    match desktop.emitter().submit_event(&token, action) {
        Ok(outcome) => Ok(outcome),
        #[cfg(feature = "test-authority")]
        Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => Ok(SubmissionOutcome::Submitted),
        Err(err) => Err(err),
    }
}

fn release_held_inputs(
    desktop: &dyn DesktopSession,
    held_inputs: &mut Vec<HeldInput>,
) -> CleanupOutcome {
    if held_inputs.is_empty() {
        return CleanupOutcome::NotRequired;
    }
    let snapshot = held_inputs.clone();
    let outcome = match desktop.emitter().release_held(&snapshot) {
        Ok(res) => res,
        #[cfg(feature = "test-authority")]
        Err(err) if err.code == "CAPABILITY_NOT_IMPLEMENTED" => CleanupOutcome::Succeeded,
        Err(_) => CleanupOutcome::Failed,
    };
    if matches!(
        outcome,
        CleanupOutcome::Succeeded | CleanupOutcome::NotRequired
    ) {
        held_inputs.clear();
    }
    outcome
}

fn validate_identifier(value: &str, field: &str) -> Result<(), PlatformError> {
    let count = value.chars().count();
    if !(1..=256).contains(&count) || value.contains('\0') {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 必须为 1..=256 个字符且不含空字符。"
        )));
    }
    Ok(())
}

fn require_non_empty_string(
    params: &Map<String, Value>,
    field: &str,
) -> Result<String, PlatformError> {
    let Some(val) = params.get(field).and_then(Value::as_str) else {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 必须为非空字符串。"
        )));
    };
    validate_identifier(val, field)?;
    Ok(val.to_owned())
}

fn require_sha256_hex(params: &Map<String, Value>, field: &str) -> Result<String, PlatformError> {
    let Some(val) = params.get(field).and_then(Value::as_str) else {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 必须为 64 位小写十六进制字符串。"
        )));
    };
    if val.len() != 64
        || !val
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 必须为 64 位小写十六进制字符串。"
        )));
    }
    Ok(val.to_owned())
}

fn require_u64_in_range(
    params: &Map<String, Value>,
    field: &str,
    min: u64,
    max: u64,
) -> Result<u64, PlatformError> {
    let Some(val) = params.get(field).and_then(Value::as_u64) else {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 必须为 {min}..={max} 范围内的整数。"
        )));
    };
    if !(min..=max).contains(&val) {
        return Err(PlatformError::invalid_argument(format!(
            "字段 {field} 超出允许范围 {min}..={max}。"
        )));
    }
    Ok(val)
}

fn current_utc_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_secs()).unwrap_or(1_761_955_200))
        .unwrap_or(1_761_955_200)
}

fn current_and_expiry_utc_timestamps(ttl_secs: i64) -> (String, String) {
    let now_secs = current_utc_secs();
    let exp_secs = now_secs.saturating_add(ttl_secs.max(1));
    (
        format_utc_timestamp(now_secs),
        format_utc_timestamp(exp_secs),
    )
}
