# Cove — Hub Specification

**Protocol:** VNTP (Verifiable Notice Transfer Protocol)
**Scope:** one organization (LWCCOA pilot). One hub.
**Status:** Draft 0.1
**Companion:** `client-spec.md` (references the data model and wire protocol defined here)

This document is the protocol authority. It defines the canonical data model, the wire protocol, and the hub's responsibilities. The client spec consumes it.

---

## 1. Role and trust model

The hub is a single, organization-run (or ProveXa-hosted) store-and-forward server. It is an **accountable hub**, not merely a dumb one: it cannot forge participant content, and its own operational claims are signed and therefore non-repudiable.

- **Cannot forge participant content.** The hub holds **no member or root private keys**. It cannot forge an entry, fake a receipt, impersonate the board, or alter a signed object without detection, because every object is content-addressed and signed by a participant key, and clients verify independently. (This rests on the root key living with the board, offline, never on the hub — see §2.1.)
- **Holds its own operational key.** The hub has a dedicated **hub keypair** (Ed25519), distinct from any participant or root key, used only to sign its operational claims: sequence assignments and signed tree heads (§6.4). This converts the hub's linearization from "trusted" into "accountable" — if the hub lies about ordering or history, its own signature proves it lied.
- **Trusted for availability only.** If the hub is down, messages are delayed, not lost or forged.
- **Residual powers of a malicious hub — stated honestly.** Even with the tamper-evident log (§6.4), a malicious hub retains two capabilities it is important not to pretend away: it can **withhold** (refuse to fan out an entry — censorship of something a client never saw), and absent cross-client gossip it could attempt to **equivocate** (show different members different histories). The tamper-evident log makes *rewriting, reordering, and deletion of already-accepted entries* detectable, and — with receipt-carried tree heads (§6.4.3) — makes *equivocation* detectable. It does **not** make withholding of a never-delivered entry provable by the victim. This residual is acceptable for a single organization and is a primary motivation for eventually distributing the hub (deferred, §12).
- **Authoritative for two conveniences, now verifiable:** the per-thread **sequence** it assigns and the **overview index** it builds (§6). Clients accept these in the pilot but, via the tamper-evident log, can verify the sequence is append-only and that any entry claimed-present truly is. Making the *index* verifiable to untrusted external parties remains the cross-org problem (deferred, §12); the tamper-evident log is the shared machinery that gets pulled forward to serve both that future need and the present malicious-hub threat.

---

## 2. Identity and directory

### 2.1 Keys
- Each participant holds an **Ed25519** signing keypair. The public key is the participant's stable identity.
- An **X25519** encryption keypair is defined but **not used in v1** (sign-only; see §10.2).
- The organization holds a **root Ed25519 keypair** — the trust root. It signs attestations and may sign broadcasts. Custody is a governance decision (single custodian + documented succession for the pilot; revisit multi-sig before any expansion).

### 2.2 Attestation
The organization binds a participant's key to a directory identity by signing an attestation:

```
attestation = {
  member_pubkey:  <ed25519 pubkey>,
  enc_pubkey:     <x25519 pubkey | null>,     // null in v1
  display_name:   "Jane Smith",
  unit:           "Unit 207",
  role:           "member" | "board" | "officer",
  issued_at:      <rfc3339>,
  expires_at:     <rfc3339 | null>,
  issuer:         <root pubkey>,
  sig:            <root signature over canonical(attestation minus sig)>
}
```

### 2.3 Directory
The membership roll **is** the keyserver. The hub hosts the directory as a signed manifest:

```
directory = {
  org:           <root pubkey>,
  attestations:  [ <attestation>, ... ],
  revocations:   [ { pubkey, revoked_at, reason }, ... ],
  updated_at:    <rfc3339>,
  sig:           <root signature over canonical(directory minus sig)>
}
```

- Served **authenticated** (contains PII + keys). Access requires a valid member signature (§5).
- Clients verify the root signature over the whole manifest, then trust individual attestations.
- **Revocation** (member departs, sells unit, key compromise) is a signed entry. Clients check revocation before trusting any author. Events signed *before* a key's revocation remain verifiable against the historical attestation.

> Recovery: a member who loses a key re-onboards; the organization re-attests a new keypair and revokes the old. The organization is the recovery authority. This is why the single-org context dissolves the key-recovery problem that killed PGP.

