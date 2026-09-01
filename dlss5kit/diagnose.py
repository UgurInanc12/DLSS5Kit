"""Reading the logs back into an answer a person can act on.

The usual failure here is silent: the game starts, nothing crashes, and it
simply looks unchanged. So after playing, the logs are the only evidence.

Three logs matter:
    ReShade.log       did ReShade load, did it load the add-on, did the NGX
                      hooks install, which module copies were found
    dlss5-feed.log    feeder route: did the shader load, are motion vectors
                      alive, was the DLSS feature created, are frames flowing
    <game>/Game.log   some engines log their own NGX failures, which is often
                      the most direct answer of all
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

OK, WARN, BAD, UNKNOWN = "ok", "warn", "bad", "unknown"


@dataclass
class Finding:
    level: str
    text: str
    evidence: str = ""


@dataclass
class Diagnosis:
    verdict: str = UNKNOWN
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, text: str, evidence: str = "") -> None:
        self.findings.append(Finding(level, text, evidence))


def _tail(path: Path, limit: int = 4_000_000) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf8", "replace")
    except OSError:
        return ""


_NGX_FAIL = re.compile(
    r"Failed to (NVSDK_NGX_\w+).*?(0x[0-9a-fA-F]+)", re.I)
_CREATE_FEATURE_EXC = re.compile(
    r"CreateFeature raised exception (0x[0-9A-Fa-f]+)")
_MV_PROBE = re.compile(r"MV probe[^\n]*?(\d+)%\s*non-zero", re.I)


def diagnose(game_dir: Path, route: str | None = None) -> Diagnosis:
    """Read whichever logs apply and say, in words, what happened."""
    root = Path(game_dir)
    d = Diagnosis()

    # ---------------------------------------------------------- ReShade.log
    rlog = root / "ReShade.log"
    if not rlog.is_file():
        d.add(BAD, "ReShade.log does not exist, so ReShade never loaded.",
              "The game has not been run since installing, or the proxy DLL "
              "is not being loaded - try renaming dxgi.dll to d3d11.dll.")
        d.verdict = BAD
        d.summary = "ReShade never loaded. Run the game once, then check again."
        return d

    text = _tail(rlog)

    m = re.search(r"Initializing crosire's ReShade version '([\d.]+)'", text)
    if m:
        d.add(OK, f"ReShade {m.group(1)} loaded.")
    else:
        d.add(WARN, "ReShade.log exists but records no initialisation.")

    if "Another ReShade instance was already loaded" in text:
        d.add(WARN,
              "Two ReShade copies tried to load at once.",
              "Usually a global ReShade installation (C:\\ProgramData\\ReShade) "
              "alongside the one in the game folder. The second one aborts, "
              "which is harmless, but remove the game from the global "
              "ReShadeApps.ini to keep it clean.")

    # Which renderer did the game actually create?
    n12 = len(re.findall(r"D3D12CreateDevice", text))
    n11 = len(re.findall(r"D3D11CreateDevice", text))
    if n12:
        d.add(OK, f"The game creates a D3D12 device ({n12} call(s)).")
    elif n11:
        d.add(OK, f"The game creates a D3D11 device ({n11} call(s)).")

    # Add-on registration
    if re.search(r"Registered add-on \"DLSS 5 Neural Rendering\"", text):
        v = re.search(r"Registered add-on \"DLSS 5 Neural Rendering\" (v[\d.]+)", text)
        d.add(OK, f"The DLSS 5 add-on registered{' ' + v.group(1) if v else ''}.")
    else:
        d.add(BAD, "The DLSS 5 add-on never registered.",
              "renodx-dlss5.addon64 is missing, or AddonPath is not set in "
              "ReShade.ini, or this is a ReShade build without add-on support.")

    hooked12 = "NVSDK_NGX_D3D12_CreateFeaturehooked" in text
    hooked11 = "NVSDK_NGX_D3D11_CreateFeaturehooked" in text
    if hooked12:
        d.add(OK, "The add-on hooked NVSDK_NGX_D3D12_CreateFeature.")
    if hooked11:
        d.add(OK, "The add-on hooked NVSDK_NGX_D3D11_CreateFeature.")

    # THE decisive mismatch: hooks on one API, the game rendering on another.
    if hooked12 and not hooked11 and n11 and not n12:
        d.add(BAD,
              "MISMATCH: the add-on hooked the D3D12 NGX entry points, but "
              "this game renders with D3D11 and calls the D3D11 ones. The "
              "hook waits for a call that never comes.",
              "This is what a silent 'nothing happens' looks like. The fix is "
              "the bridge route (dlss5-bridge.addon64), which reproduces the "
              "contract on a private D3D12 session.")

    for m in re.finditer(r"NGX module scan \(loaded copies\):\s*\n\s*([^\n]*)", text):
        mod = m.group(1).strip()
        if mod and not mod.startswith("0"):
            d.add(OK, f"NGX module found: {mod}")
            break

    if "signed NR runtime (nvngx_dlssnr.dll) pre-loaded" in text:
        d.add(OK, "nvngx_dlssnr.dll was pre-loaded at device init.")

    # -------------------------------------------------- the game's own log
    for cand in (root / "Game.log", root.parent / "Game.log"):
        if not cand.is_file():
            continue
        gtext = _tail(cand, 1_000_000)
        for m in _NGX_FAIL.finditer(gtext):
            fn, code = m.group(1), m.group(2)
            hint = ""
            if code.lower() in ("0xbad00005", "0xbad00004"):
                hint = ("NVSDK_NGX_Result_FAIL_FeatureNotSupported / "
                        "InvalidParameter: the runtime refused to create the "
                        "feature. On a D3D11 game this normally means the "
                        "D3D12-only add-on is installed and the bridge is "
                        "what is actually needed.")
            d.add(BAD, f"The game itself reports: {fn} failed with {code}.", hint)
        break

    # ------------------------------------------------------ dlss5-feed.log
    flog = root / "dlss5-feed.log"
    if flog.is_file():
        ftext = _tail(flog, 2_000_000)
        if "feature ready" in ftext:
            d.add(OK, "The DLSS contract was established (feature ready).")
        if re.search(r"frame \d+ delivered", ftext):
            d.add(OK, "Frames are being processed by the feeder.")
        m = _MV_PROBE.search(ftext)
        if m:
            pct = int(m.group(1))
            if pct == 0:
                d.add(BAD, "Motion vectors are all zero.",
                      "The provider technique is not above DLSS 5 Feed in the "
                      "ReShade technique list, or the provider shader is not "
                      "enabled at all.")
            else:
                d.add(OK, f"Motion vectors are alive ({pct}% non-zero).")
        m = _CREATE_FEATURE_EXC.search(ftext)
        if m:
            d.add(BAD, f"CreateFeature raised exception {m.group(1)}.",
                  "The add-on build and the nvngx_dlssnr build do not agree. "
                  "Try a different combination - a newer renodx with an older "
                  "dlssnr, or the reverse.")
    elif route == "feeder":
        d.add(WARN, "dlss5-feed.log does not exist.",
              "The feeder add-on never ran. Check that dlss5-feed.addon64 is "
              "in the game folder and that the game was actually launched.")

    # ----------------------------------------------------------- verdict
    bad = [f for f in d.findings if f.level == BAD]
    ok = [f for f in d.findings if f.level == OK]
    if bad:
        d.verdict = BAD
        d.summary = bad[0].text
    elif ok:
        d.verdict = OK
        d.summary = ("Everything the logs can show is in order. If the picture "
                     "still looks unchanged, make sure neural rendering is "
                     "turned ON in the add-on's overlay - it is off by "
                     "default.")
    else:
        d.verdict = UNKNOWN
        d.summary = "The logs do not say enough. Run the game once and retry."
    return d


def format_text(d: Diagnosis) -> str:
    icons = {OK: "[ok]  ", WARN: "[warn]", BAD: "[BAD] ", UNKNOWN: "[?]   "}
    lines = [f"Verdict: {d.verdict.upper()}", d.summary, ""]
    for f in d.findings:
        lines.append(f"{icons.get(f.level, '')} {f.text}")
        if f.evidence:
            for chunk in _wrap(f.evidence, 76):
                lines.append(f"        {chunk}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out
