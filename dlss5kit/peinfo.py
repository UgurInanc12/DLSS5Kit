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
    # A settings dialog shipped beside the game. Measured on Far Cry (2004):
    # FarCryConfigurator.exe outscored FarCry.exe because it too matched the
    # folder name, so --check named a configurator as the game and read DX9
    # off it.
    "configurator", "config", "settings", "options",
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
    # CryEngine 1 ships its whole toolchain in Bin32/ beside the game.
    # Measured on Far Cry (2004): rc.exe (the resource compiler, 0.11 MB)
    # outranked FarCry.exe because "rc" is a substring of "farcry" and so
    # collected the folder-name bonus.
    "rc", "cgc", "cgfdump", "luacompiler", "fxc", "editor", "sandbox",
    "resourcecompiler", "lmtool", "polybump",
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

    # Helper libraries name an API in their FILE NAME without being the
    # renderer. Measured on Watch Dogs 2 2026-09-02: bin/ holds NVIDIA's
    # GameWorks helpers GFSDK_ShadowLib_DX11.win64.dll (2.5 MB) and
    # GFSDK_SSAO_D3D11.win64.dll (1.2 MB) beside the actual engine
    # Disrupt_64.dll (138.9 MB). Returning on the first name containing
    # "dx11" credited a shadow-mapping helper as the renderer - the right
    # answer for the wrong reason, and the wrong answer on any title whose
    # helper targets a different API than its renderer.
    helper = ("gfsdk_", "nvapi", "nvtt", "amd_ags", "d3dcompiler_",
              "d3dx9_", "d3dx10_", "d3dx11_", "nvcamera", "anselsdk",
              "physx", "apex_", "nvblast", "nvcloth", "nvtoolsext",
              "openvr_", "steamvr", "libovr", "dxil", "dxcompiler")
    engine_names = {n: p for n, p in names.items()
                    if not any(n.startswith(h) or h in n for h in helper)}

    # A module whose FILE NAME states the API is a strong hint, but only when
    # the module is plausibly the renderer. Prefer the largest such module so
    # a 30 MB engine outranks a 1 MB satellite.
    def _by_size(cands: list[str]) -> str | None:
        if not cands:
            return None
        try:
            return max(cands, key=lambda n: engine_names[n].stat().st_size)
        except OSError:
            return cands[0]

    d12 = _by_size([n for n in engine_names if "dx12" in n or "d3d12" in n])
    if d12:
        return DX12, f"a renderer module beside the executable is named {d12}"
    vkn = _by_size([n for n in engine_names
                    if "vulkan" in n or n.startswith("vk")])
    if vkn:
        return VULKAN, f"a renderer module beside the executable is named {vkn}"
    d11 = _by_size([n for n in engine_names if "dx11" in n or "d3d11" in n])
    if d11:
        return DX11, f"a renderer module beside the executable is named {d11}"

    # No module announces an API in its name. The engine-specific rules below
    # get first refusal; the generic biggest-module scan is the last resort.

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

    # Last resort: no name announced an API and no known engine runtime is
    # here, so read the biggest neighbouring module. Measured on Watch Dogs 2
    # 2026-09-02: Disrupt_64.dll (138.9 MB) imports d3d11.dll, d3d9.dll and
    # dxgi.dll and no d3d12 - evidence from the renderer itself rather than
    # from a satellite whose file name happens to carry an API.
    return _api_from_biggest_module(engine_names)


