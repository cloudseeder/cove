# Cove — Tauri + SvelteKit client

The native shell + PWA target. Replaces nothing; the cryptographic
verification done in `src/lib/cove/` is what the hub holds the client
accountable for.

## Why Tauri (not Electron, not PWA-only)

- **Real-app gravitas.** Signed messaging reads as "secure communications
  client" when it's in the dock, not a tab.
- **OS-keychain key custody.** Private keys live in macOS Keychain /
  Windows Credential Manager / libsecret, not in browser IndexedDB.
- **Background WS + native push.** The subscription stays open in the
  Rust process; notices fire native notifications even when the UI is
  closed. Matches the spec's "comes to the device, now" promise.
- **One codebase → desktop + mobile** since Tauri 2.0.

## Architecture

```
src/
  lib/cove/          # the TS verification + client library
    types.ts          # wire types mirroring src/cove/entry.py et al.
    crypto.ts         # sha256, Ed25519, RFC 8785 JCS canonicalization
    verify.ts         # verifyEntry / verifySth / verifyInclusion / verifyDirectoryManifest
    errors.ts         # AuthenticationError / VerificationError / ClientError
    client.ts         # Client class + Signer abstraction (InJSSigner /
                      #   TauriKeychainSigner)
    tauri.ts          # Tauri detection + typed wrappers around the
                      #   Rust commands (keychain.status/import/clear/
                      #   signMessage)
    state.svelte.ts   # AppState — reactive Svelte 5 wrapper. Tracks
                      #   inTauri + storedPublicKey for keychain mode.
    fixtures.json     # captured from the Python ref via scripts/dump_test_vectors.py
    verify.test.ts    # vitest — pins TS↔Python byte-identity (21)
    client.test.ts    # vitest — Client with mocked fetch + WebSocket
                      #   + the Signer abstraction (10)
    Seal.svelte       # the verification ceremony component (3 states)
    EntryCard.svelte  # a single VerifiedEntry rendered with its Seal
  routes/             # SvelteKit pages
    +layout.svelte
    +page.svelte      # main app: AuthPanel ↔ ThreadView based on auth state
    AuthPanel.svelte  # hub URL + keypair → connect
    ThreadView.svelte # live feed with the ceremony per entry
    ComposeBox.svelte # ⌘⏎ to send; no optimistic insert
    demo/+page.svelte # offline Seal showcase against fixtures.json
src-tauri/                # Rust shell
  src/main.rs              # registers plugins + tray + window events
  src/keys.rs              # OS keychain ops + ed25519 signing
  src/commands.rs          # Tauri command handlers
  src/subscription.rs      # background /stream subscriber (slice 4)
  capabilities/default.json# notification + core perms for the webview
  Cargo.toml
  tauri.conf.json
```

## What happens when a board notice arrives

After slice 4, with the Tauri shell running:

1. Hub accepts the board's notice, fans out a push on /stream.
2. The Rust subscriber receives the push (still running even with
   the webview closed). It emits `entry_pushed` carrying the raw
   payload, AND fires a native notification iff the window isn't
   focused. The notification body is `"New activity in <thread>"` —
   intentionally content-free because Rust does NOT verify; it
   relays.
3. If the webview is open, the JS layer's event listener picks up
   `entry_pushed`, runs the full §5 verification chain via
   `Client.verify`, and appends the resulting `VerifiedEntry` to
   the feed. The Seal renders gold; the "fresh" animation pulses.
4. If the webview is closed, the user sees the system notification
   only. Clicking it would bring the window forward (one slice 4b
   item: clickable notifications). The next time the window is
   open, the missed entries arrive via `/sync` because the
   subscriber-then-sync ordering still holds (we re-sync on
   reconnect).

## Key custody — what slice 3 changed

After slice 3, the private key flow in the Tauri shell is:

1. **First launch** — user pastes/drops a paired `.priv`/`.pub` in the
   AuthPanel. The webview calls `keys_import(priv, pub)`, which sends
   the priv string to Rust. Rust validates that the claimed pub matches
   the actual derivation, stores both in the OS keychain (macOS Keychain
   Services / Windows Credential Manager / Linux Secret Service), and
   the webview wipes its local copy of the priv string.
2. **Subsequent launches** — `keys_status` returns `{has_keys: true,
   public_key: <pub>}`. The AuthPanel shows "Unlock" instead of the
   import form. The Client is constructed with a `TauriKeychainSigner`;
   every signature operation roundtrips through Rust's `sign_message`.
   The private key never reaches the webview JS heap.
3. **Forget identity** — `keys_clear` wipes both keychain entries.
   Used for "switch identity" / "this device left the org" cleanup.

