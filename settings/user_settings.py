"""
settings/user_settings.py

Config.persona and LLMConfig.model_path are dataclass fields with
code-level defaults (blank persona, LLAMA_MODEL_PATH env var) -- fine
for a developer's .env, not fine for "the first-launch wizard asks you
for these and they should actually stick." This module is the small
persisted overlay that makes UI edits to those two survive a restart,
the same way settings/prompt.py already does for the system prompt
(kept as its own file/module rather than folded in here, since the
prompt is long-form text and these are short fields -- one clear place
per kind of setting).

    from settings.user_settings import apply_user_settings
    apply_user_settings()   # called once, early, in main.py

After that, Config.persona and LLMConfig.model_path read the user's
saved values everywhere in the app exactly like before -- nothing
downstream (llm/lifecycle.py, routes/chat.py, ...) needs to change.

Also owns the first-run flag file that gates whether main.py shows the
setup wizard before starting anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

from settings.config import BASE_DIR, Config, LLMConfig

USER_SETTINGS_PATH = BASE_DIR / "data" / "user_settings.json"
FIRST_RUN_MARKER_PATH = BASE_DIR / "data" / "first_run_complete.flag"

# Every key here must correspond to a real Config/LLMConfig attribute --
# apply_user_settings() below writes straight through to it.
_DEFAULTS = {
    "persona": Config.persona,
    "llm_model_path": LLMConfig.model_path,
}


def load_user_settings() -> dict:
    if not USER_SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        raw = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in raw.items() if k in _DEFAULTS})
    return merged


def save_user_settings(**kwargs) -> None:
    """Persists any subset of the known keys, e.g.
    save_user_settings(persona="...", llm_model_path="...")."""
    unknown = set(kwargs) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown user setting(s): {sorted(unknown)}")
    current = load_user_settings()
    current.update(kwargs)
    USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    apply_user_settings()


def apply_user_settings() -> None:
    """Pushes persisted overrides onto the live Config/LLMConfig class
    attributes. Safe to call repeatedly (e.g. right after a save)."""
    settings = load_user_settings()
    Config.persona = settings["persona"]
    LLMConfig.model_path = settings["llm_model_path"]


def is_first_run() -> bool:
    return not FIRST_RUN_MARKER_PATH.exists()


def mark_first_run_complete() -> None:
    FIRST_RUN_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIRST_RUN_MARKER_PATH.write_text("ok", encoding="utf-8")


# Apply whatever's already saved as soon as this module is imported, so
# import order elsewhere doesn't matter -- by the time anything reads
# Config.persona or LLMConfig.model_path, the user's saved values (if
# any) are already in place.
apply_user_settings()
