#!/usr/bin/env bash
#
# sync_landing_specs.sh — copy docs/*.md into landing/specs/ so the
# love.cove.oap.dev spec viewer stays in sync with the canonical files
# under docs/.
#
# Run this whenever docs/*.md changes and you want love to pick it up.
# The landing site is a plain HTML deploy (no build step) — this is the
# sync step that stands in for one.
#
# Usage: ./scripts/sync_landing_specs.sh
#        (from repo root)

set -euo pipefail

[ -d docs ] && [ -d landing/specs ] || {
  echo "run from repo root (need docs/ and landing/specs/ present)" >&2
  exit 1
}

declare -A map=(
  [docs/server-hub-spec.md]=landing/specs/hub.md
  [docs/client-spec.md]=landing/specs/client.md
  [docs/identity-vault-spec.md]=landing/specs/vault.md
)

for src in "${!map[@]}"; do
  dst="${map[$src]}"
  if ! [ -f "$src" ]; then
    echo "warn: $src missing, skipping" >&2
    continue
  fi
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "  = $dst (unchanged)"
  else
    cp "$src" "$dst"
    echo "  ↦ $dst (updated)"
  fi
done

echo
echo "Sync done. If any file was updated, commit both docs/ and landing/specs/."
