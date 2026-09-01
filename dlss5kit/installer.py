r"""The install engine: writes files, records what it wrote, undoes it exactly.

WHAT ENDS UP IN THE GAME FOLDER
-------------------------------
    dxgi.dll                    ReShade64.dll, extracted from the official
                                installer exe (which is a PE with a zip
                                appended - no wizard needed)
    ReShade.ini                 add-on loading, and shader paths on feeder
    renodx-dlss5.addon64        the DLSS 5 neural rendering add-on
    nvngx_dlssnr.dll            NVIDIA neural rendering runtime (~165 MB)
    nvngx_dlss.dll              NVIDIA DLSS runtime
  bridge route adds:
    dlss5-bridge.addon64, dlss5-bridge.cfg
  feeder route adds:
    dlss5-feed.addon64, dlss5-feed.cfg,
    reshade-shaders\Shaders\{ReShade.fxh, ReShadeUI.fxh, DrawText.fxh,
                             DLSS5_Feed.fx, lumenite_*.fx}
    reshade-shaders\Shaders\include\lumenite_*.fxh
    reshade-shaders\Textures\lumenite_*.png

THE MANIFEST IS THE POINT
-------------------------
dlss5kit-manifest.json records every path written and every backup taken, so
Remove takes out exactly what was put in and restores the game's own files.
Two rules learned from reading the alternatives:

  1. Never back up a file a PREVIOUS run of ours wrote. Doing so makes
     uninstall RESTORE our file instead of deleting it, and after installing
     twice the folder comes out of an "uninstall" still fully set up.
  2. Switching routes must remove the previous route first. The routes drop
     very different files, ReShade loads every .addon64 in the folder, and two
     add-ons hooking the same NGX calls fight each other.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, gpu, peinfo, routes, sources
from .routes import BRIDGE, FEEDER, NATIVE

MANIFEST = "dlss5kit-manifest.json"
BACKUP_SUFFIX = ".dlss5kit-backup"

PROXY_DLL = "dxgi.dll"
OPENGL_PROXY = "opengl32.dll"

RENODX = "renodx-dlss5.addon64"
DLSSNR = "nvngx_dlssnr.dll"
DLSS = "nvngx_dlss.dll"
BRIDGE_ADDON = "dlss5-bridge.addon64"
FEEDER_ADDON = "dlss5-feed.addon64"
FEEDER_FX = "DLSS5_Feed.fx"

SHADERS = Path("reshade-shaders") / "Shaders"
INCLUDE = SHADERS / "include"
TEXTURES = Path("reshade-shaders") / "Textures"

# Add-ons belonging to a route other than the one being installed. ReShade
# loads every .addon64 it finds, so a leftover from another route is not
# harmless - it hooks the same calls.
ROUTE_ADDONS = {
    NATIVE: set(),
    BRIDGE: {BRIDGE_ADDON},
    FEEDER: {FEEDER_ADDON},
}
ALL_ROUTE_ADDONS = {BRIDGE_ADDON, FEEDER_ADDON, "dlss5-dx11-bridge.addon64"}

# Anti-cheat markers. Not a refusal, but the user is told plainly.
ANTICHEAT = {
    "BattlEye": ("battleye", "beservice.exe", "beclient.dll", "beclient_x64.dll"),
    "Easy Anti-Cheat": ("easyanticheat", "easyanticheat_eos",
                        "easyanticheat.sys", "easyanticheat_x64.dll"),
    "Vanguard": ("vgc.exe", "vgk.sys"),
    "nProtect GameGuard": ("gameguard", "gamemon64.des"),
    "Denuvo Anti-Cheat": ("denuvo",),
}


class InstallError(RuntimeError):
    pass


@dataclass
class Options:
    route: str = ""                   # "" = use the recommended route
    provider: int = 3                 # feeder motion-vector provider
    renodx_version: str | None = None
    dlssnr_version: str | None = None
    dlss_version: str | None = None
    local_dir: Path | None = None     # a folder of already-downloaded files
    reshade_setup: Path | None = None # a ReShade_Setup_*_Addon.exe on disk
    ignore_gpu_mismatch: bool = False
    card: "gpu.Card | None" = None    # None = detect it
    work_resolution: int = 100
    preset: int = 0
    hdr: int = -1


@dataclass
class Report:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)
    preinstalled: set[str] = field(default_factory=set)
    route: str = ""
    complete: bool = False


# --------------------------------------------------------------- helpers

def detect_anticheat(folder: Path) -> tuple[str, list[str]]:
    """(summary, evidence). Looks two levels down, which is where it lives."""
    found: dict[str, list[str]] = {}
    try:
        entries = []
        for p in folder.iterdir():
            entries.append(p)
            if p.is_dir():
                try:
                    entries.extend(list(p.iterdir())[:200])
                except OSError:
                    pass
    except OSError:
        return "", []
    for p in entries:
        low = p.name.lower()
        for name, markers in ANTICHEAT.items():
            if any(m in low for m in markers):
                found.setdefault(name, []).append(p.name)
    if not found:
        return "", []
    return " and ".join(found), sorted({v for vs in found.values() for v in vs})


def is_reshade(path: Path) -> bool:
    """A ReShade proxy DLL is over 1 MB and carries the literal 'ReShade'."""
    try:
        if path.stat().st_size < 1_000_000:
            return False
        with open(path, "rb") as f:
            return b"ReShade" in f.read()
    except OSError:
        return False


def read_manifest(folder: Path) -> dict | None:
    p = folder / MANIFEST
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return None


def installed_route(folder: Path) -> str | None:
    m = read_manifest(folder)
    return m.get("route") if m else None


def _previously_ours(folder: Path) -> set[str]:
    m = read_manifest(folder)
    return set(m.get("files", [])) if m else set()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _same_file(a: Path, b: Path) -> bool:
    """Are two files byte-identical? Size first, then a streamed hash."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    try:
        h1, h2 = hashlib.sha256(), hashlib.sha256()
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if not ca:
                    break
                h1.update(ca)
                h2.update(cb)
        return h1.digest() == h2.digest()
    except OSError:
        return False


