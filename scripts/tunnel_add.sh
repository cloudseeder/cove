#!/usr/bin/env bash
#
# tunnel_add.sh — wire a new hub into the existing cloudflared tunnel.
#
# Companion to genesis.sh. After you've genesised a hub locally (which
# gets the container running on some port and a state dir in place),
# this script exposes it publicly:
#
#   1. Reads the tunnel name from your cloudflared config
#   2. cloudflared tunnel route dns <tunnel> <hostname>   (creates CNAME)
#   3. Inserts an ingress rule into config.yml above the catch-all 404
#   4. Reloads cloudflared so the change takes
#   5. Verifies via curl to the public /healthz
#
# Idempotent: running twice with the same args is a no-op after the
# first success.
#
# Requires root (writes to /etc/cloudflared/config.yml, runs systemctl,
# and cloudflared's cert.pem lives in /root/.cloudflared/).
#
# Usage:
#   sudo ./scripts/tunnel_add.sh <hostname> <port>
#
# Example (matches ./scripts/genesis.sh crider --port 8002 --hostname crider-hub.oap.dev):
#   sudo ./scripts/tunnel_add.sh crider-hub.oap.dev 8002
#
# Environment overrides:
#   CLOUDFLARED_CONFIG  path to cloudflared config.yml
#                       (default: /etc/cloudflared/config.yml)
#   TUNNEL              tunnel name/UUID
#                       (default: auto-detected from config)

set -euo pipefail

HOSTNAME="${1:-}"
PORT="${2:-}"
CONFIG_PATH="${CLOUDFLARED_CONFIG:-/etc/cloudflared/config.yml}"

