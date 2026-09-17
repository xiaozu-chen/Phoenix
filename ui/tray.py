import os
import re
import sqlite3
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

import pystray
from PIL import Image

from llm.lifecycle import status as _llm_status
from voice.lifecycle import status as _voice_status
from sub.activity import current as _current_activity
from settings.config import Config
from settings.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    load_system_prompt,
    save_system_prompt,
)
from settings.user_settings import load_user_settings, save_user_settings
from settings.model_registry import (
    ModelError,
    add_model,
    list_models,
    remove_model,
    set_active_model,
)
from extensions.manifest import ManifestError
from extensions.host import (
    add_external_extension,
    list_extensions,
    load_errors,
    rescan,
    restart_extension,
)

REFRESH_MS = 1000
LOG_REFRESH_MS = 2000
LOG_TAIL_LINES = 500
MEMORY_ROW_LIMIT = 500
_ICON_PATH = Path(__file__).resolve().parent.parent / "app.ico"
_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Phoenix" / "data" / "logs"
_LOG_FILES = {
    "System": _LOG_DIR / "system.log",
    "Session": _LOG_DIR / "session.log",
    "llama-server": _LOG_DIR / "llama-server.log",
}

# Terminal Theme Colors (Dark Theme / Monospace)
_BG = "#0f111a"
_FG = "#c0caf5"
_GREEN = "#9ece6a"
_YELLOW = "#e0af68"
_BLUE = "#7aa2f7"
_ORANGE = "#ff9e64"
_RED = "#f7768e"
_DIM = "#565f89"

# Log line format: "YYYY-MM-DD HH:MM:SS <sep> LEVEL <sep> message"
_LOG_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s*"
    r"[^\w\s]+\s*"
    r"(?P<level>[A-Za-z]+)\s*"
    r"[^\w\s]+\s*"
    r"(?P<msg>.*)$"
)

_LEVEL_TAGS = {
    "DEBUG": "level_debug",
    "INFO": "level_info",
    "WARNING": "level_warn",
    "WARN": "level_warn",
    "ERROR": "level_error",
    "CRITICAL": "level_error",
    "FATAL": "level_error",
}


def _level_tag(level: str) -> str:
    return _LEVEL_TAGS.get(level.upper(), "level_default")


def _get_status_display(state: str, blink_on: bool) -> tuple[str, str]:
    if state == "loading":
        return "[ LDNG ]", _YELLOW
    if state in ("awake", "external"):
        return "[  OK  ]", _GREEN
    # IDLE state blinks yellow (toggles between yellow and background/dim)
    color = _YELLOW if blink_on else _BG
    return "[ IDLE ]", color


def _panel_data(blink_on: bool) -> tuple[str, list[tuple[str, str, str]]]:
    llm = _llm_status()
    voice = _voice_status()
    activity = _current_activity()

    states = {llm["state"], voice["state"]}
    if "loading" in states:
        headline = "LOADING..."
    elif activity:
        headline = activity.upper()
    elif "awake" in states or "external" in states:
        headline = "ACTIVE"
    else:
        headline = "INACTIVE"

    llm_tag, llm_color = _get_status_display(llm["state"], blink_on)
    voice_tag, voice_color = _get_status_display(voice["state"], blink_on)
    
    if activity:
        act_tag = "[ BUSY ]"
        act_color = _BLUE
    else:
        act_tag = "[ IDLE ]"
        act_color = _YELLOW if blink_on else _DIM

    rows = [
        ("Language Model", llm_tag, llm_color),
        ("Voice Model", voice_tag, voice_color),
        ("Activity", act_tag, act_color),
    ]
    return headline, rows


def _read_tail(path: Path, max_lines: int) -> list[str] | str:
    """Return the last `max_lines` lines of `path`, or a placeholder string."""
    if not path.exists():
        return f"(no log file found at {path})"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        return f"(could not read log: {exc})"
    if not lines:
        return "(log is empty)"
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


