import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from settings.config import Config, LLMConfig
from sub.logger import log, log_exception


def _llama_server_path() -> str:
    """Absolute path to the bundled llama-server binary.

    Never relies on PATH -- PATH contents differ per machine and a
    PyInstaller-packaged app doesn't reliably inherit the user's PATH
    anyway. Instead this resolves relative to the app's own bundle
    (or the project folder, in dev), mirroring resource_path() in main.py.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent

    exe_name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    path = base / "bin" / "llama" / exe_name
    if not path.exists():
        raise RuntimeError(
            f"llama-server binary not found at {path}. "
            "Make sure it's copied into bin/llama/ in the project and "
            "listed in main.spec's datas."
        )
    return str(path)

_lock = threading.RLock()
_process: subprocess.Popen | None = None
_last_used_ts = time.time()
_state = "sleeping"  # "awake" | "loading" | "sleeping" | "external"
_watchdog_started = False


def _endpoint() -> tuple[str, str]:
    parsed = urlparse(Config.llama_url)
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or 8080)
    return host, port


def _server_ready(timeout: float = 1.0) -> bool:
    try:
        r = httpx.get(f"{Config.llama_url}/health", timeout=timeout)
        if r.status_code < 500:
            return True
    except httpx.RequestError:
        pass

    try:
        r = httpx.get(f"{Config.llama_url}/v1/models", timeout=timeout)
        return r.status_code < 500
    except httpx.RequestError:
        return False


def touch() -> None:
    global _last_used_ts, _state
    with _lock:
        _last_used_ts = time.time()
        if _state != "external":
            _state = "awake"


def status() -> dict:
    with _lock:
        return {
            "state": _state,
            "pid": _process.pid if _process and _process.poll() is None else None,
            "idle_seconds": round(time.time() - _last_used_ts, 1),
            "idle_timeout_seconds": LLMConfig.idle_timeout_seconds,
            "url": Config.llama_url,
        }


def ensure_running(blocking: bool = True) -> dict:
    """Start llama-server if needed, then wait until its HTTP API answers.

    If something else is already serving Config.llama_url, it is treated as
    external and this module will never kill it during idle cleanup.
    """
    global _process, _state

    with _lock:
        if _server_ready():
            _state = "external" if _process is None else "awake"
            touch()
            return status()

        if _process is not None and _process.poll() is None:
            _state = "loading"
        else:
            if not LLMConfig.model_path:
                raise RuntimeError("LLAMA_MODEL_PATH is not set in .env")

            host, port = _endpoint()
            Path(LLMConfig.log_path).parent.mkdir(parents=True, exist_ok=True)
            log_file = open(LLMConfig.log_path, "a", encoding="utf-8")
            cmd = [
                _llama_server_path(),
                "-m",
                LLMConfig.model_path,
                "--host",
                host,
                "--port",
                port,
                "--jinja",
                "--embeddings",
                "-fa",
                "on",
                "-ngl",
                "99",
                "-c",
                "2048",
                "--temp",
                "1.0",
                "--top-k",
                "20",
                "--top-p",
                "0.95",
                "--min-p",
                "0",
                "--presence-penalty",
                "1.5",

            ]

            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            log(
                target_log="system",
                log_type="info",
                message=f"Starting llama-server on {Config.llama_url}...",
            )
            _state = "loading"
            _process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )

    def _wait_ready() -> dict:
        global _process, _state
        deadline = time.time() + LLMConfig.startup_timeout_seconds
        while time.time() < deadline:
            with _lock:
                if _process is not None and _process.poll() is not None:
                    code = _process.returncode
                    _process = None
                    _state = "sleeping"
                    raise RuntimeError(f"llama-server exited during startup with code {code}")
            if _server_ready():
                touch()
                log(target_log="system", log_type="info", message="llama-server is ready.")
                return status()
            time.sleep(LLMConfig.startup_poll_seconds)

        raise TimeoutError(
            f"llama-server did not become ready within "
            f"{LLMConfig.startup_timeout_seconds:.0f}s"
        )

    if blocking:
        return _wait_ready()

    def _wait_ready_logged() -> None:
        try:
            _wait_ready()
        except Exception:
            log_exception("system", "llama-server background startup failed")

    threading.Thread(target=_wait_ready_logged, daemon=True).start()
    return status()


def unload() -> None:
    global _process, _state
    with _lock:
        proc = _process
        if proc is None or proc.poll() is not None:
            _process = None
            if _state != "external":
                _state = "sleeping"
            return
        _process = None
        _state = "sleeping"

    log(target_log="system", log_type="info", message="Stopping idle llama-server...")
    proc.terminate()
    try:
        proc.wait(timeout=LLMConfig.shutdown_timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log(target_log="system", log_type="info", message="llama-server stopped.")


def _idle_watchdog(check_interval: float, idle_timeout: float) -> None:
    while True:
        time.sleep(check_interval)
        with _lock:
            idle_for = time.time() - _last_used_ts
            owned_running = _process is not None and _process.poll() is None
        if owned_running and idle_for >= idle_timeout:
            log(
                target_log="system",
                log_type="info",
                message=f"LLM idle for {idle_for:.0f}s (>= {idle_timeout:.0f}s) -- unloading.",
            )
            unload()


def start_watchdog() -> None:
    global _watchdog_started
    with _lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    threading.Thread(
        target=_idle_watchdog,
        args=(LLMConfig.idle_check_interval_seconds, LLMConfig.idle_timeout_seconds),
        daemon=True,
    ).start()