In browser-only mode (no Tauri shell), `isTauri()` returns false and
the AuthPanel falls back to the slice-2 paste flow with `InJSSigner` —
private key in the JS heap. Less secure; the app still works.

## Slice status

- **Slice 1 (done):** project scaffold, TS verification library matching
  the Python reference, vitest contract suite, Seal ceremony component
  + offline fixtures demo.
- **Slice 2 (done):** `Client` TS class with auth + sync + post + WS
  subscribe, the full §5 verification chain on every entry. Svelte
  state (AppState class with $state runes), AuthPanel (paste or drop a
  `.priv`+`.pub` pair → connect), ThreadView (live feed with the seal
  ceremony per entry), ComposeBox (⌘⏎ to send). Demo route at `/demo`
  for the offline Seal showcase.
- **Slice 3 (done):** Rust keychain integration via the `keyring`
  crate. Tauri commands: `keys_status`, `keys_import`, `keys_clear`,
  `sign_message`. JS `Signer` abstraction with `InJSSigner` (browser
  mode) and `TauriKeychainSigner` (private key NEVER reaches the JS
  webview after import — Rust signs arbitrary bytes the JS hands it).
  AuthPanel branches: import-once → unlock on subsequent launches in
  Tauri; paste flow in browser-only mode.
- **Slice 4 (done):** Background /stream subscriber lives in the Rust
  process so push messages arrive when the webview is closed.
  `tokio-tungstenite` holds the connection; messages forward to JS
  via the `entry_pushed` Tauri event for verification + render.
  Native notifications via `tauri-plugin-notification` fire when the
  window isn't focused — neutral wording ("New activity in <thread>")
  because Rust doesn't verify and shouldn't make trust claims.
  System tray (Open / Quit), close-to-tray on window close so the
  subscriber keeps running. JS branches: Tauri uses Rust subscription,
  browser uses the in-tab WebSocket — verification path identical.
- **Slice 4b (next):** packaging (DMG / MSI / AppImage), code-signing,
  auto-update, mobile (Tauri 2's iOS + Android targets).

## Build / dev

You need:

- **Node 20+** and **pnpm**.
- **Rust toolchain** (`rustup` — installs `cargo` + `rustc`) when you
  want the desktop window. The web side runs without Rust.
- System WebView libs on Linux (`webkit2gtk-4.1`, etc.) — see
  https://v2.tauri.app/start/prerequisites/
- **Linux only:** `libsecret-1-dev` for the keyring crate to compile
  against the Secret Service API. On Debian/Ubuntu:
  `sudo apt install libsecret-1-dev`. macOS and Windows use built-in
  system APIs and need no extra packages.
- **Notifications:** macOS prompts on the first `requestPermission`
  call; the AuthPanel triggers it right after a successful auth so
  the prompt arrives with context (the user just consciously
  connected). Linux/Windows usually grant by default.

Install deps:

```bash
pnpm install
```

### Web only (no Rust required)

```bash
pnpm dev         # SvelteKit dev server on :1420
pnpm test:run    # vitest — verification math contract + Client behaviour (30 cases)
```

### Trying it against a running hub

1. From the repo root, with the Python venv active:
   ```bash
   python scripts/gen_keys.py --kind member --name alice --out ~/cove-keys/
   ```
2. Boot the hub (a full bootstrap script lands in a later slice;
   for now use `tests/test_e2e_broadcast.py`'s `_bootstrap_hub_at` as
   the reference wiring or run uvicorn against your own factory).
3. `pnpm dev`, open `http://localhost:1420`.
4. Drop `~/cove-keys/alice.priv` and `~/cove-keys/alice.pub` into the
   AuthPanel, point at your hub URL, connect.

### Desktop window (Rust required)

```bash
pnpm tauri dev   # spawns the Tauri webview pointing at the dev server
pnpm tauri build # production bundle
```

## Regenerating fixtures

`src/lib/cove/fixtures.json` is captured from the Python reference.
Whenever wire shapes change on either side, regenerate:

```bash
# From the repo root, with the Python venv active:
python scripts/dump_test_vectors.py
```

Then `pnpm test:run` — if the TS verify math drifted from Python, this is
where it shows up.

## Verification ceremony — design intent

The seal is the message's identity, not a footer label. Three states by
design:

- **Verified** — gold seal, subtle bloom. Tap to reveal the full chain.
  Most of the time, ambient; reveals on demand. Done well, never
  interrupts the read.
- **Pending** — neutral outline, slow rotating pulse. Brief, transient
  state while verification runs. Never the resting state of a message.
- **Broken** — red, jagged break across the seal, hard shake animation.
  Tampering must feel viscerally wrong, not quietly suspicious. The
  message must not render as a normal-looking bubble with a tiny
  asterisk somewhere.

Failed verification is a hard alarm, not a tooltip.
