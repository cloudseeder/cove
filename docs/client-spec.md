# Cove — Client Specification

**Protocol:** VNTP (Verifiable Notice Transfer Protocol)
**Scope:** one organization (LWCCOA pilot).
**Status:** Draft 0.1
**Companion:** `server-hub-spec.md` — defines the canonical data model, entry schema, and wire protocol. This document references it and does not redefine it.

The client's job is to hold keys, create and sign entries, verify everything it receives, render the log in the view the reader wants, and emit receipts. It trusts the hub for availability and index, the directory root for identity, and **no one for content integrity** — it verifies signatures itself.

It must behave like a messaging app, not a portal: **push-native** (notifications, first-class on the device), never poll-and-remember-to-check. The portal pattern — message lives in the org's silo, out-of-band email/SMS taps you on the shoulder to come read it — is the failure mode this whole design exists to avoid.

---

## 1. Key custody

- Generate the **Ed25519** signing keypair **on the device** at onboarding. (X25519 encryption keypair reserved; unused in v1.)
- The **private key never leaves the device** and is never sent to the hub.
- Store private keys in the platform secure store: Keychain / Secure Enclave (Apple), Keystore (Android), DPAPI or OS keyring (desktop). The keypair *is* the login; protect it accordingly.
- All authentication is challenge-response (server-hub-spec §5): sign the hub's nonce; receive a session token. No passwords anywhere.

---

## 2. Lifecycle flows

### 2.1 Onboarding (issuance)
1. Generate keypair on device.
2. Submit the public key to the organization through an **out-of-band-verified** step — the board confirms the person is a real member (admin enters/approves at membership; this is the human trust gate). 
3. The organization issues a signed attestation (server-hub-spec §2.2); the client caches the directory.
4. The member never "manages keys." They onboard once.

### 2.2 Recovery
- Lost device/key → re-onboard. The board re-attests a new keypair and revokes the old (server-hub-spec §2.3).
- The client handles this gracefully: entries authored under the old key before its revocation **still verify** against the historical attestation. Identity continuity is preserved; only the active key changed.

### 2.3 Rotation
- Member generates a new pair, requests a fresh attestation, old key revoked. Same handling as recovery.

---

## 3. Creating entries

To post, reply, attach, edit, or change membership, the client constructs an entry per server-hub-spec §3, then:

1. Assemble `content` (all fields except `id`, `sig`).
2. Compute `id = "sha256:" + hex(sha256(JCS(content)))`.
3. `sig = Ed25519_sign(priv, JCS(content))`.
4. For attachments: upload bytes to `POST /blobs` first (content-addressed), then reference the returned hash in `entry.blobs`.
5. Submit via `POST /entries`.

Patterns:
- **Board broadcast** (the killer case): root entry with `broadcast=true`, participants = all current members. Signed once by the board/root identity; the hub fans it out. Every member's client verifies it came from the board.
- **Reply:** entry with `parents=[target entry id]`. The parent edge *is* the thread structure — no `In-Reply-To` header reconstruction.
- **Revision:** entry with `supersedes=[prior id]` and (usually) a new blob hash.

### 3.1 Handling throttle / quota responses

The hub may reject a submission with a structured throttle response (server-hub-spec §7.2.3) rather than a silent drop. The client handles each `scope` distinctly and **never silently discards** the user's content:

- `rate` / `volume` → transient. Queue the entry locally and retry after `retry_after_s` with backoff. Show an unobtrusive "sending…" state, not an error, unless retries persist.
- `storage` → persistent. Surface clearly: the identity is over its blob quota; the user must free space or request a raised quota. Outbound blob is held, not lost.
- `structural` → permanent. The entry violates a hard bound (oversize, too many parents/blobs). This is a client-side bug or an oversized attachment; surface a real error and do not blindly retry.

