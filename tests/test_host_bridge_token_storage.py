import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from host_bridge import config
from core.secure_files import secure_load_or_create_key


@pytest.fixture
def token_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "bridge-workspace"
    monkeypatch.setattr(config, "HOST_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("JARVIS_HOST_BRIDGE_TOKEN", raising=False)
    return workspace


def test_existing_bridge_token_is_preserved(token_workspace):
    token_file = token_workspace / config.TOKEN_FILENAME
    expected = b"existing-bridge-token"
    secure_load_or_create_key(str(token_file), lambda: expected)

    assert config.get_or_create_bridge_token() == expected.decode("ascii")
    assert token_file.read_bytes() == expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit behavior")
def test_existing_permissive_bridge_token_is_restricted_before_read(
    token_workspace,
):
    token_file = token_workspace / config.TOKEN_FILENAME
    token_file.parent.mkdir(parents=True)
    token_file.write_text("existing-token", encoding="ascii")
    if os.name == "posix":
        token_file.chmod(0o644)

    assert config.get_or_create_bridge_token() == "existing-token"
    if os.name == "posix":
        assert token_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_bridge_token_symlink_is_rejected_without_replacement(
    token_workspace, tmp_path
):
    outside = tmp_path / "outside-token"
    outside.write_text("outside-token", encoding="ascii")
    token_workspace.mkdir()
    link = token_workspace / config.TOKEN_FILENAME
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")

    with pytest.raises(OSError):
        config.get_or_create_bridge_token()
    assert outside.read_text(encoding="ascii") == "outside-token"


def test_concurrent_bridge_token_creation_installs_one_complete_token(
    token_workspace,
):
    barrier = Barrier(8)

    def create_token():
        barrier.wait()
        return config.get_or_create_bridge_token()

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _index: create_token(), range(8)))

    assert len(set(tokens)) == 1
    assert len(tokens[0]) == 64
    assert all(char in "0123456789abcdef" for char in tokens[0])
    assert (token_workspace / config.TOKEN_FILENAME).read_text(
        encoding="ascii"
    ) == tokens[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL APIs required")
def test_bridge_token_has_protected_windows_user_and_system_acl(token_workspace):
    from core import secure_files

    token = config.get_or_create_bridge_token()
    token_file = token_workspace / config.TOKEN_FILENAME
    fd = secure_files._open_windows_file(str(token_file), create=False)
    os.close(fd)
    directory = secure_files._open_directory(str(token_workspace), create=False)
    try:
        secure_files._validate_windows_security(
            directory.handles[-1], is_directory=True
        )
    finally:
        directory.close()
    assert len(token) == 64
