use deskweave_platform_api::DesktopSession;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Serialize;
use serde_json::{Map, Value};
use std::io;
use std::process;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::lease::LeaseCoordinator;
use crate::native::{
    DeadlineIo, DeadlineReader, DeadlineWriter, SessionListener, SessionRead, SessionStream,
    SessionWrite, StopSignal,
};
use crate::protocol::{
    AttachChallenge, AttachHello, AttachReady, AttachRegistration, ConfirmationChallenge,
    ConfirmationClaim, ConfirmationError, ConfirmationRegister, ConfirmationRegistered, ErrorBody,
    ErrorResponse, MAX_CONTROL_FRAME, MAX_FRAME, Pong, SuccessResponse, VERSION,
    attach_ready_proof, constant_time_eq, generate_session_id, is_hex, looks_like_protocol_version,
    parse_object, parse_strict_object, read_frame, sha256_hex, valid_envelope_id,
    valid_method_name, valid_workspace_id, write_frame,
};

pub(crate) const MAX_CLIENTS: usize = 32;
pub(crate) const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
pub(crate) const FRAME_READ_TIMEOUT: Duration = Duration::from_secs(10);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_DEADLINE_MS: u64 = 86_400_000;
const MAX_IN_FLIGHT_PER_SESSION: usize = 8;
const MAX_IN_FLIGHT_CONTROL: usize = 4;

pub(crate) struct ServerConfig {
    pub(crate) agent_epoch: String,
    pub(crate) scope: String,
    pub(crate) owner_identity: [String; 2],
    pub(crate) verification_key: VerifyingKey,
    pub(crate) idle_grace: Duration,
    pub(crate) handshake_timeout: Duration,
    pub(crate) frame_read_timeout: Duration,
    pub(crate) max_clients: usize,
    pub(crate) desktop: Arc<dyn DesktopSession>,
}

#[derive(Clone)]
pub(crate) struct SessionContext {
    pub(crate) agent_epoch: String,
    pub(crate) session_id: String,
    pub(crate) workspace_id: String,
}

struct ServerState {
    active_sessions: usize,
    total_connections: usize,
    attached_threads: usize,
    draining: bool,
    idle_since: Option<Instant>,
}

struct AttachedThreadGuard {
    shared: Arc<Shared>,
}

impl Drop for AttachedThreadGuard {
    fn drop(&mut self) {
        let mut state = self.shared.state.lock().unwrap();
        state.attached_threads -= 1;
        self.shared.changed.notify_all();
    }
}

struct Shared {
    config: ServerConfig,
    state: Mutex<ServerState>,
    changed: Condvar,
    stop: StopSignal,
    leases: Arc<LeaseCoordinator>,
}

impl Shared {
    fn is_draining(&self) -> bool {
        self.state.lock().unwrap().draining
    }
}

struct ConnectionSlot {
    shared: Arc<Shared>,
    authenticated: bool,
}

impl ConnectionSlot {
    fn promote(&mut self) -> bool {
        let mut state = self.shared.state.lock().unwrap();
        if state.draining {
            return false;
        }
        state.active_sessions += 1;
        state.idle_since = None;
        self.authenticated = true;
        self.shared.changed.notify_all();
        true
    }
}

impl Drop for ConnectionSlot {
    fn drop(&mut self) {
        let mut state = self.shared.state.lock().unwrap();
        if self.authenticated {
            state.active_sessions -= 1;
            if state.active_sessions == 0 {
                state.idle_since = Some(Instant::now());
            }
        }
        state.total_connections -= 1;
        self.shared.changed.notify_all();
    }
}

fn serialize_error(reply_to: &str, code: &str, message: &str, retry: &str) -> io::Result<Vec<u8>> {
    let response = ErrorResponse {
        protocol_version: "1.0",
        kind: "response",
        reply_to,
        ok: false,
        error: ErrorBody {
            code,
            message,
            retry,
        },
    };
    serde_json::to_vec(&response).map_err(io::Error::other)
}

fn serialize_success<T: Serialize>(reply_to: &str, result: T) -> io::Result<Vec<u8>> {
    let response = SuccessResponse {
        protocol_version: "1.0",
        kind: "response",
        reply_to,
        ok: true,
        result,
    };
    let payload = serde_json::to_vec(&response).map_err(io::Error::other)?;
    if payload.len() > MAX_CONTROL_FRAME {
        return serialize_error(
            reply_to,
            "LIMIT_EXCEEDED",
            "Agent 响应超出控制帧预算。",
            "never",
        );
    }
    Ok(payload)
}

fn serialize_result<T: Serialize>(
    id: &str,
    res: Result<T, deskweave_platform_api::PlatformError>,
) -> io::Result<Vec<u8>> {
    match res {
        Ok(value) => serialize_success(id, value),
        Err(error) => serialize_error(id, error.code, &error.message, error.retry),
    }
}

#[derive(Clone)]
struct ConfirmationAuthority {
    verification_key: VerifyingKey,
    owner_identity: [String; 2],
    scope: String,
}

impl ConfirmationAuthority {
    fn from_config(config: &ServerConfig) -> Self {
        Self {
            verification_key: config.verification_key,
            owner_identity: config.owner_identity.clone(),
            scope: config.scope.clone(),
        }
    }
}

struct ValidatedRequest {
    id: String,
    method: String,
    deadline_ms: u64,
    write_budget: Duration,
    params: Map<String, Value>,
}

enum EnvelopeCheck {
    Valid(ValidatedRequest),
    EarlyResponse(Vec<u8>, Duration),
}

fn validate_request_envelope(
    map: &Map<String, Value>,
    ctx: &SessionContext,
) -> io::Result<EnvelopeCheck> {
    let reply_to = map
        .get("id")
        .and_then(Value::as_str)
        .filter(|value| valid_envelope_id(value))
        .unwrap_or("invalid");

    if map.len() != 8
        || !map.contains_key("protocolVersion")
        || !map.contains_key("kind")
        || !map.contains_key("id")
        || !map.contains_key("session")
        || !map.contains_key("workspace")
        || !map.contains_key("method")
        || !map.contains_key("deadlineMs")
        || !map.contains_key("params")
    {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求信封字段无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let Some(version) = map.get("protocolVersion").and_then(Value::as_str) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "协议版本类型无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    if version != "1.0" {
        let code = if looks_like_protocol_version(version) {
            "PROTOCOL_UNSUPPORTED"
        } else {
            "INVALID_ARGUMENT"
        };
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, code, "Agent 控制协议版本不受支持。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let Some(id) = map.get("id").and_then(Value::as_str) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(session) = map.get("session").and_then(Value::as_str) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "会话标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(workspace) = map.get("workspace").and_then(Value::as_str) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "工作空间标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(method) = map.get("method").and_then(Value::as_str) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "方法名称无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(deadline_ms) = map
        .get("deadlineMs")
        .and_then(Value::as_u64)
        .filter(|ms| (1..=MAX_DEADLINE_MS).contains(ms))
    else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求时限无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(params) = map.get("params").and_then(Value::as_object) else {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(
                reply_to,
                "INVALID_ARGUMENT",
                "请求参数必须为对象。",
                "never",
            )?,
            WRITE_TIMEOUT,
        ));
    };

    if !valid_envelope_id(id)
        || !valid_envelope_id(session)
        || !valid_workspace_id(workspace)
        || !valid_method_name(method)
    {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求信封格式无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let write_budget = Duration::from_millis(deadline_ms).min(WRITE_TIMEOUT);

    if !constant_time_eq(session.as_bytes(), ctx.session_id.as_bytes())
        || workspace != ctx.workspace_id
    {
        return Ok(EnvelopeCheck::EarlyResponse(
            serialize_error(
                id,
                "ACCESS_DENIED",
                "请求会话或工作空间与已认证连接不匹配。",
                "never",
            )?,
            write_budget,
        ));
    }

    Ok(EnvelopeCheck::Valid(ValidatedRequest {
        id: id.to_owned(),
        method: method.to_owned(),
        deadline_ms,
        write_budget,
        params: params.clone(),
    }))
}

fn execute_validated_request(
    req: &ValidatedRequest,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
    leases: &LeaseCoordinator,
    is_draining: &dyn Fn() -> bool,
    session_closing: &AtomicBool,
) -> io::Result<Vec<u8>> {
    let id = &req.id;
    let params = &req.params;
    match req.method.as_str() {
        "desktop.capabilities" => {
            if !params.is_empty() {
                return serialize_error(
                    id,
                    "INVALID_ARGUMENT",
                    "desktop.capabilities 不接受额外参数。",
                    "never",
                );
            }
            let snapshot = desktop.capabilities();
            if snapshot.validate().is_err() {
                return serialize_error(
                    id,
                    "INTERNAL_ERROR",
                    "Desktop Agent 生成的平台能力摘要不符合契约。",
                    "never",
                );
            }
            serialize_success(id, snapshot)
        }
        "desktop.topology" => {
            if !params.is_empty() {
                return serialize_error(
                    id,
                    "INVALID_ARGUMENT",
                    "desktop.topology 不接受额外参数。",
                    "never",
                );
            }
            match desktop.topology() {
                Ok(topology) => {
                    if topology.validate().is_err() {
                        return serialize_error(
                            id,
                            "INTERNAL_ERROR",
                            "Desktop Agent 生成的显示拓扑不符合契约。",
                            "never",
                        );
                    }
                    serialize_success(id, topology)
                }
                Err(error) => serialize_error(id, error.code, &error.message, error.retry),
            }
        }
        "target.bind" => serialize_result(id, leases.handle_target_bind(params, ctx, desktop)),
        "lease.acquire" => serialize_result(
            id,
            leases.handle_lease_acquire_with_close(
                params,
                ctx,
                desktop,
                is_draining,
                session_closing,
                req.deadline_ms,
            ),
        ),
        "lease.renew" => serialize_result(id, leases.handle_lease_renew(params, ctx, desktop)),
        "lease.release" => serialize_result(
            id,
            leases.handle_lease_release(params, ctx, desktop, req.deadline_ms),
        ),
        "lease.pause" => serialize_result(
            id,
            leases.handle_lease_pause(params, ctx, desktop, req.deadline_ms),
        ),
        "lease.status" => serialize_result(id, leases.handle_lease_status(params, ctx, desktop)),
        "lease.resume" => serialize_result(
            id,
            leases.handle_lease_resume(params, ctx, desktop, req.deadline_ms),
        ),
        "segment.submit" => serialize_result(
            id,
            leases.handle_segment_submit(params, ctx, desktop, req.deadline_ms),
        ),
        "segment.status" => {
            serialize_result(id, leases.handle_segment_status(params, ctx, desktop))
        }
        _ => serialize_error(id, "METHOD_UNAVAILABLE", "此 Agent 方法尚未实现。", "never"),
    }
}

