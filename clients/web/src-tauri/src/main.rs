// Cove — Tauri shell. Slice 3 wires the keychain custody commands so the
// private key lives in the OS keychain after first import, never in the
// JS webview.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod keys;

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            commands::keys_status,
            commands::keys_import,
            commands::keys_clear,
            commands::sign_message,
        ])
        .run(tauri::generate_context!())
        .expect("error while running cove-web tauri application");
}
