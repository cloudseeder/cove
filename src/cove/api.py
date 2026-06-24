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
from fastapi.responses import JSONResponse, Response

from . import crypto
from .auth import AuthError, AuthService
from .blobs import BlobStore
from .config import DEFAULT, HubConfig
from .entry import BlobRef, Entry, Receipt
from .identity import (
    Attestation, Directory, DirectoryManifest,
    InvalidManifestSignatureError, RevocationDroppedError,
    Revocation, StaleManifestError, hash_manifest,
    manifest_from_dict, manifest_to_dict,
)
from .index import Ledger, Overview
from .pipeline import AcceptanceError, Pipeline
from .store import EventStore
from .throttle import ThrottleError, Throttler
from .translog import TamperEvidentLog


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
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            conns = list(self._connections)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

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
        yield

    api = FastAPI(title="Cove Hub", version="0.1.0", lifespan=lifespan)

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

    @api.exception_handler(_AuthRequired)
    async def _auth_required_handler(_request, exc: "_AuthRequired"):
        return _err(401, error="auth_required", reason=exc.reason)

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
        await fanout.broadcast({
            "type": "entry",
            "entry": _entry_to_dict(ev),
            "seq": seq,
        })
        return {"id": ev.id, "seq": seq}

    # ---- GET /sync (§7) -------------------------------------------------
    @api.get("/sync")
    def get_sync(thread: str = Query(...), since: int = Query(...),
                 _caller: str = Depends(require_session)) -> dict:
        # Each item shape: {"entry": <signed entry>, "seq": <per-thread seq>}.
        # Matches the WS push payload — clients write one merge / dedup path
        # over both channels (client-spec §4.1).
        entries = [
            {"entry": _entry_to_dict(ev), "seq": s}
            for ev, s in store.since_with_seq(thread, since)
        ]
        return {"thread": thread, "since": since, "entries": entries}

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
    @api.get("/sth")
    def get_sth() -> dict:
        return asdict(translog.current_sth())

    # ---- GET /proof/inclusion (§6.4.2) ----------------------------------
    @api.get("/proof/inclusion")
    def get_inclusion(entry: str = Query(...)):
        try:
            return asdict(translog.inclusion_proof(entry))
        except KeyError:
            return _err(404, error="not_found", entry=entry)

    # ---- GET /proof/consistency (§6.4.2) --------------------------------
    @api.get("/proof/consistency")
    def get_consistency(from_size: int = Query(..., alias="from_size"),
                        to_size: int = Query(..., alias="to_size")):
        try:
            return asdict(translog.consistency_proof(from_size, to_size))
        except ValueError as e:
            return _err(400, error="bad_sizes", detail=str(e))

    # ---- GET /directory (§2.3) ------------------------------------------
    @api.get("/directory")
    def get_directory(_caller: str = Depends(require_session)):
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
        members = directory.attested_keys() if directory is not None else []
        return ledger.status(target.thread, required_seq=seq, members=members)

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
        if not token or auth.resolve_session(token) is None:
            await ws.close(code=1008, reason="auth required")
            return
        await fanout.register(ws)
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
            return _err(400, error="bad_request", detail="manifest required")
        try:
            new_m = _manifest_from_dict(m)
        except (TypeError, KeyError, ValueError) as e:
            return _err(400, error="bad_manifest", detail=str(e))
        if directory is None:
            return _err(503, error="no_directory")
        try:
            directory.update_from(new_m)
        except InvalidManifestSignatureError as e:
            return _err(400, error="invalid_signature", detail=str(e))
        except StaleManifestError as e:
            # 409 Conflict — the admin tool built this on a stale base; it
            # should re-pull /directory (head moved) and rebuild.
            return _err(409, error="stale_manifest", detail=str(e),
                        current_head=hash_manifest(directory.manifest))
        except RevocationDroppedError as e:
            return _err(409, error="revocation_dropped", detail=str(e))
        return {"updated_at": new_m.updated_at,
                "attestations": len(new_m.attestations),
                "revocations": len(new_m.revocations),
                "manifest_hash": hash_manifest(new_m)}

    @api.post("/admin/attest")
    def admin_attest(body: dict = Body(...)):
        return _apply_manifest(body)

    @api.post("/admin/revoke")
    def admin_revoke(body: dict = Body(...)):
        return _apply_manifest(body)

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

    return api


# ---- helpers ------------------------------------------------------------
class _AuthRequired(Exception):
    """Internal exception raised by require_session; mapped to a flat-body
    401 by an exception handler registered inside create_app."""
    def __init__(self, reason: str) -> None:
        self.reason = reason


_CONTENT_FIELDS = {"thread", "author", "kind", "created_at", "parents",
                   "body", "blobs", "supersedes", "receipt"}


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
