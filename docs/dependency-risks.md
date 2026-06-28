# Dependency risks — single-maintainer seams

This is a living catalogue of dependencies whose abandonment (sole maintainer
walks away, archive, etc.) would land specifically on us — and how much work
that would actually be. Kept so we can re-evaluate at intervals rather than
re-derive the risk picture each time.

Excluded from this list: the official Tauri `tauri-plugin-*` crates
(maintained by the Tauri Working Group in `tauri-apps/plugins-workspace`),
the `@tauri-apps/*` JS packages, and everything in `dalek-cryptography/*`.
Those are multi-maintainer with active stewardship; if they ever became
abandoned, the wider Rust/JS ecosystem would have replaced them before we'd
need to act.

## Crates / packages we depend on

### `keyring` (Rust, in `clients/web/src-tauri/Cargo.toml`)

- **Maintainer**: Hwchen, mostly solo.
- **What we use it for**: OS keychain custody of member and root private keys
  via `keys.rs`. Wraps macOS Keychain Services, Windows Credential Manager,
  and Linux Secret Service behind one API.
- **Replacement cost if abandoned**: **Medium** (~a day).
  - The surface in `keys.rs` is small: `Entry::new`, `set_password`,
    `get_password`, `delete_credential`, and the `NoEntry` error variant.
  - These are isolated behind the `KeychainBackend` trait in `keys.rs`, so
    a swap is one new `impl KeychainBackend for X` block plus the static
    `fn backend()` switch — no call-site rewrites.
  - Replacement candidates: `security-framework` (macOS) +
    `windows-rs::Win32::Security::Credentials` (Windows) +
    `secret-service` (Linux), each used directly. Each is a thin platform
    wrapper; the per-platform plumbing is the bulk of the work.

### `qrcode-generator` (JS, in `clients/web/package.json`)

- **Maintainer**: Magic Len, solo.
- **What we use it for**: QR rendering of `cove://pair?…` deep links in the
  admin onboarding panel.
- **Replacement cost if abandoned**: **Trivial** (an afternoon).
  - QR code is a 1994 ISO standard; the library is feature-complete by
    definition. "Unmaintained" doesn't break it.
  - If abandonment ever caused real concern (e.g. a security advisory in
    the encoder), vendor the source as-is or swap to `qrcode` (npm) — same
    output shape (SVG/data-URI), same call surface.

### `@noble/curves` and `@noble/hashes` (JS, in `clients/web/package.json`)

- **Maintainer**: Paul Miller, mostly solo.
- **What we use it for**: All TypeScript-side Ed25519 signing and SHA-256
  hashing (entry IDs, signature verification in `verify.ts`).
- **Replacement cost if abandoned**: **Low**.
  - The Noble libraries are de-facto critical infrastructure across the
    Node/web crypto ecosystem and externally audited. Trust posture closer
    to `serde` than to a typical solo crate.
  - If we ever did need to swap, `@stablelib/ed25519` + `@stablelib/sha256`
    are drop-in equivalents (also widely used, multi-maintainer).

## How to re-evaluate

- Quarterly: check the GitHub repos for activity, open critical advisories,
  maintainer signals (archive notice, "looking for new maintainers" issues).
- Trigger an out-of-band review whenever a CVE lands against any of them or
  when bumping their major version reveals breakage we have to deal with.
- If a seam moves from "Medium" to "High" (e.g. `keyring` grows new must-use
  APIs we'd have to re-implement), promote it to a tracked refactor task.
