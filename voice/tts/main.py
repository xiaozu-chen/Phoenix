import io
import os
import sys
import time
import types
import uuid
import threading
import collections
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile

from settings.config import TTSConfig
from sub.logger import log
from voice.lifecycle import touch as _touch_lifecycle

if TYPE_CHECKING:
    from chatterbox.tts_turbo import ChatterboxTurboTTS

_model = None
_model_lock = threading.Lock()
_chatterbox_patched = False

# Requests have wildly different text lengths -> wildly different KV-cache
# shapes each time. PyTorch's CUDA caching allocator never hands memory
# back to the OS/driver on its own, and it can't cleanly reuse a
# differently-shaped block from the last request, so over a long session
# the "reserved" VRAM footprint tends to ratchet upward (fragmentation),
# even though nothing is actually leaking Python-side. Nothing else in
# this module ever called torch.cuda.empty_cache() outside of a full
# unload_model(), so that reserved memory never got reclaimed mid-session.
# Doing it every single call would add a small sync/overhead cost to every
# request for no benefit, so only do it periodically.
_EMPTY_CACHE_EVERY_N_CALLS = 20
_generate_call_count = 0
_generate_call_count_lock = threading.Lock()

# Every /tts call gets registered here under a speech_id so a later
# /voice/interrupt call can work out exactly what was said before it got
# cut off. Bounded so a long-running server doesn't leak memory if the
# client never interrupts -- old entries just age out.
_SPEECH_REGISTRY_MAXLEN = 50
_speech_registry: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_speech_registry_lock = threading.Lock()

# Precomputed voice conditioning (Conditionals.save()'d ahead of time, see
# voice/tts/precompute_voice.py). Loaded once per .pt file and reused --
# unlike the wav flow, this is pure tensor deserialization, not
# re-embedding through VE/S3Gen, so it's cheap enough to swap voices
# between requests. Keyed by absolute .pt path rather than speaker_id so
# it stays correct if voices_dir or the model device ever changes.
_voice_conds_cache: dict[str, "object"] = {}
_voice_conds_lock = threading.Lock()


def _register_speech(speech_id: str, duration: float, full_text: str) -> None:
    with _speech_registry_lock:
        _speech_registry[speech_id] = {
            "duration": duration,
            "full_text": full_text,
        }
        while len(_speech_registry) > _SPEECH_REGISTRY_MAXLEN:
            _speech_registry.popitem(last=False)


def get_speech(speech_id: str) -> dict | None:
    with _speech_registry_lock:
        return _speech_registry.get(speech_id)


def _ensure_chatterbox_source() -> None:
    global _chatterbox_patched
    os.environ.setdefault("HF_HOME", TTSConfig.hf_home)
    os.makedirs(os.environ["HF_HOME"], exist_ok=True)
    source_dir = Path(TTSConfig.source_dir)
    if source_dir.exists():
        source = str(source_dir)
        if source not in sys.path:
            sys.path.insert(0, source)
        if "chatterbox" not in sys.modules:
            package = types.ModuleType("chatterbox")
            package.__path__ = [str(source_dir / "chatterbox")]
            package.__file__ = str(source_dir / "chatterbox" / "__init__.py")
            package.__package__ = "chatterbox"
            package.__version__ = "source"
            sys.modules["chatterbox"] = package

    if not _chatterbox_patched:
        import librosa

        original_resample = librosa.resample

        def _resample_float32(*args, **kwargs):
            return np.asarray(original_resample(*args, **kwargs), dtype=np.float32)

        librosa.resample = _resample_float32
        _chatterbox_patched = True


def get_model() -> "ChatterboxTurboTTS":
    global _model
    if _model is not None:
        _touch_lifecycle()
        return _model

    with _model_lock:
        if _model is None:
            _ensure_chatterbox_source()
            from chatterbox.tts_turbo import ChatterboxTurboTTS

            variant = TTSConfig.variant
            nano = variant == "nano"

            log(
                target_log="system",
                log_type="info",
                message=f"Loading ChatterBox {variant.capitalize()} TTS model (device={TTSConfig.device}, "
                        f"model_dir={TTSConfig.model_dir})...",
            )

            t0 = time.time()
            _model = ChatterboxTurboTTS.from_local(TTSConfig.model_dir, TTSConfig.device, nano=nano)
            log(
                target_log="system",
                log_type="info",
                message=f"ChatterBox {variant.capitalize()} TTS model loaded in {time.time() - t0:.2f}s",
            )
    _touch_lifecycle()
    return _model


