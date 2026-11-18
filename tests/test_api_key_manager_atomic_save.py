"""Test that APIKeyManager.save() uses atomic write to prevent data loss."""
import os
import json
import pytest
from unittest.mock import patch
from src.api_key_manager import APIKeyManager
from core import secure_files


def test_save_creates_private_atomic_json_file(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-test")

    # The final file should exist with the correct content
    assert os.path.exists(mgr.api_keys_file)
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    assert "openai" in keys

    assert list(tmp_path.glob(".api_keys.json.tmp.*")) == []
    if os.name == "posix":
        assert os.stat(mgr.api_keys_file).st_mode & 0o777 == 0o600


def test_save_preserves_existing_keys_atomically(tmp_path):
    """Verify atomic save doesn't corrupt other providers' keys."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-openai")
    mgr.save("anthropic", "sk-anthropic")

    loaded = mgr.load()
    assert loaded["openai"] == "sk-openai"
    assert loaded["anthropic"] == "sk-anthropic"


def test_save_preserves_original_on_write_failure(tmp_path):
    """If the temp file write fails, the original keys file must survive intact."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-original")

    # Fail after the private sibling temporary file has been opened.
    with patch(
        "core.secure_files.json.dump",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            mgr.save("anthropic", "sk-new")

    # Original file must still be intact with the original key
    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}
    assert "anthropic" not in loaded


def test_save_cleans_up_tmp_on_failure(tmp_path):
    """Temp file should be removed if the write fails."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("openai", "sk-original")

    def replace_fails(source, destination, **kwargs):
        raise OSError("simulated replace failure")

    with patch.object(secure_files.os, "replace", side_effect=replace_fails):
        with pytest.raises(OSError):
            mgr.save("anthropic", "sk-new")

    # Temp file should be cleaned up
    assert list(tmp_path.glob(".api_keys.json.tmp.*")) == []

    # Original should be intact
    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}
