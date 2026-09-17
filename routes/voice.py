import io
import threading
import wave

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

import time

from hook.registry import emit
from memory.context import record_interruption
from voice.lifecycle import wake as _wake_models, status as _lifecycle_status
from voice.stt.main import transcribe_bytes
from voice.tts.main import synthesize_bytes, synthesize_speech, compute_interrupted_text
from llm.lifecycle import ensure_running as _wake_llm, status as _llm_status
from settings.schemas import TTSIn, InterruptIn
from sub.logger import log, log_exception
from sub import console
from sub.activity import begin as _activity_begin, end as _activity_end

router = APIRouter()


def _silence_wav_bytes(duration_s: float = 0.3, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    n_frames = int(duration_s * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


@router.get("/status")
async def voice_status():
    """Current sleep/wake state for STT+TTS ("awake" if both models are
    loaded, "sleeping" if unloaded after Config.idle_timeout_seconds
    idle, "loading" if a wake is in progress), plus the LLM's own
    independent status (see llm/lifecycle.py) under "llm"."""
    result = _lifecycle_status()
    result["llm"] = _llm_status()
    return result


@router.post("/wake")
async def wake():
    """Manual wake-up call. Forces STT, TTS, and the LLM to all load
    right now (in parallel) if they aren't already, so the next real
    /stt, /tts, or /chat call doesn't have to eat that latency itself.
    Call this proactively -- e.g. the moment a voice UI opens or a mic
    button is pressed -- rather than waiting for everything to
    auto-wake on first use.

    Voice (STT+TTS) and the LLM are two independent subsystems with
    their own lifecycle/idle-timeout tracking (voice/lifecycle.py vs
    llm/lifecycle.py), so this fires both and waits for both -- calling
    just voice.lifecycle.wake() here would leave the LLM asleep with no
    other manual way to bring it back."""
    console.info("Voice: manual wake requested -- loading STT + TTS + LLM...")
    _activity_begin("Waking models...")

    def _wake_all() -> dict:
        results: dict = {}

        def _do_voice():
            results["voice"] = _wake_models(True)

        def _do_llm():
            try:
                results["llm"] = _wake_llm(blocking=True)
            except Exception:
                detail = log_exception("system", "LLM wake failed")
                results["llm"] = {"state": "error", "error": detail}

        threads = [threading.Thread(target=_do_voice), threading.Thread(target=_do_llm)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    try:
        result = await run_in_threadpool(_wake_all)
    finally:
        _activity_end("Waking models...")
    log(target_log="session", log_type="info", message=f"System woken manually -> {result}")
    emit("voice.woken", **result["voice"], llm=result["llm"])
    return result


@router.post("/warmup")
async def warmup():
    console.info("Voice: warming up (silent STT pass + short TTS pass)...")
    t0 = time.time()
    try:
        await run_in_threadpool(transcribe_bytes, _silence_wav_bytes(), ".wav")
        await run_in_threadpool(synthesize_bytes, "hi", None, 1.0)
    except Exception:
        detail = log_exception("system", "Voice warmup failed")
        raise HTTPException(status_code=500, detail=f"Failed to load voice models: {detail}")

    console.success(f"Voice: warmed up in {time.time() - t0:.2f}s")
    log(target_log="session", log_type="info", message="Voice models warmed up")
    return {"status": "ready"}


@router.post("/stt")
async def stt(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload.")

    suffix = ""
    if audio.filename and "." in audio.filename:
        suffix = "." + audio.filename.rsplit(".", 1)[-1]
    suffix = suffix or ".wav"

    console.info(f"Voice: transcribing {len(audio_bytes)} bytes ({suffix})...")
    t0 = time.time()
    _activity_begin("Transcribing audio...")
    try:
        result = await run_in_threadpool(transcribe_bytes, audio_bytes, suffix)
    except Exception:
        detail = log_exception("system", "STT failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {detail}")
    finally:
        _activity_end("Transcribing audio...")

    console.success(
        f"Voice: transcribed in {time.time() - t0:.2f}s -> "
        f"{len(result['text'])} chars ({result['language']})"
    )
    log(
        target_log="session",
        log_type="info",
        message=f"STT transcribed {len(audio_bytes)} bytes -> "
                 f"{len(result['text'])} chars ({result['language']})",
    )
    emit("voice.stt", text=result["text"], language=result["language"], duration=result["duration"])

    return result


@router.post("/tts")
async def tts(inp: TTSIn):
    text = inp.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text.")

    console.info(f"Voice: synthesizing {len(text)} chars...")
    t0 = time.time()
    _activity_begin("Synthesizing speech...")
    try:
        result = await run_in_threadpool(
            synthesize_speech,
            text,
            inp.speaker_id,
            inp.speed,
            inp.sdp_ratio,
            inp.noise_scale,
            inp.noise_scale_w,
            inp.use_clause_prosody,
        )
    except ValueError as e:
        console.warning(f"Voice: TTS rejected -- {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        detail = log_exception("system", "TTS failed")
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {detail}")
    finally:
        _activity_end("Synthesizing speech...")

    console.success(f"Voice: synthesized in {time.time() - t0:.2f}s")

    audio_bytes = result["audio_bytes"]
    log(
        target_log="session",
        log_type="info",
        message=f"TTS synthesized {len(text)} chars -> {len(audio_bytes)} bytes "
                 f"(speech_id={result['speech_id']})",
    )
    emit("voice.tts", text=text, audio_bytes=len(audio_bytes), speech_id=result["speech_id"])

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            # Client hangs onto this and sends it back with /voice/interrupt
            # if playback gets cut off mid-line.
            "X-Speech-Id": result["speech_id"],
        },
    )


@router.post("/interrupt")
async def interrupt(inp: InterruptIn):
    """Call this the INSTANT local VAD energy crosses the barge-in
    threshold during playback of a speech_id from /voice/tts -- not
    after STT finishes transcribing what the person said. Waiting on STT
    means waiting out silence-detection + the whole transcription
    round-trip before Phoenix even stops talking, which isn't really an
    interruption anymore by the time it lands. VAD firing mid-utterance
    is what makes this feel like an actual barge-in.

    `elapsed_seconds` is how much of that speech_id's audio the client
    had actually played back at the moment it decided to stop -- the
    server has no visibility into playback itself.
    """
    try:
        result = await run_in_threadpool(compute_interrupted_text, inp.speech_id, inp.elapsed_seconds)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if result["was_interrupted"]:
        record_interruption(result["full_text"], result["interrupted_text"])
        log(
            target_log="session",
            log_type="info",
            message=f"TTS interrupted at {inp.elapsed_seconds:.2f}s: {result['interrupted_text']!r}",
        )

    emit(
        "voice.interrupted",
        speech_id=inp.speech_id,
        elapsed_seconds=inp.elapsed_seconds,
        interrupted_text=result["interrupted_text"],
        full_text=result["full_text"],
        was_interrupted=result["was_interrupted"],
    )
    return result