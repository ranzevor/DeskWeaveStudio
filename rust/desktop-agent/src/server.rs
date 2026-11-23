use deskweave_platform_api::DesktopSession;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Serialize;
use serde_json::{Map, Value};
use std::io;
use std::process;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

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

fn dispatch_request(
    map: &Map<String, Value>,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
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

pub(crate) fn serve_session<S: SessionStream>(
    stream: &mut S,
    ctx: &SessionContext,
    desktop: &dyn DesktopSession,
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
        let request_started = Instant::now();
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
                let (payload, budget) = dispatch_request(&map, ctx, desktop)?;
                let deadline = Some((request_started + budget).min(Instant::now() + WRITE_TIMEOUT));
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
    let _ = serve_session(
        &mut stream,
        &ctx,
        shared.config.desktop.as_ref(),
        shared.config.frame_read_timeout,
        &shared.stop,
    );
}

pub(crate) fn run<S: SessionStream + 'static, L: SessionListener + 'static>(
    mut initial: S,
    initial_session: SessionContext,
    mut listener: L,
    config: ServerConfig,
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
    });

    let initial_shared = Arc::clone(&shared);
    let initial_slot = ConnectionSlot {
        shared: Arc::clone(&shared),
        authenticated: true,
    };
    thread::Builder::new().spawn(move || {
        let _slot = initial_slot;
        let _ = serve_session(
            &mut initial,
            &initial_session,
            initial_shared.config.desktop.as_ref(),
            initial_shared.config.frame_read_timeout,
            &initial_shared.stop,
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
        FeatureState, FeatureStatus, PlatformArch, PlatformError, PlatformOs, TopologyCoverage,
        TrajectoryVersion,
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
                "method": "lease.acquire",
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
}
