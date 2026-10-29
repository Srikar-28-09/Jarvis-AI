"""Multi-provider speech-to-text service."""

import importlib.util
import io
import logging
import os
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

LOCAL_MODEL_NAMES = frozenset({"tiny", "base", "small", "medium", "large-v3"})
LOCAL_MODEL_ROOT = Path(DATA_DIR) / "models" / "stt"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"


class STTInputError(ValueError):
    """Audio is empty, malformed, or not a supported upload format."""


class STTUnavailableError(RuntimeError):
    """The configured speech-to-text provider is not ready."""


class STTTranscriptionError(RuntimeError):
    """A configured provider failed to decode or transcribe valid audio."""


def audio_file_details(audio_bytes: bytes) -> tuple[str, str]:
    """Identify supported RIFF/WAVE PCM or WebM uploads by content."""
    if len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
                if wav.getcomptype() != "NONE" or wav.getnframes() <= 0:
                    raise STTInputError("WAV audio must contain uncompressed PCM samples.")
                if wav.getnchannels() not in (1, 2) or wav.getframerate() < 8000:
                    raise STTInputError("WAV audio must be mono or stereo at 8 kHz or higher.")
        except (wave.Error, EOFError) as exc:
            raise STTInputError("The uploaded WAV file is malformed or incomplete.") from exc
        return ".wav", "audio/wav"
    if len(audio_bytes) >= 4 and audio_bytes.startswith(WEBM_MAGIC):
        return ".webm", "audio/webm"
    raise STTInputError("Unsupported audio format. Upload PCM WAV or WebM audio.")


def _model_directory(model_name: str) -> Path:
    if model_name not in LOCAL_MODEL_NAMES:
        raise STTUnavailableError(
            f"Unsupported local Whisper model '{model_name}'. Choose one of: "
            f"{', '.join(sorted(LOCAL_MODEL_NAMES))}."
        )
    return LOCAL_MODEL_ROOT / f"faster-whisper-{model_name}"


