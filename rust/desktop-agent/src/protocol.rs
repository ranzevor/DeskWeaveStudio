use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::io::{self, Read, Write};
use std::process;
use std::time::Duration;

pub(crate) const VERSION: u8 = 1;
pub(crate) const MAX_FRAME: usize = 4096;
pub(crate) const MIN_IDLE_GRACE_MS: u64 = 50;
pub(crate) const MAX_IDLE_GRACE_MS: u64 = 300_000;
pub(crate) const DEFAULT_IDLE_GRACE_MS: u64 = 2_000;

fn default_idle_grace_ms() -> u64 {
    DEFAULT_IDLE_GRACE_MS
}

pub(crate) fn is_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn valid_workspace_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && !value.bytes().any(|byte| byte == 0)
}

pub(crate) fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(&mut encoded, "{byte:02x}");
    }
    encoded
}

pub(crate) fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for (&a, &b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Bootstrap {
    pub(crate) version: u8,
    pub(crate) engine_pid: u32,
    pub(crate) address: String,
    pub(crate) agent_epoch: String,
    pub(crate) secret: [u8; 32],
    #[serde(default = "default_idle_grace_ms")]
    pub(crate) idle_grace_ms: u64,
}

impl Bootstrap {
    pub(crate) fn check(&self) -> io::Result<()> {
        if self.version != VERSION
            || self.engine_pid == 0
            || !is_hex(&self.agent_epoch)
            || !(MIN_IDLE_GRACE_MS..=MAX_IDLE_GRACE_MS).contains(&self.idle_grace_ms)
        {
            return Err(io::ErrorKind::InvalidData.into());
        }
        Ok(())
    }

    pub(crate) fn idle_grace(&self) -> Duration {
        Duration::from_millis(self.idle_grace_ms)
    }

    pub(crate) fn proof(&self, role: &[u8], address: Option<&str>) -> Hmac<Sha256> {
        let mut proof = Hmac::<Sha256>::new_from_slice(&self.secret).expect("fixed key size");
        proof.update(b"deskweave/desktop-agent/v1\0");
        proof.update(role);
        proof.update(b"\0");
        proof.update(&self.engine_pid.to_le_bytes());
        proof.update(&process::id().to_le_bytes());
        proof.update(self.agent_epoch.as_bytes());
        if let Some(address) = address {
            proof.update(b"\0");
            proof.update(address.as_bytes());
        }
        proof
    }
}

pub(crate) fn attach_ready_proof(
    proof_key: &[u8; 32],
    engine_pid: u32,
    agent_pid: u32,
    workspace_id: &str,
    agent_epoch: &str,
    nonce: &[u8; 32],
) -> [u8; 32] {
    let mut proof = Hmac::<Sha256>::new_from_slice(proof_key).expect("fixed key size");
    proof.update(b"deskweave/desktop-agent/attach-ready/v1\0");
    proof.update(&engine_pid.to_le_bytes());
    proof.update(&agent_pid.to_le_bytes());
    proof.update(workspace_id.as_bytes());
    proof.update(b"\0");
    proof.update(agent_epoch.as_bytes());
    proof.update(b"\0");
    proof.update(nonce);
    proof.finalize().into_bytes().into()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Hello {
    pub(crate) kind: String,
    pub(crate) proof: [u8; 32],
    pub(crate) registration: String,
    pub(crate) signature: Vec<u8>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Registration {
    pub(crate) kind: String,
    pub(crate) engine_pid: u32,
    pub(crate) agent_pid: u32,
    pub(crate) agent_epoch: String,
    pub(crate) workspace_id: String,
    pub(crate) owner: [String; 2],
    pub(crate) scope: String,
    pub(crate) nonce: [u8; 32],
}

#[derive(Serialize)]
pub(crate) struct Challenge {
    pub(crate) kind: &'static str,
    pub(crate) nonce: [u8; 32],
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Ready<'a> {
    pub(crate) kind: &'static str,
    pub(crate) agent_epoch: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) address: Option<&'a str>,
    pub(crate) proof: [u8; 32],
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct AttachChallenge<'a> {
    pub(crate) kind: &'static str,
    pub(crate) version: u8,
    pub(crate) agent_epoch: &'a str,
    pub(crate) scope: &'a str,
    pub(crate) nonce: [u8; 32],
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct AttachHello {
    pub(crate) kind: String,
    pub(crate) version: u8,
    pub(crate) proof_key: [u8; 32],
    pub(crate) registration: String,
    pub(crate) signature: Vec<u8>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct AttachRegistration {
    pub(crate) kind: String,
    pub(crate) engine_pid: u32,
    pub(crate) agent_pid: u32,
    pub(crate) agent_epoch: String,
    pub(crate) workspace_id: String,
    pub(crate) owner: [String; 2],
    pub(crate) scope: String,
    pub(crate) nonce: [u8; 32],
    pub(crate) proof_key_hash: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct AttachReady<'a> {
    pub(crate) kind: &'static str,
    pub(crate) agent_epoch: &'a str,
    pub(crate) workspace_id: &'a str,
    pub(crate) proof: [u8; 32],
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Probe {
    pub(crate) kind: String,
    pub(crate) agent_epoch: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Pong<'epoch> {
    pub(crate) kind: &'static str,
    pub(crate) agent_epoch: &'epoch str,
}

pub(crate) fn read_frame<Frame: DeserializeOwned>(
    stream: &mut impl Read,
) -> io::Result<Option<Frame>> {
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

pub(crate) fn parse_object<Frame: DeserializeOwned>(payload: &[u8]) -> io::Result<Frame> {
    if payload.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        return Err(io::ErrorKind::InvalidData.into());
    }
    serde_json::from_slice(payload).map_err(|_| io::ErrorKind::InvalidData.into())
}

pub(crate) fn write_frame(stream: &mut impl Write, value: &impl Serialize) -> io::Result<()> {
    let payload = serde_json::to_vec(value)?;
    if !(1..=MAX_FRAME).contains(&payload.len()) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    stream.write_all(&(payload.len() as u32).to_le_bytes())?;
    stream.write_all(&payload)
}
