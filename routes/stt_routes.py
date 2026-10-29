# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

from fastapi import APIRouter, HTTPException, UploadFile, File
import logging
import time

from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES
from src.request_timing import current_request_id, log_stage
from services.stt.stt_service import STTInputError, STTTranscriptionError, STTUnavailableError

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/stats")
    async def get_stt_stats():
        """Get STT service statistics"""
        try:
            return stt_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/transcribe")
    async def transcribe_audio(file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        started = time.perf_counter()
        outcome = "ok"
        try:
            if not stt_service.available:
                reason = getattr(stt_service, "unavailable_reason", "")
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": reason or "STT service is not available.",
                        "provider": stt_service.get_stats().get("provider"),
                    },
                )

            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            text = stt_service.transcribe(audio_bytes)
            if text is None or not text.strip():
                raise HTTPException(
                    status_code=422,
                    detail={"message": "No speech was recognized in the uploaded audio."}
                )

            return {"text": text}

        except HTTPException:
            outcome = "error"
            raise
        except STTInputError as e:
            outcome = "error"
            raise HTTPException(status_code=400, detail={"message": str(e)}) from e
        except STTUnavailableError as e:
            outcome = "error"
            raise HTTPException(status_code=503, detail={"message": str(e)}) from e
        except STTTranscriptionError as e:
            outcome = "error"
            raise HTTPException(status_code=422, detail={"message": str(e)}) from e
        except Exception as e:
            outcome = "error"
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Transcription failed: {str(e)}"}
            )
        finally:
            log_stage("stt", (time.perf_counter() - started) * 1000,
                      outcome=outcome, request_id=current_request_id())

    return router
