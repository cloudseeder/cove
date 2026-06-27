"""Cryptographic primitives. Spec: server-hub-spec.md §3.1.

Thin, correctness-critical wrappers. Keep this module small and obvious.
All public keys are hex strings; all signatures are hex strings; all ids are
"sha256:"-prefixed hex. Canonicalization is RFC 8785 (JCS) so that ids and
signatures are reproducible across implementations.
"""
from __future__ import annotations

import hashlib
from typing import Any

import rfc8785
from nacl import signing
from nacl.exceptions import BadSignatureError


# ---- canonicalization & hashing -------------------------------------------

def canonicalize(obj: dict[str, Any]) -> bytes:
    """Deterministic RFC 8785 (JCS) serialization of a JSON-compatible dict."""
    return rfc8785.dumps(obj)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_id(content: dict[str, Any]) -> str:
    """Content address of an entry's `content` (all fields except id, sig)."""
    return "sha256:" + sha256_hex(canonicalize(content))


# ---- Ed25519 --------------------------------------------------------------

def generate_keypair() -> tuple[str, str]:
    """Return (private_hex, public_hex). Private stays on the owning device/host.

    The hub's operational key and each participant key are generated this way.
    The hub MUST NOT hold any member or root private key (CLAUDE.md, §1).
    """
    sk = signing.SigningKey.generate()
    return sk.encode().hex(), sk.verify_key.encode().hex()


def sign(private_hex: str, message: bytes) -> str:
    sk = signing.SigningKey(bytes.fromhex(private_hex))
    return sk.sign(message).signature.hex()


def derive_pubkey(private_hex: str) -> str:
    """Derive the Ed25519 public key from a private key hex. Used by the
    admin CLI to sanity-check that a root.priv on disk matches the org
    pubkey the hub advertises BEFORE attempting to sign a manifest with
    it — catches "wrong root key" before the hub rejects the bad sig."""
    return signing.SigningKey(bytes.fromhex(private_hex)).verify_key.encode().hex()


def verify(public_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        signing.VerifyKey(bytes.fromhex(public_hex)).verify(
            message, bytes.fromhex(signature_hex)
        )
        return True
    except (BadSignatureError, ValueError):
        return False
