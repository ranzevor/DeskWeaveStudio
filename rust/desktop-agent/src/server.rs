use deskweave_platform_api::DesktopSession;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Serialize;
use serde_json::{Map, Value};
use std::io;
use std::process;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::lease::LeaseCoordinator;
use crate::native::{DeadlineIo, SessionListener, SessionStream, StopSignal};
use crate::protocol::{
    AttachChallenge, AttachHello, AttachReady, AttachRegistration, ErrorBody, ErrorResponse,
    MAX_CONTROL_FRAME, Pong, SuccessResponse, VERSION, attach_ready_proof, constant_time_eq,
    generate_session_id, is_hex, looks_like_protocol_version, parse_object, parse_strict_object,
    read_frame, sha256_hex, valid_envelope_id, valid_method_name, valid_workspace_id, write_frame,
    write_frame_bounded,
};

pub(crate) const MAX_CLIENTS: usize = 32;
pub(crate) const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
pub(crate) const FRAME_READ_TIMEOUT: Duration = Duration::from_secs(10);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_DEADLINE_MS: u64 = 86_400_000;

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
    draining: bool,
    idle_since: Option<Instant>,
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

fn dispatch_request(
    map: &Map<String, Value>,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
    leases: &LeaseCoordinator,
    is_draining: impl Fn() -> bool,
) -> io::Result<(Vec<u8>, Duration)> {
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
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求信封字段无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let Some(version) = map.get("protocolVersion").and_then(Value::as_str) else {
        return Ok((
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
        return Ok((
            serialize_error(reply_to, code, "Agent 控制协议版本不受支持。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let Some(id) = map.get("id").and_then(Value::as_str) else {
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(session) = map.get("session").and_then(Value::as_str) else {
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "会话标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(workspace) = map.get("workspace").and_then(Value::as_str) else {
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "工作空间标识无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(method) = map.get("method").and_then(Value::as_str) else {
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "方法名称无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(deadline_ms) = map
        .get("deadlineMs")
        .and_then(Value::as_u64)
        .filter(|ms| (1..=MAX_DEADLINE_MS).contains(ms))
    else {
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求时限无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    };
    let Some(params) = map.get("params").and_then(Value::as_object) else {
        return Ok((
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
        return Ok((
            serialize_error(reply_to, "INVALID_ARGUMENT", "请求信封格式无效。", "never")?,
            WRITE_TIMEOUT,
        ));
    }

    let write_budget = Duration::from_millis(deadline_ms).min(WRITE_TIMEOUT);

    if !constant_time_eq(session.as_bytes(), ctx.session_id.as_bytes())
        || workspace != ctx.workspace_id
    {
        return Ok((
            serialize_error(
                id,
                "ACCESS_DENIED",
                "请求会话或工作空间与已认证连接不匹配。",
                "never",
            )?,
            write_budget,
        ));
    }

    match method {
        "desktop.capabilities" => {
            if !params.is_empty() {
                return Ok((
                    serialize_error(
                        id,
                        "INVALID_ARGUMENT",
                        "desktop.capabilities 不接受额外参数。",
                        "never",
                    )?,
                    write_budget,
                ));
            }
            let snapshot = desktop.capabilities();
            if snapshot.validate().is_err() {
                return Ok((
                    serialize_error(
                        id,
                        "INTERNAL_ERROR",
                        "Desktop Agent 生成的平台能力摘要不符合契约。",
                        "never",
                    )?,
                    write_budget,
                ));
            }
            Ok((serialize_success(id, snapshot)?, write_budget))
        }
        "desktop.topology" => {
            if !params.is_empty() {
                return Ok((
                    serialize_error(
                        id,
                        "INVALID_ARGUMENT",
                        "desktop.topology 不接受额外参数。",
                        "never",
                    )?,
                    write_budget,
                ));
            }
            match desktop.topology() {
                Ok(topology) => {
                    if topology.validate().is_err() {
                        return Ok((
                            serialize_error(
                                id,
                                "INTERNAL_ERROR",
                                "Desktop Agent 生成的显示拓扑不符合契约。",
                                "never",
                            )?,
                            write_budget,
                        ));
                    }
                    Ok((serialize_success(id, topology)?, write_budget))
                }
                Err(error) => Ok((
                    serialize_error(id, error.code, &error.message, error.retry)?,
                    write_budget,
                )),
            }
        }
        "target.bind" => {
            let res = leases.handle_target_bind(params, ctx, desktop);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.acquire" => {
            let res = leases.handle_lease_acquire(params, ctx, desktop, &is_draining, deadline_ms);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.renew" => {
            let res = leases.handle_lease_renew(params, ctx, desktop);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.release" => {
            let res = leases.handle_lease_release(params, ctx, desktop, deadline_ms);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.pause" => {
            let res = leases.handle_lease_pause(params, ctx, desktop, deadline_ms);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.status" => {
            let res = leases.handle_lease_status(params, ctx, desktop);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "lease.resume" => {
            let res = leases.handle_lease_resume(params, ctx, desktop, deadline_ms);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "segment.submit" => {
            let res = leases.handle_segment_submit(params, ctx, desktop, deadline_ms);
            Ok((serialize_result(id, res)?, write_budget))
        }
        "segment.status" => {
            let res = leases.handle_segment_status(params, ctx, desktop);
            Ok((serialize_result(id, res)?, write_budget))
        }
        _ => Ok((
            serialize_error(id, "METHOD_UNAVAILABLE", "此 Agent 方法尚未实现。", "never")?,
            write_budget,
        )),
    }
}

fn read_session_frame_bytes<S: SessionStream>(
    stream: &mut S,
    max_bytes: usize,
    assembly_timeout: Duration,
    stop: &StopSignal,
) -> io::Result<Option<Vec<u8>>> {
    use std::io::Read as _;

    let mut prefix = [0_u8; 4];
    {
        let mut idle_io = DeadlineIo::new(stream, None, stop);
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
    let mut framed_io = DeadlineIo::new(stream, deadline, stop);
    framed_io.read_exact(&mut prefix[1..])?;
    let size = u32::from_le_bytes(prefix) as usize;
    if !(1..=max_bytes).contains(&size) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut payload = vec![0_u8; size];
    framed_io.read_exact(&mut payload)?;
    Ok(Some(payload))
}

#[cfg(test)]
pub(crate) fn serve_session<S: SessionStream>(
    stream: &mut S,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
    frame_read_timeout: Duration,
    stop: &StopSignal,
) -> io::Result<()> {
    let leases = LeaseCoordinator::new();
    let res = serve_session_with_coordinator(
        stream,
        ctx,
        desktop,
        &leases,
        || false,
        frame_read_timeout,
        stop,
    );
    leases.on_session_closed(&ctx.session_id, desktop);
    res
}

fn serve_session_with_coordinator<S: SessionStream>(
    stream: &mut S,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
    leases: &LeaseCoordinator,
    is_draining: impl Fn() -> bool,
    frame_read_timeout: Duration,
    stop: &StopSignal,
) -> io::Result<()> {
    use std::io::Write as _;

    loop {
        let Some(raw) =
            read_session_frame_bytes(stream, MAX_CONTROL_FRAME, frame_read_timeout, stop)?
        else {
            return Ok(());
        };
        let map = parse_strict_object(&raw)?;
        match map.get("kind").and_then(Value::as_str) {
            Some("agent.ping") => {
                if map.len() != 2
                    || map.get("agentEpoch").and_then(Value::as_str)
                        != Some(ctx.agent_epoch.as_str())
                {
                    return Err(io::ErrorKind::PermissionDenied.into());
                }
                let deadline = Some(Instant::now() + WRITE_TIMEOUT);
                let mut io = DeadlineIo::new(stream, deadline, stop);
                write_frame_bounded(
                    &mut io,
                    &Pong {
                        kind: "agent.pong",
                        agent_epoch: &ctx.agent_epoch,
                    },
                    MAX_CONTROL_FRAME,
                )?;
            }
            Some("request") => {
                let (payload, budget) = dispatch_request(&map, ctx, desktop, leases, &is_draining)?;
                let deadline = Some(Instant::now() + budget);
                let mut io = DeadlineIo::new(stream, deadline, stop);
                io.write_all(&(payload.len() as u32).to_le_bytes())?;
                io.write_all(&payload)?;
            }
            _ => return Err(io::ErrorKind::InvalidData.into()),
        }
    }
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

fn handle_attached<S: SessionStream>(
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
    let _ = serve_session_with_coordinator(
        &mut stream,
        &ctx,
        shared.config.desktop.as_ref(),
        &shared.leases,
        || shared.is_draining(),
        shared.config.frame_read_timeout,
        &shared.stop,
    );
    shared
        .leases
        .on_session_closed(&ctx.session_id, shared.config.desktop.as_ref());
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
    mut initial: S,
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
    thread::Builder::new().spawn(move || {
        let _slot = initial_slot;
        let _ = serve_session_with_coordinator(
            &mut initial,
            &initial_session,
            initial_shared.config.desktop.as_ref(),
            &initial_shared.leases,
            || initial_shared.is_draining(),
            initial_shared.config.frame_read_timeout,
            &initial_shared.stop,
        );
        initial_shared.leases.on_session_closed(
            &initial_session.session_id,
            initial_shared.config.desktop.as_ref(),
        );
    })?;

    let accept_shared = Arc::clone(&shared);
    let accept_handle = thread::Builder::new().spawn(move || {
        while !accept_shared.stop.is_triggered() {
            let (stream, peer_pid) = match listener.accept(&accept_shared.stop) {
                Ok(accepted) => accepted,
                Err(_) => break,
            };
            let slot = {
                let mut state = accept_shared.state.lock().unwrap();
                if state.draining || state.total_connections >= accept_shared.config.max_clients {
                    continue;
                }
                state.total_connections += 1;
                ConnectionSlot {
                    shared: Arc::clone(&accept_shared),
                    authenticated: false,
                }
            };
            let worker_shared = Arc::clone(&accept_shared);
            let _ = thread::Builder::new()
                .spawn(move || handle_attached(stream, peer_pid, worker_shared, slot));
        }
        drop(listener);
    })?;

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
    let _ = accept_handle.join();

    let mut state = shared.state.lock().unwrap();
    while state.total_connections > 0 {
        state = shared.changed.wait(state).unwrap();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::read_frame_bytes;
    use deskweave_platform_api::{
        BackendKind, CapabilitySnapshot, DisplayTopology, FakeDesktopSession, FeatureSet,
        FeatureState, FeatureStatus, PlatformArch, PlatformError, PlatformOs, SubmissionOutcome,
        TargetDescriptor, TargetStatus, TopologyCoverage, TrajectoryVersion,
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

    struct MemStream {
        reader: Arc<MemPipe>,
        writer: Arc<MemPipe>,
    }

    impl MemStream {
        fn pair() -> (Self, Self) {
            let a_to_b = MemPipe::new();
            let b_to_a = MemPipe::new();
            (
                Self {
                    reader: Arc::clone(&b_to_a),
                    writer: Arc::clone(&a_to_b),
                },
                Self {
                    reader: a_to_b,
                    writer: b_to_a,
                },
            )
        }
    }

    impl Drop for MemStream {
        fn drop(&mut self) {
            self.writer.close();
        }
    }

    impl SessionStream for MemStream {
        fn read_chunk(
            &mut self,
            buf: &mut [u8],
            deadline: Option<Instant>,
            stop: &StopSignal,
        ) -> io::Result<usize> {
            let mut state = self.reader.state.lock().unwrap();
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
                let (guard, _) = self.reader.ready.wait_timeout(state, wait).unwrap();
                state = guard;
            }
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
            self.writer.push(buf)
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
                    lease_id: lease_a.clone(),
                    pause_id: pause_id.clone(),
                    agent_epoch: "a".repeat(64),
                    generation: gen_a,
                    checkpoint_hash,
                    resume_plan_hash: "f".repeat(64),
                    restore_target_token: Some("target-win-a".to_owned()),
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
                HeldInput::Button {
                    button: PointerButton::Left
                },
                HeldInput::Key {
                    key: "ShiftLeft".to_owned()
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

    struct ScriptedDesktop {
        inner: FakeDesktopSession,
        submit_outcomes: Mutex<VecDeque<Result<SubmissionOutcome, PlatformError>>>,
        verify_outcomes: Mutex<VecDeque<Result<TargetStatus, PlatformError>>>,
        bind_overrides: Mutex<VecDeque<Result<TargetDescriptor, PlatformError>>>,
        target_version_override: Mutex<Option<Result<Option<String>, PlatformError>>>,
        submit_count: Mutex<usize>,
        submit_notify: Mutex<Option<(usize, mpsc::Sender<usize>)>>,
        submit_gate: Mutex<Option<SubmitGate>>,
    }

    impl ScriptedDesktop {
        fn new(caps: CapabilitySnapshot, topology: Result<DisplayTopology, PlatformError>) -> Self {
            Self {
                inner: FakeDesktopSession::new(caps, topology),
                submit_outcomes: Mutex::new(VecDeque::new()),
                verify_outcomes: Mutex::new(VecDeque::new()),
                bind_overrides: Mutex::new(VecDeque::new()),
                target_version_override: Mutex::new(None),
                submit_count: Mutex::new(0),
                submit_notify: Mutex::new(None),
                submit_gate: Mutex::new(None),
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
            let scripted = self.submit_outcomes.lock().unwrap().pop_front();
            let outcome = match scripted {
                Some(res) => res,
                None => self.inner.emitter().submit_event(token, action),
            };
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
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
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

        // Hold a second key so held_inputs = [ControlLeft, ShiftLeft], pause, verify consumed grant cannot be reused.
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

        // 5. Mid-restore failure consumes grant-valid-2, records idempotent Uncertain receipt, and rolls back restored key.
        leases
            .register_confirmation(
                "grant-valid-2",
                crate::lease::RegisteredConfirmation {
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap_2.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap_2.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
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
            &vec![HeldInput::Key {
                key: "ControlLeft".to_owned()
            }]
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

        // 2. Index-0 Err(CAPABILITY_NOT_IMPLEMENTED) does not poison lease.
        let key_tap = json!({
            "id": "tap-a",
            "title": "按键",
            "action": "keyboard.down",
            "params": {"target": "main", "key": "KeyA"},
            "on": {}
        });
        let h_tap = compute_segment_payload_hash("target-a", &key_tap, None).unwrap();
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
                    lease_id: acq.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: Some("target-a".to_owned()),
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
            "params": {"target": "main", "text": "ab", "intervalMs": 20},
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
                "durationMs": 800
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
            thread::sleep(Duration::from_millis(5));
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
                    lease_id: acq_a2.lease_id.clone(),
                    pause_id: pause_snap.pause_id.clone(),
                    agent_epoch: ctx_a.agent_epoch.clone(),
                    generation: acq_a2.generation,
                    checkpoint_hash: pause_snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash_b.clone(),
                    restore_target_token: pause_snap.input_target_token.clone(),
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
                "durationMs": 800
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
            thread::sleep(Duration::from_millis(5));
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
                    lease_id: acq.lease_id.clone(),
                    pause_id: snap.pause_id.clone(),
                    agent_epoch: ctx.agent_epoch.clone(),
                    generation: acq.generation,
                    checkpoint_hash: snap.checkpoint_hash.clone(),
                    resume_plan_hash: plan_hash.clone(),
                    restore_target_token: snap.input_target_token.clone(),
                },
            )
            .unwrap();

        let (entered_restore, release_restore) = scripted.gate_submit_on(1);
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
        entered_restore.recv().unwrap();
        scripted.set_target_version_override(Some(Err(PlatformError::new(
            "SESSION_UNAVAILABLE",
            "目标版本暂不可用",
        ))));
        release_restore.send(()).unwrap();

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
}
