"""Deciding how DLSS 5 gets into a given game.

There are three routes and picking the wrong one produces a silent failure:
the game starts, nothing crashes, and it simply looks unchanged.

    NATIVE      The DLSS 5 add-on detours the game's own NGX D3D12 calls.
                Needs: D3D12 renderer AND the game calls NVSDK_NGX_D3D12_*.
                Best quality, and the game's own Quality/Balanced/Performance
                setting still applies.

    BRIDGE      dlss5-bridge reproduces the DLSS contract on a private D3D12
                session. For D3D11 and Vulkan games - with or without DLSS of
                their own.

    FEEDER      DLSS5-Feeder builds a synthetic DLAA contract from ReShade's
                depth buffer and shader-estimated motion vectors. Works
                anywhere ReShade attaches, but is ALWAYS DLAA, never
                upscaling, and costs the most.

TWO MISTAKES THIS MODULE EXISTS TO AVOID
----------------------------------------
1. "The game ships DLSS, therefore use the native route."
   Measured on Crysis 3 Remastered: the folder holds the full Streamline set,
   so every naive detector says native. But the executable carries
   NVSDK_NGX_D3D11 27 times and NVSDK_NGX_D3D12 once, ReShade.log shows 12
   D3D11CreateDevice calls and zero D3D12CreateDevice calls, and the game's
   own log ends with:
       Failed to NVSDK_NGX_D3D11_CreateFeature ... dlaa = 0xbad00005
   The add-on hooked the D3D12 entry point and waited for a call that never
   came. What decides the route is the NGX entry point the game actually
   calls, not the presence of DLSS files.

2. "sl.dlss.dll is in the folder, therefore the game has DLSS."
   Measured on Batman: Arkham Knight (a 2015 game that predates DLSS by three
   years): the folder held the whole Streamline set because the user had
   copied it there by hand while experimenting. The executable contains zero
   occurrences of NVSDK_NGX, nvngx, DLSS or streamline. Loose DLLs are
   evidence somebody copied files, not evidence the game calls them - so the
   executable gets a veto over the folder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gpu, peinfo

NATIVE, BRIDGE, FEEDER = "native", "bridge", "feeder"

LABELS = {
    NATIVE: "native - hook the game's own D3D12 DLSS",
    BRIDGE: "bridge - private D3D12 session",
    FEEDER: "feeder - synthetic DLAA contract from ReShade",
}

BLURB = {
    NATIVE: ("The add-on hooks the game's own NGX calls. Nothing synthetic, "
             "cheapest, and your in-game DLSS quality mode still applies."),
    BRIDGE: ("The bridge mirrors the DLSS contract onto a private D3D12 "
             "session. The only route for D3D11 and Vulkan games."),
    FEEDER: ("ReShade's depth buffer plus shader motion vectors feed a "
             "synthetic contract. Always DLAA, never upscaling, and the most "
             "expensive of the three."),
}

# NVIDIA's own plugin layer. A game shipping these ships DLSS - but only when
# the executable actually references NGX (see detect_native_dlss).
STREAMLINE = ("sl.interposer.dll", "sl.dlss.dll", "sl.common.dll",
              "sl.dlss_g.dll", "sl.reflex.dll", "sl.dlss_nr.dll")
# A game's own runtime, including one a user renamed to disable it.
OWN_RUNTIME = ("nvngx_dlss.dlsss", "nvngx_dlss.dll.bak", "_nvngx.dll")


@dataclass
class Plan:
    route: str = FEEDER
    reason: str = ""
    options: list[str] = field(default_factory=list)
    native_dlss: bool = False
    dlss_evidence: list[str] = field(default_factory=list)
    dlss_note: str = ""
    supported: bool = True
    blocker: str = ""
    warnings: list[str] = field(default_factory=list)


def _ours(folder: Path, name: str) -> bool:
    """Is this file one WE installed, rather than the game's own?"""
    from . import installer
    if (folder / (name + installer.BACKUP_SUFFIX)).is_file():
        return True
    man = folder / installer.MANIFEST
    if man.is_file():
        try:
            import json
            data = json.loads(man.read_text(encoding="utf8"))
            return name in data.get("files", [])
        except Exception:
            return False
    return False


