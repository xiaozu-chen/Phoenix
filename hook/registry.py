"""
hook/registry.py

A tiny pub/sub so other code can react to things happening in the app
(new message, reply generated, transcription done, etc) without
chat.py and voice.py having to know or care who's listening.

Usage:

    from hook.registry import on, emit

    @on("chat.reply")
    def notify_discord(message, reply, **kwargs):
        ...

    # elsewhere, wherever the event actually happens
    emit("chat.reply", message=inp.message, reply=reply)

Handlers run synchronously, in registration order, on whatever thread
called emit(). If a handler blows up we log it and move on -- one
broken hook should never take down a chat request.

If a handler is slow or does I/O (calling out to Discord, hitting a
webhook, whatever), have IT spin up its own thread, not emit()'s job.
Guessing wrong about "fire and forget" vs "must complete before we
continue" is worse than making every hook author be explicit about it.

Handlers should accept **kwargs even if they only care about a couple
of fields -- events may grow new fields over time and a handler that
does `def f(message, reply)` with no **kwargs will break the day we
add one.
"""

from collections import defaultdict
from typing import Callable

from sub.logger import log_exception

_handlers: dict[str, list[Callable]] = defaultdict(list)


def on(event: str):
    """Decorator: register a function to run when `event` fires."""

    def decorator(fn: Callable) -> Callable:
        _handlers[event].append(fn)
        return fn

    return decorator


def register(event: str, fn: Callable) -> None:
    """Same as @on but as a plain call, for when a decorator is awkward."""
    _handlers[event].append(fn)


def emit(event: str, **kwargs) -> None:
    """Fire `event`, calling every handler registered for it with kwargs.

    Never raises -- a hook throwing is a hook problem, not a request
    problem, so we log it (system log) and keep going through the rest
    of the handlers.
    """
    for fn in _handlers.get(event, []):
        try:
            fn(**kwargs)
        except Exception:
            log_exception(
                "system",
                f"Hook '{getattr(fn, '__name__', fn)}' failed on event '{event}'",
            )


def registered(event: str) -> list[Callable]:
    """Mostly for debugging/tests: what's listening to `event` right now."""
    return list(_handlers.get(event, []))