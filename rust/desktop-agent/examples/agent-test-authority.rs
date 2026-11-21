use ed25519_dalek::{Signer, SigningKey};
use std::io::{self, Read, Write};

fn main() -> io::Result<()> {
    let key = SigningKey::from_bytes(&[0x5a; 32]);
    let mut claim = Vec::new();
    io::stdin().take(4097).read_to_end(&mut claim)?;
    if !(1..=4096).contains(&claim.len()) {
        return Err(io::ErrorKind::InvalidData.into());
    }
    io::stdout().write_all(&key.sign(&claim).to_bytes())
}
