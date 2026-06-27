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
use rand_core::OsRng;
use serde::Serialize;

const SERVICE: &str = "com.cove.web";
const PRIV_SLOT: &str = "private_key";
const PUB_SLOT: &str = "public_key";
// v0.4.0: second keychain slot for the org root private key, used ONLY
// by the keymaster's Cove client to sign attestation + directory
// manifests inside the admin UI. CLAUDE.md non-negotiable #1 is about
// the HUB never holding root.priv; the keymaster's client legitimately
// holds it (their device IS the trust anchor). Slot names are distinct
// so a keymaster who's also a member has both keys cleanly separated.
const ROOT_PRIV_SLOT: &str = "root_private_key";
const ROOT_PUB_SLOT: &str = "root_public_key";

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
    /// set_password reported success but the value we read back doesn't
    /// match what we wrote. Diagnostic for the suspected unsigned-macOS
    /// silent-no-op scenario.
    #[error("keychain readback mismatch — set_password reported Ok but \
             stored value doesn't match (possible unsigned-app issue)")]
    ReadbackMismatch,
    /// set_password reported success but a subsequent read returned
    /// NoEntry — same silent-no-op symptom from a different angle.
    #[error("keychain readback failed — set_password reported Ok but \
             entry isn't present afterward (possible unsigned-app issue)")]
    ReadbackFailed,
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
    let result = pub_entry.get_password();
    eprintln!("[cove] keys::status() get_password({}/{}) → {:?}",
        SERVICE, PUB_SLOT,
        match &result {
            Ok(_) => "Ok(<pubkey>)".to_string(),
            Err(e) => format!("Err({:?})", e),
        });
    match result {
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
    eprintln!("[cove] keys::import() called: priv_len={} pub_len={}",
              private_key_hex.len(), public_key_hex.len());
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
    let derived_pub = sk.verifying_key().to_bytes();
    if derived_pub != pub_arr {
        eprintln!("[cove] keys::import() FAIL: pubkey mismatch. \
                   derived={} claimed={}",
                  hex::encode(derived_pub), public_key_hex);
        return Err(KeyError::KeysMismatched);
    }
    eprintln!("[cove] keys::import() derived pubkey OK; storing to keychain");

    let priv_result = entry(PRIV_SLOT)?.set_password(private_key_hex);
    eprintln!("[cove] keys::import() set_password({}/{}) → {:?}",
              SERVICE, PRIV_SLOT,
              match &priv_result {
                  Ok(()) => "Ok(())".to_string(),
                  Err(e) => format!("Err({:?})", e),
              });
    priv_result?;

    let pub_result = entry(PUB_SLOT)?.set_password(public_key_hex);
    eprintln!("[cove] keys::import() set_password({}/{}) → {:?}",
              SERVICE, PUB_SLOT,
              match &pub_result {
                  Ok(()) => "Ok(())".to_string(),
                  Err(e) => format!("Err({:?})", e),
              });
    pub_result?;

    // Verify by reading back immediately. If the keyring crate is
    // silently no-op'ing (suspected on unsigned macOS builds), this
    // catches it and raises a loud error instead of leaving the user
    // in an inconsistent state.
    let readback = entry(PUB_SLOT)?.get_password();
    eprintln!("[cove] keys::import() readback get_password({}/{}) → {:?}",
              SERVICE, PUB_SLOT,
              match &readback {
                  Ok(_) => "Ok(<pubkey>)".to_string(),
                  Err(e) => format!("Err({:?})", e),
              });
    match readback {
        Ok(ref s) if s == public_key_hex => {}
        Ok(_) => return Err(KeyError::ReadbackMismatch),
        Err(_) => return Err(KeyError::ReadbackFailed),
    }
    Ok(())
}

/// v0.4.0: generate a fresh Ed25519 keypair on-device using the OS CSPRNG
/// and store it to the keychain. The private key is written and IMMEDIATELY
/// dropped from the Rust stack — only the OS keychain retains it. Returns
/// the public-key hex so the JS side can build the pairing payload (QR +
/// deep link) for admin approval.
///
/// Refuses to overwrite an existing keypair — caller must `clear()` first.
/// Otherwise a misclick on the onboarding pane would silently rotate a
/// member's identity and lose any in-flight session that was tied to the
/// prior key.
pub fn generate() -> Result<String, KeyError> {
    if status()?.has_keys {
        // Surfacing this as ReadbackMismatch reuses an existing variant
        // for "keychain state isn't what we expect"; the JS layer maps
        // it to a clear "already onboarded" message.
        return Err(KeyError::ReadbackMismatch);
    }
    let sk = SigningKey::generate(&mut OsRng);
    let priv_hex = hex::encode(sk.to_bytes());
    let pub_hex = hex::encode(sk.verifying_key().to_bytes());
    eprintln!("[cove] keys::generate() generated fresh keypair, storing");
    entry(PRIV_SLOT)?.set_password(&priv_hex)?;
    entry(PUB_SLOT)?.set_password(&pub_hex)?;
    // Readback verification — same defense as import() against the
    // suspected unsigned-macOS silent-no-op pattern.
    match entry(PUB_SLOT)?.get_password() {
        Ok(ref s) if s == &pub_hex => {}
        Ok(_) => return Err(KeyError::ReadbackMismatch),
        Err(_) => return Err(KeyError::ReadbackFailed),
    }
    Ok(pub_hex)
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
    sign_with_slot(PRIV_SLOT, message)
}

