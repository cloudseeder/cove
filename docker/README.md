# Running a Cove hub with Docker

The minimum path from a fresh Linux host to a live Cove hub. Everything below assumes Docker + Docker Compose plugin, Python-free host, root/sudo for the docker daemon.

## What you'll end up with

- A Cove hub running in a container, listening on `127.0.0.1:8000`
- All state (keys, sqlite db, blobs, manifest chain) in a `./cove-state/` directory next to the compose file — backup that directory and you can restore the hub anywhere
- The root private key **moved off the host** to somewhere you control (USB, password manager, offline machine). The hub refuses to start if it finds `keys/root.priv` on-disk — that refusal is the point.

## Prerequisites

- A Linux (or macOS) host with Docker + `docker compose` plugin
- A domain name pointed at the host (or a Cloudflare Tunnel, see below)
- A TLS story (reverse proxy, tunnel, or Caddy) — the hub itself does not terminate TLS

## 1. Get the source

```sh
git clone https://github.com/cloudseeder/cove.git
cd cove
```

If you'd rather not clone the whole client + landing repo, you only need `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `src/`, and `scripts/`. Everything else is client / marketing / docs.

## 2. Run the genesis ceremony

This is a **one-time** step. It generates the root + hub keypairs, signs the initial directory manifest, and writes state to `./cove-state/`.

```sh
mkdir -p ./cove-state
docker compose --profile setup run --rm bootstrap \
    --org-name "Your Org Name" \
    --members keymaster
```

`--members` is a comma-separated list of member handles the ceremony creates keypairs for. For a one-person bootstrap where you (the operator) are the keymaster, `keymaster` alone is enough — everyone else onboards later via invite codes ([v0.4.33 flow](../CHANGELOG.md#0433)) once the hub is up.

After the ceremony:

```
cove-state/
  keys/
    root.priv        ← MOVE OFFLINE. See §3.
    root.pub
    hub.priv         ← stays here (hub needs it to sign STHs)
    hub.pub
    members/
      keymaster.priv ← give to whoever's the keymaster; delete here after
      keymaster.pub
  manifest.json      ← signed genesis manifest
  manifest.jsonl     ← manifest chain head
  data/              ← SQLite + blobs, created lazily
```

## 3. ⚠️ Move `root.priv` offline. Non-negotiable.

Cove's whole security model depends on the root private key not living on the running hub. If it stays, an attacker who compromises the host can forge attestations, revoke members, and rewrite the directory.

```sh
# Example: encrypt to a passphrase-protected file for backup,
# then delete the plaintext copy on the host.
gpg --symmetric --output ~/root.priv.gpg cove-state/keys/root.priv
shred -u cove-state/keys/root.priv    # or just rm on filesystems without shred

# Verify:
ls cove-state/keys/root.priv          # should say: No such file or directory
```

Store the encrypted backup somewhere off the host — USB, password manager, offline machine, paper. When you need to revoke a member or add a new attestation later, you'll temporarily decrypt it, sign the new manifest on your workstation, and never let it touch the hub again.

The hub runner refuses to start if it finds `keys/root.priv` on the state volume — that's not paranoia, that's the security model enforcing itself.

## 4. Bring up the hub

```sh
docker compose up -d hub
docker compose logs -f hub    # watch it come up
```

You should see uvicorn bind to `0.0.0.0:8000` inside the container (exposed at `127.0.0.1:8000` on the host, per `docker-compose.yml`).

Verify:

```sh
curl http://127.0.0.1:8000/healthz
# → {"status":"ok","version":"0.1.0"}
```

## 5. Put TLS in front of it

The hub binds to loopback by design — you never expose port 8000 to the internet directly. Two clean options:

**Cloudflare Tunnel** (recommended if you already use Cloudflare for DNS):

```sh
# On the host, once:
cloudflared tunnel login
cloudflared tunnel create cove
cloudflared tunnel route dns cove hub.yourorg.example
# Point the tunnel at http://127.0.0.1:8000 in ~/.cloudflared/config.yml
cloudflared tunnel run cove
```

Zero exposed ports, automatic TLS, and no need for a public IP on the host. This is the setup used by the LWCCOA pilot at cove.oap.dev.

**Caddy** (simplest if you have a public IP):

```
# Caddyfile
hub.yourorg.example {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy auto-provisions Let's Encrypt certificates and reverse-proxies to the hub.

## 6. Onboard the keymaster

Give whoever holds `cove-state/keys/members/keymaster.priv` a way to import it into the client. Options today:

- **macOS desktop app** — the DMG is the best-supported client. Signed and notarized. See the pinned release on GitHub.
- **PWA at app.cove.oap.dev** — works on any Safari/Chrome. Paste the private key into the client to unlock; from v0.4.34 onward, keys survive tab close via a passphrase-encrypted vault.

Once the keymaster is authenticated, they can mint invite codes for the rest of the group from the Admin panel (v0.4.33 flow).

## Upgrading

```sh
git pull
docker compose build hub
docker compose up -d hub
```

State is on the mounted volume, so upgrades don't touch data. If a release has schema changes, the runner's startup rebuild handles migration.

## Backup

```sh
tar -czf cove-state-backup-$(date +%Y%m%d).tar.gz cove-state/
```

Store it wherever you keep other server backups. Include `manifest.jsonl` — it's the tamper-evident chain of every directory change, and rebuilding without it means losing member history.

## Wiping (test-data reset)

If you're testing and want to start over WITHOUT re-doing the genesis ceremony, `scripts/wipe_hub.sh` clears the sqlite store + blobs while preserving keys/ and manifest.jsonl. Run it against a hub state directory, not the container — it targets the local filesystem.
