// Cove — Tauri shell. Slice 1 is a minimal webview host; the verification
// logic lives in the TS layer.
//
// Slice 3 will add:
//   - keychain integration (cove-key-store, calls into macOS Keychain /
//     Windows Credential Manager / libsecret)
//   - background WebSocket subscription with native notifications when
//     a board notice arrives
//   - optional Rust port of the verify path for paranoia parity

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![])
        .run(tauri::generate_context!())
        .expect("error while running cove-web tauri application");
}
