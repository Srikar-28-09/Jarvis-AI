import os
import tempfile
import pytest
from services.stt.stt_service import STTService
from services.stt.stt_service import STTTranscriptionError


def test_stt_local_transcribe_leak_on_error():
    service = STTService()

    class MockWhisper:
        def transcribe(self, *args, **kwargs):
            raise ValueError("Simulated transcribe error")

    service._get_whisper = lambda: MockWhisper()

    # Track WebM files in the temp directory before running transcription
    temp_dir = tempfile.gettempdir()
    webm_before = {f for f in os.listdir(temp_dir) if f.endswith(".webm")}

    # A valid WebM signature reaches the decoder; the mock raises and the
    # service returns an actionable transcription failure.
    with pytest.raises(STTTranscriptionError, match="Could not decode or transcribe"):
        service._transcribe_local(b"\x1aE\xdf\xa3webm-fixture")

    # Track WebM files in the temp directory after running transcription
    webm_after = {f for f in os.listdir(temp_dir) if f.endswith(".webm")}

    # Assert that no new temp files were leaked
    leaked = webm_after - webm_before
    assert len(leaked) == 0, f"Leaked files: {leaked}"