Hitting `rate`/`volume` under *normal* use should be rare; if a member hits it constantly, that is either a malfunctioning client or the throttle tier needs adjustment — both worth surfacing rather than hiding.

---

## 4. Sync and live push

- Maintain a per-thread **high-water `seq`** locally, plus the **last verified STH** (Signed Tree Head, server-hub-spec §6.4).
- On connect: `GET /sync?thread={id}&since={seq}` for each active thread — delta-sync only what's new.
- **Verify the tamper-evident log on sync.** Fetch `GET /sth`; verify the hub's signature on it; then `GET /proof/consistency?from={last_size}&to={new_size}` and verify the log only **grew** (append-only — no rewrite, reorder, or deletion of previously-seen entries). Store the new STH as the last verified head. A failed consistency proof is a hard alarm: the hub has tampered with history — surface it, do not silently proceed.
- Subscribe to `WS /stream` for live pushed entries. New entries arrive without polling; the client raises a native notification.
- **Offline:** queue outbound entries locally; submit on reconnect. Inbound caught up via delta-sync.
- This is what makes it feel like iMessage and not like logging into the HOA portal: the message comes to the member, on the device, now.

---

## 5. Verification (non-negotiable, on every entry)

For every received entry, before displaying it:

1. Resolve `author` in the cached directory; confirm an attestation exists and the key is **not revoked** (as of the entry's position).
2. Recompute `id` from `content`; reject on mismatch.
3. Verify `sig` over `JCS(content)` against `author`.
4. **Verify inclusion** in the tamper-evident log: `GET /proof/inclusion?entry={id}` and confirm the entry is committed under the current verified STH (server-hub-spec §6.4.2). This stops the hub from later denying an entry it accepted and acked.
5. Render an explicit **origin state**: *verified-from-board*, *verified-from-member (name/unit)*, or *unverified/rejected*. A board notice shows as cryptographically board-signed; a forgery cannot be shown as verified.

There is no spam score and no heuristic. Origin is binary and proven, or it is rejected.

For blobs: re-hash downloaded bytes; confirm the hash matches the reference before trusting/opening.

> Inclusion-proof checks can be batched/sampled for performance (verify on demand, or sample under live load) so long as every entry the client *acts on or relies upon as notice* is inclusion-verified. The consistency proof on each sync is the non-negotiable one.

---

## 6. Rendering — the render-time view choice

The same thread DAG renders two ways, chosen by the reader, not baked into the data:

- **Flat (chat-like):** order entries by causal/`seq` order. Optimized for immediacy — "what's the latest," low overhead. This is the messaging-app reading mode.
- **Threaded (structured):** walk `parents` edges into a tree. Optimized for organization — multi-party, multi-topic, document-heavy threads. This is the email/forum reading mode.

This resolves the immediacy-vs-organization tension that forces every existing tool to pick one and sabotage the other. Here it's a view toggle over one object.

- For speed, fetch `GET /overview?thread={id}` (the hub's prebuilt child map + seq order). Per the simulation, rendering from a built index is sub-millisecond where a naive scan grows with total log size.
- The client **may** independently rebuild the thread structure from raw synced entries to verify the hub's overview (the overview is derived/disposable per server-hub-spec §6). In the single-org pilot, trusting the hub's overview is acceptable; the verify-by-rebuild capability is what keeps the cross-org seam clean.

---

## 7. Receipts

- On receiving and validating new entries, emit a **cumulative receipt** (server-hub-spec §8): one signed entry acking the high-water `seq` per thread — *"received and validated thread C through seq N."*
- This is the default precisely because per-message receipts were 68% of all entries in the simulation. Cumulative acks collapse them to roughly one per sync.
- **Carry the observed STH.** Each receipt includes the `(tree_size, root_hash)` of the latest STH the client has verified (server-hub-spec §6.4.3). Because receipts fan out to other members, this turns the existing receipt traffic into the **equivocation detector**: if the hub ever showed two members different histories, their receipts will carry conflicting tree heads at the same size, which is cryptographic proof the hub split the view. The field ships in v1; active cross-checking/gossip enforcement is v1.1.
- Per-message receipts are available for cases that need message-specific proof, but are opt-in, not automatic.
- Read vs. delivered: the client emits **delivery** acks automatically (analogous to certified-mail delivery). **Read** receipts are off by default (privacy); enable only if the organization opts in.

---

## 8. UX surfaces

### 8.1 Member
- Thread list with unread/new indicators (driven by push).
- A thread view with a **flat ⇄ threaded toggle** (§6).
- Clear **origin verification** indication per message (§5).
- Compose / reply / attach. Attachments shown as first-class, persistent, named objects — not stream litter that scrolls away.

### 8.2 Board / admin
- **Broadcast composer:** compose a notice to all members, signed by the board/root identity.
- **Delivery-ledger view:** for any notice, who has received it and **who has not** (`GET /ledger`) — the actionable non-delivery list. This is the board's proof-of-notice and the answer to "I was never notified."
- **Member management:** issue and revoke attestations (`/admin/attest`, `/admin/revoke`), guarded by root-key custody. This is the onboarding/recovery authority in practice.

---

## 9. Form factor (pilot)

- Reach the membership fastest: **web + mobile** (mobile matters because push notifications are the whole point). A desktop/web admin surface for the board.
- Heavy client-side intelligence (local-LLM triage, the Manifest layer) is **out of scope** for v1 — a 40-household directory does not need it. The client's job in v1 is custody, verification, rendering, and receipts.

---

## 10. Trust posture (summary)

| The client trusts… | …for | …and verifies by |
|---|---|---|
| the directory root signature | identity / who is who | checking root sig over the directory manifest |
| the hub (availability) | fan-out, the overview index, *not being able to rewrite or hide history* | consistency + inclusion proofs against signed tree heads (§4, §5); rebuilding thread structure from raw entries |
| **no one** | content integrity & origin | recomputing `id` and verifying every `sig` itself |

The tamper-evident log narrows what "trusting the hub" means: the hub is still trusted to *deliver* (it can withhold), but it can no longer **rewrite, reorder, delete, or deny** an entry it accepted, nor **equivocate**, without the client catching it.

---

## 11. Deferred on the client (designed-for, not built)

Mirrors server-hub-spec §11. The client must not build anything requiring these, but should keep their seams clean:

- **Encryption / confidential threads:** X25519 reserved; v1 is sign-only.
- **Cross-org identities:** verification resolves authors against a single org root in v1; the attestation already carries `issuer`, so multi-root resolution is an additive change later — do not hardcode a single root in a way that blocks it.
- **Verifiable history:** **pulled forward to v1** — the client now verifies consistency + inclusion proofs against signed tree heads (§4, §5). The remaining deferred piece is only extending trust to *externally*-operated hubs/roots (cross-org).
- **Toll / first-contact-from-stranger UX:** there are no strangers in a single org; no such surface exists in v1.

---

## 12. Build order (client)

1. Key generation + secure storage + challenge-response auth.
2. Directory sync + verification (root sig, attestation resolution, revocation).
3. Entry creation/signing + `POST /entries`; broadcast/post path first; **throttle-response handling (§3.1)**.
4. Delta-sync + WebSocket push + native notifications.
5. **STH verification: fetch/verify signed tree heads + consistency proof on sync + inclusion proof on relied-upon entries (§4, §5).**
6. Rendering: flat first, then threaded toggle; wire up `/overview`.
7. Receipts (cumulative, **carrying observed STH**) + the board's delivery-ledger view.
8. Blob upload/download + hash verification + first-class attachment UI.

Matches the server build order so the two halves meet at each step, broadcast-notice path first. Step 5 (verifiable history) tracks server step 5, so the tamper-evidence is exercised end-to-end as soon as it exists rather than retrofitted.
