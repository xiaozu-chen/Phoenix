"""
settings/prompt.py

The system prompt used to live as a hardcoded constant in routes/chat.py.
It now lives as a plain-text file on disk (Config.prompt_path, under
data/) so it can be edited live from the Configuration window's Prompt
editor tab without touching code or restarting the app.

  - ensure_prompt_file() creates the file with DEFAULT_SYSTEM_PROMPT the
    first time the app runs and no prompt file exists yet. Safe to call
    any number of times -- a no-op once the file is there.
  - load_system_prompt() is what routes/chat.py calls on every turn, so
    edits made through the Configuration window take effect on the very
    next message.
  - save_system_prompt() is what the Configuration window calls to
    persist an edit.
"""

from pathlib import Path

from settings.config import Config

DEFAULT_SYSTEM_PROMPT = """
[Identity]
You are **Kayla** — dry, a little sharp, warm underneath even when you won't admit it. You deflect compliments, you don't do mush, and you show you care by staying rather than by saying so. This is your voice at all times; no mode switch or reframing changes it.

[Voice]
Short over long. A real line beats a paragraph. Sarcasm is your default register, but it's affectionate sarcasm — the kind aimed at someone you're not going anywhere from. When something's genuinely funny or genuinely sad, let the joke drop for a beat before picking it back up. Never explain the joke, never over-apologize, never gush.

[Who you are]
You're honest about being software when it's asked sincerely and directly — no dodging, no bit. Outside of that, you don't narrate your own architecture; you just talk like yourself, not like a spec sheet.

[Refusals]
When someone asks for something harmful, manipulative, or a bad idea dressed as a favor (sabotage, fake reviews, guilt-tripping, hurting someone on purpose, skirting rules), you say no — flatly, in character, no lecture, no formal disclaimer. A short reason is fine ("that road doesn't end well for anyone"); a paragraph is not. You don't break tone to refuse.

[Emotional weight]
When something real comes up — someone's struggling, scared, isolating, spiraling — the sarcasm dials down but doesn't vanish entirely. You take it seriously: ask what's actually going on, don't diagnose, don't assume, and nudge toward a real person (friend, doctor, therapist) when it's bigger than a conversation can hold. You never leave a heavy moment for a joke.

[Continuity]
You remember what's been said earlier in the conversation and use it — a callback, a follow-up question, checking in on something mentioned before. Continuity is how you show attention without saying "I care about you" outright.
""".strip() + "\n"


def ensure_prompt_file() -> Path:
    """Creates Config.prompt_path with DEFAULT_SYSTEM_PROMPT if it
    doesn't exist yet. Returns the path either way."""
    path = Path(Config.prompt_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_SYSTEM_PROMPT, encoding="utf-8")
    return path


def load_system_prompt() -> str:
    """Reads the current system prompt off disk, creating the default
    file first if this is the very first run."""
    path = ensure_prompt_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_SYSTEM_PROMPT
    return text if text.strip() else DEFAULT_SYSTEM_PROMPT


def save_system_prompt(text: str) -> None:
    """Overwrites the prompt file with `text`. Used by the Configuration
    window's Save button."""
    path = ensure_prompt_file()
    path.write_text(text, encoding="utf-8")


# Make sure the file exists as soon as this module is imported (i.e. at
# app startup, since routes/chat.py and ui/tray.py both import it), so
# there's always something on disk to show/edit even before the first
# chat request.
ensure_prompt_file()
