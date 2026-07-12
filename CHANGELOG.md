# Changelog

All notable changes to Cove. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The client (`clients/web`) and hub (`src/cove`) ship on the same version — a tag
covers both.

## [0.6.4] — 2026-07-12

### Fixed
- **Passkey sign-in on macOS-without-Touch-ID popped an empty picker
  first.** Chrome + Safari on a MacBook/Mac Mini with no local
  biometric interpreted the `transports: ['internal', 'hybrid']`
  hint literally — tried the internal (local hardware) authenticator
  first, found nothing configured, showed an empty passkey dialog.
  Cancel dropped it through to the hybrid picker where the actual
  iCloud-synced Passkeys (iPad Bio, Pixel Bio, MacMini) lived.
  The initial "empty from passwords" step was confusing UX — Brooks
  hit it after v0.6.3 landed. Fix: drop the transports hint on both
  Passkey unlock paths (`vault-blob.ts` for identity-vault slots,
  `passkey.ts` for the legacy single-device record). The browser
  picks the right transport reliably without our nudge; on Touch ID
  devices it still uses internal.

## [0.6.3] — 2026-07-12

### Fixed
- **Voting crashed the hub with `AttributeError: 'dict' object has
  no attribute 'options'`.** `store._row_to_entry` rehydrates
  `receipt` and `audience` sub-dataclasses when reading an entry back
  from JCS bytes, but v0.6.0 added `ballot` + `vote` fields without
  extending the same treatment. When `pipeline.accept()` looked up
  the target ballot via `store.get(ballot_id)` to validate a vote's
  `option_index`, it got an Entry whose `ballot` was still a raw
  dict — the crash bubbled up as an unhandled 500 without CORS
  headers, so the browser reported the failure as "failed to fetch"
  rather than surfacing the reason. Fix: rehydrate `ballot` +
  `vote` in `_row_to_entry`, mirroring the existing `audience` /
  `receipt` handling. Two new end-to-end tests in test_api.py
  exercise the real EventStore round-trip so this class of gap is
  caught in the API layer, not just at pipeline-with-stubs.

## [0.6.2] — 2026-07-12

### Fixed
- **"Check for updates" silently reported up-to-date when it wasn't.**
  Cloudflare Pages' default static-file `Cache-Control` is `public,
  max-age=14400, must-revalidate` — a 4-hour edge/browser cache. Fine
  for content-hashed JS/CSS chunks (whose URLs change per release),
  wrong for `sw.js`: `reg.update()` (fired by the in-app manual check
  and the periodic auto-check) fetches `/sw.js`, byte-compares against
  the running SW, and installs a new one only on diff. With the old
  sw.js sitting in HTTP cache, there IS no diff, so the update pipeline
  silently no-ops. Brooks hit this after v0.6.0 → v0.6.1 — clicking
  Check for updates left him at v0.6.0 for an hour until he closed
  the tab and reopened (which fetches / directly, bypassing the SW).
  Fix: `static/_headers` sets `Cache-Control: no-cache` on `/sw.js`.
  Content-hashed chunks keep their long cache.

## [0.6.1] — 2026-07-12

### Fixed
- **Ballot + Edit UI now render in Cards view mode.** v0.5.3 (edit)
  and v0.6.0 (ballot) shipped both features by patching only
  `ChatMessage.svelte` (chat view mode). The Cards-mode renderer,
  `EntryCard.svelte` in `src/lib/cove/`, was missed — so users
  reading threads in the default Cards layout saw ballots render as
  plain messages and had no Edit affordance on their own posts.
  Brooks hit both on the same session — testing v0.6.0 ballots on
  brooks-hub, saw the ballot body as a normal post row, and
  reported "no change in the UI." Root cause was the same missing
  integration on both sides. This release wires BallotCard,
  editVersions, and onEdit into EntryCard with the same shape they
  have in ChatMessage.

## [0.6.0] — 2026-07-12

Voting minor: signed single-choice ballots with a live tally and a
deadline close. Signed = every vote is attributable (matches Cove's
accountability model); voter eligibility = the ballot's thread
audience (no separate voter list); voters can change their mind
until the deadline (fresh vote entry per change; tally takes the
latest per voter). Secret ballot (blinded signatures) is a future
slice — when it lands it'll be a distinct entry kind, not a flag.

### Added
- **Two new entry kinds — `ballot` and `vote`** (spec §3.5).
  - Ballot: `body` = question, `ballot.options[]` = choices (2..N,
    distinct, non-empty), `ballot.closes_at` = deadline (RFC3339 UTC,
    strictly future).
  - Vote: `vote.ballot_id` = target ballot's content address,
    `vote.option_index` = choice.
  - Both fields carry the byte-identical-when-absent canonicalization
    rule so pre-v0.6.0 signatures still verify.
- **Pipeline write-side gates.** Ballot: empty options/duplicates/
  past-deadline rejected with structured reasons
  (`ballot_options_empty`, `ballot_options_duplicate`,
  `ballot_closes_in_past`, `ballot_bad_closes_at`,
  `ballot_missing_closes_at`, `ballot_missing_payload`). Vote:
  unknown ballot / wrong thread / out-of-range option / past-close /
  non-audience voter (`vote_ballot_unknown`, `vote_wrong_thread`,
  `vote_option_out_of_range`, `vote_ballot_closed`,
  `vote_not_in_audience`). Empty-body guard extended to cover
  `ballot`.
- **BallotCard rendering.** Ballot entries render as first-class
  cards in the feed with the question, each option with a live
  count + percentage fill bar, remaining-time countdown, and the
  caller's current selection. Clicking an option casts a vote (or
  changes it — "click another to change" hint appears). After
  `closes_at` the card flips to "Closed" and options become
  read-only. Vote entries are hidden from the chronological feed;
  they fold into the ballot card.
- **"New ballot" launcher** in the compose box (🗳 icon, next to
  the branch 🌿 icon). Modal takes a question, 2–10 distinct
  options, and a closes-in dropdown (1h / 6h / 24h / 3d / 7d / 14d).
  Client posts a signed ballot entry; nobody but the caller could
  have minted it.
- **Client `AppState.createBallot` + `AppState.castVote`.** Errors
  from the hub surface as `Error(reason)` for the UI to catch.

### Spec
- `docs/server-hub-spec.md` §3 kinds enumeration adds `ballot`,
  `vote`.
- New §3.5 covers the full write-side rules, tally rule, closure
  semantics, rendering mandate, and explicit non-goals (approval
  voting, ranked-choice, secret ballot, ballot-scoped voter lists).

## [0.5.3] — 2026-07-12

### Added
- **Edit your own messages.** Every message you authored gets an Edit
  button in the footer row. Editing opens an inline textarea seeded
  with the current body; saving posts a `kind='supersede'` entry
  (spec §3.3). The feed renders the newest version over the original;
  an "edited · ..." chip on the row reveals every prior version with
  its timestamp when clicked. The log carries every version — display
  collapses, audit doesn't. Same author / same thread is enforced
  both client-side (Edit affordance hidden on other authors' posts)
  and hub-side (pipeline gate returns `supersede_wrong_author` /
  `supersede_wrong_thread` / `supersede_target_unknown` /
  `supersede_missing_target` on violation).

### Fixed
- **Empty messages are refused end-to-end.** The pipeline rejects
  `post`/`reply`/`notice`/`supersede` entries with an empty
  (whitespace-only) body AND no blob references, with structured
  `reason="empty_body"`. Prior to v0.5.3 an entry with `body=""`
  could land and render as a blank row in the feed — Brooks hit one
  in the `flood-recovery` thread. The client's compose box already
  gated non-empty submits; the missing gate was the wire acceptance
  layer, which now closes the loop. Attachment-only entries (a PDF
  share with no commentary) remain legitimate — the rule is "body
  OR blobs," not both.

  Existing empty entries stay in the log (append-only) but can be
  hidden from view by editing them — the new Edit feature lands on
  the empty entry and lets the author replace the empty body with
  actual content or `"[retracted]"`.

## [0.5.2] — 2026-07-12

### Added
- **`GET /search`** — substring search over `post`/`reply`/`notice`
  bodies + thread names on the active hub. Audience-scoped (removed
  members see nothing past their removal seq — same clamp as `/sync`);
  ephemeral threads excluded so a sealed thread's content isn't
  searchable. Server-computed snippet windowed around the match.
  Debounced 250ms input in the sidebar with a compact results panel;
  clicking a result switches to that thread. Backing SQL is
  `LIKE` over the JCS content blob — false positives on pubkey
  substrings are possible but rare and readable (snippet clarifies).
  If precision becomes a problem, swap for FTS5; interface stays put.
- **Draft thread auto-save** — the "New thread" dialog's name +
  message + audience selection + retention pick auto-save to
  localStorage (debounced 500ms) under a per-hub key
  `cove.draft.newThread.<hub_url>`. Accidentally cancelling the
  dialog no longer loses in-progress work — reopening restores. A
  successful submit clears the draft; an explicit "Clear draft" button
  wipes it on demand. Per-hub keys prevent brooks-hub drafts from
  leaking into lwccoa-hub sessions.

## [0.5.1] — 2026-07-12

### Fixed
- **Invite codes and pending queue survive hub restart.** Prior to
  v0.5.1 both were process-local in-memory state — every deploy
  silently invalidated whatever the keymaster had texted to
  prospective members, and every restart forced pending members to
  reopen their app before the admin queue rebuilt. `InviteRegistry`
  and `PendingRegistry` now write through to the shared SQLite file
  (`data/cove.db`, distinct tables from EventStore/VaultStore) and
  reload on startup. Public surface unchanged.

  Invite TTLs now use wall-clock (`time.time`) instead of monotonic —
  required for `expires_at` to be comparable across processes. NTP
  drift is bounded to seconds; invites are hours-to-days scoped, so
  the change is behaviorally invisible.

  Cleanup for long-consumed / long-revoked / long-expired invite rows
  runs lazily inside `mint()` (14-day retention window) so a busy
  hub doesn't need a background sweeper. Pending rows are cleared by
  `clear()` or `mark_attested()` and don't accumulate.

## [0.5.0] — 2026-07-12

Audience governance minor: Option B. The write-side gate on audience
mutations moves the "must be in current audience" rule out of a silent
read-time filter and into a structured pipeline rejection, and adds a
new `manage_audience` capability (default: board + officer) required to
remove someone other than yourself. Self-leave and additive changes stay
open to any in-audience member.

Minor-version bump because the governance semantics change: pre-v0.5.0
clients that ship removal-of-other UI to a v0.5.0 hub will start getting
structured 403-shape rejections where they used to get accept-and-ignore
200s. Wire shape is unchanged — audience entries are still a full
pubkey-list replacement.

### Added
- **`manage_audience` capability** — new protocol capability gating
  removal of other members from a thread's audience. Default map:
  `board` gets it alongside `admin`/`archive`; `officer` gets it as
  its first default capability (previously officer had no default
  caps). Manifest override at `capabilities_by_role` still wins.
- **`kind='audience'` entries render as first-class in-stream entries.**
  Previously hidden from the chronological feed (governance metadata
  surfaced only via the header chip); now rendered as compact chips
  like "**Amy** removed *Bob*" / "**Bob** left the thread" — ejection
  can't be silent (non-negotiable #5). Diff computed client-side by
  walking the audience-entry stream in seq order.
- **Leave thread button** in the audience-editor dialog. First
  supported self-leave path — the "to leave, ask another member to
  remove you" workaround the dialog described is gone.
- **Sync grace period for removed members.** `/sync` returns entries
  up to and INCLUDING the audience entry that removed the caller, so
  the removed member sees who ejected them (their own last view).
  `/threads` + `/inbox` continue to surface the thread to them with
  `removed_at_seq: N` so the client can render "You were removed
  on ... by ..." above the (hidden) composer.
- **`AudienceGovernanceError`** in the client — typed error surfaced
  when a governance-reason rejection lands, letting the UI show a
  targeted "requires board or officer role" toast instead of the
  generic "post failed" surface.

### Changed
- **Audience acceptance is now a pipeline write-side gate**, not a
  read-side silent filter. Rejections return `400 {error:"rejected",
  reason:"not_in_audience" | "removal_requires_manage_audience"}`
  with the exact reason (spec §3.4). The read-side filter
  (`store.thread_audience`) stays as defense-in-depth using the same
  shared rule (`cove.audience.authorize_audience_change`).
- **Per-row checkbox in the audience editor**: unchecking someone
  other than yourself is disabled unless the caller has
  `manage_audience`. Adding is always enabled; self is checked+disabled
  as before.
- **`submitAudience()` no longer force-includes self.** Self stays in
  via the checkbox default; the dedicated Leave button is the only
  path that submits without self.
- **Server-hub-spec §3** — added missing entry kinds (`audience`,
  `branch`, `archive`, `reopen`, `tombstone`) to the enumeration
  they'd landed code-first across the v0.4.x arc. New §3.4 covers the
  audience-declaration rules end-to-end.
- **Server-hub-spec §7.1 step 6** — forward-references §3.4 for the
  audience ACL. `membership`-kind ACL remains a future slice.

### Fixed
- **Silent-failure on unauthorized audience mutation.** Pre-v0.5.0
  the hub accepted an audience entry from a non-audience author with
  HTTP 200 and silently dropped it at read time — violating
  non-negotiable #5. Now the pipeline rejects with a structured
  reason.

## [0.4.89] — 2026-07-12

### Fixed
- **Audience-editor dialog leaked across hub + thread switches.**
  Opening the "Edit audience" dialog on a private/group thread on one
  hub, then switching to a public thread on another hub, left the
  dialog rendered over a thread that doesn't have an audience at all.
  Because the dialog's trigger button is only shown for
  audience-scoped threads, dismissing the dialog was the only way out
  — reopening it required going back to the original hub and thread.
  Root cause: `audienceDialog` was local state in `ThreadView.svelte`
  with no reset on hub or thread change; ThreadView isn't unmounted
  across those switches. Fix: track the (hub, thread) the dialog was
  opened for; an `$effect` closes it whenever either changes.

## [0.4.88] — 2026-07-09

### Fixed
- **New public threads without a first message silently vanished.**
  Threads are open-namespace on the wire — they materialize when the
  first entry lands on the hub. The dialog's message field was
  labeled "optional (you can post later)"; for public threads with
  no message, `switchThread` ran but nothing was posted, so the thread
  never materialized. It briefly appeared in the sidebar as the
  transient pending-active row, then vanished on the next switch or
  reload — a silent failure from the user's perspective. (Private
  and ephemeral cases materialize via `setThreadAudience` /
  `openEphemeralThread` even without a message, but the mixed
  behavior made the bug harder to reason about.) Fix: require the
  first message in every case. Placeholder now reads "the thread
  exists once you send it"; a muted note explains the model.
  Submit disabled until both name and message are filled.

## [0.4.87] — 2026-07-09

Third-hub-in-an-afternoon release. Brooks stood up a hub for a friend's
FL HOA using the shipped scripts and docs, and the paper cuts he found
along the way are all fixed here.

### Added
- **`scripts/tunnel_add.sh`** — cloudflared companion to `genesis.sh`.
  Encodes the manual DNS + ingress + reload dance:
    1. Read tunnel name from `/etc/cloudflared/config.yml` (or `$TUNNEL`).
    2. `cloudflared tunnel route dns <tunnel> <hostname>` creates the CNAME.
    3. Insert the ingress rule above the `http_status:404` catch-all,
       preserving the existing file's indentation via awk.
    4. `systemctl reload cloudflared` (falls back to `restart` when the
       running cloudflared version doesn't support reload).
    5. `curl https://<hostname>/healthz` and interpret the response —
       502 → "run genesis first", 000 → "DNS hasn't propagated",
       200 → done.
  Idempotent: running twice with the same args is a no-op after the
  first success. Backs up config.yml with a timestamped `.bak.` before
  rewriting. Refuses cleanly (with a hint) when the config has no
  `http_status:404` catch-all to anchor the insertion, when `cert.pem`
  is missing, or when the hostname already CNAMEs to a different tunnel.

  Usage:
  ```
  sudo ./scripts/tunnel_add.sh <hostname> <port>
  ```

