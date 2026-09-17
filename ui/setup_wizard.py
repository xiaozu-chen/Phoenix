"""
ui/setup_wizard.py

The very first thing a fresh install sees, instead of a tray icon and a
Config window full of settings with no context. Lives entirely in this
Tkinter window -- never the web frontend, since that's reachable from a
phone or another machine on the network and isn't the right place for
local machine setup (model paths on disk, etc).

Deliberately narrow in scope: persona notes and importing one LLM
model, i.e. exactly what's needed to have a working first chat. Voice
model paths, extensions, and everything else in settings/config.py stay
on their defaults and are one Config-window edit away later -- see
ConfigWindow's General tab in ui/tray.py, which edits the same persona
setting and manages the same model registry (settings/model_registry.py)
after the fact.

Standalone by design: this module only imports tkinter and
settings.user_settings, not ui.tray (which pulls in pystray/PIL), so it
runs fine even before we know those are installed, and even in a
headless dev checkout that never used the tray at all.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog

from settings.model_registry import ModelError, add_and_activate, get_active_model
from settings.user_settings import (
    load_user_settings,
    mark_first_run_complete,
    save_user_settings,
)

_BG = "#0f111a"
_FG = "#c0caf5"
_GREEN = "#9ece6a"
_YELLOW = "#e0af68"
_DIM = "#565f89"

_ICON_PATH = Path(__file__).resolve().parent.parent / "app.ico"

_INTRO = (
    "First time running Phoenix -- two quick things before the tray "
    "starts up. Everything here can be changed later from the tray's "
    "Configuration window, so don't overthink it."
)


def run_setup_wizard() -> None:
    """Blocks until the user hits Save & Continue or Skip. Either way,
    marks first-run complete so this never shows again unprompted."""
    current = load_user_settings()

    root = tk.Tk()
    root.title("Phoenix — First-time Setup")
    root.geometry("560x600")
    root.minsize(520, 480)
    root.configure(bg=_BG)
    root.resizable(True, True)
    try:
        root.iconbitmap(str(_ICON_PATH))
    except tk.TclError:
        pass

    # Bottom bar (status + Skip/Save buttons) is packed against root
    # FIRST, before the scrollable content above it. Pack allocates
    # space in call order regardless of `side`, so packing this first
    # reserves its space at the bottom no matter how tall the content
    # above ends up being -- the alternative (what this replaced) let
    # the content silently push the buttons off the bottom of a
    # fixed-size, non-resizable window with no way to reach them.
    bottom_bar = tk.Frame(root, bg=_BG, padx=20)
    bottom_bar.pack(side="bottom", fill="x", pady=(0, 16))

    status_var = tk.StringVar(value="")
    tk.Label(
        bottom_bar, textvariable=status_var, font=("Consolas", 8), bg=_BG, fg=_YELLOW, anchor="w",
    ).pack(fill="x", pady=(0, 8))

    btn_row = tk.Frame(bottom_bar, bg=_BG)
    btn_row.pack(fill="x")

    def _finish():
        mark_first_run_complete()
        root.destroy()

    def _skip():
        status_var.set("Skipped -- using defaults, edit anytime from Configuration.")
        root.after(700, _finish)

    def _save_and_continue():
        save_user_settings(persona=persona_text.get("1.0", "end-1c").strip())
        _finish()

    tk.Button(
        btn_row, text="[ Skip for now ]", font=("Consolas", 10), bg=_BG, fg=_DIM,
        activebackground=_DIM, activeforeground=_FG, relief="flat", bd=0,
        cursor="hand2", command=_skip,
    ).pack(side="left")

    tk.Button(
        btn_row, text="[ Save & Continue ]", font=("Consolas", 10, "bold"), bg=_BG, fg=_GREEN,
        activebackground=_DIM, activeforeground=_GREEN, relief="flat", bd=0,
        cursor="hand2", command=_save_and_continue,
    ).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", _skip)

    # Everything above the bottom bar lives in a scrollable canvas, so
    # if content ever grows past whatever room is left (a smaller
    # display, larger system font, more fields added later), it
    # scrolls instead of silently clipping like the fixed-height
    # version did.
    canvas_wrap = tk.Frame(root, bg=_BG)
    canvas_wrap.pack(side="top", fill="both", expand=True)

    canvas = tk.Canvas(canvas_wrap, bg=_BG, highlightthickness=0)
    scrollbar = tk.Scrollbar(canvas_wrap, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    outer = tk.Frame(canvas, bg=_BG, padx=20, pady=20)
    outer_window = canvas.create_window((0, 0), window=outer, anchor="nw")

    def _on_outer_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event):
        canvas.itemconfig(outer_window, width=event.width)

    outer.bind("<Configure>", _on_outer_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)

    tk.Label(
        outer, text="Welcome", font=("Consolas", 16, "bold"), bg=_BG, fg=_GREEN, anchor="w"
    ).pack(fill="x")
    tk.Label(
        outer, text=_INTRO, font=("Consolas", 9), bg=_BG, fg=_DIM,
        anchor="w", justify="left", wraplength=470,
    ).pack(fill="x", pady=(4, 18))

    # -- persona name -------------------------------------------------

    tk.Label(
        outer, text="Persona notes", font=("Consolas", 10, "bold"), bg=_BG, fg=_FG, anchor="w"
    ).pack(fill="x")
    tk.Label(
        outer,
        text="Optional. Anything appended to the system prompt as extra\n"
             "notes -- a name, a detail to remember, a quirk. Leave blank\n"
             "to just use the default personality as-is.",
        font=("Consolas", 8), bg=_BG, fg=_DIM, justify="left", anchor="w",
    ).pack(fill="x", pady=(2, 6))

    persona_text = tk.Text(
        outer, font=("Consolas", 10), bg=_BG, fg=_FG, insertbackground=_FG,
        relief="flat", height=4, wrap="word",
    )
    persona_text.pack(fill="x")
    persona_text.insert("1.0", current["persona"])

    # -- LLM model -------------------------------------------------

    tk.Label(
        outer, text="LLM model", font=("Consolas", 10, "bold"), bg=_BG, fg=_FG, anchor="w"
    ).pack(fill="x", pady=(18, 0))
    tk.Label(
        outer,
        text="Import a local .gguf model for llama-server -- pick one you've\n"
             "already downloaded. Required for chat to work, but you can skip\n"
             "this and import one later from Configuration if you don't have\n"
             "one yet.",
        font=("Consolas", 8), bg=_BG, fg=_DIM, justify="left", anchor="w",
    ).pack(fill="x", pady=(2, 6))

    active = get_active_model()
    active_model_var = tk.StringVar(
        value=(f"Active model: {active['name']}" if active else "No model imported yet.")
    )
    tk.Label(
        outer, textvariable=active_model_var, font=("Consolas", 9), bg=_BG, fg=_GREEN, anchor="w",
    ).pack(fill="x")

    def _import_model():
        chosen = filedialog.askopenfilename(
            title="Select a .gguf model file",
            filetypes=[("GGUF model", "*.gguf"), ("All files", "*.*")],
        )
        if not chosen:
            return
        default_name = Path(chosen).stem
        name = simpledialog.askstring(
            "Name this model", "A short name to show in the model list:",
            initialvalue=default_name, parent=root,
        )
        if name is None:  # user cancelled the name prompt
            return
        try:
            model = add_and_activate(chosen, name)
        except ModelError as exc:
            status_var.set(str(exc))
            return
        active_model_var.set(f"Active model: {model['name']}")
        status_var.set(f"Imported and set active: {model['path']}")

    tk.Button(
        outer, text="[ Import model... ]", font=("Consolas", 10), bg=_BG, fg=_FG,
        activebackground=_DIM, activeforeground=_FG, relief="flat", bd=0,
        cursor="hand2", command=_import_model,
    ).pack(anchor="w", pady=(6, 0))

    root.mainloop()
