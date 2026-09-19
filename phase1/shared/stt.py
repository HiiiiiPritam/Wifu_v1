"""Speech-to-text: local faster-whisper, or Groq's hosted Whisper.

Local keeps your audio on the PC but runs on the CPU (slow, and it
competes with everything else). Groq is much faster and more accurate
(large-v3-turbo vs base), on the same free tier, at the cost of sending
the audio clip to Groq -- the same place your transcribed text already
goes for the LLM.
"""
import asyncio
import threading

GROQ_STT_MODEL = "whisper-large-v3-turbo"
LOCAL_MODEL_SIZE = "base"

_local_model = None
_local_lock = threading.Lock()


def local_model():
    """Loaded on first use, not at import: it takes several seconds and a
    few hundred MB, which is wasted if Groq STT never fails over to it."""
    global _local_model
    with _local_lock:
        if _local_model is None:
            from faster_whisper import WhisperModel

            print("Loading local speech recognition model (first run downloads it)...")
            _local_model = WhisperModel(LOCAL_MODEL_SIZE, device="cpu", compute_type="int8")
        return _local_model


def transcribe_local(audio) -> str:
    """audio: a file path or a float32 numpy array. Blocking."""
    segments, _ = local_model().transcribe(audio, language="en", beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def transcribe_groq(async_client, audio_bytes: bytes, filename: str) -> str:
    result = await async_client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=GROQ_STT_MODEL,
        language="en",
        response_format="text",
    )
    # response_format="text" comes back as a plain string in groq>=1.x,
    # but older builds wrap it; handle both.
    text = result if isinstance(result, str) else getattr(result, "text", "")
    return text.strip()


async def transcribe_local_async(path: str) -> str:
    return await asyncio.to_thread(transcribe_local, path)
