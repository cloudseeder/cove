# Cove — Root Key Custody Threat Model

**Scope:** how the org root private key is protected at rest and in use.
**Status:** Draft 0.1 (companion to `client-spec.md` §1)
**Applies to:** v0.4.80+ (before this, root custody was Tauri-only)

The organization root key is the highest-privilege key in Cove. Anyone who possesses it can attest arbitrary pubkeys as members, revoke members, edit roles, and change org-level settings. Its custody is therefore a governance concern first and a technical one second — the technical choices below constrain the attack surface, but the org's own decisions about *who* holds root and *how they behave with it* dominate the actual risk.

This document is honest about what each custody path can and can't defend against. Read it before choosing which platform your keymaster device runs, and before onboarding additional keymasters.

---

## 1. Two custody paths

Cove supports two ways to hold the root private key on a keymaster device.

### 1.1 Tauri desktop (OS keychain)

The Tauri desktop app stores `root.priv` in the platform's OS keychain via the `keyring` crate:

- **macOS:** Data Protection Keychain (via `security-framework` on modern builds; SecItem*)
- **Windows:** Credential Manager (Win32 wincred)
- **Linux:** Secret Service (via libsecret)

The private key crosses from the Rust process into a signing operation only when an admin op fires. It **never crosses the JS↔Rust boundary** after import — the webview's JavaScript can invoke a `root_sign_message` command over IPC, but the priv material stays in Rust memory. A malicious browser extension inside the webview cannot read the raw priv, only observe the signature the Rust side returns.

Access to the keychain slot is gated by whatever the OS enforces at unlock time: a logged-in user session, Touch ID / Windows Hello (if configured), or a keychain-specific password prompt. On a stolen locked device, an attacker cannot extract the key without breaking OS-level protections first.

### 1.2 PWA (passphrase-encrypted IndexedDB)

The PWA path — v0.4.80+ — encrypts the private key under a passphrase-derived KEK and stores the ciphertext in a per-origin IndexedDB database (`cove-root-vault`, `roots` object store, keyed by org pubkey).

- **KDF:** PBKDF2-SHA256, 600,000 iterations (OWASP 2023 minimum), 16-byte random salt.
- **Cipher:** AES-GCM-256, 12-byte random IV.
- **Live decryption:** on unlock, the priv is decrypted into a JavaScript string held in `AppState.liveRootPriv`. It stays there for the session (until Lock, logout, or tab close), so admin operations don't re-prompt the user on every action.
- **Signing:** all root signatures are computed in JavaScript using `@noble/curves/ed25519.sign(priv, message)`.

The private key is materialized in the JavaScript heap during a signing operation and, once unlocked, for the entire admin session. Anything with read access to the JS heap during that window — a malicious browser extension with `<all_urls>` permissions, a rogue devtools instance, a debugger attached to the page — can read the raw priv.

---

## 2. Attack surface comparison

| Attacker capability | Tauri | PWA |
|---|---|---|
| Read the ciphertext at rest | Blocked by OS keychain access controls | Readable via IndexedDB inspection (but AES-GCM makes this useless without the passphrase) |
| Read the plaintext priv at rest | Impossible — never stored plaintext | Impossible — never stored plaintext |
| Read the plaintext priv during a signing operation | Blocked by the JS↔Rust boundary — extension cannot reach Rust memory | **Possible** if a hostile browser extension has script access to the page during the session unlock window |
| Read the plaintext priv from a memory dump of the running process | Requires OS-level compromise to read Rust process memory | Requires OS-level compromise to read browser process memory |
| Read the plaintext priv from disk after device power-off | Impossible — priv is never on-disk plaintext | Impossible — priv is never on-disk plaintext |
| Forge a signature without the priv | Impossible — Ed25519 is EUF-CMA-secure and the priv never leaves Rust | Impossible — Ed25519 is EUF-CMA-secure and the priv only exists in JS during the signing window |
| Obtain the passphrase | N/A — no passphrase in the Tauri path | Possible via a keylogger or a rogue extension with `input`-event access |

The material difference is the **browser extension case**. A user who runs an extension with `<all_urls>` script permissions is trusting that extension with everything the page can see, including a live root priv during any admin session. The Tauri path structurally prevents this because the extension doesn't run inside the Tauri webview (extensions require a full browser extension host, which Tauri's WKWebView / WebView2 / WebKitGTK doesn't provide).

---

## 3. Current risk assessment

At pilot scale, in 2026, the practical risk of the browser-extension case is **low**:

