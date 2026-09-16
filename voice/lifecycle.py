"""
voice/lifecycle.py

Treats STT + TTS as one bundle that's either "awake" (both loaded,
ready to answer instantly) or "sleeping" (unloaded, memory reclaimed).

  - touch() is called by get_model() in both voice/stt/main.py and
    voice/tts/main.py every time a model is actually fetched -- so any
    real use (or an explicit wake()) resets the idle clock.
  - A daemon watchdog thread (start_watchdog(), started once from
    main.py's lifespan) checks periodically; once idle for
    Config.idle_timeout_seconds with nothing having touched it, it
    unloads both models and flips the state to "sleeping".
  - wake() is the manual call: forces both models to load right now,
    so a client can pre-warm ahead of actually needing them instead of
    eating the load latency on their first real request. get_model()
    being lazy means an ordinary /stt or /tts call also "wakes" things
    up on its own -- wake() just lets that happen ahead of time, on
    purpose, without waiting on the first request.

This module's own top-level imports are intentionally tiny (no torch,
no ChatterBox, no faster_whisper) -- those only get pulled in lazily, inside
wake()/the watchdog, when a model actually needs to load. That keeps
importing this module (which the model modules do at startup) cheap.
"""

import threading
import time

from settings.config import VoiceLifecycleConfig
from sub.logger import log, log_exception

_lock = threading.Lock()
_last_used_ts = time.time()
_state = "sleeping"  # "awake" | "loading" | "sleeping"
_watchdog_started = False


def touch() -> None:
    """Reset the idle clock and mark the bundle awake. Called by
    get_model() in both voice modules on every successful fetch."""
    global _last_used_ts, _state
    with _lock:
        _last_used_ts = time.time()
        _state = "awake"


def status() -> dict:
    with _lock:
        return {
            "state": _state,
            "idle_seconds": round(time.time() - _last_used_ts, 1),
            "idle_timeout_seconds": VoiceLifecycleConfig.idle_timeout_seconds,
        }


def _set_state(new_state: str) -> None:
    global _state
    with _lock:
        _state = new_state


def wake(blocking: bool = True) -> dict:
    """Forces both STT and TTS models to be loaded right now. A no-op
    (fast) for any model that's already loaded."""
    from voice.stt.main import get_model as get_stt_model
    from voice.tts.main import get_model as get_tts_model

    _set_state("loading")

    def _guarded(name: str, fn, errors: dict) -> None:
        # Bare `threading.Thread(target=fn)` swallows exceptions -- they
        # print to stderr from the background thread and t.join() returns
        # normally regardless, so a failed load here used to look
        # identical to a successful one from the caller's side (state
        # flips to "awake", no error anywhere in the response). Wrap each
        # loader so a failure is logged properly and reported back.
        try:
            fn()
        except Exception:
            errors[name] = log_exception("system", f"Failed to wake {name} model")

    def _load() -> dict:
        t0 = time.time()
        errors: dict = {}
        # NOTE: these used to load in two parallel threads. On Windows,
        # two threads doing their first-ever import of a different heavy
        # compiled extension (ctranslate2 for STT, torch/tokenizers for
        # ChatterBox) at the exact same instant can race in the OS DLL
        # loader and produce a spurious ImportError -- it wouldn't
        # reproduce testing either import alone, only here. Loading them
        # one after another costs a bit of wall-clock time on a cold
        # start but is far more reliable than debugging a heisenbug.
        _guarded("stt", get_stt_model, errors)
        _guarded("tts", get_tts_model, errors)

        if errors:
            # At least one model failed to load -- don't claim "awake".
            _set_state("sleeping")
            log(
                target_log="system",
                log_type="error",
                message=f"Voice wake failed after {time.time() - t0:.2f}s: {errors}",
            )
        else:
            touch()
            log(target_log="system", log_type="info", message=f"Voice models woken in {time.time() - t0:.2f}s")
        return errors

    if blocking:
        errors = _load()
        result = status()
        if errors:
            result["errors"] = errors
        return result
    else:
        threading.Thread(target=_load, daemon=True).start()
        return status()


def _idle_watchdog(check_interval: float, idle_timeout: float) -> None:
    from voice.stt.main import unload_model as unload_stt
    from voice.tts.main import unload_model as unload_tts

    while True:
        time.sleep(check_interval)
        with _lock:
            idle_for = time.time() - _last_used_ts
            already_sleeping = _state == "sleeping"
        if idle_for >= idle_timeout and not already_sleeping:
            log(
                target_log="system",
                log_type="info",
                message=f"Voice models idle for {idle_for:.0f}s (>= {idle_timeout:.0f}s) -- unloading, going to sleep.",
            )
            unload_stt()
            unload_tts()
            _set_state("sleeping")


def start_watchdog() -> None:
    """Idempotent -- safe to call more than once (e.g. reload during
    dev), only ever starts one watchdog thread."""
    global _watchdog_started
    with _lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    threading.Thread(
        target=_idle_watchdog,
        args=(VoiceLifecycleConfig.idle_check_interval_seconds, VoiceLifecycleConfig.idle_timeout_seconds),
        daemon=True,
    ).start()