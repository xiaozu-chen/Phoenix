"""
extensions/host.py

The extension system's front door. Two ways an extension gets found:

  - "local"    -- a folder with an extension.json dropped straight into
                   Config.extensions_dir (data/extensions/<anything>/).
                   This is the "import" half of "import or point an
                   extension": copy the folder in, rescan, done.
  - "external" -- a folder living anywhere else on disk, whose path is
                   recorded in registry.json (inside extensions_dir).
                   This is the "point" half: register a path without
                   moving the extension there. Useful during an
                   extension's own development, or for one shared
                   between multiple apps.

Nothing in here talks tool-calling or chat.py -- this module only
manages the processes and the request/response channel. Re-adding a
tool-calling layer on top later just means writing a caller that
discovers extensions' declared capabilities (via list_extensions()'s
`info` field, whatever an extension chose to report at handshake) and
calls call_extension(id, method, params); the extensions themselves
never need to change, and neither does this module.

All state here is process-wide (module-level), same pattern as
llm/lifecycle.py and voice/lifecycle.py, since there's exactly one of
these per backend process and both the FastAPI app and the tray UI need
to see the same picture of what's running.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from extensions.manifest import ManifestError, load_manifest
from extensions.process import ExtensionProcess
from hook.registry import emit
from settings.config import Config
from sub.logger import log, log_exception

_lock = threading.RLock()
_processes: dict[str, ExtensionProcess] = {}
_load_errors: list[str] = []  # human-readable strings for manifests that failed to load


def _registry_path() -> Path:
    return Path(Config.extensions_dir) / "registry.json"


def _load_external_paths() -> list[str]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(p) for p in data.get("external_paths", [])]


def _save_external_paths(paths: list[str]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"external_paths": paths}, indent=2), encoding="utf-8")


def add_external_extension(folder: str) -> None:
    """Registers a folder elsewhere on disk (must contain extension.json)
    as an extension without copying it into extensions_dir. This is the
    "point" side of discovery. Raises ManifestError if the folder isn't
    a valid extension, so the Extensions tab can show why an add failed."""
    folder_path = Path(folder).resolve()
    manifest_path = folder_path / "extension.json"
    load_manifest(manifest_path, source="external")  # validates, raises on failure

    paths = _load_external_paths()
    resolved = str(folder_path)
    if resolved not in paths:
        paths.append(resolved)
        _save_external_paths(paths)


def remove_external_extension(folder: str) -> None:
    paths = _load_external_paths()
    resolved = str(Path(folder).resolve())
    if resolved in paths:
        paths.remove(resolved)
        _save_external_paths(paths)


def _discover_manifests():
    manifests = []
    errors = []

    local_root = Path(Config.extensions_dir)
    if local_root.is_dir():
        for child in sorted(local_root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "extension.json"
            if not manifest_path.exists():
                continue
            try:
                manifests.append(load_manifest(manifest_path, source="local"))
            except ManifestError as exc:
                errors.append(str(exc))

    for external in _load_external_paths():
        manifest_path = Path(external) / "extension.json"
        try:
            manifests.append(load_manifest(manifest_path, source="external"))
        except ManifestError as exc:
            errors.append(str(exc))

    return manifests, errors


def rescan(start_new: bool = True) -> None:
    """Re-reads every manifest (local + external). New extensions are
    started (if start_new); extensions whose manifest disappeared are
    stopped and dropped; extensions that are still around keep running
    untouched (rescanning doesn't restart anything already working)."""
    global _load_errors

    with _lock:
        Path(Config.extensions_dir).mkdir(parents=True, exist_ok=True)
        manifests, errors = _discover_manifests()
        _load_errors = errors
        seen_ids = set()

        for manifest in manifests:
            seen_ids.add(manifest.id)
            existing = _processes.get(manifest.id)
            if existing is not None:
                existing.manifest = manifest  # pick up any manifest edits
                continue
            proc = ExtensionProcess(manifest)
            _processes[manifest.id] = proc
            if start_new:
                threading.Thread(target=proc.start, daemon=True).start()

        gone = [ext_id for ext_id in _processes if ext_id not in seen_ids]
        for ext_id in gone:
            proc = _processes.pop(ext_id)
            threading.Thread(target=proc.stop, daemon=True).start()

    if errors:
        for err in errors:
            log(target_log="system", log_type="warning", message=f"Extension manifest error: {err}")


def start_all() -> None:
    """Called once at app startup (main.py's lifespan)."""
    rescan(start_new=True)
    emit("extensions.started", count=len(_processes))


def stop_all() -> None:
    """Called at shutdown so extension child processes don't linger."""
    with _lock:
        procs = list(_processes.values())
    for proc in procs:
        try:
            proc.stop()
        except Exception:
            log_exception("system", f"Error stopping extension '{proc.manifest.id}'")


def list_extensions() -> list[dict]:
    """Snapshot for the Extensions tab: one dict per known extension,
    JSON-friendly and safe to call from the UI thread on a timer."""
    with _lock:
        procs = list(_processes.values())
    return [
        {
            "id": p.manifest.id,
            "name": p.info.get("name") or p.manifest.name,
            "version": p.info.get("version") or p.manifest.version,
            "description": p.manifest.description,
            "status": p.status,
            "source": p.manifest.source,
            "folder": str(p.manifest.folder),
            "last_error": p.last_error,
        }
        for p in procs
    ]


def load_errors() -> list[str]:
    with _lock:
        return list(_load_errors)


def restart_extension(ext_id: str) -> None:
    with _lock:
        proc = _processes.get(ext_id)
    if proc is None:
        raise KeyError(f"no extension registered with id '{ext_id}'")
    threading.Thread(target=proc.restart, daemon=True).start()


def call_extension(ext_id: str, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    """The entry point anything in the app (a future tool-calling layer,
    a hook handler, whatever) uses to actually invoke an extension.
    Raises KeyError if ext_id isn't known, ExtensionError (from
    extensions.process) if the call itself fails."""
    with _lock:
        proc = _processes.get(ext_id)
    if proc is None:
        raise KeyError(f"no extension registered with id '{ext_id}'")
    return proc.call(method, params, timeout=timeout)
