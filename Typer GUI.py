#!/usr/bin/env python3
"""
human_typer_gui.py - a small window for human_typer.py.

Keep this file in the same folder as human_typer.py, then run:
    python human_typer_gui.py

Paste or load your text, set the sliders, press Start, and click into your
Google Doc / form field during the countdown.
    F8  = pause / resume      Esc = stop
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from types import SimpleNamespace

from pynput import keyboard

import Typer as ht

DEFAULT_MODEL = "claude-sonnet-5-5"


class ScrollFrame(ttk.Frame):
    """A vertically scrollable container. Put content in `self.body`."""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        # keep the scroll region in sync with the body's size
        self.body.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        # make the body as wide as the canvas (so fill="x" works, no sideways scroll)
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win, width=e.width),
        )
        # only grab the wheel while the pointer is over this area
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)   # Windows / macOS
        self.canvas.bind_all("<Button-4>", self._on_wheel)     # Linux scroll up
        self.canvas.bind_all("<Button-5>", self._on_wheel)     # Linux scroll down

    def _unbind_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, e):
        if getattr(e, "num", None) == 4:
            step = -1
        elif getattr(e, "num", None) == 5:
            step = 1
        elif abs(e.delta) >= 120:      # Windows reports multiples of 120
            step = int(-e.delta / 120)
        else:                          # macOS reports small deltas
            step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


class App:
    def __init__(self, root):
        self.root = root
        root.title("Human Typer")
        root.minsize(560, 460)

        self.q = queue.Queue()
        self.worker = None
        self.listener = None
        self.was_paused = False
        self.vars = {}

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True)

        # --- bottom action area (pinned; always visible) ----------------
        bottom = ttk.Frame(main, padding=(12, 8, 12, 10))
        bottom.pack(side="bottom", fill="x")
        ttk.Separator(main, orient="horizontal").pack(side="bottom", fill="x")

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.pause_btn = ttk.Button(bar, text="Pause", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.estimate = ttk.Label(bar, text="")
        self.estimate.pack(side="right")

        self.progress = ttk.Progressbar(bottom, maximum=100)
        self.progress.pack(fill="x", pady=(8, 2))
        self.status = ttk.Label(bottom, text="Ready.")
        self.status.pack(anchor="w")

        # --- text area (top; fixed height, own scrollbar) ---------------
        textwrap = ttk.Frame(main, padding=(12, 12, 12, 6))
        textwrap.pack(side="top", fill="x")

        hdr = ttk.Frame(textwrap)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Text to type", font=("", 11, "bold")).pack(side="left")
        ttk.Button(hdr, text="Load file…", command=self.load_file).pack(side="right")
        ttk.Button(hdr, text="Clear", command=lambda: self.text.delete("1.0", "end")).pack(side="right", padx=4)

        tbox = ttk.Frame(textwrap)
        tbox.pack(fill="x", pady=(4, 0))
        tscroll = ttk.Scrollbar(tbox, orient="vertical")
        self.text = tk.Text(tbox, height=7, wrap="word", undo=True, padx=6, pady=6,
                            yscrollcommand=tscroll.set)
        tscroll.config(command=self.text.yview)
        tscroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<<Modified>>", self.on_text_modified)

        # --- scrollable settings ----------------------------------------
        scroller = ScrollFrame(main)
        scroller.pack(side="top", fill="both", expand=True)
        body = scroller.body

        typing = ttk.LabelFrame(body, text="Typing", padding=8)
        typing.pack(fill="x", padx=12, pady=(8, 0))
        self.slider(typing, "wpm", "Speed (WPM)", 15, 140, 55, "{:.0f}")
        self.slider(typing, "typo", "Typos (% of letters)", 0, 10, 3, "{:.1f}%")
        self.slider(typing, "revise", "Retype hesitations (% of words)", 0, 6, 1.5, "{:.1f}%")
        self.slider(typing, "think", "Thinking pauses (% of spaces)", 0, 15, 3, "{:.1f}%")
        self.slider(typing, "pause", "Pause length", 0.25, 3, 1.0, "{:.2f}x")
        self.slider(typing, "countdown", "Countdown", 2, 15, 5, "{:.0f}s")

        realism = ttk.LabelFrame(body, text="Realism", padding=8)
        realism.pack(fill="x", padx=12, pady=8)
        self.slider(realism, "dwell_min", "Key hold min (ms)", 30, 120, 70, "{:.0f} ms")
        self.slider(realism, "dwell_max", "Key hold max (ms)", 60, 260, 150, "{:.0f} ms")
        self.slider(realism, "rollover_rate", "Overlap common pairs (%)", 0, 100, 0, "{:.0f}%")

        rr = realism.grid_size()[1]
        self.rollover = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            realism, variable=self.rollover,
            text="Allow key overlap so speeds above ~100 WPM are actually reached",
        ).grid(row=rr, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(realism, text="Seed").grid(row=rr + 1, column=0, sticky="w", pady=2)
        self.seed = tk.StringVar(value="")
        ttk.Entry(realism, textvariable=self.seed, width=12).grid(row=rr + 1, column=1, sticky="w", padx=8)
        ttk.Label(realism, text="blank = random each run").grid(row=rr + 1, column=2, sticky="w")

        ai = ttk.LabelFrame(body, text="AI false starts (optional)", padding=8)
        ai.pack(fill="x", padx=12, pady=(0, 8))
        self.ai_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            ai, variable=self.ai_on,
            text="Use Claude to type, then delete, realistic alternate sentence openings",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.slider(ai, "ai_rate", "Sentences affected", 0, 100, 25, "{:.0f}%")

        r = ai.grid_size()[1]
        ttk.Label(ai, text="API key").grid(row=r, column=0, sticky="w", pady=2)
        self.api_key = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        ttk.Entry(ai, textvariable=self.api_key, show="\u2022").grid(row=r, column=1, columnspan=2, sticky="ew", padx=8)
        ttk.Label(ai, text="Model").grid(row=r + 1, column=0, sticky="w", pady=2)
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        ttk.Entry(ai, textvariable=self.model).grid(row=r + 1, column=1, columnspan=2, sticky="ew", padx=8)

        options = ttk.Frame(body)
        options.pack(fill="x", padx=12, pady=(0, 10))
        self.minimize = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, variable=self.minimize,
                        text="Minimize this window when the countdown starts").pack(anchor="w")
        self.hotkeys = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, variable=self.hotkeys,
                        text="Enable F8 (pause) / Esc (stop) hotkeys").pack(anchor="w")

        for key in ("wpm", "pause"):
            self.vars[key].trace_add("write", lambda *_: self.update_estimate())
        self.update_estimate()

        # open at a height that fits the screen; the settings area scrolls if needed
        root.update_idletasks()
        want = root.winfo_reqheight()
        screen = root.winfo_screenheight()
        height = min(want, screen - 120)
        root.geometry(f"600x{height}")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    # ------------------------------------------------------------ helpers --
    def slider(self, parent, key, label, lo, hi, default, fmt):
        r = parent.grid_size()[1]
        var = tk.DoubleVar(value=default)
        value_lbl = ttk.Label(parent, width=7, anchor="e")

        def refresh(*_):
            value_lbl.config(text=fmt.format(var.get()))

        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Scale(parent, from_=lo, to=hi, variable=var, command=refresh).grid(
            row=r, column=1, sticky="ew", padx=8)
        value_lbl.grid(row=r, column=2, sticky="e")
        parent.columnconfigure(1, weight=1)
        refresh()
        self.vars[key] = var

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
            messagebox.showerror("Human Typer", f"Couldn't read file:\n{e}")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.pause_btn.config(state="normal" if running else "disabled", text="Pause")
        self.stop_btn.config(state="normal" if running else "disabled")

    def read_seed(self):
        """Parse the seed box: an integer, or None when blank/invalid."""
        txt = self.seed.get().strip()
        if not txt:
            return None
        try:
            return int(txt)
        except ValueError:
            return None

    # ------------------------------------------------------------ actions --
    def start(self):
        text = self.get_text()
        if not text.strip():
            messagebox.showinfo("Human Typer", "Paste or load some text first.")
            return

        v = {k: var.get() for k, var in self.vars.items()}
        # the two dwell sliders are independent, so make sure min <= max
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
                messagebox.showwarning("Human Typer", "AI false starts need an Anthropic API key.")
                return
            os.environ["ANTHROPIC_API_KEY"] = key

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
        except SystemExit as e:  # e.g. anthropic package missing
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
                    messagebox.showerror("Human Typer", msg[1])
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
        ht.stop_event.set()
        ht.pause_event.clear()
        if self.listener:
            self.listener.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()