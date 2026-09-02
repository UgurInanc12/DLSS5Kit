"""Mutation check: revert each fix, prove a test turns red, restore.

Run from the repo root. Every mutation is applied to a COPY of the file's
text held in memory and written back verbatim afterwards, and the script
verifies the restore by hashing before and after.
"""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "dlss5kit" / "peinfo.py"
I = ROOT / "dlss5kit" / "installer.py"
R = ROOT / "dlss5kit" / "routes.py"

MUTATIONS = [
    ("length floor (rc.exe)", P,
     "and min(len(fn), len(sn)) >= 4:", ":"),
    ("_SKIP_PARTS configurator", P,
     '"configurator", "config", "settings", "options",', ""),
    ("_ENGINE_TOOLS cryengine", P,
     '"rc", "cgc", "cgfdump", "luacompiler", "fxc", "editor", "sandbox",', ""),
    ("FC3 tie-break", P,
     "key=lambda p: (_score(p, folder), _api_selfevidence(p)),\n                    reverse=True)",
     "key=lambda p: _score(p, folder), reverse=True)"),
    ("exact import match", P,
     'if imp_exact("d3d11.dll"):', 'if has_imp("d3d11.dll"):'),
    ("gfsdk helper filter", P,
     'helper = ("gfsdk_", "nvapi"', 'helper = ("zzz_", "nvapi"'),
    ("anti-cheat in --check", R,
     "ac = anticheat_warning(folder)", 'ac = ""'),
    ("anti-cheat install_root", I,
     "detect_anticheat(install_root(root))", "detect_anticheat(root)"),
]


def run_tests():
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    r = subprocess.run([sys.executable, "tests/test_all.py"],
                       cwd=ROOT, capture_output=True, text=True, env=env)
    m = re.search(r"(\d+) passed, (\d+) failed", r.stdout)
    fails = [l.strip() for l in r.stdout.splitlines()
             if l.strip().startswith("FAIL")]
    if not m:
        return "?", "?", ["NO SUMMARY: " + r.stdout[-300:]]
    return m.group(1), m.group(2), fails


def main():
    # Crash safety: a mutation left on disk by an interrupted run is worse
    # than no run at all, so every original is saved beside the file first
    # and restored from there, even after a kill.
    saved = {}
    for p in (P, I, R):
        bak = p.with_suffix(p.suffix + ".mutbak")
        if bak.exists():
            print(f"restoring an earlier interrupted run: {p.name}")
            p.write_bytes(bak.read_bytes())
        bak.write_bytes(p.read_bytes())
        saved[p] = bak

    try:
        return _run_all()
    finally:
        for p, bak in saved.items():
            p.write_bytes(bak.read_bytes())
            bak.unlink()


def _run_all():
    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (P, I, R)}
    p, f, _ = run_tests()
    print(f"BASELINE: {p} passed, {f} failed")
    if f != "0":
        print("baseline is not green, aborting")
        return 1

    bad = []
    for label, path, old, new in MUTATIONS:
        original = path.read_text(encoding="utf8")
        if old not in original:
            print(f"ANCHOR MISSING     [{label}]")
            bad.append(label)
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf8")
            passed, failed, fails = run_tests()
        finally:
            path.write_text(original, encoding="utf8")
        caught = failed not in ("0", "?")
        print(f"{'CAUGHT' if caught else '!!! NOT CAUGHT !!!':18} "
              f"[{label}] -> {passed} passed, {failed} failed")
        for x in fails[:2]:
            print(f"       {x[:95]}")
        if not caught:
            bad.append(label)

    for path, want in digests.items():
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"restore {path.name}: {'ok' if got == want else 'CORRUPTED'}")
        if got != want:
            bad.append(f"restore {path.name}")

    p, f, _ = run_tests()
    print(f"FINAL: {p} passed, {f} failed")
    if bad:
        print("\nUNPROTECTED / BROKEN: " + ", ".join(bad))
        return 1
    print("\nevery fix is protected by a test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
