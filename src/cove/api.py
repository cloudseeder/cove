"""FastAPI app: wire protocol (read paths + POST /entries). Spec §7.

Routes map 1:1 to the spec's wire table. Push (WebSocket) is mandatory in v1
but lands in a later slice alongside the §5 auth module — the broadcast-notice
path goes through POST /entries + GET /sync first.

Handlers are thin: parsing + module dispatch + error mapping. All logic lives
in the modules below. The factory `create_app(deps)` wires the modules so tests
get their own isolated app and production wires real deps.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse

from .entry import BlobRef, Entry
from .identity import Directory, DirectoryManifest
from .index import Ledger, Overview
from .pipeline import AcceptanceError, Pipeline
from .store import EventStore
from .throttle import ThrottleError
from .translog import TamperEvidentLog


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
               directory_manifest: Optional[DirectoryManifest] = None) -> FastAPI:
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
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        translog.rebuild(store.iter_global())
        yield

    api = FastAPI(title="Cove Hub", version="0.1.0", lifespan=lifespan)

    @api.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": api.version}

    # ---- POST /entries (§7.1) -------------------------------------------
    @api.post("/entries")
    def post_entries(body: dict = Body(...)):
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
        return {"id": ev.id, "seq": seq}

    # ---- GET /sync (§7) -------------------------------------------------
    @api.get("/sync")
    def get_sync(thread: str = Query(...), since: int = Query(...)) -> dict:
        entries = [_entry_to_dict(ev) for ev in store.since(thread, since)]
        return {"thread": thread, "since": since, "entries": entries}

    # ---- GET /overview (§6) --------------------------------------------
    @api.get("/overview")
    def get_overview(thread: str = Query(...)) -> dict:
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
    def get_directory():
        if directory_manifest is None:
            return _err(503, error="no_directory")
        return _manifest_to_dict(directory_manifest)

    # ---- GET /ledger (§8) -----------------------------------------------
    @api.get("/ledger")
    def get_ledger(entry: str = Query(...)):
        target = store.get(entry)
        seq = store.seq_of(entry) if target is not None else None
        if target is None or seq is None:
            return _err(404, error="not_found", entry=entry)
        members = directory.attested_keys() if directory is not None else []
        return ledger.status(target.thread, required_seq=seq, members=members)

    # ---- TODO routes (need deps not yet built) -------------------------
    # POST /auth/challenge, POST /auth/verify           — §5 auth module
    # WS   /stream                                      — fan-out plumbing
    # POST /blobs, GET /blobs/{hash}                    — §4 blob store
    # POST /admin/attest, /admin/revoke, /admin/limits  — root-key / admin ops

    return api


# ---- helpers ------------------------------------------------------------
_CONTENT_FIELDS = {"thread", "author", "kind", "created_at", "parents",
                   "body", "blobs", "supersedes"}


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
    ev = Entry(**fields)
    ev.id = d.get("id")
    ev.sig = d.get("sig")
    return ev


def _entry_to_dict(ev: Entry) -> dict:
    return asdict(ev)


def _manifest_to_dict(m: DirectoryManifest) -> dict:
    return {
        "org": m.org,
        "attestations": [asdict(a) for a in m.attestations],
        "revocations": [asdict(r) for r in m.revocations],
        "updated_at": m.updated_at,
        "sig": m.sig,
    }




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
