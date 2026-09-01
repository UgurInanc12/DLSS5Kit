"""Command-line interface.

    dlss5kit "D:\\Games\\Game"                install with the recommended route
    dlss5kit "D:\\Games\\Game" --check        inspect only, write nothing
    dlss5kit "D:\\Games\\Game" --diagnose     read the logs back
    dlss5kit "D:\\Games\\Game" --remove       uninstall
    dlss5kit --gpu                           show the card and the build matrix
    dlss5kit --gui                           open the window
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, diagnose, gpu, installer, peinfo, routes


def _resolve_card(choice: str | None) -> gpu.Card:
    """Auto-detect, or take the generation the user named."""
    if not choice or choice.lower() == "auto":
        return gpu.detect_card()
    want = choice.strip().upper().replace("RTX", "").strip()
    for gen in gpu.GENERATIONS:
        if gen.upper().replace("RTX", "").strip() == want:
            return gpu.card_for_generation(gen)
    raise SystemExit(f"error: unknown generation {choice!r}. "
                     f"Use one of: auto, 20, 30, 40, 50")


def _print_gpu_report() -> int:
    card = gpu.detect_card()
    print(f"Detected     : {card.describe()}")
    if card.all_adapters:
        print(f"All adapters : {', '.join(card.all_adapters)}")
    ok, why = gpu.card_supported(card.sm)
    print(f"DLSS 5       : {'yes' if ok else 'NO'} - {why}")
    print()
    print("Fetching the build list and reading each file's CUDA "
          "architectures ...")
    try:
        from . import sources
        cat = sources.rhi_catalog()
    except Exception as e:
        print(f"could not reach the mirror: {e}", file=sys.stderr)
        return 1

    import zipfile
    rows = []
    for e in cat["dlssnr"]:
        try:
            z = sources.download(e["url"], f"dlssnr-{e['label']}.zip")
            out = sources.CACHE / f"scan-dlssnr-{e['label']}.dll"
            if not out.is_file():
                with zipfile.ZipFile(z) as zf:
                    name = [n for n in zf.namelist()
                            if n.endswith(installer.DLSSNR)][0]
                    out.write_bytes(zf.read(name))
            rows.append((e["label"], gpu.dll_architectures(out)))
        except Exception as err:
            print(f"  {e['label']}: could not read ({err})")
    print()
    head = f"{'nvngx_dlssnr build':24}" + "".join(f"{g:>9}" for g in gpu.GENERATIONS)
    print(head)
    print("-" * len(head))
    for label, archs in rows:
        gens = archs.generations()
        line = f"{label:24}" + "".join(
            f"{('yes' if gens[g] else '-'):>9}" for g in gpu.GENERATIONS)
        if card.generation in gens and gens.get(card.generation):
            line += "   <- works on yours"
        print(line)
    print()
    usable = [l for l, a in rows if a.runs_on(card.sm or 0)[0]]
    if usable:
        print(f"Auto would pick: {usable[0]} (newest build that supports "
              f"{card.generation})")
    else:
        print(f"No build supports {card.generation}.")
    return 0


def _inspection_dict(game_dir: Path, exe: Path, cands: list[Path],
                     api: peinfo.ApiInfo, bits: int, plan: routes.Plan,
                     card: gpu.Card) -> dict:
    """Everything an automated caller needs, as plain data.

    This is the contract --json prints. Keep the key names stable: agents and
    scripts parse them, and a rename is a breaking change.
    """
    st = installer.status(game_dir)
    return {
        "schema": 1,
        "tool": "dlss5kit",
        "version": __version__,
        "folder": str(game_dir),
        "executable": exe.name,
        "executable_path": str(exe),
        "exe_candidates": [c.name for c in cands],
        "bitness": bits,
        "api": api.api,
        "api_confidence": api.confidence,
        "api_reason": api.reason,
        "ngx": {
            "d3d11": api.ngx_d3d11,
            "d3d12": api.ngx_d3d12,
            "vulkan": api.ngx_vulkan,
            "any": api.uses_ngx,
        },
        "native_dlss": plan.native_dlss,
        "dlss_evidence": plan.dlss_evidence,
        "dlss_note": plan.dlss_note,
        "gpu": {
            "name": card.name,
            "generation": card.generation,
            "sm": card.sm,
            "detected": card.detected,
            "supported": card.supported,
        },
        "supported": plan.supported,
        "blocker": plan.blocker,
        "route": plan.route if plan.supported else None,
        "route_reason": plan.reason,
        "route_options": plan.options,
        "warnings": plan.warnings,
        "installed": st["installed"],
        "installed_route": st["route"],
        "install_complete": st["complete"],
        "components": st["components"],
        "present": st["present"],
    }


def _print_inspection(game_dir: Path, exe: Path, cands: list[Path],
                      api: peinfo.ApiInfo, bits: int, plan: routes.Plan,
                      card: gpu.Card) -> None:
    print(f"Folder      : {game_dir}")
    print(f"Executable  : {exe.name}"
          + (f"   ({len(cands)} candidates)" if len(cands) > 1 else ""))
    print(f"Architecture: {bits}-bit")
    print(f"Graphics API: {api.api}  [{api.confidence} confidence]")
    print(f"              {api.reason}")
    ngx = [n for n, on in (("D3D11", api.ngx_d3d11), ("D3D12", api.ngx_d3d12),
                           ("Vulkan", api.ngx_vulkan)) if on]
    ngx_text = (", ".join(ngx) if ngx
                else "none - this game does not call DLSS itself")
    print(f"NGX calls   : {ngx_text}")
    print(f"Own DLSS    : {'yes' if plan.native_dlss else 'no'}"
          + (f"  ({', '.join(plan.dlss_evidence[:4])}"
             + (" ..." if len(plan.dlss_evidence) > 4 else "") + ")"
             if plan.dlss_evidence else ""))
    if plan.dlss_note:
        print(f"              NOTE: {plan.dlss_note}")
    print(f"Graphics card: {card.describe()}")
    print()
    if not plan.supported:
        print(f"NOT SUPPORTED: {plan.blocker}")
        return
    print(f"Route       : {plan.route.upper()}  ({routes.LABELS[plan.route]})")
    print(f"              {plan.reason}")
    if plan.options:
        print(f"Alternatives: {', '.join(plan.options)}")
    for w in plan.warnings:
        print(f"WARNING     : {w}")

    st = installer.status(game_dir)
    if st["installed"]:
        print()
        print(f"Already installed: route={st['route']} complete={st['complete']}")
        for k, v in st["components"].items():
            print(f"    {k}: {v}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="dlss5kit",
        description="One-click DLSS 5 neural rendering setup for PC games.",
        epilog="Exit codes: 0 ok, 1 install failed, 2 bad path or unreadable "
               "executable, 3 game not supported, 4 diagnosis says it is not "
               "working.")
    p.add_argument("target", nargs="?", help="game folder or .exe")
    p.add_argument("--gui", action="store_true", help="open the window")
    p.add_argument("--gpu", action="store_true",
                   help="show the detected card and which builds support it")
    p.add_argument("--check", action="store_true",
                   help="inspect only, write nothing")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output for --check and --diagnose")
    p.add_argument("--diagnose", action="store_true",
                   help="read the logs back and report")
    p.add_argument("--remove", action="store_true", help="uninstall")
    p.add_argument("--generation", "--gen", dest="generation", default="auto",
                   help="RTX generation: auto (default), 20, 30, 40 or 50")
    p.add_argument("--route", choices=[routes.NATIVE, routes.BRIDGE, routes.FEEDER],
                   help="override the recommended route")
    p.add_argument("--provider", type=int, default=3,
                   help="feeder motion-vector provider (default 3, LumeniteFX)")
    p.add_argument("--local", type=Path,
                   help="folder of already-downloaded components to prefer")
    p.add_argument("--reshade-setup", type=Path,
                   help="a ReShade_Setup_*_Addon.exe to use instead of downloading")
    p.add_argument("--work-resolution", type=int, default=100,
                   help="feeder neural work area, 50-100 (default 100)")
    p.add_argument("--replace-game-dlss", action="store_true",
                   help="replace the game's own nvngx_dlss.dll (backed up)")
    p.add_argument("--ignore-gpu-mismatch", action="store_true",
                   help="install even when the build has no code for your card")
    p.add_argument("--yes", "-y", action="store_true",
                   help="accepted for scripting; this CLI never prompts anyway")
    p.add_argument("--version", action="version", version=f"dlss5kit {__version__}")
    a = p.parse_args(argv)

    if a.gpu:
        return _print_gpu_report()

    if a.gui or not a.target:
        from .gui import run
        run(str(a.target) if a.target else None)
        return 0

    target = Path(a.target)
    try:
        exe, cands = peinfo.resolve_target(target)
    except peinfo.PEError as e:
        if a.json:
            print(json.dumps({"schema": 1, "ok": False, "error": str(e)}, indent=2))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    game_dir = exe.parent

    if a.remove:
        lines = installer.uninstall(game_dir)
        if a.json:
            print(json.dumps({"schema": 1, "ok": True, "action": "remove",
                              "log": lines}, indent=2))
        else:
            for line in lines:
                print(line)
        return 0

    if a.diagnose:
        st = installer.status(game_dir)
        d = diagnose.diagnose(game_dir, st.get("route"))
        if a.json:
            print(json.dumps({
                "schema": 1,
                "action": "diagnose",
                "verdict": d.verdict,
                "summary": d.summary,
                "installed_route": st.get("route"),
                "findings": [{"level": f.level, "text": f.text,
                              "evidence": f.evidence} for f in d.findings],
            }, indent=2))
        else:
            print(diagnose.format_text(d))
        return 4 if d.verdict == diagnose.BAD else 0

    try:
        bits = peinfo.exe_bitness(exe)
    except peinfo.PEError as e:
        if a.json:
            print(json.dumps({"schema": 1, "ok": False, "error": str(e)}, indent=2))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2
    card = _resolve_card(a.generation)
    api = peinfo.detect_api(exe, game_dir)
    plan = routes.choose(game_dir, api, bits, card)

    if a.check:
        if a.json:
            print(json.dumps(
                _inspection_dict(game_dir, exe, cands, api, bits, plan, card),
                indent=2))
        else:
            _print_inspection(game_dir, exe, cands, api, bits, plan, card)
        return 0 if plan.supported else 3

    _print_inspection(game_dir, exe, cands, api, bits, plan, card)
    if not plan.supported:
        return 3

    opt = installer.Options(
        route=a.route or "",
        provider=a.provider,
        local_dir=a.local,
        reshade_setup=a.reshade_setup,
        keep_game_dlss=not a.replace_game_dlss,
        ignore_gpu_mismatch=a.ignore_gpu_mismatch,
        card=card,
        work_resolution=max(50, min(100, a.work_resolution)),
    )

    print()
    try:
        rep = installer.install(game_dir, exe, api, bits, plan, opt,
                                on_log=print)
    except installer.InstallError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1

    print()
    print(f"Done. Route: {rep.route}. {len(rep.written)} file(s) written.")
    for w in rep.warnings:
        print(f"WARNING: {w}")
    print()
    for line in next_steps(rep.route, opt.provider):
        print(line)
    return 0


def next_steps(route: str, provider: int = 3) -> list[str]:
    """What the user has to do in game. Shared by the CLI and the GUI."""
    out = ["Press Home in game for the ReShade overlay."]
    if route == routes.FEEDER:
        tech = config.PROVIDERS.get(provider, (None, None, False))[1]
        name = tech.split("@")[0] if tech else "your motion-vector shader"
        out += [
            f"  1. Tick {name} AND DLSS 5 Feed, with {name} ABOVE the feed.",
            "  2. Enable neural rendering in the DLSS 5 panel (off by default).",
            "  3. Turn the game's own MSAA/SSAA off.",
            "This route is DLAA: it anti-aliases at full resolution and does "
            "not upscale, so it costs frame rate rather than giving it back.",
        ]
    else:
        out += [
            "  1. Open the DLSS 5 Neural Rendering panel.",
            "  2. Turn neural rendering ON - it is off by default (F6 toggles).",
        ]
    out.append("Set your resolution BEFORE turning neural rendering on: the "
               "DLSS feature is built for one backbuffer size and changing it "
               "while running can freeze the game.")
    return out


if __name__ == "__main__":
    sys.exit(main())
