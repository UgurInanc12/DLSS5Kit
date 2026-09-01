"""NVIDIA GPU detection, RTX generation selection, and build compatibility.

WHY THIS MATTERS
----------------
The CUDA code inside the DLSS 5 neural rendering runtime is compiled per GPU
architecture. Install a build with no code for your card and DLSS silently
never starts - the game runs, nothing crashes, and it simply looks unchanged.
That is the single most confusing failure mode in this whole exercise, so the
architectures are read out of the files themselves rather than kept in a
table that goes stale.

CUBIN vs PTX (this distinction decides RTX 50 support)
------------------------------------------------------
A CUDA fatbin holds two kinds of record:

    CUBIN  machine code for one exact architecture. Runs on that
           architecture (and binary-compatible minor revisions), nothing else.
    PTX    portable intermediate assembly. The driver JIT-compiles it at load
           time for ANY architecture at least as new as the PTX target, so
           sm_89 PTX runs on an RTX 50 card.

Measured on the rhi-repo builds 2026-09-01:

    nvngx_dlssnr 310.8.0        CUBIN 120                PTX 120
    nvngx_dlssnr 310.8.0-RTX40  CUBIN 89,120             PTX 120
    nvngx_dlssnr 310.8.SF       CUBIN 75,86,89,120       PTX 120
    nvngx_dlssnr 310.8.SF-v2    CUBIN 75,86,89,120       PTX 120
    nvngx_dlss   310.8.0        CUBIN 75,80,86,89        PTX 80,89

nvngx_dlss carries no sm_120 CUBIN at all, yet it works on RTX 50 because its
sm_89 PTX is JIT-compiled by the driver. Reporting that file as incompatible
with an RTX 50 card - which a CUBIN-only check does - would be wrong.
"""
from __future__ import annotations

import collections
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------- generations

RTX20, RTX30, RTX40, RTX50 = "RTX 20", "RTX 30", "RTX 40", "RTX 50"
GTX10 = "GTX 10"
UNKNOWN_GEN = "Unknown"

# The generation picker, in the order it is offered in the UI.
GENERATIONS = (RTX20, RTX30, RTX40, RTX50)

# One representative compute capability per generation. Turing's RTX cards are
# all sm_75; Ampere consumer is sm_86 (sm_80 is the A100, data centre only).
GEN_SM = {
    GTX10: 61,
    RTX20: 75,
    RTX30: 86,
    RTX40: 89,
    RTX50: 120,
}
SM_GEN = {v: k for k, v in GEN_SM.items()}

GEN_EXAMPLES = {
    RTX20: "2060, 2070, 2080, 2080 Ti, and the GTX 16 series",
    RTX30: "3060, 3070, 3080, 3090",
    RTX40: "4060, 4070, 4080, 4090",
    RTX50: "5060, 5070, 5080, 5090",
}

SM_NAMES = {
    61:  "GTX 10 (Pascal)",
    75:  "RTX 20 / GTX 16 (Turing)",
    80:  "A100 (Ampere data centre)",
    86:  "RTX 30 (Ampere)",
    87:  "Orin",
    89:  "RTX 40 (Ada Lovelace)",
    90:  "H100 (Hopper)",
    100: "Blackwell (data centre)",
    120: "RTX 50 (Blackwell)",
    121: "Blackwell",
}
KNOWN_SM = set(SM_NAMES) | {50, 52, 53, 60, 62, 70, 72, 101}

# DLSS 5 neural rendering needs Tensor cores: Turing (sm_75) and newer.
MIN_SM = 75

_FATBIN_MAGIC = struct.pack("<I", 0xBA55ED50)
_KIND_PTX = 1
_KIND_CUBIN = 2


def label(sm: int | None) -> str:
    return SM_NAMES.get(sm, f"sm_{sm}") if sm is not None else "unknown"


def generation_of(sm: int | None) -> str:
    """Which RTX generation an architecture number belongs to."""
    if sm is None:
        return UNKNOWN_GEN
    return SM_GEN.get(sm, UNKNOWN_GEN)


# ------------------------------------------------------------ card detection

