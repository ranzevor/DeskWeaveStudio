use hmac::{Hmac, Mac};
use serde::de::{self, DeserializeSeed, Deserializer, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::fmt;
use std::io::{self, Read, Write};
use std::process;
use std::time::Duration;

pub(crate) const VERSION: u8 = 1;
pub(crate) const MAX_FRAME: usize = 4096;
pub(crate) const MAX_CONTROL_FRAME: usize = 1_048_576;
pub(crate) const MAX_JSON_DEPTH: usize = 64;
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
    let chars = value.chars().count();
    (1..=128).contains(&chars) && !value.bytes().any(|byte| byte == 0)
}

pub(crate) fn valid_envelope_id(value: &str) -> bool {
    let chars = value.chars().count();
    (1..=128).contains(&chars) && !value.bytes().any(|byte| byte == 0)
}

pub(crate) fn valid_method_name(value: &str) -> bool {
    if value.is_empty() || value.len() > 128 {
        return false;
    }
    let mut segments = value.split('.');
    let mut count = 0_usize;
    for segment in &mut segments {
        if segment.is_empty() || !segment.bytes().all(|b| b.is_ascii_lowercase()) {
            return false;
        }
        count += 1;
    }
    count >= 2
}

pub(crate) fn looks_like_protocol_version(value: &str) -> bool {
    let Some((major, minor)) = value.split_once('.') else {
        return false;
    };
    (1..=4).contains(&major.len())
        && (1..=4).contains(&minor.len())
        && major.bytes().all(|b| b.is_ascii_digit())
        && minor.bytes().all(|b| b.is_ascii_digit())
}

pub(crate) fn generate_session_id() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| io::Error::other("random unavailable"))?;
    let mut encoded = String::with_capacity(32);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(&mut encoded, "{byte:02x}");
    }
    Ok(encoded)
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

    pub(crate) fn proof(
        &self,
        role: &[u8],
        address: Option<&str>,
        session: Option<&str>,
    ) -> Hmac<Sha256> {
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
        if let Some(session) = session {
            proof.update(b"\0");
            proof.update(session.as_bytes());
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
    session: &str,
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
    proof.update(b"\0");
    proof.update(session.as_bytes());
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) session: Option<&'a str>,
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
    pub(crate) session: &'a str,
    pub(crate) proof: [u8; 32],
}

#[cfg(test)]
#[allow(dead_code)]
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

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SuccessResponse<'a, T: Serialize> {
    pub(crate) protocol_version: &'static str,
    pub(crate) kind: &'static str,
    pub(crate) reply_to: &'a str,
    pub(crate) ok: bool,
    pub(crate) result: T,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ErrorBody<'a> {
    pub(crate) code: &'a str,
    pub(crate) message: &'a str,
    pub(crate) retry: &'a str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ErrorResponse<'a> {
    pub(crate) protocol_version: &'static str,
    pub(crate) kind: &'static str,
    pub(crate) reply_to: &'a str,
    pub(crate) ok: bool,
    pub(crate) error: ErrorBody<'a>,
}

struct StrictValueSeed {
    depth: usize,
}

impl<'de> DeserializeSeed<'de> for StrictValueSeed {
    type Value = Value;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
        deserializer.deserialize_any(StrictValueVisitor { depth: self.depth })
    }
}

struct StrictValueVisitor {
    depth: usize,
}

impl<'de> Visitor<'de> for StrictValueVisitor {
    type Value = Value;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a strict JSON value")
    }

    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }

    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }

    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Value, E> {
        Ok(Value::Number(v.into()))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        serde_json::Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite JSON number"))
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_owned()))
    }

    fn visit_string<E: de::Error>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }

    fn visit_unit<E: de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_none<E: de::Error>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let next_depth = self.depth + 1;
        if next_depth > MAX_JSON_DEPTH {
            return Err(de::Error::custom("JSON nesting depth exceeded"));
        }
        let mut items = Vec::new();
        while let Some(item) = seq.next_element_seed(StrictValueSeed { depth: next_depth })? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        let next_depth = self.depth + 1;
        if next_depth > MAX_JSON_DEPTH {
            return Err(de::Error::custom("JSON nesting depth exceeded"));
        }
        let mut out = Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if out.contains_key(&key) {
                return Err(de::Error::custom("duplicate JSON object key"));
            }
            let value = map.next_value_seed(StrictValueSeed { depth: next_depth })?;
            out.insert(key, value);
        }
        Ok(Value::Object(out))
    }
}

