"""
hook/handlers.py

This is where you actually plug things into the events the app fires
(see hook/registry.py for how the plumbing works, and the emit() calls
in routes/chat.py + routes/voice.py for the full list of events).

Nothing in here does anything by default -- it's just imported once
(see main.py) so that any @on(...) decorators below actually register.
Uncomment / add your own as you build things out.

Events currently fired, and what they hand you:

    chat.message   -- message
        a user message just got stored
    chat.reply     -- message, reply
        the assistant replied to that message
    chat.reset     -- (no kwargs)
        /reset was called, history got wiped
    voice.stt      -- text, language, duration
        a voice clip just got transcribed
    voice.tts      -- text, audio_bytes, speech_id
        text just got synthesized to speech (audio_bytes = byte count,
        not the audio itself -- don't want to accidentally log a wav)
    voice.interrupted -- speech_id, elapsed_seconds, interrupted_text,
                          full_text, was_interrupted
        /voice/interrupt got called; was_interrupted is False if the
        line had actually already finished playing
    voice.woken      -- state, idle_seconds, idle_timeout_seconds, llm
        /voice/wake was called manually (as opposed to a model just
        lazily loading because a real request needed it). state/
        idle_seconds/idle_timeout_seconds describe the voice (STT+TTS)
        bundle; llm is the separate llm/lifecycle.py status dict (or
        {"state": "error", "error": ...} if the LLM failed to come up).
"""

from hook.registry import on

# --- example: log every reply to the system log, just to prove hooks work ---
#
# from sub.logger import log
#
# @on("chat.reply")
# def _log_reply(message, reply, **kwargs):
#     log(target_log="system", log_type="info", message=f"[hook] replied to: {message!r}")


# --- example: ping a Discord webhook whenever Mint-sama replies ---
#
# import httpx
#
# DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
#
# @on("chat.reply")
# def _notify_discord(message, reply, **kwargs):
#     httpx.post(DISCORD_WEBHOOK_URL, json={"content": reply}, timeout=10)