def _adapters() -> list[str]:
    """Display adapter names from the registry - no third-party dependency."""
    names: list[str] = []
    try:
        import winreg
        key = (r"SYSTEM\CurrentControlSet\Control\Class"
               r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        names.append(str(winreg.QueryValueEx(k, "DriverDesc")[0]))
                except OSError:
                    continue
    except Exception:
        pass
    return names


def sm_for_name(name: str) -> int | None:
    """CUDA architecture number from a card name, None if not an NVIDIA GPU.

    Covers desktop and laptop parts, Titan and Quadro/RTX A-series, because
    "RTX A4000" and "Titan RTX" carry no model number the plain pattern finds.
    A bare "GTX 1660 Ti" counts too: the GTX/RTX prefix is itself an NVIDIA
    marque, so requiring the word NVIDIA as well would reject real names.
    """
    n = name.upper()
    if not any(t in n for t in ("NVIDIA", "GEFORCE", "RTX", "GTX",
                                "QUADRO", "TITAN")):
        return None

    # Workstation Ada / Ampere parts, named without a GeForce-style number.
    if "RTX A" in n:                       # RTX A4000, A5000 = Ampere
        return 86
    if re.search(r"\bRTX\s+\d{3,4}\s+ADA\b", n):
        return 89
    if "TITAN RTX" in n:
        return 75
    if "TITAN V" in n:
        return 70
    if "TITAN XP" in n or "TITAN X" in n:
        return 61

    m = re.search(r"(?:RTX|GTX)\s*(\d{3,4})", n)
    if m:
        num = int(m.group(1))
        if 5000 <= num <= 5999:
            return 120
        if 4000 <= num <= 4999:
            return 89
        if 3000 <= num <= 3999:
            return 86
        if 2000 <= num <= 2999 or 1600 <= num <= 1699:
            return 75
        if 1000 <= num <= 1099:
            return 61
    return None


@dataclass
class Card:
    name: str | None = None
    sm: int | None = None
    generation: str = UNKNOWN_GEN
    detected: bool = True          # False when the user picked it by hand
    all_adapters: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.all_adapters is None:
            self.all_adapters = []
        if self.generation == UNKNOWN_GEN:
            self.generation = generation_of(self.sm)

    @property
    def supported(self) -> bool:
        return self.sm is not None and self.sm >= MIN_SM

    def describe(self) -> str:
        if self.name and self.sm is not None:
            src = "detected" if self.detected else "chosen"
            return f"{self.name} - {self.generation} ({src})"
        if self.sm is not None:
            return f"{self.generation} (chosen by hand)"
        return "no NVIDIA card detected"


def detect() -> tuple[str | None, int | None]:
    """(card name, sm). Laptops list an iGPU too, so the newest arch wins."""
    best: tuple[str | None, int | None] = (None, None)
    for name in _adapters():
        sm = sm_for_name(name)
        if sm is not None and (best[1] is None or sm > best[1]):
            best = (name, sm)
    return best


def detect_card() -> Card:
    """Full detection result, including every adapter seen."""
    adapters = _adapters()
    name, sm = detect()
    return Card(name=name, sm=sm, detected=True, all_adapters=adapters)


def card_for_generation(gen: str) -> Card:
    """A Card the user selected by hand from the generation picker."""
    sm = GEN_SM.get(gen)
    return Card(name=None, sm=sm, generation=gen if sm else UNKNOWN_GEN,
                detected=False)


def card_supported(sm: int | None) -> tuple[bool, str]:
    """Is this card capable of DLSS 5 at all, regardless of build?"""
    if sm is None:
        return False, ("No NVIDIA card detected. DLSS 5 neural rendering needs "
                       "an RTX 20 series card or newer. If you do have one, "
                       "pick your generation by hand.")
    if sm < MIN_SM:
        return False, (f"{label(sm)} has no Tensor cores. DLSS 5 neural "
                       f"rendering needs RTX 20 series or newer.")
    return True, f"{label(sm)} supports DLSS 5 neural rendering."


# ------------------------------------------------- fatbin architecture scan

@dataclass
class Archs:
    """What a CUDA-carrying DLL can actually run on."""
    cubin: set[int] = None        # type: ignore[assignment]
    ptx: set[int] = None          # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cubin is None:
            self.cubin = set()
        if self.ptx is None:
            self.ptx = set()

    def __bool__(self) -> bool:
        return bool(self.cubin or self.ptx)

    @property
    def all(self) -> set[int]:
        return self.cubin | self.ptx

    def runs_on(self, sm: int) -> tuple[bool, str]:
        """(can run, how). PTX at or below sm is JIT-compiled by the driver."""
        if sm in self.cubin:
            return True, "native"
        usable_ptx = [p for p in self.ptx if p <= sm]
        if usable_ptx:
            return True, f"JIT from sm_{max(usable_ptx)} PTX"
        return False, ""

    def summary(self) -> str:
        parts = []
        if self.cubin:
            parts.append("compiled for "
                         + ", ".join(SM_NAMES.get(a, f"sm_{a}")
                                     for a in sorted(self.cubin)))
        if self.ptx:
            parts.append("PTX for "
                         + ", ".join(f"sm_{a}" for a in sorted(self.ptx))
                         + " (JIT-compiled for newer cards)")
        return "; ".join(parts) or "no CUDA code found"

    def generations(self) -> dict[str, bool]:
        """Which RTX generations this file can serve."""
        return {g: self.runs_on(GEN_SM[g])[0] for g in GENERATIONS}


def dll_architectures(path: Path) -> Archs:
    """Read the CUDA fatbin records inside a DLL.

    The cubins are compressed, so the sm field is read from the fatbin entry
    headers rather than from ELF headers. Memory-mapped, so a 165 MB file
    costs no resident memory.
    """
    out = Archs()
    try:
        import mmap
        fh = open(path, "rb")
    except OSError:
        return out
    try:
        try:
            d = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            d = fh.read()
        off = 0
        while True:
            i = d.find(_FATBIN_MAGIC, off)
            if i < 0:
                break
            off = i + 4
            try:
                hsize = struct.unpack_from("<H", d, i + 6)[0]
                fatsize = struct.unpack_from("<Q", d, i + 8)[0]
                if hsize < 16 or not (0 < fatsize <= len(d)):
                    continue
                p, end = i + hsize, i + hsize + fatsize
                while p < end - 32:
                    kind = struct.unpack_from("<H", d, p)[0]
                    ehdr = struct.unpack_from("<I", d, p + 4)[0]
                    payload = struct.unpack_from("<Q", d, p + 8)[0]
                    if ehdr < 24 or ehdr > 4096 or not (0 < payload <= len(d)):
                        break
                    for so in (24, 28, 20):
                        if p + so + 4 > len(d):
                            continue
                        sm = struct.unpack_from("<I", d, p + so)[0]
                        if sm in KNOWN_SM:
                            if kind == _KIND_PTX:
                                out.ptx.add(sm)
                            else:
                                out.cubin.add(sm)
                            break
                    p += ehdr + payload
            except Exception:
                continue
        try:
            if hasattr(d, "close"):
                d.close()
        except Exception:
            pass
        return out
    finally:
        try:
            fh.close()
        except Exception:
            pass


def check(path: Path, sm: int | None) -> tuple[bool | None, str]:
    """(compatible, explanation). None means it could not be determined."""
    archs = dll_architectures(path)
    if not archs:
        return None, "could not read CUDA architectures from the file"
    if sm is None:
        return None, f"card not known; the file is {archs.summary()}"
    ok, how = archs.runs_on(sm)
    if ok:
        if how == "native":
            return True, f"compatible with {label(sm)} (natively {archs.summary()})"
        return True, (f"compatible with {label(sm)} by driver JIT ({how}); "
                      f"the file is {archs.summary()}")
    return False, (f"THIS BUILD WILL NOT RUN ON {label(sm).upper()}. It has no "
                   f"code for it: {archs.summary()}")


def build_matrix(files: dict[str, Path]) -> dict[str, dict[str, bool]]:
    """{build label: {generation: runs}} for a set of downloaded files."""
    return {name: dll_architectures(p).generations()
            for name, p in files.items()}
