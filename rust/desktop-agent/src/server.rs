use ed25519_dalek::{Signature, VerifyingKey};
use std::io;
use std::process;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::native::{DeadlineIo, SessionListener, SessionStream, StopSignal};
use crate::protocol::{
    AttachChallenge, AttachHello, AttachReady, AttachRegistration, Pong, Probe, VERSION,
    attach_ready_proof, constant_time_eq, is_hex, parse_object, read_frame, sha256_hex,
    valid_workspace_id, write_frame,
};

pub(crate) const MAX_CLIENTS: usize = 32;
pub(crate) const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

pub(crate) struct ServerConfig {
    pub(crate) agent_epoch: String,
    pub(crate) scope: String,
    pub(crate) owner_identity: [String; 2],
    pub(crate) verification_key: VerifyingKey,
    pub(crate) idle_grace: Duration,
    pub(crate) handshake_timeout: Duration,
    pub(crate) max_clients: usize,
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

pub(crate) fn serve_session<S: SessionStream>(
    stream: &mut S,
    agent_epoch: &str,
    stop: &StopSignal,
) -> io::Result<()> {
    loop {
        let request = {
            let mut io = DeadlineIo::new(stream, None, stop);
            read_frame::<Probe>(&mut io)?
        };
        let Some(request) = request else {
            return Ok(());
        };
        if request.kind != "agent.ping" || request.agent_epoch != agent_epoch {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        let deadline = Some(Instant::now() + WRITE_TIMEOUT);
        let mut io = DeadlineIo::new(stream, deadline, stop);
        write_frame(
            &mut io,
            &Pong {
                kind: "agent.pong",
                agent_epoch,
            },
        )?;
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
    );
    {
        let mut io = DeadlineIo::new(&mut stream, deadline, &shared.stop);
        let ready = AttachReady {
            kind: "agent.attach.ready",
            agent_epoch: &shared.config.agent_epoch,
            workspace_id: &registration.workspace_id,
            proof,
        };
        if write_frame(&mut io, &ready).is_err() {
            return;
        }
    }
    let _ = serve_session(&mut stream, &shared.config.agent_epoch, &shared.stop);
}

pub(crate) fn run<S: SessionStream + 'static, L: SessionListener + 'static>(
    mut initial: S,
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
            &initial_shared.config.agent_epoch,
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
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::{Value, json};
    use std::collections::VecDeque;
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
            self.reader.close();
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
                    for byte in buf.iter_mut().take(count) {
                        *byte = state.buf.pop_front().unwrap();
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
                        (limit - now).min(Duration::from_millis(10))
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

    fn test_config(
        signing_key: &SigningKey,
        idle_grace: Duration,
        max_clients: usize,
    ) -> ServerConfig {
        ServerConfig {
            agent_epoch: "a".repeat(64),
            scope: "b".repeat(64),
            owner_identity: ["1".into(), "2".into()],
            verification_key: signing_key.verifying_key(),
            idle_grace,
            handshake_timeout: Duration::from_millis(200),
            max_clients,
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
        let expected_proof = attach_ready_proof(
            &proof_key,
            engine_pid,
            process::id(),
            workspace_id,
            &"a".repeat(64),
            &nonce,
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

    #[test]
    fn shared_server_serves_multiple_engines_and_exits_after_idle_grace() {
        let key = SigningKey::from_bytes(&[0x5a; 32]);
        let (server_init, mut client_a) = MemStream::pair();
        let (tx, rx) = mpsc::channel();
        let stop = StopSignal::new().unwrap();
        let client_stop = StopSignal::new().unwrap();
        let config = test_config(&key, Duration::from_millis(120), 4);
        let server_thread =
            thread::spawn(move || run(server_init, MemListener { rx }, config, stop));

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
        thread::sleep(Duration::from_millis(180));
        assert_eq!(
            client_ping(&mut client_b, &client_stop).unwrap()["kind"],
            "agent.pong"
        );

        // Closing client B triggers idle grace; attaching client C within idle grace cancels exit.
        drop(client_b);
        thread::sleep(Duration::from_millis(40));
        let (server_c, mut client_c) = MemStream::pair();
        tx.send((server_c, 3003)).unwrap();
        let ready_c =
            client_attach(&mut client_c, &key, 3003, "ws-3", ["1", "2"], &client_stop).unwrap();
        assert_eq!(ready_c["workspaceId"], "ws-3");
        thread::sleep(Duration::from_millis(150));
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
        let config = test_config(&key, Duration::from_millis(80), 2);
        let server_thread =
            thread::spawn(move || run(server_init, MemListener { rx }, config, stop));

        // Mismatched owner identity must be rejected.
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

        // Stalled unauthenticated connection fills the 2nd slot (max_clients=2) and never replies,
        // yet closing authenticated client A must still allow idle exit without waiting forever.
        let (server_stalled, _client_stalled) = MemStream::pair();
        tx.send((server_stalled, 5005)).unwrap();
        thread::sleep(Duration::from_millis(20));

        // 3rd connection exceeds max_clients=2 and is immediately closed.
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
}
