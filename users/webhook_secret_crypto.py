"""Encryption helpers for persisted webhook signing secrets."""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


ENCRYPTED_PREFIX = 'whsec:v1:'


def is_encrypted_webhook_secret(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


def _normalize_fernet_key(raw_key: str) -> bytes:
    key_text = (raw_key or '').strip()
    key_bytes = key_text.encode('utf-8')

    try:
        decoded = base64.urlsafe_b64decode(key_bytes)
        if len(decoded) == 32:
            return key_bytes
    except Exception:
        pass

    return base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    configured = getattr(settings, 'JOB_CONNECTION_STRING_ENCRYPTION_KEY', '')
    key_source = configured or settings.SECRET_KEY
    return Fernet(_normalize_fernet_key(key_source))


def encrypt_webhook_secret(secret: str | None) -> str:
    if secret is None:
        return ''
    if not isinstance(secret, str):
        raise TypeError('webhook secret must be a string or None')

    normalized = secret.strip()
    if not normalized:
        return ''
    if is_encrypted_webhook_secret(normalized):
        return normalized

    encrypted = _fernet().encrypt(normalized.encode('utf-8')).decode('utf-8')
    return f'{ENCRYPTED_PREFIX}{encrypted}'


def decrypt_webhook_secret(secret: str | None) -> str:
    if secret is None:
        return ''
    if not isinstance(secret, str):
        raise TypeError('webhook secret must be a string or None')

    if not secret:
        return ''

    if not is_encrypted_webhook_secret(secret):
        # Backward compatibility for legacy plaintext rows.
        return secret

    token = secret[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ValueError('Stored webhook secret could not be decrypted.') from exc
