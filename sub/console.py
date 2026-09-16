"""
sub/console.py

Terminal indicator for the backend -- so whoever's watching the process
can tell whether it's booting, loading a model, handling a request, or
blowing up, instead of staring at a blank terminal until something
breaks.

Design notes
------------
No animated spinners. Two voice models load on separate background
threads at startup (see voice/lifecycle.py:wake), and multiple HTTP
requests can be mid-flight on different threads at once. Rich's
spinner/status widgets assume a single "Live" region on screen -- two
threads fighting over one spinner corrupts the display. Plain,
icon-prefixed lines are safe to print from any thread (Console.print
takes an internal lock) and still make it obvious what's in progress
and what just finished, the same way a well-behaved server's access
log does.

File logging (data/logs/*.log) is untouched and still owned by
sub/logger.py -- log() there now also echoes through here, so every
existing log() call in the codebase (model loads, tool errors, voice
events, ...) shows up on screen for free. This module has no
dependency on sub/logger.py (logger imports this, not the other way
around) to avoid a circular import.
"""

import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme
from rich.traceback import Traceback
from rich import box

_theme = Theme({
    "ind.dim": "#4C566A",
    "ind.info": "#81A1C1",
    "ind.success": "bold #A3BE8C",
    "ind.warning": "bold #EBCB8B",
    "ind.error": "bold #BF616A",
    "ind.brand": "bold #88C0D0",
    "ind.border": "#5E81AC",
})

console = Console(theme=_theme, highlight=False)

BANNER = r"""
 _   _  ___  _   _  ___  __  __ ___
| \ | |/ _ \| \ | |/ _ \|  \/  |_ _|
|  \| | | | |  \| | | | | |\/| || |
| |\  | |_| | |\  | |_| | |  | || |
|_| \_|\___/|_| \_|\___/|_|  |_|___|
""".strip("\n")


def banner(subtitle: str = "") -> None:
    body = Text(BANNER, style="ind.brand")
    if subtitle:
        body.append("\n")
        body.append(subtitle, style="ind.dim")
    console.print(
        Panel(body, border_style="ind.border", box=box.DOUBLE, padding=(0, 2))
    )


def rule(title: str = "") -> None:
    console.rule(title, style="ind.border")


def info(msg: str) -> None:
    console.print(f"[ind.dim]\u203a[/] {msg}")


def success(msg: str) -> None:
    console.print(f"[ind.success]\u2714[/] {msg}")


def warning(msg: str) -> None:
    console.print(f"[ind.warning]![/] {msg}")


def error(msg: str) -> None:
    console.print(f"[ind.error]\u2718[/] {msg}")


def exception(context: str, exc: BaseException | None = None) -> None:
    """Prints a full, syntax-highlighted traceback panel for `exc`
    (or whatever exception is currently being handled, if called with
    no argument from inside an `except` block) under a short
    human-readable `context` line.

    This is the terminal equivalent of norma's log_tail(): on failure,
    don't just print "Request failed: <str(e)>" and move on -- show
    the actual traceback so the cause is visible right away, without
    having to go dig it out of a log file.
    """
    if exc is None:
        exc = sys.exc_info()[1]

    if exc is None:
        error(f"{context} (no active exception to show)")
        return

    tb = Traceback.from_exception(
        type(exc),
        exc,
        exc.__traceback__,
        show_locals=False,
        width=max(console.width - 4, 60),
    )
    console.print(
        Panel(
            tb,
            title=f"[ind.error]\u2718 {context}[/]",
            border_style="ind.error",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