usage() {
  cat >&2 <<EOF
usage: sudo $0 <hostname> <port>

Positional:
  hostname   FQDN to route (e.g. crider-hub.oap.dev)
  port       local port the hub is on (e.g. 8002; must match --port
             passed to genesis.sh)

Environment:
  CLOUDFLARED_CONFIG  path to cloudflared config.yml
                      (default: /etc/cloudflared/config.yml)
  TUNNEL              tunnel name/UUID override (default: read from
                      the config file's 'tunnel:' key)

What it does:
  1. Read tunnel name from cloudflared config (or \$TUNNEL if set)
  2. cloudflared tunnel route dns <tunnel> <hostname>
  3. Insert ingress rule for hostname → http://127.0.0.1:port into
     config.yml above the catch-all http_status:404 line
  4. systemctl reload cloudflared
  5. curl https://<hostname>/healthz to verify
EOF
}

if [ -z "$HOSTNAME" ] || [ -z "$PORT" ]; then
  usage
  exit 2
fi

if ! echo "$PORT" | grep -qE '^[0-9]+$'; then
  echo "error: port must be a number, got '$PORT'" >&2
  exit 2
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "error: cloudflared config not found at $CONFIG_PATH" >&2
  echo "  set CLOUDFLARED_CONFIG to the correct path if it's elsewhere" >&2
  exit 1
fi

# Colored progress helpers.
say() { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }

# ─── Step 0: tunnel name ─────────────────────────────────────────────
if [ -n "${TUNNEL:-}" ]; then
  say "Tunnel: $TUNNEL (from \$TUNNEL)"
else
  TUNNEL="$(grep -E '^[[:space:]]*tunnel:' "$CONFIG_PATH" \
    | head -1 \
    | sed -E 's/^[[:space:]]*tunnel:[[:space:]]*//' \
    | tr -d '"'"'"'' \
    | awk '{print $1}')"
  if [ -z "$TUNNEL" ]; then
    echo "error: no 'tunnel:' key found in $CONFIG_PATH" >&2
    echo "  set the TUNNEL env var to the tunnel name/UUID" >&2
    exit 1
  fi
  say "Tunnel: $TUNNEL (from $CONFIG_PATH)"
fi
echo "  hostname: $HOSTNAME"
echo "  port:     $PORT"

# ─── Step 1: DNS route via cloudflared ────────────────────────────────
# This command is idempotent when the hostname already routes to the
# same tunnel; it errors if the hostname is claimed by a different
# tunnel. We surface either outcome.
say "Step 1/4: cloudflared tunnel route dns"
if ! cloudflared tunnel route dns "$TUNNEL" "$HOSTNAME"; then
  echo "  DNS route command failed. Check the error above." >&2
  echo "  Common causes:" >&2
  echo "    - $HOSTNAME already CNAMEs to a different tunnel" >&2
  echo "    - your account lacks the DNS zone for $HOSTNAME" >&2
  echo "    - cert.pem is missing (run: cloudflared login)" >&2
  exit 1
fi

# ─── Step 2: ingress rule in config.yml ───────────────────────────────
say "Step 2/4: ingress rule in $CONFIG_PATH"
if grep -qE "^[[:space:]]*-[[:space:]]+hostname:[[:space:]]*${HOSTNAME}[[:space:]]*$" "$CONFIG_PATH"; then
  echo "  ✓ $HOSTNAME already in ingress list — leaving config as-is"
else
  # Timestamped backup — small, cheap, saves you from awk misbehaving.
  BACKUP="${CONFIG_PATH}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$CONFIG_PATH" "$BACKUP"
  echo "  backup: $BACKUP"

  # Insert the new ingress block above the catch-all http_status:404
  # line. Matches indentation of the catch-all so the inserted block
  # aligns with the existing file style.
  TMP="$(mktemp)"
  awk -v host="$HOSTNAME" -v port="$PORT" '
    /service:[[:space:]]+.*http_status:404/ && !inserted {
      # Grab the leading whitespace of the catch-all line so our new
      # block matches its indentation (usually 2 spaces under `ingress:`).
      match($0, /^[[:space:]]*/)
      indent = substr($0, RSTART, RLENGTH)
      print indent "- hostname: " host
      print indent "  service: http://127.0.0.1:" port
      inserted = 1
    }
    { print }
  ' "$CONFIG_PATH" > "$TMP"

  if ! grep -q "hostname: $HOSTNAME" "$TMP"; then
    echo "  error: didn't find a 'service: http_status:404' catch-all" >&2
    echo "    line in $CONFIG_PATH. Add the ingress rule manually:" >&2
    echo "" >&2
    echo "      - hostname: $HOSTNAME" >&2
    echo "        service: http://127.0.0.1:$PORT" >&2
    echo "" >&2
    echo "    then reload cloudflared." >&2
    rm -f "$TMP"
    exit 1
  fi
  mv "$TMP" "$CONFIG_PATH"
  echo "  ✓ inserted ingress rule"
fi

# ─── Step 3: reload cloudflared ───────────────────────────────────────
say "Step 3/4: systemctl reload cloudflared"
if systemctl reload cloudflared 2>/dev/null; then
  echo "  ✓ reloaded"
else
  echo "  reload failed — trying restart"
  systemctl restart cloudflared
  echo "  ✓ restarted"
fi

# ─── Step 4: verify ───────────────────────────────────────────────────
say "Step 4/4: verifying via https://$HOSTNAME/healthz"
sleep 2  # give the tunnel a beat to pick up the new config
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
  "https://$HOSTNAME/healthz" 2>/dev/null || echo 000)"
case "$code" in
  200)
    body="$(curl -fsS --max-time 5 "https://$HOSTNAME/healthz" || true)"
    echo "  ✓ HTTP 200: $body"
    echo
    echo "Tunnel routing is live. Send the keys."
    ;;
  502)
    echo "  ⚠ HTTP 502 — tunnel routing is fine, but nothing's listening on port $PORT"
    echo "     → run ./scripts/genesis.sh <hub-name> --port $PORT ... if you haven't yet"
    exit 1
    ;;
  404)
    echo "  ⚠ HTTP 404 — likely FastAPI's default 404 shape, which means the"
    echo "     hub answered but the root path isn't defined (normal). Try:"
    echo "       curl https://$HOSTNAME/healthz"
    echo "     If THAT returns 200, everything is working."
    ;;
  000)
    echo "  ⚠ couldn't reach $HOSTNAME — DNS may not have propagated"
    echo "     → try again in ~30s, or run: curl -v https://$HOSTNAME/healthz"
    exit 1
    ;;
  *)
    echo "  ⚠ HTTP $code from https://$HOSTNAME/healthz"
    echo "     → run: curl -v https://$HOSTNAME/healthz for detail"
    exit 1
    ;;
esac
