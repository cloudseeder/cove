//! Tauri commands exposed to the JS webview.
//!
//! These are the only entry points the webview has into Rust. The
//! signing surface is intentionally narrow: the JS side computes
//! canonical bytes (via the RFC 8785 implementation in cove/crypto.ts)
//! and asks Rust to sign them. The private key never leaves Rust after
//! `keys_import`.

use crate::keys::{self, KeyStatus};

#[tauri::command]
pub fn keys_status() -> Result<KeyStatus, String> {
    keys::status().map_err(Into::into)
}

#[tauri::command]
pub fn keys_import(private_key: String, public_key: String) -> Result<(), String> {
    keys::import(&private_key, &public_key).map_err(Into::into)
}

#[tauri::command]
pub fn keys_clear() -> Result<(), String> {
    keys::clear().map_err(Into::into)
}

/// Sign arbitrary bytes — used for the /auth/verify nonce signature
/// AND for canonical-content signatures on every entry. The caller
/// passes bytes; we sign and return hex. No transformation.
#[tauri::command]
pub fn sign_message(message: Vec<u8>) -> Result<String, String> {
    keys::sign_message(&message).map_err(Into::into)
}
