use std::fmt::Write as _;

use serde::Serialize;
use serde_json::Value;

use crate::capabilities::MAX_SAFE_INT;
use crate::error::PlatformError;

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// FIPS 180-4 SHA-256 digest calculation (32-byte raw digest).
pub fn sha256_bytes(data: &[u8]) -> [u8; 32] {
    let mut state: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let (chunks, rem) = data.as_chunks::<64>();
    for block in chunks {
        compress_block(&mut state, block);
    }

    let mut tail = [0_u8; 128];
    tail[..rem.len()].copy_from_slice(rem);
    tail[rem.len()] = 0x80;

    let total_tail = if rem.len() < 56 { 64 } else { 128 };
    tail[total_tail - 8..total_tail].copy_from_slice(&bit_len.to_be_bytes());

    compress_block(&mut state, tail[..64].try_into().expect("first tail block"));
    if total_tail == 128 {
        compress_block(
            &mut state,
            tail[64..128].try_into().expect("second tail block"),
        );
    }

    let mut out = [0_u8; 32];
    for (idx, word) in state.iter().enumerate() {
        out[idx * 4..(idx + 1) * 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

fn compress_block(state: &mut [u32; 8], block: &[u8; 64]) {
    let mut w = [0_u32; 64];
    for i in 0..16 {
        w[i] = u32::from_be_bytes([
            block[i * 4],
            block[i * 4 + 1],
            block[i * 4 + 2],
            block[i * 4 + 3],
        ]);
    }
    for i in 16..64 {
        let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
        let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16]
            .wrapping_add(s0)
            .wrapping_add(w[i - 7])
            .wrapping_add(s1);
    }

    let mut a = state[0];
    let mut b = state[1];
    let mut c = state[2];
    let mut d = state[3];
    let mut e = state[4];
    let mut f = state[5];
    let mut g = state[6];
    let mut h = state[7];

    for i in 0..64 {
        let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
        let ch = (e & f) ^ ((!e) & g);
        let temp1 = h
            .wrapping_add(s1)
            .wrapping_add(ch)
            .wrapping_add(K[i])
            .wrapping_add(w[i]);
        let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
        let maj = (a & b) ^ (a & c) ^ (b & c);
        let temp2 = s0.wrapping_add(maj);

        h = g;
        g = f;
        f = e;
        e = d.wrapping_add(temp1);
        d = c;
        c = b;
        b = a;
        a = temp1.wrapping_add(temp2);
    }

    state[0] = state[0].wrapping_add(a);
    state[1] = state[1].wrapping_add(b);
    state[2] = state[2].wrapping_add(c);
    state[3] = state[3].wrapping_add(d);
    state[4] = state[4].wrapping_add(e);
    state[5] = state[5].wrapping_add(f);
    state[6] = state[6].wrapping_add(g);
    state[7] = state[7].wrapping_add(h);
}

/// Computes lowercase 64-character hex SHA-256 digest of `data`.
pub fn sha256_hex(data: &[u8]) -> String {
    let digest = sha256_bytes(data);
    let mut hex = String::with_capacity(64);
    for byte in digest {
        let _ = write!(&mut hex, "{byte:02x}");
    }
    hex
}

/// Formats an IEEE-754 double according to RFC 8785 §3.2.2.3 (ECMAScript `Number::toString`).
pub fn format_jcs_f64(value: f64) -> Result<String, PlatformError> {
    if !value.is_finite() {
        return Err(PlatformError::invalid_argument("JSON 不接受非有限数。"));
    }
    if value == 0.0 {
        return Ok("0".to_owned());
    }

    let negative = value.is_sign_negative();
    let abs = value.abs();
    let sci = format!("{abs:e}");
    let (mantissa, exp_str) = sci
        .split_once('e')
        .ok_or_else(|| PlatformError::invalid_argument("数值无法按 RFC 8785 编码。"))?;
    let exp: i32 = exp_str
        .parse()
        .map_err(|_| PlatformError::invalid_argument("数值指数无法按 RFC 8785 编码。"))?;

    let mut digits = String::with_capacity(mantissa.len());
    for ch in mantissa.chars() {
        if ch != '.' {
            digits.push(ch);
        }
    }
    while digits.len() > 1 && digits.ends_with('0') {
        digits.pop();
    }

    let k = digits.len() as i32;
    let n = exp + 1;
    let mut out = String::with_capacity(32);
    if negative {
        out.push('-');
    }

    if k <= n && n <= 21 {
        out.push_str(&digits);
        for _ in 0..(n - k) {
            out.push('0');
        }
    } else if 0 < n && n < k {
        let split = n as usize;
        out.push_str(&digits[..split]);
        out.push('.');
        out.push_str(&digits[split..]);
    } else if (-5..=0).contains(&n) {
        out.push_str("0.");
        for _ in 0..(-n) {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        out.push_str(&digits[..1]);
        if k > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        if exp >= 0 {
            out.push('+');
            let _ = write!(&mut out, "{exp}");
        } else {
            let _ = write!(&mut out, "{exp}");
        }
    }
    Ok(out)
}

fn write_jcs_string(value: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in value.chars() {
        match ch {
            '\u{0008}' => out.extend_from_slice(b"\\b"),
            '\u{0009}' => out.extend_from_slice(b"\\t"),
            '\u{000A}' => out.extend_from_slice(b"\\n"),
            '\u{000C}' => out.extend_from_slice(b"\\f"),
            '\u{000D}' => out.extend_from_slice(b"\\r"),
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            c if (c as u32) <= 0x1F => {
                let code = c as u32;
                let hex = format!("\\u{code:04x}");
                out.extend_from_slice(hex.as_bytes());
            }
            c => {
                let mut buf = [0_u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}

fn write_jcs_value(value: &Value, depth: usize, out: &mut Vec<u8>) -> Result<(), PlatformError> {
    if depth > 64 {
        return Err(PlatformError::invalid_argument("JSON 嵌套层数超过限制。"));
    }
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(num) => {
            if let Some(i) = num.as_i64() {
                if i.unsigned_abs() > MAX_SAFE_INT {
                    return Err(PlatformError::invalid_argument("数值超出允许范围。"));
                }
                out.extend_from_slice(i.to_string().as_bytes());
            } else if let Some(u) = num.as_u64() {
                if u > MAX_SAFE_INT {
                    return Err(PlatformError::invalid_argument("数值超出允许范围。"));
                }
                out.extend_from_slice(u.to_string().as_bytes());
            } else if let Some(f) = num.as_f64() {
                let formatted = format_jcs_f64(f)?;
                out.extend_from_slice(formatted.as_bytes());
            } else {
                return Err(PlatformError::invalid_argument("数值超出允许范围。"));
            }
        }
        Value::String(s) => write_jcs_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    out.push(b',');
                }
                write_jcs_value(item, depth + 1, out)?;
            }
            out.push(b']');
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|(left, _), (right, _)| left.encode_utf16().cmp(right.encode_utf16()));
            out.push(b'{');
            for (idx, (key, val)) in entries.into_iter().enumerate() {
                if idx > 0 {
                    out.push(b',');
                }
                write_jcs_string(key, out);
                out.push(b':');
                write_jcs_value(val, depth + 1, out)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// Encodes any `Serialize` value into RFC 8785 canonical JSON UTF-8 bytes.
pub fn canonical_json_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>, PlatformError> {
    let json_val = serde_json::to_value(value)
        .map_err(|err| PlatformError::invalid_argument(format!("JSON 序列化失败：{err}")))?;
    let mut out = Vec::with_capacity(128);
    write_jcs_value(&json_val, 0, &mut out)?;
    Ok(out)
}

/// Computes the 64-character lowercase hex SHA-256 digest of the RFC 8785 canonical JSON representation.
pub fn jcs_sha256_hex<T: Serialize>(value: &T) -> Result<String, PlatformError> {
    let bytes = canonical_json_bytes(value)?;
    Ok(sha256_hex(&bytes))
}
