"""
sub/activity.py

Tiny thread-safe "what is this process doing right now" tracker, purely
for UI purposes (the tray icon + status window in ui/tray.py). Nothing
in here affects request handling -- if this module broke, at worst the
status window shows stale info.

llm/lifecycle.py and voice/lifecycle.py already track "loaded or not",
but that's not the same question as "is a request in flight right
now" -- a model can be fully loaded and awake while the process sits
idle between requests. This module answers the second question.

Usage, wherever a request handler starts real work:

    from sub.activity import begin, end

    begin("Replying to chat...")
    try:
        ...actual work...
    finally:
        end("Replying to chat...")

or, equivalently, as a context manager:

    from sub.activity import busy

    with busy("Replying to chat..."):
        ...actual work...

Concurrent requests stack -- current() always reports whichever label
was most recently begun and hasn't ended yet, so nested or overlapping
activity still shows something reasonable rather than blanking out.
"""

import threading
import time

_lock = threading.Lock()
_stack: list[str] = []
_last_change_ts = time.time()


def begin(label: str) -> None:
    global _last_change_ts
    with _lock:
        _stack.append(label)
        _last_change_ts = time.time()


def end(label: str) -> None:
    """Pops one occurrence of `label`. Safe to call even if it's not
    on the stack (e.g. this module reloaded mid-request) -- just a
    no-op then, never raises."""
    global _last_change_ts
    with _lock:
        try:
            _stack.remove(label)
        except ValueError:
            pass
        _last_change_ts = time.time()


class busy:
    """Context-manager form of begin()/end()."""

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        begin(self.label)
        return self

    def __exit__(self, exc_type, exc, tb):
        end(self.label)
        return False


def current() -> str | None:
    """The most recently started still-active label, or None if idle."""
    with _lock:
        return _stack[-1] if _stack else None


def is_busy() -> bool:
    with _lock:
        return bool(_stack)


def idle_seconds() -> float:
    """Seconds since the activity stack last changed (went busy or
    went idle) -- not the same as "seconds since a model was used",
    see llm/voice lifecycle idle_seconds for that."""
    with _lock:
        return round(time.time() - _last_change_ts, 1)