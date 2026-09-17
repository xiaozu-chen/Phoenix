"""
memory/context.py

Ephemeral, per-turn context that gets folded into what the LLM sees, but
never written to the `messages` table -- if it's not meant to live in
Phoenix's long-term memory, it belongs here, not in chat.py.

Two things live here:

  - Environment awareness (current time/day). Gated to once every
    Config.timestamp_gap_seconds (30 min) so it doesn't spam every turn
    and crowd out the actual conversation in the context window.

  - Interruption awareness. When TTS gets cut off mid-line (see
    voice/tts/main.py + the /voice/interrupt route), we stash what she
    was cut off at here. It rides along with the *next* user turn so she
    knows she got interrupted, then it's gone -- draining the buffer is
    what makes it "next turn only". The buffer is capped
    (Config.interruption_history_limit) purely so a burst of rapid
    interruptions that never get consumed can't grow it forever; the
    oldest unconsumed ones just fall off.
"""

import threading
from collections import deque
from datetime import datetime

from settings.config import Config

_interruptions: deque = deque(maxlen=Config.interruption_history_limit)
_interruptions_lock = threading.Lock()


def record_interruption(full_text: str, interrupted_text: str) -> None:
    """Called from the /voice/interrupt route once it's worked out where
    in `full_text` playback actually stopped. No-ops if there was
    nothing left to cut (she finished the line before the interrupt
    landed)."""
    full_text = (full_text or "").strip()
    interrupted_text = (interrupted_text or "").strip()
    if not interrupted_text or interrupted_text == full_text:
        return
    with _interruptions_lock:
        _interruptions.append({"full_text": full_text, "interrupted_text": interrupted_text})


def pop_interruption_block() -> str:
    """Drain every pending interruption and render them for the next
    chat turn. This DRAINS (not peeks) the buffer -- once handed to a
    turn, it's consumed, which is what keeps this to "the next user
    turn" rather than repeating forever."""
    with _interruptions_lock:
        if not _interruptions:
            return ""
        pending = list(_interruptions)
        _interruptions.clear()

    blocks = [
        f'"{item["full_text"]}"\nYou were interrupted at: "{item["interrupted_text"]}"'
        for item in pending
    ]
    return "\n\n".join(blocks)


def should_include_environment(prev_ts_iso: str | None, current_time: float) -> bool:
    """Same 30-minute gate the existing timestamp SSE event uses -- one
    decision feeds both the UI event and the LLM context so they never
    disagree about whether 'enough time has passed'."""
    if prev_ts_iso is None:
        return True
    try:
        prev_time = datetime.fromisoformat(prev_ts_iso).timestamp()
    except ValueError:
        return True
    return (current_time - prev_time) >= Config.timestamp_gap_seconds


def format_env_time(dt: datetime) -> str:
    """`00:00 PM (Noon) @ July 24, 2026` -- note the hour is rendered as
    "00" (not "12") at both noon and midnight, matching the requested
    format exactly."""
    hour24 = dt.hour
    minute = dt.minute
    period = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12  # 12am and 12pm both fold to 0 -> "00"
    time_str = f"{hour12:02d}:{minute:02d} {period}"

    suffix = ""
    if hour24 == 12 and minute == 0:
        suffix = " (Noon)"
    elif hour24 == 0 and minute == 0:
        suffix = " (Midnight)"

    date_str = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    return f"{time_str}{suffix} @ {date_str}"
