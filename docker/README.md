# Running a Cove hub with Docker

The minimum path from a fresh Linux host to a live Cove hub. Everything below assumes Docker + Docker Compose plugin, Python-free host, root/sudo for the docker daemon.

## What you'll end up with

- A Cove hub running in a container, listening on `127.0.0.1:8000`
- All state (keys, sqlite db, blobs, manifest chain) in a `./cove-state/` directory next to the compose file — backup that directory and you can restore the hub anywhere
- The root private key **moved off the host** to somewhere you control (USB, password manager, offline machine). The hub refuses to start if it finds `keys/root.priv` on-disk — that refusal is the point.

## Prerequisites

- A Linux (or macOS) host with Docker + `docker compose` plugin (v2). On Debian 12 the base repo only ships legacy `docker-compose` v1 — install v2 as a CLI plugin from Docker's GitHub releases:
  ```sh
  sudo apt install -y docker.io
  sudo usermod -aG docker $USER
  mkdir -p ~/.docker/cli-plugins
  curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
      -o ~/.docker/cli-plugins/docker-compose
  chmod +x ~/.docker/cli-plugins/docker-compose
  # In a fresh shell (or via `newgrp docker`), verify:
  docker compose version
  ```
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

**Do the `mkdir` yourself first.** If the state directory doesn't exist when compose runs, the docker daemon creates it as `root:root` and the container's non-root `cove` user can't write into it. Pre-creating it as your host user avoids the `PermissionError: [Errno 13]` you'd otherwise hit inside bootstrap.

```sh
mkdir -p ./cove-state
docker compose --profile setup run --rm bootstrap \
    --org-name "Your Org Name" \
    --members keymaster
```

