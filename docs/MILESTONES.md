# Milestones

## 2026-06-25 — Pilot first light (`pilot-first-light`)

First verified entry rendered end-to-end on the LWCCOA pilot stack.

- Member key pasted into the Tauri shell on macOS
- JS canonicalized the entry via RFC 8785 (JCS)
- Ed25519 signature computed in the JS heap (paste-mode signer)
- POSTed to the FastAPI hub at `https://cove.oap.dev`, through Cloudflare
  Tunnel to uvicorn on the headless Debian box
- Hub's acceptance pipeline assigned per-thread seq, persisted to SQLite,
  appended the (entry_id, seq) leaf to the RFC 6962 Merkle log, signed a
  fresh STH with the hub operational key
- `/stream` WebSocket pushed the accepted entry back to the Rust
  subscriber over `wss://` (rustls)
- Rust relayed the raw payload to the webview; JS verified:
  - signature against the directory-attested member pubkey
  - inclusion proof against a freshly-fetched STH at the new tree size
- Svelte 5 rendered the gold Seal on the verified entry, fresh-arrival
  shimmer animation playing

What this proves: every load-bearing claim of the architecture is real
in production — sign-only v1, the hub holds no member private keys, the
tamper-evident log is integrated end-to-end with client verification,
the directory manifest chain is signed by an offline root and verified
client-side. The thing works.

Shipped at: `v0.1.6`
Hub: `cove.oap.dev` (Hetzner-future, Debian-today)
Pilot org: LWCCOA (kevin, bubba, dave, scott, roger, amy, annie)
