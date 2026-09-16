"""
extensions/manifest.py

Every extension is a folder containing an extension.json manifest next
to its binary:

    my-extension/
      extension.json
      my-extension.exe

    {
      "id": "weather",                 # unique, addressed by this elsewhere
      "name": "Weather",                # display name (Config's Extensions tab)
      "version": "1.0.0",
      "description": "Looks up current weather for a location.",
      "entry": "weather.exe",           # path to the binary, relative to this file
      "interpreter": null,              # optional, e.g. "python" for a .py entry during dev
      "args": []                        # optional extra argv appended after entry
    }

Only "id", "name", and "entry" are required -- everything else has a
sane default. This is intentionally *not* the same shape as the old
tools/registry.py ToolSpec: that described a single Python function's
signature; this describes a whole external process, since the point of
this system is that adding a new one is "drop a folder in, don't touch
or repackage the backend."
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


class ManifestError(Exception):
    """Raised for a missing/malformed extension.json or a missing entry
    binary. Callers should catch this per-extension and skip it (log +
    move on) rather than let one bad manifest stop discovery of every
    other extension."""


@dataclass
class ExtensionManifest:
    id: str
    name: str
    version: str
    description: str
    entry: str
    interpreter: str | None
    args: list
    folder: Path   # directory the manifest (and, normally, the binary) lives in
    source: str    # "local" (data/extensions/<id>/) or "external" (pointed-to path)

    @property
    def entry_path(self) -> Path:
        return (self.folder / self.entry).resolve()

    def command(self) -> list[str]:
        """The argv to hand to subprocess.Popen.

        A real binary (.exe, or anything else without a ".py" suffix)
        is run directly. A ".py" entry -- for developing an extension
        in Python before compiling it, or for people happy to just ship
        a script -- is run through an interpreter: whatever
        "interpreter" says, or failing that sys.executable in dev / a
        "python" found on PATH when this app itself is frozen (a frozen
        build doesn't carry its own Python interpreter to lend out).
        """
        entry = str(self.entry_path)
        if self.interpreter:
            return [self.interpreter, entry, *self.args]
        if entry.lower().endswith(".py"):
            py = sys.executable if not getattr(sys, "frozen", False) else "python"
            return [py, entry, *self.args]
        return [entry, *self.args]


def load_manifest(manifest_path: Path, source: str = "local") -> ExtensionManifest:
    import json

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"couldn't read {manifest_path}: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
        raise ManifestError(f"{manifest_path} isn't valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"{manifest_path} must contain a JSON object")

    missing = [k for k in ("id", "name", "entry") if not raw.get(k)]
    if missing:
        raise ManifestError(f"{manifest_path}: missing required field(s) {missing}")

    folder = manifest_path.resolve().parent
    manifest = ExtensionManifest(
        id=str(raw["id"]),
        name=str(raw["name"]),
        version=str(raw.get("version", "0.0.0")),
        description=str(raw.get("description", "")),
        entry=str(raw["entry"]),
        interpreter=(raw.get("interpreter") or None),
        args=list(raw.get("args", [])),
        folder=folder,
        source=source,
    )

    if not manifest.entry_path.exists():
        raise ManifestError(
            f"{manifest_path}: entry '{manifest.entry}' not found at {manifest.entry_path}"
        )

    return manifest
