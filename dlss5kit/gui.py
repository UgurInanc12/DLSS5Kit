"""Tkinter window: pick a folder, see what it found, press Install.

Deliberately flat and matte: a dark panel, one accent, no gradients and no
animation. Everything the tool decided is on screen with its reason, because
a route chosen without a stated reason is a route you cannot argue with.

Long work runs on a worker thread; the UI is only ever touched from the main
thread through a queue drained by `after`.
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, cli, config, diagnose, gpu, installer, peinfo, routes

BG = "#1e2124"
PANEL = "#282b30"
PANEL2 = "#36393e"
FG = "#dcddde"
MUTED = "#8e9297"
ACCENT = "#c8cdd3"
OK = "#5b9e6b"
WARN = "#b58b4a"
BAD = "#b5544a"

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI Semibold", 10)
FONT_H = ("Segoe UI Semibold", 15)
FONT_S = ("Segoe UI", 8)
FONT_M = ("Consolas", 9)

AUTO = "Auto (detect)"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        self.game_dir: Path | None = None
        self.exe: Path | None = None
        self.cands: list[Path] = []
        self.api: peinfo.ApiInfo | None = None
        self.bits = 0
        self.plan: routes.Plan | None = None
        self.detected = gpu.detect_card()

        root.title(f"DLSS5Kit {__version__}")
        root.configure(bg=BG)
        root.geometry("1000x800")
        root.minsize(880, 680)

        self._style()
        self._build()
        self.root.after(80, self._drain)

    # ------------------------------------------------------------ chrome

    def _style(self) -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2,
                    bordercolor=PANEL2, lightcolor=ACCENT, darkcolor=ACCENT)
        # clam draws the combobox entry from the widget's own options, not
        # from the style map, so without these the field renders as the
        # default light grey and light text on it is unreadable.
        s.configure("Dark.TCombobox",
                    fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG, arrowcolor=FG,
                    bordercolor=PANEL2, lightcolor=PANEL2, darkcolor=PANEL2,
                    selectbackground=PANEL2, selectforeground=FG,
                    insertcolor=FG, padding=4)
        s.map("Dark.TCombobox",
              fieldbackground=[("readonly", PANEL2), ("disabled", PANEL),
                               ("!disabled", PANEL2)],
              foreground=[("readonly", FG), ("disabled", MUTED),
                          ("!disabled", FG)],
              background=[("readonly", PANEL2), ("active", PANEL2)],
              arrowcolor=[("disabled", MUTED), ("!disabled", FG)],
              selectbackground=[("readonly", PANEL2)],
              selectforeground=[("readonly", FG)])
        # The dropdown list itself is a Tk listbox owned by the option
        # database, not by ttk, so it has to be coloured separately.
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)
        self.root.option_add("*TCombobox*Listbox.font", FONT)

    def _label(self, parent, text, font=FONT, fg=FG, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg,
                        bg=kw.pop("bg", parent["bg"]), **kw)

    def _button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, font=FONT_B,
                         bg=ACCENT if primary else PANEL2,
                         fg="#1e2124" if primary else FG,
                         activebackground="#e2e6ea" if primary else "#41454c",
                         activeforeground="#1e2124" if primary else FG,
                         relief="flat", bd=0, padx=18, pady=8,
                         cursor="hand2", disabledforeground=MUTED)

    def _build(self) -> None:
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 8))
        self._label(head, "DLSS5Kit", font=FONT_H).pack(side="left")
        self._label(head, "  DLSS 5 neural rendering setup", fg=MUTED).pack(
            side="left", pady=(4, 0))
        self.gpu_label = self._label(head, self.detected.describe(), fg=MUTED)
        self.gpu_label.pack(side="right", pady=(4, 0))

        # --- folder row ---
        row = tk.Frame(self.root, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(4, 10))
        inner = tk.Frame(row, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=12)
        self.path_var = tk.StringVar(value="")
        e = tk.Entry(inner, textvariable=self.path_var, font=FONT, bg=PANEL2,
                     fg=FG, insertbackground=FG, relief="flat", bd=0)
        e.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        e.bind("<Return>", lambda _: self.inspect())
        self._button(inner, "Browse", self.browse).pack(side="left", padx=(0, 6))
        self._button(inner, "Inspect", self.inspect, primary=True).pack(side="left")

        # --- verdict panel ---
        self.verdict = tk.Frame(self.root, bg=PANEL)
        self.verdict.pack(fill="x", padx=18, pady=(0, 10))
        self.v_inner = tk.Frame(self.verdict, bg=PANEL)
        self.v_inner.pack(fill="x", padx=14, pady=12)
        self.v_title = self._label(self.v_inner, "Choose a game folder to begin.",
                                   font=FONT_B)
        self.v_title.pack(anchor="w")
        self.v_body = self._label(self.v_inner, "", fg=MUTED, wraplength=900,
                                  justify="left")
        self.v_body.pack(anchor="w", pady=(4, 0))
        self.v_note = self._label(self.v_inner, "", fg=WARN, wraplength=900,
                                  justify="left")
        self.facts = tk.Frame(self.v_inner, bg=PANEL)
        self.facts.pack(fill="x", pady=(10, 0))

        # --- controls: graphics card ---
        gpu_row = tk.Frame(self.root, bg=BG)
        gpu_row.pack(fill="x", padx=18, pady=(0, 6))
        self._label(gpu_row, "Graphics card", fg=MUTED).pack(side="left",
                                                             padx=(0, 6))
        self.gen_var = tk.StringVar(value=AUTO)
        self.gen_box = ttk.Combobox(
            gpu_row, textvariable=self.gen_var, state="readonly", width=16,
            font=FONT, style="Dark.TCombobox",
            values=[AUTO] + list(gpu.GENERATIONS))
        self.gen_box.pack(side="left", padx=(0, 10))
        self.gen_box.bind("<<ComboboxSelected>>", lambda _: self._on_gen())
        self.gen_note = self._label(gpu_row, "", fg=MUTED, font=FONT_S)
        self.gen_note.pack(side="left")
        self._button(gpu_row, "Build support...", self.show_matrix).pack(
            side="right")

        # --- controls: route and tuning ---
        ctl = tk.Frame(self.root, bg=BG)
        ctl.pack(fill="x", padx=18, pady=(0, 8))
        self._label(ctl, "Route", fg=MUTED).pack(side="left", padx=(0, 6))
        self.route_var = tk.StringVar()
        self.route_box = ttk.Combobox(ctl, textvariable=self.route_var,
                                      state="disabled", width=32, font=FONT,
                                      style="Dark.TCombobox")
        self.route_box.pack(side="left", padx=(0, 16))
        self.route_box.bind("<<ComboboxSelected>>", lambda _: self._refresh_route())

        self._label(ctl, "Work area", fg=MUTED).pack(side="left", padx=(0, 6))
        self.work_var = tk.IntVar(value=100)
        self.work_scale = tk.Scale(ctl, from_=50, to=100, orient="horizontal",
                                   variable=self.work_var, bg=BG, fg=FG,
                                   troughcolor=PANEL2, highlightthickness=0,
                                   bd=0, length=140, showvalue=True,
                                   font=FONT_S, sliderrelief="flat",
                                   activebackground=ACCENT, state="disabled")
        self.work_scale.pack(side="left", padx=(0, 16))

        self.keep_dlss = tk.BooleanVar(value=True)
        tk.Checkbutton(ctl, text="Keep the game's own nvngx_dlss.dll",
                       variable=self.keep_dlss, bg=BG, fg=MUTED,
                       selectcolor=PANEL2, activebackground=BG,
                       activeforeground=FG, font=FONT, bd=0,
                       highlightthickness=0).pack(side="left")

        # --- action row ---
        act = tk.Frame(self.root, bg=BG)
        act.pack(fill="x", padx=18, pady=(0, 10))
        self.b_install = self._button(act, "Install", self.do_install, primary=True)
        self.b_install.pack(side="left", padx=(0, 8))
        self.b_install.configure(state="disabled")
        self.b_remove = self._button(act, "Remove", self.do_remove)
        self.b_remove.pack(side="left", padx=(0, 8))
        self.b_remove.configure(state="disabled")
        self.b_diag = self._button(act, "Did it work?", self.do_diagnose)
        self.b_diag.pack(side="left", padx=(0, 8))
        self.b_diag.configure(state="disabled")
        self.b_open = self._button(act, "Open folder", self.open_folder)
        self.b_open.pack(side="left")
        self.b_open.configure(state="disabled")

        self.pbar = ttk.Progressbar(act, mode="determinate", length=200)
        self.pbar.pack(side="right")
        self.p_text = self._label(act, "", fg=MUTED)
        self.p_text.pack(side="right", padx=(0, 10))

        # --- log ---
        logf = tk.Frame(self.root, bg=PANEL)
        logf.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        bar = tk.Frame(logf, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        self._label(bar, "Log", fg=MUTED, bg=PANEL).pack(side="left")
        sb = tk.Scrollbar(logf, bg=PANEL2, troughcolor=PANEL, bd=0,
                          relief="flat", activebackground=MUTED)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 10))
        self.log = tk.Text(logf, bg=PANEL2, fg=FG, font=FONT_M, relief="flat",
                           bd=0, wrap="word", yscrollcommand=sb.set,
                           insertbackground=FG, padx=10, pady=8)
        self.log.pack(fill="both", expand=True, padx=(12, 0), pady=(0, 10))
        sb.configure(command=self.log.yview)
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("bad", foreground=BAD)
        self.log.tag_configure("muted", foreground=MUTED)
        self.log.configure(state="disabled")

        self._on_gen()

    # ------------------------------------------------------------- output

    def say(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.say(*payload)
                elif kind == "prog":
                    pct, txt = payload
                    self.pbar["value"] = pct
                    self.p_text.configure(text=txt[:48])
                elif kind == "matrix":
                    self._show_matrix_window(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    # ---------------------------------------------------------------- gpu

    def card(self) -> gpu.Card:
        """The card in force: detected, or the generation the user picked."""
        choice = self.gen_var.get()
        if choice == AUTO:
            return self.detected
        return gpu.card_for_generation(choice)

    def _on_gen(self) -> None:
        c = self.card()
        if self.gen_var.get() == AUTO:
            if c.sm is None:
                self.gen_note.configure(
                    text="no NVIDIA card found - pick your generation by hand",
                    fg=BAD)
            else:
                self.gen_note.configure(text=f"detected: {c.generation}", fg=MUTED)
        else:
            self.gen_note.configure(
                text=f"forced: {c.generation} ({gpu.GEN_EXAMPLES.get(c.generation, '')})",
                fg=WARN if c.generation != self.detected.generation else MUTED)
        self.gpu_label.configure(text=c.describe())
        # A different card can change whether the game is supported at all.
        if self.game_dir and self.api:
            self.plan = routes.choose(self.game_dir, self.api, self.bits, c)
            self._show_verdict()
            self.b_install.configure(
                state="normal" if self.plan.supported else "disabled")

    def show_matrix(self) -> None:
        """Download each dlssnr build and report which cards it supports."""
        if self.busy:
            return
        self.say("")
        self.say("--- reading CUDA architectures out of every build "
                 "(downloads on first use)", "muted")

        def work():
            import zipfile
            from . import sources
            rows = []
            cat = sources.rhi_catalog()
            for e in cat["dlssnr"]:
                z = sources.download(
                    e["url"], f"dlssnr-{e['label']}.zip",
                    progress=lambda p, t: self.q.put(("prog", (p, t))))
                out = sources.CACHE / f"scan-dlssnr-{e['label']}.dll"
                if not out.is_file():
                    with zipfile.ZipFile(z) as zf:
                        name = [n for n in zf.namelist()
                                if n.endswith(installer.DLSSNR)][0]
                        out.write_bytes(zf.read(name))
                rows.append((e["label"], gpu.dll_architectures(out)))
            self.q.put(("matrix", rows))
            return rows

        self._run(work)

    def _show_matrix_window(self, rows) -> None:
        w = tk.Toplevel(self.root)
        w.title("Which build supports which card")
        w.configure(bg=BG)
        w.geometry("640x360")
        tk.Label(w, text="nvngx_dlssnr builds", font=FONT_B, fg=FG,
                 bg=BG).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(w, text="Read from the CUDA fatbin records inside each file, "
                         "not from a hard-coded table.",
                 font=FONT_S, fg=MUTED, bg=BG, wraplength=600,
                 justify="left").pack(anchor="w", padx=16, pady=(0, 10))

        grid = tk.Frame(w, bg=PANEL)
        grid.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        mine = self.card().generation
        headers = ["build"] + list(gpu.GENERATIONS)
        for c, h in enumerate(headers):
            tk.Label(grid, text=h, font=FONT_B,
                     fg=ACCENT if h == mine else MUTED, bg=PANEL,
                     anchor="w" if c == 0 else "center").grid(
                row=0, column=c, sticky="ew", padx=10, pady=(10, 6))
        for c in range(len(headers)):
            grid.columnconfigure(c, weight=2 if c == 0 else 1)
        for r, (label, archs) in enumerate(rows, start=1):
            tk.Label(grid, text=label, font=FONT_M, fg=FG, bg=PANEL,
                     anchor="w").grid(row=r, column=0, sticky="ew", padx=10,
                                      pady=3)
            gens = archs.generations()
            for c, g in enumerate(gpu.GENERATIONS, start=1):
                yes = gens[g]
                tk.Label(grid, text="yes" if yes else "-", font=FONT_B,
                         fg=(OK if yes else MUTED), bg=PANEL).grid(
                    row=r, column=c, pady=3)
        tk.Label(w, text=f"Your card: {self.card().describe()}", font=FONT_S,
                 fg=MUTED, bg=BG).pack(anchor="w", padx=16, pady=(0, 12))

    # -------------------------------------------------------------- input

    def browse(self) -> None:
        d = filedialog.askdirectory(title="Choose the folder the game's .exe is in")
        if d:
            self.path_var.set(d)
            self.inspect()

    def open_folder(self) -> None:
        if self.game_dir:
            import subprocess
            subprocess.Popen(["explorer", str(self.game_dir)])

    # ------------------------------------------------------------ inspect

    def inspect(self) -> None:
        raw = self.path_var.get().strip().strip('"')
        if not raw:
            return
        try:
            self.exe, self.cands = peinfo.resolve_target(Path(raw))
        except peinfo.PEError as e:
            messagebox.showerror("DLSS5Kit", str(e))
            return
        self.game_dir = self.exe.parent
        try:
            self.bits = peinfo.exe_bitness(self.exe)
        except peinfo.PEError as e:
            messagebox.showerror("DLSS5Kit", str(e))
            return
        self.api = peinfo.detect_api(self.exe, self.game_dir)
        self.plan = routes.choose(self.game_dir, self.api, self.bits, self.card())

        self.say(f"--- {self.game_dir}", "muted")
        self.say(f"executable  {self.exe.name}  ({self.bits}-bit)")
        self.say(f"api         {self.api.api}  [{self.api.confidence}]  "
                 f"{self.api.reason}")
        ngx = [n for n, on in (("D3D11", self.api.ngx_d3d11),
                               ("D3D12", self.api.ngx_d3d12),
                               ("Vulkan", self.api.ngx_vulkan)) if on]
        self.say(f"ngx calls   {', '.join(ngx) if ngx else 'none'}")
        self.say(f"own dlss    {'yes' if self.plan.native_dlss else 'no'}")
        if self.plan.dlss_note:
            self.say(f"            {self.plan.dlss_note}", "warn")
        self.say(f"card        {self.card().describe()}")

        self._show_verdict()
        self._show_facts()

        if self.plan.supported:
            self.route_box.configure(
                state="readonly",
                values=[routes.LABELS[r] for r in self.plan.options])
            self.route_var.set(routes.LABELS[self.plan.route])
            self.b_install.configure(state="normal")
        else:
            self.route_box.configure(state="disabled")
            self.b_install.configure(state="disabled")

        st = installer.status(self.game_dir)
        self.b_remove.configure(state="normal" if st["installed"] else "disabled")
        self.b_diag.configure(state="normal")
        self.b_open.configure(state="normal")
        self._refresh_route()

    def _show_verdict(self) -> None:
        p = self.plan
        if not p.supported:
            self.v_title.configure(text="Not supported", fg=BAD)
            self.v_body.configure(text=p.blocker)
            self.v_note.pack_forget()
            return
        self.v_title.configure(text=f"Route: {routes.LABELS[p.route]}", fg=FG)
        body = p.reason
        for w in p.warnings:
            body += f"\n\nWARNING: {w}"
        self.v_body.configure(text=body)
        if p.dlss_note:
            self.v_note.configure(text=p.dlss_note)
            self.v_note.pack(anchor="w", pady=(6, 0))
        else:
            self.v_note.pack_forget()

    def _show_facts(self) -> None:
        for w in self.facts.winfo_children():
            w.destroy()
        st = installer.status(self.game_dir)
        for i, (name, present) in enumerate(st["present"].items()):
            f = tk.Frame(self.facts, bg=PANEL2)
            f.grid(row=0, column=i, sticky="ew", padx=(0, 6))
            self.facts.columnconfigure(i, weight=1)
            tk.Label(f, text=name, font=FONT_S, fg=MUTED,
                     bg=PANEL2).pack(padx=10, pady=(7, 0))
            tk.Label(f, text="present" if present else "-", font=FONT_B,
                     fg=OK if present else MUTED, bg=PANEL2).pack(padx=10,
                                                                  pady=(0, 7))

    def _refresh_route(self) -> None:
        route = self._selected_route()
        self.work_scale.configure(
            state="normal" if route == routes.FEEDER else "disabled")

    # ------------------------------------------------------------- action

    def _selected_route(self) -> str:
        label = self.route_var.get()
        return next((r for r, l in routes.LABELS.items() if l == label),
                    self.plan.route if self.plan else routes.FEEDER)

    def do_install(self) -> None:
        if self.busy or not self.plan or not self.plan.supported:
            return
        route = self._selected_route()
        card = self.card()
        msg = (f"Install the {route} route into\n\n{self.game_dir}\n\n"
               f"Graphics card: {card.describe()}\n\n"
               f"Files the game already has are backed up and restored by "
               f"Remove.")
        if not card.detected and card.generation != self.detected.generation:
            msg += (f"\n\nNOTE: you forced {card.generation} but this machine "
                    f"has {self.detected.generation}. The build chosen may not "
                    f"run here.")
        for w in self.plan.warnings:
            msg += f"\n\nWARNING: {w}"
        if not messagebox.askokcancel("DLSS5Kit", msg):
            return

        opt = installer.Options(
            route=route,
            provider=3,
            keep_game_dlss=self.keep_dlss.get(),
            card=card,
            work_resolution=int(self.work_var.get()),
        )
        self._run(lambda: installer.install(
            self.game_dir, self.exe, self.api, self.bits, self.plan, opt,
            on_log=lambda s: self.q.put(("log", (s,))),
            on_progress=lambda p, t: self.q.put(("prog", (p, t)))))

    def do_remove(self) -> None:
        if self.busy or not self.game_dir:
            return
        if not messagebox.askokcancel(
                "DLSS5Kit",
                f"Remove everything DLSS5Kit installed into\n\n{self.game_dir}\n\n"
                f"The game's own files are restored from their backups."):
            return
        self._run(lambda: [self.q.put(("log", (line,)))
                           for line in installer.uninstall(self.game_dir)])

    def do_diagnose(self) -> None:
        if not self.game_dir:
            return
        st = installer.status(self.game_dir)
        d = diagnose.diagnose(self.game_dir, st.get("route"))
        self.say("")
        self.say(f"--- diagnosis: {d.verdict.upper()}", "muted")
        for f in d.findings:
            tag = {"ok": "ok", "warn": "warn", "bad": "bad"}.get(f.level, "")
            self.say(f"  {f.text}", tag)
            if f.evidence:
                self.say(f"      {f.evidence}", "muted")
        self.say(f"  => {d.summary}", "bad" if d.verdict == "bad" else "ok")

    def _run(self, fn) -> None:
        self.busy = True
        for b in (self.b_install, self.b_remove):
            b.configure(state="disabled")
        self.pbar["value"] = 0

        def worker():
            try:
                result = fn()
                self.q.put(("done", ("ok", result)))
            except Exception as e:
                self.q.put(("log", (f"FAILED: {e}", "bad")))
                self.q.put(("log", (traceback.format_exc(), "muted")))
                self.q.put(("done", ("error", e)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, payload) -> None:
        kind, result = payload
        self.busy = False
        self.pbar["value"] = 100 if kind == "ok" else 0
        self.p_text.configure(text="")
        if self.game_dir:
            self._show_facts()
            st = installer.status(self.game_dir)
            self.b_remove.configure(state="normal" if st["installed"] else "disabled")
        self.b_install.configure(
            state="normal" if self.plan and self.plan.supported else "disabled")

        if kind == "error":
            messagebox.showerror("DLSS5Kit", str(result))
            return
        if isinstance(result, installer.Report):
            self.say("")
            self.say(f"Done. Route: {result.route}. "
                     f"{len(result.written)} file(s) written.", "ok")
            for w in result.warnings:
                self.say(f"WARNING: {w}", "warn")
            for line in cli.next_steps(result.route):
                self.say(line)


def run(initial_path: str | None = None) -> None:
    root = tk.Tk()
    app = App(root)
    if initial_path:
        app.path_var.set(initial_path)
        root.after(120, app.inspect)
    root.mainloop()


if __name__ == "__main__":
    run()