def _backup(dst: Path, rep: Report, root: Path,
            src: Path | None = None) -> None:
    """Preserve the game's own file before overwriting it.

    A file a PREVIOUS install of ours wrote is emphatically not the game's:
    backing it up would make uninstall restore our file instead of deleting
    it, leaving the folder still set up after a "removal".

    When the incoming file is byte-identical to what is already there, no
    backup is taken - a 165 MB copy of a file we are about to write again,
    identically, is pure waste. The path still enters the manifest so
    uninstall removes it.
    """
    if not dst.is_file():
        return
    bak = dst.with_name(dst.name + BACKUP_SUFFIX)
    rel = _rel(bak, root)
    if _rel(dst, root) in rep.preinstalled:
        # This file is ours from an earlier run, so it must NOT be backed up.
        # But a backup of the GAME's original may already exist from that run,
        # and it has to be carried into this manifest - otherwise the next
        # uninstall reads a manifest with no backup listed, deletes our file
        # and leaves the game's original stranded in a .backup nobody restores.
        if bak.is_file() and rel not in rep.written:
            rep.written.append(rel)
        return
    if src is not None and src.is_file() and _same_file(src, dst):
        rep.notes.append(f"{dst.name} was already identical, no backup needed")
        return
    if bak.exists():
        # Backed up by an earlier run. Do not copy again, that would overwrite
        # the game's original with our file - but the entry must still enter
        # this manifest or a later uninstall would never restore it.
        if rel not in rep.written:
            rep.written.append(rel)
            rep.notes.append(f"kept the existing backup of {dst.name}")
        return
    try:
        shutil.copy2(dst, bak)
        rep.written.append(rel)
        rep.notes.append(f"backed up the game's own {dst.name}")
    except OSError as e:
        raise InstallError(f"Could not back up {dst.name}: {e}") from e


