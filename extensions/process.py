"""
extensions/process.py

Runs one extension binary as a long-lived subprocess (spawned once, not
per call) and speaks a tiny newline-delimited JSON protocol with it over
stdin/stdout:

    -> {"id": "<uuid>", "method": "<name>", "params": {...}}\n
    <- {"id": "<uuid>", "ok": true,  "result": {...}}\n
    <- {"id": "<uuid>", "ok": false, "error": "message"}\n

Right after spawning, the host sends a "handshake" request with no
params; the extension is expected to answer within HANDSHAKE_TIMEOUT_SECONDS
with at least {"name": ..., "version": ...} (and whatever else it wants
the host to know about it) so it shows up correctly in the tray's
Extensions tab. An extension that never answers its handshake is marked
"unresponsive" rather than treated as a crash -- it's still a live
process, just not talking, which is worth surfacing differently.

Extensions must not print anything else to stdout -- that's the
protocol channel. Debug output belongs on stderr, which this class
reads and forwards line-by-line into the system log so it's still
visible (Logs window -> System tab) without corrupting request/response
matching. A stray non-JSON stdout line is logged as a warning and
otherwise ignored rather than crashing the reader thread.
"""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from dataclasses import dataclass, field

from extensions.manifest import ExtensionManifest
from sub.logger import log

HANDSHAKE_TIMEOUT_SECONDS = 5.0
DEFAULT_CALL_TIMEOUT_SECONDS = 15.0


class ExtensionError(Exception):
    """Raised when an extension responds with ok: false, times out, or
    isn't running when called."""


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


class ExtensionProcess:
    """One running (or not-yet/no-longer-running) extension. Owns its
    subprocess and the two background threads that drain its stdout
    (protocol) and stderr (logs)."""

    def __init__(self, manifest: ExtensionManifest):
        self.manifest = manifest
        self._proc: subprocess.Popen | None = None
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

        self.info: dict = {}           # whatever the handshake result contained
        self.status = "stopped"        # stopped | starting | running | crashed | unresponsive
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return  # already running
            self.status = "starting"
            self.last_error = None
            try:
                creationflags = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags = subprocess.CREATE_NO_WINDOW
                self._proc = subprocess.Popen(
                    self.manifest.command(),
                    cwd=str(self.manifest.folder),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    creationflags=creationflags,
                )
            except OSError as exc:
                self.status = "crashed"
                self.last_error = f"failed to start: {exc}"
                log(target_log="system", log_type="error",
                    message=f"Extension '{self.manifest.id}' failed to start: {exc}")
                return

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            self.info = self.call("handshake", {}, timeout=HANDSHAKE_TIMEOUT_SECONDS)
            self.status = "running"
            log(target_log="system", log_type="info",
                message=(
                    f"Extension '{self.manifest.id}' ready "
                    f"({self.info.get('name', self.manifest.name)} "
                    f"v{self.info.get('version', self.manifest.version)})"
                ))
        except ExtensionError as exc:
            # The stdout reader thread already set self.status to
            # "crashed" if the process actually exited out from under
            # the handshake call -- don't stomp that with the more
            # generic "unresponsive" (still-running, just not
            # answering), since the two call for different next steps
            # (crashed: check its stderr / logs; unresponsive: it's
            # hung, maybe still worth a Restart).
            if self.status != "crashed":
                self.status = "unresponsive"
            self.last_error = str(exc)
            log(target_log="system", log_type="warning",
                message=f"Extension '{self.manifest.id}' didn't answer its handshake: {exc}")

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            self.status = "stopped"
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.status = "stopped"
        self._fail_all_pending("extension stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- request/response ---------------------------------------------

    def call(self, method: str, params: dict | None = None,
              timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS) -> dict:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise ExtensionError(f"extension '{self.manifest.id}' is not running")

        req_id = uuid.uuid4().hex
        pending = _Pending()
        with self._lock:
            self._pending[req_id] = pending

        line = json.dumps({"id": req_id, "method": method, "params": params or {}})
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise ExtensionError(f"failed to write to '{self.manifest.id}': {exc}") from exc

        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            if self.status == "running":
                self.status = "unresponsive"
            raise ExtensionError(
                f"'{self.manifest.id}' timed out after {timeout}s answering '{method}'"
            )

        resp = pending.response or {}
        if not resp.get("ok", False):
            raise ExtensionError(
                resp.get("error") or f"'{self.manifest.id}' returned an error with no message"
            )
        return resp.get("result", {}) or {}

    # -- internals ------------------------------------------------------

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw_line in proc.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    msg = json.loads(raw_line)
                except ValueError:
                    log(target_log="system", log_type="warning",
                        message=f"Extension '{self.manifest.id}' wrote non-JSON to stdout: {raw_line[:200]}")
                    continue

                req_id = msg.get("id")
                with self._lock:
                    pending = self._pending.pop(req_id, None) if req_id else None
                if pending is None:
                    log(target_log="system", log_type="warning",
                        message=f"Extension '{self.manifest.id}' sent an unmatched message: {raw_line[:200]}")
                    continue
                pending.response = msg
                pending.event.set()
        finally:
            # stdout closed -- the process exited (or was stopped, in
            # which case stop() already set status/cleared pending and
            # this is a no-op).
            if self.status not in ("stopped",):
                self.status = "crashed"
            self._fail_all_pending("extension process exited")

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw_line in proc.stderr:
            raw_line = raw_line.rstrip()
            if raw_line:
                log(target_log="system", log_type="info", message=f"[{self.manifest.id}] {raw_line}")

    def _fail_all_pending(self, reason: str) -> None:
        with self._lock:
            pending_items = list(self._pending.items())
            self._pending.clear()
        for _req_id, pending in pending_items:
            pending.response = {"ok": False, "error": reason}
            pending.event.set()
