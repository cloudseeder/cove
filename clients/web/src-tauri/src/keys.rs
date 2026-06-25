//! Key custody.
//!
//! The private key is stored in the OS keychain under
//! `service = "com.cove.web"`, with the username slot disambiguating
//! `private_key` vs `public_key`. The actual storage is delegated to
//! the `keyring` crate, which dispatches to macOS Keychain Services,
//! Windows Credential Manager, or the Linux Secret Service depending
//! on the platform.
//!
//! Once imported the private key is never returned to the JS webview.
//! The only operation that needs it is `sign_message`, which signs
//! and returns the hex signature — the JS side computes everything else
//! (sha256, JCS canonicalization, request assembly) and asks Rust to
//! sign arbitrary bytes when it needs a signature.

use ed25519_dalek::{Signer, SigningKey};
use keyring::Entry;
use serde::Serialize;

const SERVICE: &str = "com.cove.web";
const PRIV_SLOT: &str = "private_key";
const PUB_SLOT: &str = "public_key";

#[derive(Debug, thiserror::Error)]
pub enum KeyError {
    #[error("keychain: {0}")]
    Keychain(#[from] keyring::Error),
    #[error("invalid hex: {0}")]
    Hex(#[from] hex::FromHexError),
    #[error("invalid private key (must be 32 bytes)")]
    InvalidPrivateKey,
    #[error("invalid public key (must be 32 bytes)")]
    InvalidPublicKey,
    #[error("public key does not match the private key derivation")]
    KeysMismatched,
    #[error("no key stored — import first")]
    NotImported,
}

// Manual From-into-String for the Tauri command boundary; serde_json's
// String error pattern is the simplest contract there.
impl From<KeyError> for String {
    fn from(e: KeyError) -> Self {
        e.to_string()
    }
}

#[derive(Serialize)]
pub struct KeyStatus {
    pub has_keys: bool,
    pub public_key: Option<String>,
}

fn entry(slot: &str) -> Result<Entry, KeyError> {
    Entry::new(SERVICE, slot).map_err(Into::into)
}

/// Read current keychain state without exposing the private key.
pub fn status() -> Result<KeyStatus, KeyError> {
    let pub_entry = entry(PUB_SLOT)?;
    match pub_entry.get_password() {
        Ok(pk) => Ok(KeyStatus {
            has_keys: true,
            public_key: Some(pk),
        }),
        Err(keyring::Error::NoEntry) => Ok(KeyStatus {
            has_keys: false,
            public_key: None,
        }),
        Err(e) => Err(e.into()),
    }
}

/// Import a paired (priv, pub) — both 64-char hex. We refuse to store
/// keys where the claimed public key doesn't match the actual derivation
/// from the private key; that catches paste-mistakes BEFORE the user
/// builds a session under a mismatched identity.
pub fn import(private_key_hex: &str, public_key_hex: &str) -> Result<(), KeyError> {
    let priv_bytes = hex::decode(private_key_hex)?;
    let pub_bytes = hex::decode(public_key_hex)?;
    let priv_arr: [u8; 32] = priv_bytes
        .as_slice()
        .try_into()
        .map_err(|_| KeyError::InvalidPrivateKey)?;
    let pub_arr: [u8; 32] = pub_bytes
        .as_slice()
        .try_into()
        .map_err(|_| KeyError::InvalidPublicKey)?;
    let sk = SigningKey::from_bytes(&priv_arr);
    if sk.verifying_key().to_bytes() != pub_arr {
        return Err(KeyError::KeysMismatched);
    }
    entry(PRIV_SLOT)?.set_password(private_key_hex)?;
    entry(PUB_SLOT)?.set_password(public_key_hex)?;
    Ok(())
}

/// Wipe both slots. Used for "switch identity" / "this device left the
/// org" cleanup. Tolerant of missing entries — already-cleared is success.
pub fn clear() -> Result<(), KeyError> {
    for slot in [PRIV_SLOT, PUB_SLOT] {
        match entry(slot)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

/// Sign arbitrary message bytes with the stored private key. Used for
/// both the /auth/verify nonce signature and the canonical-content
/// signature on every entry. The bytes the caller passes are exactly
/// what gets signed — no double-hashing, no implicit transformation.
pub fn sign_message(message: &[u8]) -> Result<String, KeyError> {
    let priv_hex = match entry(PRIV_SLOT)?.get_password() {
        Ok(s) => s,
        Err(keyring::Error::NoEntry) => return Err(KeyError::NotImported),
        Err(e) => return Err(e.into()),
    };
    let priv_bytes = hex::decode(&priv_hex)?;
    let priv_arr: [u8; 32] = priv_bytes
        .as_slice()
        .try_into()
        .map_err(|_| KeyError::InvalidPrivateKey)?;
    let sk = SigningKey::from_bytes(&priv_arr);
    let sig = sk.sign(message);
    Ok(hex::encode(sig.to_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::Verifier;

    // These tests touch the real OS keychain and are gated behind an env
    // var so CI / contributor builds don't prompt for keychain access by
    // default. Run with:
    //
    //   COVE_KEYCHAIN_TESTS=1 cargo test --manifest-path clients/web/src-tauri/Cargo.toml
    //
    // before relying on the import/sign path on a new platform.

    fn enabled() -> bool {
        std::env::var("COVE_KEYCHAIN_TESTS").as_deref() == Ok("1")
    }

    fn fresh_keypair() -> (String, String) {
        let sk = SigningKey::from_bytes(&[7u8; 32]);
        (
            hex::encode(sk.to_bytes()),
            hex::encode(sk.verifying_key().to_bytes()),
        )
    }

    #[test]
    fn import_then_sign_roundtrips() {
        if !enabled() {
            return;
        }
        clear().unwrap();
        let (priv_hex, pub_hex) = fresh_keypair();
        import(&priv_hex, &pub_hex).unwrap();
        let st = status().unwrap();
        assert!(st.has_keys);
        assert_eq!(st.public_key.as_deref(), Some(pub_hex.as_str()));

        let sig_hex = sign_message(b"hello cove").unwrap();
        let sig_bytes = hex::decode(&sig_hex).unwrap();
        let sig = ed25519_dalek::Signature::from_slice(&sig_bytes).unwrap();
        let pub_bytes = hex::decode(&pub_hex).unwrap();
        let vk = ed25519_dalek::VerifyingKey::from_bytes(
            pub_bytes.as_slice().try_into().unwrap(),
        )
        .unwrap();
        assert!(vk.verify(b"hello cove", &sig).is_ok());
        clear().unwrap();
    }

    #[test]
    fn import_rejects_mismatched_pubkey() {
        if !enabled() {
            return;
        }
        let (priv_hex, _) = fresh_keypair();
        let (_, wrong_pub) = {
            let sk = SigningKey::from_bytes(&[9u8; 32]);
            (
                hex::encode(sk.to_bytes()),
                hex::encode(sk.verifying_key().to_bytes()),
            )
        };
        let err = import(&priv_hex, &wrong_pub).unwrap_err();
        assert!(matches!(err, KeyError::KeysMismatched));
    }
}
