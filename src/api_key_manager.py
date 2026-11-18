import os
import json
import logging
from typing import Dict
from cryptography.fernet import Fernet, InvalidToken

from core.secure_files import (
    secure_load_or_create_key,
    secure_read_bytes_at,
    secure_read_json_at,
    secure_write_json_at,
)

logger = logging.getLogger(__name__)

_WEBHOOK_SECRET_PREFIX = "webhook-secret:v1:"
_ENCRYPTION_INITIALIZED_MARKER = ".encryption_initialized"
_INSTALLATION_STATE_MARKER = ".odysseus_installation"


class EncryptionKeyError(RuntimeError):
    """The application encryption key is missing or unusable."""


class MalformedInstallationStateError(EncryptionKeyError):
    """The installation marker exists but cannot be authenticated as valid state."""


class WebhookSecretStorageError(EncryptionKeyError):
    """A webhook secret could not be securely encrypted or authenticated."""


class APIKeyManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.api_keys_file = os.path.join(data_dir, "api_keys.json")
        self.key_file = os.path.join(data_dir, ".key")

    def _marker_exists(self) -> bool:
        try:
            secure_read_bytes_at(self.data_dir, _ENCRYPTION_INITIALIZED_MARKER)
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            raise EncryptionKeyError(
                "Encryption initialization marker could not be accessed"
            ) from exc

    def get_installation_state(self) -> str:
        """Read the explicit installation/bootstrap marker, never infer it."""
        try:
            raw = secure_read_bytes_at(self.data_dir, _INSTALLATION_STATE_MARKER)
        except FileNotFoundError:
            return "unknown"
        except (OSError, ValueError) as exc:
            raise EncryptionKeyError(
                "Installation state could not be accessed"
            ) from exc
        try:
            state = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise MalformedInstallationStateError(
                "Installation state is malformed; operator recovery is required"
            ) from exc
        if not isinstance(state, dict):
            raise MalformedInstallationStateError(
                "Installation state is malformed; operator recovery is required"
            )
        value = state.get("state")
        if value not in ("first_run_pending", "installed", "existing"):
            raise MalformedInstallationStateError(
                "Installation state is unrecognized; operator recovery is required"
            )
        return value

    def mark_installation_state(self, state: str) -> None:
        if state not in ("first_run_pending", "installed", "existing"):
            raise ValueError("invalid installation state")
        try:
            secure_write_json_at(
                self.data_dir,
                _INSTALLATION_STATE_MARKER,
                {"state": state},
            )
        except Exception as exc:
            raise EncryptionKeyError(
                "Installation state could not be recorded"
            ) from exc

    def has_existing_installation_state(self) -> bool:
        """Return evidence that this is not a pristine first installation.

        The result is deliberately based on multiple persisted-state indicators:
        initialization marker, provider-key file, and webhook database rows.
        Presence of an automatically-created SQLite database alone is not used.
        """
        if self._marker_exists():
            return True
        try:
            if os.path.isfile(self.api_keys_file):
                return True
        except OSError:
            pass
        try:
            from core.database import SessionLocal, Webhook

            db = SessionLocal()
            try:
                if db.query(Webhook).filter(Webhook.secret != None).first() is not None:  # noqa: E711
                    return True
            finally:
                db.close()
        except Exception:
            pass
        return False

    def get_key(self) -> bytes:
        """Read and validate the existing encryption key without creating it."""
        try:
            key = secure_read_bytes_at(self.data_dir, ".key")
        except FileNotFoundError as exc:
            raise EncryptionKeyError(
                "Encryption key is missing; restore the original key from a secure backup"
            ) from exc
        except (OSError, ValueError) as exc:
            raise EncryptionKeyError(
                "Encryption key could not be accessed"
            ) from exc
        try:
            Fernet(key)
        except (TypeError, ValueError) as exc:
            raise EncryptionKeyError(
                "Encryption key is invalid or corrupted; restore the original key from a secure backup"
            ) from exc
        return key

    def initialize_key(self, *, first_time: bool = False) -> bytes:
        """Initialize a key only after explicit first-run bootstrap provenance.

        Normal startup passes first_time=False unless core database initialization
        atomically recorded an unambiguous first-run-pending state.
        """
        try:
            key = self.get_key()
        except EncryptionKeyError as key_error:
            installation_state = self.get_installation_state()
            if installation_state != "first_run_pending" or not first_time:
                raise
            try:
                secure_read_bytes_at(self.data_dir, ".key")
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                raise EncryptionKeyError(
                    "Encryption key could not be accessed"
                ) from exc
            else:
                # A key appeared concurrently. Read and preserve whichever
                # complete key won the atomic installation.
                key = self.get_key()
            key = secure_load_or_create_key(self.key_file, Fernet.generate_key)
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise EncryptionKeyError(
                    "Encryption key initialization produced an invalid key"
                ) from exc
        try:
            secure_write_json_at(
                self.data_dir,
                _ENCRYPTION_INITIALIZED_MARKER,
                {"initialized": True},
            )
        except Exception as exc:
            raise EncryptionKeyError(
                "Encryption key could not be marked as initialized"
            ) from exc
        self.mark_installation_state("installed")
        return key

    def get_or_create_key(self) -> bytes:
        """Backward-compatible read-only name.

        Normal callers must use initialize_key() only during first-time bootstrap.
        This method never creates or replaces a key.
        """
        return self.get_key()
    
    def encrypt_api_key(self, api_key: str) -> str:
        """Encrypt an API key"""
        if not api_key:
            return ""
        f = Fernet(self.get_key())
        return f.encrypt(api_key.encode()).decode()
    
    def decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key"""
        if not encrypted_key:
            return ""
        f = Fernet(self.get_key())
        try:
            return f.decrypt(encrypted_key.encode()).decode()
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise EncryptionKeyError(
                "Stored API key could not be authenticated"
            ) from exc

    def encrypt_webhook_secret(self, secret: str) -> str:
        """Encrypt a webhook secret in a versioned envelope."""
        if not secret:
            return ""
        try:
            return _WEBHOOK_SECRET_PREFIX + self.encrypt_api_key(secret)
        except Exception as exc:
            raise WebhookSecretStorageError(
                "Webhook secret encryption is unavailable"
            ) from exc

    def decrypt_webhook_secret(self, stored_secret: str) -> str:
        """Decrypt new and authenticated legacy webhook-secret records.

        Older webhook records contain unprefixed Fernet tokens. They remain
        readable when authenticated by the existing API-key key; plaintext or
        invalid records fail closed. Unlike API-key loading, this path never
        creates a replacement key while attempting decryption.
        """
        if not stored_secret:
            return ""
        token = (
            stored_secret[len(_WEBHOOK_SECRET_PREFIX):]
            if stored_secret.startswith(_WEBHOOK_SECRET_PREFIX)
            else stored_secret
        )
        try:
            key = self.get_key()
        except EncryptionKeyError as exc:
            raise WebhookSecretStorageError(str(exc)) from exc
        try:
            return Fernet(key).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise WebhookSecretStorageError(
                "Stored webhook secret could not be authenticated; "
                "create a replacement webhook and remove the old one"
            ) from exc
    
    def _load_raw(self) -> Dict[str, str]:
        """Load the raw, still-encrypted keys dict from disk.

        Tolerates a missing/corrupt/wrong-shaped file by returning {} — the
        same robustness load() relies on at startup.
        """
        try:
            encrypted_keys = secure_read_json_at(self.data_dir, "api_keys.json")
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeError) as e:
            # A corrupt/truncated api_keys.json must not crash load() (called on
            # startup via app_initializer) — treat it as no stored keys.
            logger.warning("Failed to read API keys file: %s", e)
            return {}
        if not isinstance(encrypted_keys, dict):
            # Legacy/wrong shape (e.g. a list) — .items() would raise. Ignore it.
            logger.warning("API keys file has unexpected shape (%s); ignoring", type(encrypted_keys).__name__)
            return {}

        return {
            str(provider): key
            for provider, key in encrypted_keys.items()
            if isinstance(key, str)
        }

    def save(self, provider: str, api_key: str):
        """Save encrypted API key to file.

        Operates on the raw (still-encrypted) on-disk dict so other providers'
        keys stay encrypted. Loading via load() first would decrypt them and
        write them back as plaintext, which then fails to decrypt on the next
        load() and silently drops those providers.

        Uses atomic write (temp file + os.replace) so a crash, disk-full, or
        mid-write error never truncates the existing keys file.
        """
        keys = self._load_raw()
        keys[provider] = self.encrypt_api_key(api_key)
        secure_write_json_at(self.data_dir, "api_keys.json", keys)

    def load(self) -> Dict[str, str]:
        """Load and decrypt API keys"""
        encrypted_keys = self._load_raw()
        if encrypted_keys:
            # Establish key health before attempting to decrypt any provider.
            # A missing or replaced key must fail startup instead of returning a
            # partial configuration or silently appearing to delete providers.
            self.get_key()
        decrypted = {}
        for provider, key in encrypted_keys.items():
            try:
                decrypted[provider] = self.decrypt_api_key(key)
            except EncryptionKeyError as e:
                logger.warning("Failed to authenticate stored API key for %s", provider)
                raise EncryptionKeyError(
                    "Existing API-key ciphertext could not be authenticated; "
                    "restore the original encryption key from a secure backup"
                ) from e
            except (InvalidToken, ValueError) as e:
                logger.warning("Failed to decrypt API key for %s", provider)
                raise EncryptionKeyError(
                    "Existing API-key ciphertext could not be authenticated"
                ) from e
        return decrypted