def unload_model() -> None:
    """Drops the loaded ChatterBox model so its memory can be reclaimed.
    Safe to call when nothing's loaded. get_model() transparently
    reloads on the next call."""
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

    log(target_log="system", log_type="info", message="ChatterBox TTS model unloaded (idle).")


def _resolve_precomputed_conds(model, speaker_id: str | None):
    """If `speaker_id` matches a precomputed voice under
    TTSConfig.voices_dir/<speaker_id>.pt, loads (and memoizes) it and
    returns the Conditionals object. Returns None if there's no such
    file, so the caller can fall back to the legacy wav-based flow --
    this never touches audio, the voice encoder, or S3Gen's reference
    encoder for a voice that's already been compiled."""
    voices_dir = getattr(TTSConfig, "voices_dir", None)
    if not voices_dir or not speaker_id:
        return None

    pt_path = os.path.join(voices_dir, f"{speaker_id}.pt")
    if not os.path.exists(pt_path):
        return None

    cached = _voice_conds_cache.get(pt_path)
    if cached is not None:
        return cached

    with _voice_conds_lock:
        cached = _voice_conds_cache.get(pt_path)
        if cached is None:
            _ensure_chatterbox_source()
            from chatterbox.tts_turbo import Conditionals

            cached = Conditionals.load(pt_path, map_location=model.device).to(model.device)
            _voice_conds_cache[pt_path] = cached
    return cached


def _run_generate(model, text: str, **kwargs):
    if TTSConfig.amp and TTSConfig.device.startswith("cuda"):
        import torch
        # Master weights stay fp32 -- autocast only runs individual ops
        # (matmuls etc.) in fp16 where it's numerically safe to do so,
        # rather than permanently converting the model like .half()
        # would. Safer, but still a real quality tradeoff -- see
        # TTSConfig.amp's comment.
<<<<<<< HEAD
        with torch.autocast(device_type="cuda", dtype=torch.int8):
=======
        with torch.autocast(device_type="cuda", dtype=torch.float16):
>>>>>>> c46c4fc4c354acc81047dbfe31c9164dfbc20f93
            result = model.generate(text, **kwargs)
    else:
        result = model.generate(text, **kwargs)

    _maybe_empty_cuda_cache()
    return result


def _maybe_empty_cuda_cache() -> None:
    """Periodically releases PyTorch's cached-but-unused CUDA blocks back
    to the driver so a long session's reserved VRAM doesn't just climb
    forever as request shapes vary (see _EMPTY_CACHE_EVERY_N_CALLS above).
    Cheap to skip on most calls; only actually touches CUDA every Nth one."""
    global _generate_call_count
    if not TTSConfig.device.startswith("cuda"):
        return

    with _generate_call_count_lock:
        _generate_call_count += 1
        should_empty = _generate_call_count % _EMPTY_CACHE_EVERY_N_CALLS == 0

    if should_empty:
        import torch
        torch.cuda.empty_cache()


