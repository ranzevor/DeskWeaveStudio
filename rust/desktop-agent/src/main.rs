mod native;
mod protocol;
mod server;

use ed25519_dalek::{Signature, VerifyingKey};
use hmac::Mac;
use std::io::{self, Read, Write};
use std::process::{self, ExitCode};
use std::time::{Duration, Instant};

use native::{DeadlineIo, Listener, StopSignal};
use protocol::{
    Bootstrap, Challenge, Hello, Ready, Registration, is_hex, parse_object, read_frame,
    valid_workspace_id, write_frame,
};
use server::{HANDSHAKE_TIMEOUT, MAX_CLIENTS, ServerConfig};

const BOOTSTRAP_TIMEOUT: Duration = Duration::from_secs(15);

#[cfg(all(feature = "test-authority", not(debug_assertions)))]
compile_error!("test-authority requires a debug test build");

fn verification_key() -> io::Result<VerifyingKey> {
    #[cfg(feature = "test-authority")]
    {
        Ok(ed25519_dalek::SigningKey::from_bytes(&[0x5a; 32]).verifying_key())
    }
    #[cfg(not(feature = "test-authority"))]
    {
        let encoded = option_env!("DESKWEAVE_AGENT_AUTHORITY_KEY")
            .filter(|value| is_hex(value))
            .ok_or(io::ErrorKind::PermissionDenied)?;
        let mut bytes = [0; 32];
        for (index, value) in bytes.iter_mut().enumerate() {
            *value = u8::from_str_radix(&encoded[index * 2..index * 2 + 2], 16)
                .map_err(|_| io::ErrorKind::InvalidData)?;
        }
        VerifyingKey::from_bytes(&bytes).map_err(|_| io::ErrorKind::InvalidData.into())
    }
}

