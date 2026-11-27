import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def test_persistent_upload_quota_is_per_owner(monkeypatch, tmp_path):
    from src.upload_handler import UploadHandler
    from src import upload_limits

    monkeypatch.setenv("ODYSSEUS_UPLOAD_STORAGE_LIMIT_PER_USER_MB", "1")

    handler = UploadHandler(str(tmp_path), str(tmp_path / "uploads"))
    monkeypatch.setattr(handler, "_load_upload_index", lambda: {
        "alice-file": {"owner": "alice", "size": 700 * 1024},
        "bob-file": {"owner": "bob", "size": 900 * 1024},
    })
    monkeypatch.setattr(handler, "get_upload_dir", lambda: str(tmp_path))
    monkeypatch.setattr(handler, "detect_content_type", lambda *_: "text/plain")

    upload = SimpleNamespace(
        filename="allowed.txt",
        file=io.BytesIO(b"x" * (400 * 1024)),
    )

    with pytest.raises(HTTPException) as exc:
        handler.save_upload(upload, "127.0.0.1", owner="alice")
    assert exc.value.status_code == 413
    assert "storage limit" in exc.value.detail


def test_zero_upload_quota_is_unlimited(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_UPLOAD_STORAGE_LIMIT_PER_USER_MB", "0")
    from src.upload_limits import get_persistent_upload_quota_bytes

    assert get_persistent_upload_quota_bytes() == 0
