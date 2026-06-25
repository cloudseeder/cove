# Cove hub deployment — pilot runbook

This is the runbook for getting a Cove hub from a fresh clone to
publicly reachable at a `cove.oap.dev`-style URL via Cloudflare Tunnel.

Posture: pilot. Single Debian host, runs as the dev user, state under
`~/cove-state`. The production move (dedicated `cove` user,
`/var/lib/cove`, dedicated VPS) is a config tweak away — see notes at
the bottom.

## 1. Bootstrap the hub state

One-time. Generates root + hub + member keypairs, signs the genesis
directory manifest. Root private key touches the box once and must be
moved offline immediately after.

```bash
cd ~/dev/cove
source .venv/bin/activate
python scripts/bootstrap_pilot.py \
    --org-name "LWCCOA" \
    --members alice,bob,carol \
    --state-dir ~/cove-state
```

When it finishes, **do what it tells you**:

```bash
# 1. Copy the root private key somewhere safe (USB, password manager).
#    Then destroy the on-disk copy:
shred -u ~/cove-state/keys/root.priv

# 2. Distribute member .priv files to each member, then delete the
#    on-disk copies. For testing today you can leave one (e.g. alice)
#    in place to paste into the Tauri client.
```

The hub's runner (`scripts/run_hub.py`) refuses to start if
`root.priv` is still present — that's a deliberate guardrail, not a
bug.

## 2. Install the systemd unit for the hub

```bash
sudo cp deploy/cove-hub.service /etc/systemd/system/cove-hub.service
sudo systemctl daemon-reload
sudo systemctl enable --now cove-hub.service
sudo systemctl status cove-hub.service     # should be active (running)
curl -s http://127.0.0.1:8000/healthz       # {"status":"ok",...}
```

Logs:
```bash
sudo journalctl -u cove-hub.service -f
```

## 3. Cloudflare Tunnel → cove.oap.dev

### Install `cloudflared`

```bash
curl -L \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared --version
```

### Authenticate against the oap.dev zone

```bash
cloudflared tunnel login
```

This prints a URL. Copy it to a browser logged into the Cloudflare
account that owns `oap.dev`, pick the `oap.dev` zone, click
Authorize. A `cert.pem` lands in `~/.cloudflared/`.

### Create the tunnel + DNS route

```bash
cloudflared tunnel create cove-pilot
# Note the TUNNEL UUID and credentials file path it prints.

cloudflared tunnel route dns cove-pilot cove.oap.dev
# Creates the CNAME — cove.oap.dev → <uuid>.cfargotunnel.com
```

### Wire the config

```bash
cp deploy/cloudflared-config.yml.example ~/.cloudflared/config.yml
# Edit ~/.cloudflared/config.yml — replace BOTH TUNNEL_UUID
# occurrences with the UUID from the create step. The template
# uses /etc/cloudflared/ paths because `service install` (next step)
# runs as the `cloudflared` user which cannot read user home dirs.
```

### Install as a system service

```bash
sudo cloudflared service install
# This copies ~/.cloudflared/config.yml + cert.pem to /etc/cloudflared/
# but does NOT copy the per-tunnel credentials JSON. Do that manually:
sudo cp ~/.cloudflared/*.json /etc/cloudflared/
sudo chmod 644 /etc/cloudflared/*.json

sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared    # active (running)
```

**Gotcha that bit us once already.** If you skip the credentials copy
above, the service fails to start with `Tunnel credentials file
'/home/<user>/.cloudflared/<uuid>.json' doesn't exist` — the
`cloudflared` system user can't see your home directory. Symptom:
`systemctl status cloudflared` shows `failed (exit-code)`. The fix is
the two-line copy + chmod above.

### Verify

```bash
curl https://cove.oap.dev/healthz
# {"status":"ok","version":"..."}
```

If you get TLS error / cert mismatch: DNS hasn't propagated yet.
Usually under a minute on Cloudflare; up to 5 worst case.

## 4. Connect the Tauri client

In the AppImage (or `pnpm tauri dev`):

1. Drop alice's keypair into the AuthPanel (or paste in browser mode).
2. Hub URL: `https://cove.oap.dev`
3. Click connect.

If the Seal renders gold on the first entry you post, the loop is
closed — Python hub, TS verification, signed Ed25519, cryptographic
update channel, the works.

## Production migration (later)

When you outgrow the dev-user posture:

- Create a dedicated `cove` user with `/var/lib/cove` as home.
- Re-run bootstrap with `--state-dir /var/lib/cove`.
- Edit `deploy/cove-hub.service`: `User=cove`, `Group=cove`,
  `WorkingDirectory=` whichever venv path, `Environment=COVE_STATE_DIR=/var/lib/cove`,
  `ReadWritePaths=/var/lib/cove`.
- Move the box to a VPS (Hetzner CPX11 / DO $5 / Linode Nanode) and
  re-route the tunnel from there.

Everything else stays the same — the runner doesn't care where state
lives, only that `COVE_STATE_DIR` points at a valid bootstrap.