def _generate_audio(
    text: str,
    speaker_id: str | None = None,
    speed: float | None = None,
    sdp_ratio: float | None = None,
    noise_scale: float | None = None,
    noise_scale_w: float | None = None,
) -> tuple[np.ndarray, int]:
    model = get_model()
    temperature = max(0.05, min(5.0, noise_scale if noise_scale is not None else TTSConfig.temperature))
    top_p = max(0.05, min(1.0, noise_scale_w if noise_scale_w is not None else TTSConfig.top_p))
    repetition_penalty = max(1.0, sdp_ratio if sdp_ratio is not None else TTSConfig.repetition_penalty)

    # Prefer an already-compiled voice (Conditionals.load()'d from
    # voices_dir/<speaker_id>.pt) over the wav flow. Falls back to
    # `TTSConfig.speaker_id` so the configured default voice also gets
    # the fast path, not just explicit per-request speaker_ids.
    precomputed_conds = _resolve_precomputed_conds(model, speaker_id) or _resolve_precomputed_conds(model, TTSConfig.speaker_id)
    audio_prompt_path = None
    if precomputed_conds is None:
        # Legacy fallback: no compiled voice found for this speaker_id,
        # so reference audio still has to be embedded live.
        audio_prompt_path = speaker_id if speaker_id and os.path.exists(speaker_id) else TTSConfig.audio_prompt_path or None

    # One shot for the whole reply now (see _generate_audio_with_duration) --
    # T3's autoregressive decoder defaults to a 1000-speech-token cap
    # (~20-40s depending on the checkpoint's frame rate), which was a
    # non-issue back when each sentence/clause got its own generate()
    # call. Scale the cap with the text so a long reply doesn't just get
    # cut off mid-sentence. ~15 speech tokens/word is a generous
    # (over-)estimate on purpose -- better to allow a bit of headroom
    # than clip audio.
    word_count = max(1, len(text.split()))
    max_gen_len = min(4000, max(1000, word_count * 15))

    wav = _run_generate(
        model,
        text,
        repetition_penalty=repetition_penalty,
        top_p=top_p,
        temperature=temperature,
        top_k=TTSConfig.top_k,
        audio_prompt_path=audio_prompt_path,
        conds=precomputed_conds,
        norm_loudness=TTSConfig.norm_loudness,
        max_gen_len=max_gen_len,
        n_cfm_timesteps=TTSConfig.n_cfm_timesteps,
    )
    audio = wav.squeeze().detach().cpu().numpy().astype(np.float32)

    # Preserve the endpoint's historical speed knob by resampling the
    # generated waveform. Neither ChatterBox variant (turbo or nano)
    # exposes a Melo-style duration control.
    requested_speed = speed or TTSConfig.speed
    if requested_speed and requested_speed > 0 and abs(requested_speed - 1.0) > 1e-3:
        target_len = max(1, int(round(len(audio) / requested_speed)))
        x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        audio = np.interp(x_new, x_old, audio).astype(np.float32)

    return audio, model.sr


def _generate_audio_with_duration(
    text: str,
    speaker_id: str | None = None,
    speed: float | None = None,
    sdp_ratio: float | None = None,
    noise_scale: float | None = None,
    noise_scale_w: float | None = None,
) -> tuple[np.ndarray, int, float]:
    # Used to split text into sentence/clause segments and call
    # model.generate() once per segment so /voice/interrupt could report
    # exactly which segment got cut off. Removed: each segment re-ran
    # prepare_conditionals() (re-embedding the reference voice through VE
    # + S3Gen from scratch) on top of its own generate() call, so a
    # 3-sentence reply meant 3x the fixed per-call overhead for no
    # benefit -- and a stall/failure on segment 2 or 3 silently produced
    # partial or no audio. One generate() call now; /voice/interrupt
    # estimates position via compute_interrupted_text()'s word-weighted
    # timing instead of a real per-segment timeline.
    text = text.strip()
    if not text:
        raise ValueError("Nothing to say -- text was empty.")

    audio, sr = _generate_audio(
        text,
        speaker_id=speaker_id,
        speed=speed,
        sdp_ratio=sdp_ratio,
        noise_scale=noise_scale,
        noise_scale_w=noise_scale_w,
    )
    duration = len(audio) / sr
    return audio, sr, duration


def synthesize_to_file(text: str, speaker_id: str | None = None, speed: float | None = None, sdp_ratio: float | None = None, noise_scale: float | None = None, noise_scale_w: float | None = None, use_clause_prosody: bool | None = None) -> str:
    os.makedirs(TTSConfig.tmp_dir, exist_ok=True)
    out_path = os.path.join(TTSConfig.tmp_dir, f"{uuid.uuid4().hex}.wav")
    audio, sr, _duration = _generate_audio_with_duration(
        text,
        speaker_id=speaker_id,
        speed=speed or TTSConfig.speed,
        sdp_ratio=sdp_ratio,
        noise_scale=noise_scale,
        noise_scale_w=noise_scale_w,
    )
    soundfile.write(out_path, audio, sr, format="WAV")
    return out_path


