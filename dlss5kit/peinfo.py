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
    "dxc", "shadercompile", "benchmark", "launcher", "downloader", "updater",
    "patcher", "repair", "diagnostic", "reporter", "bugtrap", "jirabugtrap",
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

# Engine SDK tools that ship inside a game install and are not the game.
# Source engine puts its map compilers in bin/, which the bin/ score bonus
# would otherwise promote above the real executable (Half-Life 2 RTX, 2026-09).
_ENGINE_TOOLS = {
    "studiomdl", "studiomdl_modified", "vbsp", "vbspinfo", "vrad", "vvis",
    "hammer", "hammerplusplus", "hammer_run_map_launcher", "propper",
    "hammerplusplus_compiler", "glview", "height2ssbump", "vtex", "vtf2tga",
    "captioncompiler", "motionmapper", "qc_eyes", "phonemeextractor",
}


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

# Entry-point names, which are exported symbols and therefore exact. A game
# that resolves its renderer with GetProcAddress carries these even when it
# never stores the DLL file name in a form the token scan above would match.
#
# Measured on Metro Exodus Enhanced Edition 2026-09-01: the executable
# contains ZERO occurrences of "d3d12.dll" or "dxgi.dll" in any case, because
# it stores the module name as the bare uppercase string "D3D12" (26 times).
# A scan for lower-case file names alone reports "no graphics API could be
# identified" for a game that is D3D12-only, and the route then falls through
# to the D3D11 default - which is wrong twice over.
_SCAN_ENTRYPOINTS = {
    "d3d12": (b"D3D12CreateDevice",),
    "d3d11": (b"D3D11CreateDevice", b"D3D11CreateDeviceAndSwapChain"),
    "d3d10": (b"D3D10CreateDevice",),
    "d3d9": (b"Direct3DCreate9",),
    "dxgi": (b"CreateDXGIFactory",),
    "vulkan": (b"vkCreateInstance", b"vkGetInstanceProcAddr"),
    "opengl": (b"wglCreateContext",),
}

# Bare module names, matched case-insensitively as a last resort. Only the
# forms that carry a delimiter, because a naked "d3d11" matches things that
# are not renderer references at all.
#
# Measured on Metro Exodus Enhanced Edition 2026-09-01: a case-insensitive
# search for "d3d11" hits twice, and BOTH hits are inside NGX parameter names:
#     NVSDK_NGX_Parameter_GetD3d11Resource
#     NVSDK_NGX_Parameter_SetD3d11Resource
# Those strings ship with every NGX-using game regardless of its renderer, so
# counting them as D3D11 evidence made a D3D12-only game report as D3D11.
_SCAN_BARE = {
    "d3d12": (b"\x00d3d12\x00", b"d3d12.dll"),
    "d3d11": (b"\x00d3d11\x00", b"d3d11.dll"),
    "d3d10": (b"\x00d3d10\x00", b"d3d10.dll"),
    "vulkan": (b"vulkan-1",),
    "opengl": (b"opengl32",),
}


def _count(data, token: bytes, limit: int = 1000) -> int:
    n, off = 0, 0
    while n < limit:
        i = data.find(token, off)
        if i < 0:
            break
        n += 1
        off = i + len(token)
    return n


