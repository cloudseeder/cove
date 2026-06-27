//! Tauri commands exposed to the JS webview.
//!
//! These are the only entry points the webview has into Rust. The
//! signing surface is intentionally narrow: the JS side computes
//! canonical bytes (via the RFC 8785 implementation in cove/crypto.ts)
//! and asks Rust to sign them. The private key never leaves Rust after
//! `keys_import`.

use std::sync::Arc;

use tauri::{AppHandle, State};

use crate::keys::{self, KeyStatus};
use crate::subscription::Subscription;

#[tauri::command]
pub fn keys_status() -> Result<KeyStatus, String> {
    keys::status().map_err(Into::into)
}

#[tauri::command]
pub fn keys_import(private_key: String, public_key: String) -> Result<(), String> {
    keys::import(&private_key, &public_key).map_err(Into::into)
}

/// v0.4.0: generate a fresh Ed25519 keypair on-device. The private key
/// goes straight to the OS keychain; only the public-key hex is returned
/// to JS so the UI can build the pairing payload (QR + deep link).
#[tauri::command]
pub fn keys_generate() -> Result<String, String> {
    keys::generate().map_err(Into::into)
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

/// Start the background /stream subscriber. Replaces any prior
/// subscription. The Rust task survives webview close; messages forward
/// to JS as `entry_pushed` Tauri events, AND fire a native notification
/// when the window isn't focused.
#[tauri::command]
pub async fn stream_start(
    state: State<'_, Arc<Subscription>>,
    app: AppHandle,
    hub_url: String,
    token: String,
    thread: String,
) -> Result<(), String> {
    state.inner().clone().start(app, hub_url, token, thread).await
}

/// Stop the background subscriber. Used on logout / "switch identity".
#[tauri::command]
pub async fn stream_stop(state: State<'_, Arc<Subscription>>) -> Result<(), String> {
    state.inner().stop().await;
    Ok(())
}