pub(crate) fn parse_strict_object(payload: &[u8]) -> io::Result<Map<String, Value>> {
    if payload.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut deserializer = serde_json::Deserializer::from_slice(payload);
    let value = StrictValueSeed { depth: 0 }
        .deserialize(&mut deserializer)
        .map_err(|_| io::ErrorKind::InvalidData)?;
    deserializer.end().map_err(|_| io::ErrorKind::InvalidData)?;
    match value {
        Value::Object(map) => Ok(map),
        _ => Err(io::ErrorKind::InvalidData.into()),
    }
}

pub(crate) fn read_frame_bytes(
    stream: &mut impl Read,
    max_bytes: usize,
) -> io::Result<Option<Vec<u8>>> {
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
    if !(1..=max_bytes).contains(&size) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    let mut payload = vec![0; size];
    stream.read_exact(&mut payload)?;
    Ok(Some(payload))
}

pub(crate) fn read_frame<Frame: DeserializeOwned>(
    stream: &mut impl Read,
) -> io::Result<Option<Frame>> {
    let Some(payload) = read_frame_bytes(stream, MAX_FRAME)? else {
        return Ok(None);
    };
    parse_object(&payload).map(Some)
}

pub(crate) fn parse_object<Frame: DeserializeOwned>(payload: &[u8]) -> io::Result<Frame> {
    if payload.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        return Err(io::ErrorKind::InvalidData.into());
    }
    serde_json::from_slice(payload).map_err(|_| io::ErrorKind::InvalidData.into())
}

pub(crate) fn write_frame_bounded(
    stream: &mut impl Write,
    value: &impl Serialize,
    max_bytes: usize,
) -> io::Result<()> {
    let payload = serde_json::to_vec(value)?;
    if !(1..=max_bytes).contains(&payload.len()) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    stream.write_all(&(payload.len() as u32).to_le_bytes())?;
    stream.write_all(&payload)
}

pub(crate) fn write_frame(stream: &mut impl Write, value: &impl Serialize) -> io::Result<()> {
    write_frame_bounded(stream, value, MAX_FRAME)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_id_counts_unicode_code_points_and_rejects_nul() {
        assert!(!valid_workspace_id(""));
        assert!(valid_workspace_id("workspace"));
        assert!(valid_workspace_id(&"工".repeat(128)));
        assert!(!valid_workspace_id(&"工".repeat(129)));
        assert!(!valid_workspace_id("ws\0bad"));
    }

    #[test]
    fn strict_object_parser_rejects_duplicates_and_excessive_depth() {
        assert!(parse_strict_object(br#"{"a":1,"b":{"c":2}}"#).is_ok());
        assert!(parse_strict_object(br#"{"a":1,"a":2}"#).is_err());
        assert!(parse_strict_object(br#"{"a":{"x":1,"x":2}}"#).is_err());
        assert!(parse_strict_object(br#"{"a":1} trailing"#).is_err());
        assert!(parse_strict_object(br#"[1,2,3]"#).is_err());

        let deep_ok = format!(
            "{}1{}",
            "{\"a\":".repeat(MAX_JSON_DEPTH),
            "}".repeat(MAX_JSON_DEPTH)
        );
        assert!(parse_strict_object(deep_ok.as_bytes()).is_ok());
        let too_deep = format!(
            "{}1{}",
            "{\"a\":".repeat(MAX_JSON_DEPTH + 1),
            "}".repeat(MAX_JSON_DEPTH + 1)
        );
        assert!(parse_strict_object(too_deep.as_bytes()).is_err());
    }

    #[test]
    fn method_name_validation_matches_envelope_pattern() {
        assert!(valid_method_name("desktop.capabilities"));
        assert!(valid_method_name("a.b.c"));
        assert!(!valid_method_name("desktop"));
        assert!(!valid_method_name("desktop.Capabilities"));
        assert!(!valid_method_name("desktop..capabilities"));
        assert!(!valid_method_name(""));
    }
}