class LogWindow:
    """Toplevel window with tabbed views of the system/session/llama-server logs."""

    def __init__(self, parent: tk.Tk):
        self._parent = parent
        self._closing = False
        self._after_id = None

        self.win = tk.Toplevel(parent)
        self.win.title("Phoenix Backend — Logs")
        self.win.geometry("640x420")
        self.win.configure(bg=_BG)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        try:
            self.win.iconbitmap(str(_ICON_PATH))
        except tk.TclError:
            pass

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Log.TNotebook", background=_BG, borderwidth=0)
        style.configure(
            "Log.TNotebook.Tab",
            background=_BG,
            foreground=_FG,
            font=("Consolas", 10),
            padding=(12, 6),
        )
        style.map(
            "Log.TNotebook.Tab",
            background=[("selected", _DIM)],
            foreground=[("selected", _GREEN)],
        )

        outer = tk.Frame(self.win, bg=_BG, padx=12, pady=12)
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer, style="Log.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self._text_widgets: dict[str, tk.Text] = {}
        for name, path in _LOG_FILES.items():
            tab = tk.Frame(self.notebook, bg=_BG)
            self.notebook.add(tab, text=name)

            scrollbar = tk.Scrollbar(tab, bg=_BG)
            scrollbar.pack(side="right", fill="y")

            text = tk.Text(
                tab,
                font=("Consolas", 9),
                bg=_BG,
                fg=_FG,
                insertbackground=_FG,
                relief="flat",
                wrap="none",
                state="disabled",
                yscrollcommand=scrollbar.set,
            )
            text.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=text.yview)

            text.tag_configure("date", foreground=_BLUE)
            text.tag_configure("time", foreground=_ORANGE)
            text.tag_configure("sep", foreground=_DIM)
            text.tag_configure("level_info", foreground=_GREEN)
            text.tag_configure("level_warn", foreground=_YELLOW)
            text.tag_configure("level_error", foreground=_RED)
            text.tag_configure("level_debug", foreground=_DIM)
            text.tag_configure("level_default", foreground=_FG)
            text.tag_configure("msg", foreground=_FG)
            text.tag_configure("placeholder", foreground=_DIM)

            self._text_widgets[name] = text

        btn_row = tk.Frame(outer, bg=_BG)
        btn_row.pack(fill="x", pady=(10, 0))

        tk.Label(
            btn_row,
            text=str(_LOG_DIR),
            font=("Consolas", 8),
            bg=_BG,
            fg=_DIM,
            anchor="w",
        ).pack(side="left")

        tk.Button(
            btn_row,
            text="[ Refresh ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._refresh,
        ).pack(side="right")

        self._refresh()

    @staticmethod
    def _insert_line(text: tk.Text, line: str):
        match = _LOG_LINE_RE.match(line)
        if not match:
            text.insert("end", line + "\n", "msg")
            return
        text.insert("end", match.group("date"), "date")
        text.insert("end", " ")
        text.insert("end", match.group("time"), "time")
        text.insert("end", "  |  ", "sep")
        text.insert("end", match.group("level"), _level_tag(match.group("level")))
        text.insert("end", "  |  ", "sep")
        text.insert("end", match.group("msg") + "\n", "msg")

    def _refresh(self):
        if self._closing:
            return
        for name, path in _LOG_FILES.items():
            lines = _read_tail(path, LOG_TAIL_LINES)
            text = self._text_widgets[name]
            # Preserve the user's scroll position across refreshes: if they're
            # pinned to the bottom, keep following the tail; otherwise, keep
            # them looking at roughly the same spot instead of snapping to top.
            top_fraction, bottom_fraction = text.yview()
            at_bottom = bottom_fraction >= 0.999
            text.config(state="normal")
            text.delete("1.0", "end")
            if isinstance(lines, str):
                text.insert("1.0", lines, "placeholder")
            else:
                for line in lines:
                    self._insert_line(text, line)
            if at_bottom:
                text.see("end")
            else:
                text.yview_moveto(top_fraction)
            text.config(state="disabled")
        self._after_id = self.win.after(LOG_REFRESH_MS, self._refresh)

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _close(self):
        self._closing = True
        if self._after_id is not None:
            self.win.after_cancel(self._after_id)
        self.win.destroy()


