# Changelog

All notable changes to Cove. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The client (`clients/web`) and hub (`src/cove`) ship on the same version — a tag
covers both.

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
