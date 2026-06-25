//! Background WebSocket subscription.
//!
//! The Rust process holds the /stream connection so push messages arrive
//! even when the webview is closed (the spec's "comes to the device,
//! now" promise). Two outputs per inbound message:
//!
//!   1. A `entry_pushed` Tauri event carrying the raw push payload. The
//!      webview (when open) consumes it and runs the JS-side §5
//!      verification chain before render. Rust does NOT verify — it
//!      relays — so the trust posture lives entirely in one place
//!      (cove/client.ts).
//!
//!   2. A native notification ("New activity in <thread>") iff the
//!      window is NOT focused. The notification body is intentionally
//!      content-free — the relay is unverified, so claims about who
//!      sent what would mislead.

use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use serde::Deserialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_notification::NotificationExt;
use tokio::sync::Mutex;
use tokio_tungstenite::tungstenite::Message;

/// Shared handle: a JoinHandle so a new stream_start can cancel the
/// previous task before opening another connection.
pub struct Subscription {
    inner: Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl Subscription {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(None),
        }
    }

    pub async fn start(
        self: Arc<Self>,
        app: AppHandle,
        hub_url: String,
        token: String,
        thread: String,
    ) -> Result<(), String> {
        // Cancel any prior subscription first.
        self.stop().await;
        let app_for_task = app.clone();
        let sub_for_task = self.clone();
        let handle = tokio::spawn(async move {
            run_subscription(app_for_task, hub_url, token, thread).await;
            // When the task exits naturally, clear the slot.
            let mut guard = sub_for_task.inner.lock().await;
            *guard = None;
        });
        let mut guard = self.inner.lock().await;
        *guard = Some(handle);
        Ok(())
    }

    pub async fn stop(&self) {
        let mut guard = self.inner.lock().await;
        if let Some(h) = guard.take() {
            h.abort();
        }
    }
}

#[derive(Deserialize)]
struct PushEnvelope<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
}

/// The reconnecting subscriber loop. Each iteration: build the WS URL
/// (token as a query param since browsers can't set Authorization on a
/// WS handshake; the hub accepts both), connect, drain messages,
/// reconnect on close with capped exponential backoff.
async fn run_subscription(app: AppHandle, hub_url: String, token: String, thread: String) {
    let mut delay = Duration::from_secs(1);
    let max_delay = Duration::from_secs(30);

    loop {
        let ws_url = match build_ws_url(&hub_url, &token) {
            Ok(u) => u,
            Err(e) => {
                eprintln!("cove subscriber: invalid hub url ({e})");
                return;
            }
        };
        match tokio_tungstenite::connect_async(ws_url).await {
            Ok((stream, _resp)) => {
                delay = Duration::from_secs(1); // reset backoff on success
                let (_write, mut read) = stream.split();
                while let Some(msg) = read.next().await {
                    match msg {
                        Ok(Message::Text(text)) => handle_push(&app, &thread, &text).await,
                        Ok(Message::Binary(_)) => continue, // entries are text JSON
                        Ok(Message::Close(_)) | Err(_) => break,
                        _ => continue,
                    }
                }
            }
            Err(e) => {
                eprintln!("cove subscriber: connect failed ({e}); backing off {delay:?}");
            }
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(max_delay);
    }
}

async fn handle_push(app: &AppHandle, thread: &str, text: &str) {
    // Forward the raw payload to any open webview. JS does the
    // verification; we don't lie about what we received.
    if let Err(e) = app.emit("entry_pushed", text) {
        eprintln!("cove subscriber: emit failed ({e})");
    }

    // Fire a notification ONLY when the user isn't already looking at the
    // app. Otherwise the in-app feed update is the user-visible cue.
    let window_focused = app
        .get_webview_window("main")
        .and_then(|w| w.is_focused().ok())
        .unwrap_or(false);
    if window_focused {
        return;
    }

    // Decide if this is even an entry push. Empty/unparseable payloads
    // shouldn't fire a notification.
    let is_entry_push = serde_json::from_str::<PushEnvelope>(text)
        .map(|p| p.kind == "entry")
        .unwrap_or(false);
    if !is_entry_push {
        return;
    }

    // Neutral notification — no trust claim. The JS layer will render
    // the ceremony when the user opens the app.
    let _ = app
        .notification()
        .builder()
        .title("Cove")
        .body(format!("New activity in {thread}"))
        .show();
}

fn build_ws_url(hub_url: &str, token: &str) -> Result<url::Url, String> {
    let mut parsed = url::Url::parse(hub_url).map_err(|e| e.to_string())?;
    match parsed.scheme() {
        "http" => parsed.set_scheme("ws").map_err(|_| "scheme rewrite failed")?,
        "https" => parsed.set_scheme("wss").map_err(|_| "scheme rewrite failed")?,
        s if s == "ws" || s == "wss" => {}
        other => return Err(format!("unsupported hub scheme: {other}")),
    }
    parsed.set_path("/stream");
    parsed.set_query(Some(&format!("token={token}")));
    Ok(parsed)
}
