"""Encryption helpers for persisted PostgreSQL connection strings."""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


ENCRYPTED_PREFIX = 'enc:v1:'


def is_encrypted_connection_string(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


def _normalize_fernet_key(raw_key: str) -> bytes:
    key_text = (raw_key or '').strip()
    key_bytes = key_text.encode('utf-8')

    # Accept a valid Fernet key directly when provided.
    try:
        decoded = base64.urlsafe_b64decode(key_bytes)
        if len(decoded) == 32:
            return key_bytes
    except Exception:
        pass

    # Otherwise derive a valid key from the configured secret.
    return base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    configured = getattr(settings, 'JOB_CONNECTION_STRING_ENCRYPTION_KEY', '')
    key_source = configured or settings.SECRET_KEY
    return Fernet(_normalize_fernet_key(key_source))


def encrypt_connection_string(connection_string: str | None) -> str | None:
    if connection_string is None:
        return None
    if not isinstance(connection_string, str):
        raise TypeError('connection_string must be a string or None')
    if not connection_string:
        return connection_string
    if is_encrypted_connection_string(connection_string):
        return connection_string

    encrypted = _fernet().encrypt(connection_string.encode('utf-8')).decode('utf-8')
    return f'{ENCRYPTED_PREFIX}{encrypted}'


def decrypt_connection_string(connection_string: str | None) -> str | None:
    if connection_string is None:
        return None
    if not isinstance(connection_string, str):
        raise TypeError('connection_string must be a string or None')
    if not connection_string:
        return connection_string
    if not is_encrypted_connection_string(connection_string):
        # Backward compatibility for legacy plaintext rows.
        return connection_string

    token = connection_string[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError('Stored connection_string could not be decrypted.') from exc