---

## 3. Entry model — the thread log

The atomic unit is an **entry**, not a message — VNTP transfers entries. A thread is a DAG of entries. Everything — notices, posts, replies, edits, attachments, membership changes, receipts — is an entry.

```
entry = {
  id:           <multihash: sha256 of canonical(content)>,   // content address
  thread:         <entry id of thread root>,             // root: thread == id
  author:       <ed25519 pubkey>,
  parents:      [ <entry id>, ... ],     // causal DAG edges; root: []
  kind:         "notice" | "post" | "reply" | "supersede" | "membership" | "receipt" | "revoke"
              | "branch" | "archive" | "reopen" | "audience" | "tombstone",
  created_at:   <rfc3339>,               // advisory only; causal order from DAG, not clock
  body:         <utf-8 / markdown>,      // may be empty
  blobs:        [ { hash, media_type, size, name }, ... ],   // content-addressed refs
  supersedes:   <entry id | null>,       // for edits/revisions
  audience:     { pubkeys: [<pubkey>, ...] } | null,   // §3.4 — only for kind="audience"
  sig:          <ed25519 sig over canonical(content)>
}
```

where `content` = the entry with `id` and `sig` removed.

### 3.1 Canonical serialization
- Use **JCS (JSON Canonicalization Scheme, RFC 8785)** for v1 — deterministic, debuggable. (CBOR/dCBOR is a valid later optimization; JCS chosen for pilot legibility.)
- `id = "sha256:" + hex(sha256(JCS(content)))`.
- `sig = Ed25519_sign(author_priv, JCS(content))`.
- Verification: recompute `id` from `content` and confirm it matches; verify `sig` over `JCS(content)` against `author` resolved through the directory (and not revoked).

### 3.2 Threads and membership
- A **thread** is the set of entries sharing a `thread` root, plus its participant set and policy.
- Participant set and ACL are expressed as **`membership` entries** (signed, in the log) → fully auditable. A membership entry names who is added/removed and who may add others.
- Root entry of a thread declares: `title`, initial participants, and a `broadcast` flag. A board notice is a root entry of `kind: "notice"` with `broadcast: true` (one-to-all-members) — the headline case that gives VNTP its name.

### 3.3 Edits and revisions
- An edit/revision is a new entry with `supersedes` pointing at the prior entry (and typically a new blob hash for a revised document). Nothing is mutated in place. The log is append-only; history is preserved; the superseding chain is itself auditable. (ARC drawing v1→v2→v3 = three entries, three blobs.)
- **Authorization (v0.5.3)**: a `kind="supersede"` entry MUST (a) name a real prior entry via `supersedes`, (b) live in the same `thread` as its target, and (c) share the same `author` as its target. Rejected with structured `reason` on the write side (`supersede_missing_target`, `supersede_target_unknown`, `supersede_wrong_thread`, `supersede_wrong_author`). Without (c) anyone could rewrite anyone else's posts; that would gut the accountability that makes the log useful.
- **Empty content refusal (v0.5.3)**: a `post`/`reply`/`notice`/`supersede` MUST have a non-whitespace body OR at least one blob reference. A body of `""` with an empty `blobs` list is rejected with `reason="empty_body"`. Attachment-only entries (a PDF share with no commentary) remain legitimate. Retracting a message is a supersede with an explicit body like `"[retracted]"`, not a supersede with an empty body.
- **Rendering (v0.5.3)**: clients render the newest supersede's body over the original in the chronological feed and surface an "edited" affordance that reveals every prior version with its timestamp. The log carries every version regardless — display collapses, audit doesn't.

### 3.4 Audience-declaration entries (`kind="audience"`) — v0.4.27 + v0.5.0

An **audience-declaration entry** scopes a thread's visibility to a closed pubkey list. A thread with no audience entry is public to every attested member; a thread with any audience entry is scoped, and only in-audience members can `/sync`, appear in `/threads`, or receive `/stream` pushes for it.

**Wire shape.** `kind="audience"`, `body=""`, `audience.pubkeys` is the **full replacement list** — not a diff. The new audience is exactly the pubkeys named, replacing whatever came before. The hub derives per-entry (added, removed) by comparing against the prior accepted audience at the entry's seq; clients compute the same diff for rendering.