def synthesize_bytes( text: str, speaker_id: str | None = None, speed: float | None = None, sdp_ratio: float | None = None, noise_scale: float | None = None, noise_scale_w: float | None = None, use_clause_prosody: bool | None = None ) -> bytes:
    out_path = synthesize_to_file(
        text,
        speaker_id=speaker_id,
        speed=speed,
        sdp_ratio=sdp_ratio,
        noise_scale=noise_scale,
        noise_scale_w=noise_scale_w,
    )
    try:
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def synthesize_speech(
    text: str,
    speaker_id: str | None = None,
    speed: float | None = None,
    sdp_ratio: float | None = None,
    noise_scale: float | None = None,
    noise_scale_w: float | None = None,
    use_clause_prosody: bool | None = None,
) -> dict:
    """Synthesizes `text` in a single ChatterBox generate() call and
    returns:

        {
          "audio_bytes": <wav bytes>,
          "speech_id": "...",           # pass this to /voice/interrupt
          "plain_text": "..."           # the text that actually got spoken
        }

    ChatterBox doesn't expose per-word timings, so /voice/interrupt
    estimates how much text made it out from elapsed time alone (see
    compute_interrupted_text) rather than from a real per-segment
    timeline.

    `use_clause_prosody` is accepted (and still in TTSIn) purely so
    existing callers/UI don't break -- generation is no longer split
    into segments at all, so it's currently a no-op.
    """
    plain_text = (text or "").strip()
    if not plain_text:
        raise ValueError("Nothing to say -- text was empty.")

    audio, sr, duration = _generate_audio_with_duration(
        plain_text,
        speaker_id=speaker_id,
        speed=speed,
        sdp_ratio=sdp_ratio,
        noise_scale=noise_scale,
        noise_scale_w=noise_scale_w,
    )

    buf = io.BytesIO()
    soundfile.write(buf, audio, sr, format="WAV")
    audio_bytes = buf.getvalue()

    speech_id = uuid.uuid4().hex
    _register_speech(speech_id, duration, plain_text)

    return {
        "audio_bytes": audio_bytes,
        "speech_id": speech_id,
        "plain_text": plain_text,
    }


def _partial_text(full_text: str, frac: float) -> str:
    """Word-weighted estimate (len(word)+1 as a rough per-word duration
    weight) of how much of `full_text` had been spoken by the time
    `frac` of the total audio duration had played. Approximate by
    nature -- ChatterBox doesn't expose real per-word timing -- but
    good enough for /voice/interrupt to hand back a plausible
    "here's where she got cut off" caption. A word only counts if it
    fully finished before the cutoff; the word still being spoken when
    the interrupt landed is dropped rather than shown whole, since it
    never actually finished playing."""
    words = full_text.split()
    if not words or frac <= 0:
        return ""
    weights = [len(w) + 1 for w in words]
    target = frac * sum(weights)
    acc = 0.0
    out = []
    for w, wt in zip(words, weights):
        if acc + wt > target:
            break
        out.append(w)
        acc += wt
    return " ".join(out)


def compute_interrupted_text(speech_id: str, elapsed_seconds: float) -> dict:
    """Given how many seconds of a registered speech actually played
    before it was cut off, estimates how much of the text made it out
    (word-weighted proportional to elapsed/duration -- see
    _partial_text).

    Returns {"interrupted_text", "full_text", "was_interrupted"}.
    was_interrupted is False if elapsed_seconds covers (or exceeds) the
    whole line -- i.e. she wasn't actually cut off, she just finished.
    """
    entry = get_speech(speech_id)
    if entry is None:
        raise ValueError(f"Unknown speech_id: {speech_id}")

    full_text = entry["full_text"]
    duration = entry["duration"]
    elapsed_seconds = max(0.0, elapsed_seconds)

    if duration <= 0 or elapsed_seconds >= duration:
        return {"interrupted_text": full_text, "full_text": full_text, "was_interrupted": False}

    frac = elapsed_seconds / duration
    interrupted_text = _partial_text(full_text, frac)
    if interrupted_text and not interrupted_text.endswith(("—", "-")):
        interrupted_text += "—"

    return {"interrupted_text": interrupted_text, "full_text": full_text, "was_interrupted": True}