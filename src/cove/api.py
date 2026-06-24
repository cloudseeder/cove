"""FastAPI app: wire protocol + WebSocket fan-out. Spec: server-hub-spec.md §7.

Routes map 1:1 to the spec's wire table. Push (WebSocket) is mandatory — clients
must not have to poll (client-spec.md §4). Keep handlers thin; logic lives in the
modules above.
"""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Accountable Thread Hub", version="0.1.0")

# --- auth (§5) ---
# POST /auth/challenge  -> nonce
# POST /auth/verify     -> session token (challenge-response, no passwords)

# --- directory (§2) ---
# GET  /directory       -> root-signed manifest (authenticated)

# --- entries (§7.1) ---
# POST /entries          -> run Pipeline.accept; structured throttle errors (§7.2.3)
# GET  /sync            -> delta-sync since seq
# WS   /stream          -> push entries for the member's threads

# --- rendering (§6) ---
# GET  /overview        -> child map + seq order

# --- tamper-evident log (§6.4) ---
# GET  /sth                  -> latest Signed Tree Head
# GET  /proof/inclusion      -> inclusion proof
# GET  /proof/consistency    -> consistency proof

# --- blobs (§4) ---
# POST /blobs           -> content-addressed upload (storage quota enforced)
# GET  /blobs/{hash}    -> download

# --- ledger (§8) ---
# GET  /ledger          -> who has / has NOT acked

# --- admin (root/admin) ---
# POST /admin/attest    -> issue attestation (signs with ROOT key, off-hub in practice)
# POST /admin/revoke    -> revoke
# POST /admin/limits    -> per-identity throttle overrides (§7.2.2)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": app.version}
