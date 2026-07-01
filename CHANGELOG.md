# Changelog

All notable changes to Cove. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The client (`clients/web`) and hub (`src/cove`) ship on the same version — a tag
covers both.

## [0.4.38] — 2026-07-01

**Ephemeral threads — deletion + client UI.** Builds on the 37a
substrate. A thread's creator can now open an ephemeral thread with
a TTL from the new-thread dialog, and the hub auto-seals it at TTL
expiration. The seal ceremony deletes the entries from the hub and
publishes a signed tombstone to the main log with the sealed
ephemeral STH preserved forever.

### Added
- New `tombstone` entry kind + optional `tombstone_valid_after`
  field with the byte-identical-when-absent rule so every prior
  entry's signature stays valid.
- `EphemeralTransLog.close_thread(t)` — freezes the tree, returns
  the final STH, refuses further appends, idempotent for retry
  safety.
- `POST /threads/{T}/tombstone` — manual early seal. Only the
  thread creator can seal; requires a fresh tombstone Entry with
  `valid_after ≤ now`.
- Auto-seal background task — polls every
  `ephemeral_seal_check_seconds` (default 60s) and seals any live
  thread past its TTL, using the pre-signed tombstone entry stored
  at open time. Hub still holds no member keys.
- `GET /ephemeral/final_sth?thread=T` — returns the sealed STH for
  a tombstoned thread. Anyone who kept a copy of an entry can prove
  inclusion by reconstructing the leaf.
- `/threads` rows carry `type`, `expires_at`, and `final_sth`.
- WebSocket `thread_tombstoned` event; clients purge local entries
  for the sealed thread and refresh their listings.
- New-thread dialog: Retention section with `Permanent` / `Ephemeral`
  toggle; TTL presets 7d/30d/90d + a custom 1–365d field.
- Thread list: ⏳ badge with relative expiry on live ephemeral
  threads; ⚰ on tombstoned.
- Thread view: persistent ephemeral banner with a `Seal now` action
  for the creator; tombstone card after sealing.
- Client methods: `openEphemeralThread`, `tombstoneThread`,
  `fetchFinalSth`.

### Changed
- `POST /threads/ephemeral` now takes a full signed tombstone Entry
  (kind='tombstone', author=caller, thread=<T>,
  `tombstone_valid_after`=created_at+ttl) instead of the 37a
  `delete_authorization` dict. One verification path
  (`verify_entry`) replaces the bespoke JCS reconstruction. Wire
  break vs 37a; only the pilot hub was running 37a and it had no
  live ephemeral threads.

## [0.4.37] — 2026-07-01

**Ephemeral threads substrate.** Hub gains the machinery for threads that
carry their own tamper-evident log and can be deleted at the end of their
life. No client UI yet — this ship lands the substrate behind the
`/threads/ephemeral` endpoint so the primitives can be reviewed on their
own before the deletion + UI half (0.4.37b).

### Added
- `EphemeralTransLog`: per-thread hash chain, Merkle tree, and signed
  tree heads. `EphemeralSTH` binds the thread name into its signing
  payload so an STH from thread A cannot be relabeled as thread B's —
  cross-tree substitution fails at every proof layer.
- `POST /threads/ephemeral` opens a thread with a TTL and a
  creator-signed `delete_authorization`. The hub verifies the signature,
  refuses TTLs outside `[1d, 365d]`, and refuses duplicate or already-
  permanent thread names.
- Pipeline rejects governance kinds (`notice`, `membership`,
  `supersede`, `revoke`, `branch`, `archive`, `reopen`, `audience`)
  structurally in ephemeral threads. Only `post`, `reply`, `receipt`
  are allowed.
- `/sth?thread=T`, `/proof/inclusion` (auto-routed), and
  `/proof/consistency?thread=T` serve per-thread proofs when `T` is
  ephemeral.
- Startup rebuilds each live ephemeral thread's log from the store, the
  same way the main translog already reconciled from `iter_global`.

### Changed
- Main-log `iter_global` now excludes ephemeral-thread entries so the
  main tree stays free of leaves that belong to per-thread trees.

## [0.4.36] — 2026-06-30

### Fixed
- `/ledger` now treats the entry's author as acked-by-construction. The
  signature on the entry is stronger evidence of "seen" than a receipt
  — before this fix, an author appeared in `not_acked` for their own
  notice until they happened to re-sync the thread.

## [0.4.35] — 2026-06-30

### Added
- Per-entry delivery indicator. Every visible entry gets a "Show
  delivery" pill; tapping it fetches `/ledger?entry=…` and expands to
  "N of M delivered" with each member's name partitioned into acked and
  not-yet. Closes the visibility half of the accountability story —
  the ledger has always been computable, now it's surfaced.
- `Client.fetchLedger(entryId)` on the client for the same endpoint.

## [0.4.34] — 2026-06-30

### Added
- PWA session persistence via passphrase-encrypted IndexedDB vault.
  Closing the browser tab no longer requires re-onboarding. Private
  key is stored under an AES-GCM-256 key derived from a passphrase
  (PBKDF2-SHA-256, 600k iterations). The derived AES key is
  non-extractable; the hub never sees the passphrase or the plaintext
  key.
- Onboarding prompts for a passphrase (PWA only) and stores the key
  before the `/pending` POST so a network failure mid-onboard still
  leaves a recoverable identity.
- AuthPanel adds a "pwa-unlock" mode that shows the masked pubkey +
  passphrase input when a vault is present on the device.

## [0.4.33] — 2026-06-29

### Added
- Invite-code admission gate on `/pending`. Onboarding requires a
  single-use root-signed invite code, minted by the board. Satisfies
  the "no spam scoring" non-negotiable via a binary structural gate
  (you have a valid unused code, or you don't).
- Admin UI for minting + revoking invite codes.

## [0.4.32] — 2026-06-29

### Fixed
- PWA browser-mode: onboarding no longer required Tauri shell.
  One-line gate fix so "Get started" is reachable when the app is
  loaded as a plain browser page instead of a PWA install.

## Earlier

Not backfilled here — see `git log v0.4.31` and earlier tags on
GitHub. Notable prior ships:

- **0.4.31** — atomic bundling of `/proof/inclusion` + `/sth` to fix
  the client-side "size error" race between separate fetches.
- **0.4.27** — audience-scoped threads.
- **0.4.25** — role-keyed `capabilities_by_role` in the directory
  manifest; thread archive/reopen.
- **0.4.19** — `/inbox` landing view.
- **0.4.0** — first pilot-ready ship.

[0.4.38]: https://github.com/cloudseeder/cove/releases/tag/v0.4.38
[0.4.37]: https://github.com/cloudseeder/cove/releases/tag/v0.4.37
[0.4.36]: https://github.com/cloudseeder/cove/releases/tag/v0.4.36
[0.4.35]: https://github.com/cloudseeder/cove/releases/tag/v0.4.35
[0.4.34]: https://github.com/cloudseeder/cove/releases/tag/v0.4.34
[0.4.33]: https://github.com/cloudseeder/cove/releases/tag/v0.4.33
[0.4.32]: https://github.com/cloudseeder/cove/releases/tag/v0.4.32