`--members` is a comma-separated list of member handles the ceremony creates keypairs for. For a one-person bootstrap where you (the operator) are the keymaster, `keymaster` alone is enough — everyone else onboards later via invite codes ([v0.4.33 flow](../CHANGELOG.md#0433)) once the hub is up.

**Important role gotcha.** `--members` **always** attests each name at `role=member`. Under the default capability map (board → admin+archive, nobody else has caps), that means the keymaster you just bootstrapped **cannot** mint invite codes, edit groups, revoke members, or archive threads — everything gated on the `admin` capability is closed to them. Fine if you're bootstrapping a hub where a separate board will be attested later, wrong if the keymaster IS the sole admin.

For a solo-admin bootstrap (personal testbed, keymaster-is-you), use the roster CSV path in the next section instead — it lets you set `role=board`.

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

### Alternate: roster CSV (solo-admin, federation-friendly, or multi-role bootstrap)

The `--roster` flag lets you attest members with fine-grained control over role, affiliation, title, and pubkey.

Write a small CSV:

```csv
display_name,affiliation,role,pubkey
Alice,Your Org,board,
```

Required columns: `display_name`, `affiliation`, `role` (`member`|`officer`|`board`). Optional: `title`, `key_name`, `pubkey`.

- Leaving `pubkey` empty → bootstrap generates a fresh keypair for that row, drops the `.priv` at `keys/members/<key_name>.priv` (hand it off to the person, then delete the on-host copy per §3).
- Filling `pubkey` with a 64-char hex value → bootstrap **skips** keypair generation and attests the provided pubkey directly. Enables federation-friendly bring-ups: a keymaster with an existing Cove identity (already attested by another hub, paired to a vault, whatever) is attested here under the same pubkey, so **one keypair works across N hubs**. The bootstrap output flags each row with `[pubkey provided — no .priv written]` and drops the "hand each member their .priv" step from the custody banner since there's nothing to hand off.

Mount the CSV into the bootstrap container and pass `--roster`:

```sh
docker compose --profile setup run --rm \
    -v $(pwd)/roster.csv:/roster.csv:ro bootstrap \
    --org-name "Your Org" --roster /roster.csv
```

**Getting a user's pubkey.** The client stores each user's private key locally and never leaves the device, so pubkey isn't obvious. In the desktop app / PWA (v0.4.65+), the bottom of the left sidebar shows a truncated pubkey chip below your display name — **click it to copy the full 64-char hex to clipboard**. Paste that into the `pubkey` column when attesting on another hub.

## 3. ⚠️ Move `root.priv` offline. Non-negotiable.

Cove's whole security model depends on the root private key not living on the running hub. If it stays, an attacker who compromises the host can forge attestations, revoke members, and rewrite the directory.

The essential move is **get the bits somewhere durable that isn't this host, then delete locally**. How you do the first half is your choice — password manager, physical media, an encrypted archive on a workstation, whatever your team's practice is:

```sh
# One option: read the hex and paste into a password manager.
cat cove-state/keys/root.priv

# Another: copy to physical media / another host.
# scp cove-state/keys/root.priv you@workstation:/somewhere-safe/

# Then delete the on-host copy.
shred -u cove-state/keys/root.priv    # or just rm on filesystems without shred

# Verify:
ls cove-state/keys/root.priv          # should say: No such file or directory
```

Store the offline backup somewhere durable — USB, password manager, offline machine, paper. When you need to revoke a member or add a new attestation later, you'll temporarily bring it back to a signing workstation, sign the new manifest, and never let it touch the hub again.

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

# The directory the ceremony signed:
curl http://127.0.0.1:8000/directory | head -c 200

# The initial signed tree head — empty tree, hub key signature:
curl http://127.0.0.1:8000/sth
```

**Port already in use?** If you already run something on `:8000` on this host — a dev process, or another Cove hub — edit `docker-compose.yml`'s `ports:` line to bind a different host port (e.g. `127.0.0.1:8001:8000`).

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

## Running a second hub on the same host (personal testbed)

Common case: a production hub is already running (systemd or an earlier `docker compose up`) on port 8000 and you want a separate testbed you can wipe/rebuild without touching production. The compose file is parameterized so you don't edit it — you set three env vars per invocation:

| Var | Default | Change to |
|---|---|---|
| `COVE_HUB_PORT` | `8000` | free port (e.g. `8001`) |
| `COVE_STATE_DIR` | `./cove-state` | distinct dir (e.g. `./testbed-state`) |
| `COVE_CONTAINER_NAME` | `cove-hub` | distinct name (e.g. `cove-testbed`) |

Two ways to set them.

**Ad-hoc** — one command at a time:

```sh
COVE_HUB_PORT=8001 COVE_STATE_DIR=./testbed-state COVE_CONTAINER_NAME=cove-testbed \
    docker compose --profile setup run --rm bootstrap \
    --org-name "Brooks Testbed" --members brooks

# Move testbed-state/keys/root.priv offline (§3 in the main flow).

COVE_HUB_PORT=8001 COVE_STATE_DIR=./testbed-state COVE_CONTAINER_NAME=cove-testbed \
    docker compose up -d hub
```

**Persistent** — copy `.env.example` and edit:

```sh
cp .env.example testbed.env
$EDITOR testbed.env   # set the three vars

docker compose --env-file testbed.env --profile setup run --rm bootstrap \
    --org-name "Brooks Testbed" --members brooks
# ... move root.priv offline ...
docker compose --env-file testbed.env up -d hub
```

Now `docker ps` shows both `cove-hub` (production) and `cove-testbed` (testbed) side by side. `docker logs -f cove-testbed` follows just the testbed. Wiping the testbed is `rm -rf ./testbed-state` (or `scripts/wipe_hub.sh` pointed at it) — the production hub is untouched.

**TLS for the testbed.** Same choices as §5. If you already run the production hub behind Cloudflare Tunnel at `hub.yourorg.example`, add a second `hostname` in `~/.cloudflared/config.yml` pointing at `http://127.0.0.1:8001` — one `cloudflared` process can serve multiple hostnames.

**Which hub does my client talk to?** The desktop app and PWA both remember the hub URL you paired against. Point the testbed session at the new URL/port (or the tunneled hostname) and it keeps the LWCCOA session on the production URL. This does mean two separate client installs / browser profiles today — the "one client, N hubs" federation UI is banked in [[deferred-slices]] and not yet built.

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
