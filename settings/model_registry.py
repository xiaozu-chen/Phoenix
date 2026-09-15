"""
settings/model_registry.py

A small address book of GGUF models the user has pointed the app at:

    data/models.json:
    {
      "models": [
        {"id": "a1b2c3d4", "name": "Llama 3 8B Instruct Q4", "path": "D:/models/llama3-8b-q4.gguf"}
      ]
    }

Deliberately reference-only, never copy-on-import: GGUF files routinely
run into multiple gigabytes, so "importing" one here means registering
its path and a friendly name, not duplicating gigabytes of it into
data/models/. This mirrors the extension system's "point" (register a
path elsewhere on disk) -- there's no "copy the bytes in" side, since
there'd be nothing to gain from it and a lot of disk space to lose.

There's exactly one place the *active* model path actually lives:
settings/user_settings.py's llm_model_path, which is what LLMConfig.model_path
reads. This module doesn't duplicate that -- "which registered model is
active" is derived by matching each entry's path against the current
llm_model_path, and set_active_model() below just calls
save_user_settings(llm_model_path=...) same as always. That keeps a
single source of truth: llm/lifecycle.py and everything else downstream
never needs to know this registry exists.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from settings.config import BASE_DIR
from settings.user_settings import load_user_settings, save_user_settings

REGISTRY_PATH = BASE_DIR / "data" / "models.json"


class ModelError(Exception):
    """Raised for a path that doesn't exist, isn't a .gguf file, or an
    unknown model id."""


def _load() -> dict:
    if not REGISTRY_PATH.exists():
        return {"models": []}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": []}
    data.setdefault("models", [])
    return data


def _save(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_models() -> list[dict]:
    """Every registered model, each tagged with whether it's the one
    LLMConfig.model_path currently points at."""
    active_path = load_user_settings()["llm_model_path"]
    return [
        {**m, "active": (m["path"] == active_path)}
        for m in _load()["models"]
    ]


def get_active_model() -> dict | None:
    return next((m for m in list_models() if m["active"]), None)


def add_model(path: str, name: str | None = None) -> dict:
    """Registers a .gguf file by path -- doesn't touch/copy the file
    itself. Returns the (possibly pre-existing, if this exact path was
    already registered) model dict. Raises ModelError if the path
    doesn't look like a usable model file."""
    resolved = str(Path(path).expanduser().resolve())
    if not Path(resolved).exists():
        raise ModelError(f"file not found: {resolved}")
    if not resolved.lower().endswith(".gguf"):
        raise ModelError(f"not a .gguf file: {resolved}")

    data = _load()
    for m in data["models"]:
        if m["path"] == resolved:
            return m  # already registered -- don't create a duplicate entry

    model = {
        "id": uuid.uuid4().hex[:8],
        "name": (name or Path(resolved).stem).strip() or Path(resolved).stem,
        "path": resolved,
    }
    data["models"].append(model)
    _save(data)
    return model


def remove_model(model_id: str) -> None:
    """Un-registers a model (just forgets it -- never deletes the file).
    If it was the active model, clears llm_model_path too, since it'd
    otherwise be left pointing at something no longer in the list."""
    data = _load()
    match = next((m for m in data["models"] if m["id"] == model_id), None)
    if match is None:
        return
    data["models"] = [m for m in data["models"] if m["id"] != model_id]
    _save(data)
    if load_user_settings()["llm_model_path"] == match["path"]:
        save_user_settings(llm_model_path="")


def set_active_model(model_id: str) -> dict:
    data = _load()
    match = next((m for m in data["models"] if m["id"] == model_id), None)
    if match is None:
        raise ModelError(f"no registered model with id '{model_id}'")
    save_user_settings(llm_model_path=match["path"])
    return match


def add_and_activate(path: str, name: str | None = None) -> dict:
    """Convenience for the setup wizard / "Import model...": register
    and immediately make it the active model in one step."""
    model = add_model(path, name)
    return set_active_model(model["id"])