### Changed
- **`genesis.sh` accepts arbitrary hub names** instead of only `lwccoa`
  and `brooks`. The hub-name argument now truly drives the identifier:
  Compose project name = `<name>`, state dir = `./<name>-state`,
  container = `<name>-hub`, default public hostname =
  `<name>-hub.oap.dev`. New optional flags: `--port <n>`,
  `--hostname <fqdn>`, `--org-name "<name>"`, `--keymaster "<name>"`.
  `lwccoa` and `brooks` retain their historical port (8000 / 8001)
  and org-name defaults so the two shipped commands work unchanged.
  Everything else derives from the name.

  `/start` docs updated with the new usage; new callout walks through
  a full flag combo for a fresh org (`--port`, `--hostname`,
  `--org-name`, `--keymaster` together).

### Docs
- **`/start` explicitly states that no host Python or venv is needed.**
  Someone spinning up a hub for the first time might reasonably wonder
  whether they need to `pip install cove` before running `genesis.sh`
  — they don't (the bootstrap runs inside the container). New callout
  right after the "Docker on a small Linux VM" paragraph makes this
  unambiguous.

- **`/start` references `tunnel_add.sh`** right after the "point a
  domain at the container" paragraph, so anyone standing up a hub
  behind an existing Cloudflare Tunnel doesn't have to re-derive the
  manual DNS + ingress + reload steps.

## [0.4.86] — 2026-07-08

### Docs
- **`/join` naming conventions section clarifies the space → hyphen
  behavior.** Copy said "hyphens are conventional but spaces work
  too" — accurate for "you can type a space," misleading for "the
  space stays a space." `sanitizeThreadName` converts spaces to
  hyphens, so `budget 2027` becomes `budget-2027`. Updated to say so
  explicitly so Amy doesn't wonder what happened.

## [0.4.85] — 2026-07-08

Rollup for the pre-Amy-onboarding push. v0.4.84 skipped on the way to a
clean landing number.

### Added
- **Hub list synced across devices via the vault.** The vault content
  plaintext's `meta` object gained an optional `hubs?: string[]`
  field (the growth surface `identity-vault-spec.md` §3.1 called out).
  Every device that unlocks the vault reads `meta.hubs` and merges
  any URLs not already in local `cove.hubs` into the sidebar list.
  Adding or removing a hub triggers a vault content rewrite (fresh IV,
  same CEK, wrap slots untouched) and pushes to every joined hub;
  concurrent edits from multiple devices are handled by the existing
  CAS retry loop on the vault PUT. Additive-merge semantics for
  removals so a hub-drop on one device doesn't silently drop it on
  another — governance-shaped actions like hub removal stay explicit
  per-device.

- **Newly-joined hubs get the vault pushed to them automatically.**
  Old behavior: `saveVault` fanned out only to hubs authenticated at
  mutation time. If you joined a hub AFTER creating your vault, the
  new hub never received a copy, and cross-device "Sign in from a new
  device?" against that hub failed with "no vault at hubUrl for that
  pubkey." Fix: `connect()` detects a hub returning 404 on
  `fetchVault` when we already hold a vault matching this pubkey and
  pushes it there to seed. From that moment on, the hub is a valid
  source for cross-device sign-in.

- **`love.cove.oap.dev/join`** — new-user landing page. Eight steps,
  PWA-first and opinionated about it: install (Safari on
  iPhone/iPad, Chrome on Android, Chrome/Safari on desktop), use the
  invite code, understand Inbox vs. threads, reading and posting
  (cards vs. chat), starting a thread (naming conventions, public
  vs. private), ephemeral vs. permanent with what-gets-kept spelled
  out, using Cove on multiple devices via the vault, and being in
  more than one group (federation). Tone is welcoming rather than
  technical; cross-references `/specs` at the bottom.

- **Pre-composed welcome message in AdminPanel mint flow.** After a
  successful mint, a textarea appears below the code with a ready-
  to-send message: personalized greeting using the invitee's
  name-hint, sender-signed with the keymaster's own attested
  display_name, hub URL and invite code auto-filled, and a link to
  `love.cove.oap.dev/join` for everything else. One-click Copy
  message; delivery is the keymaster's choice (SMS / Signal /
  email / in person).

### Fixed
- **Empty "annual-meeting" ghost thread on newly-joined hubs.**
  `HubConnection.thread` defaulted to the pilot-era LWCCOA string;
  on a newly-added hub that doesn't have that thread, ThreadView
  rendered an empty state. `connect()` now tracks whether the URL
  was new to the local Map and, on successful auth to a new hub,
  routes to Inbox instead of the thread view. Existing-hub
  reconnects preserve their current route.