def _api_from_biggest_module(names: dict[str, Path]) -> tuple[str | None, str]:
    """Read the renderer out of the largest neighbouring DLL.

    The engine module is by far the biggest file beside the executable, and
    its own import table is real evidence. Only the Direct3D/Vulkan split is
    decided here: the import table is definitive about what the module links
    against, so counts are not needed.
    """
    if not names:
        return None, ""
    try:
        biggest = max(names.values(), key=lambda p: p.stat().st_size)
    except (OSError, ValueError):
        return None, ""
    try:
        if biggest.stat().st_size < 8 * 1024 * 1024:
            return None, ""          # too small to be an engine
    except OSError:
        return None, ""

    imports = pe_imports(biggest)
    has = lambda d: any(d in i for i in imports)
    if has("d3d12.dll"):
        return DX12, (f"the engine module beside the executable "
                      f"({biggest.name}) imports d3d12.dll")
    if has("d3d11.dll"):
        return DX11, (f"the engine module beside the executable "
                      f"({biggest.name}) imports d3d11.dll")
    if any(i.startswith("vulkan") for i in imports):
        return VULKAN, (f"the engine module beside the executable "
                        f"({biggest.name}) imports vulkan-1.dll")
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
    # Exact match, for the tiers where the CLAIM is "links against Microsoft's
    # own runtime". A game module named after the API satisfies a substring
    # test without being the API: measured on Far Cry 3 2026-09-02,
    # farcry3_d3d11.exe imports fc3_d3d11.dll (Ubisoft's own renderer) and
    # `"d3d11.dll" in "fc3_d3d11.dll"` is True, so the tool reported "imports
    # d3d11.dll statically" at HIGH confidence about a DLL Microsoft never
    # shipped. Same trap for d3d12.dll (e.g. amd_d3d12.dll, nv_d3d12.dll).
    imp_exact = lambda d: d in info.imports

    # Tier 2: static imports. A statically linked renderer is definitive.
    if imp_exact("d3d12.dll"):
        info.api, info.reason, info.confidence = DX12, "imports d3d12.dll statically", "high"
        return info
    if imp_exact("d3d11.dll"):
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
    # The folder-name bonus must mean something. A very short stem is a
    # substring of almost any title by accident: measured on Far Cry (2004),
    # "rc" (the CryEngine resource compiler) matched "farcry" and collected
    # the full bonus, outranking the game itself. Require the shorter side of
    # the comparison to be a substantial name, not two characters.
    if fn and sn and (sn in fn or fn in sn) and min(len(fn), len(sn)) >= 4:
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

    # Prefer a 64-bit executable when the folder offers both. CryEngine-era
    # games ship Bin32/ and Bin64/ with the SAME file name, and Crysis (2007)
    # additionally keeps a 32-bit Crysis.exe inside Bin64/ next to the real
    # 64-bit Crysis64.exe - so the path says nothing and only the PE header
    # settles it. Measured 2026-09-02: without this, Bin64\Crysis.exe (32-bit,
    # 9.4 MB) outscored Crysis64.exe (64-bit, 53 KB) purely on file size and
    # the game was routed as 32-bit.
    try:
        if exe_bitness(exe) == 64:
            s += 500
    except PEError:
        pass

    # Dedicated servers render nothing.
    if "dedicatedserver" in stem.replace("_", "") or stem.endswith("server"):
        s -= 1100
    try:
        mb = exe.stat().st_size / (1024 * 1024)
        s += min(mb, 300) * 1.2
    except OSError:
        pass
    s -= rel.count("/") * 15
    return s


def _api_selfevidence(exe: Path) -> int:
    """Tie-breaker: does this executable name its own renderer?

    Some publishers ship one thin shell per backend, identical in size and
    location, and only the import table tells them apart. Measured on Far Cry 3
    (2012), both 0.2 MB in bin/ and scored identically at 535.2:

        farcry3.exe          no API strings at all      -> Unknown [low]
        farcry3_d3d11.exe    imports d3d11.dll          -> DX11 [high]

    Directory order decided the winner, and it picked the D3D9 shell, whose own
    API then had to be guessed from a neighbouring DLL at medium confidence.
    Ranking modern-API self-evidence above silence keeps the decision on the
    binary rather than on the filesystem, and never overrides _score() - it
    only breaks exact ties.

    Matches are EXACT: a shell that imports the publisher's own fc3_d3d11.dll
    is not importing Microsoft's d3d11.dll, and must not be credited as if it
    were. What such a shell does prove is which RENDERER MODULE it pulls in,
    so that is scored separately and lower.
    """
    try:
        imports = pe_imports(exe)
    except Exception:      # noqa: BLE001 - ranking must never raise
        return 0
    if "d3d12.dll" in imports:
        return 6
    if "d3d11.dll" in imports:
        return 5
    if any(i.startswith("vulkan") for i in imports):
        return 4
    # The publisher's own renderer module, named after the API it implements.
    # Weaker evidence than a Microsoft import, stronger than nothing at all:
    # it is how Far Cry 3's two identical shells are told apart.
    if any("d3d12" in i or "dx12" in i for i in imports):
        return 3
    if any("d3d11" in i or "dx11" in i for i in imports):
        return 2
    return 0


def find_game_exes(folder: Path) -> list[Path]:
    """Candidate game executables, most likely first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    cands = _walk_exes(folder)
    if not cands:
        return []
    scored = sorted(cands, key=lambda p: (_score(p, folder), _api_selfevidence(p)),
                    reverse=True)
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
