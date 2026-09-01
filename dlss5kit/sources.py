"""Every network location this tool uses, in one auditable place.

It contacts these hosts and no others:

    reshade.me                  the ReShade add-on installer
    raw.githubusercontent.com   crosire/reshade-shaders headers
    codeload.github.com         umar-afzaal/LumeniteFX source zip
    api.github.com / github.com release metadata and assets for
                                jlrouzies-fr/DLSS5-Feeder,
                                NIGos/dlss5-bridge,
                                RankFTW/rhi-repo

There is no telemetry, no analytics and no account. Downloads are cached under
%LOCALAPPDATA%\\dlss5kit\\cache and reused, so installing a second game costs
nothing.

A NOTE ON WHAT COMES FROM WHERE
-------------------------------
ReShade, DLSS5-Feeder, LumeniteFX, dlss5-bridge and the shader headers all
come from their authors' own repositories. The DLSS 5 add-on
(renodx-dlss5.addon64) and the NVIDIA NGX runtimes (nvngx_dlssnr.dll,
nvngx_dlss.dll) are closed-source files with no published licence, mirrored by
the community at RankFTW/rhi-repo. They are not redistributed by this tool;
they are fetched from that mirror exactly as a person would by hand. Local
copies are always preferred when present.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "dlss5kit/1.0 (+local install helper)"}

RESHADE_HOME = "https://reshade.me"
RESHADE_SETUP_RE = re.compile(r"/downloads/ReShade_Setup_([\d.]+)_Addon\.exe")

RESHADE_HEADERS_BASE = (
    "https://raw.githubusercontent.com/crosire/reshade-shaders/slim/Shaders/")
RESHADE_HEADERS = ("ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh")

FEEDER_API = "https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest"
BRIDGE_API = "https://api.github.com/repos/NIGos/dlss5-bridge/releases/latest"
RHI_API = "https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100"
LUMENITE_ZIP = ("https://codeload.github.com/umar-afzaal/LumeniteFX/"
                "zip/refs/heads/mainline")

ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "dlss5kit"
CACHE = ROOT / "cache"
API_CACHE = ROOT / "api-cache"
API_FRESH_SECONDS = 6 * 3600

# Set when a stale cached API answer had to be used, so the caller can say why
# the version list might be out of date rather than failing the install.
last_fallback: str | None = None


class RateLimited(RuntimeError):
    """GitHub allows 60 anonymous API calls an hour per address."""


class DownloadError(RuntimeError):
    pass


# ------------------------------------------------------------------ HTTP

def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 429) and "api.github.com" in url:
            raise RateLimited(
                "GitHub is rate limiting this connection (60 anonymous API "
                "requests per hour). Wait an hour, or use a different network. "
                "Anything already in the cache still installs.") from e
        raise


def _cache_path(url: str) -> Path:
    return API_CACHE / (hashlib.sha256(url.encode("utf8")).hexdigest()[:32] + ".json")


def _json(url: str):
    """Fetch JSON, backed by an on-disk cache.

    A fresh cache answers without a request at all. When the live request
    fails - rate limit, no connection - a stale cache of any age is used
    rather than failing the install outright.
    """
    global last_fallback
    p = _cache_path(url)
    try:
        if time.time() - p.stat().st_mtime < API_FRESH_SECONDS:
            return json.loads(p.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        raw = _get(url).decode("utf8")
        data = json.loads(raw)
        try:
            API_CACHE.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf8")
        except OSError:
            pass
        return data
    except Exception as original:
        try:
            data = json.loads(p.read_text(encoding="utf8"))
            age_h = int((time.time() - p.stat().st_mtime) / 3600)
        except (OSError, json.JSONDecodeError):
            raise original from None
        last_fallback = (f"GitHub could not be reached; using the version list "
                         f"cached {age_h}h ago.")
        return data


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def download(url: str, filename: str, progress=None) -> Path:
    """Download into the cache and return the path. Cached files are reused."""
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / filename
    if dest.is_file() and dest.stat().st_size > 0:
        if progress:
            progress(100, f"{filename} (cached)")
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(int(got * 100 / total) if total else 0,
                                 f"{filename} - {human(got)}"
                                 + (f" / {human(total)}" if total else ""))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"Could not download {filename}: {e}") from e
    tmp.replace(dest)
    return dest


def fetch_bytes(url: str) -> bytes:
    return _get(url)


# ------------------------------------------------------------- resolvers

def resolve_reshade() -> tuple[str, str]:
    """(version, url) of the current ReShade add-on installer."""
    html = _get(RESHADE_HOME).decode("utf8", "replace")
    m = RESHADE_SETUP_RE.search(html)
    if not m:
        raise RuntimeError("Could not find the ReShade add-on installer on reshade.me.")
    return m.group(1), RESHADE_HOME + m.group(0)


def resolve_feeder() -> tuple[str, dict[str, str]]:
    """Latest DLSS5-Feeder release: (tag, {asset name: url})."""
    rel = _json(FEEDER_API)
    return (rel.get("tag_name", "?"),
            {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])})


def resolve_bridge() -> tuple[str, str]:
    """Latest dlss5-bridge release: (tag, .addon64 url)."""
    rel = _json(BRIDGE_API)
    for a in rel.get("assets", []):
        if a["name"].lower().endswith(".addon64"):
            return rel.get("tag_name", "?"), a["browser_download_url"]
    raise RuntimeError("The dlss5-bridge release has no .addon64 asset.")


def _ver_key(tag: str, prefix: str) -> tuple:
    """'dlssnr-310.8.SF-v2' -> a sortable tuple of its numbers."""
    raw = tag[len(prefix):].lstrip("-")
    nums = re.findall(r"\d+", raw)
    return tuple(int(n) for n in nums) if nums else (0,)


_CATALOG: dict[str, list[dict]] | None = None


def rhi_catalog(force: bool = False) -> dict[str, list[dict]]:
    """rhi-repo releases grouped by component family, newest first.

    Cached for the process lifetime: setting up several games in one session
    should not burn through the anonymous API allowance.
    """
    global _CATALOG
    if _CATALOG is not None and not force:
        return _CATALOG
    rels = _json(RHI_API)
    fams: dict[str, list[dict]] = {}
    for r in rels:
        tag = r.get("tag_name", "")
        for prefix, fam in (("renodx-dlss5", "renodx"),
                            ("dlssnr", "dlssnr"),
                            ("dlss-", "dlss")):
            if not tag.startswith(prefix):
                continue
            for a in r.get("assets", []):
                if not a["name"].endswith(".zip"):
                    continue
                fams.setdefault(fam, []).append({
                    "tag": tag,
                    "label": tag[len(prefix):].lstrip("-") or tag,
                    "url": a["browser_download_url"],
                    "size": a.get("size", 0),
                    "key": _ver_key(tag, prefix.rstrip("-")),
                })
            break
    for fam in fams.values():
        fam.sort(key=lambda d: d["key"], reverse=True)
    _CATALOG = fams
    return fams


def pick(entries: list[dict], want: str | None) -> dict:
    """The entry matching `want`, else the newest."""
    if want:
        for e in entries:
            if e["label"] == want or e["tag"] == want:
                return e
    return entries[0]


def clear_cache() -> int:
    """Delete the download cache. Returns bytes freed."""
    freed = 0
    if CACHE.is_dir():
        for p in CACHE.rglob("*"):
            if p.is_file():
                freed += p.stat().st_size
        shutil.rmtree(CACHE, ignore_errors=True)
    return freed
