#!/usr/bin/env python3
"""
human_typer_gui.py - a small window for human_typer.py.

Keep this file in the same folder as human_typer.py, then run:
    python human_typer_gui.py

Paste or load your text, set the sliders, press Start, and click into your
Google Doc / form field during the countdown.
    F8  = pause / resume      Esc = stop
"""
import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from types import SimpleNamespace

from pynput import keyboard

import Typer as ht

DEFAULT_MODEL = "claude-sonnet-5-5"
APP_NAME = "Human Typer"
TAGLINE = "Types the way a person would \u2014 imperfectly, at a human pace."

# --- palette ---------------------------------------------------------------
PAGE = "#ffffff"       # everything content sits on
SOFT = "#f3f4f6"       # input fills, inactive hover
INK = "#111827"        # primary text
MUTED = "#6b7280"      # secondary text
ACCENT = "#4f46e5"     # indigo: header, sliders, primary action
ACCENT_HI = "#4338ca"  # hover / pressed
ACCENT_SOFT = "#c7d2fe"
HEADER_SUB = "#c7d2fe"
BORDER = "#e5e7eb"
TROUGH = "#e5e7eb"


def settings_path():
    """Per-user settings file; falls back to the home folder if needed."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    folder = os.path.join(base, "HumanTyper")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.path.expanduser("~")
    return os.path.join(folder, "settings.json")


def ui_family():
    if sys.platform == "darwin":
        return "Helvetica Neue"
    if sys.platform.startswith("win"):
        return "Segoe UI"
    return "DejaVu Sans"


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.minsize(600, 600)
        root.configure(bg=PAGE)

        self.q = queue.Queue()
        self.worker = None
        self.listener = None
        self.was_paused = False

        self.vars = {}            # slider key -> DoubleVar (used by run logic)
        self._persist = {}        # key -> tk Variable we save/restore
        self._ranges = {}         # slider key -> (lo, hi) for clamping on load
        self._defaults = {}       # key -> factory default
        self._settings_file = settings_path()

        self.tabs = {}            # name -> content frame
        self.tab_btns = {}        # name -> button

        self.fam = ui_family()
        self._build_fonts()
        self._build_style()

        self._build_header(root)
        self._build_actions(root)     # pinned bottom
        self._build_body(root)        # text + tabs in the middle

        for key in ("wpm", "pause"):
            self.vars[key].trace_add("write", lambda *_: self.update_estimate())

        self.load_settings()
        self.update_estimate()
        self.show_tab("Typing")

        root.update_idletasks()
        want = root.winfo_reqheight()
        screen = root.winfo_screenheight()
        root.geometry(f"660x{min(max(want, 640), screen - 100)}")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # --------------------------------------------------------------- styling --
    def _build_fonts(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=self.fam, size=12)
            except tk.TclError:
                pass
        self.f_h1 = (self.fam, 17, "bold")
        self.f_h2 = (self.fam, 13, "bold")
        self.f_small = (self.fam, 11)
        self.f_value = (self.fam, 12, "bold")

    def _build_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=PAGE, foreground=INK)
        s.configure("TFrame", background=PAGE)
        s.configure("TLabel", background=PAGE, foreground=INK)
        s.configure("Muted.TLabel", background=PAGE, foreground=MUTED, font=self.f_small)
        s.configure("H2.TLabel", background=PAGE, foreground=INK, font=self.f_h2)
        s.configure("Value.TLabel", background=PAGE, foreground=ACCENT, font=self.f_value)

        s.configure("TCheckbutton", background=PAGE, foreground=INK)
        s.map("TCheckbutton", background=[("active", PAGE)])

        s.configure("Horizontal.TScale", background=PAGE, troughcolor=TROUGH,
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, sliderlength=20)

        s.configure("TEntry", fieldbackground=SOFT, foreground=INK, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, insertcolor=INK, padding=7, relief="flat")

        s.configure("Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT,
                    bordercolor=TROUGH, lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        s.configure("TSeparator", background=BORDER)

        # secondary button
        s.configure("TButton", background=SOFT, foreground=INK, bordercolor=BORDER,
                    lightcolor=SOFT, darkcolor=SOFT, relief="flat", padding=(14, 9))
        s.map("TButton", background=[("active", "#e5e7eb"), ("disabled", "#f3f4f6")],
              foreground=[("disabled", MUTED)])
        # primary
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, relief="flat", padding=(24, 9))
        s.map("Accent.TButton", background=[("active", ACCENT_HI), ("disabled", ACCENT_SOFT)],
              foreground=[("disabled", "#eef2ff")])
        # ghost / link
        s.configure("Ghost.TButton", background=PAGE, foreground=ACCENT, bordercolor=PAGE,
                    lightcolor=PAGE, darkcolor=PAGE, relief="flat", padding=(10, 6))
        s.map("Ghost.TButton", foreground=[("active", ACCENT_HI)], background=[("active", PAGE)])
        # tab pills
        s.configure("Tab.TButton", background=PAGE, foreground=MUTED, bordercolor=PAGE,
                    lightcolor=PAGE, darkcolor=PAGE, relief="flat", padding=(18, 9))
        s.map("Tab.TButton", background=[("active", SOFT)], foreground=[("active", INK)])
        s.configure("TabOn.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, relief="flat", padding=(18, 9))
        s.map("TabOn.TButton", background=[("active", ACCENT_HI)], foreground=[("active", "#ffffff")])

    # ---------------------------------------------------------------- layout --
    def _build_header(self, root):
        band = tk.Frame(root, bg=ACCENT)
        band.pack(side="top", fill="x")
        row = tk.Frame(band, bg=ACCENT)
        row.pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(row, text=APP_NAME, bg=ACCENT, fg="#ffffff", font=self.f_h1).pack(side="left")
        self.estimate = tk.Label(row, text="", bg=ACCENT, fg=HEADER_SUB, font=self.f_small)
        self.estimate.pack(side="right", pady=(6, 0))
        tk.Label(band, text=TAGLINE, bg=ACCENT, fg=HEADER_SUB,
                 font=self.f_small).pack(anchor="w", padx=20, pady=(0, 16))

    def _build_actions(self, root):
        ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")
        bottom = ttk.Frame(root, padding=(20, 12, 20, 14))
        bottom.pack(side="bottom", fill="x")

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(bar, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")

        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(fill="x", pady=(12, 6))

        line = ttk.Frame(bottom)
        line.pack(fill="x")
        self.status = ttk.Label(line, text="Ready.", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Label(line, text="Settings saved automatically", style="Muted.TLabel").pack(side="right")

    def _build_body(self, root):
        body = ttk.Frame(root, padding=(20, 16, 20, 8))
        body.pack(side="top", fill="both", expand=True)

        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="Text to type", style="H2.TLabel").pack(side="left")
        ttk.Button(head, text="Load file\u2026", command=self.load_file).pack(side="right")
        ttk.Button(head, text="Clear", command=lambda: self.text.delete("1.0", "end")).pack(side="right", padx=6)

        tbox = ttk.Frame(body)
        tbox.pack(fill="x", pady=(10, 0))
        tscroll = ttk.Scrollbar(tbox, orient="vertical")
        self.text = tk.Text(
            tbox, height=6, wrap="word", undo=True, padx=12, pady=10,
            relief="flat", borderwidth=0, bg=SOFT, fg=INK, insertbackground=ACCENT,
            selectbackground="#dbe3ff", selectforeground=INK,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
            yscrollcommand=tscroll.set,
        )
        tscroll.config(command=self.text.yview)
        tscroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<<Modified>>", self.on_text_modified)

        # tab bar (pills) + swappable panel below ----------------------------
        self.tabbar = ttk.Frame(body)
        self.tabbar.pack(fill="x", pady=(18, 0))
        self.tabwrap = ttk.Frame(body)
        self.tabwrap.pack(fill="both", expand=True, pady=(12, 0))

        self._tab_typing(self._new_tab("Typing"))
        self._tab_realism(self._new_tab("Realism"))
        self._tab_ai(self._new_tab("AI"))
        self._tab_general(self._new_tab("General"))

    def _new_tab(self, name):
        frame = ttk.Frame(self.tabwrap)
        self.tabs[name] = frame
        btn = ttk.Button(self.tabbar, text=name, style="Tab.TButton",
                         command=lambda n=name: self.show_tab(n))
        btn.pack(side="left", padx=(0, 6))
        self.tab_btns[name] = btn
        return frame

    def show_tab(self, name):
        for f in self.tabs.values():
            f.pack_forget()
        self.tabs[name].pack(fill="both", expand=True)
        for n, b in self.tab_btns.items():
            b.configure(style="TabOn.TButton" if n == name else "Tab.TButton")

    def _tab_typing(self, t):
        self.slider(t, "wpm", "Speed (WPM)", 15, 140, 55, "{:.0f}")
        self.slider(t, "typo", "Typos (% of letters)", 0, 10, 3, "{:.1f}%")
        self.slider(t, "revise", "Retype hesitations (% of words)", 0, 6, 1.5, "{:.1f}%")
        self.slider(t, "think", "Thinking pauses (% of spaces)", 0, 15, 3, "{:.1f}%")
        self.slider(t, "pause", "Pause length", 0.25, 3, 1.0, "{:.2f}x")
        self.slider(t, "countdown", "Countdown", 2, 15, 5, "{:.0f}s")

    def _tab_realism(self, t):
        self.slider(t, "dwell_min", "Key hold min (ms)", 30, 120, 70, "{:.0f} ms")
        self.slider(t, "dwell_max", "Key hold max (ms)", 60, 260, 150, "{:.0f} ms")
        self.slider(t, "rollover_rate", "Overlap common pairs (%)", 0, 100, 0, "{:.0f}%")

        r = t.grid_size()[1]
        self.rollover = tk.BooleanVar(value=False)
        self._track("rollover", self.rollover, False)
        ttk.Checkbutton(
            t, variable=self.rollover,
            text="Allow key overlap so speeds above ~100 WPM are actually reached",
        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))

        ttk.Label(t, text="Seed").grid(row=r + 1, column=0, sticky="w", pady=6)
        self.seed = tk.StringVar(value="")
        self._track("seed", self.seed, "")
        ttk.Entry(t, textvariable=self.seed, width=14).grid(row=r + 1, column=1, sticky="w", padx=12)
        ttk.Label(t, text="blank = random each run", style="Muted.TLabel").grid(
            row=r + 1, column=2, sticky="w")

    def _tab_ai(self, t):
        self.ai_on = tk.BooleanVar(value=False)
        self._track("ai_on", self.ai_on, False)
        ttk.Checkbutton(
            t, variable=self.ai_on,
            text="Use Claude to type, then delete, realistic alternate sentence openings",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.slider(t, "ai_rate", "Sentences affected", 0, 100, 25, "{:.0f}%")

        r = t.grid_size()[1]
        ttk.Label(t, text="API key").grid(row=r, column=0, sticky="w", pady=6)
        # The API key is read from ANTHROPIC_API_KEY and is deliberately NOT
        # written to the settings file (no plaintext secrets on disk).
        self.api_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        ttk.Entry(t, textvariable=self.api_key, show="\u2022").grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=12)
        ttk.Label(t, text="Model").grid(row=r + 1, column=0, sticky="w", pady=6)
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self._track("model", self.model, DEFAULT_MODEL)
        ttk.Entry(t, textvariable=self.model).grid(row=r + 1, column=1, columnspan=2, sticky="ew", padx=12)
        t.columnconfigure(1, weight=1)

    def _tab_general(self, t):
        self.minimize = tk.BooleanVar(value=True)
        self._track("minimize", self.minimize, True)
        ttk.Checkbutton(t, variable=self.minimize,
                        text="Minimize this window when the countdown starts").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=4)
        self.hotkeys = tk.BooleanVar(value=True)
        self._track("hotkeys", self.hotkeys, True)
        ttk.Checkbutton(t, variable=self.hotkeys,
                        text="Enable F8 (pause) / Esc (stop) hotkeys").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Separator(t, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Button(t, text="Reset all settings to defaults", style="Ghost.TButton",
                   command=self.reset_defaults).grid(row=3, column=0, sticky="w")

    # --------------------------------------------------------------- helpers --
    def _track(self, key, var, default):
        self._persist[key] = var
        self._defaults[key] = default

    def slider(self, parent, key, label, lo, hi, default, fmt):
        r = parent.grid_size()[1]
        var = tk.DoubleVar(value=default)
        value_lbl = ttk.Label(parent, width=9, anchor="e", style="Value.TLabel")

        def refresh(*_):
            value_lbl.config(text=fmt.format(var.get()))

        var.trace_add("write", refresh)  # fires on drags AND programmatic loads
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=7)
        ttk.Scale(parent, from_=lo, to=hi, variable=var).grid(row=r, column=1, sticky="ew", padx=12)
        value_lbl.grid(row=r, column=2, sticky="e")
        parent.columnconfigure(1, weight=1)
        refresh()

        self.vars[key] = var
        self._persist[key] = var
        self._ranges[key] = (lo, hi)
        self._defaults[key] = default

    def get_text(self):
        return self.text.get("1.0", "end-1c").replace("\r\n", "\n").rstrip("\n")

    def update_estimate(self):
        n = len(self.get_text())
        if not n:
            self.estimate.config(text="")
            return
        wpm = max(self.vars["wpm"].get(), 1)
        mins = n / (wpm * 5) * 1.3 * self.vars["pause"].get()
        self.estimate.config(text=f"{n:,} chars \u00b7 ~{mins:.1f} min")

    def on_text_modified(self, _event):
        if self.text.edit_modified():
            self.update_estimate()
            self.text.edit_modified(False)

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt *.md"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Couldn't read file:\n{e}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.pause_btn.config(state="normal" if running else "disabled", text="Pause")
        self.stop_btn.config(state="normal" if running else "disabled")

    def read_seed(self):
        txt = self.seed.get().strip()
        if not txt:
            return None
        try:
            return int(txt)
        except ValueError:
            return None

    # ------------------------------------------------------- settings file ---
    def collect_settings(self):
        out = {}
        for key, var in self._persist.items():
            try:
                out[key] = var.get()
            except tk.TclError:
                pass
        return out

    def apply_settings(self, data):
        for key, var in self._persist.items():
            if key not in data:
                continue
            val = data[key]
            try:
                if key in self._ranges:
                    lo, hi = self._ranges[key]
                    var.set(min(max(float(val), lo), hi))
                elif isinstance(var, tk.BooleanVar):
                    var.set(bool(val))
                else:
                    var.set("" if val is None else str(val))
            except (tk.TclError, ValueError, TypeError):
                pass

    def load_settings(self):
        try:
            with open(self._settings_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.apply_settings(data)
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            with open(self._settings_file, "w", encoding="utf-8") as f:
                json.dump(self.collect_settings(), f, indent=2)
        except OSError:
            pass

    def reset_defaults(self):
        self.apply_settings(self._defaults)
        self.update_estimate()

    # --------------------------------------------------------------- actions --
    def start(self):
        text = self.get_text()
        if not text.strip():
            messagebox.showinfo(APP_NAME, "Paste or load some text first.")
            return

        v = {k: var.get() for k, var in self.vars.items()}
        dwell_min = min(v["dwell_min"], v["dwell_max"])
        dwell_max = max(v["dwell_min"], v["dwell_max"])

        args = SimpleNamespace(
            wpm=max(v["wpm"], 1),
            typo_rate=v["typo"] / 100,
            revise_rate=v["revise"] / 100,
            think_rate=v["think"] / 100,
            pause_scale=v["pause"],
            dwell_min=dwell_min,
            dwell_max=dwell_max,
            rollover=self.rollover.get(),
            rollover_rate=v["rollover_rate"] / 100,
            seed=self.read_seed(),
        )
        countdown = int(round(v["countdown"]))
        use_ai = self.ai_on.get()
        model = self.model.get().strip() or DEFAULT_MODEL

        if use_ai:
            key = self.api_key.get().strip()
            if not key:
                messagebox.showwarning(APP_NAME, "AI false starts need an Anthropic API key.")
                return
            os.environ["ANTHROPIC_API_KEY"] = key

        self.save_settings()

        ht.stop_event.clear()
        ht.pause_event.clear()
        self.was_paused = False
        self.progress["value"] = 0

        if self.hotkeys.get():
            self.listener = keyboard.Listener(on_press=ht.on_press)
            self.listener.start()

        self.set_running(True)
        self.worker = threading.Thread(
            target=self.work, args=(text, args, countdown, use_ai, v["ai_rate"] / 100, model), daemon=True)
        self.worker.start()

    def work(self, text, args, countdown, use_ai, ai_rate, model):
        try:
            alts = {}
            if use_ai:
                self.q.put(("status", "Asking Claude for alternate openings\u2026"))
                alts = ht.get_ai_alternates(text, ai_rate, model)
                self.q.put(("status", f"Got {len(alts)} alternate openings."))

            self.q.put(("minimize",))
            for s in range(countdown, 0, -1):
                self.q.put(("status", f"Click into your text box\u2026 starting in {s}"))
                ht.wait(1)

            self.q.put(("status", "Typing\u2026"))
            ht.Typer(args).run(text, alts, on_progress=lambda i, n: self.q.put(("progress", i * 100 / n)))
            self.q.put(("done", "Done."))
        except ht.Stopped:
            self.q.put(("done", "Stopped."))
        except SystemExit as e:
            self.q.put(("error", str(e)))
        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def toggle_pause(self):
        if ht.pause_event.is_set():
            ht.pause_event.clear()
        else:
            ht.pause_event.set()

    def stop(self):
        ht.stop_event.set()
        ht.pause_event.clear()

    def finish(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.set_running(False)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status.config(text=msg[1])
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                elif kind == "minimize":
                    if self.minimize.get():
                        self.root.iconify()
                elif kind == "done":
                    self.finish()
                    self.status.config(text=msg[1])
                elif kind == "error":
                    self.finish()
                    self.status.config(text="Error.")
                    self.root.deiconify()
                    messagebox.showerror(APP_NAME, msg[1])
        except queue.Empty:
            pass

        if self.worker and self.worker.is_alive():
            paused = ht.pause_event.is_set()
            self.pause_btn.config(text="Resume" if paused else "Pause")
            if paused and not self.was_paused:
                self.status.config(text="Paused. Press F8 or Resume to continue.")
            elif not paused and self.was_paused:
                self.status.config(text="Resuming in 2s \u2014 click back into your text box.")
            self.was_paused = paused

        self.root.after(100, self.poll)

    def on_close(self):
        self.save_settings()
        ht.stop_event.set()
        ht.pause_event.clear()
        if self.listener:
            self.listener.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()