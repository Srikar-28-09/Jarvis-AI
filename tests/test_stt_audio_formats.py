import io
import wave

import pytest

from services.stt.stt_service import STTInputError, audio_file_details


def _wav_fixture():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    return buffer.getvalue()


def test_detects_wav_recording_from_riff_wave_header():
    assert audio_file_details(_wav_fixture()) == (".wav", "audio/wav")


def test_preserves_existing_webm_recording_default():
    assert audio_file_details(b"\x1aE\xdf\xa3webm") == (".webm", "audio/webm")


def test_rejects_malformed_wav_and_unknown_audio():
    with pytest.raises(STTInputError, match="malformed"):
        audio_file_details(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    with pytest.raises(STTInputError, match="Unsupported audio format"):
        audio_file_details(b"RIFF")
