r"""DLSS Super Resolution and Ray Reconstruction render preset overrides.

WHAT A "PRESET" IS HERE
-----------------------
The DLSS runtime contains several neural networks and picks one per feature.
The letter (A..F legacy CNNs, J/K/L/M transformers for SR; D/E/F for Ray
Reconstruction) decides sharpness, ghosting behaviour and cost. Games rarely
expose the choice, so an override mechanism exists.

HOW THE OVERRIDE ACTUALLY WORKS (verified in nvngx_dlss.dll 310.8.0)
--------------------------------------------------------------------
The runtime's own log strings name three sources, in priority order:

    "Info: (%s) Using DRS Overridden Preset %s"
    "Info: (%s) Using App hint Preset %s"
    "Info: (%s) App hint Preset %s is not available, using title default"

1. DRS override: "Reading NGX_OVERRIDE_RENDER_PRESET_SELECTION from DRS" -
   the NVIDIA driver settings store. The documented public bridge into it is
   the registry hive the NGX loader reads:
       HKLM\SOFTWARE\NVIDIA Corporation\Global\NGXCore
   (verified present on this machine; the runtime errors reference exactly
   this key). Writing HKLM needs elevation, so it is a poor fit for a tool
   that promises never to ask for admin rights.

2. App hint parameters, set on the NGX parameter block before CreateFeature:
       DLSS.Hint.Render.Preset.{DLAA,UltraQuality,Quality,Balanced,
                                Performance,UltraPerformance}
       RayReconstruction.Hint.Render.Preset.{same six}
   Both families are present verbatim in nvngx_dlss.dll 310.8.0. These are
   per-process, need no elevation, and win over the title default.

THE ROUTE THIS MODULE TAKES
---------------------------
dlss5-bridge creates the DLSS feature itself (it IS the app on the bridge
route), and it reads dlss5-bridge.cfg "before launch" by its own message. The
same NGX parameter names appear inside dlss5-bridge.addon64, so hint keys in
its cfg reach the parameter block it owns.

For the native route the add-on hooks NVSDK_NGX_D3D12_CreateFeature, which is
exactly the moment the hint can be injected; the renodx add-on exposes that
via its own settings section. Where no injector owns CreateFeature, the
fallback that always works without elevation is per-user DRS via the NVIDIA
app profile - out of scope here, so the module is honest about which routes
it can serve:

    bridge  -> dlss5-bridge.cfg hint keys (this module writes them)
    native  -> [RenoDX.DLSS5] hint keys in ReShade.ini (this module writes
               them; the add-on forwards known DLSS.* keys onto the block)
    feeder  -> the synthetic contract is DLAA-only; SR presets still shape
               the DLAA pass, RR does not exist there

Preset letters map to the NVSDK_NGX_DLSS_Hint_Render_Preset enum:
A=1 B=2 C=3 D=4 E=5 F=6 ... J=10 K=11 L=12 M=13 (0 = title default).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config

# Letter -> NVSDK_NGX_DLSS_Hint_Render_Preset value.
PRESET_VALUES = {
    "default": 0,
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6,
    "G": 7, "H": 8, "I": 9, "J": 10, "K": 11, "L": 12, "M": 13,
}

# What we OFFER. The runtime accepts more, but these are the sets with a
# public meaning on current builds: transformers for SR, RR's supported trio.
SR_PRESETS = ("default", "J", "K", "L", "M")
RR_PRESETS = ("default", "D", "E", "F")

# The six perf-quality slots the hint families carry, verbatim from
# nvngx_dlss.dll. The override is written to every slot so it applies
# whatever quality mode the user selects in game.
_QUALITY_SLOTS = ("DLAA", "UltraQuality", "Quality", "Balanced",
                  "Performance", "UltraPerformance")

SR_KEY_FMT = "DLSS.Hint.Render.Preset.{}"
RR_KEY_FMT = "RayReconstruction.Hint.Render.Preset.{}"


class PresetError(ValueError):
    pass


def _validate(kind: str, letter: str, allowed: tuple[str, ...]) -> int:
    if letter not in allowed:
        raise PresetError(
            f"{kind} preset {letter!r} is not offered. "
            f"Choose one of: {', '.join(allowed)}")
    return PRESET_VALUES[letter]


@dataclass
class Presets:
    sr: str = "default"
    rr: str = "default"

    def validate(self) -> None:
        _validate("SR", self.sr, SR_PRESETS)
        _validate("RR", self.rr, RR_PRESETS)


def hint_pairs(p: Presets) -> list[tuple[str, int]]:
    """The (parameter name, value) pairs a CreateFeature owner should set.

    default (0) pairs are emitted too: overwriting a stale override with
    "title default" is how a user gets BACK to stock behaviour.
    """
    p.validate()
    out: list[tuple[str, int]] = []
    sr, rr = PRESET_VALUES[p.sr], PRESET_VALUES[p.rr]
    for slot in _QUALITY_SLOTS:
        out.append((SR_KEY_FMT.format(slot), sr))
        out.append((RR_KEY_FMT.format(slot), rr))
    return out


# ------------------------------------------------------------ persistence

def apply_to_bridge_cfg(game_dir: Path, p: Presets) -> Path:
    """Write the hint keys into dlss5-bridge.cfg (the bridge owns the
    feature, so its parameter block is where the hints land)."""
    p.validate()
    settings = {name: value for name, value in hint_pairs(p)}
    return config.write_bridge_cfg(Path(game_dir), native_dlss=True,
                                   settings=settings)


def apply_to_reshade_ini(game_dir: Path, p: Presets) -> Path:
    """Write the hint keys into [RenoDX.DLSS5] for the native route."""
    p.validate()
    path = Path(game_dir) / "ReShade.ini"
    ini = config.Ini.load(path)
    for name, value in hint_pairs(p):
        ini.set("RenoDX.DLSS5", name, str(value))
    ini.save(path)
    return path


def read_current(game_dir: Path) -> Presets:
    """What is configured right now, from whichever file carries it."""
    game_dir = Path(game_dir)
    rev = {v: k for k, v in PRESET_VALUES.items()}

    def from_value(raw: str | None) -> str | None:
        if raw is None:
            return None
        try:
            return rev.get(int(float(raw)))
        except ValueError:
            return None

    sr = rr = None
    ini = config.Ini.load(game_dir / "ReShade.ini")
    sr = from_value(ini.get("RenoDX.DLSS5", SR_KEY_FMT.format("Quality")))
    rr = from_value(ini.get("RenoDX.DLSS5", RR_KEY_FMT.format("Quality")))
    if sr is None or rr is None:
        cfg = config.read_cfg(game_dir / config.BRIDGE_CFG)
        if sr is None:
            sr = from_value(cfg.get(SR_KEY_FMT.format("Quality")))
        if rr is None:
            rr = from_value(cfg.get(RR_KEY_FMT.format("Quality")))
    return Presets(sr=sr or "default", rr=rr or "default")


def apply(game_dir: Path, route: str | None, p: Presets) -> list[Path]:
    """Apply to whichever files the installed route consults.

    Both files are written when the route is unknown: stale keys in the
    unused one are harmless (nothing reads them), missing keys in the used
    one are not.
    """
    p.validate()
    game_dir = Path(game_dir)
    written: list[Path] = []
    if route in (None, "bridge") and (game_dir / config.BRIDGE_CFG).exists():
        written.append(apply_to_bridge_cfg(game_dir, p))
    if route in (None, "native", "feeder") or not written:
        written.append(apply_to_reshade_ini(game_dir, p))
    return written


def describe(p: Presets) -> str:
    sr = "title default" if p.sr == "default" else f"preset {p.sr}"
    rr = "title default" if p.rr == "default" else f"preset {p.rr}"
    return f"DLSS SR: {sr} | Ray Reconstruction: {rr}"
