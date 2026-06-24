"""Shared fixtures."""
import pytest
from cove import crypto


@pytest.fixture
def keypair():
    priv, pub = crypto.generate_keypair()
    return priv, pub


@pytest.fixture
def hub_keypair():
    """The hub operational key used to sign STHs (translog)."""
    priv, pub = crypto.generate_keypair()
    return priv, pub


@pytest.fixture
def root_keypair():
    """The org root key. Lives offline; signs attestations and directory manifests. §2."""
    priv, pub = crypto.generate_keypair()
    return priv, pub
