# Changelog

All notable changes to Cove. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The client (`clients/web`) and hub (`src/cove`) ship on the same version — a tag
covers both.

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
