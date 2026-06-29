#!/usr/bin/env bash
#
# wipe_hub.sh — test-data reset for a running Cove hub.
#
# Stops cove-hub.service, backs up data/ to data.bak-YYYYMMDD-HHMMSS,
# nukes the sqlite store + blobs, restarts. Preserves keys/ and
# manifest.jsonl so members reconnect with their existing
# attestations — no re-onboarding, no new genesis manifest.
#
# After the wipe:
#   - the merkle tree starts fresh at size 0
#   - any prior STH + inclusion proof a client had cached no longer
#     verifies (that's the point — wipe means wipe)
#   - clients' cached high-water seqs are now ahead of the hub's
#     reality; on next /sync the hub returns nothing new, and the
#     UI's localStorage cove.thread may point at a now-empty thread.
#     That's transient: the inbox is empty, the user picks a thread.
#
# Run on the hub host. Sudo prompts twice (stop + start).

set -euo pipefail

STATE_DIR="${COVE_STATE_DIR:-$HOME/cove-state}"
DATA_DIR="$STATE_DIR/data"
SERVICE="cove-hub.service"

if [ ! -d "$STATE_DIR" ]; then
  echo "no state dir at $STATE_DIR — nothing to wipe" >&2
  exit 1
fi
if [ ! -d "$DATA_DIR" ]; then
  echo "no data dir at $DATA_DIR — already empty?" >&2
  exit 1
fi

cat <<EOF

============================================================
ABOUT TO WIPE the hub's entries + blobs.

  state dir:     $STATE_DIR
  sqlite store:  $DATA_DIR/cove.db (+ WAL/SHM)
  blobs:         $DATA_DIR/blobs/*

PRESERVED:
  - $STATE_DIR/keys/   (hub keypair, root.pub, attested-member dir)
  - $STATE_DIR/manifest.jsonl  (signed directory chain)

Existing members keep their attestations and reconnect normally.
The merkle tree resets to size 0 — old inclusion proofs no longer
verify (that's the point of a wipe).
============================================================

EOF

read -r -p "Type 'wipe' to confirm: " confirm
if [ "$confirm" != "wipe" ]; then
  echo "aborted." >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP="$STATE_DIR/data.bak-$STAMP"

echo "stopping $SERVICE…"
sudo systemctl stop "$SERVICE"

echo "backing up $DATA_DIR → $BACKUP"
cp -a "$DATA_DIR" "$BACKUP"

echo "clearing $DATA_DIR/cove.db* and $DATA_DIR/blobs/…"
rm -f "$DATA_DIR/cove.db" "$DATA_DIR/cove.db-wal" "$DATA_DIR/cove.db-shm"
rm -rf "$DATA_DIR/blobs"
mkdir -p "$DATA_DIR/blobs"

echo "starting $SERVICE…"
sudo systemctl start "$SERVICE"

echo
echo "✓ wipe complete. Backup at $BACKUP."
echo "  Members reconnect with their existing keys; inbox starts empty."
