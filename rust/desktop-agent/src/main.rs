mod native;

use ed25519_dalek::{Signature, VerifyingKey};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::Sha256;
use std::io::{self, Read, Write};
use std::process::{self, ExitCode};

const VERSION: u8 = 1;
const MAX_FRAME: usize = 4096;

#[cfg(all(feature = "test-authority", not(debug_assertions)))]
compile_error!("test-authority requires a debug test build");

fn is_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

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

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Bootstrap {
    version: u8,
    engine_pid: u32,
    address: String,
    agent_epoch: String,
    secret: [u8; 32],
}

impl Bootstrap {
    fn check(&self) -> io::Result<()> {
        if self.version != VERSION || self.engine_pid == 0 || !is_hex(&self.agent_epoch) {
            return Err(io::ErrorKind::InvalidData.into());
        }
        Ok(())
    }

    fn proof(&self, role: &[u8]) -> Hmac<Sha256> {
        let mut proof = Hmac::<Sha256>::new_from_slice(&self.secret).expect("fixed key size");
        proof.update(b"deskweave/desktop-agent/v1\0");
        proof.update(role);
        proof.update(b"\0");
        proof.update(&self.engine_pid.to_le_bytes());
        proof.update(&process::id().to_le_bytes());
        proof.update(self.agent_epoch.as_bytes());
        proof
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Hello {
    kind: String,
    proof: [u8; 32],
    registration: String,
    signature: Vec<u8>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Registration {
    kind: String,
    engine_pid: u32,
    agent_pid: u32,
    agent_epoch: String,
    owner: [String; 2],
    scope: String,
    nonce: [u8; 32],
}

#[derive(Serialize)]
struct Challenge {
    kind: &'static str,
    nonce: [u8; 32],
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Ready<'epoch> {
    kind: &'static str,
    agent_epoch: &'epoch str,
    proof: [u8; 32],
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Probe {
    kind: String,
    agent_epoch: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Pong<'epoch> {
    kind: &'static str,
    agent_epoch: &'epoch str,
}

fn read_frame<Frame: DeserializeOwned>(stream: &mut impl Read) -> io::Result<Option<Frame>> {
    let mut prefix = [0; 4];
    loop {
        match stream.read(&mut prefix[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    stream.read_exact(&mut prefix[1..])?;
    let size = u32::from_le_bytes(prefix) as usize;
    if !(1..=MAX_FRAME).contains(&size) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut payload = vec![0; size];
    stream.read_exact(&mut payload)?;
    parse_object(&payload).map(Some)
}

fn parse_object<Frame: DeserializeOwned>(payload: &[u8]) -> io::Result<Frame> {
    if payload.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        return Err(io::ErrorKind::InvalidData.into());
    }
    serde_json::from_slice(payload).map_err(|_| io::ErrorKind::InvalidData.into())
}

fn write_frame(stream: &mut impl Write, value: &impl Serialize) -> io::Result<()> {
    let payload = serde_json::to_vec(value)?;
    if !(1..=MAX_FRAME).contains(&payload.len()) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    stream.write_all(&(payload.len() as u32).to_le_bytes())?;
    stream.write_all(&payload)
}

fn authenticate(
    channel: &mut (impl Read + Write),
    bootstrap: &Bootstrap,
    owner: [String; 2],
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
        .proof(b"engine")
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
        || registration.owner != owner
        || !is_hex(&registration.scope)
        || registration.nonce != nonce
    {
        return Err(io::ErrorKind::PermissionDenied.into());
    }
    Ok(registration)
}

fn probe(channel: &mut (impl Read + Write), epoch: &str) -> io::Result<()> {
    while let Some(request) = read_frame::<Probe>(channel)? {
        if request.kind != "agent.ping" || request.agent_epoch != epoch {
            return Err(io::ErrorKind::PermissionDenied.into());
        }
        write_frame(
            channel,
            &Pong {
                kind: "agent.pong",
                agent_epoch: epoch,
            },
        )?;
    }
    Ok(())
}

fn answer(channel: &mut impl Write, bootstrap: &Bootstrap, ready: bool) -> io::Result<()> {
    let (kind, role) = if ready {
        ("agent.ready", b"agent".as_slice())
    } else {
        ("agent.busy", b"busy".as_slice())
    };
    write_frame(
        channel,
        &Ready {
            kind,
            agent_epoch: &bootstrap.agent_epoch,
            proof: bootstrap.proof(role).finalize().into_bytes().into(),
        },
    )
}

fn busy(channel: &mut (impl Read + Write), bootstrap: &Bootstrap) -> io::Result<()> {
    answer(channel, bootstrap, false)?;
    if channel.read(&mut [0])? == 0 {
        Ok(())
    } else {
        Err(io::ErrorKind::PermissionDenied.into())
    }
}

fn run() -> io::Result<()> {
    let verification_key = verification_key()?;
    let bootstrap: Bootstrap =
        read_frame(&mut io::stdin().lock())?.ok_or(io::ErrorKind::UnexpectedEof)?;
    bootstrap.check()?;
    let owner = native::owner_file()?;
    let mut channel = native::connect(&bootstrap.address, bootstrap.engine_pid)?;
    let registration = authenticate(
        &mut channel,
        &bootstrap,
        native::owner_identity(&owner)?,
        &verification_key,
    )?;
    #[cfg(any(target_os = "linux", windows))]
    let _scope = match native::claim_scope(&registration.scope) {
        Ok(scope) => scope,
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::AlreadyExists | io::ErrorKind::AddrInUse
            ) =>
        {
            return busy(&mut channel, &bootstrap);
        }
        Err(error) => return Err(error),
    };
    #[cfg(target_os = "macos")]
    drop(registration);
    if let Err(error) = owner.try_lock().map_err(io::Error::from) {
        if error.kind() == io::ErrorKind::WouldBlock {
            return busy(&mut channel, &bootstrap);
        }
        return Err(error);
    }
    answer(&mut channel, &bootstrap, true)?;
    let result = probe(&mut channel, &bootstrap.agent_epoch);
    drop(channel);
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
    use serde_json::json;
    use std::io::Cursor;

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
        let mut channel = Cursor::new(stale);
        assert_eq!(
            probe(&mut channel, "current").unwrap_err().kind(),
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

    #[cfg(any(target_os = "linux", windows))]
    #[test]
    fn scope_is_exclusive_until_native_handle_closes() {
        let scope = format!("unit-{}", process::id());
        let first = native::claim_scope(&scope).unwrap();
        assert!(native::claim_scope(&scope).is_err());
        drop(first);
        assert!(native::claim_scope(&scope).is_ok());
    }

    #[cfg(not(feature = "test-authority"))]
    #[test]
    fn unconfigured_build_has_no_authority() {
        if option_env!("DESKWEAVE_AGENT_AUTHORITY_KEY").is_none() {
            assert!(verification_key().is_err());
        }
    }
}
