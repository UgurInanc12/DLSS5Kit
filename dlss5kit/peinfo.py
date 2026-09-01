"""Windows PE inspection: bitness, imports, graphics API, NGX usage.

Parsed by hand with struct so there is nothing to audit but this file.

WHY THIS IS NOT JUST AN IMPORT-TABLE READER
-------------------------------------------
Reading only the static import table gets real games wrong. Measured on
Crysis 3 Remastered (2026-09-01):

    static imports : opengl32.dll, d3d10.dll, d3dcompiler_47.dll, vulkan-1.dll
    actual runtime : D3D11CreateDeviceAndSwapChain, feature level 11_0

The renderer DLL is picked with LoadLibrary at start-up, so it never appears
in the import table. An import-table-only detector calls this game "DX10" and
routes it to the feeder, which is the wrong install.

So the API is decided from three tiers of evidence, strongest first:

    1. ReShade.log in the game folder - it records the real device creation
       calls. This is ground truth when it exists.
    2. Static import table.
    3. DLL name strings inside the executable (the LoadLibrary targets).

The same string scan reveals which NGX entry points the game calls
(NVSDK_NGX_D3D11_* vs NVSDK_NGX_D3D12_*), which decides whether the DLSS 5
add-on can hook the game directly or the bridge is needed.
"""
from __future__ import annotations

import mmap
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

PE_X64 = 0x8664
PE_X86 = 0x014C

DX9, DX10, DX11, DX12, VULKAN, OPENGL, UNKNOWN = (
    "DX9", "DX10", "DX11", "DX12", "Vulkan", "OpenGL", "Unknown")

# Executables that are never the game itself.
_SKIP_PARTS = (
    "unins", "setup", "vcredist", "dxsetup", "dotnet", "prereq", "redist",
    "crashhandler", "crashreport", "crashpad", "easyanticheat", "battleye",
    "touchup", "installer", "activation", "cleanup", "helper", "webhelper",
    "unitycrashhandler", "ue4prereqsetup", "ue5prereqsetup", "epicwebhelper",
    "dxc", "shadercompile", "benchmark", "launcher",
)

_PRUNE_DIRS = {
    "_commonredist", "commonredist", "redist", "redistributable", "redistributables",
    "directx", "dotnet", "vcredist", "vc_redist", "easyanticheat", "easyanticheat_eos",
    "battleye", "punkbuster", "installers", "installer", "prerequisites", "prereq",
    "support", "docs", "manual", "soundtrack", "artbook", "extras", "dxsetup",
    "crashreportclient", "epicwebhelper", "thirdparty", "steamvr", "openvr",
    "__installer", "dotnetfx", "movies", "content", "data", "assets", "textures",
    "reshade-shaders", "logbackups",
}
_MAX_DEPTH = 5


class PEError(Exception):
    pass


# --------------------------------------------------------------- basic PE

