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
shift 2>/dev/null || true

# Defaults derived from hub name; overridable with flags below.
REUSE_PUBKEY=""
PORT=""
HOSTNAME=""
ORG_NAME=""
KEYMASTER_NAME="Kevin Brooks"

usage() {
  cat >&2 <<EOF
usage: $0 <hub-name> [flags]

Positional:
  hub-name                short slug for the hub (e.g. lwccoa, brooks, flhoa).
                          Becomes the Compose project name, drives state dir
                          './<name>-state', container name '<name>-hub', and
                          default hostname '<name>-hub.oap.dev'.

Flags:
  --port <n>              host-side port for the tunnel to reach (default 8000
                          for lwccoa, 8001 for brooks, or the value passed here
                          for any other name — required if you already have
                          another hub on 8000).
  --hostname <fqdn>       public hostname the hub is served under (default
                          '<hub-name>-hub.oap.dev'; override if the org owns
                          its own domain).
  --org-name "<name>"     org display name baked into attestations (default
                          derived from hub-name).
  --keymaster "<name>"    display name for the first attested member
                          (default 'Kevin Brooks').
  --reuse-pubkey <64hex>  attest an existing pubkey against this hub's fresh
                          root instead of generating a new keypair. Enables
                          'same identity, N hubs' federation.

Examples:
  # LWCCOA production hub (defaults from name):
  $0 lwccoa

  # Brooks testbed (defaults from name):
  $0 brooks

  # Federation bring-up:
  $0 brooks --reuse-pubkey <your-lwccoa-pubkey>

  # New org's hub on a fresh port + hostname:
  $0 flhoa --port 8002 --hostname flhoa-hub.oap.dev --org-name "FL HOA"
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --reuse-pubkey)       REUSE_PUBKEY="$2"; shift 2 ;;
    --reuse-pubkey=*)     REUSE_PUBKEY="${1#*=}"; shift ;;
    --port)               PORT="$2"; shift 2 ;;
    --port=*)             PORT="${1#*=}"; shift ;;
    --hostname)           HOSTNAME="$2"; shift 2 ;;
    --hostname=*)         HOSTNAME="${1#*=}"; shift ;;
    --org-name)           ORG_NAME="$2"; shift 2 ;;
    --org-name=*)         ORG_NAME="${1#*=}"; shift ;;
    --keymaster)          KEYMASTER_NAME="$2"; shift 2 ;;
    --keymaster=*)        KEYMASTER_NAME="${1#*=}"; shift ;;
    -h|--help)            usage; exit 0 ;;
    *)
      echo "unknown flag: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$HUB" ]; then
  usage
  exit 2
fi

# Hub-name-derived defaults. Explicit flags override.
# lwccoa + brooks retain their historical port + org-name defaults so the
# two shipped commands (`./scripts/genesis.sh lwccoa`, `./scripts/genesis.sh
# brooks`) work exactly as before. Any other name derives from the name.
case "$HUB" in
  lwccoa)
    : "${PORT:=8000}"
    : "${ORG_NAME:=LWCCOA}"
    ;;
  brooks)
    : "${PORT:=8001}"
    : "${ORG_NAME:=Brooks testbed}"
    ;;
  *)
    : "${PORT:=8000}"
    # Title-case the hub name as a plausible org-name default:
    # 'flhoa' → 'Flhoa', 'oakwood-hoa' → 'Oakwood Hoa'. Almost always
    # worth overriding via --org-name; this just keeps the ceremony
    # runnable without a required flag.
    : "${ORG_NAME:=$(echo "$HUB" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++){$i=toupper(substr($i,1,1))substr($i,2)}}1')}"
    ;;
esac
: "${HOSTNAME:=${HUB}-hub.oap.dev}"

export COMPOSE_PROJECT_NAME="$HUB"
export COVE_HUB_PORT="$PORT"
export COVE_STATE_DIR="./${HUB}-state"
export COVE_CONTAINER_NAME="${HUB}-hub"
HUB_HOST="$HOSTNAME"

if [ -n "$REUSE_PUBKEY" ]; then
  if ! echo "$REUSE_PUBKEY" | grep -qE '^[0-9a-f]{64}$'; then
    echo "error: --reuse-pubkey must be 64 lowercase hex chars" >&2
    exit 2
  fi
fi

MEMBER_NAME="$KEYMASTER_NAME"
# Slugify keymaster name for the on-disk .priv filename: lowercase,
# non-alnum → dashes, collapse repeats, trim edges.
MEMBER_SLUG="$(echo "$KEYMASTER_NAME" \
  | tr '[:upper:]' '[:lower:]' \
  | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-\|-$//g')"

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
#
# Uses a roster CSV (not --members) so the initial attestation carries
# role=board — otherwise --members hard-codes role=member and AdminPanel
# stays hidden until you rerun scripts/rerole_member.py.
#
# When --reuse-pubkey is set, the roster carries an existing pubkey and
# bootstrap_pilot.py skips member-keypair generation (v0.4.65 pubkey
# column) — that pubkey gets attested against this hub's fresh root,
# federating the same identity across both hubs.
if [ -n "$REUSE_PUBKEY" ]; then
  say "Step 4/6: docker compose run bootstrap — fresh root, reusing pubkey ${REUSE_PUBKEY:0:12}…"
else
  say "Step 4/6: docker compose run bootstrap — fresh root + hub keypair"
fi
ROSTER_PATH="$COVE_STATE_DIR/.genesis-roster.csv"
mkdir -p "$COVE_STATE_DIR"
if [ -n "$REUSE_PUBKEY" ]; then
  cat > "$ROSTER_PATH" <<EOF
display_name,affiliation,role,title,key_name,pubkey
${MEMBER_NAME},board,board,Keymaster,${MEMBER_SLUG},${REUSE_PUBKEY}
EOF
else
  cat > "$ROSTER_PATH" <<EOF
display_name,affiliation,role,title,key_name
${MEMBER_NAME},board,board,Keymaster,${MEMBER_SLUG}
EOF
fi
docker compose --profile setup run --rm bootstrap \
  --org-name "$ORG_NAME" \
  --roster "/state/.genesis-roster.csv" \
  --force
rm -f "$ROSTER_PATH"

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
if [ -n "$REUSE_PUBKEY" ]; then
  echo "Next steps (federation mode — reused pubkey ${REUSE_PUBKEY:0:12}…):"
  echo "  1. On your PWA, you're ALREADY signed in via the vault. Open the"
  echo "     sidebar hub switcher → '+ Add another hub' → enter"
  echo "     https://${HUB_HOST}. The client reuses your live priv, and"
  echo "     this hub already has your pubkey attested — no fresh invite."
  echo "  2. Post a message from ${HUB} and verify it renders as the same"
  echo "     identity your other hub knows."
  echo "  3. Optional: create a hub-side vault on ${HUB} too (Admin →"
  echo "     Identity vault) — the client fans out vault writes to every"
  echo "     joined hub, so a slot rotation propagates automatically."
else
  echo "Next steps:"
  echo "  1. On your PWA (https://app.cove.oap.dev), sign in via AuthPanel"
  echo "     paste mode: import the ${MEMBER_SLUG}.priv + .pub you saved."
  echo "     Hub URL: https://${HUB_HOST}"
  echo "  2. Once signed in, AdminPanel → Identity vault → Add passphrase"
  echo "     seeds your hub-stored vault. Then Add Passkey for one-tap sign-in."
  echo "  3. From a second device, use the 'Signing in from a new device?'"
  echo "     surface in AuthPanel to unlock via the vault."
fi