**Write-side authorization rules.** For a submitted audience entry with proposed list `new`, let `old` be the current audience computed by walking prior accepted audience entries in seq order:

1. **Bootstrap.** If the thread has no prior audience entry (i.e. `old` is undefined), `new` establishes the scope. Any attested, non-revoked author is accepted. Preserves the "any member can scope a public thread" property.
2. **Author-in-audience.** Otherwise the author MUST be in `old`. Otherwise reject with `reason="not_in_audience"`. Governance authority (rule 3) is exercised **from within** the thread — a board or officer member NOT in the current audience cannot post an audience change against it; they must be added first (which any current member may do under rule 4).
3. **Removal-of-other requires `manage_audience`.** Let `removed = old − new`. If `removed − {author}` is non-empty, the author MUST hold the `manage_audience` capability (default: roles `board` + `officer`). Otherwise reject with `reason="removal_requires_manage_audience"`.
4. **Additive changes** (`new ⊇ old`) — accepted for any in-audience author.
5. **Self-leave** (`removed = {author}`, plus any additions) — accepted for any in-audience author regardless of capability.

The write-side gate lives in the acceptance pipeline (§7.1). Rejection is a structured `400 {error: "rejected", reason: "<slug>"}` — never a silent accept-and-ignore. The read-side (`store.thread_audience`) still applies rules 1–3 as defense-in-depth so a hub bug can't smuggle an unauthorized change past the read layer.

**Sync grace period for removed members.** A member who was in the audience and is removed at seq N retains `/sync` visibility of the thread up to and INCLUDING seq N — the audience entry that removed them. `/threads` and `/inbox` continue to surface the thread to them with an out-of-band `removed_at_seq: N` field so their client can render "You were removed at ... by ..." rather than the thread silently disappearing. Anything posted after seq N is invisible to them; the composer must hide.

**Rendering.** Clients MUST render audience changes as first-class entries in the thread stream (not a hidden governance-metadata kind), showing added/removed/left phrasing with the actor and timestamp. Ejection cannot be silent — that is the whole point of the write-side gate. Style is a client concern; the mandate is visibility.

**Capabilities.** `manage_audience` is a v0.5.0 capability, defined in `capabilities_by_role` on the DirectoryManifest (§2.3). The hardcoded default map (used when a manifest has no `capabilities_by_role` field) grants it to `board` and `officer`. Orgs override per role via the manifest.

---

## 4. Blobs