def detect_native_dlss(folder: Path,
                       api: peinfo.ApiInfo | None = None
                       ) -> tuple[bool, list[str], str]:
    """Does the game ship DLSS of its own? (present, evidence, note)

    Loose DLLs alone are NOT proof. A user experimenting with these tools
    copies the Streamline set into a game folder, and a folder-only check then
    reports DLSS in a game released years before DLSS existed. When the
    executable is available and references no NGX entry point at all, the
    executable wins: the files are somebody's leftovers.
    """
    evidence: list[str] = []
    for m in STREAMLINE + OWN_RUNTIME:
        if (folder / m).is_file():
            evidence.append(m)
    if (folder / "nvngx_dlss.dll").is_file() and not _ours(folder, "nvngx_dlss.dll"):
        evidence.append("nvngx_dlss.dll")
    evidence = sorted(set(evidence))

    if not evidence:
        return False, [], ""

    if api is not None and not (api.ngx_d3d11 or api.ngx_d3d12 or api.ngx_vulkan
                                or api.streamline or api.nvngx):
        return False, evidence, (
            f"{len(evidence)} DLSS file(s) are in this folder, but the "
            f"executable references no NGX entry point at all - it does not "
            f"call DLSS. These are leftovers from a manual install, not the "
            f"game's own DLSS, and they are ignored when choosing the route.")

    return True, evidence, ""