fn read_session_frame_bytes<R: SessionRead>(
    reader: &mut R,
    max_bytes: usize,
    assembly_timeout: Duration,
    stop: &StopSignal,
) -> io::Result<Option<Vec<u8>>> {
    use std::io::Read as _;

    let mut prefix = [0_u8; 4];
    {
        let mut idle_io = DeadlineReader::new(reader, None, stop);
        loop {
            match idle_io.read(&mut prefix[..1]) {
                Ok(0) => return Ok(None),
                Ok(_) => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
    }
    let deadline = Some(Instant::now() + assembly_timeout);
    let mut framed_io = DeadlineReader::new(reader, deadline, stop);
    framed_io.read_exact(&mut prefix[1..])?;
    let size = u32::from_le_bytes(prefix) as usize;
    if !(1..=max_bytes).contains(&size) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut payload = vec![0_u8; size];
    framed_io.read_exact(&mut payload)?;
    Ok(Some(payload))
}

struct SessionWriter<W: SessionWrite> {
    inner: Mutex<W>,
    broken: AtomicBool,
    session_stop: StopSignal,
}

impl<W: SessionWrite> SessionWriter<W> {
    fn new(writer: W, session_stop: StopSignal) -> Self {
        Self {
            inner: Mutex::new(writer),
            broken: AtomicBool::new(false),
            session_stop,
        }
    }

    fn poison_and_stop(&self) {
        self.broken.store(true, Ordering::Release);
        self.session_stop.trigger();
    }

    fn write_framed(&self, payload: &[u8], budget: Duration) -> io::Result<()> {
        use std::io::Write as _;

        let mut guard = self.inner.lock().unwrap();
        if self.broken.load(Ordering::Acquire) || self.session_stop.is_triggered() {
            return Err(io::ErrorKind::BrokenPipe.into());
        }
        let mut frame = Vec::with_capacity(4 + payload.len());
        frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        frame.extend_from_slice(payload);
        let deadline = Some(Instant::now() + budget);
        let res = DeadlineWriter::new(&mut *guard, deadline, &self.session_stop).write_all(&frame);
        if res.is_err() {
            self.poison_and_stop();
        }
        res
    }

    fn write_json(
        &self,
        value: &impl Serialize,
        max_bytes: usize,
        budget: Duration,
    ) -> io::Result<()> {
        let payload = serde_json::to_vec(value)?;
        if !(1..=max_bytes).contains(&payload.len()) {
            return Err(io::ErrorKind::InvalidData.into());
        }
        self.write_framed(&payload, budget)
    }
}

struct ConfirmationServices<'a, W: SessionWrite> {
    ctx: &'a SessionContext,
    authority: &'a ConfirmationAuthority,
    desktop: &'a dyn DesktopSession,
    leases: &'a LeaseCoordinator,
    writer: &'a SessionWriter<W>,
}

fn handle_confirmation_register<W: SessionWrite>(
    raw: &[u8],
    expected_nonce: Option<[u8; 32]>,
    svc: &ConfirmationServices<'_, W>,
) -> io::Result<()> {
    let send_err = |code: &str, message: &str, retry: &str| {
        svc.writer.write_json(
            &ConfirmationError {
                kind: "agent.confirmation.error",
                code,
                message,
                retry,
            },
            MAX_CONTROL_FRAME,
            WRITE_TIMEOUT,
        )
    };

    if raw.len() > MAX_FRAME * 2 {
        return send_err("INVALID_ARGUMENT", "确认登记请求超出大小限制。", "never");
    }
    let Ok(req) = parse_object::<ConfirmationRegister>(raw) else {
        return send_err("INVALID_ARGUMENT", "确认登记请求格式无效。", "never");
    };
    if req.kind != "agent.confirmation.register" {
        return send_err("INVALID_ARGUMENT", "确认登记请求类型无效。", "never");
    }
    if req.version != VERSION {
        return send_err(
            "PROTOCOL_UNSUPPORTED",
            "确认登记协议版本不受支持。",
            "never",
        );
    }
    let Some(nonce) = expected_nonce else {
        return send_err(
            "RESOURCE_DENIED",
            "缺少有效的确认登记挑战或挑战已使用。",
            "never",
        );
    };
    if !(1..=MAX_FRAME).contains(&req.claim.len()) {
        return send_err("INVALID_ARGUMENT", "确认声明长度超出允许范围。", "never");
    }
    let Ok(signature) = Signature::from_slice(&req.signature) else {
        return send_err("ACCESS_DENIED", "确认声明签名格式无效。", "never");
    };
    if svc
        .authority
        .verification_key
        .verify_strict(req.claim.as_bytes(), &signature)
        .is_err()
    {
        return send_err("ACCESS_DENIED", "确认声明宿主签名验证失败。", "never");
    }
    let Ok(claim) = parse_object::<ConfirmationClaim>(req.claim.as_bytes()) else {
        return send_err("INVALID_ARGUMENT", "确认声明内容格式无效。", "never");
    };
    if claim.kind != "deskweave/agent-confirmation/v1"
        || claim.agent_pid != process::id()
        || claim.agent_epoch != svc.ctx.agent_epoch
        || claim.owner != svc.authority.owner_identity
        || claim.scope != svc.authority.scope
        || claim.workspace_id != svc.ctx.workspace_id
        || claim.nonce != nonce
    {
        return send_err(
            "ACCESS_DENIED",
            "确认声明的进程、工作空间、所有者或一次性挑战不匹配。",
            "never",
        );
    }

    match svc
        .leases
        .register_host_confirmation(svc.ctx, svc.desktop, &claim)
    {
        Ok((grant, expires_in_ms)) => svc.writer.write_json(
            &ConfirmationRegistered {
                kind: "agent.confirmation.registered",
                confirmation_grant: &grant,
                expires_in_ms,
            },
            MAX_CONTROL_FRAME,
            WRITE_TIMEOUT,
        ),
        Err(err) => send_err(err.code, &err.message, err.retry),
    }
}

struct InFlightGuard(Arc<AtomicUsize>);

impl Drop for InFlightGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

fn try_acquire_worker_slot(counter: &Arc<AtomicUsize>, limit: usize) -> Option<InFlightGuard> {
    if counter.load(Ordering::Acquire) >= limit {
        return None;
    }
    counter.fetch_add(1, Ordering::AcqRel);
    Some(InFlightGuard(Arc::clone(counter)))
}

#[cfg(test)]
pub(crate) fn serve_session<S: SessionStream + 'static>(
    stream: S,
    ctx: &SessionContext,
    desktop: Arc<dyn DesktopSession>,
    frame_read_timeout: Duration,
    stop: &StopSignal,
) -> io::Result<()> {
    let dummy_key = ed25519_dalek::SigningKey::from_bytes(&[0_u8; 32]).verifying_key();
    let authority = ConfirmationAuthority {
        verification_key: dummy_key,
        owner_identity: ["test".to_owned(), "test".to_owned()],
        scope: "test".to_owned(),
    };
    let leases = Arc::new(LeaseCoordinator::new());
    let is_draining: Arc<dyn Fn() -> bool + Send + Sync> = Arc::new(|| false);
    serve_session_with_coordinator(
        stream,
        ctx,
        &authority,
        desktop,
        leases,
        is_draining,
        frame_read_timeout,
        stop,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
fn serve_session_with_coordinator<S: SessionStream + 'static>(
    stream: S,
    ctx: &SessionContext,
    authority: &ConfirmationAuthority,
    desktop: Arc<dyn DesktopSession>,
    leases: Arc<LeaseCoordinator>,
    is_draining: Arc<dyn Fn() -> bool + Send + Sync>,
    frame_read_timeout: Duration,
    stop: &StopSignal,
    slot: Option<ConnectionSlot>,
) -> io::Result<()> {
    let session_stop = stop.child()?;
    let (mut reader, writer) = stream.split()?;
    let writer = Arc::new(SessionWriter::new(writer, session_stop.clone()));
    let session_closing = Arc::new(AtomicBool::new(false));
    let in_flight_blocking = Arc::new(AtomicUsize::new(0));
    let in_flight_control = Arc::new(AtomicUsize::new(0));
    let mut pending_confirm_nonce: Option<[u8; 32]> = None;
    let mut workers: Vec<thread::JoinHandle<()>> = Vec::new();

    let loop_result: io::Result<()> = loop {
        let raw = match read_session_frame_bytes(
            &mut reader,
            MAX_CONTROL_FRAME,
            frame_read_timeout,
            &session_stop,
        ) {
            Ok(Some(raw)) => raw,
            Ok(None) => break Ok(()),
            Err(err) => break Err(err),
        };
        let map = match parse_strict_object(&raw) {
            Ok(map) => map,
            Err(err) => break Err(err),
        };

        match map.get("kind").and_then(Value::as_str) {
            Some("agent.ping") => {
                if map.len() != 2
                    || map.get("agentEpoch").and_then(Value::as_str)
                        != Some(ctx.agent_epoch.as_str())
                {
                    break Err(io::ErrorKind::PermissionDenied.into());
                }
                if let Err(err) = writer.write_json(
                    &Pong {
                        kind: "agent.pong",
                        agent_epoch: &ctx.agent_epoch,
                    },
                    MAX_CONTROL_FRAME,
                    WRITE_TIMEOUT,
                ) {
                    break Err(err);
                }
            }
            Some("agent.confirmation.challenge.request") => {
                if map.len() != 1 {
                    if let Err(err) = writer.write_json(
                        &ConfirmationError {
                            kind: "agent.confirmation.error",
                            code: "INVALID_ARGUMENT",
                            message: "确认挑战请求包含无效字段。",
                            retry: "never",
                        },
                        MAX_CONTROL_FRAME,
                        WRITE_TIMEOUT,
                    ) {
                        break Err(err);
                    }
                    continue;
                }
                let mut nonce = [0_u8; 32];
                if getrandom::fill(&mut nonce).is_err() {
                    break Err(io::Error::other("getrandom failed"));
                }
                pending_confirm_nonce = Some(nonce);
                if let Err(err) = writer.write_json(
                    &ConfirmationChallenge {
                        kind: "agent.confirmation.challenge",
                        version: VERSION,
                        agent_epoch: &ctx.agent_epoch,
                        nonce,
                    },
                    MAX_CONTROL_FRAME,
                    WRITE_TIMEOUT,
                ) {
                    break Err(err);
                }
            }
            Some("agent.confirmation.register") => {
                let expected_nonce = pending_confirm_nonce.take();
                let svc = ConfirmationServices {
                    ctx,
                    authority,
                    desktop: desktop.as_ref(),
                    leases: leases.as_ref(),
                    writer: &writer,
                };
                if let Err(err) = handle_confirmation_register(&raw, expected_nonce, &svc) {
                    break Err(err);
                }
            }
            Some("request") => match validate_request_envelope(&map, ctx) {
                Err(err) => break Err(err),
                Ok(EnvelopeCheck::EarlyResponse(payload, budget)) => {
                    if let Err(err) = writer.write_framed(&payload, budget) {
                        break Err(err);
                    }
                }
                Ok(EnvelopeCheck::Valid(req)) => {
                    let worker_slot = match req.method.as_str() {
                        "lease.pause" | "lease.release" => {
                            let Some(guard) =
                                try_acquire_worker_slot(&in_flight_control, MAX_IN_FLIGHT_CONTROL)
                            else {
                                let payload = serialize_error(
                                    &req.id,
                                    "LIMIT_EXCEEDED",
                                    "并发租约控制请求数量超出上限。",
                                    "afterQuery",
                                )?;
                                if let Err(err) = writer.write_framed(&payload, req.write_budget) {
                                    break Err(err);
                                }
                                continue;
                            };
                            Some(guard)
                        }
                        "segment.submit" | "lease.acquire" | "lease.resume" => {
                            let Some(guard) = try_acquire_worker_slot(
                                &in_flight_blocking,
                                MAX_IN_FLIGHT_PER_SESSION,
                            ) else {
                                let payload = serialize_error(
                                    &req.id,
                                    "LIMIT_EXCEEDED",
                                    "并发会话操作请求数量超出上限。",
                                    "afterQuery",
                                )?;
                                if let Err(err) = writer.write_framed(&payload, req.write_budget) {
                                    break Err(err);
                                }
                                continue;
                            };
                            Some(guard)
                        }
                        _ => None,
                    };

                    if let Some(guard) = worker_slot {
                        workers.retain(|h| !h.is_finished());
                        let err_id = req.id.clone();
                        let err_budget = req.write_budget;
                        let w_ctx = ctx.clone();
                        let w_desktop = Arc::clone(&desktop);
                        let w_leases = Arc::clone(&leases);
                        let w_draining = Arc::clone(&is_draining);
                        let w_closing = Arc::clone(&session_closing);
                        let w_writer = Arc::clone(&writer);
                        match thread::Builder::new().spawn(move || {
                            let _guard = guard;
                            if let Ok(payload) = execute_validated_request(
                                &req,
                                &w_ctx,
                                w_desktop.as_ref(),
                                w_leases.as_ref(),
                                w_draining.as_ref(),
                                w_closing.as_ref(),
                            ) {
                                let _ = w_writer.write_framed(&payload, req.write_budget);
                            }
                        }) {
                            Ok(handle) => workers.push(handle),
                            Err(_) => {
                                let payload = serialize_error(
                                    &err_id,
                                    "INTERNAL_ERROR",
                                    "无法分派会话执行工作线程。",
                                    "never",
                                )?;
                                if let Err(err) = writer.write_framed(&payload, err_budget) {
                                    break Err(err);
                                }
                            }
                        }
                    } else {
                        let payload = execute_validated_request(
                            &req,
                            ctx,
                            desktop.as_ref(),
                            leases.as_ref(),
                            is_draining.as_ref(),
                            session_closing.as_ref(),
                        )?;
                        if let Err(err) = writer.write_framed(&payload, req.write_budget) {
                            break Err(err);
                        }
                    }
                }
            },
            _ => break Err(io::ErrorKind::InvalidData.into()),
        }
    };

    writer.poison_and_stop();
    session_closing.store(true, Ordering::Release);
    leases.on_session_closed(&ctx.session_id, desktop.as_ref());
    for handle in workers {
        let _ = handle.join();
    }
    leases.on_session_closed(&ctx.session_id, desktop.as_ref());
    drop(slot);
    drop(reader);
    drop(writer);
    loop_result
}

fn verify_attach(
    hello: &AttachHello,
    peer_pid: u32,
    expected_nonce: &[u8; 32],
    config: &ServerConfig,
) -> io::Result<AttachRegistration> {
    if hello.kind != "agent.attach.hello" || hello.version != VERSION {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    let signature =
        Signature::from_slice(&hello.signature).map_err(|_| io::ErrorKind::PermissionDenied)?;
    config
        .verification_key
        .verify_strict(hello.registration.as_bytes(), &signature)
        .map_err(|_| io::ErrorKind::PermissionDenied)?;
    let registration: AttachRegistration = parse_object(hello.registration.as_bytes())?;
    if registration.kind != "deskweave/agent-attach/v1"
        || registration.engine_pid != peer_pid
        || registration.agent_pid != process::id()
        || registration.agent_epoch != config.agent_epoch
        || !valid_workspace_id(&registration.workspace_id)
        || registration.owner != config.owner_identity
        || registration.scope != config.scope
        || registration.nonce != *expected_nonce
        || !is_hex(&registration.proof_key_hash)
    {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    let expected_hash = sha256_hex(&hello.proof_key);
    if !constant_time_eq(
        expected_hash.as_bytes(),
        registration.proof_key_hash.as_bytes(),
    ) {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(registration)
}

fn handle_attached<S: SessionStream + 'static>(
    _thread_guard: AttachedThreadGuard,
    mut stream: S,
    peer_pid: u32,
    shared: Arc<Shared>,
    mut slot: ConnectionSlot,
) {
    let deadline = Some(Instant::now() + shared.config.handshake_timeout);
    let mut nonce = [0_u8; 32];
    if getrandom::fill(&mut nonce).is_err() {
        return;
    }
    {
        let mut io = DeadlineIo::new(&mut stream, deadline, &shared.stop);
        let challenge = AttachChallenge {
            kind: "agent.attach.challenge",
            version: VERSION,
            agent_epoch: &shared.config.agent_epoch,
            scope: &shared.config.scope,
            nonce,
        };
        if write_frame(&mut io, &challenge).is_err() {
            return;
        }
    }
    let hello = {
        let mut io = DeadlineIo::new(&mut stream, deadline, &shared.stop);
        match read_frame::<AttachHello>(&mut io) {
            Ok(Some(hello)) => hello,
            Ok(None) | Err(_) => return,
        }
    };
    let Ok(registration) = verify_attach(&hello, peer_pid, &nonce, &shared.config) else {
        return;
    };
    let Ok(session_id) = generate_session_id() else {
        return;
    };
    if !slot.promote() {
        return;
    }
    let proof = attach_ready_proof(
        &hello.proof_key,
        peer_pid,
        process::id(),
        &registration.workspace_id,
        &shared.config.agent_epoch,
        &nonce,
        &session_id,
    );
    {
        let mut io = DeadlineIo::new(&mut stream, deadline, &shared.stop);
        let ready = AttachReady {
            kind: "agent.attach.ready",
            agent_epoch: &shared.config.agent_epoch,
            workspace_id: &registration.workspace_id,
            session: &session_id,
            proof,
        };
        if write_frame(&mut io, &ready).is_err() {
            return;
        }
    }
    let ctx = SessionContext {
        agent_epoch: shared.config.agent_epoch.clone(),
        session_id,
        workspace_id: registration.workspace_id,
    };
    let authority = ConfirmationAuthority::from_config(&shared.config);
    let drain_shared = Arc::clone(&shared);
    let _ = serve_session_with_coordinator(
        stream,
        &ctx,
        &authority,
        Arc::clone(&shared.config.desktop),
        Arc::clone(&shared.leases),
        Arc::new(move || drain_shared.is_draining()),
        shared.config.frame_read_timeout,
        &shared.stop,
        Some(slot),
    );
}

pub(crate) fn run<S: SessionStream + 'static, L: SessionListener + 'static>(
    initial: S,
    initial_session: SessionContext,
    listener: L,
    config: ServerConfig,
    stop: StopSignal,
) -> io::Result<()> {
    run_with_leases(
        initial,
        initial_session,
        listener,
        config,
        Arc::new(LeaseCoordinator::new()),
        stop,
    )
}

fn run_with_leases<S: SessionStream + 'static, L: SessionListener + 'static>(
    initial: S,
    initial_session: SessionContext,
    mut listener: L,
    config: ServerConfig,
    leases: Arc<LeaseCoordinator>,
    stop: StopSignal,
) -> io::Result<()> {
    let shared = Arc::new(Shared {
        config,
        state: Mutex::new(ServerState {
            active_sessions: 1,
            total_connections: 1,
            attached_threads: 0,
            draining: false,
            idle_since: None,
        }),
        changed: Condvar::new(),
        stop,
        leases,
    });

    let initial_shared = Arc::clone(&shared);
    let initial_slot = ConnectionSlot {
        shared: Arc::clone(&shared),
        authenticated: true,
    };
    let initial_handle = thread::Builder::new().spawn(move || {
        let authority = ConfirmationAuthority::from_config(&initial_shared.config);
        let drain_shared = Arc::clone(&initial_shared);
        let _ = serve_session_with_coordinator(
            initial,
            &initial_session,
            &authority,
            Arc::clone(&initial_shared.config.desktop),
            Arc::clone(&initial_shared.leases),
            Arc::new(move || drain_shared.is_draining()),
            initial_shared.config.frame_read_timeout,
            &initial_shared.stop,
            Some(initial_slot),
        );
    })?;

    let expiry_shared = Arc::clone(&shared);
    let expiry_handle = match thread::Builder::new().spawn(move || {
        expiry_shared
            .leases
            .drive_expiry_loop(expiry_shared.config.desktop.as_ref(), &expiry_shared.stop);
    }) {
        Ok(handle) => handle,
        Err(err) => {
            shared.stop.trigger();
            shared.leases.notify_all();
            let _ = initial_handle.join();
            return Err(err);
        }
    };

    let accept_shared = Arc::clone(&shared);
    let accept_handle = match thread::Builder::new().spawn(move || {
        while !accept_shared.stop.is_triggered() {
            let (stream, peer_pid) = match listener.accept(&accept_shared.stop) {
                Ok(accepted) => accepted,
                Err(_) => break,
            };
            let (slot, thread_guard) = {
                let mut state = accept_shared.state.lock().unwrap();
                if state.draining || state.total_connections >= accept_shared.config.max_clients {
                    continue;
                }
                state.total_connections += 1;
                state.attached_threads += 1;
                (
                    ConnectionSlot {
                        shared: Arc::clone(&accept_shared),
                        authenticated: false,
                    },
                    AttachedThreadGuard {
                        shared: Arc::clone(&accept_shared),
                    },
                )
            };
            let worker_shared = Arc::clone(&accept_shared);
            let _ = thread::Builder::new().spawn(move || {
                handle_attached(thread_guard, stream, peer_pid, worker_shared, slot)
            });
        }
        drop(listener);
    }) {
        Ok(handle) => handle,
        Err(err) => {
            shared.stop.trigger();
            shared.leases.notify_all();
            let _ = expiry_handle.join();
            let _ = initial_handle.join();
            return Err(err);
        }
    };

    {
        let mut state = shared.state.lock().unwrap();
        loop {
            if state.active_sessions > 0 {
                state = shared.changed.wait(state).unwrap();
                continue;
            }
            let since = *state.idle_since.get_or_insert_with(Instant::now);
            let elapsed = since.elapsed();
            if elapsed >= shared.config.idle_grace {
                state.draining = true;
                break;
            }
            let remaining = shared.config.idle_grace - elapsed;
            let (guard, _) = shared.changed.wait_timeout(state, remaining).unwrap();
            state = guard;
        }
    }

    shared.stop.trigger();
    shared.leases.notify_all();
    let _ = expiry_handle.join();
    let _ = accept_handle.join();
    let _ = initial_handle.join();

    let mut state = shared.state.lock().unwrap();
    while state.total_connections > 0 || state.attached_threads > 0 {
        state = shared.changed.wait(state).unwrap();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lease::LEASE_TTL_US;
    use crate::protocol::{read_frame_bytes, write_frame_bounded};
    use deskweave_platform_api::{
        BackendKind, CapabilitySnapshot, DisplayTopology, FakeDesktopSession, FeatureSet,
        FeatureState, FeatureStatus, NativeInputAction, PlatformArch, PlatformError, PlatformOs,
        SubmissionOutcome, TargetDescriptor, TargetStatus, TopologyCoverage, TrajectoryVersion,
        compute_segment_payload_hash,
    };
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;
    use std::collections::VecDeque;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::mpsc;

    struct PipeHalf {
        buf: VecDeque<u8>,
        closed: bool,
    }

    struct MemPipe {
        state: Mutex<PipeHalf>,
        ready: Condvar,
    }

    impl MemPipe {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                state: Mutex::new(PipeHalf {
                    buf: VecDeque::new(),
                    closed: false,
                }),
                ready: Condvar::new(),
            })
        }

        fn push(&self, data: &[u8]) -> io::Result<usize> {
            let mut state = self.state.lock().unwrap();
            if state.closed {
                return Err(io::ErrorKind::BrokenPipe.into());
            }
            state.buf.extend(data);
            self.ready.notify_all();
            Ok(data.len())
        }

        fn close(&self) {
            let mut state = self.state.lock().unwrap();
            state.closed = true;
            self.ready.notify_all();
        }
    }

    struct MemReader {
        reader: Arc<MemPipe>,
    }

    impl SessionRead for MemReader {
        fn read_chunk(
            &mut self,
            buf: &mut [u8],
            deadline: Option<Instant>,
            stop: &StopSignal,
        ) -> io::Result<usize> {
            read_mem_pipe(&self.reader, buf, deadline, stop)
        }
    }

    struct MemWriter {
        writer: Arc<MemPipe>,
    }

    impl Drop for MemWriter {
        fn drop(&mut self) {
            self.writer.close();
        }
    }

    impl SessionWrite for MemWriter {
        fn write_chunk(
            &mut self,
            buf: &[u8],
            _deadline: Option<Instant>,
            stop: &StopSignal,
        ) -> io::Result<usize> {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            self.writer.push(buf)
        }
    }

    fn read_mem_pipe(
        reader: &Arc<MemPipe>,
        buf: &mut [u8],
        deadline: Option<Instant>,
        stop: &StopSignal,
    ) -> io::Result<usize> {
        let mut state = reader.state.lock().unwrap();
        loop {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            if !state.buf.is_empty() {
                let count = buf.len().min(state.buf.len());
                for slot in buf.iter_mut().take(count) {
                    *slot = state.buf.pop_front().unwrap();
                }
                return Ok(count);
            }
            if state.closed {
                return Ok(0);
            }
            let wait = match deadline {
                Some(limit) => {
                    let now = Instant::now();
                    if now >= limit {
                        return Err(io::ErrorKind::TimedOut.into());
                    }
                    limit.duration_since(now).min(Duration::from_millis(10))
                }
                None => Duration::from_millis(10),
            };
            let (guard, _) = reader.ready.wait_timeout(state, wait).unwrap();
            state = guard;
        }
    }

    struct MemStream {
        reader: Arc<MemPipe>,
        writer: Option<Arc<MemPipe>>,
    }

    impl MemStream {
        fn pair() -> (Self, Self) {
            let a_to_b = MemPipe::new();
            let b_to_a = MemPipe::new();
            (
                Self {
                    reader: Arc::clone(&b_to_a),
                    writer: Some(Arc::clone(&a_to_b)),
                },
                Self {
                    reader: a_to_b,
                    writer: Some(b_to_a),
                },
            )
        }
    }

    impl Drop for MemStream {
        fn drop(&mut self) {
            if let Some(writer) = self.writer.take() {
                writer.close();
            }
        }
    }

    impl SessionStream for MemStream {
        type Reader = MemReader;
        type Writer = MemWriter;

        fn read_chunk(
            &mut self,
            buf: &mut [u8],
            deadline: Option<Instant>,
            stop: &StopSignal,
        ) -> io::Result<usize> {
            read_mem_pipe(&self.reader, buf, deadline, stop)
        }

        fn write_chunk(
            &mut self,
            buf: &[u8],
            _deadline: Option<Instant>,
            stop: &StopSignal,
        ) -> io::Result<usize> {
            if stop.is_triggered() {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            let Some(ref writer) = self.writer else {
                return Err(io::ErrorKind::BrokenPipe.into());
            };
            writer.push(buf)
        }

        fn split(mut self) -> io::Result<(Self::Reader, Self::Writer)> {
            let writer = self
                .writer
                .take()
                .ok_or_else(|| io::Error::from(io::ErrorKind::BrokenPipe))?;
            Ok((
                MemReader {
                    reader: Arc::clone(&self.reader),
                },
                MemWriter { writer },
            ))
        }
    }

    struct MemListener {
        rx: mpsc::Receiver<(MemStream, u32)>,
    }

    impl SessionListener for MemListener {
        type Stream = MemStream;

        fn accept(&mut self, stop: &StopSignal) -> io::Result<(MemStream, u32)> {
            loop {
                if stop.is_triggered() {
                    return Err(io::ErrorKind::ConnectionAborted.into());
                }
                match self.rx.recv_timeout(Duration::from_millis(10)) {
                    Ok(item) => return Ok(item),
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        return Err(io::ErrorKind::ConnectionAborted.into());
                    }
                }
            }
        }
    }

    fn fake_desktop(topology: Result<DisplayTopology, PlatformError>) -> Arc<dyn DesktopSession> {
        let caps = CapabilitySnapshot {
            os: PlatformOs::Macos,
            arch: PlatformArch::Arm64,
            backend: BackendKind::MacosQuartz,
            backend_version: "0.1.0".to_owned(),
            generation: 1,
            topology_version: topology.as_ref().ok().map(DisplayTopology::version),
            topology_coverage: if topology.is_ok() {
                TopologyCoverage::All
            } else {
                TopologyCoverage::Unknown
            },
            trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
            features: FeatureSet::all(FeatureStatus::unimplemented()),
        };
        Arc::new(FakeDesktopSession::new(caps, topology))
    }

    fn test_config(
        signing_key: &SigningKey,
        idle_grace: Duration,
        max_clients: usize,
        desktop: Arc<dyn DesktopSession>,
    ) -> ServerConfig {
        ServerConfig {
            agent_epoch: "a".repeat(64),
            scope: "b".repeat(64),
            owner_identity: ["1".into(), "2".into()],
            verification_key: signing_key.verifying_key(),
            idle_grace,
            handshake_timeout: Duration::from_millis(200),
            frame_read_timeout: Duration::from_millis(200),
            max_clients,
            desktop,
        }
    }

    fn initial_ctx() -> SessionContext {
        SessionContext {
            agent_epoch: "a".repeat(64),
            session_id: "1".repeat(32),
            workspace_id: "ws-1".to_owned(),
        }
    }

    fn client_attach(
        client: &mut MemStream,
        signing_key: &SigningKey,
        engine_pid: u32,
        workspace_id: &str,
        owner: [&str; 2],
        stop: &StopSignal,
    ) -> io::Result<Value> {
        let deadline = Some(Instant::now() + Duration::from_secs(2));
        let challenge: Value = read_frame(&mut DeadlineIo::new(client, deadline, stop))?
            .ok_or(io::ErrorKind::UnexpectedEof)?;
        assert_eq!(challenge["kind"], "agent.attach.challenge");
        assert_eq!(challenge["version"], VERSION);
        let nonce_vals = challenge["nonce"].as_array().unwrap();
        let mut nonce = [0_u8; 32];
        for (i, item) in nonce_vals.iter().enumerate() {
            nonce[i] = item.as_u64().unwrap() as u8;
        }
        let proof_key = [0x33_u8; 32];
        let claim = json!({
            "kind": "deskweave/agent-attach/v1",
            "enginePid": engine_pid,
            "agentPid": process::id(),
            "agentEpoch": "a".repeat(64),
            "workspaceId": workspace_id,
            "owner": [owner[0], owner[1]],
            "scope": "b".repeat(64),
            "nonce": nonce.to_vec(),
            "proofKeyHash": sha256_hex(&proof_key),
        });
        let encoded = serde_json::to_string(&claim).unwrap();
        let signature = signing_key.sign(encoded.as_bytes()).to_bytes().to_vec();
        let hello = json!({
            "kind": "agent.attach.hello",
            "version": VERSION,
            "proofKey": proof_key.to_vec(),
            "registration": encoded,
            "signature": signature,
        });
        write_frame(&mut DeadlineIo::new(client, deadline, stop), &hello)?;
        let ready: Value = read_frame(&mut DeadlineIo::new(client, deadline, stop))?
            .ok_or(io::ErrorKind::UnexpectedEof)?;
        let session = ready["session"].as_str().unwrap();
        let expected_proof = attach_ready_proof(
            &proof_key,
            engine_pid,
            process::id(),
            workspace_id,
            &"a".repeat(64),
            &nonce,
            session,
        );
        let actual_proof: Vec<u8> = ready["proof"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        assert_eq!(actual_proof, expected_proof.to_vec());
        Ok(ready)
    }

    fn client_ping(client: &mut MemStream, stop: &StopSignal) -> io::Result<Value> {
        let deadline = Some(Instant::now() + Duration::from_secs(2));
        write_frame(
            &mut DeadlineIo::new(client, deadline, stop),
            &json!({"kind": "agent.ping", "agentEpoch": "a".repeat(64)}),
        )?;
        read_frame(&mut DeadlineIo::new(client, deadline, stop))?
            .ok_or_else(|| io::Error::from(io::ErrorKind::UnexpectedEof))
    }

    fn client_rpc(client: &mut MemStream, request: &Value, stop: &StopSignal) -> io::Result<Value> {
        let deadline = Some(Instant::now() + Duration::from_secs(2));
        write_frame_bounded(
            &mut DeadlineIo::new(client, deadline, stop),
            request,
            MAX_CONTROL_FRAME,
        )?;
        let raw = read_frame_bytes(
            &mut DeadlineIo::new(client, deadline, stop),
            MAX_CONTROL_FRAME,
        )?
        .ok_or_else(|| io::Error::from(io::ErrorKind::UnexpectedEof))?;
        serde_json::from_slice(&raw).map_err(|_| io::ErrorKind::InvalidData.into())
    }

    #[test]
    fn shared_server_serves_multiple_engines_and_exits_after_idle_grace() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let desktop = fake_desktop(Err(PlatformError::capability_not_implemented("none")));
        let config = test_config(&key, Duration::from_millis(300), 4, desktop);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        assert_eq!(
            client_ping(&mut client_a, &client_stop).unwrap()["kind"],
            "agent.pong"
        );

        let (server_b, mut client_b) = MemStream::pair();
        tx.send((server_b, 2002)).unwrap();
        let ready_b =
            client_attach(&mut client_b, &key, 2002, "ws-2", ["1", "2"], &client_stop).unwrap();
        assert_eq!(ready_b["kind"], "agent.attach.ready");
        assert_eq!(ready_b["workspaceId"], "ws-2");

        // Closing launcher client A must NOT stop the shared server while B is active.
        drop(client_a);
        thread::sleep(Duration::from_millis(360));
        assert_eq!(
            client_ping(&mut client_b, &client_stop).unwrap()["kind"],
            "agent.pong"
        );

        // Closing client B triggers idle grace; attaching client C within idle grace cancels exit.
        drop(client_b);
        thread::sleep(Duration::from_millis(50));
        let (server_c, mut client_c) = MemStream::pair();
        tx.send((server_c, 3003)).unwrap();
        let ready_c =
            client_attach(&mut client_c, &key, 3003, "ws-3", ["1", "2"], &client_stop).unwrap();
        assert_eq!(ready_c["workspaceId"], "ws-3");
        thread::sleep(Duration::from_millis(350));
        assert_eq!(
            client_ping(&mut client_c, &client_stop).unwrap()["kind"],
            "agent.pong"
        );

        // Closing final client C allows idle grace to expire and server exits cleanly.
        drop(client_c);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn shared_server_rejects_mismatched_owner_and_stalled_handshake_does_not_block_exit() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, client_a) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let desktop = fake_desktop(Err(PlatformError::session_unavailable("inactive")));
        let config = test_config(&key, Duration::from_millis(80), 2, desktop);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        let (server_bad, mut client_bad) = MemStream::pair();
        tx.send((server_bad, 4004)).unwrap();
        assert!(
            client_attach(
                &mut client_bad,
                &key,
                4004,
                "ws-bad",
                ["9", "9"],
                &client_stop
            )
            .is_err()
        );

        let (server_stalled, _client_stalled) = MemStream::pair();
        tx.send((server_stalled, 5005)).unwrap();
        thread::sleep(Duration::from_millis(20));

        let (server_over, mut client_over) = MemStream::pair();
        tx.send((server_over, 6006)).unwrap();
        let over_deadline = Some(Instant::now() + Duration::from_secs(1));
        assert!(
            read_frame::<Value>(&mut DeadlineIo::new(
                &mut client_over,
                over_deadline,
                &client_stop
            ))
            .unwrap()
            .is_none()
        );

        drop(client_a);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn shared_server_routes_capabilities_topology_and_enforces_envelope_boundaries() {
        let topo_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples/platform/macos-topology.json");
        let topology: DisplayTopology =
            serde_json::from_slice(&fs::read(topo_path).unwrap()).unwrap();
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let config = test_config(
            &key,
            Duration::from_millis(80),
            4,
            fake_desktop(Ok(topology.clone())),
        );
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        // 1. Valid desktop.capabilities
        let caps_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-caps",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(caps_resp["ok"], true);
        assert_eq!(caps_resp["replyTo"], "req-caps");
        assert_eq!(caps_resp["result"]["backend"], "macos-quartz");

        // 2. Valid desktop.topology
        let topo_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-topo",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.topology",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(topo_resp["ok"], true);
        assert_eq!(topo_resp["result"]["version"], topology.version());

        // 3. Cross-workspace spoofing -> ACCESS_DENIED
        let spoof_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-spoof",
                "session": "1".repeat(32),
                "workspace": "ws-other",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(spoof_resp["ok"], false);
        assert_eq!(spoof_resp["error"]["code"], "ACCESS_DENIED");

        // 4. Unsupported protocolVersion -> PROTOCOL_UNSUPPORTED
        let proto_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "2.0",
                "kind": "request",
                "id": "req-proto",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(proto_resp["ok"], false);
        assert_eq!(proto_resp["error"]["code"], "PROTOCOL_UNSUPPORTED");

        // 5. Extra params -> INVALID_ARGUMENT
        let param_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-params",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {"extra": true}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(param_resp["ok"], false);
        assert_eq!(param_resp["error"]["code"], "INVALID_ARGUMENT");

        // 6. Unimplemented method -> METHOD_UNAVAILABLE
        let missing_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-unimpl",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "capture.create",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(missing_resp["ok"], false);
        assert_eq!(missing_resp["error"]["code"], "METHOD_UNAVAILABLE");

        // 7. Attached second Engine uses its own session/workspace independently
        let (server_b, mut client_b) = MemStream::pair();
        tx.send((server_b, 2002)).unwrap();
        let ready_b =
            client_attach(&mut client_b, &key, 2002, "ws-2", ["1", "2"], &client_stop).unwrap();
        let sess_b = ready_b["session"].as_str().unwrap();
        let b_resp = client_rpc(
            &mut client_b,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-b",
                "session": sess_b,
                "workspace": "ws-2",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(b_resp["ok"], true);

        drop(client_b);
        drop(client_a);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn serialize_success_falls_back_to_limit_exceeded_when_over_budget() {
        let oversized = "x".repeat(MAX_CONTROL_FRAME + 32);
        let bytes = serialize_success("req-big", json!({"blob": oversized})).unwrap();
        assert!(bytes.len() <= MAX_CONTROL_FRAME);
        let parsed: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["ok"], false);
        assert_eq!(parsed["replyTo"], "req-big");
        assert_eq!(parsed["error"]["code"], "LIMIT_EXCEEDED");
    }

    #[test]
    fn stalled_partial_header_and_partial_payload_time_out_and_release_slot() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_idle) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let desktop = fake_desktop(Err(PlatformError::capability_not_implemented("none")));
        let mut config = test_config(&key, Duration::from_millis(250), 2, desktop);
        config.frame_read_timeout = Duration::from_millis(120);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        // 1. Partial 1-byte frame header stalls -> server times out frame assembly and releases slot.
        let (server_hdr, mut client_hdr) = MemStream::pair();
        tx.send((server_hdr, 3001)).unwrap();
        let _ = client_attach(
            &mut client_hdr,
            &key,
            3001,
            "ws-hdr",
            ["1", "2"],
            &client_stop,
        )
        .unwrap();
        client_hdr.write_chunk(&[0x20], None, &client_stop).unwrap();
        // Wait for the server side to close the stalled connection and free the 2nd slot.
        let mut probe = [0_u8; 1];
        let closed_hdr = client_hdr
            .read_chunk(
                &mut probe,
                Some(Instant::now() + Duration::from_secs(2)),
                &client_stop,
            )
            .unwrap();
        assert_eq!(closed_hdr, 0);

        // 2. Partial payload after full 4-byte length prefix stalls -> server times out and releases slot.
        let (server_body, mut client_body) = MemStream::pair();
        tx.send((server_body, 3002)).unwrap();
        let _ = client_attach(
            &mut client_body,
            &key,
            3002,
            "ws-body",
            ["1", "2"],
            &client_stop,
        )
        .unwrap();
        let mut partial = 128_u32.to_le_bytes().to_vec();
        partial.extend_from_slice(br#"{"protocolVersion":"1.0""#);
        client_body
            .write_chunk(&partial, None, &client_stop)
            .unwrap();
        let closed_body = client_body
            .read_chunk(
                &mut probe,
                Some(Instant::now() + Duration::from_secs(2)),
                &client_stop,
            )
            .unwrap();
        assert_eq!(closed_body, 0);

        // 3. Slot is free again AND the idle healthy connection stayed alive beyond frame_read_timeout!
        thread::sleep(Duration::from_millis(180));
        let (server_ok, mut client_ok) = MemStream::pair();
        tx.send((server_ok, 3003)).unwrap();
        let ready_ok = client_attach(
            &mut client_ok,
            &key,
            3003,
            "ws-ok",
            ["1", "2"],
            &client_stop,
        )
        .unwrap();
        let sess_ok = ready_ok["session"].as_str().unwrap();
        let resp_ok = client_rpc(
            &mut client_ok,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-ok",
                "session": sess_ok,
                "workspace": "ws-ok",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(resp_ok["ok"], true);

        let idle_resp = client_rpc(
            &mut client_idle,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-idle",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(idle_resp["ok"], true);

        drop(client_hdr);
        drop(client_body);
        drop(client_ok);
        drop(client_idle);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn slot_is_released_before_peer_observes_session_eof() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, client_idle) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let desktop = fake_desktop(Err(PlatformError::capability_not_implemented("none")));
        let config = test_config(&key, Duration::from_millis(250), 2, desktop);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        for i in 0..20_u32 {
            let (server_conn, mut client_conn) = MemStream::pair();
            tx.send((server_conn, 3100 + i)).unwrap();
            client_attach(
                &mut client_conn,
                &key,
                3100 + i,
                &format!("ws-slot-{i}"),
                ["1", "2"],
                &client_stop,
            )
            .unwrap();
            client_conn
                .write_chunk(b"\x02\x00\x00\x00{}", None, &client_stop)
                .unwrap();
            let mut probe = [0_u8; 1];
            let closed = client_conn
                .read_chunk(
                    &mut probe,
                    Some(Instant::now() + Duration::from_secs(2)),
                    &client_stop,
                )
                .unwrap();
            assert_eq!(closed, 0);
        }

        drop(client_idle);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn desktop_capabilities_serves_live_refreshed_snapshot_and_generation() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (_tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();

        let initial_caps = CapabilitySnapshot {
            os: PlatformOs::Macos,
            arch: PlatformArch::Arm64,
            backend: BackendKind::MacosQuartz,
            backend_version: "0.1.0".to_owned(),
            generation: 1,
            topology_version: None,
            topology_coverage: TopologyCoverage::Unknown,
            trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
            features: FeatureSet::all(FeatureStatus::unimplemented()),
        };
        let fake = Arc::new(FakeDesktopSession::new(
            initial_caps.clone(),
            Err(PlatformError::capability_not_implemented("none")),
        ));
        let desktop: Arc<dyn DesktopSession> = fake.clone();
        let config = test_config(&key, Duration::from_millis(200), 2, desktop);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        let r1 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-gen-1",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(r1["ok"], true);
        assert_eq!(r1["result"]["generation"], 1);
        assert_eq!(
            r1["result"]["features"]["input.keyboard"]["state"],
            "unimplemented"
        );

        // Simulate runtime permission revocation / session change on the shared DesktopSession.
        let mut updated = initial_caps;
        updated.generation = 2;
        updated.features = FeatureSet::all(FeatureStatus::session_unavailable());
        assert_eq!(
            updated.features.input_keyboard.state,
            FeatureState::SessionUnavailable
        );
        fake.set_capabilities(updated);

        let r2 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-gen-2",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "desktop.capabilities",
                "deadlineMs": 5000,
                "params": {}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(r2["ok"], true);
        assert_eq!(r2["result"]["generation"], 2);
        assert_eq!(
            r2["result"]["features"]["input.keyboard"]["state"],
            "sessionUnavailable"
        );

        drop(client_a);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn desktop_lease_exclusivity_fifo_wait_pause_resume_segment_and_disconnect_cleanup() {
        use deskweave_platform_api::{
            HeldInput, PointerButton, TargetDescriptor, TargetKind, TargetToken,
            compute_segment_payload_hash,
        };

        let topo_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples/platform/macos-topology.json");
        let topology: DisplayTopology =
            serde_json::from_slice(&fs::read(topo_path).unwrap()).unwrap();
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();

        let caps = CapabilitySnapshot {
            os: PlatformOs::Macos,
            arch: PlatformArch::Arm64,
            backend: BackendKind::MacosQuartz,
            backend_version: "0.1.0".to_owned(),
            generation: 1,
            topology_version: Some(topology.version()),
            topology_coverage: TopologyCoverage::All,
            trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
            features: FeatureSet::all(FeatureStatus::available()),
        };
        let fake = Arc::new(FakeDesktopSession::new(caps, Ok(topology)));
        fake.register_grant(
            "grant-win-a",
            TargetDescriptor {
                token: TargetToken("target-win-a".to_owned()),
                display_name: "Canvas A".to_owned(),
                kind: TargetKind::Window,
                target_version: "1".to_owned(),
                expires_at: "2099-01-01T00:00:00Z".to_owned(),
            },
        );
        fake.register_grant(
            "grant-win-b",
            TargetDescriptor {
                token: TargetToken("target-win-b".to_owned()),
                display_name: "Canvas B".to_owned(),
                kind: TargetKind::Window,
                target_version: "1".to_owned(),
                expires_at: "2099-01-01T00:00:00Z".to_owned(),
            },
        );

        let desktop: Arc<dyn DesktopSession> = fake.clone();
        let config = test_config(&key, Duration::from_millis(120), 4, desktop);
        let leases = Arc::new(LeaseCoordinator::new());
        let server_leases = Arc::clone(&leases);
        let server_thread = thread::spawn(move || {
            run_with_leases(
                server_init,
                initial_ctx(),
                MemListener { rx },
                config,
                server_leases,
                stop,
            )
        });

        let sess_a = "1".repeat(32);

        // 1. target.bind binds target-win-a and consumes selectionGrant once.
        let bind_a = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-bind-a",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-win-a"}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(bind_a["ok"], true);
        assert_eq!(bind_a["result"]["targetToken"], "target-win-a");

        // Re-using consumed grant is rejected with RESOURCE_DENIED.
        let bind_dup = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-bind-dup",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-win-a"}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(bind_dup["ok"], false);
        assert_eq!(bind_dup["error"]["code"], "RESOURCE_DENIED");

        // 2. Client A acquires lease.
        let acq_a = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq-a",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-a",
                    "targets": ["target-win-a"],
                    "waitMs": 0
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(acq_a["ok"], true);
        let lease_a = acq_a["result"]["leaseId"].as_str().unwrap().to_owned();
        let gen_a = acq_a["result"]["generation"].as_u64().unwrap();
        assert_eq!(acq_a["result"]["expiresInMs"], 2000);

        // 3. Attach Client B (workspace ws-2) and bind target-win-b.
        let (server_b, mut client_b) = MemStream::pair();
        tx.send((server_b, 2002)).unwrap();
        let ready_b =
            client_attach(&mut client_b, &key, 2002, "ws-2", ["1", "2"], &client_stop).unwrap();
        let sess_b = ready_b["session"].as_str().unwrap().to_owned();

        let bind_b = client_rpc(
            &mut client_b,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-bind-b",
                "session": sess_b,
                "workspace": "ws-2",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-win-b"}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(bind_b["ok"], true);

        // Client B waitMs=0 gets immediate DESKTOP_BUSY while Client A holds lease.
        let busy_b = client_rpc(
            &mut client_b,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-busy-b",
                "session": sess_b,
                "workspace": "ws-2",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-b",
                    "targets": ["target-win-b"],
                    "waitMs": 0
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(busy_b["ok"], false);
        assert_eq!(busy_b["error"]["code"], "DESKTOP_BUSY");

        // 4. Client A submits pointer.move + keyboard.down segment.
        let op_down = json!({
            "id": "hold-shift",
            "title": "按住 Shift",
            "action": "keyboard.down",
            "params": {
                "target": "main",
                "key": "ShiftLeft"
            },
            "on": {}
        });
        let hash_down = compute_segment_payload_hash("target-win-a", &op_down, None).unwrap();
        let seg_down = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-down",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "segmentId": "seg-1",
                    "payloadHash": hash_down,
                    "targetToken": "target-win-a",
                    "operation": op_down
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg_down["ok"], true);
        assert_eq!(seg_down["result"]["state"], "sent");

        // Idempotent segment.submit replay returns identical receipt; different hash returns REQUEST_CONFLICT.
        let seg_replay = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-replay",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "segmentId": "seg-1",
                    "payloadHash": hash_down,
                    "targetToken": "target-win-a",
                    "operation": op_down
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg_replay["ok"], true);
        assert_eq!(seg_replay["result"]["state"], "sent");

        let seg_status_conflict = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-conflict",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.status",
                "deadlineMs": 5000,
                "params": {
                    "segmentId": "seg-1",
                    "payloadHash": "0".repeat(64)
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg_status_conflict["ok"], false);
        assert_eq!(seg_status_conflict["error"]["code"], "REQUEST_CONFLICT");

        // 5. Client A pauses lease -> releases held ShiftLeft and produces valid PauseSnapshot.
        let pause_a = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-pause-a",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.pause",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(pause_a["ok"], true);
        assert_eq!(pause_a["result"]["state"], "paused");
        assert_eq!(pause_a["result"]["cleanup"], "succeeded");
        let pause_id = pause_a["result"]["pauseSnapshot"]["pauseId"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(
            pause_a["result"]["pauseSnapshot"]["inputTargetToken"],
            "target-win-a"
        );
        assert_eq!(
            fake.released_batches(),
            vec![vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]]
        );

        // Resuming with held inputs without confirmationGrant fails with LOCAL_CONFIRMATION_REQUIRED.
        let resume_no_grant = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-resume-nogrant",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "pauseId": pause_id,
                    "resumeAttemptId": "attempt-1",
                    "resumePlanHash": "f".repeat(64)
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(resume_no_grant["ok"], false);
        assert_eq!(
            resume_no_grant["error"]["code"],
            "LOCAL_CONFIRMATION_REQUIRED"
        );

        // Forged/unregistered confirmationGrant must be rejected with RESOURCE_DENIED.
        let resume_forged = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-resume-forged",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "pauseId": pause_id,
                    "resumeAttemptId": "attempt-1",
                    "resumePlanHash": "f".repeat(64),
                    "confirmationGrant": "forged-grant"
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(resume_forged["ok"], false);
        assert_eq!(resume_forged["error"]["code"], "RESOURCE_DENIED");

        let checkpoint_hash = pause_a["result"]["pauseSnapshot"]["checkpointHash"]
            .as_str()
            .unwrap()
            .to_owned();
        leases
            .register_confirmation(
                "confirm-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-1".to_owned(),
                    lease_id: lease_a.clone(),
                    pause_id: pause_id.clone(),
                    agent_epoch: "a".repeat(64),
                    generation: gen_a,
                    checkpoint_hash,
                    resume_plan_hash: "f".repeat(64),
                    restore_target_token: Some("target-win-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        // Resuming with registered confirmationGrant restores held ShiftLeft and transitions back to active.
        let resume_ok = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-resume-ok",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "pauseId": pause_id,
                    "resumeAttemptId": "attempt-1",
                    "resumePlanHash": "f".repeat(64),
                    "confirmationGrant": "confirm-1"
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(resume_ok["ok"], true);
        assert_eq!(resume_ok["result"]["state"], "active");
        assert_eq!(resume_ok["result"]["lastResumeAttemptId"], "attempt-1");

        // 6. Press left mouse button on Client A, then disconnect Client A while Client B waits in FIFO queue.
        let op_btn = json!({
            "id": "press-left",
            "title": "按下左键",
            "action": "pointer.button",
            "params": {
                "target": "main",
                "button": "left",
                "state": "down"
            },
            "on": {}
        });
        let hash_btn = compute_segment_payload_hash("target-win-a", &op_btn, None).unwrap();
        let seg_btn = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-btn",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_a,
                    "generation": gen_a,
                    "segmentId": "seg-2",
                    "payloadHash": hash_btn,
                    "targetToken": "target-win-a",
                    "operation": op_btn
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg_btn["ok"], true);

        // Drop Client A (simulating Engine disconnect Q19) -> auto-revokes lease_a and releases both Button(Left) and Key(ShiftLeft).
        drop(client_a);

        // Client B acquires lease with waitMs=1500 and succeeds as soon as Client A's disconnect is processed.
        let acq_b = client_rpc(
            &mut client_b,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq-b",
                "session": sess_b,
                "workspace": "ws-2",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-b",
                    "targets": ["target-win-b"],
                    "waitMs": 1500
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(acq_b["ok"], true);
        let gen_b = acq_b["result"]["generation"].as_u64().unwrap();
        assert!(gen_b > gen_a);
        assert_eq!(
            fake.released_batches().last().unwrap(),
            &vec![
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
                },
                HeldInput::Button {
                    button: PointerButton::Left
                }
            ]
        );

        let lease_b = acq_b["result"]["leaseId"].as_str().unwrap().to_owned();
        let rel_b = client_rpc(
            &mut client_b,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-rel-b",
                "session": sess_b,
                "workspace": "ws-2",
                "method": "lease.release",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_b,
                    "generation": gen_b,
                    "reason": "finished"
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(rel_b["ok"], true);
        assert_eq!(rel_b["result"]["cleanup"], "notRequired");

        drop(client_b);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn desktop_lease_ttl_expiry_uncertain_cleanup_and_desktop_target_topology() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, TargetDescriptor, TargetKind, TargetToken,
            compute_segment_payload_hash,
        };

        let topo_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples/platform/macos-topology.json");
        let topology: DisplayTopology =
            serde_json::from_slice(&fs::read(topo_path).unwrap()).unwrap();
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (_tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();

        let caps = CapabilitySnapshot {
            os: PlatformOs::Macos,
            arch: PlatformArch::Arm64,
            backend: BackendKind::MacosQuartz,
            backend_version: "0.1.0".to_owned(),
            generation: 1,
            topology_version: Some(topology.version()),
            topology_coverage: TopologyCoverage::All,
            trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
            features: FeatureSet::all(FeatureStatus::available()),
        };
        let fake = Arc::new(FakeDesktopSession::new(caps, Ok(topology.clone())));
        fake.register_grant(
            "grant-desktop-all",
            TargetDescriptor {
                token: TargetToken("target-desktop-all".to_owned()),
                display_name: "All Displays".to_owned(),
                kind: TargetKind::Desktop,
                target_version: "1".to_owned(),
                expires_at: "2099-01-01T00:00:00Z".to_owned(),
            },
        );

        let desktop: Arc<dyn DesktopSession> = fake.clone();
        let config = test_config(&key, Duration::from_millis(80), 2, desktop);
        let server_thread = thread::spawn(move || {
            run(server_init, initial_ctx(), MemListener { rx }, config, stop)
        });

        let sess_a = "1".repeat(32);

        // 1. target.bind with kind=desktop includes validated topology.
        let bind_resp = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-bind-desk",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-desktop-all"}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(bind_resp["ok"], true);
        assert_eq!(bind_resp["result"]["kind"], "desktop");
        assert_eq!(
            bind_resp["result"]["topology"]["version"],
            topology.version()
        );

        // 2. Acquire lease, hold a key, then advance monotonic clock by 2,000ms without renew.
        let acq1 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq-1",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-ttl",
                    "targets": ["target-desktop-all"],
                    "waitMs": 0
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(acq1["ok"], true);
        let lease_1 = acq1["result"]["leaseId"].as_str().unwrap().to_owned();
        let gen_1 = acq1["result"]["generation"].as_u64().unwrap();

        let op_key = json!({
            "id": "hold-ctrl",
            "title": "按住 Control",
            "action": "keyboard.down",
            "params": {
                "target": "main",
                "key": "ControlLeft"
            },
            "on": {}
        });
        let hash_key = compute_segment_payload_hash("target-desktop-all", &op_key, None).unwrap();
        let seg1 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-1",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_1,
                    "generation": gen_1,
                    "segmentId": "seg-ttl-1",
                    "payloadHash": hash_key,
                    "targetToken": "target-desktop-all",
                    "operation": op_key
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg1["ok"], true);

        // Advance clock by 2,000,000 us -> lease expires and releases ControlLeft.
        fake.fake_clock().advance_us(2_000_000);
        let renew_expired = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-renew-exp",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.renew",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_1,
                    "generation": gen_1
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(renew_expired["ok"], false);
        assert_eq!(renew_expired["error"]["code"], "LEASE_EXPIRED");
        assert_eq!(
            fake.released_batches(),
            vec![vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]]
        );

        let status_expired = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-status-exp",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.status",
                "deadlineMs": 5000,
                "params": {"leaseId": lease_1}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(status_expired["ok"], true);
        assert_eq!(status_expired["result"]["state"], "released");
        assert_eq!(status_expired["result"]["reason"], "LEASE_EXPIRED");

        // 3. Acquire a new lease, hold a key, simulate cleanup failure on pause -> transitions to uncertain and blocks new lease.acquire.
        let acq2 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq-2",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-uncertain",
                    "targets": ["target-desktop-all"],
                    "waitMs": 0
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(acq2["ok"], true);
        let lease_2 = acq2["result"]["leaseId"].as_str().unwrap().to_owned();
        let gen_2 = acq2["result"]["generation"].as_u64().unwrap();

        let seg2 = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-seg-2",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_2,
                    "generation": gen_2,
                    "segmentId": "seg-ttl-2",
                    "payloadHash": hash_key,
                    "targetToken": "target-desktop-all",
                    "operation": op_key
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(seg2["ok"], true);

        fake.set_cleanup_outcome(CleanupOutcome::Failed);
        let pause_fail = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-pause-fail",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.pause",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_2,
                    "generation": gen_2
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(pause_fail["ok"], true);
        assert_eq!(pause_fail["result"]["state"], "uncertain");
        assert_eq!(pause_fail["result"]["cleanup"], "failed");
        assert!(pause_fail["result"]["pauseSnapshot"].is_null());

        let acq_blocked = client_rpc(
            &mut client_a,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq-blocked",
                "session": sess_a,
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {
                    "runId": "run-3",
                    "targets": ["target-desktop-all"],
                    "waitMs": 0
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(acq_blocked["ok"], false);
        assert_eq!(acq_blocked["error"]["code"], "EFFECT_UNCERTAIN");
        assert_eq!(acq_blocked["error"]["retry"], "afterQuery");

        drop(client_a);
        server_thread.join().unwrap().unwrap();
    }

    type SubmitGate = (usize, mpsc::Sender<()>, mpsc::Receiver<()>);
    type PointerOverride = Option<Result<deskweave_platform_api::PausePoint, PlatformError>>;
    type VerifyCallback = (usize, Box<dyn FnOnce(&FakeDesktopSession) + Send>);

    struct ScriptedDesktop {
        inner: FakeDesktopSession,
        submit_outcomes: Mutex<VecDeque<Result<SubmissionOutcome, PlatformError>>>,
        verify_outcomes: Mutex<VecDeque<Result<TargetStatus, PlatformError>>>,
        bind_overrides: Mutex<VecDeque<Result<TargetDescriptor, PlatformError>>>,
        target_version_override: Mutex<Option<Result<Option<String>, PlatformError>>>,
        pointer_override: Mutex<PointerOverride>,
        pointer_after_submit: Mutex<PointerOverride>,
        submit_count: Mutex<usize>,
        submit_notify: Mutex<Option<(usize, mpsc::Sender<usize>)>>,
        submit_gate: Mutex<Option<SubmitGate>>,
        panic_on_submit: Mutex<Option<usize>>,
        trigger_emergency_on_pointer: Mutex<bool>,
        verify_count: Mutex<usize>,
        verify_gate: Mutex<Option<SubmitGate>>,
        verify_on_call: Mutex<Option<VerifyCallback>>,
    }

    impl ScriptedDesktop {
        fn new(caps: CapabilitySnapshot, topology: Result<DisplayTopology, PlatformError>) -> Self {
            Self {
                inner: FakeDesktopSession::new(caps, topology),
                submit_outcomes: Mutex::new(VecDeque::new()),
                verify_outcomes: Mutex::new(VecDeque::new()),
                bind_overrides: Mutex::new(VecDeque::new()),
                target_version_override: Mutex::new(None),
                pointer_override: Mutex::new(None),
                pointer_after_submit: Mutex::new(None),
                submit_count: Mutex::new(0),
                submit_notify: Mutex::new(None),
                submit_gate: Mutex::new(None),
                panic_on_submit: Mutex::new(None),
                trigger_emergency_on_pointer: Mutex::new(false),
                verify_count: Mutex::new(0),
                verify_gate: Mutex::new(None),
                verify_on_call: Mutex::new(None),
            }
        }

        fn push_submit_outcome(&self, outcome: Result<SubmissionOutcome, PlatformError>) {
            self.submit_outcomes.lock().unwrap().push_back(outcome);
        }

        fn push_verify_outcome(&self, outcome: Result<TargetStatus, PlatformError>) {
            self.verify_outcomes.lock().unwrap().push_back(outcome);
        }

        fn push_bind_override(&self, descriptor: Result<TargetDescriptor, PlatformError>) {
            self.bind_overrides.lock().unwrap().push_back(descriptor);
        }

        fn set_target_version_override(
            &self,
            override_res: Option<Result<Option<String>, PlatformError>>,
        ) {
            *self.target_version_override.lock().unwrap() = override_res;
        }

        fn set_pointer_override(&self, override_res: PointerOverride) {
            *self.pointer_override.lock().unwrap() = override_res;
        }

        fn set_pointer_after_submit(&self, override_res: PointerOverride) {
            *self.pointer_after_submit.lock().unwrap() = override_res;
        }

        fn notify_submit_after(&self, count: usize, tx: mpsc::Sender<usize>) {
            *self.submit_count.lock().unwrap() = 0;
            *self.submit_notify.lock().unwrap() = Some((count, tx));
        }

        fn gate_submit_on(&self, count: usize) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
            let (entered_tx, entered_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            *self.submit_count.lock().unwrap() = 0;
            *self.submit_gate.lock().unwrap() = Some((count, entered_tx, release_rx));
            (entered_rx, release_tx)
        }

        fn panic_on_submit_number(&self, count: usize) {
            *self.submit_count.lock().unwrap() = 0;
            *self.panic_on_submit.lock().unwrap() = Some(count);
        }

        fn set_trigger_emergency_on_pointer(&self, trigger: bool) {
            *self.trigger_emergency_on_pointer.lock().unwrap() = trigger;
        }

        fn gate_verify_on(&self, count: usize) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
            let (entered_tx, entered_rx) = mpsc::channel();
            let (release_tx, release_rx) = mpsc::channel();
            *self.verify_count.lock().unwrap() = 0;
            *self.verify_gate.lock().unwrap() = Some((count, entered_tx, release_rx));
            (entered_rx, release_tx)
        }

        fn on_verify_call(
            &self,
            count: usize,
            cb: impl FnOnce(&FakeDesktopSession) + Send + 'static,
        ) {
            *self.verify_count.lock().unwrap() = 0;
            *self.verify_on_call.lock().unwrap() = Some((count, Box::new(cb)));
        }
    }

    impl DesktopSession for ScriptedDesktop {
        fn os(&self) -> PlatformOs {
            self.inner.os()
        }

        fn arch(&self) -> PlatformArch {
            self.inner.arch()
        }

        fn backend(&self) -> BackendKind {
            self.inner.backend()
        }

        fn backend_version(&self) -> &str {
            self.inner.backend_version()
        }

        fn capabilities(&self) -> CapabilitySnapshot {
            self.inner.capabilities()
        }

        fn topology(&self) -> Result<DisplayTopology, PlatformError> {
            self.inner.topology()
        }

        fn pointer_position(&self) -> Result<deskweave_platform_api::PausePoint, PlatformError> {
            {
                let mut trigger = self.trigger_emergency_on_pointer.lock().unwrap();
                if *trigger {
                    *trigger = false;
                    let _ = self.inner.emergency().trigger_lock();
                    return Err(PlatformError::new(
                        "RESOURCE_DENIED",
                        "emergency locked during pointer query",
                    ));
                }
            }
            if let Some(ref override_res) = *self.pointer_override.lock().unwrap() {
                return override_res.clone();
            }
            self.inner.pointer_position()
        }

        fn clock(&self) -> &dyn deskweave_platform_api::InputClock {
            self.inner.clock()
        }

        fn targets(&self) -> &dyn deskweave_platform_api::TargetBinding {
            self
        }

        fn capture(&self) -> &dyn deskweave_platform_api::CaptureSource {
            self.inner.capture()
        }

        fn recorder(&self) -> &dyn deskweave_platform_api::InputRecorder {
            self.inner.recorder()
        }

        fn emitter(&self) -> &dyn deskweave_platform_api::InputEmitter {
            self
        }

        fn emergency(&self) -> &dyn deskweave_platform_api::EmergencyControl {
            self.inner.emergency()
        }

        fn credentials(&self) -> &dyn deskweave_platform_api::CredentialStore {
            self.inner.credentials()
        }
    }

    impl deskweave_platform_api::TargetBinding for ScriptedDesktop {
        fn bind_selection(
            &self,
            selection_grant: &str,
        ) -> Result<deskweave_platform_api::TargetDescriptor, PlatformError> {
            if let Some(next) = self.bind_overrides.lock().unwrap().pop_front() {
                if let Ok(ref desc) = next {
                    self.inner.register_target(
                        desc.clone(),
                        deskweave_platform_api::TargetStatus::Active,
                    );
                }
                return next;
            }
            self.inner.targets().bind_selection(selection_grant)
        }

        fn verify_target(
            &self,
            token: &deskweave_platform_api::TargetToken,
        ) -> Result<deskweave_platform_api::TargetStatus, PlatformError> {
            let count = {
                let mut c = self.verify_count.lock().unwrap();
                *c += 1;
                *c
            };
            let cb = {
                let mut hook = self.verify_on_call.lock().unwrap();
                if hook.as_ref().is_some_and(|(target, _)| *target == count) {
                    hook.take().map(|(_, f)| f)
                } else {
                    None
                }
            };
            if let Some(f) = cb {
                f(&self.inner);
            }
            let gate = {
                let mut slot = self.verify_gate.lock().unwrap();
                if slot.as_ref().is_some_and(|(target, _, _)| *target == count) {
                    slot.take()
                } else {
                    None
                }
            };
            if let Some((_, entered_tx, release_rx)) = gate {
                let _ = entered_tx.send(());
                let _ = release_rx.recv();
            }
            if let Some(next) = self.verify_outcomes.lock().unwrap().pop_front() {
                return next;
            }
            self.inner.targets().verify_target(token)
        }

        fn focus_target(
            &self,
            token: &deskweave_platform_api::TargetToken,
        ) -> Result<(), PlatformError> {
            self.inner.targets().focus_target(token)
        }

        fn target_version(
            &self,
            token: &deskweave_platform_api::TargetToken,
        ) -> Result<Option<String>, PlatformError> {
            if let Some(ref override_res) = *self.target_version_override.lock().unwrap() {
                return override_res.clone();
            }
            self.inner.targets().target_version(token)
        }
    }

    impl deskweave_platform_api::InputEmitter for ScriptedDesktop {
        fn submit_event(
            &self,
            token: &deskweave_platform_api::TargetToken,
            action: &deskweave_platform_api::NativeInputAction,
        ) -> Result<deskweave_platform_api::SubmissionOutcome, PlatformError> {
            let current = {
                let mut cnt = self.submit_count.lock().unwrap();
                *cnt += 1;
                *cnt
            };
            let gate = {
                let mut guard = self.submit_gate.lock().unwrap();
                if guard
                    .as_ref()
                    .is_some_and(|(target, _, _)| *target == current)
                {
                    guard.take()
                } else {
                    None
                }
            };
            if let Some((_, entered_tx, release_rx)) = gate {
                let _ = entered_tx.send(());
                let _ = release_rx.recv();
            }
            let should_panic = {
                let mut guard = self.panic_on_submit.lock().unwrap();
                if guard.as_ref().is_some_and(|&target| target == current) {
                    guard.take();
                    true
                } else {
                    false
                }
            };
            if should_panic {
                panic!("simulated native driver panic during submit_event");
            }
            let scripted = self.submit_outcomes.lock().unwrap().pop_front();
            let outcome = match scripted {
                Some(res) => res,
                None => self.inner.emitter().submit_event(token, action),
            };
            if matches!(outcome, Ok(SubmissionOutcome::Submitted))
                && let Some(next_ptr) = self.pointer_after_submit.lock().unwrap().take()
            {
                *self.pointer_override.lock().unwrap() = Some(next_ptr);
            }
            if let Some((target_cnt, ref tx)) = *self.submit_notify.lock().unwrap()
                && current == target_cnt
            {
                let _ = tx.send(current);
            }
            outcome
        }

        fn release_held(
            &self,
            held: &[deskweave_platform_api::HeldInput],
        ) -> Result<deskweave_platform_api::CleanupOutcome, PlatformError> {
            self.inner.emitter().release_held(held)
        }
    }

    fn macos_topology_and_caps() -> (DisplayTopology, CapabilitySnapshot) {
        let topo_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../examples/platform/macos-topology.json");
        let topology: DisplayTopology =
            serde_json::from_slice(&fs::read(topo_path).unwrap()).unwrap();
        let caps = CapabilitySnapshot {
            os: PlatformOs::Macos,
            arch: PlatformArch::Arm64,
            backend: BackendKind::MacosQuartz,
            backend_version: "0.1.0".to_owned(),
            generation: 1,
            topology_version: Some(topology.version()),
            topology_coverage: TopologyCoverage::All,
            trajectory_versions: vec![TrajectoryVersion::V1_0, TrajectoryVersion::V1_1],
            features: FeatureSet::all(FeatureStatus::available()),
        };
        (topology, caps)
    }

    fn json_obj(val: Value) -> Map<String, Value> {
        match val {
            Value::Object(map) => map,
            _ => panic!("expected json object"),
        }
    }

    fn make_ctx(session_id: &str, workspace_id: &str) -> SessionContext {
        SessionContext {
            agent_epoch: "a".repeat(64),
            session_id: session_id.to_owned(),
            workspace_id: workspace_id.to_owned(),
        }
    }

    fn target_desc(token: &str, expires_at: &str) -> deskweave_platform_api::TargetDescriptor {
        deskweave_platform_api::TargetDescriptor {
            token: deskweave_platform_api::TargetToken(token.to_owned()),
            display_name: format!("Target {token}"),
            kind: deskweave_platform_api::TargetKind::Window,
            target_version: "1".to_owned(),
            expires_at: expires_at.to_owned(),
        }
    }

    #[test]
    fn lease_resume_security_drift_and_mid_restore_failure_recovery() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-1", "ws-1");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let hold_shift = json!({
            "id": "hold-shift",
            "title": "按住 Shift",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h1 = compute_segment_payload_hash("target-a", &hold_shift, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-1",
                    "payloadHash": h1,
                    "targetToken": "target-a",
                    "operation": hold_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let pause_snap = paused.pause_snapshot.as_ref().unwrap();
        let plan_hash = "b".repeat(64);

        // 1. Reject forged/unregistered grant and mismatched plan hash.
        let forged_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-forged",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "unregistered"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(forged_err.code, "RESOURCE_DENIED");

        leases
            .register_confirmation(
                "grant-valid-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let wrong_plan_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-bad-plan",
                    "resumePlanHash": "c".repeat(64),
                    "confirmationGrant": "grant-valid-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(wrong_plan_err.code, "RESOURCE_DENIED");

        // 2. Reject topology version drift (keep Paused).
        let mut drifted_topo = base_topo.clone();
        match &mut drifted_topo {
            DisplayTopology::Physical(t) => t.version += 1,
            DisplayTopology::DisplayLocal(t) => t.version += 1,
        }
        scripted.inner.set_topology(Ok(drifted_topo));
        let topo_drift_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-topo-drift",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(topo_drift_err.code, "TARGET_LOST");
        scripted.inner.set_topology(Ok(base_topo));

        // 3. Reject target version drift (keep Paused).
        scripted.inner.set_target_version("target-a", "2");
        let target_drift_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-target-drift",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(target_drift_err.code, "TARGET_LOST");
        scripted.inner.set_target_version("target-a", "1");

        // 4. Valid resume succeeds and consumes grant-valid-1.
        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-valid-1",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(resumed.state, LeaseState::Active);

        // Hold a second key so held_inputs preserves press order [ShiftLeft, ControlLeft], pause, verify consumed grant cannot be reused.
        let hold_ctrl = json!({
            "id": "hold-ctrl",
            "title": "按住 Control",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h2 = compute_segment_payload_hash("target-a", &hold_ctrl, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-2",
                    "payloadHash": h2,
                    "targetToken": "target-a",
                    "operation": hold_ctrl
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let paused_2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let pause_snap_2 = paused_2.pause_snapshot.as_ref().unwrap();
        let reuse_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap_2.pause_id,
                    "resumeAttemptId": "att-reuse",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(reuse_err.code, "RESOURCE_DENIED");

        // 5. Mid-restore failure consumes grant-valid-2, records idempotent Uncertain receipt, and rolls back restored + ambiguous keys.
        leases
            .register_confirmation(
                "grant-valid-2",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap_2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap_2.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: Some(pause_snap_2.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted));
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted));
        scripted.push_submit_outcome(Err(PlatformError::new(
            "INPUT_INJECTION_FAILED",
            "second key failed",
        )));
        scripted.inner.set_cleanup_outcome(CleanupOutcome::Failed);

        let mid_fail_snap = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap_2.pause_id,
                    "resumeAttemptId": "att-mid-fail",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-2"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(mid_fail_snap.state, LeaseState::Uncertain);
        assert_eq!(mid_fail_snap.cleanup, CleanupOutcome::Failed);
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
                },
                HeldInput::Key {
                    key: "ControlLeft".to_owned()
                }
            ]
        );

        let mid_fail_replay = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap_2.pause_id,
                    "resumeAttemptId": "att-mid-fail",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-valid-2"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(mid_fail_replay, mid_fail_snap);
    }

    #[test]
    fn cleanup_pending_keeps_pausing_and_uncertain_desktop_lock() {
        use deskweave_platform_api::{CleanupOutcome, LeaseState, compute_segment_payload_hash};

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted
            .inner
            .register_grant("grant-b", target_desc("target-b", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx_a = make_ctx("sess-a", "ws-a");
        let ctx_b = make_ctx("sess-b", "ws-b");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx_b,
                scripted.as_ref(),
            )
            .unwrap();

        let acq_a = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let hold_op = json!({
            "id": "hold",
            "title": "按住键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let hold_hash = compute_segment_payload_hash("target-a", &hold_op, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "segmentId": "seg-hold",
                    "payloadHash": hold_hash,
                    "targetToken": "target-a",
                    "operation": hold_op
                })),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted.inner.set_cleanup_outcome(CleanupOutcome::Pending);
        let pause_pending = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_a.lease_id, "generation": acq_a.generation})),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(pause_pending.state, LeaseState::Pausing);
        assert_eq!(pause_pending.cleanup, CleanupOutcome::Pending);
        assert!(pause_pending.pause_snapshot.is_none());

        let rel_pending = leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "reason": "cancelled"
                })),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(rel_pending.cleanup, CleanupOutcome::Pending);

        let acq_b_err = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-b", "targets": ["target-b"], "waitMs": 0})),
                &ctx_b,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(acq_b_err.code, "EFFECT_UNCERTAIN");
    }

    #[test]
    fn mid_segment_concurrent_pause_produces_real_cursor_and_idempotent_receipt() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let op_chord = json!({
            "id": "chord",
            "title": "延迟组合键",
            "action": "keyboard.chord",
            "params": {
                "target": "main",
                "keys": ["ControlLeft", "KeyS"],
                "pressDurationMs": 450
            },
            "on": {}
        });
        let seg_hash = compute_segment_payload_hash("target-a", &op_chord, None).unwrap();
        let seg_params = json_obj(json!({
            "leaseId": acq.lease_id,
            "generation": acq.generation,
            "segmentId": "seg-mid-pause",
            "payloadHash": seg_hash,
            "targetToken": "target-a",
            "operation": op_chord
        }));

        let (tx, rx) = mpsc::channel();
        scripted.notify_submit_after(2, tx);

        let worker_leases = Arc::clone(&leases);
        let worker_desktop = Arc::clone(&scripted);
        let worker_ctx = ctx.clone();
        let worker_params = seg_params.clone();
        let handle = thread::spawn(move || {
            worker_leases.handle_segment_submit(
                &worker_params,
                &worker_ctx,
                worker_desktop.as_ref(),
                5000,
            )
        });

        assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap(), 2);

        let pause_snap_wrap = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(pause_snap_wrap.state, LeaseState::Paused);
        assert_eq!(pause_snap_wrap.cleanup, CleanupOutcome::Succeeded);

        let snap = pause_snap_wrap.pause_snapshot.as_ref().unwrap();
        let cursor = snap
            .cursor
            .as_ref()
            .expect("mid-segment pause must set cursor");
        assert_eq!(cursor.segment_id, "seg-mid-pause");
        assert_eq!(cursor.event_count, 4);
        assert_eq!(cursor.next_event_index, 2);
        assert!(cursor.remaining_delay_us > 0);
        assert_eq!(
            snap.held_inputs,
            vec![
                HeldInput::Key {
                    key: "ControlLeft".to_owned()
                },
                HeldInput::Key {
                    key: "KeyS".to_owned()
                }
            ]
        );
        assert_eq!(snap.input_target_token.as_deref(), Some("target-a"));

        let seg_res = handle.join().unwrap().unwrap();
        assert_eq!(seg_res.state, SegmentState::Paused);
        assert_eq!(seg_res.pause_id.as_deref(), Some(snap.pause_id.as_str()));

        // Idempotent replay of paused segment succeeds even while lease is Paused.
        let seg_replay = leases
            .handle_segment_submit(&seg_params, &ctx, scripted.as_ref(), 5000)
            .unwrap();
        assert_eq!(seg_replay, seg_res);

        let status_res = leases
            .handle_segment_status(
                &json_obj(json!({
                    "segmentId": "seg-mid-pause",
                    "payloadHash": seg_hash
                })),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(status_res, seg_res);
    }

    #[test]
    fn segment_submit_edge_cases_bindings_and_non_poisoning_errors() {
        use deskweave_platform_api::{
            LeaseState, SegmentState, TargetStatus, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq_1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // 1. Empty keyboard.text leaves inputTargetToken = null on subsequent lease.pause.
        let empty_text = json!({
            "id": "empty-text",
            "title": "空文本",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "", "intervalMs": 0},
            "on": {}
        });
        let h_empty = compute_segment_payload_hash("target-a", &empty_text, None).unwrap();
        let res_empty = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "segmentId": "seg-empty",
                    "payloadHash": h_empty,
                    "targetToken": "target-a",
                    "operation": empty_text
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res_empty.state, SegmentState::Sent);
        let paused_empty = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_1.lease_id, "generation": acq_1.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let pause_empty_snap = paused_empty.pause_snapshot.unwrap();
        assert_eq!(pause_empty_snap.input_target_token, None);

        leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "pauseId": pause_empty_snap.pause_id,
                    "resumeAttemptId": "att-empty",
                    "resumePlanHash": "d".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // 2. Index-0 pre-dispatch errors do not poison lease.
        let key_tap = json!({
            "id": "tap-a",
            "title": "按键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "KeyA"},
            "on": {}
        });
        let h_tap = compute_segment_payload_hash("target-a", &key_tap, None).unwrap();
        #[cfg(not(feature = "test-authority"))]
        {
            scripted.push_submit_outcome(Err(PlatformError::capability_not_implemented("unimpl")));
            let err_unimpl = leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq_1.lease_id,
                        "generation": acq_1.generation,
                        "segmentId": "seg-unimpl",
                        "payloadHash": h_tap,
                        "targetToken": "target-a",
                        "operation": key_tap
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap_err();
            assert_eq!(err_unimpl.code, "CAPABILITY_NOT_IMPLEMENTED");
            assert_eq!(
                leases
                    .handle_lease_status(
                        &json_obj(json!({"leaseId": acq_1.lease_id})),
                        &ctx,
                        scripted.as_ref()
                    )
                    .unwrap()
                    .state,
                LeaseState::Active
            );
        }

        scripted.push_submit_outcome(Err(PlatformError::invalid_argument("pre-dispatch")));
        let err_pre = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "segmentId": "seg-pre-dispatch",
                    "payloadHash": h_tap,
                    "targetToken": "target-a",
                    "operation": key_tap
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err_pre.code, "INVALID_ARGUMENT");
        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq_1.lease_id})),
                    &ctx,
                    scripted.as_ref()
                )
                .unwrap()
                .state,
            LeaseState::Active
        );

        // 3. Index-0 Rejected returns Rejected without poisoning lease and binds receipt to (leaseId, generation).
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Rejected));
        let rej_res = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "segmentId": "seg-rejected",
                    "payloadHash": h_tap,
                    "targetToken": "target-a",
                    "operation": key_tap
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(rej_res.state, SegmentState::Rejected);
        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq_1.lease_id})),
                    &ctx,
                    scripted.as_ref()
                )
                .unwrap()
                .state,
            LeaseState::Active
        );

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "reason": "finished"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let acq_2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let conflict_err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_2.lease_id,
                    "generation": acq_2.generation,
                    "segmentId": "seg-rejected",
                    "payloadHash": h_tap,
                    "targetToken": "target-a",
                    "operation": key_tap
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(conflict_err.code, "REQUEST_CONFLICT");

        // 4. Mid-segment target loss after index 0 returns Uncertain consistently on first call and replay.
        let chord = json!({
            "id": "chord-loss",
            "title": "失焦测试",
            "action": "keyboard.chord",
            "params": {"target": "main", "keys": ["ControlLeft", "KeyS"], "pressDurationMs": 0},
            "on": {}
        });
        let h_chord = compute_segment_payload_hash("target-a", &chord, None).unwrap();
        scripted.push_verify_outcome(Ok(TargetStatus::Active));
        scripted.push_verify_outcome(Ok(TargetStatus::Active));
        scripted.push_verify_outcome(Ok(TargetStatus::Lost));

        let mid_loss_params = json_obj(json!({
            "leaseId": acq_2.lease_id,
            "generation": acq_2.generation,
            "segmentId": "seg-mid-loss",
            "payloadHash": h_chord,
            "targetToken": "target-a",
            "operation": chord
        }));
        let res_uncertain = leases
            .handle_segment_submit(&mid_loss_params, &ctx, scripted.as_ref(), 5000)
            .unwrap();
        assert_eq!(res_uncertain.state, SegmentState::Uncertain);

        let res_uncertain_replay = leases
            .handle_segment_submit(&mid_loss_params, &ctx, scripted.as_ref(), 5000)
            .unwrap();
        assert_eq!(res_uncertain_replay, res_uncertain);
    }

    #[test]
    fn target_expiry_and_cross_workspace_token_isolation() {
        use deskweave_platform_api::{compute_segment_payload_hash, format_utc_timestamp};
        use std::time::{SystemTime, UNIX_EPOCH};

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        let leases = LeaseCoordinator::new();
        let ctx_1 = make_ctx("sess-1", "ws-1");
        let ctx_2 = make_ctx("sess-2", "ws-2");

        // 1. target.bind rejects already-expired expiresAt.
        scripted.push_bind_override(Ok(target_desc("target-exp-past", "2020-01-01T00:00:00Z")));
        let err_past = leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-past"})),
                &ctx_1,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(err_past.code, "RESOURCE_DENIED");

        // 2. Cross-workspace overwrite of an already-bound TargetToken is rejected without stealing ownership.
        scripted.push_bind_override(Ok(target_desc("shared-tok", "2099-01-01T00:00:00Z")));
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-ws1"})),
                &ctx_1,
                scripted.as_ref(),
            )
            .unwrap();
        scripted.push_bind_override(Ok(target_desc("shared-tok", "2099-01-01T00:00:00Z")));
        let hijack_err = leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-ws2"})),
                &ctx_2,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(hijack_err.code, "RESOURCE_DENIED");

        // 3. Post-bind target expiry fails segment.submit and subsequent lease.acquire with TARGET_LOST.
        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let short_exp_secs = now_secs + 2;
        scripted.push_bind_override(Ok(target_desc(
            "target-short",
            &format_utc_timestamp(short_exp_secs),
        )));
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-short"})),
                &ctx_1,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-short", "targets": ["target-short"], "waitMs": 0})),
                &ctx_1,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        loop {
            let cur_secs = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64;
            if cur_secs >= short_exp_secs {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }

        let op_tap = json!({
            "id": "tap",
            "title": "击键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "KeyA"},
            "on": {}
        });
        let h_tap = compute_segment_payload_hash("target-short", &op_tap, None).unwrap();
        let err_seg_exp = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-after-exp",
                    "payloadHash": h_tap,
                    "targetToken": "target-short",
                    "operation": op_tap
                })),
                &ctx_1,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err_seg_exp.code, "TARGET_LOST");

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "reason": "cancelled"
                })),
                &ctx_1,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let err_acq_exp = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-short-2", "targets": ["target-short"], "waitMs": 0}),
                ),
                &ctx_1,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(err_acq_exp.code, "TARGET_LOST");
    }

    #[test]
    fn lease_acquire_wait_queue_budgets_fifo_and_limit() {
        let (base_topo, caps) = macos_topology_and_caps();
        let desktop = Arc::new(FakeDesktopSession::new(caps, Ok(base_topo)));
        desktop.register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        desktop.register_grant("grant-b", target_desc("target-b", "2099-01-01T00:00:00Z"));
        desktop.register_grant("grant-c", target_desc("target-c", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx_a = make_ctx("sess-a", "ws-a");
        let ctx_b = make_ctx("sess-b", "ws-b");
        let ctx_c = make_ctx("sess-c", "ws-c");
        for (grant, ctx) in [
            ("grant-a", &ctx_a),
            ("grant-b", &ctx_b),
            ("grant-c", &ctx_c),
        ] {
            leases
                .handle_target_bind(
                    &json_obj(json!({"selectionGrant": grant})),
                    ctx,
                    desktop.as_ref(),
                )
                .unwrap();
        }

        let acq_a = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                desktop.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // 1. deadlineMs expiry returns DEADLINE_EXCEEDED before waitMs expires.
        let deadline_err = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-dl", "targets": ["target-b"], "waitMs": 5000})),
                &ctx_b,
                desktop.as_ref(),
                || false,
                25,
            )
            .unwrap_err();
        assert_eq!(deadline_err.code, "DEADLINE_EXCEEDED");

        // 2. waitMs expiry returns DESKTOP_BUSY before deadlineMs expires.
        let wait_err = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-wt", "targets": ["target-b"], "waitMs": 25})),
                &ctx_b,
                desktop.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(wait_err.code, "DESKTOP_BUSY");

        // 3. Multi-waiter strict FIFO [B, C].
        let leases_b = Arc::clone(&leases);
        let desk_b = Arc::clone(&desktop);
        let c_b = ctx_b.clone();
        let h_b = thread::spawn(move || {
            leases_b.handle_lease_acquire(
                &json_obj(json!({"runId": "run-b", "targets": ["target-b"], "waitMs": 5000})),
                &c_b,
                desk_b.as_ref(),
                || false,
                5000,
            )
        });
        while leases.wait_queue_len() < 1 {
            thread::sleep(Duration::from_millis(5));
        }

        let leases_c = Arc::clone(&leases);
        let desk_c = Arc::clone(&desktop);
        let c_c = ctx_c.clone();
        let h_c = thread::spawn(move || {
            leases_c.handle_lease_acquire(
                &json_obj(json!({"runId": "run-c", "targets": ["target-c"], "waitMs": 5000})),
                &c_c,
                desk_c.as_ref(),
                || false,
                5000,
            )
        });
        while leases.wait_queue_len() < 2 {
            thread::sleep(Duration::from_millis(5));
        }

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "reason": "finished"
                })),
                &ctx_a,
                desktop.as_ref(),
                5000,
            )
            .unwrap();
        let acq_b = h_b.join().unwrap().unwrap();
        while leases.wait_queue_len() > 1 {
            thread::sleep(Duration::from_millis(5));
        }
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "reason": "finished"
                })),
                &ctx_b,
                desktop.as_ref(),
                5000,
            )
            .unwrap();
        let acq_c = h_c.join().unwrap().unwrap();
        assert!(acq_b.generation < acq_c.generation);

        // 4. Exceeding MAX_WAIT_QUEUE (32) returns LIMIT_EXCEEDED.
        let (tx_done, rx_done) = mpsc::channel();
        let mut handles = Vec::with_capacity(32);
        for i in 0..32 {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&desktop);
            let ctx = ctx_b.clone();
            let done = tx_done.clone();
            handles.push(thread::spawn(move || {
                let res = l.handle_lease_acquire(
                    &json_obj(json!({
                        "runId": format!("run-q-{i}"),
                        "targets": ["target-b"],
                        "waitMs": 5000
                    })),
                    &ctx,
                    d.as_ref(),
                    || false,
                    5000,
                );
                let _ = done.send(res);
            }));
        }
        drop(tx_done);
        while leases.wait_queue_len() < 32 {
            thread::sleep(Duration::from_millis(5));
        }
        let full_err = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-q-33", "targets": ["target-b"], "waitMs": 5000})),
                &ctx_b,
                desktop.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(full_err.code, "LIMIT_EXCEEDED");

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_c.lease_id,
                    "generation": acq_c.generation,
                    "reason": "finished"
                })),
                &ctx_c,
                desktop.as_ref(),
                5000,
            )
            .unwrap();
        for _ in 0..32 {
            let next = rx_done
                .recv_timeout(Duration::from_secs(2))
                .unwrap()
                .unwrap();
            leases
                .handle_lease_release(
                    &json_obj(json!({
                        "leaseId": next.lease_id,
                        "generation": next.generation,
                        "reason": "finished"
                    })),
                    &ctx_b,
                    desktop.as_ref(),
                    5000,
                )
                .unwrap();
        }
        for h in handles {
            h.join().unwrap();
        }
    }

    #[test]
    fn lease_release_rejects_uncertain_even_with_empty_held_inputs() {
        use deskweave_platform_api::{SegmentState, compute_segment_payload_hash};

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx_a = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let move_op = json!({
            "id": "smooth-move",
            "title": "平滑移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 120, "y": 90},
                "durationMs": 20
            },
            "on": {}
        });
        let h_move = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted));
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Uncertain));
        let seg_res = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-uncertain-empty",
                    "payloadHash": h_move,
                    "targetToken": "target-a",
                    "operation": move_op
                })),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(seg_res.state, SegmentState::Uncertain);

        let rel_err = leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "reason": "finished"
                })),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(rel_err.code, "EFFECT_UNCERTAIN");
    }

    #[test]
    fn paused_lease_heartbeat_expiry_and_thirty_minute_retention_cap() {
        use deskweave_platform_api::LeaseState;

        let (base_topo, caps) = macos_topology_and_caps();
        let desktop = Arc::new(FakeDesktopSession::new(caps, Ok(base_topo)));
        desktop.register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                desktop.as_ref(),
            )
            .unwrap();

        // Part A: Paused lease without renew expires after 2s heartbeat TTL.
        let acq_1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                desktop.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let p1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_1.lease_id, "generation": acq_1.generation})),
                &ctx,
                desktop.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(p1.state, LeaseState::Paused);

        desktop.fake_clock().advance_us(2_000_000);
        let exp_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_1.lease_id, "generation": acq_1.generation})),
                &ctx,
                desktop.as_ref(),
            )
            .unwrap_err();
        assert_eq!(exp_err.code, "LEASE_EXPIRED");
        let st_1 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_1.lease_id})),
                &ctx,
                desktop.as_ref(),
            )
            .unwrap();
        assert_eq!(st_1.state, LeaseState::Released);
        assert_eq!(st_1.reason.as_deref(), Some("LEASE_EXPIRED"));

        // Part B: Paused lease expires after 30 minutes even under repeated lease.renew.
        let acq_2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                desktop.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let p2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                &ctx,
                desktop.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(p2.state, LeaseState::Paused);

        let mut elapsed_us: u64 = 0;
        while elapsed_us + 1_500_000 < crate::lease::PAUSE_RETENTION_US {
            desktop.fake_clock().advance_us(1_500_000);
            elapsed_us += 1_500_000;
            let r = leases
                .handle_lease_renew(
                    &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                    &ctx,
                    desktop.as_ref(),
                )
                .unwrap();
            let remaining_us = crate::lease::PAUSE_RETENTION_US - elapsed_us;
            let expected_ms = remaining_us.div_ceil(1_000).clamp(1, 2_000);
            assert_eq!(r.expires_in_ms, expected_ms);
        }

        desktop
            .fake_clock()
            .advance_us(crate::lease::PAUSE_RETENTION_US - elapsed_us);
        let cap_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                &ctx,
                desktop.as_ref(),
            )
            .unwrap_err();
        assert_eq!(cap_err.code, "LEASE_EXPIRED");
        let st_2 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_2.lease_id})),
                &ctx,
                desktop.as_ref(),
            )
            .unwrap();
        assert_eq!(st_2.state, LeaseState::Released);
        assert_eq!(st_2.reason.as_deref(), Some("LEASE_EXPIRED"));
    }

    #[test]
    fn mid_segment_pause_resume_completes_remaining_events_and_release_invalidates_paused_receipt()
    {
        use deskweave_platform_api::{
            CleanupOutcome, LeaseState, NativeInputAction, SegmentState,
            compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // Part A: Mid-segment pause then resume must complete remaining KeyUp events & flip receipt to Sent.
        let chord_op = json!({
            "id": "chord-resume",
            "title": "续播组合键",
            "action": "keyboard.chord",
            "params": {
                "target": "main",
                "keys": ["ControlLeft", "KeyS"],
                "pressDurationMs": 220
            },
            "on": {}
        });
        let h1 = compute_segment_payload_hash("target-a", &chord_op, None).unwrap();
        let seg1_params = json_obj(json!({
            "leaseId": acq.lease_id,
            "generation": acq.generation,
            "segmentId": "seg-resume-1",
            "payloadHash": h1,
            "targetToken": "target-a",
            "operation": chord_op
        }));

        let (tx1, rx1) = mpsc::channel();
        scripted.notify_submit_after(2, tx1);
        let l_w = Arc::clone(&leases);
        let d_w = Arc::clone(&scripted);
        let c_w = ctx.clone();
        let p_w = seg1_params.clone();
        let h_seg =
            thread::spawn(move || l_w.handle_segment_submit(&p_w, &c_w, d_w.as_ref(), 5000));
        assert_eq!(rx1.recv_timeout(Duration::from_secs(2)).unwrap(), 2);

        let paused_1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused_1.state, LeaseState::Paused);
        let seg1_paused = h_seg.join().unwrap().unwrap();
        assert_eq!(seg1_paused.state, SegmentState::Paused);
        let pause_snap = paused_1.pause_snapshot.as_ref().unwrap();
        assert_eq!(pause_snap.cursor.as_ref().unwrap().next_event_index, 2);

        let plan_hash = "b".repeat(64);
        leases
            .register_confirmation(
                "grant-resume-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_snap.pause_id,
                    "resumeAttemptId": "att-resume-1",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-resume-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(resumed.state, LeaseState::Active);

        let actions: Vec<NativeInputAction> = scripted
            .inner
            .submitted_actions()
            .into_iter()
            .map(|(_, act)| act)
            .collect();
        assert_eq!(
            &actions[actions.len() - 2..],
            &[
                NativeInputAction::KeyUp("KeyS".to_owned()),
                NativeInputAction::KeyUp("ControlLeft".to_owned()),
            ]
        );

        let st_1 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-resume-1", "payloadHash": h1})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_1.state, SegmentState::Sent);
        assert_eq!(st_1.cleanup, CleanupOutcome::NotRequired);
        assert_eq!(st_1.pause_id, None);

        // Verify held_inputs is now empty by pausing cleanly.
        let post_pause = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let p2_snap = post_pause.pause_snapshot.unwrap();
        assert!(p2_snap.held_inputs.is_empty());

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "reason": "finished"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Part B: Releasing a paused mid-segment lease must invalidate Paused+pauseId receipt -> Uncertain.
        let acq_b = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-b", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let seg2_params = json_obj(json!({
            "leaseId": acq_b.lease_id,
            "generation": acq_b.generation,
            "segmentId": "seg-mid-release",
            "payloadHash": h1,
            "targetToken": "target-a",
            "operation": chord_op
        }));
        let (tx2, rx2) = mpsc::channel();
        scripted.notify_submit_after(2, tx2);
        let l_w2 = Arc::clone(&leases);
        let d_w2 = Arc::clone(&scripted);
        let c_w2 = ctx.clone();
        let p_w2 = seg2_params;
        let h_seg2 =
            thread::spawn(move || l_w2.handle_segment_submit(&p_w2, &c_w2, d_w2.as_ref(), 5000));
        assert_eq!(rx2.recv_timeout(Duration::from_secs(2)).unwrap(), 2);

        let paused_b = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_b.lease_id, "generation": acq_b.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused_b.state, LeaseState::Paused);
        assert_eq!(h_seg2.join().unwrap().unwrap().state, SegmentState::Paused);

        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let st_2 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-mid-release", "payloadHash": h1})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_2.state, SegmentState::Uncertain);
        assert_eq!(st_2.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(st_2.pause_id, None);

        // Part C: Silent expiry of a paused mid-segment lease must also invalidate stale Paused receipt.
        let acq_c = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-c", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let seg3_params = json_obj(json!({
            "leaseId": acq_c.lease_id,
            "generation": acq_c.generation,
            "segmentId": "seg-mid-expire",
            "payloadHash": h1,
            "targetToken": "target-a",
            "operation": chord_op
        }));
        let (tx3, rx3) = mpsc::channel();
        scripted.notify_submit_after(2, tx3);
        let l_w3 = Arc::clone(&leases);
        let d_w3 = Arc::clone(&scripted);
        let c_w3 = ctx.clone();
        let p_w3 = seg3_params;
        let h_seg3 =
            thread::spawn(move || l_w3.handle_segment_submit(&p_w3, &c_w3, d_w3.as_ref(), 5000));
        assert_eq!(rx3.recv_timeout(Duration::from_secs(2)).unwrap(), 2);

        let paused_c = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_c.lease_id, "generation": acq_c.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused_c.state, LeaseState::Paused);
        assert_eq!(h_seg3.join().unwrap().unwrap().state, SegmentState::Paused);

        scripted.inner.fake_clock().advance_us(2_000_000);
        let st_3 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-mid-expire", "payloadHash": h1})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_3.state, SegmentState::Uncertain);
        assert_eq!(st_3.pause_id, None);
    }

    #[test]
    fn mid_segment_pause_pending_cleanup_preserves_cursor_and_avoids_duplicate_finalize() {
        use deskweave_platform_api::{
            CleanupOutcome, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let chord_op = json!({
            "id": "chord-pending",
            "title": "待定清理测试",
            "action": "keyboard.chord",
            "params": {"target": "main", "keys": ["ControlLeft", "KeyS"], "pressDurationMs": 300},
            "on": {}
        });
        let seg_hash = compute_segment_payload_hash("target-a", &chord_op, None).unwrap();
        let seg_params = json_obj(json!({
            "leaseId": acq.lease_id,
            "generation": acq.generation,
            "segmentId": "seg-pending-pause",
            "payloadHash": seg_hash,
            "targetToken": "target-a",
            "operation": chord_op
        }));

        scripted.inner.set_cleanup_outcome(CleanupOutcome::Pending);
        let (tx, rx) = mpsc::channel();
        scripted.notify_submit_after(2, tx);

        let l_w = Arc::clone(&leases);
        let d_w = Arc::clone(&scripted);
        let c_w = ctx.clone();
        let h_seg =
            thread::spawn(move || l_w.handle_segment_submit(&seg_params, &c_w, d_w.as_ref(), 5000));
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap(), 2);

        let pause_1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(pause_1.state, LeaseState::Pausing);
        assert_eq!(pause_1.cleanup, CleanupOutcome::Pending);
        assert!(pause_1.pause_snapshot.is_none());

        let seg_res = h_seg.join().unwrap().unwrap();
        assert_eq!(seg_res.state, SegmentState::Running);
        assert_eq!(seg_res.cleanup, CleanupOutcome::Pending);
        assert_eq!(seg_res.pause_id, None);
        assert_eq!(scripted.inner.released_batches().len(), 1);

        // Retry pause while Pausing(Pending): must return existing state without calling release_held again.
        let pause_2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(pause_2, pause_1);
        assert_eq!(scripted.inner.released_batches().len(), 1);

        // Releasing Pausing(Pending) updates both lease and unfinished segment receipt to Uncertain(Pending).
        let rel = leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(rel.cleanup, CleanupOutcome::Pending);

        let seg_status = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-pending-pause", "payloadHash": seg_hash})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(seg_status.state, SegmentState::Uncertain);
        assert_eq!(seg_status.cleanup, CleanupOutcome::Pending);
        assert_eq!(seg_status.pause_id, None);
    }

    #[test]
    fn lease_pause_timeout_racing_with_last_event_still_finalizes_pause_and_releases_keys() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let hold_op = json!({
            "id": "hold-shift-race",
            "title": "末事件竞争测试",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let hold_hash = compute_segment_payload_hash("target-a", &hold_op, None).unwrap();
        let (entered_rx, release_tx) = scripted.gate_submit_on(1);

        let l_w = Arc::clone(&leases);
        let d_w = Arc::clone(&scripted);
        let c_w = ctx.clone();
        let acq_id = acq.lease_id.clone();
        let acq_gen = acq.generation;
        let h_seg = thread::spawn(move || {
            l_w.handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_id,
                    "generation": acq_gen,
                    "segmentId": "seg-last-race",
                    "payloadHash": hold_hash,
                    "targetToken": "target-a",
                    "operation": hold_op
                })),
                &c_w,
                d_w.as_ref(),
                5000,
            )
        });
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();

        let pause_timeout = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                20,
            )
            .unwrap();
        assert_eq!(pause_timeout.state, LeaseState::Pausing);

        release_tx.send(()).unwrap();
        assert_eq!(h_seg.join().unwrap().unwrap().state, SegmentState::Sent);

        let final_status = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(final_status.state, LeaseState::Paused);
        assert_eq!(final_status.cleanup, CleanupOutcome::Succeeded);
        let snap = final_status.pause_snapshot.unwrap();
        assert_eq!(
            snap.held_inputs,
            vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
    }

    #[test]
    fn concurrent_lease_release_and_disconnect_wait_for_in_flight_submit_without_key_leak() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, SegmentState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted
            .inner
            .register_grant("grant-b", target_desc("target-b", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx_a = make_ctx("sess-a", "ws-a");
        let ctx_b = make_ctx("sess-b", "ws-b");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx_b,
                scripted.as_ref(),
            )
            .unwrap();

        // Part A: lease.release waits for blocked submit_event(ShiftLeft) and releases it before unlocking.
        let acq_1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-rel-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let hold_shift = json!({
            "id": "hold-shift-rel",
            "title": "按住释放竞态",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_shift = compute_segment_payload_hash("target-a", &hold_shift, None).unwrap();
        let (entered_1, release_1) = scripted.gate_submit_on(1);

        let l_s1 = Arc::clone(&leases);
        let d_s1 = Arc::clone(&scripted);
        let c_s1 = ctx_a.clone();
        let acq1_id_s = acq_1.lease_id.clone();
        let acq1_gen_s = acq_1.generation;
        let seg_h1 = thread::spawn(move || {
            l_s1.handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq1_id_s,
                    "generation": acq1_gen_s,
                    "segmentId": "seg-rel-race",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": hold_shift
                })),
                &c_s1,
                d_s1.as_ref(),
                5000,
            )
        });
        entered_1.recv_timeout(Duration::from_secs(2)).unwrap();

        let l_r1 = Arc::clone(&leases);
        let d_r1 = Arc::clone(&scripted);
        let c_r1 = ctx_a.clone();
        let acq1_id = acq_1.lease_id.clone();
        let acq1_gen = acq_1.generation;
        let rel_h1 = thread::spawn(move || {
            l_r1.handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq1_id,
                    "generation": acq1_gen,
                    "reason": "cancelled"
                })),
                &c_r1,
                d_r1.as_ref(),
                5000,
            )
        });
        thread::sleep(Duration::from_millis(30));
        assert!(!rel_h1.is_finished());

        release_1.send(()).unwrap();
        assert_eq!(seg_h1.join().unwrap().unwrap().state, SegmentState::Sent);
        let rel_res = rel_h1.join().unwrap().unwrap();
        assert_eq!(rel_res.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );

        // Part B: on_session_closed waits for blocked Uncertain submit_event and keeps desktop locked.
        let acq_2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-rel-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let hold_ctrl = json!({
            "id": "hold-ctrl-disc",
            "title": "断连不确定竞态",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_ctrl = compute_segment_payload_hash("target-a", &hold_ctrl, None).unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Uncertain));
        let (entered_2, release_2) = scripted.gate_submit_on(1);

        let l_s2 = Arc::clone(&leases);
        let d_s2 = Arc::clone(&scripted);
        let c_s2 = ctx_a.clone();
        let seg_h2 = thread::spawn(move || {
            l_s2.handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_2.lease_id,
                    "generation": acq_2.generation,
                    "segmentId": "seg-disc-uncertain",
                    "payloadHash": h_ctrl,
                    "targetToken": "target-a",
                    "operation": hold_ctrl
                })),
                &c_s2,
                d_s2.as_ref(),
                5000,
            )
        });
        entered_2.recv_timeout(Duration::from_secs(2)).unwrap();

        let l_d2 = Arc::clone(&leases);
        let d_d2 = Arc::clone(&scripted);
        let disc_h2 = thread::spawn(move || {
            l_d2.on_session_closed("sess-a", d_d2.as_ref());
        });
        thread::sleep(Duration::from_millis(30));
        assert!(!disc_h2.is_finished());

        release_2.send(()).unwrap();
        assert_eq!(
            seg_h2.join().unwrap().unwrap().state,
            SegmentState::Uncertain
        );
        disc_h2.join().unwrap();

        let acq_b_err = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-b-blocked", "targets": ["target-b"], "waitMs": 0})),
                &ctx_b,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(acq_b_err.code, "EFFECT_UNCERTAIN");
    }

    #[test]
    fn topology_and_target_version_errors_fail_closed_in_production_mode() {
        use deskweave_platform_api::LeaseState;

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-a", "ws-a");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        scripted
            .inner
            .set_topology(Err(PlatformError::session_unavailable("locked")));
        let pause_topo_err = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(pause_topo_err.code, "SESSION_UNAVAILABLE");

        #[cfg(not(feature = "test-authority"))]
        {
            scripted
                .inner
                .set_topology(Err(PlatformError::capability_not_implemented("unimpl")));
            let pause_unimpl_err = leases
                .handle_lease_pause(
                    &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap_err();
            assert_eq!(pause_unimpl_err.code, "CAPABILITY_NOT_IMPLEMENTED");
        }

        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq.lease_id})),
                    &ctx,
                    scripted.as_ref(),
                )
                .unwrap()
                .state,
            LeaseState::Active
        );

        scripted.inner.set_topology(Ok(base_topo.clone()));
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused.state, LeaseState::Paused);
        let pause_id = paused.pause_snapshot.as_ref().unwrap().pause_id.clone();

        scripted
            .inner
            .set_topology(Err(PlatformError::session_unavailable("locked")));
        let resume_topo_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_id,
                    "resumeAttemptId": "att-topo-err",
                    "resumePlanHash": "b".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(resume_topo_err.code, "SESSION_UNAVAILABLE");

        scripted.inner.set_topology(Ok(base_topo));
        scripted.set_target_version_override(Some(Err(PlatformError::session_unavailable(
            "target-session-lost",
        ))));
        let resume_tv_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": pause_id,
                    "resumeAttemptId": "att-tv-err",
                    "resumePlanHash": "b".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(resume_tv_err.code, "SESSION_UNAVAILABLE");

        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq.lease_id})),
                    &ctx,
                    scripted.as_ref(),
                )
                .unwrap()
                .state,
            LeaseState::Paused
        );
    }

    #[test]
    fn zero_delay_segment_enforces_deadline_and_expired_token_allows_cross_workspace_rebind() {
        use deskweave_platform_api::{
            NativeInputAction, SegmentState, compute_segment_payload_hash, format_utc_timestamp,
        };
        use std::time::{SystemTime, UNIX_EPOCH};

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx_1 = make_ctx("sess-1", "ws-1");
        let ctx_2 = make_ctx("sess-2", "ws-2");

        // Part A (P3-1): Expired unreferenced bound_targets entry owned by ws-1 allows ws-2 re-bind.
        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let short_exp_secs = now_secs + 1;
        scripted.push_bind_override(Ok(target_desc(
            "rebind-tok",
            &format_utc_timestamp(short_exp_secs),
        )));
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-rebind-1"})),
                &ctx_1,
                scripted.as_ref(),
            )
            .unwrap();
        loop {
            let cur_secs = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_secs() as i64;
            if cur_secs >= short_exp_secs {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }

        scripted.push_bind_override(Ok(target_desc("rebind-tok", "2099-01-01T00:00:00Z")));
        let rebound = leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-rebind-2"})),
                &ctx_2,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(rebound.target_token, "rebind-tok");

        // Part B (P2-2): In-flight segment.status returns Running; zero-delay loop checks deadlineMs.
        let acq = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-zero-delay", "targets": ["rebind-tok"], "waitMs": 0}),
                ),
                &ctx_2,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let text_op = json!({
            "id": "zero-delay-text",
            "title": "零延迟文本",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "abc", "intervalMs": 0},
            "on": {}
        });
        let h_text = compute_segment_payload_hash("rebind-tok", &text_op, None).unwrap();
        let (entered_2, release_2) = scripted.gate_submit_on(2);

        let l_w = Arc::clone(&leases);
        let d_w = Arc::clone(&scripted);
        let c_w = ctx_2.clone();
        let acq_id = acq.lease_id.clone();
        let acq_gen = acq.generation;
        let op_clone = text_op;
        let h_clone = h_text.clone();
        let h_seg = thread::spawn(move || {
            l_w.handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_id,
                    "generation": acq_gen,
                    "segmentId": "seg-zero-delay",
                    "payloadHash": h_clone,
                    "targetToken": "rebind-tok",
                    "operation": op_clone
                })),
                &c_w,
                d_w.as_ref(),
                25,
            )
        });

        entered_2.recv_timeout(Duration::from_secs(2)).unwrap();

        let running_status = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-zero-delay", "payloadHash": h_text})),
                &ctx_2,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(running_status.state, SegmentState::Running);

        // Same segmentId with different payloadHash while Running returns REQUEST_CONFLICT (not DESKTOP_BUSY).
        let other_op = json!({
            "id": "other",
            "title": "不同参数",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "z", "intervalMs": 0},
            "on": {}
        });
        let h_other = compute_segment_payload_hash("rebind-tok", &other_op, None).unwrap();
        let conflict_err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-zero-delay",
                    "payloadHash": h_other,
                    "targetToken": "rebind-tok",
                    "operation": other_op
                })),
                &ctx_2,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(conflict_err.code, "REQUEST_CONFLICT");

        thread::sleep(Duration::from_millis(40));
        release_2.send(()).unwrap();

        let finished = h_seg.join().unwrap().unwrap();
        assert_eq!(finished.state, SegmentState::Uncertain);
        let emitted: Vec<NativeInputAction> = scripted
            .inner
            .submitted_actions()
            .into_iter()
            .map(|(_, act)| act)
            .collect();
        assert!(!emitted.contains(&NativeInputAction::UnicodeText("c".to_owned())));
    }

    #[test]
    fn concurrent_pause_release_and_release_timeout_do_not_panic_or_corrupt_next_workspace_lease() {
        use deskweave_platform_api::{
            CleanupOutcome, LeaseState, NativeInputAction, SegmentState,
            compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted
            .inner
            .register_grant("grant-b", target_desc("target-b", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx_a = make_ctx("sess-a", "ws-a");
        let ctx_b = make_ctx("sess-b", "ws-b");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx_b,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: lease.release timeout clears release_waiters cleanly and allows normal completion.
        let acq_a1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-timeout", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let text_op = json!({
            "id": "rel-timeout-text",
            "title": "释放超时清理等待计数",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "ab", "intervalMs": 0},
            "on": {}
        });
        let h_text = compute_segment_payload_hash("target-a", &text_op, None).unwrap();
        let (entered_a1, release_a1) = scripted.gate_submit_on(1);
        let seg_a1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a1.lease_id.clone();
            let lease_gen = acq_a1.generation;
            let op = text_op.clone();
            let h = h_text.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-rel-timeout",
                        "payloadHash": h,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_a1.recv().unwrap();
        let timeout_err = leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a1.lease_id,
                    "generation": acq_a1.generation,
                    "reason": "cancelled"
                })),
                &ctx_a,
                scripted.as_ref(),
                20,
            )
            .unwrap_err();
        assert_eq!(timeout_err.code, "DEADLINE_EXCEEDED");
        release_a1.send(()).unwrap();
        assert_eq!(seg_a1.join().unwrap().unwrap().state, SegmentState::Sent);
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a1.lease_id,
                    "generation": acq_a1.generation,
                    "reason": "finished"
                })),
                &ctx_a,
                scripted.as_ref(),
                2000,
            )
            .unwrap();

        // Subcase B: fresh_submit == false continuation aborted at dispatched_count == 0 records Rejected.
        scripted
            .inner
            .set_pause_pointer(deskweave_platform_api::PausePoint::Display(
                deskweave_platform_api::DisplayPoint {
                    display_id: "main".to_owned(),
                    x: 0.0,
                    y: 0.0,
                },
            ));
        let acq_a2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-cont-abort", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let move_op = json!({
            "id": "slow-move-abort",
            "title": "恢复零派发释放中止",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "displayPixels", "displayId": "main", "x": 120.0, "y": 80.0},
                "durationMs": 200_000
            },
            "on": {}
        });
        let h_move = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        let seg_a2_pause = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a2.lease_id.clone();
            let lease_gen = acq_a2.generation;
            let op = move_op;
            let h = h_move.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-cont-abort",
                        "payloadHash": h,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        let deadline_wait = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(st) = leases.handle_segment_status(
                &json_obj(json!({"segmentId": "seg-cont-abort", "payloadHash": h_move})),
                &ctx_a,
                scripted.as_ref(),
            ) && st.state == SegmentState::Running
            {
                break;
            }
            assert!(std::time::Instant::now() < deadline_wait);
            thread::sleep(Duration::from_millis(1));
        }
        let paused_a2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_a2.lease_id, "generation": acq_a2.generation})),
                &ctx_a,
                scripted.as_ref(),
                2000,
            )
            .unwrap();
        assert_eq!(paused_a2.state, LeaseState::Paused);
        let pause_snap = paused_a2.pause_snapshot.unwrap();
        assert_eq!(pause_snap.cursor.as_ref().unwrap().next_event_index, 0);
        let pause_seg_res = seg_a2_pause.join().unwrap().unwrap();
        assert_eq!(pause_seg_res.state, SegmentState::Paused);

        let plan_hash_b = "d".repeat(64);
        leases
            .register_confirmation(
                "grant-cont-abort",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx_a.workspace_id.clone(),
                    lease_id: acq_a2.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx_a.agent_epoch.clone(),
                    generation: acq_a2.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash_b.clone(),
                    restore_target_token: pause_snap.input_target_token.clone(),
                    pointer_to_restore: Some(pause_snap.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let (entered_restore_b, release_restore_b) = scripted.gate_submit_on(1);
        let h_res_b = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a2.lease_id.clone();
            let lease_gen = acq_a2.generation;
            let pid = pause_snap.pause_id.clone();
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "pauseId": pid,
                        "resumeAttemptId": "att-cont-abort",
                        "resumePlanHash": plan_hash_b,
                        "confirmationGrant": "grant-cont-abort"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_restore_b.recv().unwrap();
        release_restore_b.send(()).unwrap();
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a2.lease_id,
                    "generation": acq_a2.generation,
                    "reason": "cancelled"
                })),
                &ctx_a,
                scripted.as_ref(),
                2000,
            )
            .unwrap();
        let _ = h_res_b.join().unwrap();
        let st_b = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-cont-abort", "payloadHash": h_move})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_b.state, SegmentState::Rejected);

        // Subcase C: Concurrent lease.pause + lease.release + immediate ws-b lease.acquire with held ShiftLeft.
        let acq_a3 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-race", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let hold_shift = json!({
            "id": "hold-shift-race",
            "title": "并发释放保持键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_shift = compute_segment_payload_hash("target-a", &hold_shift, None).unwrap();
        let shift_res = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a3.lease_id,
                    "generation": acq_a3.generation,
                    "segmentId": "seg-shift-race",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": hold_shift
                })),
                &ctx_a,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(shift_res.state, SegmentState::Sent);

        let mid_text = json!({
            "id": "mid-race-text",
            "title": "并发暂停释放竞态",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "ab", "intervalMs": 200},
            "on": {}
        });
        let h_mid = compute_segment_payload_hash("target-a", &mid_text, None).unwrap();
        let h_mid_q = h_mid.clone();
        let (entered_mid, release_mid) = scripted.gate_submit_on(1);
        let h_seg_mid = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a3.lease_id.clone();
            let lease_gen = acq_a3.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-mid-race",
                        "payloadHash": h_mid,
                        "targetToken": "target-a",
                        "operation": mid_text
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_mid.recv().unwrap();

        let h_pause = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a3.lease_id.clone();
            let lease_gen = acq_a3.generation;
            thread::spawn(move || {
                l.handle_lease_pause(
                    &json_obj(json!({"leaseId": lid, "generation": lease_gen})),
                    &c,
                    d.as_ref(),
                    2000,
                )
            })
        };
        let h_release = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_a.clone();
            let lid = acq_a3.lease_id.clone();
            let lease_gen = acq_a3.generation;
            thread::spawn(move || {
                l.handle_lease_release(
                    &json_obj(
                        json!({"leaseId": lid, "generation": lease_gen, "reason": "cancelled"}),
                    ),
                    &c,
                    d.as_ref(),
                    2000,
                )
            })
        };
        thread::sleep(Duration::from_millis(30));
        release_mid.send(()).unwrap();

        let rel_res = h_release.join().unwrap().unwrap();
        assert_eq!(rel_res.cleanup, CleanupOutcome::Succeeded);

        let acq_b = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-wsb-next", "targets": ["target-b"], "waitMs": 0})),
                &ctx_b,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let mid_res = h_seg_mid.join().unwrap().unwrap();
        assert_eq!(mid_res.state, SegmentState::Uncertain);
        assert_eq!(mid_res.cleanup, CleanupOutcome::Succeeded);
        let _pause_out = h_pause.join().unwrap();

        let st_b_lease = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_b.lease_id})),
                &ctx_b,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_b_lease.state, LeaseState::Active);

        let st_a_mid = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-mid-race", "payloadHash": h_mid_q})),
                &ctx_a,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_a_mid.state, SegmentState::Uncertain);
        assert!(
            scripted
                .inner
                .submitted_actions()
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::UnicodeText(t) if t == "a"))
        );
    }

    #[test]
    fn resumed_zero_dispatched_pause_failure_marks_segment_rejected() {
        use deskweave_platform_api::{LeaseState, SegmentState, compute_segment_payload_hash};

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .set_pause_pointer(deskweave_platform_api::PausePoint::Display(
                deskweave_platform_api::DisplayPoint {
                    display_id: "main".to_owned(),
                    x: 0.0,
                    y: 0.0,
                },
            ));
        scripted
            .inner
            .register_grant("grant-p", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-p", "ws-p");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-p"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-p2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let move_op = json!({
            "id": "slow-move-p2",
            "title": "恢复零派发暂停失败标记拒绝",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "displayPixels", "displayId": "main", "x": 140.0, "y": 90.0},
                "durationMs": 200_000
            },
            "on": {}
        });
        let h_move = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        let h_move_q = h_move.clone();
        let h_seg = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lease_gen = acq.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-p2",
                        "payloadHash": h_move,
                        "targetToken": "target-a",
                        "operation": move_op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };

        let wait_limit = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(st) = leases.handle_segment_status(
                &json_obj(json!({"segmentId": "seg-p2", "payloadHash": h_move_q})),
                &ctx,
                scripted.as_ref(),
            ) && st.state == SegmentState::Running
            {
                break;
            }
            assert!(std::time::Instant::now() < wait_limit);
            thread::sleep(Duration::from_millis(1));
        }

        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                2000,
            )
            .unwrap();
        assert_eq!(paused.state, LeaseState::Paused);
        let snap = paused.pause_snapshot.unwrap();
        assert_eq!(snap.cursor.as_ref().unwrap().next_event_index, 0);
        assert_eq!(h_seg.join().unwrap().unwrap().state, SegmentState::Paused);

        let plan_hash = "e".repeat(64);
        leases
            .register_confirmation(
                "grant-res-p2",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: snap.input_target_token.clone(),
                    pointer_to_restore: Some(snap.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let h_resume = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lease_gen = acq.generation;
            let pid = snap.pause_id.clone();
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "pauseId": pid,
                        "resumeAttemptId": "att-p2",
                        "resumePlanHash": plan_hash,
                        "confirmationGrant": "grant-res-p2"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        let resume_wait_limit = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(st) = leases.handle_segment_status(
                &json_obj(json!({"segmentId": "seg-p2", "payloadHash": h_move_q})),
                &ctx,
                scripted.as_ref(),
            ) && st.state == SegmentState::Running
            {
                break;
            }
            assert!(std::time::Instant::now() < resume_wait_limit);
            thread::sleep(Duration::from_millis(1));
        }
        scripted.set_target_version_override(Some(Err(PlatformError::new(
            "SESSION_UNAVAILABLE",
            "目标版本暂不可用",
        ))));

        let pause_err = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                2000,
            )
            .unwrap_err();
        assert_eq!(pause_err.code, "SESSION_UNAVAILABLE");

        let res_snap = h_resume.join().unwrap().unwrap();
        assert_eq!(res_snap.state, LeaseState::Active);

        let final_seg = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-p2", "payloadHash": h_move_q})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(final_seg.state, SegmentState::Rejected);
    }

    struct HostConfirmInput<'a> {
        signing_key: &'a SigningKey,
        agent_epoch: &'a str,
        workspace_id: &'a str,
        nonce: &'a [u8],
        lease_id: &'a str,
        pause_id: &'a str,
        generation: u64,
        checkpoint_hash: &'a str,
        resume_plan_hash: &'a str,
        restore_target_token: Option<&'a str>,
        pointer_to_restore: Option<&'a deskweave_platform_api::PausePoint>,
        ttl_ms: u64,
    }

    fn sign_host_confirmation(input: HostConfirmInput<'_>) -> Value {
        let claim = json!({
            "kind": "deskweave/agent-confirmation/v1",
            "agentPid": process::id(),
            "agentEpoch": input.agent_epoch,
            "owner": ["1", "2"],
            "scope": "b".repeat(64),
            "workspaceId": input.workspace_id,
            "purpose": "lease.resume",
            "nonce": input.nonce,
            "leaseId": input.lease_id,
            "pauseId": input.pause_id,
            "generation": input.generation,
            "checkpointHash": input.checkpoint_hash,
            "resumePlanHash": input.resume_plan_hash,
            "restoreTargetToken": input.restore_target_token,
            "pointerToRestore": input.pointer_to_restore,
            "ttlMs": input.ttl_ms
        });
        let encoded = serde_json::to_string(&claim).unwrap();
        let signature = input
            .signing_key
            .sign(encoded.as_bytes())
            .to_bytes()
            .to_vec();
        json!({
            "kind": "agent.confirmation.register",
            "version": VERSION,
            "claim": encoded,
            "signature": signature
        })
    }

    fn read_json_frame(reader: &mut MemReader, stop: &StopSignal) -> io::Result<Value> {
        let deadline = Some(Instant::now() + Duration::from_secs(3));
        let raw = read_frame_bytes(
            &mut DeadlineReader::new(reader, deadline, stop),
            MAX_CONTROL_FRAME,
        )?
        .ok_or_else(|| io::Error::from(io::ErrorKind::UnexpectedEof))?;
        serde_json::from_slice(&raw).map_err(|_| io::ErrorKind::InvalidData.into())
    }

    fn write_json_frame(
        writer: &mut MemWriter,
        value: &Value,
        stop: &StopSignal,
    ) -> io::Result<()> {
        let deadline = Some(Instant::now() + Duration::from_secs(3));
        write_frame_bounded(
            &mut DeadlineWriter::new(writer, deadline, stop),
            value,
            MAX_CONTROL_FRAME,
        )
    }

    #[test]
    fn production_host_confirmation_registration_and_resume_end_to_end() {
        use deskweave_platform_api::{HeldInput, PausePoint, PhysicalPoint};

        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let wrong_key = SigningKey::from_bytes(&[0x6b; 32]);
        let (server_init, mut client) = MemStream::pair();
        let (_tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let (topo, caps) = macos_topology_and_caps();
        let fake = Arc::new(FakeDesktopSession::new(caps, Ok(topo)));
        fake.register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        fake.set_pointer(PhysicalPoint { x: 150, y: 210 });

        let config = test_config(
            &key,
            Duration::from_millis(120),
            4,
            Arc::clone(&fake) as Arc<dyn DesktopSession>,
        );
        let server_thread = {
            let stop = stop.clone();
            thread::spawn(move || run(server_init, initial_ctx(), MemListener { rx }, config, stop))
        };

        assert_eq!(
            client_ping(&mut client, &client_stop).unwrap()["kind"],
            "agent.pong"
        );

        let bind_res = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-bind",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-a"}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(bind_res["ok"], true);

        let acq = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-acq",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {"runId": "run-prod", "targets": ["target-a"], "waitMs": 0}
            }),
            &client_stop,
        )
        .unwrap();
        let lease_id = acq["result"]["leaseId"].as_str().unwrap().to_owned();
        let lgen = acq["result"]["generation"].as_u64().unwrap();

        let hold_op = json!({
            "id": "hold-alt",
            "title": "按住 Alt",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "AltLeft"},
            "on": {}
        });
        let h_hold = compute_segment_payload_hash("target-a", &hold_op, None).unwrap();
        assert_eq!(
            client_rpc(
                &mut client,
                &json!({
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "req-hold",
                    "session": "1".repeat(32),
                    "workspace": "ws-1",
                    "method": "segment.submit",
                    "deadlineMs": 5000,
                    "params": {
                        "leaseId": lease_id,
                        "generation": lgen,
                        "segmentId": "seg-hold",
                        "payloadHash": h_hold,
                        "targetToken": "target-a",
                        "operation": hold_op
                    }
                }),
                &client_stop
            )
            .unwrap()["ok"],
            true
        );

        let paused = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-pause",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.pause",
                "deadlineMs": 5000,
                "params": {"leaseId": lease_id, "generation": lgen}
            }),
            &client_stop,
        )
        .unwrap();
        let pause_snap = &paused["result"]["pauseSnapshot"];
        let pause_id = pause_snap["pauseId"].as_str().unwrap().to_owned();
        let cp_hash = pause_snap["checkpointHash"].as_str().unwrap().to_owned();
        let plan_hash = "d".repeat(64);
        let expected_ptr = PausePoint::Physical(PhysicalPoint { x: 150, y: 210 });

        // 1. Wrong signing key fails with ACCESS_DENIED and consumes single-use nonce.
        let chal1 = client_rpc(
            &mut client,
            &json!({"kind": "agent.confirmation.challenge.request"}),
            &client_stop,
        )
        .unwrap();
        assert_eq!(chal1["kind"], "agent.confirmation.challenge");
        let nonce1: Vec<u8> = chal1["nonce"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        let epoch_str = "a".repeat(64);
        let bad_sig_req = sign_host_confirmation(HostConfirmInput {
            signing_key: &wrong_key,
            agent_epoch: &epoch_str,
            workspace_id: "ws-1",
            nonce: &nonce1,
            lease_id: &lease_id,
            pause_id: &pause_id,
            generation: lgen,
            checkpoint_hash: &cp_hash,
            resume_plan_hash: &plan_hash,
            restore_target_token: Some("target-a"),
            pointer_to_restore: Some(&expected_ptr),
            ttl_ms: 10_000,
        });
        let bad_sig_res = client_rpc(&mut client, &bad_sig_req, &client_stop).unwrap();
        assert_eq!(bad_sig_res["kind"], "agent.confirmation.error");
        assert_eq!(bad_sig_res["code"], "ACCESS_DENIED");

        // Replaying the consumed nonce with the real key also fails.
        let replay_nonce_req = sign_host_confirmation(HostConfirmInput {
            signing_key: &key,
            agent_epoch: &epoch_str,
            workspace_id: "ws-1",
            nonce: &nonce1,
            lease_id: &lease_id,
            pause_id: &pause_id,
            generation: lgen,
            checkpoint_hash: &cp_hash,
            resume_plan_hash: &plan_hash,
            restore_target_token: Some("target-a"),
            pointer_to_restore: Some(&expected_ptr),
            ttl_ms: 10_000,
        });
        let replay_nonce_res = client_rpc(&mut client, &replay_nonce_req, &client_stop).unwrap();
        assert_eq!(replay_nonce_res["code"], "RESOURCE_DENIED");

        // 2. Old hardcoded test-confirm format is rejected even under any feature set.
        let forged_res = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-forged",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_id,
                    "generation": lgen,
                    "pauseId": pause_id,
                    "resumeAttemptId": "att-forged",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": format!("test-confirm:{pause_id}:{cp_hash}:{plan_hash}")
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(forged_res["error"]["code"], "RESOURCE_DENIED");

        // 3. Valid Host-signed confirmation registration mints unpredictable grant, then expires if TTL lapses.
        let chal_exp = client_rpc(
            &mut client,
            &json!({"kind": "agent.confirmation.challenge.request"}),
            &client_stop,
        )
        .unwrap();
        let nonce_exp: Vec<u8> = chal_exp["nonce"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        let short_reg = sign_host_confirmation(HostConfirmInput {
            signing_key: &key,
            agent_epoch: &epoch_str,
            workspace_id: "ws-1",
            nonce: &nonce_exp,
            lease_id: &lease_id,
            pause_id: &pause_id,
            generation: lgen,
            checkpoint_hash: &cp_hash,
            resume_plan_hash: &plan_hash,
            restore_target_token: Some("target-a"),
            pointer_to_restore: Some(&expected_ptr),
            ttl_ms: 100,
        });
        let short_grant =
            client_rpc(&mut client, &short_reg, &client_stop).unwrap()["confirmationGrant"]
                .as_str()
                .unwrap()
                .to_owned();
        fake.fake_clock().advance_us(150_000);
        let exp_res = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-exp-grant",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_id,
                    "generation": lgen,
                    "pauseId": pause_id,
                    "resumeAttemptId": "att-exp-grant",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": short_grant
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(exp_res["error"]["code"], "RESOURCE_DENIED");

        // 4. Fresh challenge -> valid confirmation -> resume succeeds and restores AltLeft.
        let chal2 = client_rpc(
            &mut client,
            &json!({"kind": "agent.confirmation.challenge.request"}),
            &client_stop,
        )
        .unwrap();
        let nonce2: Vec<u8> = chal2["nonce"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u8)
            .collect();
        let reg_req = sign_host_confirmation(HostConfirmInput {
            signing_key: &key,
            agent_epoch: &epoch_str,
            workspace_id: "ws-1",
            nonce: &nonce2,
            lease_id: &lease_id,
            pause_id: &pause_id,
            generation: lgen,
            checkpoint_hash: &cp_hash,
            resume_plan_hash: &plan_hash,
            restore_target_token: Some("target-a"),
            pointer_to_restore: Some(&expected_ptr),
            ttl_ms: 30_000,
        });
        let reg_res = client_rpc(&mut client, &reg_req, &client_stop).unwrap();
        assert_eq!(reg_res["kind"], "agent.confirmation.registered");
        let grant = reg_res["confirmationGrant"].as_str().unwrap().to_owned();
        assert_eq!(grant.len(), 64);

        let resume_res = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "req-res-ok",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.resume",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_id,
                    "generation": lgen,
                    "pauseId": pause_id,
                    "resumeAttemptId": "att-ok",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": grant
                }
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(resume_res["ok"], true);
        assert_eq!(resume_res["result"]["state"], "active");
        assert!(
            fake.submitted_actions()
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::KeyDown(k) if k == "AltLeft"))
        );
        assert!(
            !fake.released_batches().is_empty()
                || matches!(
                    fake.released_batches().last().map(|b| &b[0]),
                    Some(HeldInput::Key { key }) if key == "AltLeft"
                )
        );

        drop(client);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn protocol_multiplexing_keeps_lease_renew_and_pause_responsive_during_segment() {
        use deskweave_platform_api::SegmentState;

        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, client) = MemStream::pair();
        let (mut c_reader, mut c_writer) = client.split().unwrap();
        let (_tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let config = test_config(
            &key,
            Duration::from_millis(120),
            4,
            Arc::clone(&scripted) as Arc<dyn DesktopSession>,
        );
        let server_thread = {
            let stop = stop.clone();
            thread::spawn(move || run(server_init, initial_ctx(), MemListener { rx }, config, stop))
        };

        let send_and_recv = |reader: &mut MemReader, writer: &mut MemWriter, req: &Value| {
            write_json_frame(writer, req, &client_stop).unwrap();
            read_json_frame(reader, &client_stop).unwrap()
        };

        let _ = send_and_recv(
            &mut c_reader,
            &mut c_writer,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "mux-bind",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "target.bind",
                "deadlineMs": 5000,
                "params": {"selectionGrant": "grant-a"}
            }),
        );
        let acq = send_and_recv(
            &mut c_reader,
            &mut c_writer,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "mux-acq",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {"runId": "run-mux", "targets": ["target-a"], "waitMs": 0}
            }),
        );
        let lease_id = acq["result"]["leaseId"].as_str().unwrap().to_owned();
        let lgen = acq["result"]["generation"].as_u64().unwrap();

        let (entered_tx, release_rx) = scripted.gate_submit_on(1);
        let long_text = json!({
            "id": "mux-text",
            "title": "长段多路复用控制",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "ab", "intervalMs": 500},
            "on": {}
        });
        let h_text = compute_segment_payload_hash("target-a", &long_text, None).unwrap();

        // Send segment.submit WITHOUT waiting for its reply yet.
        write_json_frame(
            &mut c_writer,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "mux-seg",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": lease_id,
                    "generation": lgen,
                    "segmentId": "seg-mux",
                    "payloadHash": h_text,
                    "targetToken": "target-a",
                    "operation": long_text
                }
            }),
            &client_stop,
        )
        .unwrap();
        entered_tx.recv_timeout(Duration::from_secs(2)).unwrap();

        // Send lease.renew and lease.pause on the SAME connection while segment.submit is blocked in-flight.
        write_json_frame(
            &mut c_writer,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "mux-renew",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.renew",
                "deadlineMs": 5000,
                "params": {"leaseId": lease_id, "generation": lgen}
            }),
            &client_stop,
        )
        .unwrap();
        let renew_reply = read_json_frame(&mut c_reader, &client_stop).unwrap();
        assert_eq!(renew_reply["replyTo"], "mux-renew");
        assert_eq!(renew_reply["ok"], true);

        write_json_frame(
            &mut c_writer,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "mux-pause",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.pause",
                "deadlineMs": 5000,
                "params": {"leaseId": lease_id, "generation": lgen}
            }),
            &client_stop,
        )
        .unwrap();

        release_rx.send(()).unwrap();
        let r1 = read_json_frame(&mut c_reader, &client_stop).unwrap();
        let r2 = read_json_frame(&mut c_reader, &client_stop).unwrap();
        let mut by_id = std::collections::HashMap::new();
        by_id.insert(r1["replyTo"].as_str().unwrap().to_owned(), r1);
        by_id.insert(r2["replyTo"].as_str().unwrap().to_owned(), r2);

        assert_eq!(by_id["mux-pause"]["ok"], true);
        assert_eq!(by_id["mux-pause"]["result"]["state"], "paused");
        assert_eq!(by_id["mux-seg"]["ok"], true);
        let seg_res: deskweave_platform_api::SegmentResult =
            serde_json::from_value(by_id["mux-seg"]["result"].clone()).unwrap();
        assert_eq!(seg_res.state, SegmentState::Paused);
        assert_eq!(
            seg_res.pause_id.as_deref(),
            by_id["mux-pause"]["result"]["pauseSnapshot"]["pauseId"].as_str()
        );

        drop(c_reader);
        drop(c_writer);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn autonomous_watchdog_expires_idle_held_lease_and_stops_in_flight_segment_at_ttl() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client) = MemStream::pair();
        let (_tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let config = test_config(
            &key,
            Duration::from_millis(120),
            4,
            Arc::clone(&scripted) as Arc<dyn DesktopSession>,
        );
        let server_thread = {
            let stop = stop.clone();
            thread::spawn(move || run(server_init, initial_ctx(), MemListener { rx }, config, stop))
        };

        assert_eq!(
            client_rpc(
                &mut client,
                &json!({
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "wd-bind",
                    "session": "1".repeat(32),
                    "workspace": "ws-1",
                    "method": "target.bind",
                    "deadlineMs": 5000,
                    "params": {"selectionGrant": "grant-a"}
                }),
                &client_stop
            )
            .unwrap()["ok"],
            true
        );

        // Subcase A: Idle lease holding ShiftLeft is autonomously released by watchdog with zero incoming requests.
        let acq1 = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-acq1",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {"runId": "run-wd-1", "targets": ["target-a"], "waitMs": 0}
            }),
            &client_stop,
        )
        .unwrap();
        let l1 = acq1["result"]["leaseId"].as_str().unwrap().to_owned();
        let g1 = acq1["result"]["generation"].as_u64().unwrap();
        let hold_shift = json!({
            "id": "wd-hold",
            "title": "看门狗到期释放",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_hold = compute_segment_payload_hash("target-a", &hold_shift, None).unwrap();
        assert_eq!(
            client_rpc(
                &mut client,
                &json!({
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "wd-hold",
                    "session": "1".repeat(32),
                    "workspace": "ws-1",
                    "method": "segment.submit",
                    "deadlineMs": 5000,
                    "params": {
                        "leaseId": l1,
                        "generation": g1,
                        "segmentId": "seg-wd-hold",
                        "payloadHash": h_hold,
                        "targetToken": "target-a",
                        "operation": hold_shift
                    }
                }),
                &client_stop
            )
            .unwrap()["ok"],
            true
        );

        scripted.inner.fake_clock().advance_us(LEASE_TTL_US + 5_000);
        let poll_deadline = Instant::now() + Duration::from_secs(2);
        while scripted.inner.released_batches().is_empty() {
            assert!(
                Instant::now() < poll_deadline,
                "watchdog failed to release idle held key"
            );
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            scripted.inner.released_batches()[0],
            vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st1 = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-st1",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.status",
                "deadlineMs": 5000,
                "params": {"leaseId": l1}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(st1["result"]["state"], "released");
        assert_eq!(st1["result"]["reason"], "LEASE_EXPIRED");

        // Subcase B: Segment completion does NOT refresh TTL; advancing remaining TTL expires the lease.
        let acq2 = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-acq2",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {"runId": "run-wd-2", "targets": ["target-a"], "waitMs": 0}
            }),
            &client_stop,
        )
        .unwrap();
        let l2 = acq2["result"]["leaseId"].as_str().unwrap().to_owned();
        let g2 = acq2["result"]["generation"].as_u64().unwrap();
        scripted.inner.fake_clock().advance_us(1_950_000);
        let quick_key = json!({
            "id": "wd-quick",
            "title": "段完成不自续租期",
            "action": "keyboard.up",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_quick = compute_segment_payload_hash("target-a", &quick_key, None).unwrap();
        assert_eq!(
            client_rpc(
                &mut client,
                &json!({
                    "protocolVersion": "1.0",
                    "kind": "request",
                    "id": "wd-quick",
                    "session": "1".repeat(32),
                    "workspace": "ws-1",
                    "method": "segment.submit",
                    "deadlineMs": 5000,
                    "params": {
                        "leaseId": l2,
                        "generation": g2,
                        "segmentId": "seg-wd-quick",
                        "payloadHash": h_quick,
                        "targetToken": "target-a",
                        "operation": quick_key
                    }
                }),
                &client_stop
            )
            .unwrap()["result"]["state"],
            "sent"
        );
        scripted.inner.fake_clock().advance_us(100_000);
        let st2 = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-st2",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.status",
                "deadlineMs": 5000,
                "params": {"leaseId": l2}
            }),
            &client_stop,
        )
        .unwrap();
        assert_eq!(st2["result"]["state"], "released");
        assert_eq!(st2["result"]["reason"], "LEASE_EXPIRED");

        // Subcase C: In-flight segment stops before sending remaining events once TTL expires.
        let acq3 = client_rpc(
            &mut client,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-acq3",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.acquire",
                "deadlineMs": 5000,
                "params": {"runId": "run-wd-3", "targets": ["target-a"], "waitMs": 0}
            }),
            &client_stop,
        )
        .unwrap();
        let l3 = acq3["result"]["leaseId"].as_str().unwrap().to_owned();
        let g3 = acq3["result"]["generation"].as_u64().unwrap();
        let (entered_s3, release_s3) = scripted.gate_submit_on(1);
        let text_op = json!({
            "id": "wd-inflight",
            "title": "在途段到期终止",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "xy", "intervalMs": 500},
            "on": {}
        });
        let h_text = compute_segment_payload_hash("target-a", &text_op, None).unwrap();
        let count_before_y = scripted.inner.submitted_actions().len();
        let (mut c_r, mut c_w) = client.split().unwrap();
        write_json_frame(
            &mut c_w,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-seg3",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "segment.submit",
                "deadlineMs": 5000,
                "params": {
                    "leaseId": l3,
                    "generation": g3,
                    "segmentId": "seg-wd-3",
                    "payloadHash": h_text,
                    "targetToken": "target-a",
                    "operation": text_op
                }
            }),
            &client_stop,
        )
        .unwrap();
        entered_s3.recv_timeout(Duration::from_secs(2)).unwrap();
        release_s3.send(()).unwrap();
        scripted
            .inner
            .fake_clock()
            .advance_us(LEASE_TTL_US + 10_000);
        let seg3_reply = read_json_frame(&mut c_r, &client_stop).unwrap();
        let seg3_res: deskweave_platform_api::SegmentResult =
            serde_json::from_value(seg3_reply["result"].clone()).unwrap();
        assert_eq!(seg3_res.state, SegmentState::Uncertain);
        let tail_actions = &scripted.inner.submitted_actions()[count_before_y..];
        assert_eq!(tail_actions.len(), 1);
        assert_eq!(
            tail_actions[0].1,
            NativeInputAction::UnicodeText("x".to_owned())
        );
        let st3_lease = LeaseState::Released;
        write_json_frame(
            &mut c_w,
            &json!({
                "protocolVersion": "1.0",
                "kind": "request",
                "id": "wd-st3",
                "session": "1".repeat(32),
                "workspace": "ws-1",
                "method": "lease.status",
                "deadlineMs": 5000,
                "params": {"leaseId": l3}
            }),
            &client_stop,
        )
        .unwrap();
        let st3 = read_json_frame(&mut c_r, &client_stop).unwrap();
        assert_eq!(
            serde_json::from_value::<LeaseState>(st3["result"]["state"].clone()).unwrap(),
            st3_lease
        );
        assert_eq!(st3["result"]["reason"], "LEASE_EXPIRED");

        drop(c_r);
        drop(c_w);
        server_thread.join().unwrap().unwrap();
    }

    #[test]
    fn uncertain_first_press_and_resume_uncertain_press_are_cleaned_up() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, PointerButton, SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-u", "ws-u");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-u1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // First KeyDown returning Uncertain at dispatched_count == 0 must clean up KeyDown conservatively.
        let key_down = json!({
            "id": "unc-key",
            "title": "不确定首次按键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_k = compute_segment_payload_hash("target-a", &key_down, None).unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Uncertain));
        let k_res = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-unc-key",
                    "payloadHash": h_k,
                    "targetToken": "target-a",
                    "operation": key_down
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(k_res.state, SegmentState::Uncertain);
        assert_eq!(k_res.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );

        // Fresh session/coordinator: PointerButtonDown returning Uncertain at dispatched_count == 0 cleans up button.
        let (topo2, caps2) = macos_topology_and_caps();
        let scripted2 = Arc::new(ScriptedDesktop::new(caps2, Ok(topo2)));
        scripted2
            .inner
            .register_grant("grant-b", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases2 = LeaseCoordinator::new();
        leases2
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx,
                scripted2.as_ref(),
            )
            .unwrap();
        let acq2 = leases2
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-u2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let btn_down = json!({
            "id": "unc-btn",
            "title": "不确定首次按钮",
            "action": "pointer.button",
            "params": {"target": "main", "button": "left", "state": "down"},
            "on": {}
        });
        let h_b = compute_segment_payload_hash("target-a", &btn_down, None).unwrap();
        scripted2.push_submit_outcome(Ok(SubmissionOutcome::Uncertain));
        let b_res = leases2
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-unc-btn",
                    "payloadHash": h_b,
                    "targetToken": "target-a",
                    "operation": btn_down
                })),
                &ctx,
                scripted2.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(b_res.state, SegmentState::Uncertain);
        assert_eq!(b_res.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(
            scripted2.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Button {
                button: PointerButton::Left
            }]
        );

        // Fresh session/coordinator: handle_lease_resume cleans up Uncertain first KeyDown conservatively.
        let (topo3, caps3) = macos_topology_and_caps();
        let scripted3 = Arc::new(ScriptedDesktop::new(caps3, Ok(topo3)));
        scripted3
            .inner
            .register_grant("grant-c", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases3 = LeaseCoordinator::new();
        leases3
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-c"})),
                &ctx,
                scripted3.as_ref(),
            )
            .unwrap();
        let acq3 = leases3
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-u3", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted3.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let h_k3 = compute_segment_payload_hash("target-a", &key_down, None).unwrap();
        leases3
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq3.lease_id,
                    "generation": acq3.generation,
                    "segmentId": "seg-hold3",
                    "payloadHash": h_k3,
                    "targetToken": "target-a",
                    "operation": key_down
                })),
                &ctx,
                scripted3.as_ref(),
                5000,
            )
            .unwrap();
        let p3 = leases3
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq3.lease_id, "generation": acq3.generation})),
                &ctx,
                scripted3.as_ref(),
                5000,
            )
            .unwrap();
        let snap3 = p3.pause_snapshot.unwrap();
        let ph3 = "7".repeat(64);
        leases3
            .register_confirmation(
                "grant-u3",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq3.lease_id.clone(),
                    pause_id: snap3.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq3.generation,
                    checkpoint_hash: snap3.checkpoint_hash.clone(),
                    resume_plan_hash: ph3.clone(),
                    restore_target_token: snap3.input_target_token.clone(),
                    pointer_to_restore: Some(snap3.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        scripted3.push_submit_outcome(Ok(SubmissionOutcome::Submitted));
        scripted3.push_submit_outcome(Ok(SubmissionOutcome::Uncertain));
        let r3 = leases3
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq3.lease_id,
                    "generation": acq3.generation,
                    "pauseId": snap3.pause_id,
                    "resumeAttemptId": "att-u3",
                    "resumePlanHash": ph3,
                    "confirmationGrant": "grant-u3"
                })),
                &ctx,
                scripted3.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(r3.state, deskweave_platform_api::LeaseState::Uncertain);
        assert_eq!(
            scripted3.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
    }

    #[test]
    fn pointer_ledger_initializes_from_live_desktop_position_and_fails_closed() {
        use deskweave_platform_api::{PausePoint, PhysicalPoint, compute_segment_payload_hash};

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted.inner.set_pointer(PhysicalPoint { x: 420, y: 315 });

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-ptr", "ws-ptr");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-ptr", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // Keyboard-only pause preserves observed (420, 315) pointer instead of fake (0, 0).
        let kd = json!({
            "id": "ptr-kd",
            "title": "纯键盘暂停",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap = paused.pause_snapshot.unwrap();
        assert_eq!(
            snap.pointer,
            PausePoint::Physical(PhysicalPoint { x: 420, y: 315 })
        );

        let plan_hash = "1".repeat(64);
        leases
            .register_confirmation(
                "grant-ptr-res",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: snap.input_target_token.clone(),
                    pointer_to_restore: Some(snap.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-ptr",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-ptr-res"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Timed pointer.move starts smoothly from (420, 315), not jumping near (0, 0).
        let before = scripted.inner.submitted_actions().len();
        let move_op = json!({
            "id": "ptr-move",
            "title": "非原点起始平滑移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 520, "y": 415},
                "durationMs": 40
            },
            "on": {}
        });
        let h_m = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-move",
                    "payloadHash": h_m,
                    "targetToken": "target-a",
                    "operation": move_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let moved = &scripted.inner.submitted_actions()[before..];
        assert!(moved.len() >= 5);
        for (_, action) in moved {
            let NativeInputAction::PointerMovePhysical(pt) = action else {
                panic!("expected PointerMovePhysical");
            };
            assert!(pt.x >= 420 && pt.x <= 520);
            assert!(pt.y >= 315 && pt.y <= 415);
        }

        // Unimplemented/failing pointer_position fails closed when no prior pointer movement occurred.
        leases
            .handle_lease_release(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation, "reason": "finished"})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        scripted
            .inner
            .set_pointer_error(Some(PlatformError::session_unavailable("no pointer")));
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-ptr-err", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let fail_move_err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-fail-move",
                    "payloadHash": h_m,
                    "targetToken": "target-a",
                    "operation": move_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(fail_move_err.code, "SESSION_UNAVAILABLE");
        let fail_pause_err = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq2.lease_id, "generation": acq2.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(fail_pause_err.code, "SESSION_UNAVAILABLE");
    }

    #[test]
    fn mid_segment_target_or_topology_version_change_pauses_and_releases_held_inputs() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-ver", "ws-ver");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: Target version drift mid-segment stops next event, pauses, and releases held ShiftLeft.
        let acq1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-tv", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd = json!({
            "id": "tv-kd",
            "title": "版本漂移保持键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq1.lease_id,
                    "generation": acq1.generation,
                    "segmentId": "seg-tv-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let txt = json!({
            "id": "tv-txt",
            "title": "目标版本变化中断",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "ab", "intervalMs": 10},
            "on": {}
        });
        let h_txt = compute_segment_payload_hash("target-a", &txt, None).unwrap();
        let (entered1, release1) = scripted.gate_submit_on(1);
        let h_seg1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq1.lease_id.clone();
            let lg = acq1.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-tv-txt",
                        "payloadHash": h_txt,
                        "targetToken": "target-a",
                        "operation": txt
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered1.recv().unwrap();
        scripted.inner.set_target_version("target-a", "2");
        release1.send(()).unwrap();
        let seg1_res = h_seg1.join().unwrap().unwrap();
        assert_eq!(seg1_res.state, SegmentState::Paused);
        assert!(seg1_res.pause_id.is_some());
        let st1 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq1.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st1.state, LeaseState::Paused);
        assert_eq!(st1.reason.as_deref(), Some("TARGET_LOST"));
        let snap1 = st1.pause_snapshot.unwrap();
        assert_eq!(snap1.targets[0].target_version, "1");
        let plan_hash_tv = "a".repeat(64);
        leases
            .register_confirmation(
                "grant-tv-drift",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-ver".to_owned(),
                    lease_id: acq1.lease_id.clone(),
                    pause_id: snap1.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq1.generation,
                    checkpoint_hash: snap1.checkpoint_hash,
                    resume_plan_hash: plan_hash_tv.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        let tv_resume_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq1.lease_id,
                    "generation": acq1.generation,
                    "pauseId": snap1.pause_id,
                    "resumeAttemptId": "att-tv-drift",
                    "resumePlanHash": plan_hash_tv,
                    "confirmationGrant": "grant-tv-drift"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(tv_resume_err.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        assert!(
            !scripted
                .inner
                .submitted_actions()
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::UnicodeText(s) if s == "b"))
        );

        // Subcase B: Topology version drift mid-segment pauses with TOPOLOGY_CHANGED and releases held keys.
        leases
            .handle_lease_release(
                &json_obj(json!({"leaseId": acq1.lease_id, "generation": acq1.generation, "reason": "fault"})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        scripted.inner.set_target_version("target-a", "1");
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-topo", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd2 = json!({
            "id": "topo-kd",
            "title": "拓扑变化保持键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_kd2 = compute_segment_payload_hash("target-a", &kd2, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-topo-kd",
                    "payloadHash": h_kd2,
                    "targetToken": "target-a",
                    "operation": kd2
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let txt2 = json!({
            "id": "topo-txt",
            "title": "拓扑代次变化中断",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "mn", "intervalMs": 50},
            "on": {}
        });
        let h_txt2 = compute_segment_payload_hash("target-a", &txt2, None).unwrap();
        let (entered2, release2) = scripted.gate_submit_on(1);
        let h_seg2 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-topo-txt",
                        "payloadHash": h_txt2,
                        "targetToken": "target-a",
                        "operation": txt2
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered2.recv().unwrap();
        let base_topo_ver = base_topo.version().to_string();
        let mut topo_bumped = base_topo;
        match &mut topo_bumped {
            DisplayTopology::Physical(p) => p.version += 1,
            DisplayTopology::DisplayLocal(l) => l.version += 1,
        }
        scripted.inner.set_topology(Ok(topo_bumped));
        release2.send(()).unwrap();
        let seg2_res = h_seg2.join().unwrap().unwrap();
        assert_eq!(seg2_res.state, SegmentState::Paused);
        let st2 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq2.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st2.reason.as_deref(), Some("TOPOLOGY_CHANGED"));
        let snap2 = st2.pause_snapshot.unwrap();
        assert_eq!(snap2.targets[0].topology_version, base_topo_ver);
        let plan_hash_topo = "b".repeat(64);
        leases
            .register_confirmation(
                "grant-topo-drift",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-ver".to_owned(),
                    lease_id: acq2.lease_id.clone(),
                    pause_id: snap2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq2.generation,
                    checkpoint_hash: snap2.checkpoint_hash,
                    resume_plan_hash: plan_hash_topo.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        let topo_resume_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "pauseId": snap2.pause_id,
                    "resumeAttemptId": "att-topo-drift",
                    "resumePlanHash": plan_hash_topo,
                    "confirmationGrant": "grant-topo-drift"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(topo_resume_err.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
        assert!(
            !scripted
                .inner
                .submitted_actions()
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::UnicodeText(s) if s == "n"))
        );
    }

    #[test]
    fn absolute_deadlines_bounded_shift_and_timing_overrun_pause_preserve_frozen_state() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-time", "ws-time");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: Single >20ms stall triggers INPUT_TIMING_OVERRUN pause and releases held key.
        let acq1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-over-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd = json!({
            "id": "over-kd",
            "title": "超时保持键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq1.lease_id,
                    "generation": acq1.generation,
                    "segmentId": "seg-over-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let text_op = json!({
            "id": "over-txt",
            "title": "单次超限暂停",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "abc", "intervalMs": 10},
            "on": {}
        });
        let h_txt = compute_segment_payload_hash("target-a", &text_op, None).unwrap();
        let (entered_s1, release_s1) = scripted.gate_submit_on(1);
        let h_seg1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq1.lease_id.clone();
            let lg = acq1.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-over-txt",
                        "payloadHash": h_txt,
                        "targetToken": "target-a",
                        "operation": text_op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_s1.recv().unwrap();
        // Advance monotonic clock past event #1 deadline + 25ms single lateness budget before unblocking.
        scripted.inner.fake_clock().advance_us(35_000);
        release_s1.send(()).unwrap();
        let over_res = h_seg1.join().unwrap().unwrap();
        assert_eq!(over_res.state, SegmentState::Paused);
        assert!(over_res.pause_id.is_some());
        let st1 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq1.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st1.state, LeaseState::Paused);
        assert_eq!(st1.reason.as_deref(), Some("INPUT_TIMING_OVERRUN"));
        assert_eq!(
            st1.pause_snapshot
                .as_ref()
                .unwrap()
                .cursor
                .as_ref()
                .unwrap()
                .next_event_index,
            1
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );

        // Subcase B: Bounded 15ms suffix shifts accumulate across events and remain frozen across pause/resume.
        leases
            .handle_lease_release(
                &json_obj(json!({"leaseId": acq1.lease_id, "generation": acq1.generation, "reason": "fault"})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-over-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let long_txt = json!({
            "id": "cum-txt",
            "title": "累计顺延冻结验收",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "abcde", "intervalMs": 10},
            "on": {}
        });
        let h_long = compute_segment_payload_hash("target-a", &long_txt, None).unwrap();
        let h_long_q = h_long.clone();
        let (entered1, release1) = scripted.gate_submit_on(1);
        let h_seg2 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-cum",
                        "payloadHash": h_long,
                        "targetToken": "target-a",
                        "operation": long_txt
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered1.recv().unwrap();
        // Event 0 complete; advance 25ms so event 1 (deadline 10ms) sees 15ms lateness (<20ms single, 15ms cum).
        scripted.inner.fake_clock().advance_us(25_000);
        let (entered2, release2) = scripted.gate_submit_on(1);
        release1.send(()).unwrap();
        entered2.recv().unwrap();
        // Event 1 complete (shifted deadline 25ms); advance 28ms so event 2 (deadline 35ms) sees 18ms lateness (cum=33ms <= 50ms).
        scripted.inner.fake_clock().advance_us(28_000);
        let (entered3, release3) = scripted.gate_submit_on(1);
        release2.send(()).unwrap();
        entered3.recv().unwrap();
        // Pause cleanly during event 2 so event 3 preserves cumulative_shift=33ms across pause/resume.
        let h_pause = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            thread::spawn(move || {
                l.handle_lease_pause(
                    &json_obj(json!({"leaseId": lid, "generation": lg})),
                    &c,
                    d.as_ref(),
                    2000,
                )
            })
        };
        thread::sleep(Duration::from_millis(15));
        release3.send(()).unwrap();
        let paused2 = h_pause.join().unwrap().unwrap();
        assert_eq!(paused2.state, LeaseState::Paused);
        let seg2_paused = h_seg2.join().unwrap().unwrap();
        assert_eq!(seg2_paused.state, SegmentState::Paused);
        let snap2 = paused2.pause_snapshot.unwrap();

        let plan_hash2 = "2".repeat(64);
        leases
            .register_confirmation(
                "grant-cum-res",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq2.lease_id.clone(),
                    pause_id: snap2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq2.generation,
                    checkpoint_hash: snap2.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash2.clone(),
                    restore_target_token: snap2.input_target_token.clone(),
                    pointer_to_restore: Some(snap2.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        // On resume: submit #1 is restore pointer move, submit #2 is event 3 ("d").
        // Gate submit #2 so slice_clock_start_us is already anchored!
        let (entered4, release4) = scripted.gate_submit_on(2);
        let h_res2 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            let pid = snap2.pause_id.clone();
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "pauseId": pid,
                        "resumeAttemptId": "att-cum",
                        "resumePlanHash": plan_hash2,
                        "confirmationGrant": "grant-cum-res"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered4.recv().unwrap();
        // Event 3 waited remaining 10ms on wall clock; advancing 39ms puts fake slice_elapsed at 39ms,
        // so event 4 (deadline 20ms) sees 19ms (<20ms single) + 33ms frozen shift = 52ms > 50ms -> INPUT_TIMING_OVERRUN!
        scripted.inner.fake_clock().advance_us(39_000);
        release4.send(()).unwrap();
        let resumed2 = h_res2.join().unwrap().unwrap();
        assert_eq!(resumed2.state, LeaseState::Paused);
        assert_eq!(resumed2.reason.as_deref(), Some("INPUT_TIMING_OVERRUN"));
        let final_seg2 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-cum", "payloadHash": h_long_q})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(final_seg2.state, SegmentState::Paused);
    }

    #[test]
    fn admission_target_loss_releases_existing_held_inputs_and_leaves_active() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, TargetStatus, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-adm", "ws-adm");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-adm", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let kd = json!({
            "id": "adm-kd",
            "title": "按住按键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-adm-1",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Target loses focus BEFORE release segment arrives.
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let ku = json!({
            "id": "adm-ku",
            "title": "释放按键",
            "action": "keyboard.up",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_ku = compute_segment_payload_hash("target-a", &ku, None).unwrap();
        let err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-adm-2",
                    "payloadHash": h_ku,
                    "targetToken": "target-a",
                    "operation": ku
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_ne!(st.state, LeaseState::Active);
    }

    #[test]
    fn pointer_only_resume_plan_focuses_and_restores_pointer_and_handles_rejected_vs_uncertain() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, PausePoint, PhysicalPoint,
            compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-po", "ws-po");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-po", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // Move pointer to (350, 260) with no held inputs and pause cleanly (heldInputs = [], cursor = None).
        let move_op = json!({
            "id": "po-move",
            "title": "定位指针",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 350, "y": 260},
                "durationMs": 0
            },
            "on": {}
        });
        let h_m = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-po-1",
                    "payloadHash": h_m,
                    "targetToken": "target-a",
                    "operation": move_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap = paused.pause_snapshot.unwrap();
        assert!(snap.held_inputs.is_empty());
        assert!(snap.cursor.is_none());
        let expected_ptr = PausePoint::Physical(PhysicalPoint { x: 350, y: 260 });
        assert_eq!(snap.pointer, expected_ptr);

        let plan_hash = "3".repeat(64);
        leases
            .register_confirmation(
                "grant-po-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: Some(expected_ptr),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        // Finding [12] Subcase A: Explicit Rejected on pointer restore keeps Paused and preserves grant!
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Rejected));
        let rej_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-po-rej",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-po-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(rej_err.code, "RESOURCE_DENIED");
        let st_after_rej = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_after_rej.state, LeaseState::Paused);

        // Retry with SAME grant now succeeds AND submits PointerMovePhysical(350, 260) (Finding [11]).
        let before_res = scripted.inner.submitted_actions().len();
        let res_ok = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-po-ok",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-po-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res_ok.state, LeaseState::Active);
        assert!(
            scripted.inner.submitted_actions()[before_res..]
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::PointerMovePhysical(p) if *p == PhysicalPoint { x: 350, y: 260 }))
        );

        // Finding [12] Subcase B: Two held keys; second key Rejected with cleanup Succeeded keeps Paused + grant intact.
        for (sid, key_name) in [("seg-k1", "ControlLeft"), ("seg-k2", "ShiftLeft")] {
            let op = json!({
                "id": sid,
                "title": "按住多键",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key_name},
                "on": {}
            });
            let h = compute_segment_payload_hash("target-a", &op, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq.lease_id,
                        "generation": acq.generation,
                        "segmentId": sid,
                        "payloadHash": h,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        let paused2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap2 = paused2.pause_snapshot.unwrap();
        let plan_hash2 = "4".repeat(64);
        leases
            .register_confirmation(
                "grant-po-2",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap2.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash2.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: Some(snap2.pointer.clone()),
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted)); // pointer
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted)); // ControlLeft
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Rejected)); // ShiftLeft rejected
        scripted
            .inner
            .set_cleanup_outcome(CleanupOutcome::Succeeded);
        let rej2 = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap2.pause_id,
                    "resumeAttemptId": "att-k-rej",
                    "resumePlanHash": plan_hash2,
                    "confirmationGrant": "grant-po-2"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(rej2.code, "RESOURCE_DENIED");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
        let st_still_paused = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_still_paused.state, LeaseState::Paused);
        assert!(st_still_paused.pause_snapshot.is_some());

        // Finding [12] Subcase C: Retry same grant cleanly succeeds now that native side stopped rejecting.
        let ok2 = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap2.pause_id,
                    "resumeAttemptId": "att-k-ok",
                    "resumePlanHash": plan_hash2,
                    "confirmationGrant": "grant-po-2"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(ok2.state, LeaseState::Active);
    }

    #[test]
    fn expired_in_flight_lease_cannot_resurrect_and_emergency_beats_pause() {
        use deskweave_platform_api::{
            EmergencyState, HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-exp-guard", "ws-exp-guard");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Part A: Late lease.renew / lease.pause must NOT resurrect an in-flight lease past TTL.
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-exp-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let txt_op = json!({
            "id": "exp-txt",
            "title": "到期守护段",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "ab", "intervalMs": 50},
            "on": {}
        });
        let h_txt = compute_segment_payload_hash("target-a", &txt_op, None).unwrap();
        let (entered1, release1) = scripted.gate_submit_on(1);
        let h_seg = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            let hash_c = h_txt.clone();
            let op_c = txt_op.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-exp-guard",
                        "payloadHash": hash_c,
                        "targetToken": "target-a",
                        "operation": op_c
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered1.recv().unwrap();
        scripted.inner.fake_clock().advance_us(2_500_000);

        let renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_err.code, "LEASE_EXPIRED");

        let pause_err = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                200,
            )
            .unwrap_err();
        assert_eq!(pause_err.code, "LEASE_EXPIRED");

        release1.send(()).unwrap();
        let seg_res = h_seg.join().unwrap().unwrap();
        assert_eq!(seg_res.state, SegmentState::Uncertain);
        let st_exp = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_exp.state, LeaseState::Released);
        assert_eq!(st_exp.reason.as_deref(), Some("LEASE_EXPIRED"));

        // Part B: Emergency lock at segment.submit admission releases existing held inputs cleanly.
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-exp-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd = json!({
            "id": "emg-kd",
            "title": "急停清理按住键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-emg-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let emg_gen = scripted.emergency().trigger_lock().unwrap();
        let ad_err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-emg-adm",
                    "payloadHash": h_txt,
                    "targetToken": "target-a",
                    "operation": txt_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(ad_err.code, "RESOURCE_DENIED");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st_adm = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq2.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_adm.state, LeaseState::Released);

        // Part C: Emergency lock beats pending pause_req mid-segment.
        scripted.emergency().rearm(emg_gen).unwrap();
        assert_eq!(scripted.emergency().state(), EmergencyState::Armed);
        let acq3 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-exp-3", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let (entered3, release3) = scripted.gate_submit_on(1);
        let h_seg3 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq3.lease_id.clone();
            let lg = acq3.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-emg-mid",
                        "payloadHash": h_txt,
                        "targetToken": "target-a",
                        "operation": txt_op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered3.recv().unwrap();
        let h_pause3 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq3.lease_id.clone();
            let lg = acq3.generation;
            thread::spawn(move || {
                l.handle_lease_pause(
                    &json_obj(json!({"leaseId": lid, "generation": lg})),
                    &c,
                    d.as_ref(),
                    2000,
                )
            })
        };
        thread::sleep(Duration::from_millis(15));
        let _ = scripted.emergency().trigger_lock().unwrap();
        release3.send(()).unwrap();
        let _ = h_seg3.join().unwrap();
        let _ = h_pause3.join().unwrap();
        let st_emg3 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq3.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_ne!(st_emg3.state, LeaseState::Paused);
    }

    #[test]
    fn session_writer_poisoning_prevents_frame_interleave_and_stops_session() {
        struct PartialFailingWriter {
            bytes: Arc<Mutex<Vec<u8>>>,
            calls: usize,
        }

        impl SessionWrite for PartialFailingWriter {
            fn write_chunk(
                &mut self,
                buf: &[u8],
                _deadline: Option<Instant>,
                _stop: &StopSignal,
            ) -> io::Result<usize> {
                self.calls += 1;
                if self.calls == 1 {
                    let prefix_only = buf.len().min(4);
                    self.bytes
                        .lock()
                        .unwrap()
                        .extend_from_slice(&buf[..prefix_only]);
                    return Ok(prefix_only);
                }
                Err(io::ErrorKind::TimedOut.into())
            }
        }

        let stop = StopSignal::new().unwrap();
        let session_stop = stop.child().unwrap();
        let written = Arc::new(Mutex::new(Vec::new()));
        let writer = SessionWriter::new(
            PartialFailingWriter {
                bytes: Arc::clone(&written),
                calls: 0,
            },
            session_stop.clone(),
        );

        let first_err = writer
            .write_framed(b"{\"hello\":1}", Duration::from_secs(1))
            .unwrap_err();
        assert_eq!(first_err.kind(), io::ErrorKind::TimedOut);
        assert!(session_stop.is_triggered());
        assert_eq!(written.lock().unwrap().len(), 4);

        let second_err = writer
            .write_framed(b"{\"second\":2}", Duration::from_secs(1))
            .unwrap_err();
        assert_eq!(second_err.kind(), io::ErrorKind::BrokenPipe);
        assert_eq!(written.lock().unwrap().len(), 4);
    }

    #[test]
    fn host_null_pointer_to_restore_skips_pointer_move_and_direct_resume_refreshes_pointer() {
        use deskweave_platform_api::{
            LeaseState, PausePoint, PhysicalPoint, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted
            .inner
            .set_pause_pointer(PausePoint::Physical(PhysicalPoint { x: 100, y: 100 }));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-null-ptr", "ws-null-ptr");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-np-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // Hold ControlLeft and pause at (100, 100).
        let kd = json!({
            "id": "np-kd",
            "title": "按住控制键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-np-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let p1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap1 = p1.pause_snapshot.unwrap();
        let plan_hash1 = "9".repeat(64);
        leases
            .register_confirmation(
                "grant-null-ptr",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap1.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap1.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash1.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let actions_before = scripted.inner.submitted_actions().len();
        let res1 = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap1.pause_id,
                    "resumeAttemptId": "att-np-1",
                    "resumePlanHash": plan_hash1,
                    "confirmationGrant": "grant-null-ptr"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res1.state, LeaseState::Active);
        let restored_actions: Vec<NativeInputAction> = scripted.inner.submitted_actions()
            [actions_before..]
            .iter()
            .map(|(_, a)| a.clone())
            .collect();
        assert_eq!(
            restored_actions,
            vec![NativeInputAction::KeyDown("ControlLeft".to_owned())]
        );

        // Release ControlLeft, move live pointer to (450, 320), pause with empty heldInputs/cursor,
        // move external pointer to (600, 400), direct resume, and verify pointer ledger refreshes.
        let ku = json!({
            "id": "np-ku",
            "title": "释放控制键",
            "action": "keyboard.up",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_ku = compute_segment_payload_hash("target-a", &ku, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-np-ku",
                    "payloadHash": h_ku,
                    "targetToken": "target-a",
                    "operation": ku
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let move_op = json!({
            "id": "np-mv",
            "title": "移动指针",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 450, "y": 320},
                "durationMs": 0
            },
            "on": {}
        });
        let h_mv = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-np-mv",
                    "payloadHash": h_mv,
                    "targetToken": "target-a",
                    "operation": move_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let p2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap2 = p2.pause_snapshot.unwrap();
        assert_eq!(
            snap2.pointer,
            PausePoint::Physical(PhysicalPoint { x: 450, y: 320 })
        );

        scripted
            .inner
            .set_pause_pointer(PausePoint::Physical(PhysicalPoint { x: 600, y: 400 }));
        let plan_hash2 = "8".repeat(64);
        let res2 = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap2.pause_id,
                    "resumeAttemptId": "att-np-2",
                    "resumePlanHash": plan_hash2
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res2.state, LeaseState::Active);

        let p3 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(
            p3.pause_snapshot.unwrap().pointer,
            PausePoint::Physical(PhysicalPoint { x: 600, y: 400 })
        );
    }

    #[test]
    fn concurrent_segment_submit_during_emergency_lock_preserves_in_flight_receipt_and_cleanup() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, SegmentState, SubmissionOutcome, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-emg-inflight", "ws-emg-inflight");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-in", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let kd_shift = json!({
            "id": "emg-in-s",
            "title": "按住Shift",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_shift = compute_segment_payload_hash("target-a", &kd_shift, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-emg1-kd",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let kd_ctrl = json!({
            "id": "emg-in-c",
            "title": "按住Control",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_ctrl = compute_segment_payload_hash("target-a", &kd_ctrl, None).unwrap();
        scripted.push_submit_outcome(Ok(SubmissionOutcome::Submitted));
        let (entered, release) = scripted.gate_submit_on(1);
        let h_seg1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            let hc = h_ctrl.clone();
            let oc = kd_ctrl.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-emg1-ctrl",
                        "payloadHash": hc,
                        "targetToken": "target-a",
                        "operation": oc
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered.recv().unwrap();
        scripted.emergency().trigger_lock().unwrap();

        let ku_shift = json!({
            "id": "emg-in-u",
            "title": "并发拒绝段",
            "action": "keyboard.up",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_up = compute_segment_payload_hash("target-a", &ku_shift, None).unwrap();
        let err = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-emg1-adm",
                    "payloadHash": h_up,
                    "targetToken": "target-a",
                    "operation": ku_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err.code, "RESOURCE_DENIED");
        assert!(scripted.inner.released_batches().is_empty());

        release.send(()).unwrap();
        let seg1_res = h_seg1.join().unwrap().unwrap();
        assert_eq!(seg1_res.state, SegmentState::Sent);
        let seg1_status = leases
            .handle_segment_status(
                &json_obj(json!({
                    "segmentId": "seg-emg1-ctrl",
                    "payloadHash": h_ctrl
                })),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(seg1_status.state, SegmentState::Sent);

        let released = scripted.inner.released_batches();
        assert_eq!(released.len(), 1);
        assert_eq!(released[0].len(), 2);
        assert!(released[0].contains(&HeldInput::Key {
            key: "ShiftLeft".to_owned()
        }));
        assert!(released[0].contains(&HeldInput::Key {
            key: "ControlLeft".to_owned()
        }));
        let lease_st = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(lease_st.state, LeaseState::Released);
        assert_eq!(lease_st.reason.as_deref(), Some("EMERGENCY_LOCKED"));
    }

    #[test]
    fn emergency_lock_before_first_event_revokes_held_lease_without_pausing() {
        use deskweave_platform_api::{
            EmergencyState, HeldInput, LeaseState, PausePoint, PhysicalPoint, PlatformError,
            SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted
            .inner
            .set_pause_pointer(PausePoint::Physical(PhysicalPoint { x: 100, y: 100 }));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-emg-ev0", "ws-emg-ev0");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-ev0", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let kd_shift = json!({
            "id": "emg0-kd",
            "title": "按住Shift",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd_shift, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-emg0-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let move_op = json!({
            "id": "emg0-mv",
            "title": "首事件前急停",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 101, "y": 100},
                "durationMs": 2_000_000
            },
            "on": {}
        });
        let h_mv = compute_segment_payload_hash("target-a", &move_op, None).unwrap();
        let h_seg = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            let hm = h_mv.clone();
            let om = move_op.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-emg0-mv",
                        "payloadHash": hm,
                        "targetToken": "target-a",
                        "operation": om
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(st) = leases.handle_segment_status(
                &json_obj(json!({"segmentId": "seg-emg0-mv", "payloadHash": h_mv})),
                &ctx,
                scripted.as_ref(),
            ) && st.state == SegmentState::Running
            {
                break;
            }
            assert!(Instant::now() < deadline, "timed out waiting for Running");
            thread::sleep(Duration::from_millis(5));
        }

        let emg_gen = scripted.emergency().trigger_lock().unwrap();
        let mv_err = h_seg.join().unwrap().unwrap_err();
        assert_eq!(mv_err.code, "RESOURCE_DENIED");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st1 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st1.state, LeaseState::Released);
        assert_eq!(st1.reason.as_deref(), Some("EMERGENCY_LOCKED"));

        // Sub-part 2: Pre-dispatch TargetLost during event 0 under EmergencyState::Locked must revoke, not Pause.
        scripted.emergency().rearm(emg_gen).unwrap();
        assert_eq!(scripted.emergency().state(), EmergencyState::Armed);
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-pre", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq2.lease_id,
                    "generation": acq2.generation,
                    "segmentId": "seg-emg-pre-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted.push_submit_outcome(Err(PlatformError::target_lost("lost")));
        let (entered2, release2) = scripted.gate_submit_on(1);
        let kd_a = json!({
            "id": "emg-pre-a",
            "title": "失焦撞急停",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "KeyA"},
            "on": {}
        });
        let h_a = compute_segment_payload_hash("target-a", &kd_a, None).unwrap();
        let h_pre = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-emg-preloss",
                        "payloadHash": h_a,
                        "targetToken": "target-a",
                        "operation": kd_a
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered2.recv().unwrap();
        let _ = scripted.emergency().trigger_lock().unwrap();
        release2.send(()).unwrap();

        let pre_err = h_pre.join().unwrap().unwrap_err();
        assert_eq!(pre_err.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st2 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq2.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st2.state, LeaseState::Released);
        assert_eq!(st2.reason.as_deref(), Some("EMERGENCY_LOCKED"));
    }

    #[test]
    fn resume_restoration_enforces_expiry_and_allows_concurrent_renew() {
        use deskweave_platform_api::{HeldInput, LeaseState, compute_segment_payload_hash};

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-res-exp", "ws-res-exp");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: Expiry mid-restoration stops before next key, cleans up restored key, revokes lease.
        let acq1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-res-exp", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        for (seg_id, key) in [("seg-re-1", "ControlLeft"), ("seg-re-2", "ShiftLeft")] {
            let op = json!({
                "id": seg_id,
                "title": "恢复期限测试按住键",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            });
            let ph = compute_segment_payload_hash("target-a", &op, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq1.lease_id,
                        "generation": acq1.generation,
                        "segmentId": seg_id,
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        let paused1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq1.lease_id, "generation": acq1.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap1 = paused1.pause_snapshot.unwrap();
        let plan_h1 = "c".repeat(64);
        leases
            .register_confirmation(
                "grant-res-exp-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-res-exp".to_owned(),
                    lease_id: acq1.lease_id.clone(),
                    pause_id: snap1.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq1.generation,
                    checkpoint_hash: snap1.checkpoint_hash,
                    resume_plan_hash: plan_h1.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        let (entered1, release1) = scripted.gate_submit_on(1);
        let h_res1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq1.lease_id.clone();
            let lg = acq1.generation;
            let pid = snap1.pause_id;
            let ph = plan_h1;
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "pauseId": pid,
                        "resumeAttemptId": "att-res-exp-1",
                        "resumePlanHash": ph,
                        "confirmationGrant": "grant-res-exp-1"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered1.recv().unwrap();
        let before_count = scripted.inner.submitted_actions().len();
        scripted
            .inner
            .fake_clock()
            .advance_us(crate::lease::LEASE_TTL_US);
        release1.send(()).unwrap();
        let exp_err = h_res1.join().unwrap().unwrap_err();
        assert_eq!(exp_err.code, "LEASE_EXPIRED");
        let after_actions: Vec<NativeInputAction> = scripted
            .inner
            .submitted_actions()
            .into_iter()
            .skip(before_count)
            .map(|(_, a)| a)
            .collect();
        assert_eq!(
            after_actions,
            vec![NativeInputAction::KeyDown("ControlLeft".to_owned())]
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
        let st_exp = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq1.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_exp.state, LeaseState::Released);
        assert_eq!(st_exp.reason.as_deref(), Some("LEASE_EXPIRED"));

        // Subcase B: Concurrent lease.renew succeeds while restoration mutex is released, extending past initial TTL.
        let acq2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-res-renew", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        for (seg_id, key) in [("seg-rn-1", "ControlLeft"), ("seg-rn-2", "ShiftLeft")] {
            let op = json!({
                "id": seg_id,
                "title": "并发续期按住键",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            });
            let ph = compute_segment_payload_hash("target-a", &op, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq2.lease_id,
                        "generation": acq2.generation,
                        "segmentId": seg_id,
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        let paused2 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq2.lease_id, "generation": acq2.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap2 = paused2.pause_snapshot.unwrap();
        let plan_h2 = "d".repeat(64);
        leases
            .register_confirmation(
                "grant-res-renew-2",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-res-exp".to_owned(),
                    lease_id: acq2.lease_id.clone(),
                    pause_id: snap2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq2.generation,
                    checkpoint_hash: snap2.checkpoint_hash,
                    resume_plan_hash: plan_h2.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        let (entered2, release2) = scripted.gate_submit_on(1);
        let h_res2 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq2.lease_id.clone();
            let lg = acq2.generation;
            let pid = snap2.pause_id;
            let ph = plan_h2;
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "pauseId": pid,
                        "resumeAttemptId": "att-res-renew-2",
                        "resumePlanHash": ph,
                        "confirmationGrant": "grant-res-renew-2"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered2.recv().unwrap();
        scripted.inner.fake_clock().advance_us(1_500_000);
        let renewed = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq2.lease_id, "generation": acq2.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(renewed.lease_id, acq2.lease_id);
        scripted.inner.fake_clock().advance_us(1_000_000);
        release2.send(()).unwrap();
        let resumed2 = h_res2.join().unwrap().unwrap();
        assert_eq!(resumed2.state, LeaseState::Active);
    }

    #[test]
    fn cursor_only_pause_resumes_directly_without_confirmation_grant() {
        use deskweave_platform_api::{LeaseState, SegmentState, compute_segment_payload_hash};

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-cur", "ws-cur");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-cur", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let txt = json!({
            "id": "cur-txt",
            "title": "纯游标无确认继续",
            "action": "keyboard.text",
            "params": {"target": "main", "text": "xy", "intervalMs": 150},
            "on": {}
        });
        let h_txt = compute_segment_payload_hash("target-a", &txt, None).unwrap();
        let (sub_tx, sub_rx) = mpsc::channel();
        scripted.notify_submit_after(1, sub_tx);
        let h_seg = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-cur-txt",
                        "payloadHash": h_txt,
                        "targetToken": "target-a",
                        "operation": txt
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        sub_rx.recv().unwrap();
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(h_seg.join().unwrap().unwrap().state, SegmentState::Paused);
        let snap = paused.pause_snapshot.unwrap();
        assert!(snap.cursor.is_some());
        assert!(snap.held_inputs.is_empty());

        // Without confirmationGrant: unfocused target fails with TARGET_LOST (no focus hijack).
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let unfocused_err = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-cur-unfocus",
                    "resumePlanHash": "e".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(unfocused_err.code, "TARGET_LOST");

        // Once active again, direct resume without confirmationGrant completes remaining text.
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Active);
        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-cur-ok",
                    "resumePlanHash": "e".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(resumed.state, LeaseState::Active);
        assert!(
            scripted
                .inner
                .submitted_actions()
                .iter()
                .any(|(_, a)| matches!(a, NativeInputAction::UnicodeText(s) if s == "y"))
        );
    }

    #[test]
    fn pointer_delta_observes_live_pointer_position_after_submission() {
        use deskweave_platform_api::{
            DisplayPoint, LeaseState, PausePoint, SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-delta", "ws-delta");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        let observed_pt = PausePoint::Display(DisplayPoint {
            display_id: "secondary".to_owned(),
            x: 12.5,
            y: 34.5,
        });
        scripted.set_pointer_override(Some(Ok(PausePoint::Display(DisplayPoint {
            display_id: "main".to_owned(),
            x: 1900.0,
            y: 50.0,
        }))));
        scripted.set_pointer_after_submit(Some(Ok(observed_pt.clone())));

        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-delta", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let delta_op = json!({
            "id": "delta-cross",
            "title": "跨屏相对位移观察",
            "action": "pointer.delta",
            "params": {"target": "main", "dx": 100, "dy": 0, "durationMs": 0},
            "on": {}
        });
        let h_delta = compute_segment_payload_hash("target-a", &delta_op, None).unwrap();
        let seg_res = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-delta-1",
                    "payloadHash": h_delta,
                    "targetToken": "target-a",
                    "operation": delta_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(seg_res.state, SegmentState::Sent);

        // Pause checkpoint must contain observed secondary-display position, never out-of-bounds main(2000, 50).
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused.pause_snapshot.as_ref().unwrap().pointer, observed_pt);

        // Resuming then running PointerDelta where live pointer query fails must clear stale ledger pointer.
        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": paused.pause_snapshot.as_ref().unwrap().pause_id,
                    "resumeAttemptId": "att-delta-clear",
                    "resumePlanHash": "f".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let delta_err_op = json!({
            "id": "delta-probe-fail",
            "title": "指针查询失败清除旧坐标",
            "action": "pointer.delta",
            "params": {"target": "main", "dx": 5, "dy": 5, "durationMs": 0},
            "on": {}
        });
        let h_delta_err = compute_segment_payload_hash("target-a", &delta_err_op, None).unwrap();
        scripted.set_pointer_after_submit(Some(Err(PlatformError::session_unavailable(
            "pointer unavailable",
        ))));
        let seg_res2 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-delta-2",
                    "payloadHash": h_delta_err,
                    "targetToken": "target-a",
                    "operation": delta_err_op
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(seg_res2.state, SegmentState::Sent);
        assert_eq!(resumed.state, LeaseState::Active);
        let pause_fail = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(pause_fail.code, "SESSION_UNAVAILABLE");
    }

    #[test]
    fn keyboard_chord_pause_and_resume_preserves_original_press_order() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, SegmentState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-chord", "ws-chord");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-chord-order", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // ControlLeft -> ShiftLeft -> KeyS sorts canonically as [ControlLeft, KeyS, ShiftLeft]
        // when hashing, yet restoration MUST press ControlLeft -> ShiftLeft -> KeyS.
        let chord = json!({
            "id": "chord-order",
            "title": "组合键恢复顺序",
            "action": "keyboard.chord",
            "params": {
                "target": "main",
                "keys": ["ControlLeft", "ShiftLeft", "KeyS"],
                "pressDurationMs": 250
            },
            "on": {}
        });
        let h_chord = compute_segment_payload_hash("target-a", &chord, None).unwrap();
        let (sub_tx, sub_rx) = mpsc::channel();
        scripted.notify_submit_after(3, sub_tx);
        let h_seg = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "segmentId": "seg-chord-order",
                        "payloadHash": h_chord,
                        "targetToken": "target-a",
                        "operation": chord
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        sub_rx.recv().unwrap();
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(h_seg.join().unwrap().unwrap().state, SegmentState::Paused);
        let snap = paused.pause_snapshot.unwrap();
        assert_eq!(
            snap.held_inputs,
            vec![
                HeldInput::Key {
                    key: "ControlLeft".to_owned()
                },
                HeldInput::Key {
                    key: "KeyS".to_owned()
                },
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
                },
            ]
        );

        let before_resume = scripted.inner.submitted_actions().len();
        let plan_hash = "1".repeat(64);
        leases
            .register_confirmation(
                "grant-chord-1",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-chord".to_owned(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash,
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();
        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-chord-1",
                    "resumePlanHash": plan_hash,
                    "confirmationGrant": "grant-chord-1"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(resumed.state, LeaseState::Active);
        let resumed_actions: Vec<NativeInputAction> = scripted
            .inner
            .submitted_actions()
            .into_iter()
            .skip(before_resume)
            .map(|(_, a)| a)
            .collect();
        assert_eq!(
            resumed_actions,
            vec![
                NativeInputAction::KeyDown("ControlLeft".to_owned()),
                NativeInputAction::KeyDown("ShiftLeft".to_owned()),
                NativeInputAction::KeyDown("KeyS".to_owned()),
                NativeInputAction::KeyUp("KeyS".to_owned()),
                NativeInputAction::KeyUp("ShiftLeft".to_owned()),
                NativeInputAction::KeyUp("ControlLeft".to_owned()),
            ]
        );

        // Subcase B: Explicitly held keys in non-lexical order retain press order across double pause/resume.
        for (seg_id, key) in [
            ("seg-ord-1", "KeyZ"),
            ("seg-ord-2", "ControlLeft"),
            ("seg-ord-3", "ShiftLeft"),
        ] {
            let op = json!({
                "id": seg_id,
                "title": "显式多键顺序保持",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            });
            let ph = compute_segment_payload_hash("target-a", &op, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq.lease_id,
                        "generation": acq.generation,
                        "segmentId": seg_id,
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        for (attempt, grant_id) in [("att-ord-1", "grant-ord-1"), ("att-ord-2", "grant-ord-2")] {
            let p_snap = leases
                .handle_lease_pause(
                    &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap()
                .pause_snapshot
                .unwrap();
            assert_eq!(
                p_snap.held_inputs,
                vec![
                    HeldInput::Key {
                        key: "ControlLeft".to_owned()
                    },
                    HeldInput::Key {
                        key: "KeyZ".to_owned()
                    },
                    HeldInput::Key {
                        key: "ShiftLeft".to_owned()
                    },
                ]
            );
            let before = scripted.inner.submitted_actions().len();
            let ph = "2".repeat(64);
            leases
                .register_confirmation(
                    grant_id,
                    crate::lease::RegisteredConfirmation {
                        owner_workspace: "ws-chord".to_owned(),
                        lease_id: acq.lease_id.clone(),
                        pause_id: p_snap.pause_id.clone(),
                        agent_epoch: ctx.agent_epoch.clone(),
                        generation: acq.generation,
                        checkpoint_hash: p_snap.checkpoint_hash,
                        resume_plan_hash: ph.clone(),
                        restore_target_token: Some("target-a".to_owned()),
                        pointer_to_restore: None,
                        expires_at_us: u64::MAX,
                    },
                )
                .unwrap();
            let res = leases
                .handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": acq.lease_id,
                        "generation": acq.generation,
                        "pauseId": p_snap.pause_id,
                        "resumeAttemptId": attempt,
                        "resumePlanHash": ph,
                        "confirmationGrant": grant_id
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
            assert_eq!(res.state, LeaseState::Active);
            let pressed: Vec<NativeInputAction> = scripted
                .inner
                .submitted_actions()
                .into_iter()
                .skip(before)
                .map(|(_, a)| a)
                .collect();
            assert_eq!(
                pressed,
                vec![
                    NativeInputAction::KeyDown("KeyZ".to_owned()),
                    NativeInputAction::KeyDown("ControlLeft".to_owned()),
                    NativeInputAction::KeyDown("ShiftLeft".to_owned()),
                ]
            );
        }
    }

    #[test]
    fn resume_restoration_panic_cleans_up_restored_keys_and_unblocks_close() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-res-panic", "ws-res-panic");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-res-panic", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        for (seg_id, key) in [("seg-rp-1", "ControlLeft"), ("seg-rp-2", "ShiftLeft")] {
            let op = json!({
                "id": seg_id,
                "title": "恢复异常清理测试键",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            });
            let ph = compute_segment_payload_hash("target-a", &op, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq.lease_id,
                        "generation": acq.generation,
                        "segmentId": seg_id,
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        let snap = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap()
            .pause_snapshot
            .unwrap();

        let plan_hash = "3".repeat(64);
        leases
            .register_confirmation(
                "grant-res-panic",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: "ws-res-panic".to_owned(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash,
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        // First restored key (ControlLeft) submits, second restored key (ShiftLeft) panics while state mutex is dropped.
        scripted.panic_on_submit_number(2);
        let h_resume = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let lg = acq.generation;
            let pid = snap.pause_id;
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lg,
                        "pauseId": pid,
                        "resumeAttemptId": "att-res-panic",
                        "resumePlanHash": plan_hash,
                        "confirmationGrant": "grant-res-panic"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        assert!(h_resume.join().is_err());

        // Coordinator state must remain unpoisoned, restored + in-flight keys released, Uncertain entered, and close unblocked.
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![
                HeldInput::Key {
                    key: "ControlLeft".to_owned()
                },
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
                },
            ]
        );
        let st = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st.state, LeaseState::Uncertain);
        assert_eq!(st.cleanup, CleanupOutcome::Succeeded);

        let (closed_tx, closed_rx) = mpsc::channel();
        {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let sid = ctx.session_id.clone();
            thread::spawn(move || {
                l.on_session_closed(&sid, d.as_ref());
                let _ = closed_tx.send(());
            });
        }
        closed_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    }

    #[test]
    fn resume_restoration_revalidates_target_state_between_events() {
        use deskweave_platform_api::{
            HeldInput, LeaseState, TargetStatus, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-reval", "ws-reval");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-reval", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        for (seg_id, key) in [("seg-d1", "ShiftLeft"), ("seg-d2", "ControlLeft")] {
            let down = json!({
                "id": seg_id,
                "title": "持有输入",
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            });
            let h = compute_segment_payload_hash("target-a", &down, None).unwrap();
            leases
                .handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": acq.lease_id,
                        "generation": acq.generation,
                        "segmentId": seg_id,
                        "payloadHash": h,
                        "targetToken": "target-a",
                        "operation": down
                    })),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap();
        }
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap = paused.pause_snapshot.unwrap();
        let plan_hash = "4".repeat(64);
        leases
            .register_confirmation(
                "grant-reval-res",
                crate::lease::RegisteredConfirmation {
                    owner_workspace: ctx.workspace_id.clone(),
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
                    pointer_to_restore: None,
                    expires_at_us: u64::MAX,
                },
            )
            .unwrap();

        // 1. Target version drift between 1st and 2nd restored key aborts restoration,
        //    cleans up 1st key, never submits 2nd key, and keeps lease Paused.
        let (entered_1, release_1) = scripted.gate_submit_on(1);
        let h_1 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let pid = snap.pause_id.clone();
            let ph = plan_hash.clone();
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": acq.generation,
                        "pauseId": pid,
                        "resumeAttemptId": "att-reval-ver",
                        "resumePlanHash": ph,
                        "confirmationGrant": "grant-reval-res"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_1.recv_timeout(Duration::from_secs(2)).unwrap();
        let actions_before_release_1 = scripted.inner.submitted_actions().len();
        scripted.set_target_version_override(Some(Ok(Some("v-drifted".to_owned()))));
        release_1.send(()).unwrap();
        let err_1 = h_1.join().unwrap().unwrap_err();
        assert_eq!(err_1.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.submitted_actions().len(),
            actions_before_release_1 + 1,
            "second key must not be submitted after target version drift"
        );
        assert_eq!(
            scripted.inner.submitted_actions().last().unwrap().1,
            NativeInputAction::KeyDown("ShiftLeft".to_owned())
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq.lease_id})),
                    &ctx,
                    scripted.as_ref(),
                )
                .unwrap()
                .state,
            LeaseState::Paused
        );

        // 2. Focus loss (TargetStatus::Unfocused) between 1st and 2nd key aborts cleanly.
        scripted.set_target_version_override(None);
        let (entered_2, release_2) = scripted.gate_submit_on(1);
        let h_2 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let pid = snap.pause_id.clone();
            let ph = plan_hash.clone();
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": acq.generation,
                        "pauseId": pid,
                        "resumeAttemptId": "att-reval-focus",
                        "resumePlanHash": ph,
                        "confirmationGrant": "grant-reval-res"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_2.recv_timeout(Duration::from_secs(2)).unwrap();
        let actions_before_release_2 = scripted.inner.submitted_actions().len();
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        release_2.send(()).unwrap();
        let err_2 = h_2.join().unwrap().unwrap_err();
        assert_eq!(err_2.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.submitted_actions().len(),
            actions_before_release_2 + 1
        );
        assert_eq!(
            scripted.inner.submitted_actions().last().unwrap().1,
            NativeInputAction::KeyDown("ShiftLeft".to_owned())
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );

        // 3. Target version drift during the final restored key still rejects transition to Active
        //    and releases both restored keys before returning TARGET_LOST.
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Active);
        let (entered_3, release_3) = scripted.gate_submit_on(2);
        let h_3 = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx.clone();
            let lid = acq.lease_id.clone();
            let pid = snap.pause_id;
            thread::spawn(move || {
                l.handle_lease_resume(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": acq.generation,
                        "pauseId": pid,
                        "resumeAttemptId": "att-reval-final",
                        "resumePlanHash": plan_hash,
                        "confirmationGrant": "grant-reval-res"
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_3.recv_timeout(Duration::from_secs(2)).unwrap();
        scripted.set_target_version_override(Some(Ok(Some("v-drifted-final".to_owned()))));
        release_3.send(()).unwrap();
        let err_3 = h_3.join().unwrap().unwrap_err();
        assert_eq!(err_3.code, "TARGET_LOST");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
                },
                HeldInput::Key {
                    key: "ControlLeft".to_owned()
                },
            ]
        );
        assert_eq!(
            leases
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq.lease_id})),
                    &ctx,
                    scripted.as_ref(),
                )
                .unwrap()
                .state,
            LeaseState::Paused
        );
    }

    #[test]
    fn between_segment_pause_checkpoint_failure_releases_held_inputs_and_marks_uncertain() {
        use deskweave_platform_api::{
            CleanupOutcome, HeldInput, LeaseState, PointerButton, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-pause-fail", "ws-pause-fail");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Case 1: keyboard.down succeeds -> topology query fails on between-segment lease.pause.
        let acq_1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-pf-1", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd = json!({
            "id": "pf-kd",
            "title": "按下Shift",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_1.lease_id,
                    "generation": acq_1.generation,
                    "segmentId": "seg-pf-1",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted
            .inner
            .set_topology(Err(PlatformError::session_unavailable("topo-down")));
        let snap_1 = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_1.lease_id, "generation": acq_1.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(snap_1.state, LeaseState::Uncertain);
        assert_eq!(snap_1.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(snap_1.reason.as_deref(), Some("EFFECT_UNCERTAIN"));
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let renew_err_1 = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_1.lease_id, "generation": acq_1.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_err_1.code, "LEASE_EXPIRED");

        // Case 2: pointer.button down succeeds -> target_version fails on between-segment lease.pause.
        scripted.inner.set_topology(Ok(base_topo));
        scripted
            .inner
            .register_grant("grant-b", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases_2 = LeaseCoordinator::new();
        leases_2
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        let acq_2 = leases_2
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-pf-2", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let pb = json!({
            "id": "pf-pb",
            "title": "按下鼠标左键",
            "action": "pointer.button",
            "params": {"target": "main", "button": "left", "state": "down"},
            "on": {}
        });
        let h_pb = compute_segment_payload_hash("target-a", &pb, None).unwrap();
        leases_2
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_2.lease_id,
                    "generation": acq_2.generation,
                    "segmentId": "seg-pf-2",
                    "payloadHash": h_pb,
                    "targetToken": "target-a",
                    "operation": pb
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted
            .set_target_version_override(Some(Err(PlatformError::session_unavailable("tv-down"))));
        let snap_2 = leases_2
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(snap_2.state, LeaseState::Uncertain);
        assert_eq!(snap_2.cleanup, CleanupOutcome::Succeeded);
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Button {
                button: PointerButton::Left
            }]
        );
        let renew_err_2 = leases_2
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_err_2.code, "LEASE_EXPIRED");
    }

    #[test]
    fn between_segment_pointer_drift_refreshes_ledger_before_smooth_move() {
        use deskweave_platform_api::{PausePoint, PhysicalPoint, compute_segment_payload_hash};

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-ptr-refresh", "ws-ptr-refresh");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-ptr-refresh",
                    "targets": ["target-a"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        // Segment 1 populates both lease.pointer and lease.input_target_token at (100, 100).
        let move_1 = json!({
            "id": "ptr-move-1",
            "title": "初始移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 100, "y": 100},
                "durationMs": 0
            },
            "on": {}
        });
        let h_1 = compute_segment_payload_hash("target-a", &move_1, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-ptr-1",
                    "payloadHash": h_1,
                    "targetToken": "target-a",
                    "operation": move_1
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Simulate external pointer movement between segments while interruptPolicy=ignore.
        scripted.inner.set_pointer(PhysicalPoint { x: 400, y: 300 });
        let before = scripted.inner.submitted_actions().len();
        let move_2 = json!({
            "id": "ptr-move-2",
            "title": "跨段观测平滑移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 500, "y": 380},
                "durationMs": 40
            },
            "on": {}
        });
        let h_2 = compute_segment_payload_hash("target-a", &move_2, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-ptr-2",
                    "payloadHash": h_2,
                    "targetToken": "target-a",
                    "operation": move_2
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let moved = &scripted.inner.submitted_actions()[before..];
        assert!(moved.len() >= 5);
        for (_, action) in moved {
            let NativeInputAction::PointerMovePhysical(pt) = action else {
                panic!("expected PointerMovePhysical");
            };
            assert!(
                (400..=500).contains(&pt.x) && (300..=380).contains(&pt.y),
                "unexpected jump away from live observed (400, 300) -> (500, 380): {pt:?}"
            );
        }

        // Another external pointer move before a non-pointer segment also refreshes ledger & pause snapshot.
        scripted.inner.set_pointer(PhysicalPoint { x: 610, y: 220 });
        let kd = json!({
            "id": "ptr-refresh-kd",
            "title": "按键检查刷新位置",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-ptr-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(
            paused.pause_snapshot.unwrap().pointer,
            PausePoint::Physical(PhysicalPoint { x: 610, y: 220 })
        );
    }

    #[test]
    fn idle_held_lease_emergency_lock_revokes_clears_waiters_and_blocks_concurrent_renew() {
        use deskweave_platform_api::{
            CleanupOutcome, EmergencyState, HeldInput, LeaseState, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx_owner = make_ctx("sess-emg-idle", "ws-emg-idle");
        let ctx_waiter = make_ctx("sess-emg-waiter", "ws-emg-idle");
        let ctx_other_same_ws = make_ctx("sess-emg-other-same-ws", "ws-emg-idle");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap();

        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-idle", "targets": ["target-a"], "waitMs": 0})),
                &ctx_owner,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let kd = json!({
            "id": "emg-idle-kd",
            "title": "段间保持按键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-emg-idle-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let stop_bg = StopSignal::new().unwrap();
        let bg_handle = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let s = stop_bg.clone();
            thread::spawn(move || {
                l.drive_expiry_loop(d.as_ref(), &s);
            })
        };

        // Subcase A1: Pure autonomous `drive_expiry_loop` revocation and cleanup with zero waiters and zero RPCs.
        let releases_before_bg = scripted.inner.released_batches().len();
        let bg_emg_gen = scripted.emergency().trigger_lock().unwrap();
        let bg_cleanup_deadline = Instant::now() + Duration::from_secs(2);
        while scripted.inner.released_batches().len() == releases_before_bg {
            assert!(
                Instant::now() < bg_cleanup_deadline,
                "background emergency monitor did not release idle held inputs in time"
            );
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let non_owner_bg_renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx_other_same_ws,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(non_owner_bg_renew_err.code, "ACCESS_DENIED");
        let owner_bg_renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(owner_bg_renew_err.code, "RESOURCE_DENIED");
        let bg_status = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq.lease_id})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(bg_status.state, LeaseState::Released);
        assert_eq!(bg_status.reason.as_deref(), Some("EMERGENCY_LOCKED"));
        scripted.emergency().rearm(bg_emg_gen).unwrap();
        assert_eq!(scripted.emergency().state(), EmergencyState::Armed);
        let after_bg_rearm_renew = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(after_bg_rearm_renew.code, "LEASE_EXPIRED");

        // Subcase A2: Wait for background cleanup and wait-queue drain before invoking any coordinator RPC.
        let acq_w = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-emg-idle-waiter", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_w.lease_id,
                    "generation": acq_w.generation,
                    "segmentId": "seg-emg-idle-waiter-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let waiter_handle = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = ctx_waiter;
            thread::spawn(move || {
                l.handle_lease_acquire(
                    &json_obj(json!({
                        "runId": "run-emg-waiter",
                        "targets": ["target-a"],
                        "waitMs": 5000
                    })),
                    &c,
                    d.as_ref(),
                    || false,
                    5000,
                )
            })
        };

        let queue_deadline = Instant::now() + Duration::from_secs(2);
        while leases.wait_queue_len() == 0 {
            assert!(
                Instant::now() < queue_deadline,
                "waiter did not enqueue in time"
            );
            thread::sleep(Duration::from_millis(5));
        }

        let releases_before_lock = scripted.inner.released_batches().len();
        let emg_gen = scripted.emergency().trigger_lock().unwrap();
        let cleanup_deadline = Instant::now() + Duration::from_secs(2);
        while scripted.inner.released_batches().len() == releases_before_lock
            || leases.wait_queue_len() != 0
        {
            assert!(
                Instant::now() < cleanup_deadline,
                "idle emergency cleanup did not settle before RPC"
            );
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let waiter_err = waiter_handle.join().unwrap().unwrap_err();
        assert_eq!(waiter_err.code, "RESOURCE_DENIED");

        let non_owner_renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_w.lease_id, "generation": acq_w.generation})),
                &ctx_other_same_ws,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(non_owner_renew_err.code, "ACCESS_DENIED");

        let renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_w.lease_id, "generation": acq_w.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_err.code, "RESOURCE_DENIED");

        let status = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_w.lease_id})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(status.state, LeaseState::Released);
        assert_eq!(status.reason.as_deref(), Some("EMERGENCY_LOCKED"));

        scripted.emergency().rearm(emg_gen).unwrap();
        assert_eq!(scripted.emergency().state(), EmergencyState::Armed);
        let after_rearm_renew = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_w.lease_id, "generation": acq_w.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(after_rearm_renew.code, "LEASE_EXPIRED");

        stop_bg.trigger();
        leases.notify_all();
        bg_handle.join().unwrap();

        // Subcase B: sub-tick trigger_lock + immediate rearm without background loop still revokes idle held lease
        // and rejects any waiter woken after rearm with RESOURCE_DENIED (not SESSION_UNAVAILABLE).
        let acq_2 = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-after-rearm", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        assert_ne!(acq_2.lease_id, acq.lease_id);
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_2.lease_id,
                    "generation": acq_2.generation,
                    "segmentId": "seg-emg-subtick-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let waiter_rearm_handle = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let c = make_ctx("sess-emg-waiter-rearm", "ws-emg-idle");
            thread::spawn(move || {
                l.handle_lease_acquire(
                    &json_obj(json!({
                        "runId": "run-emg-waiter-rearm",
                        "targets": ["target-a"],
                        "waitMs": 5000
                    })),
                    &c,
                    d.as_ref(),
                    || false,
                    5000,
                )
            })
        };
        let queue_deadline_2 = Instant::now() + Duration::from_secs(2);
        while leases.wait_queue_len() == 0 {
            assert!(
                Instant::now() < queue_deadline_2,
                "second waiter did not enqueue in time"
            );
            thread::sleep(Duration::from_millis(5));
        }

        let releases_before_subtick = scripted.inner.released_batches().len();
        let subtick_lock_gen = scripted.emergency().trigger_lock().unwrap();
        scripted.emergency().rearm(subtick_lock_gen).unwrap();
        leases.notify_all();

        let waiter_rearm_err = waiter_rearm_handle.join().unwrap().unwrap_err();
        assert_eq!(waiter_rearm_err.code, "RESOURCE_DENIED");

        let subtick_renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_2.lease_id, "generation": acq_2.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(subtick_renew_err.code, "LEASE_EXPIRED");
        assert_eq!(
            scripted.inner.released_batches().len(),
            releases_before_subtick + 1
        );
        let status_2 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_2.lease_id})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(status_2.state, LeaseState::Released);
        assert_eq!(status_2.reason.as_deref(), Some("EMERGENCY_LOCKED"));

        // Subcase C: Idle emergency lock with release failure marks Uncertain and blocks subsequent acquire after rearm.
        let acq_3 = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-emg-uncertain", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_3.lease_id,
                    "generation": acq_3.generation,
                    "segmentId": "seg-emg-uncertain-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        scripted.inner.set_cleanup_outcome(CleanupOutcome::Failed);
        let uncertain_lock_gen = scripted.emergency().trigger_lock().unwrap();
        let uncertain_renew_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_3.lease_id, "generation": acq_3.generation})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(uncertain_renew_err.code, "RESOURCE_DENIED");
        let status_3 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_3.lease_id})),
                &ctx_owner,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(status_3.state, LeaseState::Uncertain);
        assert_eq!(status_3.cleanup, CleanupOutcome::Failed);
        scripted.emergency().rearm(uncertain_lock_gen).unwrap();
        let after_uncertain_acq = leases
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-blocked-uncertain", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap_err();
        assert_eq!(after_uncertain_acq.code, "EFFECT_UNCERTAIN");

        // Subcases D/E/F on clean coordinator: paused resume emergency lock, TTL+emergency ordering, and pointer_position trigger during finalize_or_uncertain_pausing_locked.
        let (topo_2, caps_2) = macos_topology_and_caps();
        let scripted_2 = Arc::new(ScriptedDesktop::new(caps_2, Ok(topo_2)));
        scripted_2
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases_2 = Arc::new(LeaseCoordinator::new());
        let ctx_owner_2 = make_ctx("sess-emg-2-owner", "ws-emg-2");
        let ctx_other_2 = make_ctx("sess-emg-2-other", "ws-emg-2");
        leases_2
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap();

        // Subcase D: Paused lease + trigger_lock() enforces ACCESS_DENIED / RESOURCE_DENIED on resume and stays revoked across rearm.
        let acq_d = leases_2
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-emg-paused-d", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let paused_d = leases_2
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_d.lease_id, "generation": acq_d.generation})),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap();
        let pause_id_d = paused_d.pause_snapshot.unwrap().pause_id;
        let emg_gen_d = scripted_2.emergency().trigger_lock().unwrap();
        let non_owner_resume_err = leases_2
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq_d.lease_id,
                    "generation": acq_d.generation,
                    "pauseId": pause_id_d,
                    "resumeAttemptId": "att-emg-non-owner",
                    "resumePlanHash": "a".repeat(64)
                })),
                &ctx_other_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(non_owner_resume_err.code, "ACCESS_DENIED");
        let owner_resume_err = leases_2
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq_d.lease_id,
                    "generation": acq_d.generation,
                    "pauseId": pause_id_d,
                    "resumeAttemptId": "att-emg-owner",
                    "resumePlanHash": "b".repeat(64)
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(owner_resume_err.code, "RESOURCE_DENIED");
        scripted_2.emergency().rearm(emg_gen_d).unwrap();
        let status_d = leases_2
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_d.lease_id})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap();
        assert_eq!(status_d.state, LeaseState::Released);
        assert_eq!(status_d.reason.as_deref(), Some("EMERGENCY_LOCKED"));
        let resume_after_rearm_err = leases_2
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq_d.lease_id,
                    "generation": acq_d.generation,
                    "pauseId": pause_id_d,
                    "resumeAttemptId": "att-emg-after-rearm",
                    "resumePlanHash": "c".repeat(64)
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(resume_after_rearm_err.code, "LEASE_EXPIRED");

        // Subcase E: Idle lease with both trigger_lock() and 2s heartbeat TTL elapsed records EMERGENCY_LOCKED (not LEASE_EXPIRED) on segment.submit.
        let acq_e = leases_2
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-ttl-e", "targets": ["target-a"], "waitMs": 0})),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let emg_gen_e = scripted_2.emergency().trigger_lock().unwrap();
        scripted_2.inner.fake_clock().advance_us(2_500_000);
        let seg_emg_err = leases_2
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_e.lease_id,
                    "generation": acq_e.generation,
                    "segmentId": "seg-emg-before-ttl",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(seg_emg_err.code, "RESOURCE_DENIED");
        let status_e = leases_2
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_e.lease_id})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap();
        assert_eq!(status_e.state, LeaseState::Released);
        assert_eq!(status_e.reason.as_deref(), Some("EMERGENCY_LOCKED"));
        scripted_2.emergency().rearm(emg_gen_e).unwrap();

        // Subcase F: Emergency lock triggered inside pointer_position during in-flight pause revokes EMERGENCY_LOCKED rather than wedging Uncertain.
        let acq_f = leases_2
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-emg-ptr-pause-f", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let (entered_rx, release_tx) = scripted_2.gate_submit_on(1);
        let seg_handle_f = {
            let l = Arc::clone(&leases_2);
            let d = Arc::clone(&scripted_2);
            let c = ctx_owner_2.clone();
            let lid = acq_f.lease_id.clone();
            let lease_gen = acq_f.generation;
            let op = kd.clone();
            let ph = h_kd.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-emg-ptr-pause",
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("gated segment did not enter submit");
        let pause_handle_f = {
            let l = Arc::clone(&leases_2);
            let d = Arc::clone(&scripted_2);
            let c = ctx_owner_2.clone();
            let lid = acq_f.lease_id.clone();
            let lease_gen = acq_f.generation;
            thread::spawn(move || {
                l.handle_lease_pause(
                    &json_obj(json!({"leaseId": lid, "generation": lease_gen})),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        let pausing_deadline = Instant::now() + Duration::from_secs(2);
        while leases_2
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_f.lease_id})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap()
            .state
            != LeaseState::Pausing
        {
            assert!(
                Instant::now() < pausing_deadline,
                "lease did not enter Pausing state before release"
            );
            thread::sleep(Duration::from_millis(5));
        }
        scripted_2.set_trigger_emergency_on_pointer(true);
        release_tx.send(()).unwrap();
        let _ = seg_handle_f.join().unwrap().unwrap();
        let pause_err_f = pause_handle_f.join().unwrap().unwrap_err();
        assert_eq!(pause_err_f.code, "RESOURCE_DENIED");
        let status_f = leases_2
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_f.lease_id})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap();
        assert_eq!(status_f.state, LeaseState::Released);
        assert_eq!(status_f.reason.as_deref(), Some("EMERGENCY_LOCKED"));
        let emg_gen_f = scripted_2.emergency().generation();
        scripted_2.emergency().rearm(emg_gen_f).unwrap();
        let acq_after_f = leases_2
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-after-ptr-emg", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases_2
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_after_f.lease_id,
                    "generation": acq_after_f.generation,
                    "reason": "finished"
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap();

        // Subcase G: Emergency lock inside finalize_pause_locked via suspend_segment_on_pause_locked (dispatched_count == 0, held_inputs non-empty) revokes EMERGENCY_LOCKED instead of wedging Uncertain.
        let acq_g = leases_2
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-emg-suspend-zero-g", "targets": ["target-a"], "waitMs": 0})),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases_2
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_g.lease_id,
                    "generation": acq_g.generation,
                    "segmentId": "seg-emg-g-kd",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap();
        let move_op_g = json!({
            "id": "slow-move-emg-g",
            "title": "未派发延迟段急停清理",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 140, "y": 90},
                "durationMs": 200_000
            },
            "on": {}
        });
        let h_move_g = compute_segment_payload_hash("target-a", &move_op_g, None).unwrap();
        let h_move_g_q = h_move_g.clone();
        let seg_handle_g = {
            let l = Arc::clone(&leases_2);
            let d = Arc::clone(&scripted_2);
            let c = ctx_owner_2.clone();
            let lid = acq_g.lease_id.clone();
            let lease_gen = acq_g.generation;
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-emg-g-move",
                        "payloadHash": h_move_g,
                        "targetToken": "target-a",
                        "operation": move_op_g
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        let running_deadline_g = Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(st) = leases_2.handle_segment_status(
                &json_obj(json!({"segmentId": "seg-emg-g-move", "payloadHash": h_move_g_q})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            ) && st.state == deskweave_platform_api::SegmentState::Running
            {
                break;
            }
            assert!(
                Instant::now() < running_deadline_g,
                "delayed segment did not enter Running state in time"
            );
            thread::sleep(Duration::from_millis(2));
        }
        let rel_before_g = scripted_2.inner.released_batches().len();
        scripted_2.set_trigger_emergency_on_pointer(true);
        let pause_err_g = leases_2
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq_g.lease_id, "generation": acq_g.generation})),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(pause_err_g.code, "RESOURCE_DENIED");
        let seg_err_g = seg_handle_g.join().unwrap().unwrap_err();
        assert_eq!(seg_err_g.code, "RESOURCE_DENIED");
        let status_g = leases_2
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_g.lease_id})),
                &ctx_owner_2,
                scripted_2.as_ref(),
            )
            .unwrap();
        assert_eq!(status_g.state, LeaseState::Released);
        assert_eq!(status_g.reason.as_deref(), Some("EMERGENCY_LOCKED"));
        assert_eq!(scripted_2.inner.released_batches().len(), rel_before_g + 1);
        assert_eq!(
            scripted_2.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let emg_gen_g = scripted_2.emergency().generation();
        scripted_2.emergency().rearm(emg_gen_g).unwrap();
        let acq_after_g = leases_2
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-after-suspend-zero-g", "targets": ["target-a"], "waitMs": 0})),
                &ctx_owner_2,
                scripted_2.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases_2
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_after_g.lease_id,
                    "generation": acq_after_g.generation,
                    "reason": "finished"
                })),
                &ctx_owner_2,
                scripted_2.as_ref(),
                5000,
            )
            .unwrap();
    }

    #[test]
    fn cross_segment_held_key_target_unfocus_and_drift_pauses_on_renew_and_monitor() {
        use deskweave_platform_api::{
            DisplayTopology, HeldInput, LeaseState, TargetStatus, compute_segment_payload_hash,
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = Arc::new(LeaseCoordinator::new());
        let ctx = make_ctx("sess-cross-focus", "ws-cross-focus");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: interruptPolicy=ignore + cross-segment held ShiftLeft -> foreground switch -> lease.renew pauses & releases.
        let acq_a = leases
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-focus-renew",
                    "targets": ["target-a"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_shift = json!({
            "id": "focus-kd-shift",
            "title": "跨段按住Shift",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_shift = compute_segment_payload_hash("target-a", &kd_shift, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "segmentId": "seg-focus-a",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let renew_unfocus_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_a.lease_id, "generation": acq_a.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_unfocus_err.code, "TARGET_LOST");
        assert_eq!(renew_unfocus_err.retry, "afterQuery");
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        let st_a = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_a.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_a.state, LeaseState::Paused);
        assert_eq!(st_a.reason.as_deref(), Some("TARGET_LOST"));
        assert_eq!(
            st_a.pause_snapshot.unwrap().held_inputs,
            vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Subcase B: background monitor pauses & releases ControlLeft without awaiting another RPC.
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Active);
        let acq_b = leases
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-focus-monitor",
                    "targets": ["target-a"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_ctrl = json!({
            "id": "focus-kd-ctrl",
            "title": "跨段按住Ctrl",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ControlLeft"},
            "on": {}
        });
        let h_ctrl = compute_segment_payload_hash("target-a", &kd_ctrl, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "segmentId": "seg-focus-b",
                    "payloadHash": h_ctrl,
                    "targetToken": "target-a",
                    "operation": kd_ctrl
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let releases_before = scripted.inner.released_batches().len();
        let stop_bg = StopSignal::new().unwrap();
        let bg_handle = {
            let l = Arc::clone(&leases);
            let d = Arc::clone(&scripted);
            let s = stop_bg.clone();
            thread::spawn(move || {
                l.drive_expiry_loop(d.as_ref(), &s);
            })
        };
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let release_deadline = Instant::now() + Duration::from_secs(2);
        while scripted.inner.released_batches().len() == releases_before {
            assert!(
                Instant::now() < release_deadline,
                "background monitor did not pause unfocused lease in time"
            );
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
        let st_b = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_b.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_b.state, LeaseState::Paused);
        assert_eq!(st_b.reason.as_deref(), Some("TARGET_LOST"));
        assert_eq!(
            st_b.pause_snapshot.unwrap().held_inputs,
            vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
        );
        stop_bg.trigger();
        leases.notify_all();
        bg_handle.join().unwrap();
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Subcase C: target_version drift and topology_version drift during renew pause & preserve pre-drift baseline.
        scripted
            .inner
            .set_target_status("target-a", TargetStatus::Active);
        let acq_c1 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-drift-tv", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_alt = json!({
            "id": "focus-kd-alt",
            "title": "跨段按住Alt",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "AltLeft"},
            "on": {}
        });
        let h_alt = compute_segment_payload_hash("target-a", &kd_alt, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_c1.lease_id,
                    "generation": acq_c1.generation,
                    "segmentId": "seg-focus-c1",
                    "payloadHash": h_alt,
                    "targetToken": "target-a",
                    "operation": kd_alt
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        scripted.inner.set_target_version("target-a", "2");
        let tv_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_c1.lease_id, "generation": acq_c1.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(tv_err.code, "TARGET_LOST");
        assert_eq!(tv_err.retry, "afterQuery");
        let st_c1 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_c1.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_c1.state, LeaseState::Paused);
        assert_eq!(st_c1.reason.as_deref(), Some("TARGET_LOST"));
        assert_eq!(st_c1.pause_snapshot.unwrap().targets[0].target_version, "1");
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_c1.lease_id,
                    "generation": acq_c1.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        scripted.inner.set_target_version("target-a", "1");
        let acq_c2 = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-drift-topo", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_c2.lease_id,
                    "generation": acq_c2.generation,
                    "segmentId": "seg-focus-c2",
                    "payloadHash": h_alt,
                    "targetToken": "target-a",
                    "operation": kd_alt
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let expected_base_topo_ver = base_topo.version().to_string();
        let mut drifted_topo = base_topo;
        match &mut drifted_topo {
            DisplayTopology::Physical(t) => t.version += 1,
            DisplayTopology::DisplayLocal(t) => t.version += 1,
        }
        scripted.inner.set_topology(Ok(drifted_topo));
        let topo_err = leases
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_c2.lease_id, "generation": acq_c2.generation})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(topo_err.code, "TARGET_LOST");
        assert_eq!(topo_err.retry, "afterQuery");
        let st_c2 = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_c2.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_c2.state, LeaseState::Paused);
        assert_eq!(st_c2.reason.as_deref(), Some("TOPOLOGY_CHANGED"));
        assert_eq!(
            st_c2.pause_snapshot.unwrap().targets[0].topology_version,
            expected_base_topo_ver
        );

        // Subcase D: Multi-target un-held focus switch between authorized targets keeps lease Active until all bound targets lose focus or held_inputs is non-empty.
        let (topo_d, caps_d) = macos_topology_and_caps();
        let scripted_d = Arc::new(ScriptedDesktop::new(caps_d, Ok(topo_d)));
        scripted_d
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        scripted_d
            .inner
            .register_grant("grant-b", target_desc("target-b", "2099-01-01T00:00:00Z"));
        let leases_d = Arc::new(LeaseCoordinator::new());
        let ctx_d = make_ctx("sess-multi-focus", "ws-multi-focus");
        leases_d
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap();
        leases_d
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-b"})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap();

        let acq_d1 = leases_d
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-multi-focus-1",
                    "targets": ["target-a", "target-b"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx_d,
                scripted_d.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let move_a = json!({
            "id": "multi-move-a",
            "title": "无按住切换焦点移动",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 110, "y": 120},
                "durationMs": 0
            },
            "on": {}
        });
        let h_move_a = compute_segment_payload_hash("target-a", &move_a, None).unwrap();
        leases_d
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_d1.lease_id,
                    "generation": acq_d1.generation,
                    "segmentId": "seg-multi-move-a",
                    "payloadHash": h_move_a,
                    "targetToken": "target-a",
                    "operation": move_a
                })),
                &ctx_d,
                scripted_d.as_ref(),
                5000,
            )
            .unwrap();

        scripted_d
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let renew_ok = leases_d
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_d1.lease_id, "generation": acq_d1.generation})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap();
        assert_eq!(renew_ok.generation, acq_d1.generation);
        assert_eq!(
            leases_d
                .handle_lease_status(
                    &json_obj(json!({"leaseId": acq_d1.lease_id})),
                    &ctx_d,
                    scripted_d.as_ref(),
                )
                .unwrap()
                .state,
            LeaseState::Active
        );

        scripted_d
            .inner
            .set_target_status("target-b", TargetStatus::Unfocused);
        let renew_all_unfocused = leases_d
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_d1.lease_id, "generation": acq_d1.generation})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_all_unfocused.code, "TARGET_LOST");
        let st_d1 = leases_d
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_d1.lease_id})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap();
        assert_eq!(st_d1.state, LeaseState::Paused);
        assert_eq!(st_d1.reason.as_deref(), Some("TARGET_LOST"));
        leases_d
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_d1.lease_id,
                    "generation": acq_d1.generation,
                    "reason": "cancelled"
                })),
                &ctx_d,
                scripted_d.as_ref(),
                5000,
            )
            .unwrap();

        scripted_d
            .inner
            .set_target_status("target-a", TargetStatus::Active);
        scripted_d
            .inner
            .set_target_status("target-b", TargetStatus::Active);
        let acq_d2 = leases_d
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-multi-focus-2",
                    "targets": ["target-a", "target-b"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx_d,
                scripted_d.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        leases_d
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_d2.lease_id,
                    "generation": acq_d2.generation,
                    "segmentId": "seg-multi-kd-a",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx_d,
                scripted_d.as_ref(),
                5000,
            )
            .unwrap();
        let rel_before_d2 = scripted_d.inner.released_batches().len();
        scripted_d
            .inner
            .set_target_status("target-a", TargetStatus::Unfocused);
        let renew_held_err = leases_d
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_d2.lease_id, "generation": acq_d2.generation})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap_err();
        assert_eq!(renew_held_err.code, "TARGET_LOST");
        let st_d2 = leases_d
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_d2.lease_id})),
                &ctx_d,
                scripted_d.as_ref(),
            )
            .unwrap();
        assert_eq!(st_d2.state, LeaseState::Paused);
        assert_eq!(st_d2.reason.as_deref(), Some("TARGET_LOST"));
        assert_eq!(scripted_d.inner.released_batches().len(), rel_before_d2 + 1);
        assert_eq!(
            scripted_d.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ShiftLeft".to_owned()
            }]
        );
    }

    #[test]
    fn between_segment_external_pointer_move_samples_live_position_on_direct_pause() {
        use deskweave_platform_api::{
            DisplayPoint, LeaseState, PausePoint, PhysicalPoint, compute_segment_payload_hash,
        };

        let (topo, caps) = macos_topology_and_caps();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(topo)));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));

        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-pause-live-ptr", "ws-pause-live-ptr");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        let acq = leases
            .handle_lease_acquire(
                &json_obj(json!({
                    "runId": "run-pause-live-ptr",
                    "targets": ["target-a"],
                    "waitMs": 0,
                    "interruptPolicy": "ignore"
                })),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();

        let move_1 = json!({
            "id": "pause-ptr-move-1",
            "title": "记录初始坐标",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 120, "y": 140},
                "durationMs": 0
            },
            "on": {}
        });
        let h_1 = compute_segment_payload_hash("target-a", &move_1, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-live-1",
                    "payloadHash": h_1,
                    "targetToken": "target-a",
                    "operation": move_1
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Simulate external pointer movement after move_1, then directly pause.
        scripted.inner.set_pointer(PhysicalPoint { x: 520, y: 340 });
        let paused = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(paused.state, LeaseState::Paused);
        let snap = paused.pause_snapshot.unwrap();
        assert_eq!(
            snap.pointer,
            PausePoint::Physical(PhysicalPoint { x: 520, y: 340 })
        );

        let resumed = leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap.pause_id,
                    "resumeAttemptId": "att-live-ptr",
                    "resumePlanHash": "a".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(resumed.state, LeaseState::Active);

        let move_disp = json!({
            "id": "pause-ptr-move-disp",
            "title": "切换到副屏前坐标",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 150, "y": 160},
                "durationMs": 0
            },
            "on": {}
        });
        let h_disp = compute_segment_payload_hash("target-a", &move_disp, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-live-disp",
                    "payloadHash": h_disp,
                    "targetToken": "target-a",
                    "operation": move_disp
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let disp_pt = PausePoint::Display(DisplayPoint {
            display_id: "secondary".to_owned(),
            x: 42.5,
            y: 88.25,
        });
        scripted.inner.set_pause_pointer(disp_pt.clone());
        let paused_disp = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        let snap_disp = paused_disp.pause_snapshot.unwrap();
        assert_eq!(snap_disp.pointer, disp_pt);
        leases
            .handle_lease_resume(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "pauseId": snap_disp.pause_id,
                    "resumeAttemptId": "att-live-disp",
                    "resumePlanHash": "b".repeat(64)
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        let move_2 = json!({
            "id": "pause-ptr-move-2",
            "title": "再次记录坐标",
            "action": "pointer.move",
            "params": {
                "target": "main",
                "position": {"kind": "point", "space": "desktopPhysical", "x": 200, "y": 200},
                "durationMs": 0
            },
            "on": {}
        });
        let h_2 = compute_segment_payload_hash("target-a", &move_2, None).unwrap();
        leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq.lease_id,
                    "generation": acq.generation,
                    "segmentId": "seg-live-2",
                    "payloadHash": h_2,
                    "targetToken": "target-a",
                    "operation": move_2
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // If live pointer query fails on direct pause, do NOT reuse old ledger (200, 200).
        scripted
            .inner
            .set_pointer_error(Some(PlatformError::session_unavailable("pointer down")));
        let pause_err = leases
            .handle_lease_pause(
                &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(pause_err.code, "SESSION_UNAVAILABLE");

        #[cfg(not(feature = "test-authority"))]
        {
            scripted
                .inner
                .set_pointer_error(Some(PlatformError::capability_not_implemented("unimpl")));
            let pause_unimpl_err = leases
                .handle_lease_pause(
                    &json_obj(json!({"leaseId": acq.lease_id, "generation": acq.generation})),
                    &ctx,
                    scripted.as_ref(),
                    5000,
                )
                .unwrap_err();
            assert_eq!(pause_unimpl_err.code, "CAPABILITY_NOT_IMPLEMENTED");
        }
    }

    #[test]
    fn pre_submit_native_target_probes_run_unlocked_and_recheck_ttl_and_rpc_deadline() {
        use deskweave_platform_api::{LeaseState, SegmentState, compute_segment_payload_hash};

        let kd = json!({
            "id": "probe-zero-kd",
            "title": "零延迟按下校验",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "ShiftLeft"},
            "on": {}
        });
        let h_kd = compute_segment_payload_hash("target-a", &kd, None).unwrap();

        // Subcase A: TTL consumed during drive_segment_events native probe aborts before key emit.
        let (topo_a, caps_a) = macos_topology_and_caps();
        let scripted_a = Arc::new(ScriptedDesktop::new(caps_a, Ok(topo_a)));
        scripted_a
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases_a = LeaseCoordinator::new();
        let ctx_a = make_ctx("sess-probe-ttl", "ws-probe-ttl");
        leases_a
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_a,
                scripted_a.as_ref(),
            )
            .unwrap();
        let acq_a = leases_a
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-probe-ttl", "targets": ["target-a"], "waitMs": 0})),
                &ctx_a,
                scripted_a.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        scripted_a.inner.fake_clock().advance_us(1_900_000);
        scripted_a.on_verify_call(2, |inner| {
            inner.fake_clock().advance_us(200_000);
        });
        let seg_err_a = leases_a
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "segmentId": "seg-probe-ttl",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_a,
                scripted_a.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(seg_err_a.code, "LEASE_EXPIRED");
        assert!(scripted_a.inner.submitted_actions().is_empty());
        let st_a = leases_a
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_a.lease_id})),
                &ctx_a,
                scripted_a.as_ref(),
            )
            .unwrap();
        assert_eq!(st_a.state, LeaseState::Released);
        assert_eq!(st_a.reason.as_deref(), Some("LEASE_EXPIRED"));

        // Subcase B: Concurrent lease.renew succeeds while drive_segment_events probe is blocked unlocked.
        let (topo_b, caps_b) = macos_topology_and_caps();
        let scripted_b = Arc::new(ScriptedDesktop::new(caps_b, Ok(topo_b)));
        scripted_b
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases_b = Arc::new(LeaseCoordinator::new());
        let ctx_b = make_ctx("sess-probe-renew", "ws-probe-renew");
        leases_b
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_b,
                scripted_b.as_ref(),
            )
            .unwrap();
        let acq_b = leases_b
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-probe-renew", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_b,
                scripted_b.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let (entered_rx, release_tx) = scripted_b.gate_verify_on(2);
        let seg_handle_b = {
            let l = Arc::clone(&leases_b);
            let d = Arc::clone(&scripted_b);
            let c = ctx_b.clone();
            let lid = acq_b.lease_id.clone();
            let lease_gen = acq_b.generation;
            let ph = h_kd.clone();
            let op = kd.clone();
            thread::spawn(move || {
                l.handle_segment_submit(
                    &json_obj(json!({
                        "leaseId": lid,
                        "generation": lease_gen,
                        "segmentId": "seg-probe-renew",
                        "payloadHash": ph,
                        "targetToken": "target-a",
                        "operation": op
                    })),
                    &c,
                    d.as_ref(),
                    5000,
                )
            })
        };
        entered_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("drive_segment_events probe did not reach verify_target");
        scripted_b.inner.fake_clock().advance_us(1_900_000);
        let renew_b = leases_b
            .handle_lease_renew(
                &json_obj(json!({"leaseId": acq_b.lease_id, "generation": acq_b.generation})),
                &ctx_b,
                scripted_b.as_ref(),
            )
            .unwrap();
        assert_eq!(renew_b.generation, acq_b.generation);
        scripted_b.inner.fake_clock().advance_us(200_000);
        release_tx.send(()).unwrap();
        let seg_res_b = seg_handle_b.join().unwrap().unwrap();
        assert_eq!(seg_res_b.state, SegmentState::Sent);
        assert_eq!(scripted_b.inner.submitted_actions().len(), 1);

        // Subcase C: RPC deadlineMs consumed during native probe fails with DEADLINE_EXCEEDED without pressing key.
        let (topo_c, caps_c) = macos_topology_and_caps();
        let scripted_c = Arc::new(ScriptedDesktop::new(caps_c, Ok(topo_c)));
        scripted_c
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases_c = LeaseCoordinator::new();
        let ctx_c = make_ctx("sess-probe-deadline", "ws-probe-deadline");
        leases_c
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx_c,
                scripted_c.as_ref(),
            )
            .unwrap();
        let acq_c = leases_c
            .handle_lease_acquire(
                &json_obj(
                    json!({"runId": "run-probe-deadline", "targets": ["target-a"], "waitMs": 0}),
                ),
                &ctx_c,
                scripted_c.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        scripted_c.on_verify_call(2, |_| {
            thread::sleep(Duration::from_millis(80));
        });
        let seg_err_c = leases_c
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_c.lease_id,
                    "generation": acq_c.generation,
                    "segmentId": "seg-probe-deadline",
                    "payloadHash": h_kd,
                    "targetToken": "target-a",
                    "operation": kd
                })),
                &ctx_c,
                scripted_c.as_ref(),
                30,
            )
            .unwrap_err();
        assert_eq!(seg_err_c.code, "DEADLINE_EXCEEDED");
        assert!(scripted_c.inner.submitted_actions().is_empty());
    }

    #[test]
    fn segment_submit_checks_existing_baseline_before_admission_and_pauses_or_releases_on_drift() {
        use deskweave_platform_api::{
            DisplayTopology, HeldInput, LeaseState, NativeInputAction, SegmentState,
            compute_segment_payload_hash,
        };

        let make_kd = |id: &str, title: &str, key: &str| {
            json!({
                "id": id,
                "title": title,
                "action": "keyboard.down",
                "params": {"target": "main", "key": key},
                "on": {}
            })
        };

        let (base_topo, caps) = macos_topology_and_caps();
        let expected_base_topo_ver = base_topo.version().to_string();
        let scripted = Arc::new(ScriptedDesktop::new(caps, Ok(base_topo.clone())));
        scripted
            .inner
            .register_grant("grant-a", target_desc("target-a", "2099-01-01T00:00:00Z"));
        let leases = LeaseCoordinator::new();
        let ctx = make_ctx("sess-baseline-drift", "ws-baseline-drift");
        leases
            .handle_target_bind(
                &json_obj(json!({"selectionGrant": "grant-a"})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();

        // Subcase A: target version drift between segments pauses lease, preserves old baseline, and releases ShiftLeft without admitting KeyA.
        let acq_a = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-drift-a", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_shift = make_kd("base-kd-shift", "保持Shift", "ShiftLeft");
        let h_shift = compute_segment_payload_hash("target-a", &kd_shift, None).unwrap();
        let res_a1 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "segmentId": "seg-drift-a1",
                    "payloadHash": h_shift,
                    "targetToken": "target-a",
                    "operation": kd_shift
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res_a1.state, SegmentState::Sent);
        assert_eq!(scripted.inner.submitted_actions().len(), 1);
        assert!(scripted.inner.released_batches().is_empty());

        scripted.inner.set_target_version("target-a", "2");
        let kd_a = make_kd("base-kd-a", "目标版本漂移阻断", "KeyA");
        let h_a = compute_segment_payload_hash("target-a", &kd_a, None).unwrap();
        let err_a2 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "segmentId": "seg-drift-a2",
                    "payloadHash": h_a,
                    "targetToken": "target-a",
                    "operation": kd_a
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err_a2.code, "TARGET_LOST");
        assert_eq!(err_a2.retry, "afterQuery");
        let actions_a = scripted.inner.submitted_actions();
        assert_eq!(actions_a.len(), 1);
        assert!(
            !actions_a
                .iter()
                .any(|(_, act)| matches!(act, NativeInputAction::KeyDown(k) if k == "KeyA"))
        );
        assert_eq!(
            scripted.inner.released_batches(),
            vec![vec![HeldInput::Key {
                key: "ShiftLeft".to_owned(),
            }]]
        );
        let st_a = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_a.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_a.state, LeaseState::Paused);
        assert_eq!(st_a.reason.as_deref(), Some("TARGET_LOST"));
        let snap_a = st_a.pause_snapshot.unwrap();
        assert_eq!(
            snap_a.held_inputs,
            vec![HeldInput::Key {
                key: "ShiftLeft".to_owned(),
            }]
        );
        assert_eq!(snap_a.targets[0].target_version, "1");
        let seg_st_a2 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-drift-a2", "payloadHash": h_a})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(seg_st_a2.code, "INVALID_ARGUMENT");
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_a.lease_id,
                    "generation": acq_a.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Subcase B: topology version drift between segments pauses lease with TOPOLOGY_CHANGED and preserves old baseline.
        scripted.inner.set_target_version("target-a", "1");
        let acq_b = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-drift-b", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_ctrl = make_kd("base-kd-ctrl", "保持Ctrl", "ControlLeft");
        let h_ctrl = compute_segment_payload_hash("target-a", &kd_ctrl, None).unwrap();
        let res_b1 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "segmentId": "seg-drift-b1",
                    "payloadHash": h_ctrl,
                    "targetToken": "target-a",
                    "operation": kd_ctrl
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res_b1.state, SegmentState::Sent);
        assert_eq!(scripted.inner.submitted_actions().len(), 2);

        let mut drifted_topo = base_topo.clone();
        match &mut drifted_topo {
            DisplayTopology::Physical(t) => t.version += 1,
            DisplayTopology::DisplayLocal(t) => t.version += 1,
        }
        scripted.inner.set_topology(Ok(drifted_topo));
        let kd_b = make_kd("base-kd-b", "拓扑版本漂移阻断", "KeyB");
        let h_b = compute_segment_payload_hash("target-a", &kd_b, None).unwrap();
        let err_b2 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "segmentId": "seg-drift-b2",
                    "payloadHash": h_b,
                    "targetToken": "target-a",
                    "operation": kd_b
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err_b2.code, "TARGET_LOST");
        assert_eq!(err_b2.retry, "afterQuery");
        let actions_b = scripted.inner.submitted_actions();
        assert_eq!(actions_b.len(), 2);
        assert!(
            !actions_b
                .iter()
                .any(|(_, act)| matches!(act, NativeInputAction::KeyDown(k) if k == "KeyB"))
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned(),
            }]
        );
        let st_b = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_b.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_b.state, LeaseState::Paused);
        assert_eq!(st_b.reason.as_deref(), Some("TOPOLOGY_CHANGED"));
        assert_eq!(
            st_b.pause_snapshot.unwrap().targets[0].topology_version,
            expected_base_topo_ver
        );
        let seg_st_b2 = leases
            .handle_segment_status(
                &json_obj(json!({"segmentId": "seg-drift-b2", "payloadHash": h_b})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap_err();
        assert_eq!(seg_st_b2.code, "INVALID_ARGUMENT");
        leases
            .handle_lease_release(
                &json_obj(json!({
                    "leaseId": acq_b.lease_id,
                    "generation": acq_b.generation,
                    "reason": "cancelled"
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();

        // Subcase C: drift fallback revokes lease and cleans held AltLeft when pause snapshot fails.
        scripted.inner.set_topology(Ok(base_topo));
        let acq_c = leases
            .handle_lease_acquire(
                &json_obj(json!({"runId": "run-drift-c", "targets": ["target-a"], "waitMs": 0})),
                &ctx,
                scripted.as_ref(),
                || false,
                5000,
            )
            .unwrap();
        let kd_alt = make_kd("base-kd-alt", "保持Alt", "AltLeft");
        let h_alt = compute_segment_payload_hash("target-a", &kd_alt, None).unwrap();
        let res_c1 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_c.lease_id,
                    "generation": acq_c.generation,
                    "segmentId": "seg-drift-c1",
                    "payloadHash": h_alt,
                    "targetToken": "target-a",
                    "operation": kd_alt
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap();
        assert_eq!(res_c1.state, SegmentState::Sent);
        assert_eq!(scripted.inner.submitted_actions().len(), 3);

        scripted.inner.set_target_version("target-a", "3");
        scripted.set_pointer_override(Some(Err(PlatformError::session_unavailable("ptr-down"))));
        let kd_c = make_kd("base-kd-c", "暂停失败回退释放", "KeyC");
        let h_c = compute_segment_payload_hash("target-a", &kd_c, None).unwrap();
        let err_c2 = leases
            .handle_segment_submit(
                &json_obj(json!({
                    "leaseId": acq_c.lease_id,
                    "generation": acq_c.generation,
                    "segmentId": "seg-drift-c2",
                    "payloadHash": h_c,
                    "targetToken": "target-a",
                    "operation": kd_c
                })),
                &ctx,
                scripted.as_ref(),
                5000,
            )
            .unwrap_err();
        assert_eq!(err_c2.code, "TARGET_LOST");
        assert_eq!(err_c2.retry, "afterQuery");
        let actions_c = scripted.inner.submitted_actions();
        assert_eq!(actions_c.len(), 3);
        assert!(
            !actions_c
                .iter()
                .any(|(_, act)| matches!(act, NativeInputAction::KeyDown(k) if k == "KeyC"))
        );
        assert_eq!(
            scripted.inner.released_batches().last().unwrap(),
            &vec![HeldInput::Key {
                key: "AltLeft".to_owned(),
            }]
        );
        let st_c = leases
            .handle_lease_status(
                &json_obj(json!({"leaseId": acq_c.lease_id})),
                &ctx,
                scripted.as_ref(),
            )
            .unwrap();
        assert_eq!(st_c.state, LeaseState::Released);
        assert_eq!(st_c.reason.as_deref(), Some("TARGET_LOST"));
    }
}
