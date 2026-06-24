"""Foundation tests — these should PASS against the seeded crypto.py/entries.py.

If these fail, the content-addressing or signature base is broken and nothing
downstream can be trusted. Spec §3, §3.1.
"""
from cove import crypto
from cove.entry import Entry, sign_entry, verify_entry, compute_id


def _evt(author):
    return Entry(thread="root", author=author, kind="post",
                 created_at="2026-01-01T00:00:00Z", body="hello")


def test_canonicalization_is_deterministic():
    a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}
    assert crypto.canonicalize(a) == crypto.canonicalize(b)


def test_sign_and_verify_roundtrip(keypair):
    priv, pub = keypair
    ev = sign_entry(_evt(pub), priv)
    assert ev.id and ev.sig
    assert verify_entry(ev) is True


def test_id_is_content_address(keypair):
    priv, pub = keypair
    ev = sign_entry(_evt(pub), priv)
    assert ev.id == compute_id(ev)


def test_tamper_breaks_verification(keypair):
    priv, pub = keypair
    ev = sign_entry(_evt(pub), priv)
    ev.body = "tampered"        # body changed but id/sig not recomputed
    assert verify_entry(ev) is False


def test_wrong_author_key_fails(keypair):
    priv, pub = keypair
    other_priv, other_pub = crypto.generate_keypair()
    ev = _evt(other_pub)        # claims to be from other_pub
    ev = sign_entry(ev, priv)   # but signed by priv -> id matches, sig won't verify vs other_pub
    assert verify_entry(ev) is False
