"""Windows ACL regression tests for persisted credential files."""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from core import secure_files
from core.atomic_io import atomic_write_json
from core.auth import AuthManager
from src import secret_storage
from src.api_key_manager import APIKeyManager
from host_bridge import config as host_bridge_config


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL APIs required")


def _grant_everyone_full_control(path, *, inherited=False):
    permission = "*S-1-1-0:(OI)(CI)F" if inherited else "*S-1-1-0:F"
    subprocess.run(
        ["icacls", str(path), "/grant", permission],
        check=True,
        capture_output=True,
        text=True,
    )


def test_default_and_custom_data_directories_create_private_keys(
    tmp_path, monkeypatch
):
    user_sid = secure_files._windows_current_user_sid()
    default_key_path = tmp_path / "default-data" / ".app_key"
    custom_data_dir = tmp_path / "custom-data"
    monkeypatch.setattr(secret_storage, "_KEY_PATH", default_key_path)
    keys = (
        (default_key_path, secret_storage._load_or_create_key()),
        (
            custom_data_dir / ".key",
            APIKeyManager(str(custom_data_dir)).get_or_create_key(),
        ),
    )

    for path, key in keys:
        assert secure_files.secure_load_or_create_key(
            str(path), Fernet.generate_key
        ) == key
        directory = secure_files._open_directory(str(path.parent), create=False)
        try:
            facts = secure_files._windows_acl_facts(directory.handles[-1])
            secure_files._validate_windows_acl_facts(
                *facts, is_directory=True
            )
        finally:
            directory.close()
        assert Fernet(key)
        assert user_sid == secure_files._windows_current_user_sid()


def test_permissive_parent_inheritance_does_not_reach_created_credentials(tmp_path):
    parent = tmp_path / "permissive-parent"
    parent.mkdir()
    _grant_everyone_full_control(parent, inherited=True)
    key_path = parent / "private-data" / ".app_key"

    key = secure_files.secure_load_or_create_key(
        str(key_path), Fernet.generate_key
    )

    assert Fernet(key)
    directory = secure_files._open_directory(
        str(key_path.parent), create=False
    )
    try:
        secure_files._validate_windows_security(
            directory.handles[-1], is_directory=True
        )
    finally:
        directory.close()


def test_existing_permissive_key_is_rejected_without_regeneration(tmp_path):
    key_path = tmp_path / "private-data" / ".app_key"
    original = secure_files.secure_load_or_create_key(
        str(key_path), Fernet.generate_key
    )
    _grant_everyone_full_control(key_path)

    with pytest.raises(PermissionError, match="ACL"):
        secure_files.secure_load_or_create_key(
            str(key_path), Fernet.generate_key
        )

    assert key_path.read_bytes() == original


def test_existing_permissive_data_directory_is_rejected(tmp_path):
    directory = tmp_path / "permissive-data"
    directory.mkdir()
    _grant_everyone_full_control(directory, inherited=True)

    with pytest.raises(PermissionError, match="ACL"):
        secure_files._open_directory(str(directory), create=False)


