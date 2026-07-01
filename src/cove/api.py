"""FastAPI app: wire protocol (read paths + POST /entries). Spec §7.

Routes map 1:1 to the spec's wire table. Push (WebSocket) is mandatory in v1
but lands in a later slice alongside the §5 auth module — the broadcast-notice
path goes through POST /entries + GET /sync first.

Handlers are thin: parsing + module dispatch + error mapping. All logic lives
in the modules below. The factory `create_app(deps)` wires the modules so tests
get their own isolated app and production wires real deps.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

from fastapi import (
    Body, Depends, FastAPI, Header, Query, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import crypto
from .auth import AuthError, AuthService
from .blobs import BlobStore
from .config import DEFAULT, HubConfig
from .entry import Audience, BlobRef, Entry, Receipt, verify_entry
from .identity import (
    Attestation, Directory, DirectoryManifest,
    InvalidManifestSignatureError, RevocationDroppedError,
    Revocation, StaleManifestError, hash_manifest,
    manifest_from_dict, manifest_to_dict,
)
from .index import Ledger, Overview
from .invites import Invite, InviteRegistry, InviteUnusable
from .pending import PendingRegistry
from .pipeline import AcceptanceError, Pipeline
from .store import EventStore
from .throttle import ThrottleError, Throttler
from .translog import TamperEvidentLog
from .translog_ephemeral import EphemeralTransLog


# ---- WebSocket fan-out (§7.1 step 10) -----------------------------------
class FanOut:
    """In-memory broadcast registry for the WS /stream push channel.

    Snapshot-then-send: the lock is held only long enough to copy the
    connection set, so a slow client cannot block register/unregister.
    Dead connections (the client went away mid-broadcast) are pruned on
    the next pass — a transient send failure doesn't bring down the whole
    fan-out, and the registry stays bounded.

    State is process-local, like throttle/session state (§9). Multi-process
    fan-out would need an external bus; deferred.
    """

    def __init__(self) -> None:
        # v0.4.27: store pubkey alongside each connection so audience-
        # scoped entry pushes can be filtered per-subscriber. Public
        # threads + directory_changed broadcasts still fan out to all.
        self._connections: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, pubkey: str = "") -> None:
        async with self._lock:
            self._connections[ws] = pubkey

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.pop(ws, None)

    async def broadcast(self, payload: dict,
                        audience_pubkeys: Optional[list[str]] = None) -> None:
        """Fan a payload to every registered subscriber, or — when
        `audience_pubkeys` is given — only to subscribers whose pubkey
        is in the list. `audience_pubkeys=None` means "public" (everyone)
        and is what every non-audience-scoped path passes."""
        async with self._lock:
            conns = list(self._connections.items())
        audience_set = set(audience_pubkeys) if audience_pubkeys is not None else None
        dead: list[WebSocket] = []
        for ws, pk in conns:
            if audience_set is not None and pk not in audience_set:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.pop(ws, None)

    def __len__(self) -> int:
        return len(self._connections)


# ---- minimal default app (so `uvicorn cove.api:app` doesn't fail) -------
# Production wires a real app via create_app(...) in a bootstrap script.
app = FastAPI(title="Cove Hub", version="0.1.0")


@app.get("/healthz")
def _healthz_default() -> dict:
    return {"status": "ok", "version": app.version}


# ---- factory ------------------------------------------------------------
def create_app(*, pipeline: Pipeline, store: EventStore,
               translog: TamperEvidentLog, overview: Overview,
               ledger: Ledger,
               directory: Optional[Directory] = None,
               directory_manifest: Optional[DirectoryManifest] = None,
               auth: Optional[AuthService] = None,
               throttler: Optional[Throttler] = None,
               fanout: Optional[FanOut] = None,
               blobs: Optional[BlobStore] = None,
               pending: Optional[PendingRegistry] = None,
               invites: Optional[InviteRegistry] = None,
               ephemeral_translog: Optional[EphemeralTransLog] = None,
               config: HubConfig = DEFAULT) -> FastAPI:
    """Build a FastAPI app with all deps captured in closures.

    `directory_manifest` is the signed wire form served by GET /directory.
    `directory` is the in-memory view used by /ledger to enumerate members.
    Both are optional — endpoints that need them return 503 when absent.

    The lifespan reconciles the in-memory translog against the on-disk store
    BEFORE serving the first request. The translog isn't persisted in the
    pilot (translog-notes §6 'don't over-engineer'); the store is canonical
    and the translog is derived. Without this reconcile, a process restart
    would serve /sth + /proof/* against an empty tree even though the store
    is non-empty — wrong proofs, not absent ones.
    """
    if fanout is None:
        fanout = FanOut()
    if pending is None:
        pending = PendingRegistry()
    if invites is None:
        invites = InviteRegistry()
    # /admin/limits applies overrides to the same Throttler the pipeline uses;
    # default to pipeline.throttler so existing callers don't need to pass it.
    if throttler is None:
        throttler = pipeline.throttler
    # Pipeline step 7 strict-checks blob references against the blob store
    # (client-spec §3 upload-before-post). If the caller passed a BlobStore
    # without wiring it to pipeline, do that now so the strict check works
    # end-to-end through POST /entries.
    if blobs is not None and pipeline.blobs is None:
        pipeline.blobs = blobs
    # v0.4.37: same shape for the ephemeral translog. Callers building
    # the API commonly pass the log here rather than into the Pipeline
    # constructor; wire it through so pipeline routing works end-to-end.
    if ephemeral_translog is not None and pipeline.ephemeral_translog is None:
        pipeline.ephemeral_translog = ephemeral_translog
    # Persist the seed manifest on the Directory so subsequent /directory
    # reads and /admin/* updates use a single source of truth.
    if directory is not None and directory_manifest is not None and directory.manifest is None:
        directory._manifest = directory_manifest                      # noqa: SLF001

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Reconcile every derived in-memory view with the on-disk source of
        # truth BEFORE serving the first request. Without this, a restart
        # would serve /sth, /overview etc. against empty in-memory state
        # while the store still has entries — wrong responses, not absent
        # ones. The store, blob store, and (when persistence is attached)
        # the directory chain handle their own durability; this loop
        # rebuilds the things that don't.
        translog.rebuild(store.iter_global())
        overview.rebuild(store.iter_overview_seed())
        # v0.4.37: rebuild each live ephemeral thread's per-thread log
        # from the store. Tombstoned threads are skipped — their
        # sealed STH lives in ephemeral_final_sths, not the log.
        if ephemeral_translog is not None:
            for rec in store.all_ephemeral():
                if rec["tombstoned_at"] is not None:
                    continue
                ephemeral_translog.open_thread(rec["thread"])
                ephemeral_translog.rebuild(
                    rec["thread"], store.iter_ephemeral_entries(rec["thread"]),
                )
        # v0.4.38: auto-seal background task. Fires every
        # ephemeral_seal_check_seconds; seals any live thread whose
        # created_at + ttl_seconds has elapsed. When the check
        # interval is 0 the loop is disabled (tests can drive the
        # seal manually via _seal_ephemeral_thread).
        seal_task: Optional[asyncio.Task] = None
        if (ephemeral_translog is not None
                and config.ephemeral_seal_check_seconds > 0):
            seal_task = asyncio.create_task(_auto_seal_loop())
        try:
            yield
        finally:
            if seal_task is not None:
                seal_task.cancel()
                try:
                    await seal_task
                except (asyncio.CancelledError, Exception):
                    pass

    api = FastAPI(title="Cove Hub", version="0.1.0", lifespan=lifespan)

    # ---- CORS: allow Tauri webview origins + dev ------------------------
    # Tauri webviews load from a custom origin that differs per platform:
    #   macOS:   tauri://localhost
    #   Linux:   http://tauri.localhost
    #   Windows: http://tauri.localhost (custom-protocol mode)
    # Plus the SvelteKit dev server origin for local browser testing.
    # Browsers refuse to surface response bodies from cross-origin requests
    # unless the response carries an explicit Access-Control-Allow-Origin
    # header naming the caller's origin. WebSocket connections are NOT
    # gated by CORS the same way, so /stream works regardless.
    api.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "http://localhost:1420",
            "http://localhost:5173",
            # v0.4.29: PWA installed from app.cove.oap.dev. Same hub
            # API at cove.oap.dev with a different origin — CORS has to
            # name it explicitly.
            "https://app.cove.oap.dev",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- auth gate (§5) -------------------------------------------------
    # Data routes require a valid session token bound to a non-revoked
    # attested key. Public routes:
    #   /healthz, /auth/*, /sth, /proof/*  — verification artifacts and the
    #   auth handshake itself stay accessible without a token (CT-style
    #   transparency: the head and proofs are intrinsically verifiable).

    def require_session(authorization: Optional[str] = Header(None)) -> str:
        if auth is None:
            raise _AuthRequired("auth not configured")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _AuthRequired("missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        pubkey = auth.resolve_session(token)
        if pubkey is None:
            raise _AuthRequired("invalid or expired token")
        return pubkey

    def require_capability(cap: str):
        """v0.4.25: auth + capability gate for admin-UI surfaces. Replaces
        the previous hardcoded `role == "board"` check; the role → caps
        map is now org-configurable via DirectoryManifest.capabilities_by_role
        (with a hardcoded default that preserves the LWCCOA shape: board
        gets admin + archive).

        Root-sig protection on mutation paths (/admin/attest, /admin/revoke,
        /admin/limits) remains independent — those checks are on the
        signing key, not the session's capability set.

        Members without `cap` get 403, not 401 — they're authenticated,
        just not authorized."""
        def _dep(caller: str = Depends(require_session)) -> str:
            if directory is None:
                raise _Forbidden("no_directory")
            if cap not in directory.caller_capabilities(caller):
                raise _Forbidden(f"capability {cap!r} required")
            return caller
        return _dep

    # Kept as a back-compat alias for any future-test or external call
    # site that hard-named the old gate; same semantics as
    # require_capability("admin").
    require_board = require_capability("admin")

    @api.exception_handler(_AuthRequired)
    async def _auth_required_handler(_request, exc: "_AuthRequired"):
        return _err(401, error="auth_required", reason=exc.reason)

    @api.exception_handler(_Forbidden)
    async def _forbidden_handler(_request, exc: "_Forbidden"):
        return _err(403, error="forbidden", reason=exc.reason)

    @api.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": api.version}

    # ---- POST /auth/challenge (§5) -------------------------------------
    @api.post("/auth/challenge")
    def auth_challenge():
        if auth is None:
            return _err(503, error="no_auth")
        return asdict(auth.issue_challenge())

    # ---- POST /auth/verify (§5) ----------------------------------------
    @api.post("/auth/verify")
    def auth_verify(body: dict = Body(...)):
        if auth is None:
            return _err(503, error="no_auth")
        pubkey = body.get("pubkey")
        nonce = body.get("nonce")
        sig = body.get("sig")
        if not (pubkey and nonce and sig):
            return _err(400, error="bad_request",
                        detail="pubkey, nonce, sig required")
        try:
            sess = auth.verify_and_issue_session(pubkey=pubkey, nonce=nonce, sig=sig)
        except AuthError as e:
            # The reason is included here for pilot debuggability; the auth
            # module docstring notes production may want to flatten this to
            # avoid leaking which check (nonce/sig/directory/revocation) failed.
            return _err(401, error="auth_failed", reason=str(e))
        return {"token": sess.token, "pubkey": sess.pubkey,
                "expires_at": sess.expires_at}

    # ---- POST /entries (§7.1) -------------------------------------------
    @api.post("/entries")
    async def post_entries(body: dict = Body(...),
                           _caller: str = Depends(require_session)):
        try:
            ev = _entry_from_dict(body)
        except (TypeError, ValueError) as e:
            return _err(400, error="bad_entry", detail=str(e))
        try:
            seq = pipeline.accept(ev)
        except AcceptanceError as e:
            return _err(400, error="rejected", reason=str(e))
        except ThrottleError as e:
            return _throttle_response(e)
        # §7.1 step 10: fan-out to live subscribers. Offline-queueing is the
        # client-side responsibility (delta-sync via GET /sync on reconnect).
        # v0.4.27: audience-scoped threads filter entry pushes to only
        # subscribers in the audience. Non-audience members never see
        # the push on /stream; they also can't /sync the thread later.
        audience = store.thread_audience(ev.thread)
        audience_pubkeys = (
            list(audience.pubkeys) if audience is not None else None
        )
        await fanout.broadcast({
            "type": "entry",
            "entry": _entry_to_dict(ev),
            "seq": seq,
        }, audience_pubkeys=audience_pubkeys)
        return {"id": ev.id, "seq": seq}

    def _caller_in_thread_audience(thread: str, caller: str) -> bool:
        """v0.4.27: is `caller` allowed to see entries from `thread`?
        Public threads (no audience entry ever) — always true. Audience-
        scoped threads — true iff caller's pubkey is in the audience
        pubkeys list. Hub-side enforcement, computed live so a freshly-
        posted audience entry takes effect on the next request."""
        audience = store.thread_audience(thread)
        if audience is None:
            return True
        return caller in (audience.pubkeys or [])

    # ---- GET /sync (§7) -------------------------------------------------
    @api.get("/sync")
    def get_sync(thread: str = Query(...), since: int = Query(...),
                 caller: str = Depends(require_session)) -> dict:
        # Each item shape: {"entry": <signed entry>, "seq": <per-thread seq>}.
        # Matches the WS push payload — clients write one merge / dedup path
        # over both channels (client-spec §4.1).
        # v0.4.27: audience-scoped threads return an empty entries list
        # to non-audience callers. The hub doesn't 404 the thread name
        # (that would leak existence); it just looks empty.
        if not _caller_in_thread_audience(thread, caller):
            return {"thread": thread, "since": since, "entries": []}
        entries = [
            {"entry": _entry_to_dict(ev), "seq": s}
            for ev, s in store.since_with_seq(thread, since)
        ]
        return {"thread": thread, "since": since, "entries": entries}

    # ---- GET /threads ---------------------------------------------------
    # Client-side navigation needs a list of what threads exist. Returns
    # [(thread_name, entry_count, latest_seq)] sorted by latest_seq desc.
    # Auth-gated like /sync; the directory authoritatively names members
    # but threads are open-namespace so this list reflects observed state,
    # not a registry.
    def _has_archive_capability(pubkey: str) -> bool:
        """v0.4.25: helper closure passed to store.archive_state_per_thread.
        Reads the live Directory so a freshly-updated capabilities_by_role
        map takes effect on the next request without a hub restart."""
        if directory is None:
            return False
        return "archive" in directory.caller_capabilities(pubkey)

    @api.get("/threads")
    def get_threads(caller: str = Depends(require_session)) -> dict:
        archived_threads = store.archive_state_per_thread(_has_archive_capability)
        audience_by_thread = store.threads_with_audience()
        # v0.4.38: ephemeral-thread lookup. Every row from
        # overview.thread_summaries() carries a `type` field:
        #   "permanent"  — default
        #   "ephemeral"  — live ephemeral thread; carries `expires_at`
        #   "tombstoned" — sealed ephemeral; carries `final_sth`
        eph_by_thread: dict[str, dict] = {
            rec["thread"]: rec for rec in store.all_ephemeral()
        }
        rows = []
        for t, n, s, parent in overview.thread_summaries():
            # v0.4.27: hide audience-scoped threads from non-audience
            # callers. Public threads stay visible to everyone.
            aud = audience_by_thread.get(t)
            if aud is not None and caller not in (aud.pubkeys or []):
                continue
            row: dict = {
                "thread": t, "entry_count": n, "latest_seq": s,
                "parent_thread": parent,
                "archived": archived_threads.get(t, False),
                "audience": (
                    {"pubkeys": list(aud.pubkeys)} if aud is not None else None
                ),
                "type": "permanent",
            }
            rec = eph_by_thread.get(t)
            if rec is not None:
                if rec["tombstoned_at"] is not None:
                    row["type"] = "tombstoned"
                    row["tombstoned_at"] = rec["tombstoned_at"]
                    row["final_sth"] = store.get_final_sth(t)
                else:
                    row["type"] = "ephemeral"
                    try:
                        expires = _add_seconds_iso(
                            _parse_iso(rec["created_at"]), rec["ttl_seconds"],
                        )
                    except ValueError:
                        expires = None
                    row["expires_at"] = expires
                    row["creator_pubkey"] = rec["creator_pubkey"]
            rows.append(row)
        # v0.4.38: also surface freshly-opened ephemeral threads that
        # have no entries yet — otherwise a just-created thread stays
        # invisible to /threads until someone posts. Overview's
        # thread_summaries() derives from entries; these are the ones
        # with entry_count == 0 that don't show up there.
        seen = {r["thread"] for r in rows}
        for thread, rec in eph_by_thread.items():
            if thread in seen:
                continue
            if rec["tombstoned_at"] is not None:
                continue  # tombstoned-but-never-had-entries is hidden
            try:
                expires = _add_seconds_iso(
                    _parse_iso(rec["created_at"]), rec["ttl_seconds"],
                )
            except ValueError:
                expires = None
            rows.append({
                "thread": thread, "entry_count": 0, "latest_seq": -1,
                "parent_thread": None,
                "archived": False, "audience": None,
                "type": "ephemeral",
                "expires_at": expires,
                "creator_pubkey": rec["creator_pubkey"],
            })
        return {"threads": rows}

    # v0.4.38: shared seal ceremony. Manual (POST
    # /threads/{T}/tombstone) and auto (TTL background task) both call
    # this. Under the ephemeral log's lock we:
    #   1) close_thread → final EphemeralSTH
    #   2) save_final_sth so the sealed head survives entry deletion
    #   3) mark_tombstoned so subsequent /entries no longer route here
    #   4) delete ephemeral entries (the leaves are gone; the
    #      commitment survives in the STH)
    #   5) publish the tombstone entry to the MAIN log via a direct
    #      store.append_atomic + translog.append (bypasses pipeline
    #      throttle/ACL — the entry was already verified at open time
    #      or manual-seal request time, and quota accounting for a
    #      hub-driven auto-seal isn't the creator's burden)
    #   6) broadcast thread_tombstoned to WS subscribers
    # Idempotent: calling twice returns the same seq/STH; the store
    # helpers use IF-NULL / INSERT OR IGNORE semantics.
    def _reconstruct_tombstone_entry(rec: dict) -> Entry:
        """Rebuild the stored tombstone Entry from its JCS content bytes
        + sig. Mirrors store._row_to_entry's shape but takes an
        ephemeral_threads row rather than an entries row."""
        import json
        content = json.loads(rec["tombstone_entry_content"])
        blobs_data = content.pop("blobs", []) or []
        blobs = [BlobRef(**b) for b in blobs_data]
        receipt_dict = content.pop("receipt", None)
        receipt = Receipt(**receipt_dict) if receipt_dict is not None else None
        audience_dict = content.pop("audience", None)
        audience = (
            Audience(**audience_dict) if audience_dict is not None else None
        )
        ev = Entry(blobs=blobs, receipt=receipt, audience=audience, **content)
        ev.id = crypto.content_id(ev.content())
        ev.sig = rec["tombstone_entry_sig"]
        return ev

    async def _seal_ephemeral_thread(
        thread: str, *, override_entry: Optional[Entry] = None,
    ) -> dict:
        """Run the seal ceremony. Returns a payload matching the
        WS thread_tombstoned event body (also returned by the manual
        endpoint). `override_entry` is a fresh tombstone Entry from a
        manual-seal request; if omitted, the pre-signed entry stored
        at open time is used."""
        if ephemeral_translog is None:
            return {"error": "no_ephemeral_log"}
        rec = store.get_ephemeral(thread)
        if rec is None:
            return {"error": "not_found"}
        if rec["tombstoned_at"] is not None:
            # Idempotent: return the already-sealed head so the caller
            # sees a consistent result on retries.
            final = store.get_final_sth(thread)
            return {
                "thread": thread,
                "tombstoned_at": rec["tombstoned_at"],
                "final_sth": final,
                "already_sealed": True,
            }

        publish_entry = override_entry or _reconstruct_tombstone_entry(rec)
        if not verify_entry(publish_entry):
            # Defense in depth: refuse to publish a tombstone whose
            # signature doesn't verify. If this fires the DB is corrupt.
            return {"error": "tombstone_signature_invalid"}

        # 1) freeze the ephemeral tree
        final_sth = ephemeral_translog.close_thread(thread)

        # 2) pin the sealed STH so it survives leaf deletion
        store.save_final_sth(
            thread=thread,
            tree_size=final_sth.tree_size,
            root_hash=final_sth.root_hash,
            prev_sth_hash=final_sth.prev_sth_hash,
            timestamp=final_sth.timestamp,
            hub_key=final_sth.hub_key,
            sig=final_sth.sig,
        )

        # 3) mark tombstoned BEFORE deleting entries so an in-flight
        # /entries POST that raced the seal sees is_ephemeral==False and
        # tries the main log, where it will 409 on the fresh tombstone.
        tombstoned_at = _now_utc().isoformat()
        store.mark_tombstoned(thread, tombstoned_at)

        # 4) delete the leaves; the sealed STH is the commitment
        store.delete_ephemeral_entries(thread)

        # 5) publish tombstone Entry to the main log. Bypasses pipeline
        # throttle/ACL — see comment on this block.
        seq = store.append_atomic(publish_entry)
        translog.append(publish_entry.id, seq)
        main_sth = translog.current_sth()
        overview.add(
            publish_entry.thread, publish_entry.id, publish_entry.parents, seq,
        )

        payload = {
            "thread": thread,
            "tombstoned_at": tombstoned_at,
            "final_sth": store.get_final_sth(thread),
            "tombstone_entry": {
                **publish_entry.content(),
                "id": publish_entry.id,
                "sig": publish_entry.sig,
            },
            "main_seq": seq,
            "main_sth": asdict(main_sth),
        }

        # 6) fan out. Wrap in a try — fanout push failure must not
        # roll back the durable seal.
        if fanout is not None:
            try:
                await fanout.broadcast({
                    "type": "thread_tombstoned",
                    "thread": thread,
                    "tombstoned_at": tombstoned_at,
                    "final_sth": payload["final_sth"],
                })
            except Exception:
                pass

        return payload

    async def _auto_seal_loop() -> None:
        """Poll every ephemeral_seal_check_seconds. Seal any live
        ephemeral thread whose created_at + ttl_seconds has elapsed.
        The loop swallows per-iteration errors so one bad row doesn't
        wedge the whole task."""
        while True:
            try:
                await asyncio.sleep(config.ephemeral_seal_check_seconds)
                now = _now_utc()
                for rec in store.all_ephemeral():
                    if rec["tombstoned_at"] is not None:
                        continue
                    try:
                        created = _parse_iso(rec["created_at"])
                    except ValueError:
                        continue
                    if (now - created).total_seconds() < rec["ttl_seconds"]:
                        continue
                    await _seal_ephemeral_thread(rec["thread"])
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a bug in one iteration kill the loop —
                # the next tick tries again. The seal ceremony itself
                # is idempotent so a partial-then-retry is safe.
                pass

    # ---- POST /threads/ephemeral (v0.4.37 / v0.4.38 shape) --------------
    # Register a thread as ephemeral with a TTL. The caller submits a
    # PRE-SIGNED tombstone Entry (kind='tombstone', author=caller,
    # thread=<T>, tombstone_valid_after=<created_at + ttl>). The hub
    # verifies the entry, records it verbatim, and holds it for the
    # auto-seal path. At TTL expiration the hub uses this stored entry
    # as the member-signed tombstone — the hub itself holds no member
    # keys and cannot fabricate one.
    #
    # No governance kinds are permitted in the resulting thread
    # (pipeline enforces this structurally). Cross-tree substitution
    # is prevented at the STH layer by binding the thread name into
    # the ephemeral STH signing payload.
    @api.post("/threads/ephemeral")
    def post_threads_ephemeral(body: dict = Body(...),
                               caller: str = Depends(require_session)):
        if ephemeral_translog is None:
            return _err(503, error="no_ephemeral_log")

        thread = body.get("thread")
        ttl_seconds = body.get("ttl_seconds")
        tombstone_body = body.get("tombstone_entry")

        if not isinstance(thread, str) or not thread:
            return _err(400, error="bad_request", reason="thread required")
        if not isinstance(ttl_seconds, int):
            return _err(400, error="bad_request", reason="ttl_seconds must be int")
        if not (config.ephemeral_ttl_min_seconds
                <= ttl_seconds
                <= config.ephemeral_ttl_max_seconds):
            return _err(
                400, error="bad_request",
                reason=(
                    f"ttl_seconds must be in "
                    f"[{config.ephemeral_ttl_min_seconds}, "
                    f"{config.ephemeral_ttl_max_seconds}]"
                ),
            )
        if not isinstance(tombstone_body, dict):
            return _err(
                400, error="bad_request", reason="tombstone_entry required",
            )
        try:
            ts_ev = _entry_from_dict(tombstone_body)
        except (TypeError, ValueError) as e:
            return _err(400, error="bad_request", reason=f"tombstone_entry: {e}")
        if ts_ev.kind != "tombstone":
            return _err(
                400, error="bad_request",
                reason="tombstone_entry.kind must be 'tombstone'",
            )
        if ts_ev.author != caller:
            return _err(
                400, error="bad_request",
                reason="tombstone_entry.author must equal caller",
            )
        if ts_ev.thread != thread:
            return _err(
                400, error="bad_request",
                reason="tombstone_entry.thread must equal thread",
            )
        if not verify_entry(ts_ev):
            return _err(
                400, error="bad_request",
                reason="tombstone_entry signature does not verify",
            )
        if not isinstance(ts_ev.tombstone_valid_after, str):
            return _err(
                400, error="bad_request",
                reason="tombstone_entry.tombstone_valid_after required (RFC3339)",
            )
        # Bind the pre-signed valid_after to the requested TTL: the
        # entry the hub holds must actually authorize deletion at the
        # TTL horizon, not earlier or later. Parse both with the same
        # helper so clock-format ambiguity can't sneak through.
        try:
            created_dt = _now_utc()
            expected_valid_after = _add_seconds_iso(created_dt, ttl_seconds)
            declared_valid_after = _parse_iso(ts_ev.tombstone_valid_after)
        except ValueError as e:
            return _err(400, error="bad_request", reason=str(e))
        # Allow ±60s tolerance for round-trip clock skew between the
        # client's sign time and the hub's arrival time. Beyond that
        # the pre-signed authorization no longer represents the TTL
        # the caller is asking us to enforce.
        skew = abs((declared_valid_after - _parse_iso(expected_valid_after)).total_seconds())
        if skew > 60:
            return _err(
                400, error="bad_request",
                reason=(
                    f"tombstone_valid_after {ts_ev.tombstone_valid_after} does not match "
                    f"created_at + ttl_seconds ({expected_valid_after}); "
                    f"skew {skew:.0f}s > 60s tolerance"
                ),
            )

        created_at = created_dt.isoformat()
        try:
            store.open_ephemeral(
                thread=thread,
                creator_pubkey=caller,
                created_at=created_at,
                ttl_seconds=ttl_seconds,
                tombstone_entry_content=crypto.canonicalize(ts_ev.content()),
                tombstone_entry_sig=ts_ev.sig,
            )
        except ValueError as e:
            return _err(409, error="conflict", reason=str(e))
        ephemeral_translog.open_thread(thread)

        return {
            "thread": thread,
            "creator_pubkey": caller,
            "created_at": created_at,
            "ttl_seconds": ttl_seconds,
            "expires_at": expected_valid_after,
        }

    # ---- POST /threads/{thread}/tombstone (v0.4.38) --------------------
    # Manual seal. Creator submits a fresh tombstone Entry with
    # valid_after ≤ now; hub verifies and runs the shared seal
    # ceremony. This is the path to seal EARLY (before TTL); the
    # auto-seal task uses the pre-signed entry stored at open time.
    # Only the thread's creator can seal (per the v0.4.37 decision
    # to keep the surface minimal — board still recovers via
    # revocation of the creator, which is a separate governance act).
    @api.post("/threads/{thread}/tombstone")
    async def post_tombstone(thread: str, body: dict = Body(...),
                             caller: str = Depends(require_session)):
        if ephemeral_translog is None:
            return _err(503, error="no_ephemeral_log")
        rec = store.get_ephemeral(thread)
        if rec is None:
            return _err(404, error="not_found", thread=thread)
        if rec["tombstoned_at"] is not None:
            return _err(409, error="already_tombstoned",
                        tombstoned_at=rec["tombstoned_at"])
        if caller != rec["creator_pubkey"]:
            return _err(403, error="forbidden",
                        reason="only the thread creator can tombstone")

        ts_body = body.get("tombstone_entry")
        if not isinstance(ts_body, dict):
            return _err(400, error="bad_request",
                        reason="tombstone_entry required")
        try:
            ts_ev = _entry_from_dict(ts_body)
        except (TypeError, ValueError) as e:
            return _err(400, error="bad_request",
                        reason=f"tombstone_entry: {e}")
        if ts_ev.kind != "tombstone":
            return _err(400, error="bad_request",
                        reason="tombstone_entry.kind must be 'tombstone'")
        if ts_ev.author != caller:
            return _err(400, error="bad_request",
                        reason="tombstone_entry.author must equal caller")
        if ts_ev.thread != thread:
            return _err(400, error="bad_request",
                        reason="tombstone_entry.thread must equal path thread")
        if not verify_entry(ts_ev):
            return _err(400, error="bad_request",
                        reason="tombstone_entry signature does not verify")
        if not isinstance(ts_ev.tombstone_valid_after, str):
            return _err(400, error="bad_request",
                        reason="tombstone_entry.tombstone_valid_after required")
        try:
            valid_after = _parse_iso(ts_ev.tombstone_valid_after)
        except ValueError as e:
            return _err(400, error="bad_request", reason=str(e))
        now = _now_utc()
        # Manual seal permits valid_after ≤ now + 60s (same skew
        # tolerance as the open endpoint). Prevents scheduling a seal
        # into the future via the manual path — auto-seal is for that.
        if (valid_after - now).total_seconds() > 60:
            return _err(
                400, error="bad_request",
                reason=(
                    f"tombstone_valid_after {ts_ev.tombstone_valid_after} is "
                    f"in the future; manual seal requires valid_after ≤ now"
                ),
            )

        result = await _seal_ephemeral_thread(thread, override_entry=ts_ev)
        if "error" in result:
            return _err(500, error=result["error"])
        return result

    # ---- GET /ephemeral/final_sth (v0.4.38) ----------------------------
    # After a thread is tombstoned, its ephemeral entries are gone from
    # the store — but the sealed STH survives here so any member who
    # kept a copy of an entry can prove inclusion by reconstructing
    # the leaf and running verify_inclusion_ephemeral against this STH.
    @api.get("/ephemeral/final_sth")
    def get_final_sth(thread: str = Query(...),
                      _caller: str = Depends(require_session)):
        row = store.get_final_sth(thread)
        if row is None:
            return _err(404, error="not_found", thread=thread)
        return row

    # ---- GET /inbox (v0.4.19) ------------------------------------------
    # Landing view bundle: for every observed thread, return the latest
    # non-receipt entry (so the client can render an "email row" preview)
    # plus the caller's high-water seq (= max seq of receipts they've
    # posted in that thread). Unread = latest_seq > my_high_water.
    #
    # One round-trip on Unlock; the client wires receipt-posting on
    # thread-view to maintain the high-water. Display name + role are
    # resolved against the in-memory directory so the row can render
    # without a separate /directory hop for each preview.
    _PREVIEW_MAX = 140

    @api.get("/inbox")
    def get_inbox(caller: str = Depends(require_session)) -> dict:
        archived_threads = store.archive_state_per_thread(_has_archive_capability)
        audience_by_thread = store.threads_with_audience()
        rows = []
        for t, n, s, parent in overview.thread_summaries():
            # v0.4.27: skip audience-scoped threads when caller not in
            # the audience. They don't get a row, not even an "archived
            # but hidden" placeholder — the thread is invisible.
            aud = audience_by_thread.get(t)
            if aud is not None and caller not in (aud.pubkeys or []):
                continue
            latest = store.latest_non_receipt(t)
            high_water = store.caller_receipt_high_water(t, caller)
            preview_entry = None
            if latest is not None:
                ev, ev_seq = latest
                author_name = None
                author_role = None
                if directory is not None:
                    att = directory.resolve(ev.author)
                    if att is not None:
                        author_name = att.display_name
                        author_role = att.role
                body = ev.body or ""
                if len(body) > _PREVIEW_MAX:
                    body = body[:_PREVIEW_MAX].rstrip() + "…"
                preview_entry = {
                    "id": ev.id,
                    "seq": ev_seq,
                    "author": ev.author,
                    "kind": ev.kind,
                    "created_at": ev.created_at,
                    "body_preview": body,
                    "display_name": author_name,
                    "role": author_role,
                }
            rows.append({
                "thread": t,
                "entry_count": n,
                "latest_seq": s,
                "parent_thread": parent,
                "my_high_water": high_water,
                "latest_entry": preview_entry,
                "archived": archived_threads.get(t, False),
                "audience": (
                    {"pubkeys": list(aud.pubkeys)} if aud is not None else None
                ),
            })
        return {"threads": rows}

    # ---- GET /overview (§6) --------------------------------------------
    @api.get("/overview")
    def get_overview(thread: str = Query(...),
                     _caller: str = Depends(require_session)) -> dict:
        rows = []
        for entry_id, seq, parents in overview.thread_entries(thread):
            rows.append({
                "id": entry_id, "seq": seq,
                "parents": parents,
                "children": overview.children(entry_id),
            })
        return {"thread": thread, "entries": rows}

    # ---- GET /sth (§6.4) ------------------------------------------------
    # v0.4.37: `?thread=T` selects the per-thread ephemeral STH when T is
    # an ephemeral thread; without the parameter (or when T is permanent)
    # the main-log STH is returned. The two shapes differ only in that
    # the ephemeral STH carries a `thread` field bound into its signature.
    @api.get("/sth")
    def get_sth(thread: Optional[str] = Query(None)) -> dict:
        if thread is not None and store.is_ephemeral(thread):
            if ephemeral_translog is None:
                return _err(503, error="no_ephemeral_log")
            try:
                return asdict(ephemeral_translog.current_sth(thread))
            except KeyError:
                return _err(404, error="not_found", thread=thread)
        return asdict(translog.current_sth())

    # ---- GET /proof/inclusion (§6.4.2) ----------------------------------
    # v0.4.37: when the entry is in an ephemeral thread, route the proof
    # to that thread's tree. Response shape stays the same (proof + sth
    # bundle) — clients dispatch on sth.thread being present.
    @api.get("/proof/inclusion")
    def get_inclusion(entry: str = Query(...)):
        target = store.get(entry)
        if target is not None and store.is_ephemeral(target.thread):
            if ephemeral_translog is None:
                return _err(503, error="no_ephemeral_log")
            try:
                proof, sth = ephemeral_translog.inclusion_proof_and_sth(
                    target.thread, entry,
                )
                return {**asdict(proof), "sth": asdict(sth)}
            except KeyError:
                return _err(404, error="not_found", entry=entry)
        try:
            # v0.4.31: bundle the proof + STH from a single atomic
            # snapshot of the translog. Eliminates the client-side
            # "size error" race where another entry could land between
            # separate /sth and /proof/inclusion fetches, producing
            # proof.tree_size != sth.tree_size.
            proof, sth = translog.inclusion_proof_and_sth(entry)
            return {**asdict(proof), "sth": asdict(sth)}
        except KeyError:
            return _err(404, error="not_found", entry=entry)

    # ---- GET /proof/consistency (§6.4.2) --------------------------------
    # v0.4.37: `?thread=T` for ephemeral consistency between two STHs of
    # the same per-thread tree. Without the parameter, the main log.
    @api.get("/proof/consistency")
    def get_consistency(from_size: int = Query(..., alias="from_size"),
                        to_size: int = Query(..., alias="to_size"),
                        thread: Optional[str] = Query(None)):
        try:
            if thread is not None and store.is_ephemeral(thread):
                if ephemeral_translog is None:
                    return _err(503, error="no_ephemeral_log")
                return asdict(ephemeral_translog.consistency_proof(
                    thread, from_size, to_size,
                ))
            return asdict(translog.consistency_proof(from_size, to_size))
        except ValueError as e:
            return _err(400, error="bad_sizes", detail=str(e))

    # ---- GET /directory (§2.3) ------------------------------------------
    # v0.4.0: dropped the auth gate. The manifest is root-signed; the
    # gate was only hiding the roster (no secrets) and blocked two
    # legitimate flows that have no session yet:
    #   - admin CLI (scripts/attest_member.py) building a new manifest
    #   - on-device-keygen onboarding verifying the signed manifest the
    #     /pending/watch push pointed it at, before authenticating
    # Acceptable for a closed-org pilot. If hiding the roster becomes
    # a requirement later, the right answer is a per-tenant view, not
    # re-gating this endpoint.
    @api.get("/directory")
    def get_directory():
        m = directory.manifest if directory is not None else None
        if m is None:
            return _err(503, error="no_directory")
        return _manifest_to_dict(m)

    # ---- GET /ledger (§8) -----------------------------------------------
    @api.get("/ledger")
    def get_ledger(entry: str = Query(...),
                   _caller: str = Depends(require_session)):
        target = store.get(entry)
        seq = store.seq_of(entry) if target is not None else None
        if target is None or seq is None:
            return _err(404, error="not_found", entry=entry)
        # v0.4.39: for audience-scoped threads, partition ONLY over the
        # audience members. The delivery indicator on a group message
        # should list "did the 4 people in this group get it?", not
        # "did the 4 in the group + 15 uninvolved members get it?" —
        # the latter forever shows every non-audience member as
        # "not yet" because they were never in the audience to begin
        # with. Public threads still enumerate the full directory.
        aud = store.thread_audience(target.thread)
        if aud is not None:
            members = list(aud.pubkeys)
        else:
            members = directory.attested_keys() if directory is not None else []
        # v0.4.44: filter out currently-revoked keys from the partition.
        # attested_keys returns everyone ever attested (per §2.3 "sent
        # before revocation is still owed"), and the ledger substrate
        # still preserves the history of what any revoked member did
        # while attested. But the delivery card is a "who has this
        # been delivered to NOW" surface — a bare pubkey with no name
        # for a long-departed member is useless noise, and it can't
        # ack anymore anyway. Board's design call recorded v0.4.44.
        if directory is not None:
            members = [m for m in members if not directory.is_revoked(m)]
        return ledger.status(
            target.thread, required_seq=seq, members=members,
            author=target.author,
        )

    # ---- WS /stream (§7, §7.1 step 10) ---------------------------------
    @api.websocket("/stream")
    async def stream(ws: WebSocket):
        # Always accept first so we can send a close frame with a reason.
        # Browsers can't set the Authorization header on a WS handshake, so
        # we also accept `?token=` as a fallback for browser clients.
        await ws.accept()
        if auth is None:
            await ws.close(code=1011, reason="auth not configured")
            return
        token: Optional[str] = None
        hdr = ws.headers.get("authorization", "")
        if hdr.lower().startswith("bearer "):
            token = hdr.split(" ", 1)[1].strip()
        if not token:
            token = ws.query_params.get("token")
        caller_pubkey = auth.resolve_session(token) if token else None
        if caller_pubkey is None:
            await ws.close(code=1008, reason="auth required")
            return
        # v0.4.27: bind the subscriber's pubkey so the fanout layer can
        # filter audience-scoped entry pushes.
        await fanout.register(ws, caller_pubkey)
        try:
            # We don't act on client-sent messages in v1; we just hold the
            # connection open until the client disconnects. receive_text()
            # raises WebSocketDisconnect when that happens.
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await fanout.unregister(ws)

    # ---- POST /blobs + GET /blobs/{hash} (§4) --------------------------
    @api.post("/blobs")
    async def post_blob(request: Request,
                        caller: str = Depends(require_session)):
        if blobs is None:
            return _err(503, error="no_blobs")
        content = await request.body()
        # §7.2.1 structural cap — pre-quota, content-agnostic.
        if len(content) > config.bounds.max_blob_bytes:
            return _throttle_response(ThrottleError(
                "structural", config.bounds.max_blob_bytes, None,
                f"blob size {len(content)} > max {config.bounds.max_blob_bytes}",
            ))
        # Dedup before quota: identical bytes from any author resolve to the
        # same content-address and have already been paid for. This is the
        # spec's 'realistic dedup ≈ 35%' (§4) at work.
        #
        # TODO(multi-tenant): the dedup:True response is a PRESENCE ORACLE —
        # an uploader can learn that someone ELSE already stored these bytes.
        # Benign in a single-org pilot (one trust domain). When a second org
        # shares a hub: scope dedup per-tenant AND return dedup:False (with
        # a fresh charge) for cross-tenant matches so presence doesn't leak.
        # See cove/blobs.py module docstring.
        h = "sha256:" + crypto.sha256_hex(content)
        if blobs.has(h):
            return {"hash": h, "size": len(content), "dedup": True}
        # §7.2.2 storage quota — per attested identity.
        att = directory.resolve(caller) if directory is not None else None
        role = att.role if att is not None else "member"
        try:
            throttler.reserve_storage(caller, role, len(content))
        except ThrottleError as e:
            return _throttle_response(e)
        blobs.put(content)
        return {"hash": h, "size": len(content), "dedup": False}

    @api.get("/blobs/{blob_hash}")
    def get_blob(blob_hash: str, _caller: str = Depends(require_session)):
        if blobs is None:
            return _err(503, error="no_blobs")
        data = blobs.get(f"sha256:{blob_hash}")
        if data is None:
            return _err(404, error="not_found")
        # The content-address IS the ETag — clients re-hash on download to
        # detect tampering (§4 integrity); a matching ETag also makes
        # blob bodies trivially cacheable since the bytes are immutable.
        return Response(content=data,
                        media_type="application/octet-stream",
                        headers={"ETag": f'"sha256:{blob_hash}"'})

    # ---- /pending — v0.4.0 on-device keygen + push approval ------------
    # A device that just generated its keypair POSTs /pending to surface
    # itself in the keymaster's queue, then holds WS /pending/watch open.
    # The keymaster reviews + attests via /admin/attest; the manifest hook
    # above wakes any matching watcher.

    @api.post("/pending")
    def post_pending(body: dict = Body(...)):
        if directory is None:
            return _err(503, error="no_directory")
        pubkey = body.get("pubkey")
        name_hint = body.get("name_hint")
        requested_at = body.get("requested_at")
        invite_code = body.get("invite")
        if not (isinstance(pubkey, str) and len(pubkey) == 64
                and all(c in "0123456789abcdef" for c in pubkey)):
            return _err(400, error="bad_request",
                        detail="pubkey must be 64-char hex")
        if not isinstance(name_hint, str) or not name_hint.strip():
            return _err(400, error="bad_request",
                        detail="name_hint required")
        if not isinstance(requested_at, str) or not requested_at:
            return _err(400, error="bad_request",
                        detail="requested_at required (rfc3339)")
        # v0.4.33: invite-code admission gate. Without a valid unused
        # code, the request gets a 401 immediately and never lands in
        # the keymaster's queue. Codes are minted via /admin/invites
        # and delivered out-of-band, same channel as the fingerprint
        # verification.
        if not isinstance(invite_code, str) or not invite_code:
            return _err(401, error="invite_required",
                        detail="invite code required")
        # If the pubkey is already attested, return 409 so the client
        # transitions to the auth flow instead of waiting for a push
        # that will never come (the /admin/attest hook only fires for
        # NEWLY-attested keys). Check BEFORE consuming the invite —
        # a re-onboard attempt by an already-attested member shouldn't
        # burn a code.
        if directory.resolve(pubkey) is not None:
            return _err(409, error="already_attested",
                        detail="pubkey is already in the directory")
        # Atomic check-and-consume: if two parallel posts use the same
        # code, exactly one wins. The other gets 'already_used'.
        try:
            invites.consume(invite_code)
        except InviteUnusable as e:
            return _err(401, error="invite_unusable", reason=str(e))
        pending.register(pubkey=pubkey, name_hint=name_hint.strip(),
                         requested_at=requested_at)
        return {"pubkey": pubkey}

    @api.get("/pending")
    def get_pending(_caller: str = Depends(require_board)):
        return {"pending": [asdict(r) for r in pending.list()]}

    @api.delete("/pending/{pubkey}")
    def delete_pending(pubkey: str, _caller: str = Depends(require_board)):
        # Idempotent — clearing a non-pending pubkey returns success so
        # an admin clicking 'reject' after a concurrent attestation
        # doesn't get a confusing error.
        pending.clear(pubkey)
        return {"pubkey": pubkey}

    @api.websocket("/pending/watch")
    async def pending_watch(ws: WebSocket):
        """Held open until the queried pubkey appears in the directory.

        Reconnect-safe: on handshake, if the pubkey is ALREADY attested
        (e.g. WS dropped during attestation; client reconnected), push
        immediately and close. Otherwise register a watcher event and
        await. The /admin/attest hook above sets the event from the same
        event loop, so wake-up is single-loop-tick.
        """
        await ws.accept()
        pubkey = ws.query_params.get("pubkey")
        if not pubkey or len(pubkey) != 64:
            await ws.close(code=1008, reason="pubkey query param required")
            return
        if directory is None:
            await ws.close(code=1011, reason="no directory")
            return

        async def _push_attested():
            await ws.send_json({
                "type": "attested",
                "pubkey": pubkey,
                "manifest_hash": hash_manifest(directory.manifest),
            })

        if directory.resolve(pubkey) is not None:
            await _push_attested()
            await ws.close()
            return

        event = pending.watcher_event(pubkey)
        # Race-window mitigation: between the resolve() above and the
        # event registration, /admin/attest might have fired. Re-check.
        if directory.resolve(pubkey) is not None:
            await _push_attested()
            await ws.close()
            return

        try:
            await event.wait()
            await _push_attested()
        except WebSocketDisconnect:
            return
        try:
            await ws.close()
        except Exception:
            pass

    # ---- /admin/* (§7, §7.2.2) -----------------------------------------
    # Self-authenticating via root signature on the payload — CLAUDE.md
    # non-negotiable #1 ('hub holds NO root private key') means the admin
    # tool signs OFF-HUB, then submits the result here. The hub verifies
    # and applies; it never signs anything with the root key.
    #
    # /admin/attest and /admin/revoke share an implementation: both take
    # a NEW root-signed manifest (the admin tool built it offline with
    # the change baked in). The route name encodes intent for log/audit
    # readers; the mechanic is the same.

    def _apply_manifest(body: dict):
        m = body.get("manifest")
        if not isinstance(m, dict):
            return _err(400, error="bad_request", detail="manifest required"), set()
        try:
            new_m = _manifest_from_dict(m)
        except (TypeError, KeyError, ValueError) as e:
            return _err(400, error="bad_manifest", detail=str(e)), set()
        if directory is None:
            return _err(503, error="no_directory"), set()
        # Snapshot the attested set BEFORE the update so we can identify
        # newly-attested pubkeys after the swap. The pending-watcher hook
        # below uses this to push only to keys that weren't previously in
        # the directory.
        prior_attested = set(directory.attested_keys())
        try:
            directory.update_from(new_m)
        except InvalidManifestSignatureError as e:
            return _err(400, error="invalid_signature", detail=str(e)), set()
        except StaleManifestError as e:
            return _err(409, error="stale_manifest", detail=str(e),
                        current_head=hash_manifest(directory.manifest)), set()
        except RevocationDroppedError as e:
            return _err(409, error="revocation_dropped", detail=str(e)), set()
        new_attested = set(directory.attested_keys()) - prior_attested
        return {"updated_at": new_m.updated_at,
                "attestations": len(new_m.attestations),
                "revocations": len(new_m.revocations),
                "manifest_hash": hash_manifest(new_m)}, new_attested

    async def _broadcast_directory_changed(result: dict) -> None:
        """v0.4.18: notify all live /stream subscribers that the directory
        has rotated. Clients refetch /directory on receipt — keeps the
        push small (no embedded manifest) since real directory churn is
        rare (board actions, not user actions). Closes the race where an
        existing client renders a freshly-attested member's first post as
        'author not attested'."""
        if isinstance(result, dict) and "manifest_hash" in result:
            await fanout.broadcast({
                "type": "directory_changed",
                "manifest_hash": result["manifest_hash"],
            })

    @api.post("/admin/attest")
    async def admin_attest(body: dict = Body(...)):
        result, newly_attested = _apply_manifest(body)
        # Push any pending watchers whose pubkey just became attested.
        # Done in the same loop iteration so the WS coroutine wakes
        # before this handler returns — turns the POST response and the
        # WS push into a tight serialized pair from the test's POV.
        for pk in newly_attested:
            pending.mark_attested(pk)
        await _broadcast_directory_changed(result)
        return result

    @api.post("/admin/revoke")
    async def admin_revoke(body: dict = Body(...)):
        result, _ = _apply_manifest(body)
        await _broadcast_directory_changed(result)
        return result

    # POST /admin/limits — per-identity throttle override (§7.2.2)
    # Intentionally asymmetric to /admin/attest+revoke: limit overrides
    # are PROCESS-LOCAL throttle state (Throttler._overrides) and
    # EVAPORATE on restart, while attest/revoke is durable in the
    # manifest chain. This is by design — a temporary 'raise the board's
    # limit for the annual mailing' should not silently persist beyond
    # the deploy that set it. If you need a durable limit change, push
    # it as a config update, not a runtime override.
    @api.post("/admin/limits")
    def admin_limits(body: dict = Body(...)):
        if directory is None or directory.manifest is None:
            return _err(503, error="no_directory")
        payload = body.get("payload")
        sig = body.get("sig")
        if not isinstance(payload, dict) or not isinstance(sig, str):
            return _err(400, error="bad_request",
                        detail="payload (object) and sig (hex) required")
        # Root signature gates this — same security model as the manifest:
        # admin authority IS root authority.
        root_pub = directory.manifest.org
        if not crypto.verify(root_pub, sig, crypto.canonicalize(payload)):
            return _err(401, error="invalid_signature")
        pubkey = payload.get("pubkey")
        tier = payload.get("tier")
        if not (isinstance(pubkey, str) and isinstance(tier, str)):
            return _err(400, error="bad_payload",
                        detail="pubkey + tier required in payload")
        try:
            throttler.set_tier_override(pubkey, tier)
        except ValueError as e:
            return _err(400, error="bad_tier", detail=str(e))
        return {"pubkey": pubkey, "tier": tier}

    # ---- /admin/invites (v0.4.33) ---------------------------------------
    # Without an admission gate, POST /pending is wide open and the
    # keymaster's queue is spam-bait. Invites are a binary structural
    # gate — have a valid unused code, get queued; don't, get rejected.
    # See cove/invites.py for the threat-model rationale.
    #
    # Mint + revoke are root-signed payloads (same security model as
    # /admin/limits). List is admin-capability-gated.

    def _invite_to_dict(inv: Invite) -> dict:
        # v0.4.33: monotonic seconds aren't meaningful on the wire.
        # Send expires_in_seconds (relative to the hub's now) so the
        # client can render "expires in N hours" without knowing when
        # the hub process started. created_at + consumed_at + revoked_at
        # stay in monotonic terms; they're only meaningful when compared
        # against each other or expires_at within this same response.
        import time as _time
        now = _time.monotonic()
        return {
            "code": inv.code,
            "expires_at": inv.expires_at,
            "expires_in_seconds": max(0, int(inv.expires_at - now)),
            "created_at": inv.created_at,
            "name_hint": inv.name_hint,
            "consumed_at": inv.consumed_at,
            "revoked_at": inv.revoked_at,
        }

    @api.post("/admin/invites")
    def admin_invites_mint(body: dict = Body(...)):
        """Mint an invite. payload = {ttl_seconds: int, name_hint?: str}.
        Root-signed (the keymaster has root.priv off-hub)."""
        if directory is None or directory.manifest is None:
            return _err(503, error="no_directory")
        payload = body.get("payload")
        sig = body.get("sig")
        if not isinstance(payload, dict) or not isinstance(sig, str):
            return _err(400, error="bad_request",
                        detail="payload (object) and sig (hex) required")
        root_pub = directory.manifest.org
        if not crypto.verify(root_pub, sig, crypto.canonicalize(payload)):
            return _err(401, error="invalid_signature")
        ttl = payload.get("ttl_seconds")
        name_hint = payload.get("name_hint")
        if not isinstance(ttl, int):
            return _err(400, error="bad_payload",
                        detail="ttl_seconds (int) required in payload")
        if name_hint is not None and not isinstance(name_hint, str):
            return _err(400, error="bad_payload",
                        detail="name_hint must be a string when present")
        try:
            inv = invites.mint(ttl_seconds=ttl, name_hint=name_hint)
        except ValueError as e:
            return _err(400, error="bad_ttl", detail=str(e))
        return _invite_to_dict(inv)

    @api.get("/admin/invites")
    def admin_invites_list(_caller: str = Depends(require_capability("admin"))):
        """List currently-active invites (unused, unrevoked, unexpired).
        Admin-capability-gated; the codes themselves are just routing
        metadata until they're presented at /pending, but consolidating
        the view to the admin panel reduces accidental disclosure."""
        return {"invites": [_invite_to_dict(i) for i in invites.list_active()]}

    @api.delete("/admin/invites/{code}")
    def admin_invites_revoke(code: str, body: dict = Body(...)):
        """Revoke an unused invite. payload = {code: str} (must match the
        path param — keeps the sig binding to the specific code, not just
        to a generic 'revoke' action). Root-signed."""
        if directory is None or directory.manifest is None:
            return _err(503, error="no_directory")
        payload = body.get("payload")
        sig = body.get("sig")
        if not isinstance(payload, dict) or not isinstance(sig, str):
            return _err(400, error="bad_request",
                        detail="payload (object) and sig (hex) required")
        root_pub = directory.manifest.org
        if not crypto.verify(root_pub, sig, crypto.canonicalize(payload)):
            return _err(401, error="invalid_signature")
        if payload.get("code") != code:
            return _err(400, error="bad_payload",
                        detail="payload.code must match the URL path")
        try:
            inv = invites.revoke(code)
        except InviteUnusable as e:
            return _err(400, error="invite_unusable", reason=str(e))
        return _invite_to_dict(inv)

    return api


# ---- helpers ------------------------------------------------------------
class _AuthRequired(Exception):
    """Internal exception raised by require_session; mapped to a flat-body
    401 by an exception handler registered inside create_app."""
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _Forbidden(Exception):
    """Internal exception raised by require_board (and any future
    role-gated dependency); mapped to a flat-body 403."""
    def __init__(self, reason: str) -> None:
        self.reason = reason


_CONTENT_FIELDS = {"thread", "author", "kind", "created_at", "parents",
                   "body", "blobs", "supersedes", "receipt", "branch_thread",
                   "audience", "tombstone_valid_after"}


def _entry_from_dict(d: dict) -> Entry:
    """Reconstruct an Entry from a wire JSON body. Strict about fields so a
    client sending unexpected keys gets a 400, not silent acceptance."""
    if not isinstance(d, dict):
        raise TypeError("entry must be a JSON object")
    extra = set(d.keys()) - (_CONTENT_FIELDS | {"id", "sig"})
    if extra:
        raise ValueError(f"unexpected fields: {sorted(extra)}")
    blobs = [BlobRef(**b) for b in d.get("blobs", []) or []]
    fields = {k: d[k] for k in _CONTENT_FIELDS if k in d}
    fields["blobs"] = blobs
    if fields.get("receipt") is not None:
        fields["receipt"] = Receipt(**fields["receipt"])
    if fields.get("audience") is not None:
        fields["audience"] = Audience(**fields["audience"])
    ev = Entry(**fields)
    ev.id = d.get("id")
    ev.sig = d.get("sig")
    return ev


def _entry_to_dict(ev: Entry) -> dict:
    return asdict(ev)


# Wire (de)serialization lives in identity.py so the persistence layer
# and the api use the same code path; the api just re-exports here for
# call sites that were already using the private name.
_manifest_to_dict = manifest_to_dict
_manifest_from_dict = manifest_from_dict




# v0.4.38: time helpers for the ephemeral-thread + tombstone flows.
# Kept module-level so tests can patch _now_utc for deterministic
# TTL scenarios without stubbing the whole datetime module.
def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_iso(s: str):
    """Parse an RFC3339 / ISO-8601 UTC timestamp. Accepts both '+00:00'
    and 'Z' suffix; returns a timezone-aware datetime."""
    from datetime import datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _add_seconds_iso(dt, seconds: int) -> str:
    from datetime import timedelta
    return (dt + timedelta(seconds=seconds)).isoformat()


def _err(status: int, **body) -> JSONResponse:
    """Flat-shaped error body (vs FastAPI's default {detail: ...} nesting),
    matching the §7.2.3 throttle shape so clients see consistent error
    layouts across the API."""
    return JSONResponse(status_code=status, content=body)


def _throttle_response(e: ThrottleError) -> JSONResponse:
    """Spec §7.2.3: structured throttle body. 429 for all scopes per spec
    wording (`429-style`); the body's `scope` tells the client whether to
    retry (rate/volume), wait for space (storage), or never retry (structural).
    """
    headers = {}
    if e.retry_after_s is not None:
        headers["Retry-After"] = str(e.retry_after_s)
    return JSONResponse(
        status_code=429,
        content={
            "error": "throttled",
            "scope": e.scope,
            "limit": e.limit,
            "retry_after_s": e.retry_after_s,
            "detail": e.detail,
        },
        headers=headers,
    )
