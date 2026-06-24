"""Cove Python client library.

The substrate every non-hub surface stands on:

  - the Tauri/SvelteKit app's verification logic mirrors this lib in TS
    (small enough surface that two implementations is cheaper than
    embedding Python in Rust);
  - the future cove.agent MCP server uses it directly to expose Cove
    tools to LLM consumers;
  - the test harness uses it to drive integration tests from outside
    the FastAPI TestClient.

What this library does NOT do:

  - render UI. VerifiedEntry carries the data a UI needs (origin role,
    signature chain summary) but the rendering is the surface's job.
  - persist anything. Slice-1 holds session + high-water state in memory
    only. A storage layer lands as a follow-up.
  - throttle backoff. A 429 response surfaces as a ClientError; queueing
    and retry are the caller's responsibility for now.
  - WebSocket subscription. /sync only in slice 1; live push lands in a
    follow-up.
"""
from .client import (
    Client,
    ClientError,
    VerificationError,
    AuthenticationError,
    VerifiedEntry,
)

__all__ = [
    "Client",
    "ClientError",
    "VerificationError",
    "AuthenticationError",
    "VerifiedEntry",
]
