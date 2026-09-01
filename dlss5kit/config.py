r"""Reading and writing ReShade .ini files and the add-on config files.

ReShade stores multi-values comma-separated and escapes a literal comma as
",,". The keys used here match crosire/reshade's runtime.cpp:

    ReShade.ini [GENERAL] : EffectSearchPaths, TextureSearchPaths,
                            PreprocessorDefinitions, PresetPath
    ReShade.ini [ADDON]   : AddonPath
    preset root section   : Techniques, TechniqueSorting,
                            PreprocessorDefinitions

Technique entries look like "TechniqueName@File.fx".

ORDERING IS NOT COSMETIC: the motion-vector provider's technique must sit
ABOVE DLSS5_Feed in the technique list, or the feed never receives vectors
and the whole feeder route silently does nothing.

Everything here preserves settings it did not write. Somebody's existing
ReShade configuration is not ours to reformat.
"""
from __future__ import annotations

from pathlib import Path

# provider id -> (label, technique entry or None, do we install the shader)
PROVIDERS = {
    3: ("LumeniteFX Kernel 2.0 (recommended)",
        "Lumenite_Kernel@lumenite_Kernel.fx", True),
    4: ("LumeniteFX QuantMotion",
        "Lumenite_QuantMotion@lumenite_QuantMotion.fx", True),
    0: ("Generic texMotionVectors (qUINT etc. - install it yourself)", None, False),
    1: ("iMMERSE Launchpad (install it yourself)", None, False),
    2: ("VORT (install it yourself)", None, False),
}

FEED_TECHNIQUE = "DLSS5_Feed@DLSS5_Feed.fx"


class Ini:
    """Ordered sections. The first is always the root section ("")."""

    def __init__(self) -> None:
        self.sections: list[tuple[str, list[list[str]]]] = [("", [])]

    @classmethod
    def parse(cls, text: str) -> "Ini":
        ini = cls()
        cur = 0
        for line in text.splitlines():
            s = line.strip()
            if not s or s[0] in ";#":
                continue
            if s.startswith("[") and s.endswith("]"):
                cur = ini._index(s[1:-1])
                continue
            if "=" in s:
                k, v = s.split("=", 1)
                ini.sections[cur][1].append([k.strip(), v.strip()])
        return ini

    @classmethod
    def load(cls, path: Path) -> "Ini":
        try:
            return cls.parse(path.read_text(encoding="utf8", errors="replace"))
        except OSError:
            return cls()

    def _index(self, name: str) -> int:
        for i, (n, _) in enumerate(self.sections):
            if n.lower() == name.lower():
                return i
        self.sections.append((name, []))
        return len(self.sections) - 1

    def get(self, section: str, key: str) -> str | None:
        for n, kv in self.sections:
            if n.lower() == section.lower():
                for k, v in kv:
                    if k.lower() == key.lower():
                        return v
        return None

    def set(self, section: str, key: str, value: str) -> None:
        kv = self.sections[self._index(section)][1]
        for e in kv:
            if e[0].lower() == key.lower():
                e[1] = value
                return
        kv.append([key, value])

    def set_default(self, section: str, key: str, value: str) -> None:
        if self.get(section, key) is None:
            self.set(section, key, value)

    def dump(self) -> str:
        out: list[str] = []
        for name, kv in self.sections:
            if not kv and not name:
                continue
            if name:
                if out:
                    out.append("")
                out.append(f"[{name}]")
            out += [f"{k}={v}" for k, v in kv]
        return "\n".join(out) + "\n"

    def save(self, path: Path) -> None:
        path.write_text(self.dump(), encoding="utf8")


def split_list(raw: str) -> list[str]:
    """Split on single commas; ',,' is an escaped literal comma."""
    items, cur, i = [], "", 0
    while i < len(raw):
        if raw[i] == ",":
            if i + 1 < len(raw) and raw[i + 1] == ",":
                cur += ","
                i += 2
                continue
            items.append(cur)
            cur = ""
        else:
            cur += raw[i]
        i += 1
    if cur:
        items.append(cur)
    return [s for s in items if s]


def join_list(items: list[str]) -> str:
    return ",".join(s.replace(",", ",,") for s in items)


def _ensure_define(raw: str, define: str) -> str:
    """Replace or append one NAME=VALUE define, keeping the others."""
    name = define.split("=", 1)[0]
    kept = [d for d in split_list(raw) if d.split("=", 1)[0] != name]
    kept.append(define)
    return join_list(kept)