def choose(folder: Path, api: peinfo.ApiInfo, bitness: int,
           card: "gpu.Card | None" = None) -> Plan:
    """Pick the route, and say why in words a person can act on."""
    p = Plan()
    p.native_dlss, p.dlss_evidence, p.dlss_note = detect_native_dlss(folder, api)

    if card is not None and not card.supported:
        p.supported = False
        p.blocker = gpu.card_supported(card.sm)[1]
        return p

    if bitness != 64:
        # 32-bit is supported ONLY through the feeder's cross-process host:
        # NGX exists as x64 only, and 32-bit ReShade loads .addon32 only, so
        # neither the DLSS 5 add-on nor the bridge can live in the game. The
        # feeder splits at the shared-NT-handle seam it already uses between
        # devices: dlss5-feed.addon32 in the game copies frames into shared
        # textures, and host64\dlss5-feed-host64.exe (a full 64-bit ReShade +
        # renodx stack of its own) does the DLSS work. Upstream lists it as
        # beta with Splinter Cell: Blacklist and BioShock Remastered verified.
        if api.api in (peinfo.DX11, peinfo.DX12):
            p.route = FEEDER
            p.supported = True
            p.reason = ("32-bit game: the feeder runs its DLSS work in a "
                        "separate 64-bit helper process (host64\\), because "
                        "NGX and the DLSS 5 add-on are 64-bit only. Frames "
                        "cross the process boundary as shared GPU textures, "
                        "not through system memory.")
            p.options = []
            p.warnings.append(
                "The 32-bit path is upstream beta. The helper opens its own "
                "window titled \"32-bit DLSS 5 Feeder\" - that is where the "
                "DLSS 5 add-on's own panel lives; press Home there.")
            return p

        p.supported = False
        p.blocker = ("32-bit games are only supported on D3D11 or D3D12, "
                     f"through the feeder's 64-bit helper process. This one "
                     f"reports {api.api}, which has no path: the DLSS 5 "
                     "add-on, the bridge and the NGX runtimes are all 64-bit "
                     "and D3D11/D3D12 only.")
        # RTX Remix is a special case worth naming, because the plain
        # "32-bit" answer is misleading: the game process is 32-bit but the
        # process that actually renders is a separate 64-bit one, and the
        # title already ships DLSS. Users otherwise assume the tool simply
        # failed to look properly. Measured on Half-Life 2 RTX 2026-09-02.
        if folder is not None and peinfo.remix_bridge(folder) is not None:
            p.blocker = (
                "This is an RTX Remix title. The game executable is 32-bit, "
                "but rendering happens in a separate 64-bit process "
                "(bin\\.trex\\NvRemixBridge.exe) whose renderer "
                "(bin\\.trex\\d3d9.dll, a dxvk-remix build) draws with "
                "Vulkan and ships its own DLSS runtimes - it references "
                "NVSDK_NGX_VULKAN and never D3D12.\n\n"
                "Nothing here can be installed into that: ReShade would have "
                "to attach to the bridge process rather than the game, the "
                "DLSS 5 add-on only detours D3D12 NGX calls, and Remix "
                "already performs its own path-traced rendering with DLSS "
                "and Ray Reconstruction. Use the game's own DLSS settings "
                "instead.")
        return p

    if api.api in (peinfo.DX9, peinfo.DX10):
        p.supported = False
        p.blocker = (f"{api.api} is not supported. Nothing hooks it, and the "
                     f"translation layers needed to reach it are out of scope.")
        return p

    if api.api == peinfo.OPENGL:
        p.route = FEEDER
        p.options = [FEEDER]
        p.reason = ("OpenGL: the add-on and the bridge both hook NGX's "
                    "D3D11/D3D12/Vulkan entry points and reach none of them "
                    "here. Only the feeder's synthetic contract can work.")
        p.warnings.append("OpenGL is a long shot - expect it not to work.")
        return p

    if api.api == peinfo.VULKAN:
        p.route = BRIDGE
        p.options = [BRIDGE]
        p.reason = ("Vulkan: the bridge mirrors the DLSS contract onto a "
                    "private D3D12 session. This is the only route that "
                    "works, and ReShade must be registered as a Vulkan layer, "
                    "which is a system-wide change.")
        p.warnings.append(
            "ReShade reaches Vulkan as an implicit layer: once registered it "
            "loads into EVERY Vulkan application on this account, not just "
            "this game. Uninstall removes the registration again.")
        return p

    if api.api == peinfo.DX12:
        # The decisive question is not "does it have DLSS" but "which NGX
        # entry point does it call". Only D3D12 NGX can be hooked directly.
        if api.ngx_d3d12:
            p.route = NATIVE
            p.options = [NATIVE, BRIDGE, FEEDER]
            p.reason = ("D3D12 renderer and the executable calls "
                        "NVSDK_NGX_D3D12_*, so the add-on can hook the game's "
                        "own DLSS directly. Your in-game DLSS quality setting "
                        "still applies.")
        elif p.native_dlss and api.ngx_d3d11:
            p.route = BRIDGE
            p.options = [BRIDGE, FEEDER]
            p.reason = ("The renderer is D3D12 but the game's DLSS calls go "
                        "through NVSDK_NGX_D3D11_*, which the add-on does not "
                        "hook. The bridge covers that.")
        else:
            p.route = FEEDER
            p.options = [FEEDER, BRIDGE]
            p.reason = ("D3D12 without DLSS of its own: the feeder builds a "
                        "DLAA contract from ReShade's depth and shader motion "
                        "vectors. The bridge can build one from the driver's "
                        "optical flow engine instead - newer, fewer moving "
                        "parts, less proven.")
        return p

    if api.api == peinfo.UNKNOWN:
        # Nothing identified the renderer. Do not silently take the D3D11
        # branch: say so, and prefer the feeder, which is the only route that
        # does not depend on knowing the API (it rides ReShade's own output).
        p.route = FEEDER
        p.options = [FEEDER, BRIDGE, NATIVE]
        p.reason = ("The graphics API could not be identified from the "
                    "executable, so no route can be chosen on evidence. The "
                    "feeder is the safest default because it works wherever "
                    "ReShade attaches at all, whatever the renderer.")
        p.warnings.append(
            "Run the game once with this installed, then press 'Did it work?' "
            "or re-inspect: ReShade.log will then record the real device calls "
            "and the correct route can be chosen.")
        return p

    # DX11 or an identified DXGI renderer.
    if p.native_dlss:
        p.route = BRIDGE
        p.options = [BRIDGE, FEEDER]
        p.reason = ("This game has its own DLSS but renders with D3D11, which "
                    "the DLSS 5 add-on cannot hook - it only detours D3D12 "
                    "NGX calls. The bridge reproduces the contract on a "
                    "private D3D12 session and the game's own DLSS quality "
                    "mode still applies.")
        if api.ngx_d3d11:
            p.reason += (f" Confirmed: the executable references "
                         f"NVSDK_NGX_D3D11 "
                         f"{api.strings.get('ngx_d3d11', 0)} time(s).")
    else:
        # No DLSS of its own on D3D11. The feeder is the proven route: the
        # bridge's synthetic path relies on the driver's optical flow engine
        # and is far less tested on older titles.
        p.route = FEEDER
        p.options = [FEEDER, BRIDGE]
        p.reason = ("This game has no DLSS of its own, so there is no contract "
                    "to hook or mirror - one has to be built. The feeder does "
                    "that from ReShade's depth buffer and shader-estimated "
                    "motion vectors. This is always DLAA (anti-aliasing at "
                    "full resolution), never upscaling: there is no "
                    "low-resolution frame to upscale from.")
        p.warnings.append(
            "The feeder route costs several milliseconds per frame and is "
            "always DLAA. If the frame rate drops too far, lower the work "
            "area below 100%.")
    if api.confidence == "low":
        p.warnings.append(
            f"The graphics API could only be guessed ({api.reason}). If the "
            f"game does not start or nothing happens, run it once, then "
            f"re-check: ReShade.log will then say what it really uses.")
    return p
