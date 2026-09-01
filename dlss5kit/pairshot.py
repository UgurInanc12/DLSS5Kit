r"""Finding and composing the add-on's F5 screenshot pairs.

THE FEATURE ALREADY EXISTS IN THE ADD-ON
----------------------------------------
renodx-dlss5.addon64 v4.1.5 has a built-in pair capture on F5 (rebindable via
NRScreenshotKey in [RenoDX.DLSS5]). Verified by string analysis of the addon
binary 2026-09-01:

    "NR screenshot armed for the next successful evaluation"
    " [pre-NR]"   " [NR on]"
    "NR screenshot pair written: "

Pressing F5 arms a capture; on the next successful DLSS evaluation the add-on
copies the frame TWICE - once as it arrived from the game's DLSS (before the
neural pass) and once after neural rendering - and writes both as PNGs. That
is the same frame, identical content, NR off vs on, which is exactly what a
fair comparison needs. It cannot be reproduced from outside the process
(toggling F6 and grabbing the screen twice is always frames apart); the
add-on does it at the only place where both images of one frame exist.

What DLSS5Kit adds on top: finding the newest pair on disk and composing the
two images into one side-by-side comparison PNG, labelled, with a separator.
Pure stdlib (zlib + struct): PyInstaller stays small and there is no Pillow
dependency to carry.

Notes from the binary: the capture aborts if no DLSS evaluation runs within
about 10 seconds of arming, warns when the pair is entirely black (GPU copies
not finished - take the shot again), and logs the destination as
"NR screenshot pair written: <path> and <path>".
"""
from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

PRE_TOKEN = "[pre-NR]"
POST_TOKEN = "[NR on]"

_PAIR_LOG = re.compile(r"NR screenshot pair written:\s*(.+?)\s+and\s+(.+?)\s*$",
                       re.M)


class PairError(RuntimeError):
    pass


# ------------------------------------------------------------- discovery

def _screenshot_dirs(game_dir: Path) -> list[Path]:
    """Where the pair can land: the game folder, ReShade's SavePath, parents."""
    dirs = [game_dir]
    ini = game_dir / "ReShade.ini"
    if ini.is_file():
        try:
            for line in ini.read_text(encoding="utf8", errors="replace").splitlines():
                if line.strip().lower().startswith("savepath="):
                    raw = line.split("=", 1)[1].strip()
                    if raw:
                        p = (game_dir / raw).resolve() if not Path(raw).is_absolute() \
                            else Path(raw)
                        dirs.append(p)
        except OSError:
            pass
    dirs.append(game_dir.parent)
    seen, out = set(), []
    for d in dirs:
        k = str(d).lower()
        if k not in seen and d.is_dir():
            seen.add(k)
            out.append(d)
    return out


def find_latest_pair(game_dir: Path) -> tuple[Path, Path] | None:
    """The newest (pre-NR, NR-on) pair, matched by shared base name.

    The definitive source is ReShade.log ("NR screenshot pair written: A and
    B"), used first; the filesystem glob is the fallback for rotated logs.
    """
    game_dir = Path(game_dir)

    log = game_dir / "ReShade.log"
    if log.is_file():
        try:
            text = log.read_text(encoding="utf8", errors="replace")
            hits = _PAIR_LOG.findall(text)
            for a, b in reversed(hits):
                pa, pb = Path(a.strip()), Path(b.strip())
                if not pa.is_absolute():
                    pa = game_dir / pa
                if not pb.is_absolute():
                    pb = game_dir / pb
                if pa.is_file() and pb.is_file():
                    pre, post = (pa, pb) if PRE_TOKEN in pa.name else (pb, pa)
                    return pre, post
        except OSError:
            pass

    best: tuple[float, Path, Path] | None = None
    for d in _screenshot_dirs(game_dir):
        try:
            pres = [p for p in d.glob("*.png") if PRE_TOKEN in p.name]
        except OSError:
            continue
        for pre in pres:
            post = pre.with_name(pre.name.replace(PRE_TOKEN, POST_TOKEN))
            if not post.is_file():
                continue
            try:
                mt = pre.stat().st_mtime
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, pre, post)
    if best:
        return best[1], best[2]
    return None


# ----------------------------------------------------- minimal PNG codec

