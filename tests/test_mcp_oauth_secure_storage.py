import json
import os
from pathlib import Path

import pytest
from starlette.requests import Request
from core import secure_files

from routes import mcp_routes


@pytest.fixture
def oauth_dir(tmp_path, monkeypatch):
    base = tmp_path / "data" / "mcp_oauth"
    monkeypatch.setattr(mcp_routes, "MCP_OAUTH_DIR", str(base))
    return base


def test_oauth_json_write_persists_content_and_restricts_permissions(oauth_dir):
    target = oauth_dir / "gmail" / "credentials.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"old": true}', encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o644)
    mcp_routes._write_mcp_oauth_json(
        str(target), {"installed": {"client_secret": "secret"}}
    )

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "installed": {"client_secret": "secret"}
    }
    if os.name == "posix":
        assert oauth_dir.stat().st_mode & 0o777 == 0o700
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert target.stat().st_mode & 0o777 == 0o600


async def test_add_server_does_not_log_oauth_credentials(
    oauth_dir, monkeypatch, caplog
):
    class Database:
        def add(self, _server):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    class Manager:
        async def connect_server(self, **_kwargs):
            return True

        def get_server_status(self, _server_id):
            return {"status": "connected", "tool_count": 0}

    monkeypatch.setattr(mcp_routes, "require_admin", lambda _request: None)
    monkeypatch.setattr(mcp_routes, "SessionLocal", Database)
    api_router = mcp_routes.setup_mcp_routes(Manager())
    endpoint = next(
        route.endpoint
        for route in reversed(api_router.routes)
        if route.path == "/api/mcp/servers" and "POST" in route.methods
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/mcp/servers",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
    })
    oauth_file = json.dumps({
        "dir": "gmail",
        "filename": "credentials.json",
        "client_id": "private-client-id",
        "client_secret": "private-client-secret",
    })

    await endpoint(
        request=request,
        name="gmail",
        transport="stdio",
        command="gmail-mcp",
        args="[]",
        env="{}",
        url=None,
        oauth_file=oauth_file,
        oauth_config=None,
    )

    assert "private-client-id" not in caplog.text
    assert "private-client-secret" not in caplog.text


def test_oauth_json_write_restricts_existing_file_and_cleans_up_on_failure(
    oauth_dir,
):
    target = oauth_dir / "gmail" / "credentials.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"existing": true}', encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o644)

    with pytest.raises(TypeError):
        mcp_routes._write_mcp_oauth_json(
            str(target), {"not_json_serializable": object()}
        )

    assert json.loads(target.read_text(encoding="utf-8")) == {"existing": True}
    assert list(target.parent.glob("credentials.json.tmp.*")) == []
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_oauth_json_write_creates_private_file_and_read_preserves_data(oauth_dir):
    target = oauth_dir / "gmail" / "credentials.json"
    payload = {"installed": {"client_id": "id", "client_secret": "secret"}}

    mcp_routes._write_mcp_oauth_json(str(target), payload)

    assert mcp_routes._read_mcp_oauth_json(str(target)) == payload
    if os.name == "posix":
        assert oauth_dir.stat().st_mode & 0o777 == 0o700
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert target.stat().st_mode & 0o777 == 0o600


def test_missing_oauth_token_parent_is_reported_as_missing(oauth_dir):
    assert not mcp_routes._mcp_oauth_file_exists(
        str(oauth_dir / "gmail" / "credentials.json")
    )


def test_oauth_json_write_rejects_path_traversal(oauth_dir, tmp_path):
    outside = tmp_path / "outside.json"
    with pytest.raises(ValueError):
        mcp_routes._write_mcp_oauth_json(str(outside), {"secret": "value"})
    assert not outside.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_oauth_json_write_rejects_symlinked_directory(oauth_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    oauth_dir.mkdir(parents=True)
    (oauth_dir / "gmail").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        mcp_routes._write_mcp_oauth_json(
            str(oauth_dir / "gmail" / "credentials.json"), {"secret": "value"}
        )
    assert not (outside / "credentials.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_oauth_json_write_rejects_symlinked_managed_directory(
    oauth_dir, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    oauth_dir.parent.mkdir(parents=True)
    oauth_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        mcp_routes._write_mcp_oauth_json(
            str(oauth_dir / "credentials.json"), {"secret": "value"}
        )
    assert not (outside / "credentials.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_oauth_json_write_rejects_symlinked_credential_file(oauth_dir, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"untouched": true}', encoding="utf-8")
    target = oauth_dir / "gmail" / "credentials.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    with pytest.raises(OSError):
        mcp_routes._write_mcp_oauth_json(str(target), {"secret": "value"})
    assert json.loads(outside.read_text(encoding="utf-8")) == {"untouched": True}


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink boundary")
def test_oauth_json_read_rejects_symlinked_credential_file(oauth_dir, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"client_secret": "not-read"}', encoding="utf-8")
    target = oauth_dir / "gmail" / "credentials.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    with pytest.raises(OSError):
        mcp_routes._read_mcp_oauth_json(str(target))


def test_oauth_json_write_preserves_old_file_and_cleans_temp_on_replace_failure(
    oauth_dir, monkeypatch
):
    target = oauth_dir / "gmail" / "credentials.json"
    mcp_routes._write_mcp_oauth_json(str(target), {"old": True})

    def fail_replace(*args, **kwargs):
        raise OSError("replacement failed")

    monkeypatch.setattr(secure_files.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        mcp_routes._write_mcp_oauth_json(str(target), {"new": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert list(target.parent.glob(".credentials.json.tmp.*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor behavior")
def test_oauth_write_remains_anchored_when_root_path_is_replaced(
    oauth_dir, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    oauth_dir.mkdir(parents=True)
    original = secure_files._open_child_directory
    replaced = False

    def replace_parent(directory, component, base_path, *, create):
        nonlocal replaced
        if component == "gmail" and not replaced:
            moved = oauth_dir.with_name("mcp_oauth_original")
            oauth_dir.rename(moved)
            oauth_dir.symlink_to(outside, target_is_directory=True)
            replaced = True
        return original(directory, component, base_path, create=create)

    monkeypatch.setattr(secure_files, "_open_child_directory", replace_parent)
    target = oauth_dir / "gmail" / "credentials.json"
    mcp_routes._write_mcp_oauth_json(str(target), {"safe": True})

    assert not (outside / "gmail" / "credentials.json").exists()
    assert (oauth_dir.with_name("mcp_oauth_original") / "gmail" / "credentials.json").exists()