def _place(src_bytes: bytes, dst: Path, rep: Report, root: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Identical incoming bytes need no backup: comparing in memory is cheaper
    # than copying a 165 MB file we are about to overwrite with itself.
    if dst.is_file() and _rel(dst, root) not in rep.preinstalled:
        try:
            if (dst.stat().st_size == len(src_bytes)
                    and dst.read_bytes() == src_bytes):
                rel = _rel(dst, root)
                if rel not in rep.written:
                    rep.written.append(rel)
                rep.notes.append(f"{dst.name} was already identical")
                return
        except OSError:
            pass
    _backup(dst, rep, root)
    try:
        dst.write_bytes(src_bytes)
    except OSError as e:
        raise InstallError(f"Could not write {dst.name}: {e}") from e
    rel = _rel(dst, root)
    if rel not in rep.written:
        rep.written.append(rel)


def _warn_dlss_gpu(path: Path, sm: int | None, card, rep: Report, log) -> None:
    """Report nvngx_dlss compatibility without blocking the install.

    Unlike the neural rendering runtime, this one legitimately relies on
    driver JIT: the 310.8.0 build carries no sm_120 CUBIN yet runs on RTX 50
    through its sm_89 PTX. So this warns and never refuses.

    The verdict comes from Archs.runs_on, not from searching the explanatory
    text: that text mentions "JIT-compiled for newer cards" whenever any PTX
    is present, which is true even when THIS card is served natively.
    """
    archs = gpu.dll_architectures(path)
    if not archs or sm is None:
        return
    ok, how = archs.runs_on(sm)
    if not ok:
        log(f"      GPU check: no code for {card.generation} "
            f"({archs.summary()})")
        rep.warnings.append(
            f"nvngx_dlss.dll has no code for {card.generation}; the neural "
            f"rendering pass still works, but a game using this runtime for "
            f"ordinary DLSS may not")
    elif how == "native":
        log(f"      GPU check: native code for {card.generation} present")
    else:
        log(f"      GPU check: no native code for {card.generation}; "
            f"the driver will JIT-compile it ({how})")
        rep.notes.append(f"nvngx_dlss.dll reaches {card.generation} by "
                         f"driver JIT rather than native code")


def _copy(src: Path, dst: Path, rep: Report, root: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _backup(dst, rep, root, src=src)
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        raise InstallError(f"Could not write {dst.name}: {e}") from e
    rel = _rel(dst, root)
    if rel not in rep.written:
        rep.written.append(rel)


def extract_member(zip_path: Path, ends_with: str) -> bytes:
    """Read the first member whose name ends with `ends_with`.

    The ReShade setup exe is a PE with a zip appended; zipfile reads it
    directly from the end-of-central-directory record, so the interactive
    wizard is entirely unnecessary.
    """
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.lower().endswith(ends_with.lower()):
                return z.read(name)
    raise InstallError(f"{zip_path.name} does not contain {ends_with}")


def extract_tree(zip_path: Path, inner_dir: str, dest: Path, rep: Report,
                 root: Path, only_ext: tuple[str, ...]) -> int:
    """Extract one directory out of a zip, ignoring its top-level folder."""
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            if len(parts) < 2:
                continue
            rel = "/".join(parts[1:])              # drop "Repo-branch/"
            if not rel.lower().startswith(inner_dir.lower() + "/"):
                continue
            tail = rel[len(inner_dir) + 1:]
            if "/" in tail:                        # this level only
                continue
            if only_ext and not tail.lower().endswith(only_ext):
                continue
            _place(z.read(info), dest / tail, rep, root)
            n += 1
    return n


# ------------------------------------------------------------ local files

LOCAL_NAMES = (RENODX, DLSSNR, DLSS, BRIDGE_ADDON, FEEDER_ADDON, FEEDER_FX)


def scan_local(folder: Path | None) -> dict[str, Path]:
    """Which components are already sitting in a folder on disk."""
    out: dict[str, Path] = {}
    if not folder or not folder.is_dir():
        return out
    for name in LOCAL_NAMES:
        p = folder / name
        if p.is_file():
            out[name] = p
    return out


def find_reshade_setup() -> Path | None:
    """A ReShade add-on installer already downloaded, newest first."""
    import os
    dl = Path(os.path.expanduser("~")) / "Downloads"
    best: tuple[float, Path] | None = None
    for d in (dl, Path.cwd()):
        if not d.is_dir():
            continue
        for p in d.glob("ReShade_Setup_*_Addon.exe"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, p)
    return best[1] if best else None


# --------------------------------------------------------------- planning

def plan_steps(route: str, provider: int) -> list[str]:
    steps = ["ReShade"]
    if route == FEEDER:
        steps += ["ReShade shader headers", "DLSS5-Feeder"]
        if provider in (3, 4):
            steps.append("LumeniteFX (motion vectors)")
    elif route == BRIDGE:
        steps.append("dlss5-bridge")
    steps += ["DLSS 5 add-on", "nvngx_dlssnr.dll", "nvngx_dlss.dll",
              "ReShade configuration"]
    if route == FEEDER:
        steps.append("dlss5-feed.cfg")
    elif route == BRIDGE:
        steps.append("dlss5-bridge.cfg")
    return steps


# ---------------------------------------------------------------- install

def install(game_dir: Path, exe: Path, api: peinfo.ApiInfo, bitness: int,
            route_plan: routes.Plan, opt: Options,
            on_log=None, on_step=None, on_progress=None) -> Report:
    """Perform the install. Every write is recorded before the next begins."""
    log = on_log or (lambda *_: None)
    step = on_step or (lambda *_: None)
    prog = on_progress or (lambda *_: None)

    root = Path(game_dir)
    rep = Report()
    route = opt.route or route_plan.route
    rep.route = route

    if not route_plan.supported:
        raise InstallError(route_plan.blocker)

    proxy = OPENGL_PROXY if api.api == peinfo.OPENGL else PROXY_DLL

    # --- refuse to fight another injector ---------------------------------
    existing = root / proxy
    rep.preinstalled = _previously_ours(root)
    if (existing.is_file() and not is_reshade(existing)
            and _rel(existing, root) not in rep.preinstalled):
        raise InstallError(
            f"{proxy} already exists here but it is not ReShade (DXVK, "
            f"Special K, an ENB or another injector). Remove it first - "
            f"overwriting it would break whatever it is doing.")

    # --- anti-cheat: state it, do not refuse ------------------------------
    ac_name, ac_evidence = detect_anticheat(root)
    if ac_name:
        rep.warnings.append(
            f"{ac_name} detected ({', '.join(ac_evidence[:4])}). ReShade "
            f"add-ons and anti-cheat do not coexist: expect the game not to "
            f"start, or nothing to happen, or a ban. Do not use this online.")
        log(f"    !! {ac_name} detected")

    # --- switching routes removes the previous one first ------------------
    previous = installed_route(root)
    if previous and previous != route:
        log(f"[0] removing the previous {previous} install first")
        for line in uninstall(root):
            log(f"    {line}")
        rep.preinstalled = set()
        rep.notes.append(f"replaced a previous {previous} install")

    # Belt and braces: no add-on from another route may remain, whatever the
    # manifest said. ReShade loads them all.
    for name in ALL_ROUTE_ADDONS - ROUTE_ADDONS.get(route, set()):
        p = root / name
        if p.is_file():
            try:
                p.unlink()
                log(f"    removed {name} (belongs to another route)")
                rep.notes.append(f"removed a stale {name}")
            except OSError:
                rep.warnings.append(f"could not remove {name} - delete it by "
                                    f"hand, it conflicts with this route")

    local = scan_local(opt.local_dir)
    if local:
        log(f"    using local files from {opt.local_dir}: "
            f"{', '.join(sorted(local))}")

    steps = plan_steps(route, opt.provider)
    n = len(steps)
    idx = 0

    def begin(name: str) -> None:
        nonlocal idx
        step(idx, n, name)
        log(f"[{idx + 1}/{n}] {name}")
        idx += 1

    def dl(url: str, fname: str) -> Path:
        return sources.download(url, fname, progress=lambda p, t: prog(p, t))

    try:
        # --- 1) ReShade ---------------------------------------------------
        begin("ReShade")
        setup = opt.reshade_setup or find_reshade_setup()
        if setup and Path(setup).is_file():
            ver = "local"
            m = sources.RESHADE_SETUP_RE.search("/downloads/" + Path(setup).name)
            if m:
                ver = m.group(1)
            setup = Path(setup)
            log(f"      using {setup.name} from disk")
        else:
            ver, url = sources.resolve_reshade()
            setup = dl(url, f"ReShade_Setup_{ver}_Addon.exe")
        rep.components["reshade"] = ver
        _place(extract_member(setup, "ReShade64.dll"), root / proxy, rep, root)
        log(f"      ReShade {ver} -> {proxy}")

        # --- 2) the route-specific middle ---------------------------------
        if route == BRIDGE:
            begin("dlss5-bridge")
            if BRIDGE_ADDON in local:
                _copy(local[BRIDGE_ADDON], root / BRIDGE_ADDON, rep, root)
                log(f"      {BRIDGE_ADDON} (local file)")
                rep.components["bridge"] = "local"
            else:
                tag, url = sources.resolve_bridge()
                f = dl(url, f"dlss5-bridge-{tag}.addon64")
                _copy(f, root / BRIDGE_ADDON, rep, root)
                log(f"      dlss5-bridge {tag}")
                rep.components["bridge"] = tag

        elif route == FEEDER:
            begin("ReShade shader headers")
            for h in sources.RESHADE_HEADERS:
                _place(sources.fetch_bytes(sources.RESHADE_HEADERS_BASE + h),
                       root / SHADERS / h, rep, root)
            log(f"      {', '.join(sources.RESHADE_HEADERS)}")

            begin("DLSS5-Feeder")
            if FEEDER_ADDON in local and FEEDER_FX in local:
                _copy(local[FEEDER_ADDON], root / FEEDER_ADDON, rep, root)
                _copy(local[FEEDER_FX], root / SHADERS / FEEDER_FX, rep, root)
                log("      DLSS5-Feeder (local files)")
                rep.components["feeder"] = "local"
            else:
                tag, assets = sources.resolve_feeder()
                for name, dest in ((FEEDER_ADDON, root / FEEDER_ADDON),
                                   (FEEDER_FX, root / SHADERS / FEEDER_FX)):
                    if name not in assets:
                        raise InstallError(
                            f"The DLSS5-Feeder release {tag} has no {name}.")
                    _copy(dl(assets[name], f"{tag}-{name}"), dest, rep, root)
                log(f"      DLSS5-Feeder {tag}")
                rep.components["feeder"] = tag

            if opt.provider in (3, 4):
                begin("LumeniteFX (motion vectors)")
                z = dl(sources.LUMENITE_ZIP, "LumeniteFX-mainline.zip")
                a = extract_tree(z, "Shaders", root / SHADERS, rep, root, (".fx",))
                b = extract_tree(z, "Shaders/include", root / INCLUDE, rep, root,
                                 (".fxh",))
                c = extract_tree(z, "Textures", root / TEXTURES, rep, root,
                                 (".png",))
                log(f"      {a} shaders, {b} headers, {c} textures")
                rep.components["lumenite"] = "mainline"

        # --- 3) the DLSS parts --------------------------------------------
        catalog = None

        def cat():
            nonlocal catalog
            if catalog is None:
                catalog = sources.rhi_catalog()
                if sources.last_fallback:
                    log(f"      {sources.last_fallback}")
                    if sources.last_fallback not in rep.warnings:
                        rep.warnings.append(sources.last_fallback)
            return catalog

        begin("DLSS 5 add-on")
        if RENODX in local and not opt.renodx_version:
            _copy(local[RENODX], root / RENODX, rep, root)
            log(f"      {RENODX} (local file)")
            rep.components["renodx"] = "local"
        else:
            e = sources.pick(cat()["renodx"], opt.renodx_version)
            f = dl(e["url"], f"renodx-{e['label']}.zip")
            _place(extract_member(f, ".addon64"), root / RENODX, rep, root)
            log(f"      renodx-dlss5 {e['label']}")
            rep.components["renodx"] = e["label"]

        begin("nvngx_dlssnr.dll")
        card = opt.card or gpu.detect_card()
        sm = card.sm
        log(f"      graphics card: {card.describe()}")
        ok_card, why_card = gpu.card_supported(sm)
        if not ok_card and not opt.ignore_gpu_mismatch:
            raise InstallError(why_card)
        rep.components["generation"] = card.generation

        if DLSSNR in local and not opt.dlssnr_version:
            _copy(local[DLSSNR], root / DLSSNR, rep, root)
            compat, why = gpu.check(root / DLSSNR, sm)
            log(f"      {DLSSNR} (local file)")
            log(f"      GPU check: {why}")
            rep.components["dlssnr"] = "local"
            if compat is False and not opt.ignore_gpu_mismatch:
                raise InstallError(
                    f"Your local {DLSSNR} will not run on a "
                    f"{card.generation} card.\n\n{why}\n\n"
                    f"Clear the local folder, or untick 'prefer local files', "
                    f"to let the tool download a build that does.")
            if compat is False:
                rep.warnings.append(
                    f"local {DLSSNR} has no code for {card.generation}")
        else:
            # Builds differ by architecture: 310.8.0 is RTX 50 only, -RTX40 is
            # RTX 40 and 50, the SF builds cover RTX 20 through 50. When the
            # user has not pinned a version, walk the list newest-first and
            # take the first build that can actually run on this card.
            tried: list[tuple[str, str]] = []
            chosen = None
            candidates = ([sources.pick(cat()["dlssnr"], opt.dlssnr_version)]
                          if opt.dlssnr_version else cat()["dlssnr"])
            for e in candidates:
                f = dl(e["url"], f"dlssnr-{e['label']}.zip")
                _place(extract_member(f, DLSSNR), root / DLSSNR, rep, root)
                compat, why = gpu.check(root / DLSSNR, sm)
                if compat is False and not opt.ignore_gpu_mismatch:
                    if opt.dlssnr_version:
                        raise InstallError(
                            f"Build {e['label']} will not run on a "
                            f"{card.generation} card.\n\n{why}\n\nLeave the "
                            f"version on Auto and a suitable build is picked "
                            f"for you.")
                    tried.append((e["label"], why))
                    log(f"      skipped {e['label']} - no code for "
                        f"{card.generation}")
                    continue
                chosen = (e, compat, why)
                break
            if chosen is None:
                detail = "\n".join(f"  {lbl}: {w.split('It has no code for it: ')[-1]}"
                                   for lbl, w in tried)
                raise InstallError(
                    f"No nvngx_dlssnr build supports a {card.generation} "
                    f"card.\n\nTried:\n{detail}")
            e, compat, why = chosen
            log(f"      nvngx_dlssnr {e['label']}")
            log(f"      GPU check: {why}")
            rep.components["dlssnr"] = e["label"]
            if tried:
                rep.notes.append("skipped as incompatible: "
                                 + ", ".join(lbl for lbl, _ in tried))
            if compat is None:
                rep.warnings.append(f"could not verify GPU compatibility ({why})")

        begin("nvngx_dlss.dll")
        # The runtime is ALWAYS updated. A game ships whatever DLSS build it
        # was released with - Metro Exodus Enhanced Edition carries a 13.8 MB
        # nvngx_dlss.dll from 2021 - and leaving that in place means the
        # neural pass runs against a years-old runtime. The game's original is
        # copied to nvngx_dlss.dll.dlss5kit-backup first and restored by
        # Remove, so nothing is lost.
        replacing_game_file = ((root / DLSS).is_file()
                               and _rel(root / DLSS, root) not in rep.written)
        if replacing_game_file:
            if api.uses_ngx:
                log("      updating the game's own nvngx_dlss.dll (backed up, "
                    "restored by Remove)")
            else:
                log("      replacing a stray nvngx_dlss.dll (this game does "
                    "not call NGX, so it is not the game's own)")
        if DLSS in local and not opt.dlss_version:
            _copy(local[DLSS], root / DLSS, rep, root)
            log(f"      {DLSS} (local file)")
            rep.components["dlss"] = "local"
            _warn_dlss_gpu(root / DLSS, sm, card, rep, log)
        else:
            e = sources.pick(cat()["dlss"], opt.dlss_version)
            f = dl(e["url"], f"dlss-{e['label']}.zip")
            _place(extract_member(f, DLSS), root / DLSS, rep, root)
            log(f"      nvngx_dlss {e['label']}")
            rep.components["dlss"] = e["label"]
            _warn_dlss_gpu(root / DLSS, sm, card, rep, log)

        # --- 4) configuration ---------------------------------------------
        begin("ReShade configuration")
        _backup(root / "ReShade.ini", rep, root)
        if route == FEEDER:
            _backup(root / "ReShadePreset.ini", rep, root)
            config.write_reshade_ini(root, opt.provider)
            config.write_preset(root, opt.provider)
            for f in ("ReShade.ini", "ReShadePreset.ini"):
                if f not in rep.written:
                    rep.written.append(f)
            label, tech, _ = config.PROVIDERS[opt.provider]
            log(f"      DLSS5_MV_PROVIDER={opt.provider} ({label})")
            if tech:
                log(f"      technique order: {tech} above {config.FEED_TECHNIQUE}")
            else:
                rep.notes.append(
                    "Install your chosen motion-vector shader yourself and put "
                    "its technique ABOVE DLSS 5 Feed in the ReShade overlay.")
        else:
            config.write_reshade_ini(root, None)
            if "ReShade.ini" not in rep.written:
                rep.written.append("ReShade.ini")
            log("      add-on loading enabled (this route needs no shaders)")

        if route == FEEDER:
            begin("dlss5-feed.cfg")
            _backup(root / config.FEED_CFG, rep, root)
            config.write_feed_cfg(root, {
                "work_resolution": opt.work_resolution,
                "preset": opt.preset,
                "hdr": opt.hdr,
            })
            if config.FEED_CFG not in rep.written:
                rep.written.append(config.FEED_CFG)
            if opt.work_resolution != 100:
                log(f"      work_resolution={opt.work_resolution}%")
        elif route == BRIDGE:
            begin("dlss5-bridge.cfg")
            _backup(root / config.BRIDGE_CFG, rep, root)
            config.write_bridge_cfg(root, route_plan.native_dlss)
            if config.BRIDGE_CFG not in rep.written:
                rep.written.append(config.BRIDGE_CFG)
            if not route_plan.native_dlss:
                log("      synth_after=3 (the game has no DLSS of its own)")

        rep.complete = True
        prog(100, "Done")
    finally:
        # Even on failure the manifest is written: files already placed must
        # be removable, not orphaned in the game folder with no record.
        _write_manifest(root, exe, api, route, opt, rep, proxy)

    return rep


def _write_manifest(root: Path, exe: Path, api: peinfo.ApiInfo, route: str,
                    opt: Options, rep: Report, proxy: str) -> None:
    data = {
        "tool": "dlss5kit",
        "version": 1,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "route": route,
        "proxy": proxy,
        "exe": exe.name,
        "api": api.api,
        "api_reason": api.reason,
        "provider": opt.provider if route == FEEDER else None,
        "components": rep.components,
        "files": rep.written,
        "skipped": rep.skipped,
        "notes": rep.notes,
        "warnings": rep.warnings,
        "complete": rep.complete,
    }
    try:
        (root / MANIFEST).write_text(json.dumps(data, indent=2), encoding="utf8")
    except OSError:
        rep.warnings.append("could not write the manifest - Remove will not "
                            "be able to undo this install automatically")


# -------------------------------------------------------------- uninstall

def uninstall(game_dir: Path) -> list[str]:
    """Undo exactly what the manifest records. Returns log lines."""
    root = Path(game_dir)
    out: list[str] = []
    man = read_manifest(root)
    if not man:
        out.append("No dlss5kit manifest here - nothing to remove.")
        return out

    files = list(man.get("files", []))
    # Restore backups first: a backup entry and the file it restores can both
    # be listed, and deleting after restoring would undo the restore.
    backups = [f for f in files if f.endswith(BACKUP_SUFFIX)]
    plain = [f for f in files if not f.endswith(BACKUP_SUFFIX)]

    restored: set[str] = set()
    for rel in backups:
        bak = root / rel
        orig = root / rel[: -len(BACKUP_SUFFIX)]
        if not bak.is_file():
            continue
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bak), str(orig))
            restored.add(rel[: -len(BACKUP_SUFFIX)])
            out.append(f"restored {orig.name} (the game's own file)")
        except OSError as e:
            out.append(f"could not restore {orig.name}: {e}")

    removed = 0
    for rel in plain:
        if rel in restored:
            continue                       # the game's file is back, keep it
        p = root / rel
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                out.append(f"could not remove {rel}: {e}")

    changed = config.remove_our_techniques(root)
    if changed:
        out.append("removed our techniques from ReShadePreset.ini")

    # Prune directories we created, only while they are empty.
    for d in (root / INCLUDE, root / SHADERS, root / TEXTURES,
              root / "reshade-shaders"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    try:
        (root / MANIFEST).unlink()
    except OSError:
        pass

    out.append(f"removed {removed} file(s), restored {len(restored)}")
    return out


# ----------------------------------------------------------------- status

def status(game_dir: Path) -> dict:
    """What is present in this folder right now."""
    root = Path(game_dir)
    man = read_manifest(root)
    present = {
        "ReShade": is_reshade(root / PROXY_DLL) or is_reshade(root / OPENGL_PROXY),
        "DLSS 5 add-on": (root / RENODX).is_file(),
        "nvngx_dlssnr": (root / DLSSNR).is_file(),
        "nvngx_dlss": (root / DLSS).is_file(),
        "bridge": (root / BRIDGE_ADDON).is_file(),
        "feeder": (root / FEEDER_ADDON).is_file(),
    }
    return {
        "installed": man is not None,
        "route": man.get("route") if man else None,
        "components": man.get("components", {}) if man else {},
        "complete": man.get("complete", False) if man else False,
        "present": present,
    }
