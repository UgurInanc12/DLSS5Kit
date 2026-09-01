"""AGENTS.md must describe the CLI that actually exists.

A guide for automated callers is worse than no guide when it drifts: an agent
follows an invented flag or branches on an exit code that never occurs, and
the failure is confusing rather than obvious. So the document is checked
against the real argument parser and the real exit codes, offline.

Run with the rest of the suite, or on its own:
    python tests/test_agents_doc.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOC = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"
CLI_PY = ROOT / "dlss5kit" / "cli.py"

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


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "dlss5kit.cli", *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)


def test_no_invented_flags():
    print("\n[every flag in AGENTS.md exists in the CLI]")
    doc = DOC.read_text(encoding="utf8")
    cli = CLI_PY.read_text(encoding="utf8")

    real = set(re.findall(r'p\.add_argument\(\s*"(--[a-z-]+)"', cli))
    mentioned = set(re.findall(r"`(--[a-z-]+)", doc))
    mentioned |= set(re.findall(r"\|\s*`(--[a-z-]+)", doc))

    invented = sorted(f for f in mentioned if f not in real and f != "--help")
    check("no invented flags", not invented, f"not in the CLI: {invented}")

    # --help is argparse's own and needs no prose.
    undocumented = sorted(f for f in real if f not in mentioned)
    check("no undocumented flags", not undocumented,
          f"missing from AGENTS.md: {undocumented}")


def test_exit_codes_are_real():
    print("\n[the documented exit codes are the ones the CLI returns]")
    doc = DOC.read_text(encoding="utf8")
    for code, meaning in (("0", "success"), ("1", "install failed"),
                          ("2", "bad path"), ("3", "not supported"),
                          ("4", "diagnose")):
        check(f"exit {code} is documented", f"| {code} |" in doc)

    # 2: a path that does not exist
    r = run_cli("Z:/definitely/not/here", "--check", "--json")
    check("missing path returns 2", r.returncode == 2, f"got {r.returncode}")
    payload = json.loads(r.stdout)
    check("and reports ok:false in JSON", payload.get("ok") is False, r.stdout[:120])

    # 0: --gpu always works, needs no game
    r = run_cli("--gpu")
    check("--gpu returns 0", r.returncode == 0, f"got {r.returncode}")

    # 4: a folder with a BAD diagnosis (no ReShade.log at all)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # A minimal PE so resolve_target succeeds.
        import struct
        buf = bytearray(b"\0" * 0x400)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 0x3C, 0x80)
        buf[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", buf, 0x84, 0x8664)
        struct.pack_into("<H", buf, 0x98, 0x20B)
        (d / "Game.exe").write_bytes(bytes(buf))
        r = run_cli(str(d), "--diagnose", "--json")
        check("a bad diagnosis returns 4", r.returncode == 4, f"got {r.returncode}")
        payload = json.loads(r.stdout)
        check("verdict is 'bad'", payload.get("verdict") == "bad", r.stdout[:160])
        check("findings are a list", isinstance(payload.get("findings"), list))


def test_json_schema_matches_doc():
    print("\n[the --check --json keys the doc promises are all present]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        import struct
        buf = bytearray(b"\0" * 0x400)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 0x3C, 0x80)
        buf[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", buf, 0x84, 0x8664)
        struct.pack_into("<H", buf, 0x98, 0x20B)
        (d / "Game.exe").write_bytes(bytes(buf) + b"d3d12.dll\0")

        r = run_cli(str(d), "--check", "--json")
        check("--check --json exits 0 or 3", r.returncode in (0, 3),
              f"got {r.returncode}: {r.stderr[:160]}")
        payload = json.loads(r.stdout)

        # Every top-level key the document shows in its jsonc block.
        doc = DOC.read_text(encoding="utf8")
        block = doc.split('"schema": 1,', 1)[1].split("```", 1)[0]
        promised = set(re.findall(r'^\s*"([a-z_]+)":', block, re.M))
        promised.add("schema")
        missing = sorted(k for k in promised if k not in payload)
        check("no promised key is missing", not missing, f"missing: {missing}")

        check("schema is 1", payload.get("schema") == 1)
        check("route is one of the three or null",
              payload.get("route") in ("native", "bridge", "feeder", None),
              str(payload.get("route")))
        check("api_confidence is a known level",
              payload.get("api_confidence") in ("high", "medium", "low"),
              str(payload.get("api_confidence")))
        check("ngx is a dict with the four keys",
              set(payload.get("ngx", {})) == {"d3d11", "d3d12", "vulkan", "any"},
              str(payload.get("ngx")))
        check("gpu block carries a generation",
              "generation" in payload.get("gpu", {}), str(payload.get("gpu")))


def test_documented_route_names():
    print("\n[route names in the doc match the code]")
    from dlss5kit import routes
    doc = DOC.read_text(encoding="utf8")
    for r in (routes.NATIVE, routes.BRIDGE, routes.FEEDER):
        check(f"'{r}' is described", f"`{r}`" in doc)
    check("the feeder DLAA trade-off is stated",
          "DLAA" in doc and "never upscaling" in doc)
    check("the off-by-default trap is stated",
          "off by default" in doc)
    check("the game-must-be-closed rule is stated",
          "must be closed" in doc)


def test_claude_md_points_at_agents_md():
    print("\n[CLAUDE.md defers to AGENTS.md]")
    check("CLAUDE.md exists", CLAUDE.is_file())
    t = CLAUDE.read_text(encoding="utf8")
    check("it links AGENTS.md", "AGENTS.md" in t)
    check("it repeats the three rules", "route" in t and "closed" in t
          and "off by default" in t)


def main() -> int:
    print("AGENTS.md contract tests")
    test_no_invented_flags()
    test_documented_route_names()
    test_claude_md_points_at_agents_md()
    test_exit_codes_are_real()
    test_json_schema_matches_doc()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