def scan_strings(path: Path) -> dict[str, int]:
    """Count renderer and NGX markers inside the executable.

    Catches renderers reached through LoadLibrary / GetProcAddress, which the
    import table cannot show. Three tiers, because game executables spell
    these differently:

        1. lower-case DLL file names   ("d3d12.dll")
        2. exported entry points       ("D3D12CreateDevice") - exact, reliable
        3. bare module names, any case ("D3D12") - last resort

    Memory-mapped, so a 500 MB executable costs no resident memory.
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
                    counts[key] = _count(data, tok)

                # Tier 2: entry points. Their presence is proof the API is used.
                for key, toks in _SCAN_ENTRYPOINTS.items():
                    hits = sum(_count(data, t) for t in toks)
                    if hits:
                        counts[key] = counts.get(key, 0) + hits

                # Tier 3: bare names, case-insensitive, only where still zero.
                need = [k for k in _SCAN_BARE if not counts.get(k)]
                if need:
                    try:
                        lower = bytes(data[:]).lower()
                    except Exception:
                        lower = b""
                    for key in need:
                        counts[key] = sum(_count(lower, t) for t in _SCAN_BARE[key])
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


def api_from_neighbour_dlls(folder: Path) -> tuple[str | None, str]:
    """Infer the renderer from the DLLs shipped beside the executable.

    Some engines put the whole renderer in a separate module and leave the
    executable a thin launcher. Measured on this workstation 2026-09-01:

        Bills Must Be Paid   exe 0.7 MB, renderer inside UnityPlayer.dll
        PlagueIncEvolved     exe 0.7 MB, renderer inside UnityPlayer.dll
        Battlefield 6        Engine.Render.Core2.PlatformPcDx12.retail.dll

    Scanning the executable alone reports "no graphics API could be
    identified" for all three, so the neighbours are consulted before giving
    up. Named engine modules are checked first, then any DLL whose own name
    announces the API.
    """
    if not folder or not folder.is_dir():
        return None, ""

    names = {}
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() == ".dll":
                names[p.name.lower()] = p
    except OSError:
        return None, ""

    # A module whose FILE NAME states the API is the strongest hint here.
    for n in names:
        if "dx12" in n or "d3d12" in n:
            return DX12, f"a renderer module beside the executable is named {n}"
        if "vulkan" in n or n.startswith("vk"):
            return VULKAN, f"a renderer module beside the executable is named {n}"
    for n in names:
        if "dx11" in n or "d3d11" in n:
            return DX11, f"a renderer module beside the executable is named {n}"

    # Engine runtimes: scan the module itself, it holds the real strings.
    #
    # CAUTION: a general-purpose engine module contains EVERY backend it can
    # use, so presence alone proves nothing. Measured on PlagueInc's
    # UnityPlayer.dll 2026-09-01: d3d11 x79, d3d12 x103, vulkan-1 x1. An
    # early "if vulkan: return VULKAN" reported Vulkan for a game that runs
    # D3D11 by default. So the counts are weighed, DXGI decides between the
    # two Direct3D versions, and Vulkan only wins when it clearly dominates.
    for engine in ("unityplayer.dll", "gameassembly.dll"):
        if engine not in names:
            continue
        s = scan_strings(names[engine])
        d11, d12 = s.get("d3d11", 0), s.get("d3d12", 0)
        vk, dxgi = s.get("vulkan", 0), s.get("dxgi", 0)
        direct3d = d11 + d12 + dxgi
        # Compare Direct3D as a whole against Vulkan: a single "vulkan-1"
        # string outnumbering "d3d11" alone is not evidence of a Vulkan game
        # when DXGI and D3D12 are also present (measured: Bills Must Be Paid
        # has d3d11 x2, d3d12 x1, dxgi x5, vulkan x3 - Direct3D 8 to 3).
        if direct3d and direct3d >= vk:
            api = DX12 if d12 > d11 * 2 else DX11
            return api, (f"{engine} carries every backend "
                         f"(Direct3D x{direct3d}, Vulkan x{vk}); "
                         f"Direct3D is the Windows default")
        if vk:
            return VULKAN, f"{engine} is predominantly Vulkan (x{vk})"
    return None, ""


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

    # Tier 3: LoadLibrary targets found in the string table.
    #
    # The NGX entry point is checked FIRST here, because it is an exact,
    # unambiguous symbol while the module-name strings are fuzzy.
    # NVSDK_NGX_D3D12_* can only be called by a D3D12 renderer.
    if info.ngx_d3d12 and not info.ngx_d3d11:
        info.api, info.reason, info.confidence = (
            DX12,
            f"calls NVSDK_NGX_D3D12_* {s.get('ngx_d3d12', 0)} time(s), which "
            f"only a D3D12 renderer can do",
            "high")
        return info
    if info.ngx_d3d11 and not info.ngx_d3d12:
        info.api, info.reason, info.confidence = (
            DX11,
            f"calls NVSDK_NGX_D3D11_* {s.get('ngx_d3d11', 0)} time(s)",
            "high")
        return info

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

    # Tier 4: the executable said nothing. Some engines keep the renderer in a
    # separate module (Unity, and engines with a named PlatformPcDx12 DLL), so
    # ask the neighbours before reporting Unknown.
    if folder is not None:
        api, why = api_from_neighbour_dlls(folder)
        if api:
            info.api, info.reason, info.confidence = api, why, "medium"
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


def remix_bridge(folder: Path) -> Path | None:
    """The 64-bit renderer of an RTX Remix game, if this is one.

    RTX Remix runs a 32-bit game process that hands rendering to a separate
    64-bit process: bin/.trex/NvRemixBridge.exe, with the real renderer in
    bin/.trex/d3d9.dll (a dxvk-remix build that draws with Vulkan). Measured
    on Half-Life 2 RTX 2026-09-02: hl2.exe is 32-bit, NvRemixBridge.exe is
    64-bit, and .trex/d3d9.dll references NVSDK_NGX_VULKAN 43 times and
    D3D12 zero times, alongside its own nvngx_dlss/dlssd/dlssg runtimes.

    This matters because judging such a game by its main executable calls it
    "32-bit, not supported" when the process that would host an injected
    add-on is 64-bit. It is still not installable - see route selection - but
    the tool must say WHY correctly.
    """
    folder = Path(folder)
    for rel in ("bin/.trex/NvRemixBridge.exe", ".trex/NvRemixBridge.exe"):
        p = folder / rel
        if p.is_file():
            return p
    return None


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

    # Engine SDK tools that ship beside the game. Measured on Half-Life 2 RTX:
    # bin/ holds studiomdl, vbsp, vrad, vvis, hammer and propper - Source's map
    # compilers - and the bin/ bonus above floated them over the real hl2.exe
    # in the root, so --check reported a 1.8 MB map compiler as "the game" and
    # refused the install as 32-bit-only. These names are never a game.
    if stem in _ENGINE_TOOLS:
        s -= 1200
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
