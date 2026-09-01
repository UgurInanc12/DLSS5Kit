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

from . import (__version__, cli, config, diagnose, gpu, installer, pairshot,
               peinfo, presets, routes)

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

        # --- controls: route ---
        ctl = tk.Frame(self.root, bg=BG)
        ctl.pack(fill="x", padx=18, pady=(0, 8))
        self._label(ctl, "Route", fg=MUTED).pack(side="left", padx=(0, 6))
        self.route_var = tk.StringVar()
        self.route_box = ttk.Combobox(ctl, textvariable=self.route_var,
                                      state="disabled", width=40, font=FONT,
                                      style="Dark.TCombobox")
        self.route_box.pack(side="left")
        self.route_box.bind("<<ComboboxSelected>>", lambda _: self._refresh_route())
        self.route_note = self._label(ctl, "", fg=MUTED, font=FONT_S)
        self.route_note.pack(side="left", padx=(10, 0))

        # --- controls: DLSS render presets (separate from DLSS 5 NR) ---
        pr = tk.Frame(self.root, bg=BG)
        pr.pack(fill="x", padx=18, pady=(0, 8))
        self._label(pr, "DLSS SR preset", fg=MUTED).pack(side="left", padx=(0, 6))
        self.sr_var = tk.StringVar(value="default")
        self.sr_box = ttk.Combobox(pr, textvariable=self.sr_var,
                                   state="disabled", width=9, font=FONT,
                                   style="Dark.TCombobox",
                                   values=list(presets.SR_PRESETS))
        self.sr_box.pack(side="left", padx=(0, 14))
        self._label(pr, "RR preset", fg=MUTED).pack(side="left", padx=(0, 6))
        self.rr_var = tk.StringVar(value="default")
        self.rr_box = ttk.Combobox(pr, textvariable=self.rr_var,
                                   state="disabled", width=9, font=FONT,
                                   style="Dark.TCombobox",
                                   values=list(presets.RR_PRESETS))
        self.rr_box.pack(side="left", padx=(0, 10))
        self.b_presets = self._button(pr, "Apply presets", self.do_presets)
        self.b_presets.pack(side="left", padx=(0, 10))
        self.b_presets.configure(state="disabled")
        self._label(pr, "the game's own DLSS networks (J/K/L/M, D/E/F); "
                        "restart the game after applying", fg=MUTED,
                    font=FONT_S).pack(side="left")

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
        self.b_open.pack(side="left", padx=(0, 8))
        self.b_open.configure(state="disabled")
        self.b_compare = self._button(act, "Compare pair", self.do_compare)
        self.b_compare.pack(side="left")
        self.b_compare.configure(state="disabled")

        # A determinate progress bar was here and it lied: the steps are not
        # equal in size (a 165 MB download and an ini write are one step
        # each), so the fraction meant nothing. A plain status line saying
        # what is happening right now is honest and more useful.
        self.p_text = self._label(act, "", fg=MUTED, font=FONT_S)
        self.p_text.pack(side="right")

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
                    _, txt = payload
                    self.p_text.configure(text=txt[:60])
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
        self.b_compare.configure(state="normal")
        cur = presets.read_current(self.game_dir)
        self.sr_var.set(cur.sr)
        self.rr_var.set(cur.rr)
        for box in (self.sr_box, self.rr_box):
            box.configure(state="readonly")
        self.b_presets.configure(state="normal" if st["installed"] else "disabled")
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
        note = {
            routes.NATIVE: "cheapest; the game's own DLSS quality mode still applies",
            routes.BRIDGE: "private D3D12 session; quality mode still applies",
            routes.FEEDER: "always DLAA, never upscaling - costs frame rate",
        }.get(route, "")
        self.route_note.configure(text=note,
                                  fg=WARN if route == routes.FEEDER else MUTED)

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
            card=card,
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

    def do_presets(self) -> None:
        if not self.game_dir:
            return
        st = installer.status(self.game_dir)
        pr = presets.Presets(sr=self.sr_var.get(), rr=self.rr_var.get())
        try:
            written = presets.apply(self.game_dir, st.get("route"), pr)
        except presets.PresetError as e:
            messagebox.showerror("DLSS5Kit", str(e))
            return
        self.say(f"presets: {presets.describe(pr)}", "ok")
        for w in written:
            self.say(f"  wrote {w.name}", "muted")
        self.say("Restart the game for the presets to take effect.", "warn")

    def do_compare(self) -> None:
        if not self.game_dir:
            return
        try:
            pre, post, comp = pairshot.compare_latest(self.game_dir)
        except pairshot.PairError as e:
            self.say(str(e), "warn")
            return
        self.say(f"compare: {comp.name}", "ok")
        self.say(f"  from {pre.name} + {post.name}", "muted")
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(comp)])

    def _run(self, fn) -> None:
        self.busy = True
        for b in (self.b_install, self.b_remove):
            b.configure(state="disabled")
        self.p_text.configure(text="working...")

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
