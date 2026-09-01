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

# dlss5-bridge writes its own log. It is the only place that says whether the
# bridge route is actually delivering frames, and reading it turns "the game
# reported an NGX error" from an alarm into a fact with context.
_BRIDGE_FRAME = re.compile(r"\[bridge\] frame (\d+) delivered \((\d+)x(\d+)\)")
_BRIDGE_STATS = re.compile(
    r"\[bridge\] \d+ frames: bridge CPU ([\d.]+) ms/frame.*?"
    r"bridge is (\d+)% of the frame")
_BRIDGE_ATTACH = re.compile(r"dlss5-bridge ([\d.]+) .*?attached", re.I)
# "05:44:40" or "05:44:40.123" at the start of a log line, either bracketed
# by <> (the game's own log) or bare (the bridge's).
_TS = re.compile(r"<?(\d{2}):(\d{2}):(\d{2})")


def _seconds(text: str) -> int | None:
    m = _TS.search(text)
    if not m:
        return None
    h, mi, s = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _read_bridge_log(root: Path) -> dict:
    """What dlss5-bridge.log says about this run. {} when there is none."""
    log = root / "dlss5-bridge.log"
    if not log.is_file():
        return {}
    text = _tail(log, 2_000_000)
    frames = _BRIDGE_FRAME.findall(text)
    stats = _BRIDGE_STATS.findall(text)
    attach = _BRIDGE_ATTACH.search(text)
    first_ts = _seconds(text[:200])
    return {
        "present": True,
        "version": attach.group(1) if attach else None,
        "frames": int(frames[-1][0]) if frames else 0,
        "resolution": f"{frames[-1][1]}x{frames[-1][2]}" if frames else None,
        "cpu_ms": float(stats[-1][0]) if stats else None,
        "frame_share": int(stats[-1][1]) if stats else None,
        "clean_shutdown": "shut down cleanly" in text,
        "attached_at": first_ts,
    }


def diagnose(game_dir: Path, route: str | None = None) -> Diagnosis:
    """Read whichever logs apply and say, in words, what happened."""
    root = Path(game_dir)
    d = Diagnosis()

    # Read the bridge log first: on the bridge route it is the ground truth,
    # and it changes how the game's own NGX errors should be read.
    bridge = _read_bridge_log(root)

    # ---------------------------------------------------------- ReShade.log
    rlog = root / "ReShade.log"
    if not rlog.is_file():
        if bridge.get("frames"):
            # The bridge delivered frames, so everything loaded. Some games
            # write ReShade.log elsewhere, or a cleanup tool removed it; that
            # is not a failure when there is proof of frames.
            d.add(OK, f"The bridge delivered {bridge['frames']:,} frames, so "
                      f"the whole chain loaded.",
                  "ReShade.log is absent, but dlss5-bridge.log proves ReShade "
                  "loaded the add-on and the contract ran.")
        else:
            d.add(BAD, "ReShade.log does not exist, so ReShade never loaded.",
                  "The game has not been run since installing, or the proxy "
                  "DLL is not being loaded - try renaming dxgi.dll to "
                  "d3d11.dll.")
            d.verdict = BAD
            d.summary = ("ReShade never loaded. Run the game once, then check "
                         "again.")
            return d
        text = ""
    else:
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
    elif not bridge.get("frames"):
        # Delivered frames prove the add-on loaded, whatever ReShade.log says
        # (or does not say, when the file is absent entirely).
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

    # ------------------------------------------------ dlss5-bridge.log
    # On the bridge route this is the decisive evidence, and it must be read
    # BEFORE the game's own log, because it explains the NGX errors there.
    if bridge:
        if bridge.get("frames"):
            det = f"{bridge['frames']:,} frames delivered"
            if bridge.get("resolution"):
                det += f" at {bridge['resolution']}"
            extra = ""
            if bridge.get("cpu_ms") is not None:
                extra = (f"The bridge costs {bridge['cpu_ms']:.2f} ms/frame, "
                         f"about {bridge.get('frame_share', '?')}% of the "
                         f"frame.")
            d.add(OK, f"The bridge is working: {det}.", extra)
        elif bridge.get("version"):
            d.add(WARN, f"dlss5-bridge {bridge['version']} attached but "
                        f"delivered no frames.",
                  "Neural rendering may still be switched off in the overlay "
                  "(F6), or the game was closed before a frame was produced.")
    elif route == "bridge":
        d.add(WARN, "dlss5-bridge.log does not exist.",
              "The bridge add-on never ran. Check that "
              "dlss5-bridge.addon64 is in the game folder.")

    # -------------------------------------------------- the game's own log
    for cand in (root / "Game.log", root.parent / "Game.log"):
        if not cand.is_file():
            continue
        gtext = _tail(cand, 1_000_000)
        for m in _NGX_FAIL.finditer(gtext):
            fn, code = m.group(1), m.group(2)
            # A game whose own DLSS call fails while the bridge is delivering
            # frames is NORMAL: the bridge takes the contract over, so the
            # game's direct attempt is expected to be refused. Measured on
            # Crysis 3 Remastered 2026-09-01: the game logged
            # NVSDK_NGX_D3D11_CreateFeature ... 0xbad00002 three seconds after
            # the bridge attached, and the bridge then delivered 12,600
            # frames. Reporting that as a failure sends the user chasing a
            # problem that does not exist.
            line_ts = _seconds(gtext[max(0, m.start() - 40):m.start() + 40])
            after_bridge = (bridge.get("attached_at") is not None
                            and line_ts is not None
                            and line_ts >= bridge["attached_at"])
            if bridge.get("frames") and (after_bridge or line_ts is None):
                d.add(OK,
                      f"The game's own {fn} was refused ({code}), which is "
                      f"expected: the bridge has taken the DLSS contract over.",
                      "This line is the hand-over, not a fault. The frame "
                      "counter above is what says whether it works.")
                continue
            hint = ""
            if code.lower() in ("0xbad00005", "0xbad00004", "0xbad00002"):
                hint = ("The runtime refused to create the feature. On a D3D11 "
                        "game this normally means the D3D12-only add-on is "
                        "installed and the bridge is what is actually needed.")
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
    elif bridge.get("frames"):
        # Frames delivered is the strongest evidence there is: it means the
        # contract was created AND is running, not merely that things loaded.
        d.verdict = OK
        d.summary = (f"Working. The bridge delivered {bridge['frames']:,} "
                     f"frames"
                     + (f" at {bridge['resolution']}" if bridge.get("resolution")
                        else "")
                     + ". Neural rendering is on and running.")
    elif ok:
        d.verdict = OK
        d.summary = ("Everything the logs can show is in order. If the picture "
                     "still looks unchanged, make sure neural rendering is "
                     "turned ON in the add-on's overlay - it is off by "
                     "default (F6).")
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