def write_reshade_ini(game_dir: Path, provider: int | None = None) -> None:
    """Create or update ReShade.ini without disturbing the user's settings.

    provider=None means no shaders are needed (native and bridge routes):
    only add-on loading is configured.
    """
    p = game_dir / "ReShade.ini"
    ini = Ini.load(p)
    # Add-ons are loaded from beside the game executable.
    ini.set_default("ADDON", "AddonPath", ".\\")
    if provider is not None:
        ini.set_default("GENERAL", "EffectSearchPaths",
                        r".\reshade-shaders\Shaders\**")
        ini.set_default("GENERAL", "TextureSearchPaths",
                        r".\reshade-shaders\Textures\**")
        ini.set_default("GENERAL", "PresetPath", r".\ReShadePreset.ini")
        ini.set("GENERAL", "PreprocessorDefinitions",
                _ensure_define(ini.get("GENERAL", "PreprocessorDefinitions") or "",
                               f"DLSS5_MV_PROVIDER={provider}"))
    ini.save(p)


def write_preset(game_dir: Path, provider: int = 3) -> None:
    """Put the motion-vector technique ABOVE DLSS5_Feed in the preset."""
    p = game_dir / "ReShadePreset.ini"
    ini = Ini.load(p)
    tech = PROVIDERS.get(provider, (None, None, False))[1]
    ours = ([tech] if tech else []) + [FEED_TECHNIQUE]
    for key in ("Techniques", "TechniqueSorting"):
        if key == "TechniqueSorting" and ini.get("", key) is None:
            continue
        rest = [t for t in split_list(ini.get("", key) or "") if t not in ours]
        ini.set("", key, join_list(ours + rest))
    ini.set("", "PreprocessorDefinitions",
            _ensure_define(ini.get("", "PreprocessorDefinitions") or "",
                           f"DLSS5_MV_PROVIDER={provider}"))
    ini.save(p)


def remove_our_techniques(game_dir: Path) -> bool:
    """Take only our techniques out of the preset. True if it changed.

    An untouched preset is left byte-for-byte alone: parsing and re-dumping
    somebody's own file would reformat it for no reason.
    """
    p = game_dir / "ReShadePreset.ini"
    if not p.is_file():
        return False
    ours = {FEED_TECHNIQUE} | {v[1] for v in PROVIDERS.values() if v[1]}
    ini = Ini.load(p)
    changed = False
    for key in ("Techniques", "TechniqueSorting"):
        raw = ini.get("", key)
        if raw is None:
            continue
        before = split_list(raw)
        kept = [t for t in before if t not in ours]
        if len(kept) != len(before):
            ini.set("", key, join_list(kept))
            changed = True
    if changed:
        ini.save(p)
    return changed


# ------------------------------------------------------- add-on config files

FEED_CFG = "dlss5-feed.cfg"
BRIDGE_CFG = "dlss5-bridge.cfg"

# DLSS preset hint. Per the DLSS5-Feeder troubleshooting notes: warping around
# flames or transparent objects is helped by the legacy CNN presets.
PRESETS = {
    0:  "Default (let the add-on decide)",
    5:  "Preset E - legacy CNN (helps with flame/transparency warping)",
    6:  "Preset F - legacy CNN",
    10: "Preset J - transformer",
    11: "Preset K - transformer (newest)",
}
HDR_MODES = {-1: "Auto", 0: "Force SDR", 1: "Force HDR"}


def feed_defaults() -> dict:
    return {
        "enabled": 1,
        "mode": 2,
        "hdr": -1,
        "depth_inverted": -1,
        "flags": -1,
        "reset_every": 0,
        "warmup_rebuild": 180,
        "rebuild": 0,
        "log_frames": 3,
        "create_delay": 60,
        "preset": 0,
        "work_resolution": 100,
        "mv_scale_x": 1.0,
        "mv_scale_y": 1.0,
    }


def read_cfg(path: Path) -> dict:
    out: dict = {}
    try:
        for line in path.read_text(encoding="utf8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_feed_cfg(dir_: Path, settings: dict | None = None) -> Path:
    """Create or update dlss5-feed.cfg, preserving keys we do not manage."""
    p = dir_ / FEED_CFG
    cur = feed_defaults()
    cur.update(read_cfg(p))
    if settings:
        cur.update(settings)
    lines = []
    for k, v in cur.items():
        if k.startswith("mv_scale") or isinstance(v, float):
            lines.append(f"{k}={float(v):.3f}")
        else:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf8")
    return p


def write_bridge_cfg(dir_: Path, native_dlss: bool,
                     settings: dict | None = None) -> Path:
    """Create dlss5-bridge.cfg. synth_after only matters without native DLSS."""
    p = dir_ / BRIDGE_CFG
    cur: dict = {"vk_mirror": 1}
    if not native_dlss:
        cur["synth_after"] = 3
    cur.update(read_cfg(p))
    if settings:
        cur.update(settings)
    p.write_text("\n".join(f"{k}={v}" for k, v in cur.items()) + "\n",
                 encoding="utf8")
    return p