- **iPad Safari "Update failed: newestWorker is null" after PWA
  reinstall.** iOS Safari right after a fresh install has a brief
  window where `navigator.serviceWorker.getRegistration()` returns
  a registration whose `installing` / `waiting` / `active` are all
  null. `reg.update()` in that state throws. Now guarded: if all
  three worker slots are null, treat as up-to-date instead of
  forcing an update() that fails. Also made silent auto-check
  errors invisible (previously they surfaced via UpdateBar); only
  manual "Check for updates" clicks show failures.

## [0.4.83] — 2026-07-08

### Fixed
- **Mint invite / revoke invite / tier override from PWA.** The v0.4.80
  PWA-safe root-custody feature wired up `HubConnection.rootSigner()`
  for manifest-signing paths (attest, revoke, edit roles, capabilities,
  groups), but three admin-payload paths — `submitTierOverride`,
  `mintInvite`, `revokeInvite` — still called
  `rootKeychain.signMessage(...)` directly. On PWA that threw
  "Tauri command 'root_sign_message' called outside Tauri context"
  and the click did nothing. Extracted a `signRootPayload(message)`
  helper on HubConnection that branches on `inTauri` exactly like
  `rootSigner()`; PWA signs in-JS with `liveRootPriv`, Tauri routes
  through the Rust keychain. All three admin-payload sites now
  complete the same round-trip on both platforms.

## [0.4.82] — 2026-07-08

Board-rollout UX polish before Amy's onboarding test tomorrow. No
protocol changes; all client-side.

### Added
- **Sidebar reorganization.** Old layout put the hub switcher at the
  top and Inbox / Admin as list items above threads — threads (the
  primary user activity) competed with nav items in the same scroll
  region. New layout:
  - Header: **Cove** brand + 🔑 admin key (board-only, with a gold
    dot when pending approvals wait) + collapse chevron.
  - **Inbox pinned** below the header — email-style landing shortcut,
    always visible, with unread badge.
  - Threads section with `+` (new) / `↻` (refresh) controls and a
    scrollable thread list — the middle region, dominant by height.
  - **Hubs pinned** above the identity chip — multi-hub users flip
    between hubs without scrolling through threads.
  - Non-admin members see a clean header with no admin key at all.

- **Light-mode theme.** The app now honors `prefers-color-scheme`
  with a warm cream + brown palette aligned to love.cove.oap.dev's
  landing. Dark palette unchanged. Also flips form controls,
  scrollbars, and the mobile browser status-bar tint via
  `color-scheme: light dark` and paired `theme-color` metas. New
  `--hover` CSS variable replaces hardcoded `rgba(255,255,255,X)`
  hover states across 9 route files so they flip cleanly with mode.

- **Root-import passphrase confirmation + eye toggle.** A typo in the
  create case would silently encrypt the priv under a passphrase the
  user can't reproduce — there's no recovery from that. Two-field
  entry with a shared visibility toggle, live status message
  ("N/12 characters" → "Confirm below" → "Passphrases don't match"
  → "Passphrase confirmed"), and disabled Import button until both
  criteria pass. Unlock case keeps its single field because a wrong
  passphrase there fails loudly at AES-GCM decrypt.

### Fixed
- **UpdateBar was hidden behind iPad's translucent status bar.** With
  `viewport-fit=cover` + `apple-mobile-web-app-status-bar-style=black-
  translucent` (both set in `app.html`), content extends behind the
  notch/status-bar area. UpdateBar sat right there — the Reload button
  was hard to tap. Padded with `env(safe-area-inset-top / -left /
  -right)`. No-op on desktop and Android where env() returns 0.

- **Org settings copy referenced v0.4.13.** Backward-compat notes for
  versions old enough that no deployed client corresponds to them.
  Pre-1.0, pre-public-pilot — dead weight. Just removes the version
  references; behavior unchanged.

## [0.4.81] — 2026-07-07

### Added
- **`docs/root-custody-threat-model.md`** — companion to the v0.4.80
  PWA root-custody feature. Documents the two custody paths (Tauri OS
  keychain vs. PWA passphrase-encrypted IndexedDB), an attack-surface
  comparison row-by-row, honest current risk assessment (why pilot-
  scale exposure is low), future risk considerations, mitigations
  available today, and a decision matrix for when to prefer Tauri vs.
  PWA. Published to `love.cove.oap.dev/specs/root-custody` alongside
  the other three specs; `scripts/sync_landing_specs.sh` extended to
  keep the docs/ and landing/specs/ copies in lockstep.

## [0.4.80] — 2026-07-07

### Added
- **PWA-safe root key custody.** Root custody was Tauri-only because it
  used the OS keychain via the `keyring` crate. PWA users on Mac /
  Windows / Linux couldn't be keymasters from the browser — every
  admin op required either the desktop app or the offline admin CLIs.
  That's now closed.

  New module `clients/web/src/lib/cove/root-vault.ts` mirrors the
  v0.4.34 identity-vault discipline applied to root: PBKDF2-SHA256
  (600k iterations) derives a KEK from the operator's passphrase,
  AES-GCM-256 encrypts the priv, ciphertext lives in a per-org record
  in a new `cove-root-vault` IndexedDB database. Storage is local per
  device — root does NOT sync across devices via the hub (deliberate:
  root propagation is a governance act, not automatic).

  `AppState.importRootKeys` takes an optional passphrase — required
  on PWA, ignored on Tauri. New methods `unlockRootVaultPwa` and
  `lockRootVault` gate the in-memory `liveRootPriv` lifecycle;
  `logoutAll` wipes it alongside the identity-priv material.

  `HubConnection.rootSigner` branches on `inTauri`: Tauri still routes
  through the Rust keychain, PWA signs in-JS with the decrypted
  `liveRootPriv`. Admin ops on PWA before unlock throw a clear
  "Root vault is locked — enter your passphrase" error the UI catches.

  AdminPanel gains three new UI surfaces:
    - Passphrase field on the root-import form (PWA only, ≥ 12 chars).
    - Unlock form when a PWA root vault exists but is locked this session.
    - Lock button in the danger zone (wipes in-memory priv, encrypted
      record stays for next session).

  **Threat model tradeoff, acknowledged:** a compromised browser
  extension can potentially observe the decrypted priv during the
  signing window on PWA, whereas Tauri keeps the priv in the OS
  keychain and never crosses the JS↔Rust boundary. Both are strictly
  better than "you can't be a keymaster on this device," which was
  the prior state. Board keymasters with elevated risk profiles
  should still prefer the Tauri desktop app; the PWA path is the
  ergonomic option for occasional keymaster ops from a shared or
  browser-only device.

  Test coverage: 7 new tests in `root-vault.test.ts` — status,
  import + unlock round-trip, wrong passphrase, per-org isolation,
  re-import overwrite, short passphrase rejection, clear. 187
  client tests total, all green.

## [0.4.79] — 2026-07-07

Board-rollout polish + admin-CLI reach.

### Added
- **`scripts/mint_invite.py`** — CLI companion for the AdminPanel's
  mint-invite flow. Same offline-root.priv + signed-payload shape as
  `attest_member.py` and `rerole_member.py`, POSTs to `/admin/invites`,
  prints the code + expiry. Fills the gap when the keymaster device is
  a PWA-only Mac (no root-key custody there — that's Tauri-only via the
  OS keychain). Ships with the same Cloudflare-WAF-dodging UA header as
  the other admin CLIs.

### Fixed
- **PWA no longer flashes "You're on the latest version" on login.**
  The auto-check on page mount ran `updateStatus` through
  `checking → up-to-date → idle`, producing a distracting flash across
  every launch. Fix: `checkForUpdate({silent = true})` — the routine
  no-update outcome is now completely invisible; only an actual
  `available` / `error` state surfaces. The sidebar footer's manual
  "Check for updates" button passes `silent: false` so a user click
  still gets the toast confirmation.

- **Sidebar header buttons are properly-sized tap targets.** The `+`
  (new thread), `↻` (refresh), and collapse chevron were around 16×16
  px — well below Apple's 44×44 pt and Google's 48×48 dp guidelines.
  Bumped fonts to 1.4em+ and added `min-width: 44px; min-height: 44px`
  so slim glyphs still get a full-size hit region. Also swapped the
  slim `‹` collapse chevron for the heavier `❮` (glyph visual weight
  matters when fingers are close-but-not-precise).

## [0.4.78] — 2026-07-07

Two focused fixes surfaced during the federation demo.

### Fixed
- **Federation sidebar didn't update after Add-Hub.** `AppState.hubs`
  was a plain `Map`; in Svelte 5, `Map.set()` mutations don't trigger
  `$derived` re-runs — only whole-Map reassignment does. HubSwitcher's
  `$derived([...app.hubs.entries()])` therefore only saw hubs added
  during the constructor's `restoreHubsFromStorage()` pass; every
  subsequent `addHub()` (via AddHubPanel, `connect()`, silent re-auth)
  landed in the Map but the sidebar never re-rendered — attempting to
  add the same URL again fired the "already connected" guard, so the
  hub was actually present, just invisible. Fix: `SvelteMap` from
  `svelte/reactivity`, which wraps every mutation with a signal update.

- **"Inbox" title sat under the hamburger toggle on desktop.** When the
  sidebar was collapsed, the toggle floated absolute at top-left of the
  layout, overlapping the InboxPanel header text. Mobile already had a
  `padding-left: 3.8rem` compensation via the `max-width: 640px` rule;
  add a `.sidebar-closed > header` selector that fires on desktop too,
  reserving 4rem of left padding whenever the toggle is visible.

## [0.4.77] — 2026-07-07

Polish arc across the v0.4.76 identity-vault landing — sign-in ergonomics,
deployment tooling, docs, and rough edges surfaced by the LWCCOA pilot's
first real board-ready pass. No protocol changes; the wire is unchanged
from v0.4.76.

### Added
- **"Welcome back to Cove" landing.** On the PWA, when we know your
  pubkey for the current hub URL (persisted after any successful vault
  sign-in), AuthPanel now renders a top-of-page welcome card with your
  pubkey surfaced as "Signed in as ...", editable Hub URL, and Passkey
  + Passphrase unlock buttons side by side. The old "Signing in from a
  new device?" bottom collapse suppresses in this mode (redundant with
  the top surface). "Use a different key" link falls back to paste
  + Get started for shared devices.