fn sign_with_slot(slot: &str, message: &[u8]) -> Result<String, KeyError> {
    let priv_hex = match entry(slot)?.get_password() {
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

// ---- v0.4.0: root keychain slot (keymaster-only) -----------------------

/// Status of the keymaster's root.priv slot. Distinct from the member
/// status so a keymaster who's also a member sees both clearly.
pub fn root_status() -> Result<KeyStatus, KeyError> {
    let pub_entry = entry(ROOT_PUB_SLOT)?;
    match pub_entry.get_password() {
        Ok(pk) => Ok(KeyStatus { has_keys: true, public_key: Some(pk) }),
        Err(keyring::Error::NoEntry) => Ok(KeyStatus { has_keys: false, public_key: None }),
        Err(e) => Err(e.into()),
    }
}

/// Import a paired root (priv, pub) into the dedicated root slot. Same
/// derivation check as import() — if the claimed pub doesn't derive
/// from priv we refuse, so a paste-typo doesn't end up storing a
/// useless key. Same readback verification too.
pub fn root_import(private_key_hex: &str, public_key_hex: &str) -> Result<(), KeyError> {
    let priv_bytes = hex::decode(private_key_hex)?;
    let pub_bytes = hex::decode(public_key_hex)?;
    let priv_arr: [u8; 32] = priv_bytes.as_slice().try_into()
        .map_err(|_| KeyError::InvalidPrivateKey)?;
    let pub_arr: [u8; 32] = pub_bytes.as_slice().try_into()
        .map_err(|_| KeyError::InvalidPublicKey)?;
    let sk = SigningKey::from_bytes(&priv_arr);
    if sk.verifying_key().to_bytes() != pub_arr {
        return Err(KeyError::KeysMismatched);
    }
    entry(ROOT_PRIV_SLOT)?.set_password(private_key_hex)?;
    entry(ROOT_PUB_SLOT)?.set_password(public_key_hex)?;
    match entry(ROOT_PUB_SLOT)?.get_password() {
        Ok(ref s) if s == public_key_hex => {}
        Ok(_) => return Err(KeyError::ReadbackMismatch),
        Err(_) => return Err(KeyError::ReadbackFailed),
    }
    Ok(())
}

/// Wipe the root slot. Used when retiring a device that was previously
/// the keymaster station.
pub fn root_clear() -> Result<(), KeyError> {
    for slot in [ROOT_PRIV_SLOT, ROOT_PUB_SLOT] {
        match entry(slot)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

/// Sign arbitrary bytes with the root private key. Used by the admin
/// UI to sign attestations (canonical-content bytes) and directory
/// manifests (canonical-content bytes) — same two-step sign-once-per-
/// piece pattern the Python admin tool uses. Returns 64-byte hex sig.
pub fn root_sign_message(message: &[u8]) -> Result<String, KeyError> {
    sign_with_slot(ROOT_PRIV_SLOT, message)
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

    #[test]
    fn generate_creates_keypair_and_signs() {
        if !enabled() {
            return;
        }
        clear().unwrap();
        let pub_hex = generate().unwrap();
        let st = status().unwrap();
        assert!(st.has_keys);
        assert_eq!(st.public_key.as_deref(), Some(pub_hex.as_str()));

        // Newly-generated key signs and verifies. This is the same surface
        // the auth/verify path will use immediately after attestation lands.
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
    fn generate_refuses_overwriting_existing_keys() {
        if !enabled() {
            return;
        }
        clear().unwrap();
        let (priv_hex, pub_hex) = fresh_keypair();
        import(&priv_hex, &pub_hex).unwrap();
        // Onboarding should not blow away an already-imported identity.
        let err = generate().unwrap_err();
        assert!(matches!(err, KeyError::ReadbackMismatch));
        // And the original pubkey is still in place.
        let st = status().unwrap();
        assert_eq!(st.public_key.as_deref(), Some(pub_hex.as_str()));
        clear().unwrap();
    }
}
