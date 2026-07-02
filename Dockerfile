# Cove hub — minimal, self-hostable Docker image.
#
# What this image is: everything the hub needs at runtime — Python
# 3.11-slim, the cove package, and the runner script. State (keys,
# manifest, sqlite, blobs) lives in a mounted volume, NEVER baked in.
#
# What this image is NOT: an all-in-one turnkey deployment. Genesis
# state (root + hub keypairs, signed manifest) is produced by a
# separate bootstrap ceremony documented in docker/README.md.
#
# Non-negotiable #1 from CLAUDE.md: the root private key must NEVER
# live on the running hub. The runner refuses to start if it finds
# keys/root.priv in state. This is enforced in scripts/run_hub.py,
# not here — but it's why the image doesn't try to be clever about
# key generation.

FROM python:3.11-slim AS base

# System deps kept minimal — we only need what fastapi/uvicorn/pynacl
# pull in transitively. curl is included for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching — deps change less
# than app code, so this layer sticks across most rebuilds).
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir -e .

# Non-root runtime user. The state volume is chowned to this UID/GID
# by the entrypoint so bind-mounts from the host work regardless of
# the host user's uid.
RUN useradd --uid 1000 --create-home --shell /bin/bash cove
USER cove

# State lives under /state — expected to be a mounted volume in
# production. COVE_STATE_DIR points scripts/run_hub.py at it.
ENV COVE_STATE_DIR=/state
VOLUME ["/state"]

EXPOSE 8000

# Healthcheck hits the hub's /healthz endpoint. Fails cheaply if the
# hub can't bind, and gives docker/orchestrators a signal for restart
# policies.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Default command runs the hub. The bootstrap ceremony is invoked
# explicitly via `docker compose run bootstrap` (see docker-compose.yml).
CMD ["uvicorn", "scripts.run_hub:app", "--host", "0.0.0.0", "--port", "8000"]
