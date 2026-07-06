#!/usr/bin/env bash
#
# genesis.sh — clean-slate bootstrap for one Cove hub.
#
# Usage:
#   ./scripts/genesis.sh {lwccoa|brooks}
#
# What it does, in order:
#   1.  docker compose down     — stop + remove the running container
#   2.  wipe COVE_STATE_DIR     — delete hub.db, blobs, directory chain
#   3.  docker compose build    — rebuild image so it holds current code
#   4.  bootstrap_pilot.py      — mint a fresh root + hub keypair, seed
#                                 a genesis directory with one attested
#                                 member (Kevin Brooks), print pubkeys
#   5.  HALT for custody handoff — you copy root.priv + member .priv off
#                                  the host to local storage (paste +
#                                  sha256sum verify — do NOT scp), then
#                                  the script shreds them from the host
#   6.  docker compose up -d    — start the hub with root.priv gone
#   7.  curl /healthz + /vault  — sanity-check both endpoints work
#
# Env vars per hub (matches the manual invocation we've been using):
#   lwccoa: project=lwccoa port=8000 state=./lwccoa-state
#   brooks: project=brooks port=8001 state=./brooks-state
#
# Non-negotiable #1 (CLAUDE.md): the hub NEVER holds the root private key.
# Step 5 refuses to start the container while root.priv is still on disk.

set -euo pipefail

HUB="${1:-}"
case "$HUB" in
  lwccoa)
    export COMPOSE_PROJECT_NAME=lwccoa
    export COVE_HUB_PORT=8000
    export COVE_STATE_DIR=./lwccoa-state
    export COVE_CONTAINER_NAME=lwccoa-hub
    HUB_HOST=lwccoa-hub.oap.dev
    ORG_NAME="LWCCOA"
    ;;
  brooks)
    export COMPOSE_PROJECT_NAME=brooks
    export COVE_HUB_PORT=8001
    export COVE_STATE_DIR=./brooks-state
    export COVE_CONTAINER_NAME=brooks-hub
    HUB_HOST=brooks-hub.oap.dev
    ORG_NAME="Brooks testbed"
    ;;
  *)
    echo "usage: $0 {lwccoa|brooks}" >&2
    exit 2
    ;;
esac

MEMBER_NAME="Kevin Brooks"
MEMBER_SLUG="kevin-brooks"

# Guard: must run from repo root.
[ -f docker-compose.yml ] || {
  echo "error: no docker-compose.yml in $PWD — cd to the repo root first" >&2
  exit 1
}

say() { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
confirm() {
  local ans
  read -r -p "$* [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

say "Genesis for ${HUB} (${COVE_CONTAINER_NAME} → ${HUB_HOST})"
echo "  project=${COMPOSE_PROJECT_NAME}  port=${COVE_HUB_PORT}  state=${COVE_STATE_DIR}"
tag_at_head="$(git tag --points-at HEAD 2>/dev/null | tail -1 || true)"
if [ -n "$tag_at_head" ]; then
  echo "  git HEAD: $tag_at_head  ($(git rev-parse --short HEAD))"
else
  echo "  git HEAD: $(git rev-parse --short HEAD) — NOT at a release tag"
fi

# ─── Step 1: docker compose down ─────────────────────────────────────
say "Step 1/6: docker compose down"
docker compose down

# ─── Step 2: wipe state (DESTRUCTIVE) ─────────────────────────────────
if [ -d "$COVE_STATE_DIR" ] && [ -n "$(ls -A "$COVE_STATE_DIR" 2>/dev/null)" ]; then
  say "Step 2/6: about to wipe $COVE_STATE_DIR"
  echo "  Contents:"
  ls -la "$COVE_STATE_DIR" | sed 's/^/    /'
  confirm "Wipe? This deletes hub.db, blobs, directory chain, and all keys." \
    || { echo "aborted."; exit 1; }
  sudo rm -rf "${COVE_STATE_DIR:?}"/*
else
  say "Step 2/6: state dir already empty (skipping wipe)"
fi
mkdir -p "$COVE_STATE_DIR"

# ─── Step 3: rebuild image so v0.4.76 code lands in the container ─────
say "Step 3/6: docker compose build --no-cache"
docker compose build --no-cache

# ─── Step 4: bootstrap fresh root ─────────────────────────────────────
# Runs INSIDE the freshly-built container via the `bootstrap` compose
# service (docker-compose.yml, profile=setup). That way we reuse the
# same Python + deps the hub itself uses — no touching the host's
# system Python (which would trip the "running pip as root" warning
# and pollute site-packages).
say "Step 4/6: docker compose run bootstrap — fresh root + hub keypair"
docker compose --profile setup run --rm bootstrap \
  --org-name "$ORG_NAME" \
  --members "$MEMBER_NAME" \
  --force

# The bootstrap container runs as uid 1000 (the `cove` user), so the
# files it writes into the host-mounted state dir may not be owned by
# the invoking shell user. Chown so `shred -u` below can delete them
# without another sudo prompt mid-flow.
if [ "$(stat -c '%u' "$COVE_STATE_DIR/keys/root.priv" 2>/dev/null || echo 0)" != "$(id -u)" ]; then
  say "Chowning $COVE_STATE_DIR back to $(id -un)"
  sudo chown -R "$(id -u):$(id -g)" "$COVE_STATE_DIR"
fi

ROOT_PRIV_PATH="$COVE_STATE_DIR/keys/root.priv"
ROOT_PUB_PATH="$COVE_STATE_DIR/keys/root.pub"
MEMBER_PRIV_PATH="$COVE_STATE_DIR/keys/members/${MEMBER_SLUG}.priv"
MEMBER_PUB_PATH="$COVE_STATE_DIR/keys/members/${MEMBER_SLUG}.pub"

# ─── Step 5: custody handoff — halt until priv material leaves host ───
say "Step 5/6: custody handoff — root.priv MUST leave this host"
echo
echo "  Files to move off the host (via paste-into-file + sha256sum verify,"
echo "  NOT scp — brace-expansion has clobbered these before):"
echo
printf "    %-52s  %s\n" "path" "sha256"
printf "    %s\n" "----------------------------------------------------  ----------------------------------------------------------------"
for p in "$ROOT_PRIV_PATH" "$ROOT_PUB_PATH" "$MEMBER_PRIV_PATH" "$MEMBER_PUB_PATH"; do
  if [ -f "$p" ]; then
    printf "    %-52s  %s\n" "$p" "$(sha256sum "$p" | awk '{print $1}')"
  fi
done
echo
echo "  On the receiving machine (Mac / password manager / offline USB):"
echo "    1. Paste each file's contents into a matching file."
echo "    2. sha256sum each — must match the hashes above verbatim."
echo "    3. Save root.priv + root.pub in offline storage."
echo "    4. Save the member .priv + .pub for your PWA / desktop identity."
echo
if ! confirm "Handoff complete — ready to shred both privs from THIS host?"; then
  echo
  echo "aborted. The hub MUST NOT start with root.priv on disk."
  echo "Re-run the script once the handoff is done, OR shred manually:"
  echo "  shred -u $ROOT_PRIV_PATH $MEMBER_PRIV_PATH"
  exit 1
fi

echo "  Shredding root.priv + ${MEMBER_SLUG}.priv from host…"
shred -u "$ROOT_PRIV_PATH"
[ -f "$MEMBER_PRIV_PATH" ] && shred -u "$MEMBER_PRIV_PATH"
echo "  Done. root.pub and hub.priv stay — the hub needs both to serve."

# ─── Step 6: bring the hub up + sanity check ─────────────────────────
say "Step 6/6: docker compose up -d hub"
docker compose up -d hub

say "Waiting for /healthz on localhost:${COVE_HUB_PORT}"
for _ in {1..30}; do
  if curl -fsS "http://localhost:${COVE_HUB_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo
echo "─── Local sanity ────────────────────────────────────────────────"
printf "  /healthz          "
curl -fsS "http://localhost:${COVE_HUB_PORT}/healthz" || echo "(failed)"
echo
printf "  /vault/deadbeef   "
curl -sS -o /dev/null -w "HTTP %{http_code}  (expect 404 vault_not_found)\n" \
  "http://localhost:${COVE_HUB_PORT}/vault/deadbeef" || true

echo
echo "─── Public sanity (may lag if cloudflared is warming up) ────────"
printf "  /healthz          "
curl -fsS --max-time 5 "https://${HUB_HOST}/healthz" 2>/dev/null || echo "(not up yet)"
echo
printf "  /vault/deadbeef   "
curl -sS --max-time 5 -o /dev/null -w "HTTP %{http_code}  (expect 404)\n" \
  "https://${HUB_HOST}/vault/deadbeef" 2>/dev/null || true

echo
say "Genesis for ${HUB} complete."
echo
echo "Next steps:"
echo "  1. On your PWA (https://app.cove.oap.dev), sign in via AuthPanel"
echo "     paste mode: import the ${MEMBER_SLUG}.priv + .pub you saved."
echo "     Hub URL: https://${HUB_HOST}"
echo "  2. Once signed in, AdminPanel → Identity vault → Add passphrase"
echo "     seeds your hub-stored vault. Then Add Passkey for one-tap sign-in."
echo "  3. From a second device, use the 'Signing in from a new device?'"
echo "     surface in AuthPanel to unlock via the vault."
