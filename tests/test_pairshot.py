"""Tests for the F5 pair discovery and the side-by-side composer.

Offline: synthetic PNGs, no game, no add-on. What is being pinned:
the pair naming contract ("[pre-NR]" / "[NR on]"), the ReShade.log fast
path, the PNG round trip through all five filters, and the composition
geometry (left = pre, right = post, separator between).
"""
from __future__ import annotations

import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlss5kit import pairshot  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  <- {detail}" if detail else ""))


def make_png(path: Path, w: int, h: int, rgb: tuple[int, int, int],
             alpha: bool = False) -> None:
    ch = 4 if alpha else 3
    ctype = 6 if alpha else 2
    px = bytes(rgb + ((255,) if alpha else ())) * (w * h)
    raw = b"".join(b"\x00" + px[y * w * ch:(y + 1) * w * ch] for y in range(h))
    comp = zlib.compress(raw)

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def test_png_roundtrip():
    print("\n[png read/write round trip]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_png(d / "a.png", 20, 10, (10, 200, 30))
        w, h, ch, px = pairshot._read_png(d / "a.png")
        check("dimensions read back", (w, h, ch) == (20, 10, 3), f"{w}x{h}x{ch}")
        check("pixel content correct",
              px[0:3] == bytes((10, 200, 30)), str(px[0:6]))
        pairshot._write_png(d / "b.png", w, h, ch, bytes(px))
        w2, h2, ch2, px2 = pairshot._read_png(d / "b.png")
        check("write -> read identical", px == px2 and (w, h) == (w2, h2))

        # RGBA is accepted and reduced to RGB by compose
        make_png(d / "rgba.png", 8, 8, (1, 2, 3), alpha=True)
        w3, h3, ch3, _ = pairshot._read_png(d / "rgba.png")
        check("RGBA read", ch3 == 4)

        (d / "bad.png").write_bytes(b"not a png at all")
        try:
            pairshot._read_png(d / "bad.png")
            check("non-PNG refused", False)
        except pairshot.PairError:
            check("non-PNG refused", True)


def test_all_filters_decode():
    print("\n[all five PNG filter types decode]")
    # Build a 4x4 RGB image encoded with filters 0..4 on successive rows.
    w, h, ch = 4, 5, 3
    rows = [bytes(range(y * 40, y * 40 + w * ch)) for y in range(h)]

    def sub(cur):     # filter 1: enc[i] = raw[i] - raw[i-ch] (from RAW, not encoded)
        return bytes((cur[i] - (cur[i - ch] if i >= ch else 0)) & 0xFF
                     for i in range(len(cur)))

    def up(cur, prev):  # filter 2
        return bytes((c - p) & 0xFF for c, p in zip(cur, prev))

    def avg(cur, prev):  # filter 3
        out = bytearray()
        for i, c in enumerate(cur):
            a = cur[i - ch] if i >= ch else 0
            out.append((c - ((a + prev[i]) >> 1)) & 0xFF)
        return bytes(out)

    def paeth(cur, prev):  # filter 4
        out = bytearray()
        for i, cv in enumerate(cur):
            a = cur[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out.append((cv - pr) & 0xFF)
        return bytes(out)

    enc = b"\x00" + rows[0]
    enc += b"\x01" + sub(rows[1])
    enc += b"\x02" + up(rows[2], rows[1])
    enc += b"\x03" + avg(rows[3], rows[2])
    enc += b"\x04" + paeth(rows[4], rows[3])
    comp = zlib.compress(enc)

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "f.png"
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                      + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
        _, _, _, px = pairshot._read_png(p)
        want = b"".join(rows)
        check("filters 0-4 reconstruct exactly", bytes(px) == want,
              f"first diff at {next((i for i,(a,b) in enumerate(zip(px,want)) if a!=b), -1)}")


def test_pair_discovery_and_compose():
    print("\n[pair discovery and composition]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        pre = d / "Crysis3 2026-09-01 [pre-NR].png"
        post = d / "Crysis3 2026-09-01 [NR on].png"
        make_png(pre, 32, 16, (200, 0, 0))     # red = before
        make_png(post, 32, 16, (0, 200, 0))    # green = after

        found = pairshot.find_latest_pair(d)
        check("pair found by glob", found is not None)
        check("pre identified by token", found and "[pre-NR]" in found[0].name)
        check("post identified by token", found and "[NR on]" in found[1].name)

        out = pairshot.compose(found[0], found[1])
        check("composed file created", out.is_file())
        check("named [compare]", "[compare]" in out.name, out.name)
        w, h, ch, px = pairshot._read_png(out)
        check("width = 2x + separator", w == 32 * 2 + 4, str(w))
        check("height preserved", h == 16)
        # sample the midline of each half (away from the stamped labels)
        y = h - 2
        left = px[(y * w + 5) * ch:(y * w + 5) * ch + 3]
        right = px[(y * w + 32 + 4 + 5) * ch:(y * w + 32 + 4 + 5) * ch + 3]
        sep = px[(y * w + 33) * ch:(y * w + 33) * ch + 3]
        check("left half is the pre image", left == bytes((200, 0, 0)), str(left))
        check("right half is the post image", right == bytes((0, 200, 0)), str(right))
        check("separator is white", sep == b"\xff\xff\xff", str(sep))

        # compare_latest end to end
        pre2, post2, comp2 = pairshot.compare_latest(d)
        check("compare_latest finds and composes", comp2.is_file())

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        try:
            pairshot.compare_latest(d)
            check("no pair -> clear error", False)
        except pairshot.PairError as e:
            check("no pair -> clear error", "F5" in str(e), str(e)[:60])


def test_log_fast_path():
    print("\n[ReShade.log names the exact pair]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # two pairs on disk; the log names the OLDER one - log must win
        for stem, rgb in (("old", (1, 1, 1)), ("new", (2, 2, 2))):
            make_png(d / f"{stem} [pre-NR].png", 8, 8, rgb)
            make_png(d / f"{stem} [NR on].png", 8, 8, rgb)
        import os, time
        t = time.time()
        os.utime(d / "old [pre-NR].png", (t - 100, t - 100))
        os.utime(d / "new [pre-NR].png", (t, t))
        (d / "ReShade.log").write_text(
            f"INFO | [DLSS 5 Neural Rendering] NR screenshot pair written: "
            f"{d / 'old [pre-NR].png'} and {d / 'old [NR on].png'}\n",
            encoding="utf8")
        found = pairshot.find_latest_pair(d)
        check("the log's pair wins over mtime", found and "old" in found[0].name,
              found[0].name if found else "none")

        # sizes differing must refuse, not mis-compose
        make_png(d / "big [pre-NR].png", 16, 8, (0, 0, 0))
        make_png(d / "big [NR on].png", 8, 8, (0, 0, 0))
        try:
            pairshot.compose(d / "big [pre-NR].png", d / "big [NR on].png")
            check("size mismatch refused", False)
        except pairshot.PairError:
            check("size mismatch refused", True)


def main() -> int:
    print("pairshot tests")
    test_png_roundtrip()
    test_all_filters_decode()
    test_pair_discovery_and_compose()
    test_log_fast_path()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