fn authenticate(
    channel: &mut (impl Read + Write),
    bootstrap: &Bootstrap,
    owner: &[String; 2],
    verification_key: &VerifyingKey,
) -> io::Result<Registration> {
    let mut nonce = [0; 32];
    getrandom::fill(&mut nonce).map_err(|_| io::ErrorKind::Other)?;
    write_frame(
        channel,
        &Challenge {
            kind: "agent.challenge",
            nonce,
        },
    )?;
    let hello: Hello = read_frame(channel)?.ok_or(io::ErrorKind::UnexpectedEof)?;
    if hello.kind != "agent.hello" {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    bootstrap
        .proof(b"engine", None)
        .verify_slice(&hello.proof)
        .map_err(|_| io::ErrorKind::PermissionDenied)?;
    let signature =
        Signature::from_slice(&hello.signature).map_err(|_| io::ErrorKind::PermissionDenied)?;
    verification_key
        .verify_strict(hello.registration.as_bytes(), &signature)
        .map_err(|_| io::ErrorKind::PermissionDenied)?;
    let registration: Registration = parse_object(hello.registration.as_bytes())?;
    if registration.kind != "deskweave/agent-registration/v1"
        || registration.agent_pid != process::id()
        || registration.engine_pid != bootstrap.engine_pid
        || registration.agent_epoch != bootstrap.agent_epoch
        || !valid_workspace_id(&registration.workspace_id)
        || registration.owner != *owner
        || !is_hex(&registration.scope)
        || registration.nonce != nonce
    {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(registration)
}

fn answer(
    channel: &mut impl Write,
    bootstrap: &Bootstrap,
    address: Option<&str>,
) -> io::Result<()> {
    let (kind, role) = match address {
        Some(_) => ("agent.ready", b"agent".as_slice()),
        None => ("agent.busy", b"busy".as_slice()),
    };
    write_frame(
        channel,
        &Ready {
            kind,
            agent_epoch: &bootstrap.agent_epoch,
            address,
            proof: bootstrap
                .proof(role, address)
                .finalize()
                .into_bytes()
                .into(),
        },
    )
}

fn busy(channel: &mut (impl Read + Write), bootstrap: &Bootstrap) -> io::Result<()> {
    answer(channel, bootstrap, None)?;
    if channel.read(&mut [0])? == 0 {
        Ok(())
    } else {
        Err(io::ErrorKind::PermissionDenied.into())
    }
}

fn acquire_scope<T>(
    scope: &str,
    claim: impl FnOnce(&str) -> io::Result<T>,
    channel: &mut (impl Read + Write),
    bootstrap: &Bootstrap,
) -> io::Result<Option<T>> {
    match claim(scope) {
        Ok(guard) => Ok(Some(guard)),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::AlreadyExists | io::ErrorKind::AddrInUse
            ) =>
        {
            busy(channel, bootstrap)?;
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn run() -> io::Result<()> {
    let verification_key = verification_key()?;
    let bootstrap: Bootstrap =
        read_frame(&mut io::stdin().lock())?.ok_or(io::ErrorKind::UnexpectedEof)?;
    bootstrap.check()?;
    let owner = native::owner_file()?;
    let owner_identity = native::owner_identity(&owner)?;
    let stop = StopSignal::new()?;
    let mut channel = native::connect(&bootstrap.address, bootstrap.engine_pid)?;
    let deadline = Some(Instant::now() + BOOTSTRAP_TIMEOUT);
    let registration = {
        let mut io = DeadlineIo::new(&mut channel, deadline, &stop);
        authenticate(&mut io, &bootstrap, &owner_identity, &verification_key)?
    };
    let _scope = {
        let mut io = DeadlineIo::new(&mut channel, deadline, &stop);
        match acquire_scope(
            &registration.scope,
            native::claim_scope,
            &mut io,
            &bootstrap,
        )? {
            Some(scope) => scope,
            None => return Ok(()),
        }
    };
    if let Err(error) = owner.try_lock().map_err(io::Error::from) {
        if error.kind() == io::ErrorKind::WouldBlock {
            let mut io = DeadlineIo::new(&mut channel, deadline, &stop);
            return busy(&mut io, &bootstrap);
        }
        return Err(error);
    }
    let listener = Listener::bind()?;
    {
        let mut io = DeadlineIo::new(&mut channel, deadline, &stop);
        answer(&mut io, &bootstrap, Some(listener.address()))?;
    }
    let config = ServerConfig {
        agent_epoch: bootstrap.agent_epoch.clone(),
        scope: registration.scope,
        owner_identity,
        verification_key,
        idle_grace: bootstrap.idle_grace(),
        handshake_timeout: HANDSHAKE_TIMEOUT,
        max_clients: MAX_CLIENTS,
    };
    let result = server::run(channel, listener, config, stop);
    result?;
    owner.unlock()
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::FAILURE,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native::SessionStream;
    use crate::protocol::{DEFAULT_IDLE_GRACE_MS, MAX_FRAME, Probe, VERSION};
    use serde_json::json;
    use std::io::Cursor;

    struct CursorStream {
        cursor: Cursor<Vec<u8>>,
    }

    impl SessionStream for CursorStream {
        fn read_chunk(
            &mut self,
            buf: &mut [u8],
            _deadline: Option<Instant>,
            _stop: &StopSignal,
        ) -> io::Result<usize> {
            self.cursor.read(buf)
        }

        fn write_chunk(
            &mut self,
            buf: &[u8],
            _deadline: Option<Instant>,
            _stop: &StopSignal,
        ) -> io::Result<usize> {
            self.cursor.write(buf)
        }
    }

    fn frame(payload: &[u8]) -> Vec<u8> {
        let mut data = (payload.len() as u32).to_le_bytes().to_vec();
        data.extend(payload);
        data
    }

    #[test]
    fn frames_reject_truncation_and_out_of_budget_lengths() {
        assert!(read_frame::<Probe>(&mut &[][..]).unwrap().is_none());
        for mut partial in [&[20, 0][..], &[5, 0, 0, 0, b'{'][..]] {
            assert_eq!(
                read_frame::<Probe>(&mut partial).err().unwrap().kind(),
                io::ErrorKind::UnexpectedEof
            );
        }
        for size in [0, (MAX_FRAME + 1) as u32, u32::MAX] {
            assert_eq!(
                read_frame::<Probe>(&mut size.to_le_bytes().as_slice())
                    .err()
                    .unwrap()
                    .kind(),
                io::ErrorKind::InvalidData
            );
        }
    }

    #[test]
    fn duplicate_fields_and_old_epochs_are_rejected() {
        let duplicate = frame(br#"{"kind":"agent.ping","kind":"agent.ping","agentEpoch":"old"}"#);
        assert!(read_frame::<Probe>(&mut duplicate.as_slice()).is_err());
        let stale = frame(br#"{"kind":"agent.ping","agentEpoch":"old"}"#);
        let mut channel = CursorStream {
            cursor: Cursor::new(stale),
        };
        let stop = StopSignal::new().unwrap();
        assert_eq!(
            server::serve_session(&mut channel, "current", &stop)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn frames_require_json_objects() {
        let array = frame(br#"["agent.ping","epoch"]"#);
        assert!(read_frame::<Probe>(&mut array.as_slice()).is_err());
        let value = json!(["agent.hello", vec![0; 32], "{}", vec![0; 64]]);
        let array = frame(&serde_json::to_vec(&value).unwrap());
        assert!(read_frame::<Hello>(&mut array.as_slice()).is_err());
        let value = json!([1, 123, "endpoint", "f".repeat(64), vec![0; 32]]);
        let array = frame(&serde_json::to_vec(&value).unwrap());
        assert!(read_frame::<Bootstrap>(&mut array.as_slice()).is_err());
    }

    fn test_bootstrap() -> Bootstrap {
        Bootstrap {
            version: VERSION,
            engine_pid: 1,
            address: String::from("endpoint"),
            agent_epoch: "a".repeat(64),
            secret: [0x11; 32],
            idle_grace_ms: DEFAULT_IDLE_GRACE_MS,
        }
    }

    #[test]
    fn scope_is_exclusive_until_native_handle_closes() {
        let scope = format!("unit-{}", process::id());
        let first = native::claim_scope(&scope).unwrap();
        let error = native::claim_scope(&scope).err().unwrap();
        assert!(matches!(
            error.kind(),
            io::ErrorKind::AlreadyExists | io::ErrorKind::AddrInUse
        ));
        drop(first);
        assert!(native::claim_scope(&scope).is_ok());
    }

    #[test]
    fn scope_creation_failure_is_not_reported_as_busy() {
        let bootstrap = test_bootstrap();
        let mut channel = Cursor::new(Vec::new());
        let error = acquire_scope(
            "scope-a",
            |_| Err::<(), _>(io::Error::other("CFMessagePortCreateLocal failed")),
            &mut channel,
            &bootstrap,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Other);
        assert!(channel.get_ref().is_empty());
    }

    #[test]
    fn occupied_scope_reports_busy_frame() {
        let bootstrap = test_bootstrap();
        for kind in [io::ErrorKind::AlreadyExists, io::ErrorKind::AddrInUse] {
            let mut channel = Cursor::new(Vec::new());
            let result = acquire_scope(
                "scope-a",
                |_| Err::<(), _>(kind.into()),
                &mut channel,
                &bootstrap,
            )
            .unwrap();
            assert!(result.is_none());
            let mut written = Cursor::new(channel.into_inner());
            let frame: serde_json::Value = read_frame(&mut written).unwrap().unwrap();
            assert_eq!(frame["kind"], "agent.busy");
            assert!(frame.get("address").is_none());
        }
    }

    #[cfg(not(feature = "test-authority"))]
    #[test]
    fn unconfigured_build_has_no_authority() {
        if option_env!("DESKWEAVE_AGENT_AUTHORITY_KEY").is_none() {
            assert!(verification_key().is_err());
        }
    }
}