- **Content-addressed by sha256.** Events reference blobs by hash; bytes live in the blob store, never embedded in the log. This physically separates the tiny entry log from the heavy blob store (sim: log was 0.24% of total storage).
- **Dedup within the organization** by hash. (Realistic dedup ≈ 35% — revisions don't dedup because the bytes differ; only re-shares collapse. Do not over-promise dedup.)
- **Tiering and retention:** bytes may be tiered hot→cold or expired per policy, **but the entry retains the hash and metadata permanently.** Cryptographic proof that a file existed and what it was survives at near-zero cost even after the bytes are tiered to the cheapest storage.
- **Encryption:** v1 stores blobs as-is (sign-only). Client-side encryption (provider holds only ciphertext, hash is of ciphertext) is designed-for but deferred (§10.2).
- **Integrity:** the hub stores bytes addressed by hash; any client re-hashes on download to detect tampering. The hub cannot substitute blob content undetected.

---

## 5. Authentication

- **No passwords.** The keypair is the credential.
- **Challenge-response:** client requests a nonce; hub issues a short-lived nonce; client signs it; hub verifies against the directory (and checks not revoked); hub issues a session token (bearer, short TTL) for subsequent calls.
- All authenticated endpoints require a valid session token bound to a non-revoked attested key.

---

## 6. Indexing / overview — the operational core

The simulation showed naive thread reconstruction (scan the whole log to assemble one thread) grows linearly with total log size — 0.7 ms → 78 ms as the log grows to 1M entries — while an index keeps it sub-millisecond. **The hub MUST maintain an overview index.** This is the modern descendant of the NNTP overview (NOV) database; the operational burden did not vanish, it relocated from spool-and-expiry management to index coherence.

The hub maintains, per thread:
- **`seq`** — a monotonic per-thread sequence number assigned at acceptance (local linearization). Used for delta-sync and cumulative receipts. (Analogous to NNTP article numbers / high-water marks.)
- **child map** — parent→children edges, for O(thread) threaded rendering.
- **causal order** — for flat (chat-like) rendering.
- **derived ledger** — see §8.

Index integrity rule: the index is **rebuildable from the raw signed entry store**. The entry store is the source of truth; the index is derived and disposable. If the index is lost or suspected stale, it is rebuilt from entries. (At pilot scale the entire log is single-digit MB and rebuilds in seconds; even a 25,000-tenant federation's combined logs are ~67 GB — index fits in memory.)

### 6.4 Tamper-evident log (verifiable append-only history) — **v1**

*Promoted from deferred.* The adversary analysis showed this defends against a malicious-or-compromised hub **and** is the same machinery the deferred cross-org verifiable index needs. Two independent threats, one mechanism — so it is pulled forward into v1 core. It is cheap: hashing plus periodic signing, with O(log n) proofs that are trivial at pilot scale.

The hub maintains an **append-only Merkle log** over accepted entries (global acceptance order), in the manner of a Certificate-Transparency-style verifiable log.

#### 6.4.1 Structure
- Every accepted entry's record commits to the **prior log head**: each stored entry includes `prev_log_hash`, forming a hash chain. The leaf hashed into the Merkle tree is the entry `id` (already a content hash) plus its assigned `seq`.
- The hub periodically (and on demand) publishes a **Signed Tree Head (STH)**:
```
sth = {
  tree_size:     <n>,                 // number of entries committed
  root_hash:     <merkle root>,
  prev_sth_hash: <hash of previous STH>,
  timestamp:     <rfc3339>,
  hub_key:       <hub pubkey>,
  sig:           <hub signature over the above>   // hub operational key, §1
}
```
- Because the STH is signed by the **hub operational key**, the hub's claim about history is non-repudiable. A hub that later presents a contradictory history is provably caught.

#### 6.4.2 Proofs
- **Inclusion proof** (`GET /proof/inclusion?entry={id}`): lets a client verify a given entry is committed in the log at the claimed position under a given STH. A hub that accepted an entry (and acked it) cannot later pretend it never existed.
- **Consistency proof** (`GET /proof/consistency?from={size}&to={size}`): lets a client verify the log only **grew** between two STHs — no rewrite, no reorder, no deletion of already-committed entries. This is the append-only guarantee, verified rather than trusted.

#### 6.4.3 Equivocation / split-view detection
A single hub could still try to show different members different histories. Detection rides on the **receipt** mechanism that already exists (§8): a client's cumulative receipt **carries the `(tree_size, root_hash)` of the latest STH it observed**. Because receipts are themselves signed entries that fan out, members (and the board) cross-check observed tree heads. **Two valid STHs at the same `tree_size` with different `root_hash` are cryptographic proof the hub equivocated.** Full gossip enforcement is a small additive step (**v1.1**); the receipt-carries-STH field ships in **v1** so the evidence is captured from day one.

#### 6.4.4 What it does and does not defend
- **Defends (detectable):** rewriting, reordering, or deleting already-accepted entries; falsely denying an entry's inclusion after acking it; equivocating about history (via 6.4.3).
- **Does NOT defend:** **withholding** — a hub can still refuse to ever deliver an entry, and the intended recipient cannot prove the absence of something they never saw. (A `seq` gap in a thread is a *hint*, not a proof.) Eliminating withholding requires more than one hub, which is why distributing the hub remains the eventual answer (deferred, §12).

#### 6.4.5 Relationship to `seq` and the index
The tamper-evident log upgrades §1's "sequence is trusted" caveat to "sequence is **verifiable and append-only**." The overview index remains a derived convenience, but its inputs (the entries and their order) are now tamper-evident, so a client rebuilding the index to check the hub has a signed, provable basis to check against.

---

## 7. Wire protocol (client ↔ hub)

Transport: **HTTPS** for request/response, **WebSocket** for push. Push (not poll) is mandatory so clients behave like a messaging app, not a portal — the client is notified, it does not have to remember to check.

| Method / path | Purpose |
|---|---|
| `POST /auth/challenge` | Get a nonce |
| `POST /auth/verify` | Submit signed nonce → session token |
| `GET  /directory` | Signed directory manifest (attestations + revocations) |
| `POST /entries` | Submit signed entry(s). Hub validates, assigns `seq`, persists, fans out |
| `GET  /sync?thread={id}&since={seq}` | Delta-sync: entries in thread after `seq` |
| `WS   /stream` | Subscribe; receive pushed entries for the member's threads |
| `GET  /overview?thread={id}` | Precomputed thread structure (child map + seq order) for fast render |
| `POST /blobs` | Upload blob (content-addressed); returns hash |
| `GET  /blobs/{hash}` | Download blob bytes |
| `GET  /ledger?entry={id}` | Delivery ledger for a message: who has acked, who has not |
| `GET  /sth` | Latest Signed Tree Head of the tamper-evident log (§6.4) |
| `GET  /proof/inclusion?entry={id}` | Inclusion proof for an entry under the current STH (§6.4.2) |
| `GET  /proof/consistency?from={size}&to={size}` | Append-only consistency proof between two STHs (§6.4.2) |
| `POST /admin/attest` | (root/admin) issue an attestation — signs with root key |
| `POST /admin/revoke` | (root/admin) revoke an attestation |
| `POST /admin/limits` | (root/admin) set per-identity throttle/quota overrides (§7.2) |

### 7.1 Entry acceptance pipeline (`POST /entries`)
1. **Structural sanity bounds (cheap, pre-auth).** Reject before doing real work if the payload violates hard bounds (§7.2.1): oversized entry, too many `parents`, too many/oversized `blobs`. Cheap to check, and stops malformed/adversarial payloads before they cost anything.
2. Resolve `author` in directory; reject if unknown or revoked.
3. Recompute `id` from `content`; reject on mismatch.
4. Verify `sig` over `JCS(content)` against `author`; reject on failure.
5. **Per-identity throttle / quota (§7.2).** Now that `author` is known and proven, apply this identity's rate limit (token bucket) and storage quota. If exceeded, reject with a structured throttle response (`429`-style, §7.2.3) — the entry is *not* persisted. Repeated/sustained violations raise an admin alert (§7.2.4).
6. Check ACL: author is a participant permitted to post to `thread` (per `membership` entries); reject otherwise. For `kind="audience"` specifically, apply the write-side gate in §3.4 (rules 1–5) — structured `400 {reason: "not_in_audience" | "removal_requires_manage_audience"}` on violation. (`membership`-kind ACL for non-audience posts remains a future slice.)
7. Verify `parents` exist in the store (or are accepted concurrently); reject dangling parents.
8. Assign per-thread `seq`; persist to append-only entry store; **extend the tamper-evident log** (§6.4) and update the STH.
9. Update overview index and (if `kind=receipt`) the ledger.
10. Fan out to connected participants via `/stream`; queue for offline participants.

The hub performs **no content moderation and makes no trust decision** beyond ACL, signature validity, structural bounds, and rate/quota. There is no spam score. Origin is binary and proven; throttling is mechanical and identity-keyed, not judgmental.

### 7.2 Per-identity throttle and quota layer

The historical reminder is the whole point: NNTP's original abuse resistance was an *accident of scarcity* — slow pipes and expensive disk throttled abuse for free, and the model collapsed when scarcity lifted (and NNTP later had to bolt throttling on: `ctlinnd throttle`, streaming flow control, per-feed rate limits). We have abundance and we deliberately removed the heuristic layer ("origin is binary"). So the throttle that bandwidth used to provide must now live **in the protocol, keyed to the attested identity**. It does not decide *whether* an identity is bad — accountability and revocation do that (§7.2.4). It caps the **blast radius** an identity can cause in the seconds-to-minutes before a human responds.

This layer answers the **attested-insider** threat: a valid member whose every entry renders as "verified origin" and who therefore cannot be stopped by authentication, only bounded by quota and ended by revocation.

#### 7.2.1 Hard structural bounds (apply to every entry, all identities)
Sanity limits that bound algorithmic-complexity attacks (adversarial DAG shapes) and oversized payloads. Defaults (org-configurable):
- `max_entry_bytes` — e.g. 256 KB for the entry record (excluding referenced blobs).
- `max_parents` — e.g. 32. Bounds fan-in and the cost of parent validation / DAG traversal.
- `max_blobs_per_event` — e.g. 16.
- `max_blob_bytes` — e.g. 100 MB per blob (org policy).
These are checked at pipeline step 1, before authentication, and are the cheapest line of defense.

#### 7.2.2 Per-identity rate and volume (token bucket)
Each identity has a token bucket with a **sustained rate** and a **burst** allowance, plus a rolling **volume cap** and a **storage quota**. Limits are **role-differentiated**, because a board broadcast legitimately fans to all members while a member posting hundreds of entries/minute is almost certainly malfunctioning or malicious. Illustrative defaults:

| Tier | entries/min (sustained) | burst | bytes/day | blob storage quota |
|---|---|---|---|---|
| member | 20 | 60 | 50 MB | 2 GB |
| officer | 60 | 120 | 200 MB | 10 GB |
| board / broadcast | 120 | 300 | 1 GB | 50 GB |

- **Rate** smooths sustained flooding while allowing normal bursts (a member catching up on a thread).
- **Volume (bytes/day)** bounds a slow-drip large-payload attack that stays under the rate limit.
- **Storage quota** bounds the expensive half — deliberately-unique large blobs aimed at the blob store (which content-addressing will not dedup). On quota exhaustion, new blob uploads are rejected until the identity frees space or an admin raises the quota.
- Limits are overridable per identity via `POST /admin/limits` (e.g. temporarily raise the board's limit for an annual-meeting mailing), and overrides are logged as signed admin entries for auditability.

#### 7.2.3 What a throttled client sees
A structured rejection, not a silent drop (consistent with the no-silent-failure principle):
```
{ error: "throttled",
  scope: "rate" | "volume" | "storage" | "structural",
  limit: <the limit hit>,
  retry_after_s: <seconds, for rate/volume>,   // absent for storage/structural
  detail: "human-readable" }
```
- `rate`/`volume` → transient; the client backs off and retries after `retry_after_s` (queued locally, §client-spec).
- `storage` → persistent until space is freed or quota raised.
- `structural` → permanent; the entry is malformed and must not be retried as-is.

#### 7.2.4 Interplay with revocation — throttle bounds, governance ends
Throttling is *not* the enforcement layer. It is the circuit breaker that limits damage while a human decides. The actual response to a member who is abusing a valid identity is **accountability + revocation**: every abusive entry is signed and attributable, so the board can identify the actor, revoke the attestation (§2.3), and apply whatever the governing documents allow. The hub should therefore:
- Emit an **admin alert** when an identity sustains limit violations beyond a threshold (candidate for board review).
- Optionally apply **escalating auto-throttle** (progressively tighter limits on repeat violation) to buy time — but **never auto-revoke**, because revocation is a governance act, not an automated one.

This is the single-org analogue of the toll/reputation machinery deferred for strangers: *inside the org, the board is the reputation system, and revocation is the slash.*

---

## 8. Delivery ledger

The feature the duopoly structurally cannot offer.

- A **receipt** is an entry (`kind=receipt`) authored by the recipient, signed, acking message(s) received and validated.
- **Cumulative by default.** Per the simulation, one-receipt-per-message made receipts 68% of all entries. A receipt therefore acks a **high-water `seq`** per thread: *"I have received and validated all entries in thread C through seq N,"* collapsing many acks into one per sync (TCP-cumulative-ack / NNTP-high-water style). Per-entry receipts remain possible for cases needing message-specific proof, but are not the default.
- The hub derives, per message, the set of `(recipient, acked_at)` — the **ledger**.
- **Non-delivery is surfaced, not hidden.** `GET /ledger` returns both who has acked and **who has not**. The board sees exactly which members have not received a notice and can fall back (paper, phone). Email's silent void becomes an actionable list.
- **Notice-validity is out of protocol scope.** The ledger is strong evidence; whether it satisfies the LWCCOA Declaration's notice provisions is a governance/legal question to confirm against the governing documents. The protocol produces the proof; the board pairs it with what the documents require.

---

## 9. Storage

| Store | Contents | Notes |
|---|---|---|
| Entry store | append-only signed entries, indexed by `(thread, seq)` and by `id` | source of truth |
| Tamper-evident log | hash chain + Merkle tree over accepted entries; STH history | hub-signed; serves inclusion/consistency proofs (§6.4) |
| Blob store | content-addressed bytes | dedup within tenant; hot/cold tiering; metadata retained after byte expiry |
| Directory store | current signed directory manifest + history | root-signed |
| Index/overview | child maps, seq order, ledger | **derived; rebuildable from entry store** |
| Throttle state | per-identity token buckets, volume/storage counters, limit overrides | operational; transient counters + logged admin overrides (§7.2) |

---

## 10. Hub configuration decisions (pilot)

1. **Encryption:** off in v1 (sign-only). X25519 fields reserved.
2. **Root-key custody:** single custodian + documented succession. *Note: the adversary analysis strengthens the case for **multi-sig on the root key** — it converts a single rogue/compromised custodian into a requirement for collusion. Recommended to revisit before any expansion.*
3. **Hub operational key (§1):** generate and protect the hub keypair used to sign `seq` claims and STHs. Distinct from the root key; loss/rotation invalidates old STH signatures, so plan rotation with overlap.
4. **Hosting:** organization-run vs. ProveXa-hosted (operational choice).
5. **Blob retention/tiering policy:** define hot window and cold/expiry rules; always retain hash+metadata.
6. **Receipt policy:** cumulative high-water acks default; per-entry receipts opt-in; receipts carry observed STH (§6.4.3).
7. **Throttle/quota defaults (§7.2):** ratify the per-tier rate, volume, storage limits and structural bounds for the organization; define the admin-alert threshold and whether escalating auto-throttle is enabled.
8. **STH cadence:** how often the hub publishes a Signed Tree Head (e.g. every N entries and/or every M minutes).

---

## 11. Deliberately deferred (designed-for, not built)

Per scope discipline, these are documented as dormant with the seam that must stay clean and the trigger that wakes each.

| Deferred | Seam kept clean now | Wake trigger |
|---|---|---|
| **Inter-org communication** | entries/threads reference a `thread` root and authors carry an `issuer` (org root); cross-org would chain roots and route between hubs — but no routing, no foreign-root resolution in v1 | a second organization needs to participate in a shared thread |
| **Inter-org / cross-root trust** | directory is single-root; attestations name their `issuer`; cross-org would require root-to-root trust statements | accepting identities attested by a *different* org's root |
| **Verifiable index / verifiable history** | **Pulled forward to v1** as the tamper-evident log (§6.4) — promoted because it defends the single-org malicious-hub threat *and* is the same machinery cross-org needs. The remaining cross-org piece is only extending verification to *externally*-attested parties. | external orgs verifying a hub they don't operate |
| **Confidential messaging (encryption)** | X25519 fields reserved; blob store is content-agnostic | member↔member private threads (likely v1.1) |
| **Federation / multi-hub** | hub is the only authority for `seq`/fan-out; the tamper-evident log already constrains a single hub | more than one hub — *and the only real fix for hub **withholding** (§1, §6.4.4)* |
| **Toll / Sybil / reputation** | attestation issuance is the single trust gate; no self-registration path exists; throttle+revocation (§7.2) is the in-org analogue | any first contact from outside the attested directory |

v1 builds none of these. It must build nothing that *requires* them, and leave the named seams clean.

---

## 12. Build order (server)

1. Identity + directory: root key, **hub operational key**, attestation issuance (`/admin/attest`, `/admin/revoke`), signed directory manifest, `/directory`.
2. Auth: challenge-response (`/auth/*`).
3. Entry store + acceptance pipeline (`POST /entries`) with full validation **including structural bounds (§7.2.1)**; start with the broadcast/post path.
4. Overview index + `seq` assignment + `/sync` + `/overview`.
5. **Tamper-evident log (§6.4): hash chain + Merkle tree + STH signing + `/sth` + `/proof/*`.** Pull forward here, alongside `seq`, since it commits the same accepted-entry order.
6. **Per-identity throttle + quota (§7.2)** in the pipeline + `/admin/limits` + admin-alert hook.
7. WebSocket `/stream` fan-out (push).
8. Blob store + `/blobs` (content-addressed, dedup, tiering hooks, storage-quota enforcement).
9. Cumulative receipts (carrying observed STH) + ledger derivation + `/ledger` (including the who-has-NOT-acked view).

This yields a working accountable-notice channel at the broadcast path earliest, which is the first thing to put in front of the membership. The tamper-evident log and throttle are sequenced early (steps 5–6) because both are pipeline-level guarantees that are far cheaper to build in than to retrofit.
