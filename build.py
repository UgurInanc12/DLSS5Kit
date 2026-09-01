"""Build DLSS5Kit.exe with PyInstaller.

    python build.py

Produces dist\\DLSS5Kit.exe - a single windowed executable with no console.
The CLI still works from it: DLSS5Kit.exe "D:\\Games\\Game" --check writes to
a console when one is attached, and opens the window when given no arguments.
"""
from __future__ import annotations

import os
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
    ]
    # Bundle the in-game control panel add-on when it has been built
    # (addon/build_addon.bat, needs MSVC). The installer looks for it in
    # sys._MEIPASS and quietly skips the step when it is absent, so a build
    # without MSVC still produces a fully working exe.
    addon = HERE / "addon" / "dlss5kit.addon64"
    if addon.is_file():
        cmd += ["--add-data", f"{addon}{os.pathsep}."]
        print(f"bundling {addon.name} ({addon.stat().st_size // 1024} KB)")
    else:
        print("addon/dlss5kit.addon64 not built - exe will not bundle the "
              "in-game control panel")
    cmd.append(str(HERE / "dlss5kit.py"))
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