def _read_png(path: Path) -> tuple[int, int, int, bytearray]:
    """(width, height, channels, raw RGB/RGBA bytes). 8-bit only.

    Handles the five standard filters. Interlaced or paletted PNGs are
    refused with a clear message rather than mis-decoded.
    """
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PairError(f"{path.name} is not a PNG")
    pos, w, h, bit, ctype, interlace = 8, 0, 0, 0, 0, 0
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        ctag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctag == b"IHDR":
            w, h, bit, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif ctag == b"IDAT":
            idat += body
        elif ctag == b"IEND":
            break
    if bit != 8 or ctype not in (2, 6):
        raise PairError(f"{path.name}: unsupported PNG (bit={bit} type={ctype}); "
                        f"only 8-bit RGB/RGBA is handled")
    if interlace:
        raise PairError(f"{path.name}: interlaced PNG is not handled")
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = w * ch
    out = bytearray(h * stride)
    prev = bytearray(stride)
    src = 0
    for y in range(h):
        f = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if f == 1:      # Sub
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif f == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:    # Average
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:    # Paeth
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif f != 0:
            raise PairError(f"{path.name}: unknown PNG filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, out


def _write_png(path: Path, w: int, h: int, ch: int, pixels: bytes) -> None:
    ctype = 2 if ch == 3 else 6
    stride = w * ch
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filter: None
        raw += pixels[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


# 5x7 bitmap digits/letters for the labels, enough for "NR OFF" / "NR ON".
_GLYPHS = {
    "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
}


def _stamp(pixels: bytearray, w: int, h: int, ch: int, x0: int, y0: int,
           label: str, scale: int) -> None:
    """Paint a label. Out-of-bounds pixels are DROPPED, not wrapped: a flat
    offset check alone lets a label wider than the image spill onto the next
    row, painting stray white pixels across the wrong half."""
    for gi, glyph in enumerate(label):
        rows = _GLYPHS.get(glyph, _GLYPHS[" "])
        for gy, row in enumerate(rows):
            for gx, bit in enumerate(row):
                if bit != "#":
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        x = x0 + (gi * 6 + gx) * scale + sx
                        y = y0 + gy * scale + sy
                        if not (0 <= x < w and 0 <= y < h):
                            continue
                        off = (y * w + x) * ch
                        pixels[off] = pixels[off + 1] = pixels[off + 2] = 255


def compose(pre: Path, post: Path, out: Path | None = None,
            separator: int = 4) -> Path:
    """One side-by-side PNG: pre-NR on the left, NR on the right, labelled."""
    w1, h1, c1, p1 = _read_png(pre)
    w2, h2, c2, p2 = _read_png(post)
    if (w1, h1) != (w2, h2):
        raise PairError(f"pair sizes differ: {w1}x{h1} vs {w2}x{h2}")
    ch = 3
    def to_rgb(px, c):
        if c == 3:
            return px
        o = bytearray(len(px) // 4 * 3)
        o[0::3], o[1::3], o[2::3] = px[0::4], px[1::4], px[2::4]
        return o
    p1, p2 = to_rgb(p1, c1), to_rgb(p2, c2)

    W = w1 * 2 + separator
    stride1, strideo = w1 * ch, W * ch
    canvas = bytearray(W * h1 * ch)
    for y in range(h1):
        row = y * strideo
        canvas[row:row + stride1] = p1[y * stride1:(y + 1) * stride1]
        for s in range(separator):                       # white divider
            off = row + stride1 + s * ch
            canvas[off:off + ch] = b"\xff\xff\xff"
        canvas[row + stride1 + separator * ch:row + strideo] = \
            p2[y * stride1:(y + 1) * stride1]

    scale = max(2, w1 // 480)
    _stamp(canvas, W, h1, ch, 12, 12, "NR OFF", scale)
    _stamp(canvas, W, h1, ch, w1 + separator + 12, 12, "NR ON", scale)

    if out is None:
        out = pre.with_name(pre.name.replace(PRE_TOKEN, "[compare]"))
    _write_png(out, W, h1, ch, bytes(canvas))
    return out


def compare_latest(game_dir: Path) -> tuple[Path, Path, Path]:
    """Find the newest pair and compose it. (pre, post, composed)."""
    pair = find_latest_pair(Path(game_dir))
    if not pair:
        raise PairError(
            "No F5 screenshot pair found. In game, press F5 (the add-on's "
            "Capture Screenshot key) while DLSS is active; the add-on then "
            "writes the same frame twice - before and after neural rendering. "
            "Note: F5 needs an active NR evaluation within ~10 seconds of "
            "being pressed.")
    pre, post = pair
    return pre, post, compose(pre, post)
