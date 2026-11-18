"""Regression: the API-key encryption key file (data/.key) must be owner-only
(0o600).

``APIKeyManager.get_or_create_key`` writes the Fernet key that decrypts *every*
stored provider credential. Older versions created it with the process umask
(commonly 0o644 — group/world-readable). It must be locked to the owner, both
when freshly created and when an older, too-permissive key is read back.

POSIX-only: ``core.platform_compat.safe_chmod`` is a documented no-op on Windows
(files under the user profile are ACL-restricted), so the mode assertions are
skipped there.
"""
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from src.api_key_manager import APIKeyManager

_WINDOWS = sys.platform.startswith("win")


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_new_key_file_is_owner_only(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.get_or_create_key()
    assert _mode(mgr.key_file) == 0o600, f"expected 0o600, got {oct(_mode(mgr.key_file))}"


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_existing_world_readable_key_is_relocked(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    # Simulate a key written by an older version with a permissive umask.
    with open(mgr.key_file, "wb") as f:
        f.write(b"x" * 44)
    os.chmod(mgr.key_file, 0o644)
    mgr.get_or_create_key()  # existing-file branch should re-lock it
    assert _mode(mgr.key_file) == 0o600, f"expected re-lock to 0o600, got {oct(_mode(mgr.key_file))}"


def test_encrypt_decrypt_roundtrip_still_works(tmp_path):
    # The permission hardening must not change functional behaviour.
    mgr = APIKeyManager(str(tmp_path))
    enc = mgr.encrypt_api_key("sk-secret")
    assert enc and enc != "sk-secret"
    assert mgr.decrypt_api_key(enc) == "sk-secret"


def test_concurrent_first_key_creation_installs_one_complete_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    barrier = Barrier(8)

    def create_key():
        barrier.wait()
        return mgr.get_or_create_key()

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: create_key(), range(8)))

    assert len(set(keys)) == 1
    assert Fernet(keys[0])


def test_api_key_creation_rejects_symlinked_key_file(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-outside-key"
    outside.write_bytes(Fernet.generate_key())
    try:
        os.symlink(outside, mgr.key_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(OSError):
        mgr.get_or_create_key()


def test_api_key_storage_rejects_symlinked_json_file(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}-outside-api-keys.json"
    outside.write_text('{"provider": "preserve"}', encoding="utf-8")
    try:
        os.symlink(outside, mgr.api_keys_file)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(OSError):
        mgr.save("provider", "new-secret")
    assert outside.read_text(encoding="utf-8") == '{"provider": "preserve"}'