- **Cove is not a target.** Malicious browser extensions are typically designed to steal high-value credentials from a known list of large financial services, big-tech accounts, and cryptocurrency wallets. An org running a Cove hub for its own board is not on any attacker's target list; the payoff of compromising `lwccoa-hub.oap.dev`'s directory is not worth writing a targeted extension.
- **Keymasters are a small, technical group.** In a pilot org, the person running admin operations is usually the same person who set up the hub. They know what a browser extension is and can decide what to install. This changes as Cove scales to less-technical keymasters, but at pilot scale the population is self-selecting.
- **The window is bounded.** A root priv is in the JS heap only while the admin session is unlocked. The Lock button in AdminPanel's danger zone wipes it explicitly; `logoutAll` and closing the tab both wipe it too. A keymaster who unlocks, mints an invite, locks — total window a few seconds — presents a very short opportunity even to an extension already present.

Given this, the PWA path is a reasonable choice for the pilot. The dominant risk remains the **governance** side: who has been trusted with root, whether they've followed the paste-into-file + SHA-verify handoff, whether they've shredded temporary on-host copies. The technical custody choice is second-order at this scale.

---

## 4. Future risk considerations

The risk assessment above depends on three things that could change:

1. **Cove becomes a known target.** If Cove is widely adopted by boards, HOAs, and small institutions, the aggregate value of compromising Cove keymasters may attract targeted extensions. This is the "success problem" — the same growth that validates the protocol also makes it worth attacking.
2. **Keymaster population widens.** Board members onboarded via v0.4.79 CLIs or v0.4.80 PWA custody may not have the same technical background as the pilot's original keymaster. They may run browser extensions casually, use their keymaster device for general browsing, or ignore Lock affordances.
3. **Browser extension permission models weaken.** Browsers periodically loosen extension permissions in response to market pressure. If a future browser makes `<all_urls>` easier to obtain or bundle with popular extensions, the base rate of "extension can read this page" rises.

Any of these should prompt a reassessment of whether PWA custody remains appropriate for the org.

---

## 5. Mitigations available today

For orgs choosing the PWA path, these practices reduce the browser-extension exposure:

- **Use a dedicated browser profile for keymaster work.** Every major browser supports multiple profiles (`chrome://profiles`, Safari User Profiles, Edge profiles, Firefox containers). Create one profile with no extensions installed and use it exclusively for the Cove PWA. All other browsing goes through the regular profile.
- **Audit extensions.** If you use extensions in the same profile as Cove, review them against a strict list: only extensions from known vendors with a track record of security disclosures, and only ones that don't request `<all_urls>` or "read and change all your data on all websites." Password managers (1Password, Bitwarden) generally do request this and should be reviewed carefully. Ad blockers vary — uBlock Origin is well-audited; most others aren't.
- **Lock frequently.** After every admin operation, click Lock root vault for this session (AdminPanel → danger zone). This shortens the window an extension could observe the priv. Amortized cost: a single passphrase re-entry per admin action.
- **Prefer Tauri for high-value hubs.** If you're a keymaster for an org whose compromise would have material consequences — a large HOA with a Reserve fund, a nonprofit board voting on grants, a church council with an endowment — install the Tauri desktop app on your keymaster device and use it instead of PWA custody. The desktop app is the strictly-better custody option for these cases.
- **Prefer a device-signed OS.** Keymaster devices running signed OSes (macOS with Gatekeeper, Windows with SmartScreen, Chromebook) have baseline protections against arbitrary browser-extension installs and rogue processes. Linux desktops offer more flexibility but need explicit care.
- **Rotate root on suspected compromise.** Root rotation is a governance act, not a technical one: the org votes to accept a new root, the manifest chain accepts a new `org` in a fresh manifest signed by both the old root and the new root, and every client re-verifies. This is expensive to coordinate but recoverable — the identity vault and every attestation survive as historical records signed by the old root, and everything going forward is signed by the new root.

---

## 6. When to prefer Tauri

The PWA path is meant to make root custody *accessible* — it removes the "install a desktop app just to be a keymaster" barrier that was blocking board rollouts. It is not meant to be *strictly better* than Tauri custody.

Prefer Tauri when:

- The org's root-key compromise would have material governance, legal, or financial consequences (funds, votes, contracts).
- The keymaster device is shared with other users or general browsing.
- The keymaster runs browser extensions requested by their day-job (dev tools, work SSO, etc.) that request broad script permissions.
- The org's compliance posture requires it (SOC 2, HIPAA-adjacent, similar).

Prefer PWA when:

- The org is at pilot / demonstration scale.
- The keymaster device is browser-only (iPad, Chromebook, borrowed device).
- The Tauri app isn't installed and the admin operation is time-sensitive.
- The keymaster is not the "primary" keymaster and only performs occasional attestations.

Both paths honor the same protocol invariants: the hub never sees the root priv, the priv is never on-disk plaintext, and every attestation carries a root signature that clients verify independently.

---

## Cross-references

- `docs/client-spec.md` §1 (key custody, general)
- `docs/identity-vault-spec.md` §1 (parallel discipline applied to member identity)
- `docs/server-hub-spec.md` §1 (trust model — the invariants this document upholds)
- CLAUDE.md non-negotiable #1 (the hub never holds the root private key — preserved by both paths)
