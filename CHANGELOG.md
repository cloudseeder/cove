# Changelog

All notable changes to Cove. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The client (`clients/web`) and hub (`src/cove`) ship on the same version — a tag
covers both.

## [0.4.46] — 2026-07-02

### Fixed
- On an iPhone with the PWA installed to Home Screen, the sidebar
  hamburger button sat too close to the top edge of the screen — cramped
  against the notch and hard to tap reliably. Now positioned with
  `env(safe-area-inset-top)` clearance so it drops below the notch/status
  bar. Tap target grew from ~30×20px to ~46×34px to clear Apple's 44pt
  minimum guideline. Panel content clearance grew to match, so nothing
  slides under the button. Android and desktop unaffected (safe-area
  values are 0 there; the base clearance still applies).

## [0.4.45] — 2026-07-01

### Added
- Collapsible thread sidebar. First step in making the layout work on
  a phone-sized screen. A small toggle button sits in the top-left
  corner of the main content area — hamburger when the sidebar is
  hidden, chevron when it's visible.
  - **Desktop (viewport ≥ 640px):** the sidebar remains inline;
    toggling shrinks it to zero width so the main pane gets the whole
    viewport.
  - **Mobile (viewport < 640px):** the sidebar becomes an overlay
    drawer that slides in from the left over the content, with a
    tinted backdrop. Tapping the backdrop or picking a thread /
    Inbox closes the drawer so the newly-selected content is
    actually visible.
- Sidebar open/closed state persists to `localStorage`. First-launch
  default is chosen from the viewport width: open on desktop, closed
  on mobile. Once the user makes an explicit choice it wins over the
  viewport default.

## [0.4.44] — 2026-07-01

### Changed
- Delivery card no longer shows currently-revoked members. `/ledger`
  filters them out of both `acked` and `not_acked` before returning
  the partition. Rationale: the client resolves names against the
  current-members list (revoked keys have no display name there),
  so a revoked pubkey rendered as `aa123456…` was useless noise —
  and a revoked key can't ack anymore anyway. The receipt-substrate
  history is preserved separately: any signed receipt a revoked
  member posted before revocation is still durable in the log and
  visible to anyone auditing the receipts directly. This is a
  UI-facing filter, not a governance-log change.
- One end-to-end assertion updated (`test_revocation_mid_session_
  immediately_cuts_off_revoked_member`) — the new invariant is
  "revoked disappears from the delivery partition, receipt evidence
  remains in the store."

## [0.4.43] — 2026-07-01

### Changed
- "Seal now" is gone from the ephemeral banner. Replaced with a "…"
  actions menu (creator-only) that opens an in-app confirmation
  card asking to "Delete this thread now." The confirm card is
  inline (no browser dialog) and shows a spinner while the seal
  request is in flight, so a slow round-trip is visible instead of
  looking like the button did nothing.

### Fixed
- Manual tombstone from the desktop app was silently failing because
  `window.confirm()` on Tauri's WKWebView often returns `false`
  without opening a dialog. The old "Seal now" button fired
  `confirm()`, got false back, and never hit `tombstoneThread()`.
  Removed all browser-dialog calls from the seal flow.

## [0.4.42] — 2026-07-01

### Fixed
- Client-side `verifySth` now includes the `thread` field in its
  recomputed signing bytes when present. The hub signs ephemeral
  per-thread STHs over content that binds the thread name (see
  `translog_ephemeral._sth_content`); the client verifier omitted
  the field, so its recomputed bytes didn't match what the hub
  signed. Every ephemeral entry's inclusion-proof step failed at
  the STH signature check with "STH signature invalid — pinned
  hub key check failed", and no ephemeral message could be
  verified end-to-end. Main-log STHs (no `thread` field on the
  wire) continue to verify unchanged — byte-identical-when-absent
  rule preserves both shapes through one verifier.
- Two regression tests pin the invariant: an ephemeral STH
  verifies through the shared verifier, and one whose thread
  label was swapped fails (cross-tree substitution defense the
  hub-side already had, now honored on the client too).

## [0.4.41] — 2026-07-01

### Fixed
- Client-side `verify.entryContent` now strips
  `tombstone_valid_after` when null, mirroring `client.signEntry`.
  Without this, every non-tombstone entry (posts, replies,
  receipts) the client signed verified against different bytes
  than it was signed over, and the UI rejected its own first
  message in every thread with "id/sig invalid". The
  `protocol_extensibility` memory calls this out explicitly:
  "the TS-side `client.signEntry` and `verify.entryContent`
  each have their own field-filter loop. Both must mirror the
  Python conditional." Missed it during v0.4.38; now fixed.
- Two regression tests pin the byte-identical rule for
  tombstone_valid_after (plain post + kind=tombstone entry).

## [0.4.40] — 2026-07-01

### Fixed
- `scripts/run_hub.py` (production bootstrap) now wires
  `EphemeralTransLog` into both the pipeline and `create_app`.
  Without this, `POST /threads/ephemeral` returned 503
  `no_ephemeral_log` on the pilot hub — the API layer got the
  parameter in v0.4.37 but the production runner was never
  updated. Only the test conftest was passing the log through.

## [0.4.39] — 2026-07-01

### Fixed
- Delivery indicator on a group message now lists only the audience,
  not every attested member. `/ledger` scopes the acked/not_acked
  partition to the thread's audience when one is set; public threads
  still enumerate the full directory. Prevents the perpetual
  "not yet" list for members who were never in the group to begin
  with.
- Sidebar "Start a new thread…" input now routes through the shared
  new-thread dialog (with the name pre-filled) so the sidebar entry
  point offers the same audience + retention controls as the Inbox
  button. Previously it created a name-only public thread.

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

[0.4.46]: https://github.com/cloudseeder/cove/releases/tag/v0.4.46
[0.4.45]: https://github.com/cloudseeder/cove/releases/tag/v0.4.45
[0.4.44]: https://github.com/cloudseeder/cove/releases/tag/v0.4.44
[0.4.43]: https://github.com/cloudseeder/cove/releases/tag/v0.4.43
[0.4.42]: https://github.com/cloudseeder/cove/releases/tag/v0.4.42
[0.4.41]: https://github.com/cloudseeder/cove/releases/tag/v0.4.41
[0.4.40]: https://github.com/cloudseeder/cove/releases/tag/v0.4.40
[0.4.39]: https://github.com/cloudseeder/cove/releases/tag/v0.4.39
[0.4.38]: https://github.com/cloudseeder/cove/releases/tag/v0.4.38
[0.4.37]: https://github.com/cloudseeder/cove/releases/tag/v0.4.37
[0.4.36]: https://github.com/cloudseeder/cove/releases/tag/v0.4.36
[0.4.35]: https://github.com/cloudseeder/cove/releases/tag/v0.4.35
[0.4.34]: https://github.com/cloudseeder/cove/releases/tag/v0.4.34
[0.4.33]: https://github.com/cloudseeder/cove/releases/tag/v0.4.33
[0.4.32]: https://github.com/cloudseeder/cove/releases/tag/v0.4.32