def exe_bitness(path: Path) -> int:
    """32 or 64, from the COFF header Machine field. Reads a few bytes only."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                raise PEError("Not a Windows executable (no MZ signature).")
            (off,) = struct.unpack_from("<I", head, 0x3C)
            f.seek(off)
            sig = f.read(6)
            if len(sig) < 6 or sig[:4] != b"PE\0\0":
                raise PEError("No PE header found.")
            (machine,) = struct.unpack_from("<H", sig, 4)
    except OSError as e:
        raise PEError(f"Cannot read {path.name}: {e}") from e
    if machine == PE_X64:
        return 64
    if machine == PE_X86:
        return 32
    raise PEError(f"Unsupported machine type: 0x{machine:04x}")


def pe_imports(path: Path) -> list[str]:
    """Lower-cased DLL names from the static import table, [] if unreadable.

    Only the small regions actually needed are read: some game executables are
    hundreds of megabytes and the import table can sit near the end.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            def at(offset: int, n: int) -> bytes:
                if offset < 0 or offset >= size:
                    return b""
                f.seek(offset)
                return f.read(n)

            head = at(0, 0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return []
            (pe,) = struct.unpack_from("<I", head, 0x3C)
            if at(pe, 4) != b"PE\0\0":
                return []
            coff = at(pe + 4, 20)
            if len(coff) < 20:
                return []
            n_sections = struct.unpack_from("<H", coff, 2)[0]
            opt_size = struct.unpack_from("<H", coff, 16)[0]
            opt = pe + 24

            magic_b = at(opt, 2)
            if len(magic_b) < 2:
                return []
            magic = struct.unpack_from("<H", magic_b, 0)[0]
            if magic == 0x20B:
                dd = 112       # PE32+
            elif magic == 0x10B:
                dd = 96        # PE32
            else:
                return []

            imp = at(opt + dd + 8, 4)
            if len(imp) < 4:
                return []
            import_rva = struct.unpack_from("<I", imp, 0)[0]
            if import_rva == 0:
                return []

            sec_raw = at(opt + opt_size, n_sections * 40)
            sections = []
            for i in range(min(n_sections, len(sec_raw) // 40)):
                vsize, vaddr, rawsize, rawptr = struct.unpack_from(
                    "<IIII", sec_raw, i * 40 + 8)
                sections.append((vaddr, max(vsize, rawsize), rawptr))

            def to_off(rva: int) -> int | None:
                for vaddr, vlen, rawptr in sections:
                    if vaddr <= rva < vaddr + vlen:
                        return rawptr + (rva - vaddr)
                return None

            desc = to_off(import_rva)
            if desc is None:
                return []

            table = at(desc, 20 * 1024)
            names: list[str] = []
            for i in range(len(table) // 20):
                name_rva, first_thunk = struct.unpack_from("<II", table, i * 20 + 12)
                if name_rva == 0 and first_thunk == 0:
                    break
                n_off = to_off(name_rva)
                if n_off is None:
                    continue
                blob = at(n_off, 256)
                end = blob.find(b"\0")
                if end > 0:
                    names.append(blob[:end].decode("ascii", "ignore").lower())
            return names
    except Exception:
        return []


# ------------------------------------------------------------ string scan

_SCAN_TOKENS = {
    "d3d12": b"d3d12.dll",
    "d3d11": b"d3d11.dll",
    "d3d10": b"d3d10.dll",
    "d3d9": b"d3d9.dll",
    "vulkan": b"vulkan-1.dll",
    "opengl": b"opengl32.dll",
    "dxgi": b"dxgi.dll",
    "ngx_d3d12": b"NVSDK_NGX_D3D12",
    "ngx_d3d11": b"NVSDK_NGX_D3D11",
    "ngx_vk": b"NVSDK_NGX_VULKAN",
    "streamline": b"sl.interposer",
    "nvngx": b"nvngx",
}


def scan_strings(path: Path) -> dict[str, int]:
    """Count marker strings inside the executable.

    Catches renderers and NGX entry points reached through LoadLibrary /
    GetProcAddress, which the import table cannot show. Memory-mapped, so a
    500 MB executable costs no resident memory.
    """
    counts = {k: 0 for k in _SCAN_TOKENS}
    try:
        with open(path, "rb") as fh:
            try:
                data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                data = fh.read()
            try:
                for key, tok in _SCAN_TOKENS.items():
                    off = 0
                    n = 0
                    while n < 1000:
                        i = data.find(tok, off)
                        if i < 0:
                            break
                        n += 1
                        off = i + len(tok)
                    counts[key] = n
            finally:
                if hasattr(data, "close"):
                    data.close()
    except OSError:
        pass
    return counts


# ----------------------------------------------------------- ReShade log

_LOG_D3D12 = re.compile(r"D3D12CreateDevice")
_LOG_D3D11 = re.compile(r"D3D11CreateDevice")
_LOG_D3D10 = re.compile(r"D3D10CreateDevice")
_LOG_VK = re.compile(r"vkCreateDevice|Vulkan")


def api_from_reshade_log(folder: Path) -> tuple[str | None, str]:
    """Read the real device-creation calls out of an existing ReShade.log.

    Ground truth when it exists: the log records what the game actually did,
    not what the executable might do. Only the tail is read - the log grows
    with every shader compile line and the device calls are near the start of
    each run, so the tail of a long log is read whole but bounded.

    ONE TRAP: our own bridge add-on creates a PRIVATE D3D12 device, and that
    call lands in this same log. Measured on Crysis 3 Remastered after a
    bridge install: 12 D3D11CreateDevice calls (the game) and 1
    D3D12CreateDevice call (the bridge). Taking "any D3D12 call" as proof of a
    D3D12 game then flips the verdict to native on the next inspection and
    recommends the very route that was already proven not to work.

    So the counts are compared rather than checked for presence, and a lone
    D3D12 call against a clear majority of D3D11 calls is attributed to the
    bridge.
    """
    log = folder / "ReShade.log"
    if not log.is_file():
        return None, ""
    try:
        size = log.stat().st_size
        with open(log, "rb") as f:
            if size > 4_000_000:
                f.seek(size - 4_000_000)
            text = f.read().decode("utf8", "replace")
    except OSError:
        return None, ""

    n12 = len(_LOG_D3D12.findall(text))
    n11 = len(_LOG_D3D11.findall(text))
    n10 = len(_LOG_D3D10.findall(text))

    # A private D3D12 session opened by dlss5-bridge, not by the game.
    bridge_present = ((folder / "dlss5-bridge.addon64").is_file()
                      or "dlss5-bridge" in text.lower())
    if n12 and n11 > n12:
        why = (f"ReShade.log records {n11} D3D11CreateDevice call(s) against "
               f"{n12} D3D12")
        if bridge_present:
            why += " (the D3D12 one is dlss5-bridge's private session)"
        return DX11, why
    if n12:
        return DX12, f"ReShade.log records {n12} D3D12CreateDevice call(s)"
    if n11:
        return DX11, f"ReShade.log records {n11} D3D11CreateDevice call(s)"
    if n10:
        return DX10, f"ReShade.log records {n10} D3D10CreateDevice call(s)"
    if "Vulkan" in text and "vkCreateDevice" in text:
        return VULKAN, "ReShade.log records Vulkan device creation"
    return None, ""


# ------------------------------------------------------------ API verdict

@dataclass
class ApiInfo:
    api: str = UNKNOWN
    reason: str = ""
    confidence: str = "low"          # high | medium | low
    ngx_d3d11: bool = False
    ngx_d3d12: bool = False
    ngx_vulkan: bool = False
    streamline: bool = False
    nvngx: bool = False
    imports: list[str] = field(default_factory=list)
    strings: dict[str, int] = field(default_factory=dict)

    @property
    def uses_ngx(self) -> bool:
        """Does the executable reference NVIDIA's NGX layer at all?

        False on a game that merely has DLSS DLLs sitting in its folder
        because somebody copied them there.
        """
        return (self.ngx_d3d11 or self.ngx_d3d12 or self.ngx_vulkan
                or self.streamline or self.nvngx)


def detect_api(exe: Path, folder: Path | None = None) -> ApiInfo:
    """Work out the graphics API, using the strongest evidence available."""
    info = ApiInfo()
    info.imports = pe_imports(exe)
    info.strings = scan_strings(exe)
    s = info.strings
    info.ngx_d3d11 = s.get("ngx_d3d11", 0) > 0
    info.ngx_d3d12 = s.get("ngx_d3d12", 0) > 0
    info.ngx_vulkan = s.get("ngx_vk", 0) > 0
    info.streamline = s.get("streamline", 0) > 0
    info.nvngx = s.get("nvngx", 0) > 0

    # Tier 1: what the game actually did last time it ran.
    if folder is not None:
        api, why = api_from_reshade_log(folder)
        if api:
            info.api, info.reason, info.confidence = api, why, "high"
            return info

    has_imp = lambda d: any(d in i for i in info.imports)

    # Tier 2: static imports. A statically linked renderer is definitive.
    if has_imp("d3d12.dll"):
        info.api, info.reason, info.confidence = DX12, "imports d3d12.dll statically", "high"
        return info
    if has_imp("d3d11.dll"):
        info.api, info.reason, info.confidence = DX11, "imports d3d11.dll statically", "high"
        return info

    # Tier 3: LoadLibrary targets. A game that statically imports d3d10 but
    # carries a d3d11.dll string is a D3D11 game with a legacy import left in
    # (Crysis 3 Remastered is exactly this).
    if s.get("d3d12", 0) and not s.get("d3d11", 0):
        info.api, info.reason, info.confidence = (
            DX12, "loads d3d12.dll at runtime (string table)", "medium")
        return info
    if s.get("d3d11", 0):
        extra = " (a legacy d3d10.dll import is present but unused)" if has_imp("d3d10.dll") else ""
        info.api, info.reason, info.confidence = (
            DX11, f"loads d3d11.dll at runtime (string table){extra}", "medium")
        return info
    if s.get("d3d12", 0):
        info.api, info.reason, info.confidence = (
            DX12, "loads d3d12.dll at runtime (string table)", "medium")
        return info

    if has_imp("d3d10.dll") or has_imp("d3d10_1.dll") or has_imp("d3d10core.dll"):
        info.api, info.reason, info.confidence = DX10, "imports d3d10.dll statically", "medium"
        return info
    if has_imp("dxgi.dll") or s.get("dxgi", 0):
        info.api, info.reason, info.confidence = (
            DX11, "uses DXGI without a static d3d11/d3d12 import", "low")
        return info
    if has_imp("vulkan-1.dll") or s.get("vulkan", 0):
        info.api, info.reason, info.confidence = VULKAN, "uses vulkan-1.dll, no DXGI", "medium"
        return info
    if has_imp("opengl32.dll") or s.get("opengl", 0):
        info.api, info.reason, info.confidence = OPENGL, "uses opengl32.dll, no DXGI", "medium"
        return info
    if has_imp("d3d9.dll") or s.get("d3d9", 0):
        info.api, info.reason, info.confidence = DX9, "uses d3d9.dll, no DXGI", "medium"
        return info

    info.api, info.reason, info.confidence = (
        UNKNOWN, "no graphics API could be identified", "low")
    return info


# ------------------------------------------------------- exe discovery

def looks_like_game(exe: Path) -> bool:
    low = exe.name.lower()
    return not any(p in low for p in _SKIP_PARTS)


def _walk_exes(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    found: list[Path] = []
    base_depth = len(folder.parts)
    for root, dirs, files in os.walk(folder, topdown=True):
        rp = Path(root)
        depth = len(rp.parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs
                       if d.lower() not in _PRUNE_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".exe"):
                found.append(rp / f)
        if len(found) > 400:
            break
    return found


def _score(exe: Path, folder: Path) -> float:
    try:
        rel = str(exe.relative_to(folder)).lower().replace("\\", "/")
    except ValueError:
        rel = exe.name.lower()
    stem = exe.stem.lower()
    s = 0.0
    if stem.endswith("-shipping") or "shipping" in stem:
        s += 1000
    if "/binaries/win64/" in rel or "/bin/win64/" in rel or rel.startswith("binaries/win64/"):
        s += 400
    elif "/binaries/win32/" in rel or "/bin/win32/" in rel:
        s += 300
    elif "/bin/" in rel or rel.startswith("bin/"):
        s += 200
    if "/engine/binaries/" in rel or rel.startswith("engine/binaries/"):
        s -= 900
    fn = re.sub(r"[^a-z0-9]", "", folder.name.lower())
    sn = re.sub(r"[^a-z0-9]", "", stem)
    if fn and sn and (sn in fn or fn in sn):
        s += 350
    if exe.parent == folder:
        s += 120
    if not looks_like_game(exe):
        s -= 1500
    try:
        mb = exe.stat().st_size / (1024 * 1024)
        s += min(mb, 300) * 1.2
    except OSError:
        pass
    s -= rel.count("/") * 15
    return s


def find_game_exes(folder: Path) -> list[Path]:
    """Candidate game executables, most likely first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    cands = _walk_exes(folder)
    if not cands:
        return []
    scored = sorted(cands, key=lambda p: _score(p, folder), reverse=True)
    good = [p for p in scored if _score(p, folder) > -500]
    return good or scored


def resolve_target(target: Path) -> tuple[Path, list[Path]]:
    """Accept an .exe or a folder; return (chosen exe, all candidates)."""
    target = Path(target)
    if target.is_file() and target.suffix.lower() == ".exe":
        return target, [target]
    if target.is_dir():
        cands = find_game_exes(target)
        if not cands:
            raise PEError(f"No .exe found in {target}.")
        return cands[0], cands
    raise PEError(f"{target} not found.")
