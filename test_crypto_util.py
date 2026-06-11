"""Tests for crypto_util: Fernet round-trip of token strings."""
import importlib

import pytest
from cryptography.fernet import Fernet

import config
import crypto_util


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', k)
    crypto_util._reset_cache()  # drop any cached Fernet built from a prior key
    return k


def test_round_trip(key):
    token = 'sk-ant-oat01-secret-value'
    enc = crypto_util.encrypt(token)
    assert enc != token
    assert crypto_util.decrypt(enc) == token


def test_ciphertext_differs_each_call(key):
    # Fernet embeds a random IV/timestamp, so two encryptions differ
    assert crypto_util.encrypt('x') != crypto_util.encrypt('x')


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(config, 'TOKEN_ENCRYPTION_KEY', None)
    crypto_util._reset_cache()
    with pytest.raises(crypto_util.CryptoError):
        crypto_util.encrypt('x')
