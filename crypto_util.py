"""Encrypt/decrypt OAuth token strings at rest with Fernet.

Key comes from config.TOKEN_ENCRYPTION_KEY (env TOKEN_ENCRYPTION_KEY). The
Fernet instance is cached; _reset_cache() exists so tests can swap keys.
"""
from cryptography.fernet import Fernet

import config

_fernet = None


class CryptoError(Exception):
    """Raised when encryption is requested without a configured key."""


def _reset_cache() -> None:
    global _fernet
    _fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.TOKEN_ENCRYPTION_KEY
        if not key:
            raise CryptoError(
                'TOKEN_ENCRYPTION_KEY is not set — cannot encrypt/decrypt tokens')
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
