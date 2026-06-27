// Cove — Tauri shell.
//
// Slice 3 wired the keychain custody commands. Slice 4 added:
//   - background /stream subscriber held by this process so push
//     messages arrive even when the webview is closed
//   - native notifications (tauri-plugin-notification) — fires only
//     when the main window isn't focused
//   - system tray with Open / Quit
//   - hide-to-tray on window close (the subscriber stays alive)
//
// Slice 4b adds:
//   - tauri-plugin-updater: cryptographically-verified self-update
//     against the latest.json feed configured in tauri.conf.json.
//     The Tauri-signer public key embedded there is what gates an
//     update install — separate from (and independent of) OS code-
//     signing, so even unsigned macOS/Windows builds get an integrity-
//     checked update channel.
//   - tauri-plugin-process: lets the JS side restart the app after
//     applying an update.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod keys;
mod subscription;

use std::sync::Arc;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, WindowEvent,
};

use crate::subscription::Subscription;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        // Shared subscription handle — slice 4. Wrapped in Arc so the
        // tokio task can also hold a reference and clear its own slot
        // when it exits.
        .manage(Arc::new(Subscription::new()))
        .invoke_handler(tauri::generate_handler![
            commands::keys_status,
            commands::keys_import,
            commands::keys_generate,
            commands::keys_clear,
            commands::sign_message,
            commands::stream_start,
            commands::stream_stop,
        ])
        .setup(|app| {
            // System tray. Uses the app's default window icon so we
            // don't have to ship a second asset; menu has Open/Quit.
            let open_item = MenuItem::with_id(app, "open", "Open Cove", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open_item, &quit_item])?;
            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .icon(app.default_window_icon().cloned().ok_or("missing window icon")?)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "open" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.unminimize();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // Close → hide to tray. The Rust subscription task survives
            // so notifications keep firing.
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running cove-web tauri application");
}