- **`scripts/genesis.sh`** — one-command clean-slate bootstrap per hub.
  Wraps the docker compose down → wipe state → rebuild → containerized
  bootstrap → SHA-verified custody handoff → docker compose up sequence
  we'd been running by hand. Refuses to start the container while
  `root.priv` is on disk. Two hubs on one host (production + testbed)
  parameterized via a single argument: `./scripts/genesis.sh lwccoa`
  or `./scripts/genesis.sh brooks`.

- **`--reuse-pubkey` flag on `genesis.sh`** — federation bring-up. Skip
  member keypair generation for the initial attestation and attest an
  existing pubkey against the fresh hub root. Enables "same identity,
  N hubs" bootstrap without a fresh invite / re-attest cycle on the
  second hub.

- **`scripts/rerole_member.py`** — change the role of an existing
  attestation. Companion to `attest_member.py` (which refuses already-
  attested pubkeys). Signs a fresh manifest with an offline root.priv
  and posts to `/admin/attest`. Preserves display_name / affiliation
  unless overridden.

- **PWA "Check for updates" surface.** Extended the existing Tauri
  UpdateBar to cover service-worker updates: when a new SW installs
  while there's already a controller (i.e. an update, not initial
  install), UpdateBar shows "A new version of Cove is ready. Reload
  to update." with a Reload button. Sidebar footer next to the
  version gains a "Check for updates" link that force-checks and
  shows a transient "You're on the latest version" toast if nothing
  new.

- **`docs/identity-vault-spec.md`** — protocol specification for the
  v0.4.76 vault. Companion to `server-hub-spec.md` and `client-spec.md`.
  Covers trust model + non-negotiables preserved, design premise, wire
  schema (record + slot types), hub responsibilities (endpoint 10-step
  validation order, storage schema, throttler split), client
  responsibilities (JCS canonicalization, multi-hub replication,
  divergence resolution, session persistence, sign-in flows), extension
  seam for new unlock methods, and an explicit list of boundaries the
  vault does not touch.

- **`/specs` on love.cove.oap.dev.** Hub, Client, and Identity Vault
  specs are now published on the landing site with a hand-rolled
  markdown renderer (no external deps, matches the plain-HTML-no-build
  ethos). Every spec offers three read paths: rendered here, GitHub-
  rendered, raw markdown. `scripts/sync_landing_specs.sh` copies
  `docs/*.md` → `landing/specs/*.md` so the two stay in lockstep.

- **`/start` guide rewrite.** Step 1 hub bootstrap points at
  `genesis.sh` with the SHA-verified handoff walkthrough. Step 2
  keymaster walks the paste → vault-create sequence. New Step 4
  "Sign in on any device" covers Passkey vs. passphrase unlock, the
  cross-ecosystem story (Apple + Google + Windows), the no-biometric-
  hardware fallback, and the last-slot backup warning. New MacOS
  callout on the Cove.app / PWA dock unification gotcha.

### Fixed
- **Passphrase-mode sign-in now populates the vault.** Pre-v0.4.77,
  a paste sign-in on a device with an existing hub vault left
  `liveVault` null — AdminPanel showed the "Create vault" CTA even
  when the hub already had one (clicking Create would 409). Fix:
  `connect()` now fetches `/vault/{pubkey}` after any successful
  auth and populates `liveVault` + saves the pubkey to localStorage
  if the hub confirms a vault exists. Silently skips on 404 (fresh
  onboard case).

- **AdminPanel Identity vault section renders for all signed-in
  users.** The old gate hid the section on Tauri desktop until you
  had a `liveVault`, which meant no way to seed the first vault
  from a Tauri session. Removed the platform gate — the section is
  shown to any authenticated user; the Create-vault CTA branch
  handles the empty state.

- **Fresh-hub bootstrap now attests role=board.** `bootstrap_pilot.py
  --members` hardcodes `role=member`, which meant the initial member
  couldn't see AdminPanel after their first sign-in. `genesis.sh`
  writes an inline roster CSV with `role=board` + `title=Keymaster`
  instead of using `--members`. Existing v0.4.76 deployments can
  promote via `scripts/rerole_member.py`.

- **`run_hub.py` passes an explicit `VaultStore` path.** Default
  `data/hub.db` landed at `/app/data` in the container (unwritable
  by uid 1000), so the hub crash-looped at import time. Explicit
  `VaultStore(state / "data" / "cove.db")` puts the vaults table in
  the same SQLite file as `EventStore`, distinct table, per the
  vault-spec §4.2.

- **`genesis.sh` bootstraps inside the container, not on host.**
  Original version shelled out to `python scripts/bootstrap_pilot.py`
  on the host, tripping the "running pip as root" warning if the
  host lacked deps. Switched to `docker compose --profile setup run
  --rm bootstrap` which uses the freshly-built image's Python.

- **Admin CLIs send a browser-shaped User-Agent.** urllib's default
  `Python-urllib/3.x` gets 1010'd by Cloudflare's default WAF rules.
  Both `attest_member.py` and `rerole_member.py` now set
  `user-agent: cove-admin-cli/0.4.77` on every request. Auth is on
  the signed payload, so the UA value is cosmetic beyond dodging
  the WAF.

- **Onboarding back button now reads "← Back".** Was labeled "I
  already have a key" — accurate but read like a different function
  rather than a return path. Renamed for clarity.

- **Spec renderer no longer double-escapes special chars.** The
  landing-site markdown renderer escaped HTML on the whole string,
  then extracted inline code from the already-escaped text, then
  escaped again on restore. Any inline code containing `"`, `<`, `>`,
  or `&` came out as literal `&quot;`, `&lt;`, etc. Fix: extract
  code from raw text before escaping. Also restores `&` in link
  URLs so hrefs with query strings don't come out with `&amp;`.

- **WebAuthn user fields carry the slot label.** Vault Passkeys
  were registering with hardcoded `user.name = 'cove-vault'` and
  `user.displayName = 'Cove Vault'`, so the OS Passkey picker
  showed multiple slots as identical entries — impossible to tell
  them apart at unlock time. New registrations use the slot label
  as `user.name` and `Cove vault: <label>` as `user.displayName`.
  Applies to NEW Passkeys only; existing "cove-vault"-named
  credentials need to be deleted from OS Passwords settings and
  re-added to pick up the new naming.

### Federation validated end-to-end
- LWCCOA hub + Brooks-testbed hub, same Kevin Brooks pubkey attested
  by each hub's own fresh root, `--reuse-pubkey` genesis path. Client
  sidebar switcher lets him talk to both hubs from a single PWA
  session, same identity chip on both. First real proof of the
  "one keypair, N hubs" federation invariant end-to-end with the
  v0.4.76 vault flow.

### Known limitations (banked for future)
- Passkey orphan detection: WebAuthn has no delete API, so a
  partial-failure Add-Passkey (ceremony succeeds, vault push
  fails) leaves an orphan credential in iCloud/Google Password
  Manager. Cleanup is manual via OS Settings. Follow-up work
  banked in the deferred-slices memory.

## [0.4.76] — 2026-07-05

### Added
- **Cove-native identity vault.** Cross-platform key custody for people
  whose devices don't share an ecosystem (iCloud Keychain doesn't sync
  with Google Password Manager; Windows sits outside both). The
  canonical Ed25519 priv is encrypted once under a random content-
  encryption key (CEK); each unlock method (passphrase, Passkey PRF,
  future YubiKey / recovery-code) has its own AES-GCM-wrapped copy of
  the CEK sitting in a `method_slots[]` entry. Any device with any one
  unlock method fetches the opaque blob from any joined hub, unwraps
  the CEK, decrypts the priv. Adding, rotating, or removing a method
  rewrites only that slot — the content ciphertext is minted once at
  vault creation and lives for the vault's lifetime.

- **Hub endpoints.** `GET /vault/{pubkey}` is public — the blob is
  opaque so discoverability of "this pubkey has a vault" leaks nothing
  beyond what `/directory` already publishes. `PUT /vault/{pubkey}` is
  authenticated against the vault-owner's own session token; the hub
  verifies the record's Ed25519 sig against the vault-owner pubkey
  (NOT the org root), enforces membership + not-revoked, checks the
  size cap (`HubConfig.max_vault_body_bytes` = 64 KiB), then CASes on
  `prev_vault_hash`. On a stale 409 the response carries `head_hash`
  so the client retries in one round-trip. Non-negotiable #1 preserved:
  the hub sees only ciphertext, never plaintext key material.

- **Two-phase storage quota.** `Throttler.check_storage_delta` and
  `Throttler.commit_storage_delta` split the existing `reserve_storage`
  into a read-only preflight + a commit that accepts signed deltas.
  Prevents quota leaks when a CAS 409 aborts the write between check
  and commit, and lets vault-shrink release quota correctly.