def test_bridge_token_with_unsafe_acl_is_rejected_without_regeneration(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "bridge-workspace"
    monkeypatch.setattr(
        host_bridge_config, "HOST_WORKSPACE_DIR", str(workspace)
    )
    token = host_bridge_config.get_or_create_bridge_token()
    token_file = workspace / host_bridge_config.TOKEN_FILENAME
    _grant_everyone_full_control(token_file)

    with pytest.raises(PermissionError, match="ACL"):
        host_bridge_config.get_or_create_bridge_token()

    assert token_file.read_text(encoding="ascii") == token


def test_bridge_token_with_unsafe_workspace_acl_is_rejected(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "bridge-workspace"
    monkeypatch.setattr(
        host_bridge_config, "HOST_WORKSPACE_DIR", str(workspace)
    )
    token = host_bridge_config.get_or_create_bridge_token()
    _grant_everyone_full_control(workspace, inherited=True)

    with pytest.raises(PermissionError, match="ACL"):
        host_bridge_config.get_or_create_bridge_token()

    assert (workspace / host_bridge_config.TOKEN_FILENAME).read_text(
        encoding="ascii"
    ) == token


def test_unexpected_owner_is_rejected():
    current_sid = secure_files._windows_current_user_sid()
    with pytest.raises(PermissionError, match="owner"):
        secure_files._validate_windows_acl_facts(
            "S-1-5-18",
            True,
            [
                (3, 0x001F01FF, current_sid),
                (3, 0x001F01FF, "S-1-5-18"),
            ],
            is_directory=True,
        )


def test_reparse_point_in_credential_path_is_rejected(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "linked-data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Windows symlink creation unavailable: {exc}")

    with pytest.raises(OSError):
        secure_files.secure_load_or_create_key(
            str(link / ".app_key"), Fernet.generate_key
        )


def test_reparse_point_at_oauth_credential_file_is_rejected(tmp_path):
    base = tmp_path / "private-data"
    outside = tmp_path / "outside.json"
    outside.write_text('{"client_secret": "outside"}', encoding="utf-8")
    secure_files.secure_write_json_at(
        str(base), "mcp_oauth/credentials.json", {"client_secret": "original"}
    )
    target = base / "mcp_oauth" / "linked.json"
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Windows symlink creation unavailable: {exc}")

    with pytest.raises(OSError):
        secure_files.secure_write_json_at(
            str(base), "mcp_oauth/linked.json", {"client_secret": "replacement"}
        )
    assert outside.read_text(encoding="utf-8") == '{"client_secret": "outside"}'


def test_existing_valid_key_is_preserved(tmp_path):
    key_path = tmp_path / "private-data" / ".key"
    key = secure_files.secure_load_or_create_key(
        str(key_path), Fernet.generate_key
    )

    assert secure_files.secure_load_or_create_key(
        str(key_path), lambda: pytest.fail("existing key must not be regenerated")
    ) == key


def test_temporary_oauth_file_is_private_when_atomic_replace_starts(
    tmp_path, monkeypatch
):
    base = tmp_path / "private-data"
    target = base / "mcp_oauth" / "credentials.json"
    original_replace = secure_files.os.replace
    checked = []

    def check_then_replace(source, destination, **kwargs):
        fd = secure_files._open_windows_file(source, create=False)
        os.close(fd)
        checked.append(True)
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(secure_files.os, "replace", check_then_replace)
    secure_files.secure_write_json_at(
        str(base), "mcp_oauth/credentials.json", {"client_secret": "secret"}
    )

    assert checked == [True]
    assert target.exists()


def test_sensitive_atomic_json_temp_file_has_private_acl_before_replace(
    tmp_path, monkeypatch
):
    target = tmp_path / "private-data" / "auth.json"
    original_replace = secure_files.os.replace
    checked = []

    def check_then_replace(source, destination, **kwargs):
        fd = secure_files._open_windows_file(source, create=False)
        os.close(fd)
        checked.append(True)
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr(secure_files.os, "replace", check_then_replace)
    atomic_write_json(str(target), {"users": {}}, sensitive=True)

    assert checked == [True]
    assert target.exists()


def test_sensitive_atomic_json_rejects_unsafe_existing_file_without_replacing(
    tmp_path,
):
    target = tmp_path / "private-data" / "auth.json"
    atomic_write_json(str(target), {"users": {"alice": "hash"}}, sensitive=True)
    original = target.read_bytes()
    _grant_everyone_full_control(target)

    with pytest.raises(PermissionError, match="ACL"):
        atomic_write_json(str(target), {"users": {}}, sensitive=True)

    assert target.read_bytes() == original


def test_auth_manager_rejects_unsafe_existing_auth_file(tmp_path):
    target = tmp_path / "private-data" / "auth.json"
    atomic_write_json(str(target), {"users": {}}, sensitive=True)
    original = target.read_bytes()
    _grant_everyone_full_control(target)

    with pytest.raises(PermissionError, match="ACL"):
        AuthManager(str(target))

    assert target.read_bytes() == original


@pytest.mark.parametrize("failure_point", ["validation", "crt-conversion"])
def test_failed_windows_temp_open_removes_only_its_created_file(
    tmp_path, monkeypatch, failure_point
):
    path = tmp_path / "private-temp"
    if failure_point == "validation":
        monkeypatch.setattr(
            secure_files,
            "_validate_windows_file_handle",
            lambda _handle: (_ for _ in ()).throw(OSError("validation failed")),
        )
    else:
        import msvcrt

        monkeypatch.setattr(
            secure_files, "_validate_windows_file_handle", lambda _handle: None
        )
        monkeypatch.setattr(
            msvcrt,
            "open_osfhandle",
            lambda *_args: (_ for _ in ()).throw(OSError("conversion failed")),
        )

    with pytest.raises(OSError):
        secure_files._open_windows_file(str(path), create=True)

    assert not path.exists()


def test_failed_windows_temp_open_does_not_remove_existing_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "preexisting"
    path.write_bytes(b"untouched")
    with pytest.raises(OSError):
        secure_files._open_windows_file(str(path), create=True)
    assert path.read_bytes() == b"untouched"


def test_concurrent_first_key_creation_installs_one_key(tmp_path):
    key_path = tmp_path / "private-data" / ".app_key"
    barrier = Barrier(8)

    def create():
        barrier.wait()
        return secure_files.secure_load_or_create_key(
            str(key_path), Fernet.generate_key
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _index: create(), range(8)))

    assert len(set(keys)) == 1
    assert Fernet(keys[0])
