from contextlib import asynccontextmanager
import os
import sys

# Windowed/no-console PyInstaller builds (console=False) run with
# sys.stdout/sys.stderr set to None. Several libraries -- uvicorn's
# default logging config among them -- call .isatty() on these streams
# during setup, which crashes with AttributeError on None. Give them a
# harmless dummy stream instead, before anything else can touch stdio.
if getattr(sys, "frozen", False):
    # encoding="utf-8" matters here, not just the None-check: without it,
    # open() falls back to the OS's default codepage (cp1252 on Windows),
    # which can't encode the Unicode symbols sub/console.py prints
    # (checkmarks, etc.). That raised UnicodeEncodeError the first time
    # console.success()/console.error() ran, which in turn broke the
    # /chat SSE stream and other in-flight responses mid-write.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import hook.handlers
from settings.user_settings import apply_user_settings
from routes.chat import router as chat_router
from routes.voice import router as voice_router

import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="torch.nn.utils.weight_norm")

# Applied again here even though settings/user_settings.py already runs
# this at import time -- cheap, idempotent, and makes it obvious at a
# glance that persona/model-path overrides are live before anything
# else in this file touches Config or LLMConfig.
apply_user_settings()


def _frontend_dist_dir() -> Path:
    """Where the built React app lives.

    Frozen (PyInstaller): bundled alongside everything else under
    sys._MEIPASS, see the ("static", "static") entry added to
    main.spec's datas.

    Dev: backend/static/, i.e. the contents of `npm run build`'s
    dist/ folder copied in manually before running `python main.py`
    or PyInstaller.

    Override with FRONTEND_DIST_DIR if you'd rather point elsewhere.
    """
    if override := os.environ.get("FRONTEND_DIST_DIR"):
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "static"
    return Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from voice.lifecycle import start_watchdog, wake
    from llm.lifecycle import ensure_running as wake_llm
    from llm.lifecycle import start_watchdog as start_llm_watchdog
    from settings.config import LLMConfig, VoiceLifecycleConfig
    from extensions.host import start_all as start_extensions, stop_all as stop_extensions

    # Always watching for idle, regardless of whether we prewarm below.
    start_watchdog()
    start_llm_watchdog()

    # Discovers and starts every extension under Config.extensions_dir
    # (plus anything "pointed" at via registry.json). Each extension is
    # its own long-lived subprocess -- see extensions/host.py -- so this
    # doesn't block on any of them being slow to start.
    start_extensions()

    if VoiceLifecycleConfig.warm_on_startup:
        # Non-blocking -- the two model loads happen on background
        # threads (in parallel with each other too, see
        # voice/lifecycle.py:wake) so the app is already accepting
        # requests before either finishes loading.
        wake(blocking=False)

    if LLMConfig.warm_on_startup:
        # Same idea as voice prewarm: start llama-server in the
        # background so the API can accept requests while the model loads.
        wake_llm(blocking=False)

    yield

    stop_extensions()
    
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Custom response header on /voice/tts (speech id, used by
    # /voice/interrupt) is invisible to browser JS unless explicitly
    # exposed.
    expose_headers=["X-Speech-Id"],
)

app.include_router(chat_router)
app.include_router(voice_router, prefix="/voice")

# Serve the built React app. Must be mounted last -- StaticFiles claims
# every path under "/" it's given (html=True falls back to index.html
# for unknown paths, which is what makes client-side routing work), so
# any API route mounted after this point would be unreachable.
_frontend_dir = _frontend_dist_dir()
if _frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
else:
    from sub.console import warning as console_warning
    console_warning(
        f"Frontend build not found at {_frontend_dir} -- API routes only. "
        "Run `npm run build` in the frontend folder (or set FRONTEND_DIST_DIR)."
    )


if __name__ == "__main__":
    import threading
    import uvicorn

    from settings.user_settings import is_first_run

    if is_first_run():
        # Blocks on the main thread, before the server or tray exist --
        # deliberately a pre-flight step, not part of the tray's normal
        # lifecycle, so there's no risk of e.g. llama-server starting
        # with an empty model path because it raced the wizard. Lives
        # in the backend's own window, never the web frontend, since
        # the frontend's reachable from a phone/other machine and local
        # setup (model file paths, etc) doesn't belong there.
        try:
            from ui.setup_wizard import run_setup_wizard
        except ImportError:
            # No tkinter available (e.g. a headless/server-only
            # checkout) -- skip silently and leave first-run unmarked,
            # so a future launch that does have a display still gets
            # offered the wizard once.
            pass
        else:
            run_setup_wizard()

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
            port=int(os.environ.get("BACKEND_PORT", "8000")),
            log_level=os.environ.get("BACKEND_LOG_LEVEL", "info"),
        )
    )

    # uvicorn.Server.run() drives its own asyncio loop and blocks, so it
    # needs its own thread -- the main thread is reserved for Tkinter's
    # mainloop below (Tk requires it). uvicorn already checks
    # threading.current_thread() is main_thread() before installing its
    # own signal handlers, so running it off-thread like this is safe
    # and doesn't need any extra config on our end.
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    def _stop_server() -> None:
        server.should_exit = True
        server_thread.join(timeout=10)

    try:
        from ui.tray import run_tray_app
    except ImportError:
        # pystray/Pillow aren't installed -- e.g. a bare dev checkout
        # that never pulled the tray extras. Fall back to the old
        # headless behavior: just block until the server thread exits,
        # same as calling uvicorn.run() directly used to.
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=1)
        except KeyboardInterrupt:
            _stop_server()
    else:
        # Blocks (owns the main thread) until "Exit" is chosen from the
        # tray menu -- that's the one button meant to replace "find
        # backend.exe in Task Manager and End Task".
        run_tray_app(on_exit=_stop_server)