- **Multi-hub replication (client-side).** `AppState.saveVault()`
  pushes to every joined authenticated hub via `Promise.allSettled`;
  per-hub `StaleVaultError` triggers a per-hub retry loop (fetch head,
  replay slot delta, re-sign, re-push; capped at 3 attempts). Partial
  failures surface via `vaultPushFailures` — the AdminPanel shows a
  banner naming the stale hub(s). Divergence resolution on
  `loadIdentityVault`: chain-follows-chain wins (a candidate whose
  hash matches another's `prev_vault_hash` is strictly later), with
  `updated_at` as fallback.

- **Auto-vault-mint on PWA onboard.** Passphrase-path onboarding now
  mints an identity vault after successful attestation using the same
  passphrase, so device #2 can sign in without a fresh invite / re-
  attest cycle. Best-effort — a failed push doesn't undo the successful
  onboard.

- **AuthPanel: cross-device sign-in.** New collapsed "Signing in from
  a new device? Use your Cove vault" surface at the bottom of the auth
  view. Enter hub URL + pubkey + passphrase → client fetches the vault
  from the hub, unwraps the CEK locally, decrypts the priv, routes
  through the standard `connect()` flow.

- **AdminPanel: Identity vault section.** Lists the current vault's
  unlock methods (label, type icon, created date). "Add passphrase"
  and "Add Passkey" open inline forms; "Remove" refuses to drop the
  last method. Push-failure banner shows which hubs didn't receive
  the latest write.

### Tests
- Hub: 12 new `test_vaults.py` tests cover round-trip, sig verify,
  membership gate, CAS races (mirror `test_pipeline_atomicity.py`'s
  monkeypatch-and-thread pattern), stale prev_hash 409 with head_hash
  echo, size cap, storage-quota delta accounting, shape validation,
  and Passkey slot acceptance.
- Client: 11 new `vault-blob.test.ts` tests cover passphrase + Passkey
  round-trips, wrong-passphrase rejection, slot-add preserves CEK,
  slot-remove is sig-valid, JCS canonicalization matches Python
  `rfc8785` on a golden vector, hash stability under key reordering,
  and the vault-KEK PRF salt separation invariant.
- Totals: hub 382 preexisting + 12 new = 394 all green; client 169
  preexisting + 11 new = 180 all green.

### Fresh-start note
- New installs go through the vault path from day one. Existing users
  re-onboard through the new flow — the old `vault.ts` (single-device
  IndexedDB) and OS-keychain-only paths still exist in the code but
  aren't part of the primary onboarding UX anymore. `git revert` on
  the v0.4.76 commits cleanly restores the v0.4.75 code path; any user
  who onboarded during v0.4.76 would need to re-onboard again on the
  reverted client. Given the pilot has ~1 attested user this is
  acceptable.

## [0.4.75] — 2026-07-04

### Fixed
- **Passkey chooser silently hidden on capable Macs.** v0.4.74's
  `passkeySupported()` gated on `PublicKeyCredential.getClientCapabilities()`
  reporting `extension:prf`, but early cuts of that API (Chrome 133 /
  Safari 18) don't consistently list PRF even on browsers that support
  it — the check produced false negatives, and the OnboardingPanel's
  identity-method chooser silently didn't appear on browsers that
  could actually handle the flow.

  Fixed by making `passkeySupported()` optimistic-by-default:
  returns `false` only for the definitive negatives (no
  `PublicKeyCredential` at all, or `isUserVerifyingPlatformAuthenticatorAvailable()`
  explicitly returns false). Everything else returns `true` and lets
  the actual `registerPasskey()` ceremony surface a clear error at
  create time if PRF genuinely isn't returned — better UX than
  silently hiding the whole affordance behind a strict capability check.

  Test suite: 168 preexisting + 1 new = 169 all green.

## [0.4.74] — 2026-07-04

### Added
- **WebAuthn Passkey identity for the PWA — one keypair per PERSON,
  synced across devices.** Extends the federation slice (v0.4.68–
  v0.4.73, "one keypair, N hubs") with the missing cross-device
  portability leg. Brooks's phone-PWA vs. laptop-PWA case surfaced
  the pain: each device generated its own random keypair, so a
  person ended up with N attestations across M hubs. Now the PWA
  can derive its Ed25519 identity from a WebAuthn Passkey via the
  PRF extension, and the Passkey syncs via iCloud Keychain / Google
  Password Manager. Every device with the same synced Passkey
  derives the same priv.

  **Protocol layer unchanged.** Verifiers on hub + client still see
  Ed25519 sigs over canonical bytes exactly like today. Only the
  SOURCE of the priv material changes:
  ```
  Passkey → PRF (WebAuthn ext) → 32-byte pseudo-random output
                              → HKDF-SHA256 (info="cove-ed25519-seed-v1")
                              → 32-byte Ed25519 seed
                              → @noble/curves keypair
  ```

  **New module** `clients/web/src/lib/cove/passkey.ts` (mirrors
  `vault.ts` shape):
  - `passkeySupported()` — feature detect via
    `PublicKeyCredential.getClientCapabilities()` where available,
    optimistic assumption otherwise (registerPasskey() will fail
    cleanly if PRF isn't returned).
  - `passkeyStatus()` — read the persisted `cove-passkey`
    IndexedDB record (single-identity-per-device, keyed by pubkey).
  - `registerPasskey()` — new-identity ceremony. rp.id = `cove.oap.dev`
    (parent domain, covers all `*.cove.oap.dev` origins); user
    verification required; residentKey required so the Passkey is
    discoverable across devices. Persists pubkey + credentialId to IDB.
  - `unlockWithPasskey()` — returning-user ceremony. Reads credentialId,
    challenges via `navigator.credentials.get`, re-derives the priv,
    verifies against the persisted pubkey.
  - `clearPasskeyStorage()` — wipes the client-side record. Does NOT
    delete the platform-level Passkey (WebAuthn doesn't expose that);
    users are told to delete via OS Settings if they want.

  **Fixed PRF salt:** `sha256("cove-passkey-prf-v1")` — same salt on
  every ceremony so the output is stable across create/get and
  across devices. Different apps using the same Passkey with a
  different salt get a different PRF output by WebAuthn spec.

  **AppState additions** parallel to the vault surface:
  `passkeyStatus`, `passkeySupported`, `refreshPasskeyStatus()`,
  `refreshPasskeySupport()`, `unlockFromPasskey({hubUrl, thread})`,
  `clearPasskey()`, `generateAndPairWithPasskey({hubUrl, nameHint,
  thread, invite})`. Constructor kicks off feature detect + status
  refresh alongside the existing vault checks.

  **AuthPanel** gains a 5th mode `pwa-passkey` (rendered when both
  `app.passkeySupported` and `app.passkeyStatus.exists` are true).
  Passkey takes precedence over vault when both exist — cleaner UX.
  Welcome Back → "Sign in with Passkey" button → biometric/PIN prompt
  → derived priv → standard `connect({mode: 'paste', ...})`. "Use a
  different key" affordance clears the local Passkey pointer.

  **OnboardingPanel** gains a top-of-form chooser (PWA-only, hidden
  in Tauri or when Passkey isn't supported): two cards side-by-side —
  🔑 Passkey (recommended, syncs across devices) vs. 🔒 passphrase
  (device-local, old flow). Default derives from
  `app.passkeySupported` via `$derived` so a late feature-detect
  updates the default cleanly. Users can override with the chooser
  buttons.

  **Migration for existing members:** none forced. Existing vault
  and paste-mode users keep their random-priv identity forever. To
  adopt Passkey, create a Passkey via the OnboardingPanel Passkey
  card → new derived pubkey → keymaster attests via the v0.4.71
  "Attest a public key" AdminPanel section. Old pubkey stays valid
  and unused; both coexist in the manifest.

  **Tauri desktop unchanged.** Custom protocol origin isn't a
  WebAuthn RP; Tauri stays on OS-keychain custody (v0.4.73 per-hub
  slots). Users wanting cross-device sync use the PWA on all their
  devices; users wanting native desktop use Tauri with a device-local
  key. Documented tradeoff.

  **Tests:** new `passkey.test.ts` (7 tests) — feature detection,
  status shape, register writes IDB + roundtrips through
  `ed25519.sign/verify`, unlock is deterministic across ceremonies,
  unlock throws on pub mismatch, clear wipes IDB. Uses
  `fake-indexeddb/auto` (already installed) + a hand-rolled
  `navigator.credentials.{create,get}` mock returning canned PRF
  output. Full suite: 168 tests, all green.

## [0.4.73] — 2026-07-03

### Added
- **Per-hub root-key custody.** A keymaster admining multiple hubs
  (Brooks on LWCCOA + his personal testbed) previously had to
  forget-and-reimport every time they switched hubs — one OS-keychain
  slot for all root keys, keyed against the wrong org sig. Now each
  hub gets its own slot keyed by that hub's `org` pubkey (from
  `DirectoryManifest.org`).
  - **Rust** (`clients/web/src-tauri/src/keys.rs`): slot names are
    now suffixed with the org pubkey (`root_private_key.<org>` /
    `root_public_key.<org>`). All four `root_*` functions
    (`root_status`, `root_import`, `root_clear`, `root_sign_message`)
    accept an `Option<&str>` org parameter. When `None`, they fall
    back to the legacy un-suffixed slot for **backward compat with
    pre-v0.4.73 installs** — an existing single-hub keymaster keeps
    working without touching anything, AND if the legacy slot's
    pubkey matches the org they're now asking about, the migration
    happens automatically at read time.
  - **Tauri commands** pass the org through as an optional string
    argument.
  - **TypeScript** (`tauri.ts`): `rootKeychain.{status,import,clear,
    signMessage}` all accept `org?: string`.
  - **`HubConnection`** builds a per-hub `rootSigner()` scoped to
    `this.manifest.org`; every admin op (attest, revoke, edit caps,
    save groups, tier override, mint/revoke invite, set default
    thread, `attestPubkey`) now signs against the correct hub's root.
  - **`AppState`** methods (`refreshRootKeychain` /
    `importRootKeys` / `clearRootKeys`) scope to the active hub via
    a private `activeOrgKey()` helper.
  - **`switchToHub`** refreshes `rootKeysPresent` on every path so
    the AdminPanel reactively reflects whether the newly-active hub
    has its root loaded.
  - **`AdminPanel`** copy names the active hub — "Set up root key
    custody for `lwccoa-hub.oap.dev`", "Import root key for
    `lwccoa-hub.oap.dev`", "Forget `brooks-hub.oap.dev`'s root key
    on this device" — via a new `activeHubLabel` derived value.
    Switching hubs in the sidebar reactively flips all of this.

  **Migration path** for pre-v0.4.73 keymasters: nothing to do
  proactively. The keychain slot you already have keeps working
  against whichever hub its pub matches. When you connect to a
  *second* hub and open Admin, you'll be prompted to import that
  hub's root key — into a new slot, without touching the first.

## [0.4.72] — 2026-07-03

### Fixed
- **Hub URL didn't persist across launches.** `AuthPanel` and
  `OnboardingPanel` still read/wrote the legacy `cove.hubUrl`
  localStorage key. My v0.4.69 `migrateLegacyKeys()` wipes that key
  on every boot after `cove.hubs` is populated (correct behavior for
  the *migration*, wrong assumption about who else was using it) —
  so after the first successful connect, the URL vanished from the
  input on every subsequent launch. Both panels now source their
  pre-fill from `loadActiveHubUrl()` (the multi-hub source of truth
  written by `AppState.connect` on every successful auth), fall back
  to `loadHubUrls()[0]`, then to the LWCCOA default. `AuthPanel`
  now also writes to `cove.activeHubUrl` per-keystroke so partial
  typing survives a reload — safe because `restoreHubsFromStorage`
  only *activates* a stored URL if it also appears in `cove.hubs`
  (which is only written by a successful `addHub()`).
- **`OnboardingPanel`'s local-fallback thread now uses the per-hub
  key** (`cove.thread.${hubUrl}` via `loadThreadFor`) instead of the
  legacy global `cove.thread`. Same fix as the v0.4.69 `switchThread`
  update, extended to onboarding.

## [0.4.71] — 2026-07-03

### Added
- **"Attest a public key" section in the admin panel.** Until now the
  only path from "someone has a pubkey" to "that pubkey is attested"
  was the invite → pending → approve dance. Federation surfaced the
  gap: to attest an identity that already exists on another hub, the
  keymaster had to paste-in the pubkey but had nowhere in the UI to do
  it. New section sits between "Pending approvals" and "Members" with
  a form for pubkey + display name + affiliation + role + optional
  title. Validates 64-char hex on the pubkey field. Warns if the
  pubkey is already an attested member. Wire path: new
  `HubConnection.attestPubkey()` method + `AppState.attestPubkey()`
  delegator; the actual attestation issuance reuses the same
  `issueAttestation` → `issueDirectory` → `submitAttestation` chain
  as `approvePending`.

## [0.4.70] — 2026-07-03

### Fixed
- **Failed add-hub no longer leaves a broken row in the switcher.**
  Discovered from a phone-PWA test: the PWA's own generated identity
  was attested only on brooks-hub, so trying to add lwccoa-hub failed
  with "unknown identity" from the hub. The Map still held the row
  though — the sidebar showed a lock icon, and re-opening the add-hub
  modal said "Already connected." Now: on failure `AddHubPanel`
  removes the just-added hub via `removeHub()` AND restores the
  previously-active hub as active, so the user's working session
  isn't disrupted at all.
- **Clicking an unauth hub row no longer kicks the user to AuthPanel.**
  Same phone test: clicking the failed lwccoa row swapped
  `activeHubUrl` to lwccoa, which flipped `app.authStatus` to
  unauthenticated, which made `+page.svelte` render `AuthPanel` — and
  the user lost their brooks-hub session view. `switchToHub` is now
  async and, if the target is unauthenticated AND a live signer is
  available (Tauri keychain or PWA `livePriv`), silently attempts
  re-auth first. Only swaps `activeHubUrl` on success; on failure the
  current authenticated hub stays active.
- **"Already connected" no longer blocks retry against a failed hub.**
  The check now looks at auth status, not just Map membership.
- **`AddHubPanel` translates "unknown identity" into an actionable
  message.** Instead of the raw hub error, users see: "This hub
  hasn't attested `abc12345…9876`. Ask the hub's keymaster to attest
  your public key, then try again."

### Added
- **Per-row remove affordance in `HubSwitcher`.** Hover shows a ×
  button on the right of each non-active row; click removes the hub
  from the switcher + persisted list. Hidden on the active row (can't
  accidentally cut the branch you're sitting on).
- **Row status indicators.** Failed auth shows `⚠` with the hub's
  error as a tooltip; `connecting` shows `…`; unauthenticated
  placeholder shows the existing `🔒`.

## [0.4.69] — 2026-07-03

### Added
- **Federation UI, Phase 2: talk to N hubs from one client session.**
  Builds on Phase 1's `HubConnection` extraction (v0.4.68). One
  keypair, N hubs; same identity attested by each hub's board. See
  /home/brooks/.claude/plans/glimmering-fluttering-boole.md.

  **Multi-hub state model.** `AppState` now holds
  `hubs: Map<HubUrl, HubConnection>` + `activeHubUrl: string | null`
  + `activeHub` derived getter. All existing per-hub surfaces
  (`app.entries`, `app.thread`, `app.client`, etc.) proxy through
  `activeHub`, so the 67 consumer `.svelte` sites keep working
  unchanged. New methods: `addHub(url)`, `switchToHub(url)`,
  `removeHub(url)`, `logoutAll()`.

  **Sidebar hub switcher.** New `HubSwitcher.svelte` component sits
  between the sidebar header and the thread list. One row per joined
  hub (labelled by hostname), active hub highlighted with the gold
  accent. Placeholder hubs (restored from localStorage but not yet
  authenticated this session) show a small lock icon. "+ Add
  another hub" opens the `AddHubPanel` modal.

  **Add-hub modal.** New `AddHubPanel.svelte` — the user is already
  authenticated on some hub, so we reuse the live keypair (Tauri:
  OS keychain; PWA/paste: `AppState.livePriv` set at unlock time).
  All the panel asks for is the new hub URL. On success the new
  hub joins the switcher and becomes active.

  **Persistence.** New `hubs.ts` helper module with
  `cove.hubs` (JSON array of hub URLs), `cove.activeHubUrl`, and
  `cove.thread.${hubUrl}` per-hub last-viewed thread keys. Boot-time
  one-shot legacy migration folds `cove.hubUrl` + `cove.thread`
  from v0.4.68 and earlier into the new shape.

  **`cove.thread` collision fixed.** Previously a single global key —
  switching to Hub B would silently rehydrate Hub A's last-viewed
  thread name. Now keyed per hub.

  **Signer sharing.** `AppState.livePriv` (memory-only, cleared on
  `logoutAll()`) holds the PWA/paste-mode priv so a second
  `HubConnection` can be constructed without asking the user to
  re-paste. Same threat-model exposure as today's paste-mode session
  priv — just a controlled reference point.

### Fixed
- **`AppState.reset()`** now semantically "log out of everything":
  disposes every joined hub, clears the Map, wipes `livePriv`, clears
  persisted `cove.hubs` + `cove.activeHubUrl`. Aliased to the new
  `logoutAll()` for clarity.

### Testing
- New `hubs.test.ts` (15 tests): hub-list round-trip, activeHubUrl
  round-trip, per-hub thread key isolation, `removeHubUrl`, four
  legacy-migration cases, `hubLabel` hostname parsing.
- Extended `state.test.ts` (+10 tests): `addHub` idempotency,
  `switchToHub`, `removeHub` with fallback, `logoutAll`, delegator
  routing through active hub, restore-from-storage on boot, legacy
  migration on boot.
- Extended `hub.test.ts` (+1 test): two-hub `switchThread`
  isolation regression cover.
- **Total suite: 161 tests, all green.**

## [0.4.68] — 2026-07-03

### Changed
- **Extracted `HubConnection` class from `AppState`.** Phase 1 of the
  federation UI slice (see /home/brooks/.claude/plans/glimmering-fluttering-boole.md).
  All per-hub state (client, entries, threads, inboxRows, members,
  manifest, myAttestation, revoked, pending queue, invites,
  newThreadDialog, view, replyOpen, threadStatus, thread, authStatus,
  session token, WS teardown, seenIds, myReceiptSeq) moved from
  `AppState` into a new `HubConnection` class in
  `clients/web/src/lib/cove/hub.svelte.ts`. `AppState` now holds a
  single `hub: HubConnection | null` field with backward-compat
  delegating getters/methods for every existing surface. All 67
  consumer sites across `.svelte` files are unchanged. Zero
  user-visible change — Phase 2 (v0.4.69) will grow this to
  `hubs: Map<HubUrl, HubConnection>` and add the sidebar switcher.

  **Fragile join points preserved:**
  - `onSessionRefreshed` closure now captures `this` on the
    HubConnection, ready for Phase 2's per-hub isolation.
  - Directory-view mirror pattern (`myAttestation` / `manifest` /
    `members` / `revoked` re-copied after every `fetchDirectory`)
    kept intact; `$derived` refactor deferred to Phase 3.
  - `resetHighWater` + `entries=[]` pairing lives inside
    `HubConnection.switchThread()` — locality preserves the invariant.
  - `pwaTransientPriv` stays on `AppState` — bridges the pre-hub gap
    during onboarding.

  **New tests:** `hub.test.ts` (7 smokes) + `state.test.ts` (4
  delegation smokes). Total suite: 135 tests, all green. The 124
  pre-existing tests pass unmodified.

## [0.4.67] — 2026-07-03

### Changed
- **Client default hub URL: `lwccoa.cove.oap.dev` → `lwccoa-hub.oap.dev`.**
  Follows up on v0.4.66: the two-level `*.cove.oap.dev` subdomain
  pattern turned out not to be covered by Cloudflare's Universal SSL
  wildcard (which only covers one level of subdomain — `*.oap.dev`),
  so requests to `lwccoa.cove.oap.dev` and `brooks.cove.oap.dev`
  both failed with `sslv3 alert handshake failure`. Rather than pay
  for Advanced Certificate Manager to get a `*.cove.oap.dev` wildcard,
  we flattened one level: LWCCOA moves to `lwccoa-hub.oap.dev`,
  personal testbed to `brooks-hub.oap.dev`. Both are first-level
  subdomains of `oap.dev` and covered by the free Universal SSL
  wildcard. Client `OnboardingPanel`, `AuthPanel`, and
  `scripts/attest_member.py` all updated.

## [0.4.66] — 2026-07-03

### Changed
- **Client default hub URL: `cove.oap.dev` → `lwccoa.cove.oap.dev`.**
  Part of a clean-slate URL reshuffling done while the pilot is
  still just two test users. `OnboardingPanel`, `AuthPanel`, and
  the `scripts/attest_member.py` CLI docstring all now default to
  the new URL. The old `cove.oap.dev` alias is being retired at the
  cloudflared layer (dropped, not redirected) — the URL is dead.

## [0.4.65] — 2026-07-03

### Added
- **Identity chip in the sidebar footer surfaces the user's own
  pubkey.** Auto-generated on-device keys never had a UI surface —
  a user couldn't tell a *different* hub's admin what pubkey to
  attest without fishing it out of devtools or a manifest. Now the
  bottom of the left sidebar shows a compact chip with your display
  name + `abcdef…1234` truncated pubkey; click to copy the full
  64-char hex to clipboard (with a brief `✓ copied` feedback). The
  hex is also `user-select: all` for a fallback drag-and-copy.
  Unlocks the federation workflow: paste the copied hex into
  another hub's `roster.csv` under the `pubkey` column and that
  hub attests you under the same identity.
- **Bootstrap `--roster` CSV accepts an optional `pubkey` column.**
  Reuse an existing keypair on a new hub without generating a fresh
  one. When present, bootstrap skips keypair generation for that
  row, drops the "hand each member their .priv" step from the
  custody banner (nothing to hand off), and flags the row in the
  completion output as `[pubkey provided — no .priv written]`.
  Enables the one-keypair-N-hubs federation pattern from CLAUDE.md
  §7 — same identity attested by multiple orgs.

### Changed
- **`docker/README.md` documents three gotchas hit during the
  personal-testbed bring-up:** (1) pre-`mkdir` the state directory
  before bootstrap so the docker daemon doesn't create it as
  root-owned and lock the container user out; (2) `--members`
  always attests at `role=member` (fine for pilots with a separate
  board, wrong for solo-admin bootstraps) — solo admins want the
  roster CSV path; (3) roster CSV can now reuse an existing pubkey
  via the `pubkey` column, with a pointer to the sidebar chip for
  finding your own hex.

## [0.4.64] — 2026-07-03

### Added
- **Keypair groups — one-click audience shortcuts for multi-device
  members.** A person with multiple device keypairs ("Kevin",
  "Kevin's Phone") used to require the admin to select every device
  when adding them to a private thread. Groups bundle N pubkeys under
  a display name so one chip adds all of them at once. Audience on
  the wire is still a flat pubkey list — groups are purely an
  ergonomic layer; no delivery-time semantic changes.

  **Protocol.** New `DirectoryManifest.groups: Optional[list[KeypairGroup]]`
  field, root-signed like attestations. `KeypairGroup = { name,
  member_pubkeys }`. Byte-identical-when-absent canonicalization
  per [[protocol-extensibility]]: pre-v0.4.64 manifests never had
  the field and still verify with their existing signatures.
  Per-group `member_pubkeys` is sorted+deduped on the wire so the
  signed bytes reflect the SET of pubkeys, not input order. Cross-
  group array is sorted by name for a deterministic canonical form.
  Mirrored in Python (`src/cove/identity.py`) and TypeScript
  (`types.ts`, `identity.ts:manifestContent`, `verify.ts:manifestContent`).
  Four new Python tests cover round-trip, byte-identical-absent,
  sig-covers-field, and pubkey-set-not-order equivalence.

  **State.** `AppState.saveGroups(next: KeypairGroup[] | null)` — same
  root-signed manifest-update flow as `setCapabilitiesByRole`. Every
  other `issueDirectory` call site now forwards `groups: current.groups
  ?? null` so a caps edit or attestation add can't silently strip a
  groups list they weren't editing. Plus `AppState.addGroupToNewThread`
  for the new-thread dialog's bulk-add.

  **Admin UI.** New "Keypair groups" section in `AdminPanel.svelte`
  with the same visual shape as the Roles section. Draft-state editor:
  add/remove groups, name each one, multi-select members via checklist,
  Save/Cancel. Validation refuses empty names, empty groups, and
  duplicate names. Save cleans up (trim, sort/dedupe) then calls
  `saveGroups`; empty draft → null so the manifest omits the field.

  **Audience UX.** Both audience dialogs (edit-existing on
  `ThreadView` + new-thread creation) now show a "Shortcuts:" row of
  group chips above the members checklist. Each chip shows `+ Name
  +N` where N is how many of the group's keypairs aren't yet in the
  selection. Click to bulk-add; chip goes disabled with a ✓ once
  every keypair in the group is already selected. Skips revoked
  pubkeys — a group can safely reference a formerly-attested member
  without smuggling them into fresh audiences.

## [0.4.63] — 2026-07-03

### Changed
- **Latest reply surfaces inline under the parent message.** Replaces
  the small "1 reply / N replies" footer link with a visible chip
  showing the reply's author + a 100-char preview + the total count,
  clickable to open the reply panel (same effect as the old link).
  Applies to both Cards and Chat modes via a new shared
  `$lib/cove/ReplyPreview.svelte` (dense variant for Chat). Parents
  with zero replies still show the plain "Reply" button. Since the
  reply chip is the parent's opener, the footer no longer double-shows
  a "Reply" button when a preview is present. Both `EntryCard` and
  `ChatMessage` now take a `latestReply` prop; `ThreadView` computes
  it once per parent via a small `latestReplyFor()` helper that scans
  `app.entries`.

### Fixed
- **Reply panel: new replies no longer land off-screen under the
  compose box.** After posting a reply into a full list, the entry
  appended at the bottom of `.scroll` — below the compose-wrap fold —
  because scroll position didn't move on content growth. `ReplyPanel`
  now scroll-locks to bottom whenever `replies.length` changes and on
  panel open (parent flip from null → entry), so the freshest message
  is always visible above the compose input.

## [0.4.62] — 2026-07-03

### Changed
- **Cards mode: verification chain reveals directly under the seal.**
  Previously the chain panel opened at the bottom of the card, past
  the body, attachments, and footer — on tall cards that meant the
  panel could sit below the fold and require scrolling to see, right
  after the user clicked the trigger. Moved the `<VerificationChain>`
  block up so it renders immediately below the header. Chat mode is
  unchanged — its trigger is the ✓ button at the row's right edge,
  and the panel appearing below the message body is already close to
  the click point.

## [0.4.61] — 2026-07-03

### Changed
- **Long messages in the thread view truncate at 100 chars with a
  "Show more / Show less" toggle.** Applies to both cards mode
  (`EntryCard`) and chat mode (`ChatMessage`) so the two render paths
  keep identical truncation semantics. Introduced a shared
  `$lib/cove/ExpandableBody.svelte` component that owns the 100-char
  threshold, the ellipsis, and the per-message expand state — the
  parent component picks the typography via a `dense` prop (chat mode
  uses dense; cards mode uses reading-rhythm defaults). Toggling
  expand/collapse is per-message so long threads don't force a global
  choice between "everything expanded" and "everything trimmed".
- **Reverted the v0.4.60 Inbox preview cap.** Brooks clarified the
  ask was for the thread view, not Inbox. `previewBody()` in
  `InboxPanel.svelte` returns to using the hub's 140-char cap. The
  full v0.4.60 diff is undone.

### Removed
- Dead `.body` CSS block in `EntryCard.svelte` (superseded by
  `ExpandableBody`'s own body styling) and `.body` + `.row.notice
  .body` blocks in `ChatMessage.svelte`. The `.row.notice .body`
  font-size bump for notice bodies in chat mode is gone; notices still
  stand out via gold row border + NOTICE badge.

## [0.4.60] — 2026-07-03 — REVERTED

## [0.4.59] — 2026-07-03

### Fixed
- **Compose box was visually narrower than the message feed above it.**
  Both `.feed` and `.compose` are direct children of the flex-column
  `.thread` and both get `max-width: 720px; margin: 0 auto` from
  `.thread > :global(*)` — so on paper they should be the same width.
  Under `box-sizing: content-box` (default) the 720px caps only the
  content area; `.feed` has no padding/border so its outer box is 720px,
  but `.compose` has `padding: 0.6rem` + `border: 1px` so its content
  is 720px and its outer is ~741px. Combined with cross-axis auto
  margins in a flex column parent, this can leave the compose
  intrinsic-sized to less than 720px on some layouts.
  Fixed by giving `.compose` `box-sizing: border-box`, an explicit
  `width: 100%`, and `align-self: stretch` — the OUTER box now caps at
  720px, matching `.feed`'s outer, and there's no path for
  intrinsic-sizing to shrink it below the cross-axis fill.

## [0.4.58] — 2026-07-03

### Fixed
- **Sidebar-toggle button no longer overlaps sidebar content — for
  real this time.** v0.4.51 and v0.4.57 both tried to *push* the
  absolute-positioned toggle past the sidebar's edge with a `left`
  offset gated on the sidebar-open class. Both attempts had subtle
  failure modes (Svelte's `:global()` scoper in one, and the reactive
  class binding in the other appeared not to update the button's
  visual position on the desktop-app webview). Replaced the whole
  approach with two mutually-exclusive buttons: a `☰` hamburger over
  the main pane that only renders when the sidebar is CLOSED, and a
  `‹` close-chevron *inside* the sidebar header (`ThreadList`) that
  only exists when the sidebar is OPEN. They live in different DOM
  parents and are gated by `{#if}` — structurally impossible to
  overlap.
- **"Start a new thread…" input clipped the `+` submit button on the
  right of the sidebar.** The flex text-input's default
  `min-width: auto` (min-content) kept it from shrinking below its
  intrinsic ~150–180px, pushing the submit past the 240px sidebar
  column where `overflow: hidden` on `.thread-list` clipped it.
  Added `min-width: 0` to the input — the classic flexbox text-input
  fix.
- **Compose box floated ~1rem above the pane bottom and messages
  scrolled behind it instead of terminating at its top edge.**
  `.compose` was `position: sticky; bottom: 1rem;` inside the same
  scroll container as the feed, so it floated with a gap under it
  and the feed's content scrolled *through* the compose area. Made
  `.thread` a flex column with `.feed` (`flex: 1; overflow-y: auto`)
  as the single scrolling child and compose (`flex: 0 0 auto`) as a
  natural block below it. Compose now sits flush at the pane bottom
  (with `env(safe-area-inset-bottom)` clearance on iPhone) and the
  feed scrolls above it, not under it.

### Added
- `AppState.openSidebar()` complements `closeSidebar()`. Each new
  mutually-exclusive toggle button calls the direction it means
  rather than sharing `toggleSidebar()`, so an accidental double-click
  can't overshoot into the wrong state.

## [0.4.57] — 2026-07-03

### Changed
- **Cards-mode entry header, slimmer and less redundant.** The gold
  seal used to render as a wide pill with "Verified from *Name*,
  *Title*" text plus a "board" role summary — repeating the byline
  that sat inches to its right and, on narrower widths, overlapping
  the timestamp. Now the seal is a compact icon-only emblem next to
  the byline (`Seal` gained a `compact` mode and a `tooltip` prop for
  the hover-only "Verified from …" text), the byline stays as the
  single source of the name/title, and the timestamp uses
  `smartTimestamp()` (`just now` / `12m` / `3:15 PM` / `Jul 3` / `Jul 3,
  2025`) instead of the raw ISO literal. The full ISO is preserved
  on the `<time datetime>` attribute for screen readers and on `title`
  for hover.

### Fixed
- **Desktop sidebar-toggle button rendered on top of the "Inbox" title
  when the sidebar was open.** The v0.4.51 fix — a
  `:global(.layout.sidebar-open) .sidebar-toggle` descendant selector
  in ThreadView's scoped styles — didn't reliably shift the button
  past the sidebar's right edge under Svelte's CSS scoper, so the
  button stayed at `left: 0.6rem` right on top of the sidebar header.
  Switched to a direct `class:pushed={app.sidebarOpen}` binding on the
  button with a plain `.sidebar-toggle.pushed` selector — no `:global`
  ambiguity. Also collapsed two competing `transition:` declarations
  on `.sidebar-toggle` into one; the second was clobbering the first,
  which is why the shift snapped instantly instead of animating.

## [0.4.56] — 2026-07-02

### Fixed
- **PWA service worker cache never invalidated across releases.** The
  `CACHE` constant in `static/sw.js` sat at `'cove-shell-v0.4.29'`
  across 26 releases because nothing bumped it. The SW's `activate`
  handler only prunes caches whose name doesn't match the current
  one, so cleanup never fired and installed PWAs accumulated stale
  asset caches — old JS chunks, old shell HTML, old-shape localStorage
  reads, all coexisting with whatever was fresh. Plausible cause of
  Brooks's "thread-red" phantom row on the phone (sidebar rendering
  driven by stale JS behavior that no longer exists in the current
  codebase). Now: `scripts/bump-sw-cache.js` runs as a `postbuild`
  step and rewrites `build/sw.js`'s CACHE constant to include the
  current package.json version, so every release gets a distinct
  cache name and old caches get pruned on next activation. The
  source-file default (`'cove-shell-vDEV'`) covers `pnpm dev` where
  no build step runs.

## [0.4.55] — 2026-07-02

### Fixed
- **PWA sidebar didn't update when a new audience-scoped thread was
  created (Tauri did).** The browser subscribe path used
  `client.subscribe(thread, cb)` where the callback filtered by
  thread and skipped the v0.4.48 unknown-thread detection that
  lives in `handlePushedRaw`. So on the PWA, a push announcing a
  brand-new thread you're in the audience of arrived on the WS,
  got dropped by the thread filter, and the sidebar stayed empty
  until a manual refresh. The Tauri path was fine because
  `stream.start` forwards raw payloads to `handlePushedRaw`
  regardless of thread. Now the browser path does the same via a
  minimal `subscribeRawWs()` helper that hooks the WebSocket
  directly and routes every push through `handlePushedRaw`.

## [0.4.54] — 2026-07-02

### Fixed
- **Non-audience members briefly saw new group threads in their
  sidebar.** Two compounding leaks introduced in v0.4.38 and v0.4.48:
  1. `POST /threads/ephemeral` broadcast a `{type: "thread_opened"}`
     WS event with no audience filter (couldn't have one — no
     audience exists yet at open time), so every attested member's
     client learned about every new private thread.
  2. `/threads` surfaced empty ephemeral rows with `audience: null`
     to every caller. A non-creator hitting `/threads` between the
     open and the first audience entry saw the thread as public.

  Together: Amy would see a new group thread pop into her sidebar
  before you added her; tapping it showed 0 messages because
  `/sync` correctly filtered her out.

  Fix: dropped the `thread_opened` broadcast entirely — audience
  members learn about the thread via the audience-entry push
  (which IS audience-filtered by the fan-out layer). Scoped the
  `/threads` empty-ephemeral fallback to the CREATOR only.

  Client keeps an inert `thread_opened` handler for forward-compat
  with older self-hosted hubs that might still emit the event.

## [0.4.53] — 2026-07-02

### Fixed
- **New audience-scoped threads didn't appear in the sidebar until
  a manual refresh.** Two related sub-bugs:
  1. `goToInbox` explicitly tore down the WebSocket subscription, so
     while a user was on the Inbox route no pushes reached the
     client at all. The audience-entry that would have added someone
     to a new thread, the `thread_opened` broadcast, receipts — all
     dropped silently. Fix: don't tear down the socket when leaving
     a thread; `handlePushedRaw` already knows to refresh listings
     via the v0.4.48 handlers regardless of current route.
  2. Fresh authentication landed on Inbox without ever opening the
     socket in the first place, so someone who just logged in and
     stayed on Inbox never got pushes. Fix: `ensureSubscribed()` now
     runs at post-auth boot, opening the WS eagerly. The socket
     stays alive across route changes and gets restarted (with the
     new bound thread) when the user actually opens a thread.

## [0.4.52] — 2026-07-02

### Fixed
- **`Client.sync` no longer abandons the whole batch on one bad
  entry.** Previously `for (…) verified.push(await verify(…))` threw
  on any single verification failure, so a single flaky historical
  entry (bad inclusion proof, weird cached STH, whatever) hid every
  other entry that would have verified fine. Symptom Brooks saw:
  Amy was just added to an audience-scoped ephemeral thread, tapped
  it, and only the freshly-arrived push showed up; historical
  entries were silently missing until she quit and restarted (fresh
  boot cleared whatever transient state was tripping verify). Now:
  each entry's signature stands on its own — a failing verify is
  logged via `console.warn` and skipped; the rest of the batch still
  lands. High-water advances to the max seq of what verified
  successfully. Test updated to reflect the new (defensible)
  semantics: tampered entry skipped, remaining entries verify.

## [0.4.51] — 2026-07-02

### Fixed
- **Sidebar toggle button was covering the sidebar's own contents on
  desktop.** The v0.4.45 button was positioned at `left: 0.6rem`
  unconditionally, so when the sidebar was open on desktop (240px
  inline column) the chevron sat on top of the "THREADS" heading /
  Inbox row. Now on viewports ≥641px, the toggle shifts to
  `left: calc(240px + 0.6rem)` when the sidebar is open — sits at
  the sidebar's edge as a proper collapse chevron. Mobile drawer
  behavior is unchanged.

## [0.4.50] — 2026-07-02

### Fixed
- **Mobile thread header stacks vertically.** On narrow viewports
  (<640px) the thread-name column and the view-toggle + status +
  archive cluster used to sit side-by-side and fight for space —
  short names left awkward whitespace on the right, long names
  pushed the right cluster off-edge and truncated the toggle
  button text ("Cards" clipping to "Car"). Now the name gets its
  own row and the cluster gets its own beneath it, both with the
  full column width to breathe.
- **Toggle buttons never truncate.** `.view-toggle button` gains
  `white-space: nowrap` + `flex-shrink: 0` so a cramped parent
  can't clip the label. The wrap-underneath behavior on the
  cluster now handles the tight-space case gracefully.
- Long thread names wrap inside the h1 rather than overflowing
  the column; h1 shrinks slightly on mobile (1.4rem → 1.25rem)
  to match the tighter feel.

## [0.4.49] — 2026-07-02

### Fixed
- **Writes to a tombstoned thread were silently accepted.** After a
  member sealed an ephemeral thread, the sidebar showed ⚰ and the
  compose banner said "tombstone card." But the pipeline routed new
  entries to the *main log* (because `is_ephemeral()` returned False
  post-seal — the check ORs on tombstoned_at being null), and
  `store.append_atomic` happily appended them as if the thread name
  had never been used. New posts landed at seq 1 in the main log
  next to the seq 0 tombstone. "The thread is sealed" was a lie.
  New rule: `store.is_tombstoned(t)` returns true iff the name has
  been sealed, and the pipeline rejects with
  `"thread T is tombstoned — no further writes accepted"` before
  seq allocation. One new pipeline regression test.
- Compose box is hidden client-side on tombstoned threads so a user
  doesn't type into a phantom input and get a bewildering error card
  on submit. The tombstone card at the top of the thread view is now
  the entire story.

## [0.4.48] — 2026-07-02

### Added
- **Group ephemeral threads.** `audience` is now an allowed entry
  kind inside ephemeral threads. Previously the pipeline rejected
  it alongside real governance kinds, which made "recital chat
  with just three people that expires next week" structurally
  impossible. Audience is per-thread routing, not governance —
  the entry lives in the ephemeral log and dies with the thread.
  New-thread dialog: pick both "Ephemeral" and "Just these
  people" freely; the client now posts the audience entry into
  the newly opened ephemeral thread after `POST /threads/ephemeral`.
- **`thread_opened` WebSocket broadcast** on `POST
  /threads/ephemeral`. Connected clients see the new thread appear
  in their sidebar without a manual /threads refresh. Client also
  now detects unknown-thread pushed entries (someone else created
  a permanent thread) and triggers a `/threads` reload so those
  appear too.

### Fixed
- New-thread dialog no longer silently overrides your audience
  selection when you flip to "Ephemeral." The prior behavior set
  `d.scope = 'public'` and cleared the selected pubkeys on the
  ephemeral radio's onchange handler, so if you picked members
  and then toggled ephemeral (or vice versa in click order), the
  UI showed your picks but the backend created a public thread.

## [0.4.47] — 2026-07-02

### Fixed
- PWA sidebar version footer showed nothing. Root cause:
  `tauri.ts::appVersion()` returned null in browser mode (no Tauri
  shell to ask), and the ThreadList footer's `{#if app.appVersion}`
  never fired. Now Vite injects `clients/web/package.json`'s version
  at build time as the PWA fallback; Tauri desktop still calls the
  runtime `getVersion()` which is authoritative for the installed
  bundle. Both surfaces render the version now.

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

[0.4.56]: https://github.com/cloudseeder/cove/releases/tag/v0.4.56
[0.4.55]: https://github.com/cloudseeder/cove/releases/tag/v0.4.55
[0.4.54]: https://github.com/cloudseeder/cove/releases/tag/v0.4.54
[0.4.53]: https://github.com/cloudseeder/cove/releases/tag/v0.4.53
[0.4.52]: https://github.com/cloudseeder/cove/releases/tag/v0.4.52
[0.4.51]: https://github.com/cloudseeder/cove/releases/tag/v0.4.51
[0.4.50]: https://github.com/cloudseeder/cove/releases/tag/v0.4.50
[0.4.49]: https://github.com/cloudseeder/cove/releases/tag/v0.4.49
[0.4.48]: https://github.com/cloudseeder/cove/releases/tag/v0.4.48
[0.4.47]: https://github.com/cloudseeder/cove/releases/tag/v0.4.47
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
