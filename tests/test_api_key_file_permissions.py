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
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from unittest.mock import patch

from src.api_key_manager import (
    APIKeyManager,
    EncryptionKeyError,
    MalformedInstallationStateError,
)

_WINDOWS = sys.platform.startswith("win")


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_new_key_file_is_owner_only(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    mgr.initialize_key(first_time=True)
    assert _mode(mgr.key_file) == 0o600, f"expected 0o600, got {oct(_mode(mgr.key_file))}"


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_existing_world_readable_valid_key_is_relocked(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    key = Fernet.generate_key()
    with open(mgr.key_file, "wb") as f:
        f.write(key)
    os.chmod(mgr.key_file, 0o644)
    mgr.mark_installation_state("existing")
    mgr.initialize_key()
    assert _mode(mgr.key_file) == 0o600, f"expected re-lock to 0o600, got {oct(_mode(mgr.key_file))}"
    assert mgr.get_key() == key


def test_corrupted_existing_key_is_not_replaced(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    tmp_path.joinpath(".key").write_bytes(b"not-a-valid-fernet-key")

    with pytest.raises(Exception, match="invalid|corrupted"):
        mgr.mark_installation_state("existing")
        mgr.initialize_key()

    assert tmp_path.joinpath(".key").read_bytes() == b"not-a-valid-fernet-key"


def test_repeated_initialization_preserves_exact_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    first = mgr.initialize_key(first_time=True)
    assert mgr.initialize_key() == first
    assert mgr.get_key() == first


def test_missing_key_fails_for_existing_api_key_installation(tmp_path):
    tmp_path.joinpath("api_keys.json").write_text(
        '{"openai":"encrypted-ciphertext"}',
        encoding="utf-8",
    )
    mgr = APIKeyManager(str(tmp_path))

    with pytest.raises(EncryptionKeyError, match="restore the original key"):
        mgr.initialize_key()

    assert not tmp_path.joinpath(".key").exists()


def test_normal_api_key_encryption_cannot_create_missing_key(tmp_path):
    tmp_path.joinpath("api_keys.json").write_text("{}", encoding="utf-8")
    mgr = APIKeyManager(str(tmp_path))

    with pytest.raises(EncryptionKeyError, match="missing"):
        mgr.encrypt_api_key("secret")

    assert not tmp_path.joinpath(".key").exists()


def test_failed_initialization_does_not_destroy_valid_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    key = mgr.initialize_key(first_time=True)
    original = tmp_path.joinpath(".key").read_bytes()

    import src.api_key_manager as api_key_module

    with patch.object(api_key_module, "secure_write_json_at", side_effect=OSError("disk full")):
        with pytest.raises(EncryptionKeyError, match="marked"):
            mgr.initialize_key()

    assert tmp_path.joinpath(".key").read_bytes() == original
    assert mgr.get_key() == key


def test_existing_installation_with_no_credentials_still_fails_on_missing_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("existing")
    tmp_path.joinpath("settings.json").write_text("{}", encoding="utf-8")

    with pytest.raises(EncryptionKeyError, match="missing"):
        mgr.initialize_key()

    assert not tmp_path.joinpath(".key").exists()


def test_ambiguous_installation_without_state_marker_fails_closed(tmp_path):
    mgr = APIKeyManager(str(tmp_path))

    with pytest.raises(EncryptionKeyError, match="missing"):
        mgr.initialize_key()

    assert not tmp_path.joinpath(".key").exists()


def test_truncated_or_malformed_key_never_triggers_creation(tmp_path):
    for value in (b"x", b"not-base64"):
        mgr = APIKeyManager(str(tmp_path))
        tmp_path.joinpath(".key").write_bytes(value)
        mgr.mark_installation_state("first_run_pending")

        with pytest.raises(EncryptionKeyError, match="invalid|corrupted"):
            mgr.initialize_key(first_time=True)

        assert tmp_path.joinpath(".key").read_bytes() == value
        tmp_path.joinpath(".key").unlink()


def test_corrupt_marker_with_valid_key_is_healed(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    mgr.initialize_key(first_time=True)
    tmp_path.joinpath(".odysseus_installation").write_text("{bad", encoding="utf-8")

    with pytest.raises(MalformedInstallationStateError):
        mgr.get_installation_state()

    key = mgr.get_key()
    mgr.mark_installation_state("installed")
    assert mgr.get_installation_state() == "installed"
    assert mgr.get_key() == key


def test_unreadable_marker_fails_closed(tmp_path, monkeypatch):
    mgr = APIKeyManager(str(tmp_path))
    tmp_path.joinpath(".odysseus_installation").write_text(
        '{"state":"first_run_pending"}',
        encoding="utf-8",
    )

    def deny_marker(_base_dir, relative_path):
        if relative_path == ".odysseus_installation":
            raise PermissionError("permission denied")
        return b""

    monkeypatch.setattr(
        "src.api_key_manager.secure_read_bytes_at",
        deny_marker,
    )

    with pytest.raises(EncryptionKeyError, match="could not be accessed"):
        mgr.get_installation_state()

    assert not tmp_path.joinpath(".key").exists()


def test_marker_missing_with_valid_key_preserves_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    first = mgr.initialize_key(first_time=True)
    tmp_path.joinpath(".odysseus_installation").unlink()

    second = mgr.initialize_key()

    assert second == first
    assert mgr.get_installation_state() == "installed"


def test_marker_present_key_missing_never_initializes(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("existing")

    with pytest.raises(EncryptionKeyError, match="missing"):
        mgr.initialize_key(first_time=True)

    assert not tmp_path.joinpath(".key").exists()


def test_crash_after_key_creation_before_marker_creation_is_safe(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    key = Fernet.generate_key()
    tmp_path.joinpath(".key").write_bytes(key)
    os.chmod(mgr.key_file, 0o600)

    assert mgr.initialize_key(first_time=True) == key
    assert mgr.get_installation_state() == "installed"


def test_encrypt_decrypt_roundtrip_still_works(tmp_path):
    # The permission hardening must not change functional behaviour.
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    mgr.initialize_key(first_time=True)
    enc = mgr.encrypt_api_key("sk-secret")
    assert enc and enc != "sk-secret"
    assert mgr.decrypt_api_key(enc) == "sk-secret"


def test_concurrent_first_key_creation_installs_one_complete_key(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    barrier = Barrier(8)

    def create_key():
        barrier.wait()
        return mgr.initialize_key(first_time=True)

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

    with pytest.raises((OSError, EncryptionKeyError)):
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


def _read_installation_marker(data_dir):
    marker = data_dir / ".odysseus_installation"
    if not marker.exists():
        return None
    return json.loads(marker.read_text(encoding="utf-8"))["state"]


def test_database_bootstrap_marks_genuinely_absent_directory_as_first_run(tmp_path):
    data_dir = tmp_path / "absent-data"
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "ODYSSEUS_INITIALIZE_ENCRYPTION_KEY": "0",
    })
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", "import core.database"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert _read_installation_marker(data_dir) == "first_run_pending"


def test_database_bootstrap_treats_existing_empty_directory_as_ambiguous(tmp_path):
    data_dir = tmp_path / "existing-empty"
    data_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "ODYSSEUS_INITIALIZE_ENCRYPTION_KEY": "0",
    })
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", "import core.database"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert _read_installation_marker(data_dir) == "existing"


def test_database_bootstrap_explicit_confirmation_allows_first_run(tmp_path):
    data_dir = tmp_path / "existing-empty-confirmed"
    data_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "ODYSSEUS_INITIALIZE_ENCRYPTION_KEY": "1",
    })
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", "import core.database"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert _read_installation_marker(data_dir) == "first_run_pending"


def test_database_crash_state_reconciles_first_run_marker_after_database_exists(tmp_path):
    data_dir = tmp_path / "crash-state"
    data_dir.mkdir()
    (data_dir / ".odysseus_installation").write_text(
        '{"state":"first_run_pending"}',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "ODYSSEUS_INITIALIZE_ENCRYPTION_KEY": "1",
    })
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    (tmp_path / "app.db").write_bytes(b"")
    result = subprocess.run(
        [sys.executable, "-c", "import core.database"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert _read_installation_marker(data_dir) == "existing"
