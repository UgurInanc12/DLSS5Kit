"""Build DLSS5Kit.exe with PyInstaller.

    python build.py

Produces dist\\DLSS5Kit.exe - a single windowed executable with no console.
The CLI still works from it: DLSS5Kit.exe "D:\\Games\\Game" --check writes to
a console when one is attached, and opens the window when given no arguments.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "DLSS5Kit"


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    print("Installing PyInstaller ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def main() -> int:
    ensure_pyinstaller()
    for d in ("build", "dist"):
        shutil.rmtree(HERE / d, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", NAME,
        # Console kept: the CLI is a real interface, and a windowed build
        # swallows --check and --diagnose output entirely.
        "--console",
        "--noconfirm",
        "--clean",
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE / "build"),
        str(HERE / "dlss5kit.py"),
    ]
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    exe = HERE / "dist" / f"{NAME}.exe"
    if not exe.is_file():
        print("build finished but the exe is missing", file=sys.stderr)
        return 1
    print(f"\nBuilt {exe}  ({exe.stat().st_size / (1024 * 1024):.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
