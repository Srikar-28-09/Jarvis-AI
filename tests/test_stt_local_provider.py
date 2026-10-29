import asyncio
import io
import shutil
import sys
import threading
import time
import types
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from routes.stt_routes import setup_stt_routes
from services.stt import stt_service as stt_module
from services.stt.stt_service import STTInputError, STTService


def _wav_fixture():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    return buffer.getvalue()


def _ready_service(monkeypatch, tmp_path):
    model_dir = tmp_path / "faster-whisper-tiny"
    model_dir.mkdir()
    for name in stt_module.MODEL_FILES:
        (model_dir / name).write_bytes(b"fixture")
    monkeypatch.setattr(stt_module, "LOCAL_MODEL_ROOT", tmp_path)
    monkeypatch.setattr(stt_module.importlib.util, "find_spec", lambda _name: object())
    service = STTService()
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True,
        "stt_provider": "local",
        "stt_model": "tiny",
        "stt_language": "",
    })
    return service


def _install_fake_model(monkeypatch, model):
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = lambda *_args, **_kwargs: model
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


@pytest.mark.parametrize(
    ("audio", "suffix"),
    [(_wav_fixture(), ".wav"), (b"\x1aE\xdf\xa3webm-fixture", ".webm")],
)
def test_local_provider_passes_wav_and_webm_files_to_model(monkeypatch, tmp_path, audio, suffix):
    observed = {}

    class FakeModel:
        def transcribe(self, path, **kwargs):
            observed["suffix"] = __import__("pathlib").Path(path).suffix
            observed["language"] = kwargs.get("language")
            observed["bytes"] = __import__("pathlib").Path(path).read_bytes()
            return iter([SimpleNamespace(text=" hello "), SimpleNamespace(text="Odysseus")]), SimpleNamespace(language="en")

    service = _ready_service(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": True,
        "stt_provider": "local",
        "stt_model": "tiny",
        "stt_language": "en",
    })
    _install_fake_model(monkeypatch, FakeModel())

    assert service.available is True
    assert service.transcribe(audio) == "hello Odysseus"
    assert observed == {"suffix": suffix, "language": "en", "bytes": audio}


def test_disabled_provider_does_not_load_model(monkeypatch):
    service = STTService()
    monkeypatch.setattr(service, "_load_settings", lambda: {
        "stt_enabled": False,
        "stt_provider": "local",
        "stt_model": "tiny",
        "stt_language": "",
    })
    monkeypatch.setattr(service, "_get_whisper", lambda: pytest.fail("model must stay unloaded"))

    assert service.available is False
    assert service.transcribe(b"anything") is None
    assert service.get_stats()["enabled"] is False


def test_missing_model_weights_report_unavailable_without_download(monkeypatch, tmp_path):
    service = _ready_service(monkeypatch, tmp_path)
    shutil.rmtree(tmp_path / "faster-whisper-tiny")

    assert service.available is False
    assert "not installed locally" in service.unavailable_reason
    assert "never downloaded automatically" in service.unavailable_reason
    assert service.get_stats()["model_loaded"] is False


def test_missing_faster_whisper_dependency_reports_unavailable(monkeypatch, tmp_path):
    service = _ready_service(monkeypatch, tmp_path)
    monkeypatch.setattr(stt_module.importlib.util, "find_spec", lambda _name: None)

    assert service.available is False
    assert "package is missing" in service.unavailable_reason


def test_provider_loads_only_one_model_for_concurrent_callers(monkeypatch, tmp_path):
    service = _ready_service(monkeypatch, tmp_path)
    count = {"loads": 0}

    class FakeModel:
        def transcribe(self, path, **_kwargs):
            time.sleep(0.01)
            return iter([SimpleNamespace(text="ok")]), SimpleNamespace(language="en")

    def load(*_args, **_kwargs):
        count["loads"] += 1
        time.sleep(0.02)
        return FakeModel()

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = load
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.transcribe(_wav_fixture()))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["ok"] * 4
    assert count["loads"] == 1


def test_route_reports_readiness_and_rejects_invalid_audio():
    class UnavailableService:
        available = False
        unavailable_reason = "Local Whisper model 'tiny' is not installed locally."

        def get_stats(self):
            return {"provider": "local"}

    routes = setup_stt_routes(UnavailableService())
    route = next(route for route in routes.routes if route.path == "/api/stt/transcribe")
    with pytest.raises(HTTPException) as unavailable:
        asyncio.run(route.endpoint(file=UploadFile(filename="audio.wav", file=io.BytesIO(b""))))
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["message"].startswith("Local Whisper model")

    class AvailableInvalidService(UnavailableService):
        available = True

        def transcribe(self, _audio):
            raise STTInputError("Unsupported audio format. Upload PCM WAV or WebM audio.")

    routes2 = setup_stt_routes(AvailableInvalidService())
    route2 = next(route for route in routes2.routes if route.path == "/api/stt/transcribe")
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(route2.endpoint(file=UploadFile(filename="audio.bin", file=io.BytesIO(b"bad"))))
    assert invalid.value.status_code == 400
