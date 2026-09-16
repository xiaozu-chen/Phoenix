import sys
import logging
import traceback as _traceback
from pathlib import Path

from settings.config import Logging
from sub import console as _console

log_format = logging.Formatter(
    fmt="%(asctime)s — %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

session_logger = logging.getLogger(Logging.session)
session_logger.setLevel(logging.DEBUG)

system_logger = logging.getLogger(Logging.system)
system_logger.setLevel(logging.DEBUG)

Path(Logging.session_path).parent.mkdir(parents=True, exist_ok=True)
Path(Logging.system_path).parent.mkdir(parents=True, exist_ok=True)

if not session_logger.handlers:
    session_handler = logging.FileHandler(Logging.session_path, mode="a")
    session_handler.setFormatter(log_format)
    session_logger.addHandler(session_handler)

if not system_logger.handlers:
    system_handler = logging.FileHandler(Logging.system_path, mode="a")
    system_handler.setFormatter(log_format)
    system_logger.addHandler(system_handler)


# Every log() call also gets echoed to the terminal (see sub/console.py)
# so nothing that's already being logged to file needs a second,
# separate call to show up on screen -- model loads, tool errors, voice
# events, etc. all become visible for free.
_CONSOLE_ECHO = {
    "debug": _console.info,
    "info": _console.info,
    "warning": _console.warning,
    "error": _console.error,
    "critical": _console.error,
}


def log(target_log, log_type, message):
    if target_log not in ("system", "session"):
        raise ValueError("Invalid log target. Choose 'system' or 'session'.")
    if log_type not in ("debug", "info", "warning", "error", "critical"):
        raise ValueError("Invalid log type.")

    if target_log == "session":
        logger = session_logger
    else:
        logger = system_logger

    log_func = getattr(logger, log_type)
    log_func(message)
    _CONSOLE_ECHO[log_type](message)


def log_exception(target_log, context, exc: BaseException | None = None) -> str:
    """Use this instead of log(..., "error", f"... {e}") whenever you're
    inside (or have) an exception you want to actually be able to debug.

    - Writes the full traceback (not just str(e)) into the usual log
      file for `target_log`.
    - Prints a syntax-highlighted traceback panel to the terminal via
      sub/console.py, the same way norma's log_tail() surfaces a
      failure immediately instead of leaving you to go find it later.

    Returns a short "{context}: {exc}" string, handy for building an
    HTTPException detail message from the same call site.
    """
    if target_log not in ("system", "session"):
        raise ValueError("Invalid log target. Choose 'system' or 'session'.")

    if exc is None:
        exc = sys.exc_info()[1]

    logger = session_logger if target_log == "session" else system_logger

    if exc is None:
        logger.error(context)
        _console.error(f"{context} (no active exception to show)")
        return context

    tb_text = "".join(
        _traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).rstrip()
    logger.error(f"{context}\n{tb_text}")
    _console.exception(context, exc)
    return f"{context}: {exc}"
