"""
secret_storage.py

Fernet-based symmetric encryption for secrets stored in the SQLite DB.
New values use a versioned `enc:v2:` envelope. Existing `enc:` Fernet values
remain readable and are upgraded by the database migrations.

Legacy values beginning with `enc:` cannot be distinguished from malformed
ciphertext without authenticating them using the current key. Migrations
therefore stop on any unauthenticated legacy-prefixed value instead of
reinterpreting it as plaintext. Resolve such rows deliberately before retrying.
"""

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.secure_files import secure_load_or_create_key
from src.constants import APP_KEY_FILE

_KEY_PATH = Path(APP_KEY_FILE)
_PREFIX = "enc:"
_VERSIONED_PREFIX = "enc:v2:"
_fernet: Fernet | None = None


class SecretStorageError(Exception):
    """Base error for secret encryption and decryption failures."""


class SecretDecryptionError(SecretStorageError):
    """A stored encrypted value could not be authenticated or decoded."""


class AmbiguousLegacySecretError(SecretDecryptionError):
    """A legacy-prefixed value cannot be safely distinguished from corruption."""


def _load_or_create_key() -> bytes:
    return secure_load_or_create_key(str(_KEY_PATH), Fernet.generate_key)


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def _encrypt_plaintext(plaintext: str) -> str:
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _VERSIONED_PREFIX + token


def _decrypt_prefixed(value: str) -> str:
    is_versioned = value.startswith(_VERSIONED_PREFIX)
    prefix = _VERSIONED_PREFIX if is_versioned else _PREFIX
    token = value[len(prefix):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        if is_versioned:
            raise SecretDecryptionError(
                "Stored v2 secret failed authentication or decoding"
            ) from exc
        raise AmbiguousLegacySecretError(
            "Legacy enc:-prefixed secret failed authentication; "
            "it may be corrupted ciphertext or legacy plaintext"
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt plaintext, preserving only authenticated application ciphertext.

    An authenticated existing value is passed through for compatibility with
    callers that may save a value they just loaded. An unauthenticated
    `enc:`-prefixed input is ambiguous and fails closed. Call
    :func:`encrypt_plaintext` when a caller knows that such a value is literal
    user-provided plaintext.
    """
    if not plaintext:
        return plaintext or ""
    if plaintext.startswith(_PREFIX):
        _decrypt_prefixed(plaintext)
        return plaintext
    return _encrypt_plaintext(plaintext)


def encrypt_plaintext(plaintext: str) -> str:
    """Encrypt known literal plaintext, including values beginning with `enc:`."""
    if not plaintext:
        return plaintext or ""
    return _encrypt_plaintext(plaintext)


def decrypt(value: str) -> str:
    """Decrypt application ciphertext or pass through unprefixed legacy text.

    Invalid, wrong-key, or corrupted ciphertext raises rather than returning
    ciphertext or an empty string that could be mistaken for a valid secret.
    """
    if not value:
        return value or ""
    if not value.startswith(_PREFIX):
        return value
    return _decrypt_prefixed(value)


def is_encrypted(value: str) -> bool:
    """Return whether a value is valid application ciphertext.

    A prefixed value that cannot be authenticated raises, because it may be
    corrupted legacy ciphertext rather than plaintext.
    """
    if not value or not value.startswith(_PREFIX):
        return False
    _decrypt_prefixed(value)
    return True


def migrate_encrypted_value(value: str) -> str:
    """Encrypt plaintext or upgrade valid legacy ciphertext to the v2 format.

    Ambiguous or corrupted prefixed values raise without changing the stored
    value. Re-running this migration leaves authenticated v2 values unchanged.
    """
    if not value:
        return value or ""
    if value.startswith(_VERSIONED_PREFIX):
        _decrypt_prefixed(value)
        return value
    if value.startswith(_PREFIX):
        return _encrypt_plaintext(_decrypt_prefixed(value))
    return _encrypt_plaintext(value)