class ConfigWindow:
    """Toplevel window with tabbed configuration panels: Prompt editor,
    General (persona + LLM model path -- the same two things the
    first-launch wizard asks about, editable again anytime here),
    Extensions (manage extension binaries), and placeholders for
    settings that will land later so the tab strip doesn't need to be
    reshuffled when they show up."""

    _PLACEHOLDER_TABS = ["Voice"]

    def __init__(self, parent: tk.Tk):
        self._parent = parent
        self._dirty = False
        self._closing = False
        self._ext_rows_by_iid: dict[str, dict] = {}

        self.win = tk.Toplevel(parent)
        self.win.title("Phoenix Backend — Configuration")
        self.win.geometry("680x520")
        self.win.configure(bg=_BG)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        try:
            self.win.iconbitmap(str(_ICON_PATH))
        except tk.TclError:
            pass

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Log.TNotebook", background=_BG, borderwidth=0)
        style.configure(
            "Log.TNotebook.Tab",
            background=_BG,
            foreground=_FG,
            font=("Consolas", 10),
            padding=(12, 6),
        )
        style.map(
            "Log.TNotebook.Tab",
            background=[("selected", _DIM)],
            foreground=[("selected", _GREEN)],
        )
        style.configure(
            "Config.Treeview",
            background=_BG,
            fieldbackground=_BG,
            foreground=_FG,
            font=("Consolas", 9),
            rowheight=22,
            borderwidth=0,
        )
        style.configure(
            "Config.Treeview.Heading",
            background=_DIM,
            foreground=_FG,
            font=("Consolas", 9, "bold"),
        )
        style.map("Config.Treeview", background=[("selected", _DIM)])

        outer = tk.Frame(self.win, bg=_BG, padx=12, pady=12)
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer, style="Log.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self._build_prompt_tab()
        self._build_general_tab()
        self._build_extensions_tab()
        for name in self._PLACEHOLDER_TABS:
            self._build_placeholder_tab(name)

        self.status_var = tk.StringVar(value="")
        tk.Label(
            outer,
            textvariable=self.status_var,
            font=("Consolas", 9),
            bg=_BG,
            fg=_DIM,
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        self._refresh_extensions()

    def _build_prompt_tab(self):
        tab = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(tab, text="Prompt editor")

        tk.Label(
            tab,
            text=f"System prompt file: {Config.prompt_path}",
            font=("Consolas", 8),
            bg=_BG,
            fg=_DIM,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        text_frame = tk.Frame(tab, bg=_BG)
        text_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(text_frame, bg=_BG)
        scrollbar.pack(side="right", fill="y")

        self.prompt_text = tk.Text(
            text_frame,
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="flat",
            wrap="word",
            undo=True,
            yscrollcommand=scrollbar.set,
        )
        self.prompt_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.prompt_text.yview)
        self.prompt_text.insert("1.0", load_system_prompt())
        self.prompt_text.edit_modified(False)
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)

        btn_row = tk.Frame(tab, bg=_BG)
        btn_row.pack(fill="x", pady=(10, 0))

        tk.Button(
            btn_row,
            text="[ Reload from disk ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._reload_prompt,
        ).pack(side="left")

        tk.Button(
            btn_row,
            text="[ Reset to default ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_YELLOW,
            activebackground=_DIM,
            activeforeground=_YELLOW,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._reset_prompt,
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            btn_row,
            text="[ Save ]",
            font=("Consolas", 10, "bold"),
            bg=_BG,
            fg=_GREEN,
            activebackground=_DIM,
            activeforeground=_GREEN,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._save_prompt,
        ).pack(side="right")

    def _build_general_tab(self):
        tab = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(tab, text="General")

        current = load_user_settings()
        self._model_rows_by_iid: dict[str, dict] = {}

        tk.Label(
            tab, text="Persona notes", font=("Consolas", 10, "bold"),
            bg=_BG, fg=_FG, anchor="w",
        ).pack(fill="x")
        tk.Label(
            tab,
            text="Appended to the system prompt as extra notes. Leave blank to\n"
                 "use the default personality as-is.",
            font=("Consolas", 8), bg=_BG, fg=_DIM, justify="left", anchor="w",
        ).pack(fill="x", pady=(2, 6))

        self.persona_text = tk.Text(
            tab, font=("Consolas", 10), bg=_BG, fg=_FG, insertbackground=_FG,
            relief="flat", height=4, wrap="word",
        )
        self.persona_text.pack(fill="x")
        self.persona_text.insert("1.0", current["persona"])

        tk.Button(
            tab,
            text="[ Save persona ]",
            font=("Consolas", 10, "bold"),
            bg=_BG,
            fg=_GREEN,
            activebackground=_DIM,
            activeforeground=_GREEN,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._save_persona,
        ).pack(anchor="e", pady=(6, 0))

        # -- LLM models -------------------------------------------------

        tk.Label(
            tab, text="LLM models", font=("Consolas", 10, "bold"),
            bg=_BG, fg=_FG, anchor="w",
        ).pack(fill="x", pady=(18, 0))
        tk.Label(
            tab,
            text="Models you've imported (pointed at a .gguf file someone downloaded --\n"
                 "nothing gets copied). Pick which one is active; switching restarts\n"
                 "the LLM automatically if it's currently running.",
            font=("Consolas", 8), bg=_BG, fg=_DIM, justify="left", anchor="w",
        ).pack(fill="x", pady=(2, 6))

        model_tree_frame = tk.Frame(tab, bg=_BG)
        model_tree_frame.pack(fill="both", expand=True)

        model_scrollbar = tk.Scrollbar(model_tree_frame, bg=_BG)
        model_scrollbar.pack(side="right", fill="y")

        self.model_tree = ttk.Treeview(
            model_tree_frame,
            columns=("name", "active", "path"),
            show="headings",
            style="Config.Treeview",
            yscrollcommand=model_scrollbar.set,
            height=5,
        )
        self.model_tree.heading("name", text="Name")
        self.model_tree.heading("active", text="Active")
        self.model_tree.heading("path", text="Path")
        self.model_tree.column("name", width=140, anchor="w", stretch=False)
        self.model_tree.column("active", width=50, anchor="center", stretch=False)
        self.model_tree.column("path", width=340, anchor="w")
        self.model_tree.pack(side="left", fill="both", expand=True)
        model_scrollbar.config(command=self.model_tree.yview)

        model_btn_row = tk.Frame(tab, bg=_BG)
        model_btn_row.pack(fill="x", pady=(8, 0))

        tk.Button(
            model_btn_row, text="[ Set active ]", font=("Consolas", 10), bg=_BG, fg=_YELLOW,
            activebackground=_DIM, activeforeground=_YELLOW, relief="flat", bd=0,
            cursor="hand2", command=self._set_active_model,
        ).pack(side="left")

        tk.Button(
            model_btn_row, text="[ Remove ]", font=("Consolas", 10), bg=_BG, fg=_RED,
            activebackground=_DIM, activeforeground=_RED, relief="flat", bd=0,
            cursor="hand2", command=self._remove_model,
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            model_btn_row, text="[ Import model... ]", font=("Consolas", 10, "bold"), bg=_BG, fg=_GREEN,
            activebackground=_DIM, activeforeground=_GREEN, relief="flat", bd=0,
            cursor="hand2", command=self._import_model,
        ).pack(side="right")

        self._refresh_models()

    def _refresh_models(self):
        for iid in self.model_tree.get_children():
            self.model_tree.delete(iid)
        self._model_rows_by_iid.clear()
        for row in list_models():
            iid = row["id"]
            self.model_tree.insert(
                "", "end", iid=iid,
                values=(row["name"], "✓" if row["active"] else "", row["path"]),
            )
            self._model_rows_by_iid[iid] = row

    def _save_persona(self):
        save_user_settings(persona=self.persona_text.get("1.0", "end-1c").strip())
        self.status_var.set("persona saved")

    def _import_model(self):
        from tkinter import filedialog, simpledialog
        chosen = filedialog.askopenfilename(
            title="Select a .gguf model file",
            filetypes=[("GGUF model", "*.gguf"), ("All files", "*.*")],
        )
        if not chosen:
            return
        name = simpledialog.askstring(
            "Name this model", "A short name to show in the model list:",
            initialvalue=Path(chosen).stem, parent=self.win,
        )
        if name is None:
            return
        try:
            model = add_model(chosen, name)
        except ModelError as exc:
            self.status_var.set(f"couldn't import model: {exc}")
            return
        self.status_var.set(f"imported '{model['name']}' -- select it and hit Set active to use it")
        self._refresh_models()

    def _set_active_model(self):
        sel = self.model_tree.selection()
        if not sel:
            self.status_var.set("select a model first")
            return
        try:
            model = set_active_model(sel[0])
        except ModelError as exc:
            self.status_var.set(str(exc))
            return
        self._refresh_models()
        self._maybe_restart_llm_for_model_switch(model["name"])

    def _maybe_restart_llm_for_model_switch(self, model_name: str):
        """If llama-server is currently up, restart it so the switch
        actually takes effect now instead of silently doing nothing
        until the next idle-unload/wake cycle."""
        from llm.lifecycle import ensure_running, status as llm_status, unload

        state = llm_status()["state"]
        if state not in ("awake", "loading", "external"):
            self.status_var.set(f"active model set to '{model_name}' -- will load next time the LLM starts")
            return
        if state == "external":
            self.status_var.set(
                f"active model set to '{model_name}' -- not restarting llama-server "
                "since something else is managing it"
            )
            return

        self.status_var.set(f"switching to '{model_name}' -- restarting llama-server...")

        def _restart():
            unload()
            try:
                ensure_running(blocking=True)
                self.status_var.set(f"now running '{model_name}'")
            except Exception as exc:
                self.status_var.set(f"failed to restart llama-server with '{model_name}': {exc}")

        threading.Thread(target=_restart, daemon=True).start()

    def _remove_model(self):
        sel = self.model_tree.selection()
        if not sel:
            self.status_var.set("select a model first")
            return
        row = self._model_rows_by_iid.get(sel[0])
        remove_model(sel[0])
        self._refresh_models()
        if row:
            self.status_var.set(f"removed '{row['name']}' from the list (file untouched)")

    def _build_extensions_tab(self):
        tab = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(tab, text="Extensions")

        tk.Label(
            tab,
            text=f"Extensions folder: {Config.extensions_dir}",
            font=("Consolas", 8), bg=_BG, fg=_DIM, anchor="w",
        ).pack(fill="x", pady=(0, 6))

        tree_frame = tk.Frame(tab, bg=_BG)
        tree_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(tree_frame, bg=_BG)
        scrollbar.pack(side="right", fill="y")

        self.ext_tree = ttk.Treeview(
            tree_frame,
            columns=("name", "status", "version", "source"),
            show="headings",
            style="Config.Treeview",
            yscrollcommand=scrollbar.set,
        )
        for col, width in (("name", 160), ("status", 100), ("version", 80), ("source", 80)):
            self.ext_tree.heading(col, text=col.capitalize())
            self.ext_tree.column(col, width=width, anchor="w", stretch=(col == "name"))
        self.ext_tree.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.ext_tree.yview)
        self.ext_tree.bind("<<TreeviewSelect>>", self._on_extension_select)

        self.ext_detail_var = tk.StringVar(value="Select an extension to see details.")
        tk.Label(
            tab, textvariable=self.ext_detail_var, font=("Consolas", 8), bg=_BG, fg=_DIM,
            anchor="w", justify="left", wraplength=620,
        ).pack(fill="x", pady=(8, 0))

        btn_row = tk.Frame(tab, bg=_BG)
        btn_row.pack(fill="x", pady=(10, 0))

        tk.Button(
            btn_row, text="[ Rescan ]", font=("Consolas", 10), bg=_BG, fg=_FG,
            activebackground=_DIM, activeforeground=_FG, relief="flat", bd=0,
            cursor="hand2", command=self._rescan_extensions,
        ).pack(side="left")

        tk.Button(
            btn_row, text="[ Restart selected ]", font=("Consolas", 10), bg=_BG, fg=_YELLOW,
            activebackground=_DIM, activeforeground=_YELLOW, relief="flat", bd=0,
            cursor="hand2", command=self._restart_selected_extension,
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            btn_row, text="[ Open folder ]", font=("Consolas", 10), bg=_BG, fg=_FG,
            activebackground=_DIM, activeforeground=_FG, relief="flat", bd=0,
            cursor="hand2", command=self._open_extensions_folder,
        ).pack(side="left", padx=(10, 0))

        tk.Button(
            btn_row, text="[ Add extension... ]", font=("Consolas", 10, "bold"), bg=_BG, fg=_GREEN,
            activebackground=_DIM, activeforeground=_GREEN, relief="flat", bd=0,
            cursor="hand2", command=self._add_extension,
        ).pack(side="right")

    def _refresh_extensions(self):
        if self._closing:
            return
        selected_id = None
        sel = self.ext_tree.selection()
        if sel:
            selected_id = sel[0]

        for iid in self.ext_tree.get_children():
            self.ext_tree.delete(iid)
        self._ext_rows_by_iid.clear()

        for row in list_extensions():
            iid = row["id"]
            self.ext_tree.insert(
                "", "end", iid=iid,
                values=(row["name"], row["status"], row["version"], row["source"]),
            )
            self._ext_rows_by_iid[iid] = row
            if iid == selected_id:
                self.ext_tree.selection_set(iid)

        for err in load_errors():
            self.status_var.set(f"manifest error: {err}")

        # Extension processes transition state (starting -> running/crashed)
        # on their own background threads, so this window needs to keep
        # polling rather than only refreshing on user action.
        self.win.after(2000, self._refresh_extensions)

    def _on_extension_select(self, _event=None):
        sel = self.ext_tree.selection()
        if not sel:
            return
        row = self._ext_rows_by_iid.get(sel[0])
        if not row:
            return
        detail = f"{row['description'] or '(no description)'}\nFolder: {row['folder']}"
        if row["last_error"]:
            detail += f"\nLast error: {row['last_error']}"
        self.ext_detail_var.set(detail)

    def _rescan_extensions(self):
        rescan()
        self.status_var.set("rescanning...")
        self._refresh_now()

    def _restart_selected_extension(self):
        sel = self.ext_tree.selection()
        if not sel:
            self.status_var.set("select an extension first")
            return
        try:
            restart_extension(sel[0])
        except KeyError as exc:
            self.status_var.set(str(exc))
            return
        self.status_var.set(f"restarting '{sel[0]}'...")

    def _open_extensions_folder(self):
        folder = Path(Config.extensions_dir)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)  # Windows-only, matches the rest of this app
        except (AttributeError, OSError) as exc:
            self.status_var.set(f"couldn't open folder: {exc}")

    def _add_extension(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Select an extension folder (containing extension.json)")
        if not folder:
            return
        try:
            add_external_extension(folder)
        except ManifestError as exc:
            self.status_var.set(f"couldn't add extension: {exc}")
            return
        self.status_var.set(f"added extension from {folder}")
        rescan()
        self._refresh_now()

    def _refresh_now(self):
        """Immediate, one-off list refresh (as opposed to the polling
        loop in _refresh_extensions, which reschedules itself)."""
        for iid in self.ext_tree.get_children():
            self.ext_tree.delete(iid)
        self._ext_rows_by_iid.clear()
        for row in list_extensions():
            iid = row["id"]
            self.ext_tree.insert(
                "", "end", iid=iid,
                values=(row["name"], row["status"], row["version"], row["source"]),
            )
            self._ext_rows_by_iid[iid] = row

    def _build_placeholder_tab(self, name: str):
        tab = tk.Frame(self.notebook, bg=_BG)
        self.notebook.add(tab, text=name)
        tk.Label(
            tab,
            text=f"{name} settings are coming soon.",
            font=("Consolas", 10),
            bg=_BG,
            fg=_DIM,
        ).pack(expand=True)

    def _on_prompt_modified(self, _event=None):
        if self.prompt_text.edit_modified():
            self._dirty = True
            self.status_var.set("unsaved changes")
            self.prompt_text.edit_modified(False)

    def _reload_prompt(self):
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", load_system_prompt())
        self.prompt_text.edit_modified(False)
        self._dirty = False
        self.status_var.set("reloaded from disk")

    def _reset_prompt(self):
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", DEFAULT_SYSTEM_PROMPT)
        self.prompt_text.edit_modified(False)
        self._dirty = True
        self.status_var.set("reset to default (not yet saved)")

    def _save_prompt(self):
        content = self.prompt_text.get("1.0", "end-1c")
        try:
            save_system_prompt(content)
        except OSError as exc:
            self.status_var.set(f"failed to save: {exc}")
            return
        self._dirty = False
        self.status_var.set(f"saved to {Config.prompt_path}")

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _close(self):
        self._closing = True
        self.win.destroy()


class MemoryWindow:
    """Toplevel window that browses the memory database (the `messages`
    table Phoenix's chat history + recall embeddings live in) read-only."""

    def __init__(self, parent: tk.Tk):
        self._parent = parent
        self._closing = False

        self.win = tk.Toplevel(parent)
        self.win.title("Phoenix Backend — Memory")
        self.win.geometry("760x480")
        self.win.configure(bg=_BG)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        try:
            self.win.iconbitmap(str(_ICON_PATH))
        except tk.TclError:
            pass

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Memory.Treeview",
            background=_BG,
            fieldbackground=_BG,
            foreground=_FG,
            font=("Consolas", 9),
            rowheight=22,
            borderwidth=0,
        )
        style.configure(
            "Memory.Treeview.Heading",
            background=_DIM,
            foreground=_FG,
            font=("Consolas", 9, "bold"),
        )
        style.map("Memory.Treeview", background=[("selected", _DIM)])

        outer = tk.Frame(self.win, bg=_BG, padx=12, pady=12)
        outer.pack(fill="both", expand=True)

        tree_frame = tk.Frame(outer, bg=_BG)
        tree_frame.pack(fill="both", expand=True)

        scrollbar = tk.Scrollbar(tree_frame, bg=_BG)
        scrollbar.pack(side="right", fill="y")

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("id", "role", "ts", "content"),
            show="headings",
            style="Memory.Treeview",
            yscrollcommand=scrollbar.set,
        )
        self.tree.heading("id", text="ID")
        self.tree.heading("role", text="Role")
        self.tree.heading("ts", text="Timestamp")
        self.tree.heading("content", text="Content")
        self.tree.column("id", width=50, anchor="w", stretch=False)
        self.tree.column("role", width=80, anchor="w", stretch=False)
        self.tree.column("ts", width=180, anchor="w", stretch=False)
        self.tree.column("content", width=400, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.tree.yview)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail_frame = tk.Frame(outer, bg=_BG)
        detail_frame.pack(fill="both", pady=(10, 0))

        tk.Label(
            detail_frame,
            text="Full content:",
            font=("Consolas", 9),
            bg=_BG,
            fg=_DIM,
            anchor="w",
        ).pack(fill="x")

        self.detail_text = tk.Text(
            detail_frame,
            font=("Consolas", 9),
            bg=_BG,
            fg=_FG,
            insertbackground=_FG,
            relief="flat",
            wrap="word",
            height=6,
            state="disabled",
        )
        self.detail_text.pack(fill="both", expand=True)

        btn_row = tk.Frame(outer, bg=_BG)
        btn_row.pack(fill="x", pady=(10, 0))

        self.status_var = tk.StringVar(value="")
        tk.Label(
            btn_row,
            textvariable=self.status_var,
            font=("Consolas", 8),
            bg=_BG,
            fg=_DIM,
            anchor="w",
        ).pack(side="left")

        tk.Button(
            btn_row,
            text="[ Refresh ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._refresh,
        ).pack(side="right")

        self._rows_by_iid: dict[str, str] = {}
        self._refresh()

    def _fetch_rows(self):
        """Reads the most recent messages directly from Config.db_path.
        Read-only: doesn't create the DB if it's missing, since this
        window has nothing useful to write."""
        db_path = Path(Config.db_path)
        if not db_path.exists():
            return [], f"no database found at {db_path}"
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError as exc:
            return [], f"could not open database: {exc}"
        try:
            rows = conn.execute(
                "SELECT id, role, content, ts FROM messages "
                "ORDER BY id DESC LIMIT ?",
                (MEMORY_ROW_LIMIT,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return [], f"could not read messages table: {exc}"
        finally:
            conn.close()
        rows.reverse()
        return rows, None

    def _refresh(self):
        if self._closing:
            return
        selected_id = None
        sel = self.tree.selection()
        if sel:
            selected_id = self.tree.item(sel[0], "values")[0]

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._rows_by_iid.clear()

        rows, error = self._fetch_rows()
        if error:
            self.status_var.set(error)
        else:
            self.status_var.set(f"{len(rows)} message(s) (most recent {MEMORY_ROW_LIMIT})")

        for row_id, role, content, ts in rows:
            preview = (content or "").replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "…"
            iid = str(row_id)
            self.tree.insert("", "end", iid=iid, values=(row_id, role, ts, preview))
            self._rows_by_iid[iid] = content or ""
            if selected_id == str(row_id):
                self.tree.selection_set(iid)
                self.tree.see(iid)

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        content = self._rows_by_iid.get(sel[0], "")
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", content)
        self.detail_text.config(state="disabled")

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _close(self):
        self._closing = True
        self.win.destroy()


class TrayApp:
    def __init__(self, on_exit: Callable[[], None]):
        self._on_exit = on_exit
        self._closing = False
        self._blink_on = True  # Tracks the 1s blink state
        self._log_window: "LogWindow | None" = None
        self._config_window: "ConfigWindow | None" = None
        self._memory_window: "MemoryWindow | None" = None

        self.root = tk.Tk()
        self.root.title("Phoenix Backend")
        self.root.geometry("340x220")
        self.root.resizable(False, False)
        self.root.configure(bg=_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        try:
            self.root.iconbitmap(str(_ICON_PATH))
        except tk.TclError:
            pass

        self.headline_var = tk.StringVar(value="> status: INITIALIZING...")

        main_frame = tk.Frame(self.root, bg=_BG, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)

        # Terminal status headline
        tk.Label(
            main_frame,
            textvariable=self.headline_var,
            font=("Consolas", 11, "bold"),
            bg=_BG,
            fg=_GREEN,
            anchor="w",
        ).pack(fill="x", pady=(0, 15))

        # Rows container
        self._row_widgets = []
        rows_frame = tk.Frame(main_frame, bg=_BG)
        rows_frame.pack(fill="x", pady=(0, 15))

        for _ in range(3):
            row_f = tk.Frame(rows_frame, bg=_BG)
            row_f.pack(fill="x", pady=3)

            tag_lbl = tk.Label(row_f, text="[ .... ]", font=("Consolas", 10, "bold"), bg=_BG, fg=_DIM, width=8, anchor="w")
            tag_lbl.pack(side="left")

            name_lbl = tk.Label(row_f, text="", font=("Consolas", 10), bg=_BG, fg=_FG, anchor="w")
            name_lbl.pack(side="left", padx=(8, 0))

            self._row_widgets.append((tag_lbl, name_lbl))

        # Terminal style button row: Logs, Config, Memory (left), Hide (right)
        btn_row = tk.Frame(main_frame, bg=_BG)
        btn_row.pack(fill="x")

        logs_btn = tk.Button(
            btn_row,
            text="[ Logs ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_logs
        )
        logs_btn.pack(side="left")

        config_btn = tk.Button(
            btn_row,
            text="[ Config ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_config
        )
        config_btn.pack(side="left", padx=(10, 0))

        memory_btn = tk.Button(
            btn_row,
            text="[ Memory ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._open_memory
        )
        memory_btn.pack(side="left", padx=(10, 0))

        hide_btn = tk.Button(
            btn_row,
            text="[ Hide ]",
            font=("Consolas", 10),
            bg=_BG,
            fg=_FG,
            activebackground=_DIM,
            activeforeground=_FG,
            relief="flat",
            bd=0,
            cursor="hand2",
            command=self._hide_to_tray
        )
        hide_btn.pack(side="right")

        self.icon = self._build_tray_icon()
        self._icon_thread = threading.Thread(target=self.icon.run, daemon=True)

    def _build_tray_icon(self) -> pystray.Icon:
        try:
            image = Image.open(_ICON_PATH)
        except Exception:
            image = Image.new("RGB", (64, 64), color=(15, 17, 26))

        menu = pystray.Menu(
            pystray.MenuItem("Show window", self._show_window, default=True),
            pystray.MenuItem("Exit", self._request_exit),
        )
        return pystray.Icon("phoenix-backend", image, "Phoenix Backend", menu)

    def _show_window(self, icon=None, item=None):
        self.root.after(0, self._show_window_on_main_thread)

    def _show_window_on_main_thread(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self):
        self.root.withdraw()

    def _open_logs(self):
        if self._log_window is not None:
            self._log_window.show()
            return
        self._log_window = LogWindow(self.root)
        self._log_window.win.bind(
            "<Destroy>",
            lambda e: setattr(self, "_log_window", None) if e.widget is self._log_window.win else None,
        )

    def _open_config(self):
        if self._config_window is not None:
            self._config_window.show()
            return
        self._config_window = ConfigWindow(self.root)
        self._config_window.win.bind(
            "<Destroy>",
            lambda e: setattr(self, "_config_window", None) if e.widget is self._config_window.win else None,
        )

    def _open_memory(self):
        if self._memory_window is not None:
            self._memory_window.show()
            return
        self._memory_window = MemoryWindow(self.root)
        self._memory_window.win.bind(
            "<Destroy>",
            lambda e: setattr(self, "_memory_window", None) if e.widget is self._memory_window.win else None,
        )

    def _request_exit(self, icon=None, item=None):
        self.root.after(0, self._do_exit)

    def _do_exit(self):
        if self._closing:
            return
        self._closing = True
        self.headline_var.set("> status: EXITING...")
        self.root.update_idletasks()
        if self._log_window is not None:
            self._log_window._close()
            self._log_window = None
        if self._config_window is not None:
            self._config_window._close()
            self._config_window = None
        if self._memory_window is not None:
            self._memory_window._close()
            self._memory_window = None
        try:
            self._on_exit()
        finally:
            self.icon.stop()
            self.root.destroy()

    def _refresh(self):
        if self._closing:
            return
        
        # Toggle blink state every second
        self._blink_on = not self._blink_on

        headline, rows = _panel_data(self._blink_on)
        self.headline_var.set(f"> status: {headline}")
        
        for idx, (label_text, tag, color) in enumerate(rows):
            tag_lbl, name_lbl = self._row_widgets[idx]
            tag_lbl.config(text=tag, fg=color)
            name_lbl.config(text=label_text)

        self.icon.title = f"Phoenix Backend — {headline}"
        self.root.after(REFRESH_MS, self._refresh)

    def run(self):
        self._icon_thread.start()
        self._refresh()
        self.root.mainloop()


def run_tray_app(on_exit: Callable[[], None]) -> None:
    TrayApp(on_exit).run()