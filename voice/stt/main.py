import os
import time
import threading

from settings.config import STTConfig
from sub.logger import log
from voice.lifecycle import touch as _touch_lifecycle

_model = None
_model_lock = threading.Lock()

def get_model():
    global _model
    if _model is not None:
        _touch_lifecycle()
        return _model

    with _model_lock:
        if _model is None:
            # Deferred so importing this module (which routes/voice.py
            # does at process startup) doesn't drag in faster_whisper's
            # own heavy imports (av, ctranslate2, tokenizers,
            # huggingface_hub) before the server's even listening.
            from voice.stt.faster_whisper import WhisperModel

            log(
                target_log="system",
                log_type="info",
                message=f"Loading faster-whisper model '{STTConfig.model_size}' "
                         f"(device={STTConfig.device}, compute_type={STTConfig.compute_type})...",
            )
            t0 = time.time()
            _model = WhisperModel(
                STTConfig.model_dir,
                device=STTConfig.device,
                compute_type=STTConfig.compute_type,
                local_files_only=True,
            )
            log(
                target_log="system",
                log_type="info",
                message=f"faster-whisper model loaded in {time.time() - t0:.2f}s",
            )
    _touch_lifecycle()
    return _model


def unload_model() -> None:
    """Drops the loaded model so its memory (GPU or otherwise) can be
    reclaimed. Safe to call when nothing's loaded. get_model() will
    transparently reload on the next call -- this only affects memory,
    never correctness."""
    global _model
    with _model_lock:
        if _model is None:
            return
        _model = None

    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass

    log(target_log="system", log_type="info", message="faster-whisper model unloaded (idle).")


def transcribe_file(path: str, language: str | None = None) -> dict:
    model = get_model()

    segments, info = model.transcribe(
        path,
        language=language or STTConfig.language,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,  # merge short pauses instead of cutting mid-sentence
            speech_pad_ms=200,            # keep a little padding so words at edges aren't clipped
        ),
        condition_on_previous_text=False,   # stops hallucination loops from compounding across segments
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,    # flags/filters repetitive garbage text
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],  # fallback ladder: retries at higher temp only if low-confidence
    )

    seg_list = []
    text_parts = []
    for seg in segments:
        seg_list.append(
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
            }
        )
        text_parts.append(seg.text.strip())

    full_text = " ".join(part for part in text_parts if part)

    return {
        "text": full_text,
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "segments": seg_list,
    }


def transcribe_bytes(audio_bytes: bytes, suffix: str = ".wav", language: str | None = None) -> dict:
    import uuid

    os.makedirs(STTConfig.tmp_dir, exist_ok=True)
    tmp_path = os.path.join(STTConfig.tmp_dir, f"{uuid.uuid4().hex}{suffix}")

    try:
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        return transcribe_file(tmp_path, language=language)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass