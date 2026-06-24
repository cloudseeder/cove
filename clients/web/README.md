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
  lib/cove/       # the TS verification + client library
    types.ts       # wire types mirroring src/cove/entry.py et al.
    crypto.ts      # sha256, Ed25519, RFC 8785 JCS canonicalization
    verify.ts      # verifyEntry / verifySth / verifyInclusion / verifyDirectoryManifest
    fixtures.json  # captured from the Python ref via scripts/dump_test_vectors.py
    verify.test.ts # vitest — pins TS↔Python byte-identity
    Seal.svelte    # the verification ceremony component (3 states)
  routes/          # SvelteKit pages
    +layout.svelte
    +page.svelte   # current demo: Seal in all states against a fixture entry
src-tauri/         # Rust shell (minimal in slice 1)
  src/main.rs
  Cargo.toml
  tauri.conf.json
```

## Slice status

- **Slice 1 (done):** project scaffold, TS verification library matching
  the Python reference, vitest contract suite, Seal ceremony component
  + static demo page.
- **Slice 2 (next):** `Client` TS class — auth + sync + post against a
  running hub. Auth + thread UI. Live `/stream` subscription via
  `EventSource` / `WebSocket`.
- **Slice 3:** Tauri Rust layer for keychain custody, background subscription
  hosting, native notifications. Native build + distribution.

## Build / dev

You need:

- **Node 20+** and **pnpm**.
- **Rust toolchain** (`rustup` — installs `cargo` + `rustc`) when you
  want the desktop window. The web side runs without Rust.
- System WebView libs on Linux (`webkit2gtk-4.1`, etc.) — see
  https://v2.tauri.app/start/prerequisites/

Install deps:

```bash
pnpm install
```

### Web only (no Rust required)

```bash
pnpm dev         # SvelteKit dev server on :1420
pnpm test:run    # vitest — verification math contract
```

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