class STTService:
    """Dispatches to local Faster-Whisper, API endpoint, or browser mode."""

    def __init__(self):
        self._whisper_model = None
        self._model_name = None
        self._model_lock = threading.Lock()
        # Faster-Whisper model loading is protected separately from inference;
        # serializing inference avoids concurrent use of the single model copy.
        self._transcription_lock = threading.Lock()

    def _load_settings(self) -> dict:
        from src.settings import load_settings

        saved = load_settings()
        return {
            "stt_enabled": saved.get("stt_enabled", False),
            "stt_provider": saved.get("stt_provider", "disabled"),
            "stt_model": saved.get("stt_model", "base"),
            "stt_language": saved.get("stt_language", ""),
        }

    def _local_readiness(self, model_name: str) -> tuple[bool, str, Path | None]:
        try:
            model_dir = _model_directory(model_name)
        except STTUnavailableError as exc:
            return False, str(exc), None

        try:
            has_dependency = importlib.util.find_spec("faster_whisper") is not None
        except (ImportError, ValueError):
            has_dependency = False
        if not has_dependency:
            return False, "The local STT package is missing. Install the project's STT dependencies and rebuild the backend image.", model_dir
        if not model_dir.is_dir() or any(not (model_dir / name).is_file() for name in MODEL_FILES):
            return (
                False,
                f"Whisper model '{model_name}' is not installed locally. Place its Faster-Whisper model files under data/models/stt/faster-whisper-{model_name}; model weights are never downloaded automatically.",
                model_dir,
            )
        return True, "", model_dir

    def _readiness(self) -> tuple[bool, str]:
        settings = self._load_settings()
        if settings.get("stt_enabled") is not True:
            return False, "Speech-to-text is disabled in Odysseus settings."
        provider = settings.get("stt_provider")
        if not isinstance(provider, str) or provider == "disabled":
            return False, "No speech-to-text provider is configured."
        if provider == "browser":
            return True, ""
        if provider == "local":
            ready, reason, _ = self._local_readiness(settings.get("stt_model", "base"))
            return ready, reason
        if provider.startswith("endpoint:") and provider.split(":", 1)[1].strip():
            return True, ""
        return False, "The configured speech-to-text provider is invalid."

    @property
    def available(self) -> bool:
        return self._readiness()[0]

    @property
    def unavailable_reason(self) -> str:
        return self._readiness()[1]

    def _get_whisper(self):
        settings = self._load_settings()
        ready, reason, model_dir = self._local_readiness(settings.get("stt_model", "base"))
        if not ready or model_dir is None:
            raise STTUnavailableError(reason)

        model_name = settings["stt_model"]
        if self._whisper_model is not None and self._model_name == model_name:
            return self._whisper_model

        with self._model_lock:
            if self._whisper_model is not None and self._model_name == model_name:
                return self._whisper_model
            try:
                from faster_whisper import WhisperModel
                # Container hardware inspection found no GPU device passed into
                # the backend. CPU INT8 is the compatible path and avoids CUDA
                # assumptions or downloading GPU runtimes.
                self._whisper_model = WhisperModel(
                    str(model_dir),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=min(os.cpu_count() or 1, 4),
                    num_workers=1,
                )
                self._model_name = model_name
                logger.info("Faster-Whisper '%s' loaded on CPU (int8)", model_name)
            except Exception as exc:
                self._whisper_model = None
                self._model_name = None
                logger.error("Failed to load local Whisper model '%s': %s", model_name, exc)
                raise STTUnavailableError(
                    f"Could not load local Whisper model '{model_name}'. Check the model files and backend logs."
                ) from exc
        return self._whisper_model

    def _transcribe_local(self, audio_bytes: bytes, language: str = "") -> str:
        model = self._get_whisper()
        suffix, _mime_type = audio_file_details(audio_bytes)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            kwargs = {"language": language} if language else {}
            with self._transcription_lock:
                segments, info = model.transcribe(tmp_path, **kwargs)
                text = " ".join(segment.text.strip() for segment in segments).strip()
            logger.info(
                "Local STT completed (%s, %s): %d characters",
                self._model_name,
                info.language,
                len(text),
            )
            return text
        except (STTInputError, STTUnavailableError):
            raise
        except Exception as exc:
            logger.warning("Local STT could not transcribe uploaded audio: %s", exc)
            raise STTTranscriptionError(
                "Could not decode or transcribe this audio. Try a complete PCM WAV or WebM/Opus recording."
            ) from exc
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    def _transcribe_api(self, audio_bytes: bytes, endpoint_id: str, model: str, language: str = "") -> Optional[str]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error("STT endpoint %s not found", endpoint_id)
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        suffix, mime_type = audio_file_details(audio_bytes)
        files = {"file": (f"audio{suffix}", io.BytesIO(audio_bytes), mime_type)}
        data = {"model": model or "whisper-1"}
        if language:
            data["language"] = language
        try:
            response = httpx.post(url, headers=headers, files=files, data=data, timeout=60)
            response.raise_for_status()
            return response.json().get("text", "")
        except Exception as exc:
            logger.error("API STT transcription failed: %s", exc)
            return None

    def transcribe(self, audio_bytes: bytes) -> Optional[str]:
        settings = self._load_settings()
        if settings.get("stt_enabled") is not True:
            return None
        provider = settings.get("stt_provider")
        if provider in ("disabled", "browser"):
            return None

        audio_file_details(audio_bytes)
        model = settings.get("stt_model", "base")
        language = settings.get("stt_language", "")
        if provider == "local":
            return self._transcribe_local(audio_bytes, language)
        if isinstance(provider, str) and provider.startswith("endpoint:"):
            endpoint_id = provider.split(":", 1)[1]
            return self._transcribe_api(audio_bytes, endpoint_id, model, language)
        raise STTUnavailableError("The configured speech-to-text provider is invalid.")

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings.get("stt_provider", "disabled")
        ready, reason = self._readiness()
        stats: Dict[str, Any] = {
            "available": ready,
            "enabled": settings.get("stt_enabled") is True,
            "provider": provider,
            "model": settings.get("stt_model", "base"),
            "language": settings.get("stt_language", ""),
            "device": "cpu" if provider == "local" else None,
            "compute_type": "int8" if provider == "local" else None,
        }
        if reason:
            stats["reason"] = reason
        if provider == "local":
            stats["model_loaded"] = self._whisper_model is not None and self._model_name == stats["model"]
            stats["model_path"] = f"data/models/stt/faster-whisper-{stats['model']}"
        elif isinstance(provider, str) and provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        return stats


_stt_service = None


def get_stt_service() -> STTService:
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service
