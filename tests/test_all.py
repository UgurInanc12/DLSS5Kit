"""Offline tests. No network, no game, no GPU required.

The point of these is the install/uninstall round trip: a folder must come
out of Remove byte-identical to how it went in, including the game's own
files that were replaced.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlss5kit import config, diagnose, gpu, installer, peinfo, routes, sources  # noqa: E402

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


def make_pe(path: Path, machine: int = peinfo.PE_X64,
            extra: bytes = b"") -> Path:
    """A minimal but genuinely parseable PE file."""
    pe_off = 0x80
    buf = bytearray(b"\0" * 0x400)
    buf[0:2] = b"MZ"
    struct.pack_into("<I", buf, 0x3C, pe_off)
    buf[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<H", buf, pe_off + 4, machine)     # Machine
    struct.pack_into("<H", buf, pe_off + 6, 0)           # NumberOfSections
    struct.pack_into("<H", buf, pe_off + 20, 0)          # SizeOfOptionalHeader
    struct.pack_into("<H", buf, pe_off + 24, 0x20B)      # PE32+ magic
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(buf) + extra)
    return path


def make_reshade_setup(path: Path) -> Path:
    """A stub of the real thing: a PE with a zip appended."""
    stub = bytearray(b"\0" * 4096)
    stub[0:2] = b"MZ"
    path.write_bytes(bytes(stub))
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ReShade64.dll", b"ReShade" + b"\0" * (1_200_000))
        z.writestr("ReShade32.dll", b"ReShade" + b"\0" * (1_000_000))
        z.writestr("ReShade64.json", "{}")
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# Every install test pins a card explicitly. Without this the suite depends on
# whatever GPU the machine happens to have, and a CI runner with no NVIDIA
# card fails the install tests for a reason that has nothing to do with the
# code under test. RTX 30 is used because the stand-in component files carry
# no CUDA fatbins at all, so any supported generation behaves identically.
TEST_CARD = gpu.Card(name="Test RTX 3090", sm=86)


def snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): sha(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ ini

def test_ini():
    print("\n[ini]")
    ini = config.Ini.parse("[GENERAL]\nA=1\nB=2\n\n[ADDON]\nC=3\n")
    check("parse reads sections", ini.get("GENERAL", "A") == "1")
    check("section lookup is case-insensitive", ini.get("general", "b") == "2")
    ini.set("GENERAL", "A", "9")
    check("set replaces in place", ini.get("GENERAL", "A") == "9")
    ini.set_default("GENERAL", "A", "0")
    check("set_default does not overwrite", ini.get("GENERAL", "A") == "9")
    check("dump round trips", "A=9" in ini.dump() and "[ADDON]" in ini.dump())

    check("split_list splits", config.split_list("a,b,c") == ["a", "b", "c"])
    check("split_list unescapes ',,'",
          config.split_list("a,,b,c") == ["a,b", "c"],
          str(config.split_list("a,,b,c")))
    check("join_list escapes", config.join_list(["a,b", "c"]) == "a,,b,c")
    check("list round trip",
          config.split_list(config.join_list(["x,y", "z"])) == ["x,y", "z"])


def test_preset_order():
    print("\n[technique ordering]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ReShadePreset.ini").write_text(
            "Techniques=UserShader@User.fx\n"
            "TechniqueSorting=UserShader@User.fx\n", encoding="utf8")
        config.write_preset(d, 3)
        ini = config.Ini.load(d / "ReShadePreset.ini")
        techs = config.split_list(ini.get("", "Techniques"))
        check("provider is first", techs[0] == "Lumenite_Kernel@lumenite_Kernel.fx",
              str(techs))
        check("feed is second", techs[1] == config.FEED_TECHNIQUE, str(techs))
        check("the user's shader survives", "UserShader@User.fx" in techs)
        check("provider sits ABOVE feed",
              techs.index("Lumenite_Kernel@lumenite_Kernel.fx")
              < techs.index(config.FEED_TECHNIQUE))
        check("define written",
              "DLSS5_MV_PROVIDER=3" in (ini.get("", "PreprocessorDefinitions") or ""))

        # installing twice must not duplicate entries
        config.write_preset(d, 3)
        ini = config.Ini.load(d / "ReShadePreset.ini")
        techs2 = config.split_list(ini.get("", "Techniques"))
        check("no duplicates on reinstall", len(techs2) == len(techs), str(techs2))

        config.remove_our_techniques(d)
        ini = config.Ini.load(d / "ReShadePreset.ini")
        left = config.split_list(ini.get("", "Techniques"))
        check("removal leaves only the user's", left == ["UserShader@User.fx"],
              str(left))


def test_ini_preserves_user():
    print("\n[ini preserves the user's settings]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ReShade.ini").write_text(
            "[GENERAL]\nEffectSearchPaths=D:\\MyShaders\\**\n"
            "PreprocessorDefinitions=MY_OWN=7\n"
            "[INPUT]\nKeyOverlay=36,0,0,0\n", encoding="utf8")
        config.write_reshade_ini(d, 3)
        ini = config.Ini.load(d / "ReShade.ini")
        check("existing EffectSearchPaths kept",
              ini.get("GENERAL", "EffectSearchPaths") == "D:\\MyShaders\\**")
        check("existing hotkey kept", ini.get("INPUT", "KeyOverlay") == "36,0,0,0")
        defs = ini.get("GENERAL", "PreprocessorDefinitions")
        check("the user's define kept", "MY_OWN=7" in defs, defs)
        check("our define added", "DLSS5_MV_PROVIDER=3" in defs, defs)
        check("AddonPath set", ini.get("ADDON", "AddonPath") == ".\\")

        # native/bridge routes must not add shader paths to a clean folder
        d2 = d / "clean"
        d2.mkdir()
        config.write_reshade_ini(d2, None)
        ini2 = config.Ini.load(d2 / "ReShade.ini")
        check("no shader paths on the addon-only route",
              ini2.get("GENERAL", "EffectSearchPaths") is None)
        check("addon path still set", ini2.get("ADDON", "AddonPath") == ".\\")


# ---------------------------------------------------------------- PE

def test_pe():
    print("\n[pe]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        e64 = make_pe(d / "Game.exe", peinfo.PE_X64)
        e32 = make_pe(d / "Old.exe", peinfo.PE_X86)
        check("64-bit read", peinfo.exe_bitness(e64) == 64)
        check("32-bit read", peinfo.exe_bitness(e32) == 32)
        bad = d / "notpe.exe"
        bad.write_bytes(b"hello")
        try:
            peinfo.exe_bitness(bad)
            check("non-PE rejected", False)
        except peinfo.PEError:
            check("non-PE rejected", True)


def test_api_detection_from_strings():
    print("\n[api detection: LoadLibrary renderers]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # A game whose renderer is only ever a string (the Crysis 3 shape).
        exe = make_pe(d / "Game.exe", peinfo.PE_X64,
                      extra=b"d3d11.dll\0" + b"NVSDK_NGX_D3D11_CreateFeature\0" * 5)
        info = peinfo.detect_api(exe, d)
        check("string-only d3d11 detected as DX11", info.api == peinfo.DX11,
              f"{info.api} / {info.reason}")
        check("NGX D3D11 usage seen", info.ngx_d3d11 is True)
        check("NGX D3D12 usage not seen", info.ngx_d3d12 is False)

        exe2 = make_pe(d / "G12.exe", peinfo.PE_X64,
                       extra=b"d3d12.dll\0NVSDK_NGX_D3D12_CreateFeature\0")
        info2 = peinfo.detect_api(exe2, d)
        check("string-only d3d12 detected as DX12", info2.api == peinfo.DX12)
        check("NGX D3D12 usage seen", info2.ngx_d3d12 is True)


def test_api_from_log_wins():
    print("\n[api detection: ReShade.log outranks the executable]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        exe = make_pe(d / "Game.exe", peinfo.PE_X64, extra=b"d3d12.dll\0")
        (d / "ReShade.log").write_text(
            "INFO | Redirecting D3D11CreateDeviceAndSwapChain(...)\n" * 3,
            encoding="utf8")
        info = peinfo.detect_api(exe, d)
        check("log-derived DX11 beats the string scan", info.api == peinfo.DX11,
              f"{info.api} / {info.reason}")
        check("confidence is high", info.confidence == "high")


# ------------------------------------------------------------- routing

def test_routing():
    print("\n[routing]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # D3D12 game with D3D12 NGX -> native
        api = peinfo.ApiInfo(api=peinfo.DX12, ngx_d3d12=True, confidence="high")
        (d / "sl.interposer.dll").write_bytes(b"x")
        p = routes.choose(d, api, 64)
        check("D3D12 + NGX D3D12 -> native", p.route == routes.NATIVE, p.route)
        check("native dlss seen", p.native_dlss is True)

        # The Crysis 3 case: DLSS files present, but D3D11 + NGX D3D11
        api11 = peinfo.ApiInfo(api=peinfo.DX11, ngx_d3d11=True, confidence="high",
                               strings={"ngx_d3d11": 27})
        p2 = routes.choose(d, api11, 64)
        check("D3D11 + own DLSS -> bridge, NOT native",
              p2.route == routes.BRIDGE, p2.route)
        check("the reason names the real cause",
              "D3D12" in p2.reason and "NVSDK_NGX_D3D11" in p2.reason)

        # No DLSS at all, D3D12
        d2 = d / "plain"
        d2.mkdir()
        apip = peinfo.ApiInfo(api=peinfo.DX12, confidence="high")
        p3 = routes.choose(d2, apip, 64)
        check("D3D12 without DLSS -> feeder", p3.route == routes.FEEDER, p3.route)

        # 32-bit D3D11/D3D12 is supported through the feeder's out-of-process
        # 64-bit helper (DLSS5-Feeder's addon32 + host64\). NGX is x64-only
        # and 32-bit ReShade loads .addon32 only, so this is the ONLY route
        # available there - and only on D3D11/D3D12.
        p4 = routes.choose(d2, apip, 32)
        check("32-bit D3D12 -> feeder", p4.supported and p4.route == routes.FEEDER,
              f"{p4.supported}/{p4.route}")
        check("and it explains the helper process",
              "64-bit helper" in p4.reason or "host64" in p4.reason,
              p4.reason[:70])
        check("no alternative routes offered on 32-bit", p4.options == [],
              str(p4.options))
        check("the beta status is stated",
              any("beta" in w for w in p4.warnings), str(p4.warnings)[:70])

        # 32-bit OpenGL used to be refused here. It is not: upstream's 32-bit
        # add-on accepts "Direct3D 11, OpenGL and Vulkan"
        # (src/dlss5-feed32.cpp), and their Status table verifies OpenGL on
        # Worms Ultimate Mayhem (32-bit, 4K, 0.13 ms/frame). This assertion
        # was inverted on 2026-09-02 after reading the upstream source: the
        # old expectation encoded our refusal, not reality.
        p5 = routes.choose(d2, peinfo.ApiInfo(api=peinfo.OPENGL,
                                              confidence="high"), 32)
        check("32-bit OpenGL is supported, not refused",
              p5.supported and p5.route == routes.FEEDER,
              f"{p5.supported}/{p5.route}")
        check("and it says ReShade goes in as opengl32.dll",
              "opengl32.dll" in p5.reason, p5.reason[:70])

        # D3D10 has no translation layer and no hook, so it stays refused -
        # proving the fall-through above did not simply open the gates.
        p6 = routes.choose(d2, peinfo.ApiInfo(api=peinfo.DX10,
                                              confidence="high"), 32)
        check("32-bit D3D10 is still refused", p6.supported is False)
        check("and the blocker names DirectX 10",
              "DirectX 10" in p6.blocker, p6.blocker[:70])

        # Vulkan warns about the global layer
        pv = routes.choose(d2, peinfo.ApiInfo(api=peinfo.VULKAN), 64)
        check("Vulkan -> bridge", pv.route == routes.BRIDGE)
        check("Vulkan warns it is global",
              any("EVERY Vulkan" in w for w in pv.warnings))


# ----------------------------------------------------------- install

def build_local(dirp: Path) -> Path:
    """A folder of stand-in components, as if already downloaded."""
    dirp.mkdir(parents=True, exist_ok=True)
    (dirp / installer.RENODX).write_bytes(b"RENODX-ADDON" + b"\0" * 5000)
    (dirp / installer.DLSSNR).write_bytes(b"DLSSNR-RUNTIME" + b"\0" * 9000)
    (dirp / installer.DLSS).write_bytes(b"DLSS-RUNTIME" + b"\0" * 7000)
    (dirp / installer.BRIDGE_ADDON).write_bytes(b"BRIDGE-ADDON" + b"\0" * 4000)
    (dirp / installer.FEEDER_ADDON).write_bytes(b"FEEDER-ADDON" + b"\0" * 4000)
    (dirp / installer.FEEDER_FX).write_bytes(b"// DLSS5_Feed.fx")
    return dirp


def _install_bridge(game: Path, local: Path, setup: Path):
    exe = game / "Game.exe"
    api = peinfo.ApiInfo(api=peinfo.DX11, ngx_d3d11=True, confidence="high")
    plan = routes.choose(game, api, 64)
    opt = installer.Options(route=routes.BRIDGE, local_dir=local,
                            reshade_setup=setup, card=TEST_CARD)
    return installer.install(game, exe, api, 64, plan, opt), plan, api


def test_install_uninstall_round_trip():
    print("\n[install / uninstall round trip]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        # The game's own files that our install will replace.
        (game / installer.DLSS).write_bytes(b"THE GAMES OWN DLSS RUNTIME")
        (game / "ReShade.ini").write_text("[GENERAL]\nMine=1\n", encoding="utf8")
        (game / "sl.interposer.dll").write_bytes(b"streamline")
        (game / "game.pak").write_bytes(b"untouchable asset data")

        before = snapshot(game)
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "ReShade_Setup_6.8.0_Addon.exe")

        rep, plan, api = _install_bridge(game, local, setup)
        check("install reports complete", rep.complete is True)
        check("route recorded as bridge", rep.route == routes.BRIDGE)
        check("proxy dll written", (game / installer.PROXY_DLL).is_file())
        check("proxy really is ReShade",
              installer.is_reshade(game / installer.PROXY_DLL))
        check("bridge addon written", (game / installer.BRIDGE_ADDON).is_file())
        check("renodx written", (game / installer.RENODX).is_file())
        check("dlssnr written", (game / installer.DLSSNR).is_file())
        check("bridge cfg written", (game / config.BRIDGE_CFG).is_file())
        check("manifest written", (game / installer.MANIFEST).is_file())

        # the game's own DLSS was replaced, so it must have been backed up
        bak = game / (installer.DLSS + installer.BACKUP_SUFFIX)
        check("game's own dlss backed up", bak.is_file())
        check("backup holds the ORIGINAL bytes",
              bak.read_bytes() == b"THE GAMES OWN DLSS RUNTIME")
        check("the game's asset is untouched",
              (game / "game.pak").read_bytes() == b"untouchable asset data")
        # feeder-only artefacts must NOT appear on the bridge route
        check("no feeder addon on the bridge route",
              not (game / installer.FEEDER_ADDON).is_file())
        check("no shader tree on the bridge route",
              not (game / "reshade-shaders").is_dir())

        lines = installer.uninstall(game)
        after = snapshot(game)
        check("uninstall restores the folder exactly", after == before,
              f"added={set(after) - set(before)} removed={set(before) - set(after)}")
        check("the game's own dlss is back byte-for-byte",
              (game / installer.DLSS).read_bytes() == b"THE GAMES OWN DLSS RUNTIME")
        check("ReShade.ini is the user's again",
              (game / "ReShade.ini").read_text() == "[GENERAL]\nMine=1\n")
        check("manifest removed", not (game / installer.MANIFEST).is_file())


def test_double_install_then_remove():
    """Installing twice must still uninstall cleanly.

    This is the bug that makes a folder come out of 'uninstall' still fully
    set up: the second install backs up the FIRST install's files, so the
    uninstall restores them instead of deleting them.
    """
    print("\n[install twice, then remove]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        (game / installer.DLSS).write_bytes(b"ORIGINAL GAME DLSS")
        before = snapshot(game)

        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")

        _install_bridge(game, local, setup)
        _install_bridge(game, local, setup)          # again
        installer.uninstall(game)

        after = snapshot(game)
        check("folder restored after a double install", after == before,
              f"left over: {sorted(set(after) - set(before))}")
        check("original dlss restored, not ours",
              (game / installer.DLSS).read_bytes() == b"ORIGINAL GAME DLSS")


def test_route_switch_cleans_up():
    print("\n[switching routes removes the previous one]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d12.dll\0")
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")
        exe = game / "Game.exe"
        api = peinfo.ApiInfo(api=peinfo.DX12, ngx_d3d12=True, confidence="high")
        plan = routes.choose(game, api, 64)

        installer.install(game, exe, api, 64, plan,
                          installer.Options(route=routes.BRIDGE, local_dir=local,
                                            reshade_setup=setup, card=TEST_CARD))
        check("bridge addon present", (game / installer.BRIDGE_ADDON).is_file())

        installer.install(game, exe, api, 64, plan,
                          installer.Options(route=routes.NATIVE, local_dir=local,
                                            reshade_setup=setup, card=TEST_CARD))
        check("bridge addon removed when switching to native",
              not (game / installer.BRIDGE_ADDON).is_file())
        check("manifest now says native",
              installer.installed_route(game) == routes.NATIVE)
        check("renodx still present", (game / installer.RENODX).is_file())


def test_refuses_foreign_injector():
    print("\n[refuses to overwrite another injector]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        (game / "dxgi.dll").write_bytes(b"DXVK" + b"\0" * 2_000_000)
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")
        try:
            _install_bridge(game, local, setup)
            check("foreign dxgi.dll refused", False, "install went ahead")
        except installer.InstallError as e:
            check("foreign dxgi.dll refused", True)
            check("the refusal names the file", "dxgi.dll" in str(e))
        check("the foreign dll was not touched",
              (game / "dxgi.dll").read_bytes()[:4] == b"DXVK")


def test_anticheat_warning():
    print("\n[anti-cheat]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        (game / "EasyAntiCheat").mkdir(parents=True)
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        name, ev = installer.detect_anticheat(game)
        check("EAC folder detected", name == "Easy Anti-Cheat", f"{name} {ev}")

        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")
        rep, _, _ = _install_bridge(game, local, setup)
        check("install proceeds but warns",
              rep.complete and any("Anti-Cheat" in w for w in rep.warnings))


def test_zip_extraction():
    print("\n[extraction]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        setup = make_reshade_setup(d / "setup.exe")
        data = installer.extract_member(setup, "ReShade64.dll")
        check("ReShade64.dll pulled out of the setup exe",
              data.startswith(b"ReShade") and len(data) > 1_000_000)
        try:
            installer.extract_member(setup, "NoSuch.dll")
            check("missing member raises", False)
        except installer.InstallError:
            check("missing member raises", True)


def test_diagnose():
    print("\n[diagnose]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # no log at all
        r = diagnose.diagnose(d)
        check("missing log is reported as bad", r.verdict == diagnose.BAD)

        # the mismatch this tool exists to catch
        (d / "ReShade.log").write_text(
            "INFO | Initializing crosire's ReShade version '6.8.0.2155'\n"
            "INFO | Redirecting D3D11CreateDeviceAndSwapChain(...)\n"
            "INFO | Registered add-on \"DLSS 5 Neural Rendering\" v0.1\n"
            "DEBUG | vtable::Hook(NVSDK_NGX_D3D12_CreateFeaturehooked with x)\n",
            encoding="utf8")
        r = diagnose.diagnose(d)
        check("D3D12 hook on a D3D11 game is caught",
              any("MISMATCH" in f.text for f in r.findings))
        check("verdict is bad", r.verdict == diagnose.BAD)
        check("the fix is named",
              any("bridge" in (f.evidence + f.text).lower() for f in r.findings))

        # zero motion vectors on the feeder route
        (d / "dlss5-feed.log").write_text(
            "feature ready DLAA\nMV probe: 0% non-zero\n", encoding="utf8")
        r2 = diagnose.diagnose(d, route="feeder")
        check("zero motion vectors caught",
              any("Motion vectors are all zero" in f.text for f in r2.findings))
        check("ordering named as the cause",
              any("above DLSS 5 Feed" in f.evidence for f in r2.findings))


def test_manifest_survives_failure():
    print("\n[a failed install is still removable]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        setup = make_reshade_setup(base / "setup.exe")
        # A local folder missing the bridge add-on forces a failure partway,
        # after ReShade has already been written.
        local = base / "local"
        local.mkdir()
        (local / installer.RENODX).write_bytes(b"x" * 100)
        exe = game / "Game.exe"
        api = peinfo.ApiInfo(api=peinfo.DX11, ngx_d3d11=True, confidence="high")
        plan = routes.choose(game, api, 64)
        opt = installer.Options(route=routes.BRIDGE, local_dir=local,
                                reshade_setup=setup, card=TEST_CARD)
        # Point the resolver at nothing so the download step fails.
        import dlss5kit.sources as S
        original = S.resolve_bridge
        S.resolve_bridge = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
        try:
            installer.install(game, exe, api, 64, plan, opt)
            check("install failed as designed", False, "it succeeded")
        except Exception:
            check("install failed as designed", True)
        finally:
            S.resolve_bridge = original

        check("manifest written despite the failure",
              (game / installer.MANIFEST).is_file())
        man = json.loads((game / installer.MANIFEST).read_text())
        check("manifest marks it incomplete", man["complete"] is False)
        check("the proxy it did write is listed",
              installer.PROXY_DLL in man["files"])
        installer.uninstall(game)
        check("the partial install is removable",
              not (game / installer.PROXY_DLL).is_file())


def test_identical_file_skips_backup():
    """A file identical to what we are about to write needs no backup.

    Without this, reinstalling over an already-correct 165 MB
    nvngx_dlssnr.dll costs a pointless 165 MB copy - and the copy is of OUR
    file, so uninstall would restore it and leave the folder set up.
    """
    print("\n[identical files are not backed up]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")

        # The folder already holds byte-identical copies, as if a previous
        # manual install had put them there.
        for name in (installer.RENODX, installer.DLSSNR):
            shutil.copyfile(local / name, game / name)
        before = snapshot(game)

        rep, _, _ = _install_bridge(game, local, setup)
        check("no backup for the identical renodx",
              not (game / (installer.RENODX + installer.BACKUP_SUFFIX)).is_file())
        check("no backup for the identical dlssnr",
              not (game / (installer.DLSSNR + installer.BACKUP_SUFFIX)).is_file())
        check("the identical files are still in the manifest",
              installer.RENODX in rep.written and installer.DLSSNR in rep.written)
        check("it is noted, not silent",
              any("identical" in n for n in rep.notes))

        installer.uninstall(game)
        after = snapshot(game)
        # The identical copies are ours to remove: they entered the manifest.
        check("uninstall removes them",
              not (game / installer.RENODX).is_file()
              and not (game / installer.DLSSNR).is_file())
        check("no stray backups left behind",
              not any(p.name.endswith(installer.BACKUP_SUFFIX)
                      for p in game.rglob("*")))


def test_generations_and_ptx():
    """Generation picking, and CUBIN vs PTX compatibility.

    The PTX half matters: nvngx_dlss 310.8.0 carries no sm_120 CUBIN yet runs
    on RTX 50 because the driver JIT-compiles its sm_89 PTX. A CUBIN-only
    check calls that file incompatible, which is wrong.
    """
    print("\n[gpu generations and PTX]")
    check("RTX 20 maps to sm_75", gpu.GEN_SM[gpu.RTX20] == 75)
    check("RTX 30 maps to sm_86", gpu.GEN_SM[gpu.RTX30] == 86)
    check("RTX 40 maps to sm_89", gpu.GEN_SM[gpu.RTX40] == 89)
    check("RTX 50 maps to sm_120", gpu.GEN_SM[gpu.RTX50] == 120)
    check("four generations offered", len(gpu.GENERATIONS) == 4)

    for name, sm in (("RTX 2080 Ti", 75), ("GTX 1660 Ti", 75),
                     ("NVIDIA GeForce RTX 3090", 86),
                     ("NVIDIA GeForce RTX 4090", 89),
                     ("NVIDIA GeForce RTX 5090", 120),
                     ("NVIDIA GeForce RTX 5070 Laptop GPU", 120),
                     ("NVIDIA RTX A4000", 86),
                     ("NVIDIA TITAN RTX", 75),
                     ("NVIDIA GeForce GTX 1080 Ti", 61)):
        check(f"{name} -> sm_{sm}", gpu.sm_for_name(name) == sm,
              str(gpu.sm_for_name(name)))
    check("AMD card is not NVIDIA", gpu.sm_for_name("AMD Radeon RX 7900 XTX") is None)
    check("Intel iGPU is not NVIDIA", gpu.sm_for_name("Intel(R) UHD Graphics") is None)

    # Hand-picked generations
    c = gpu.card_for_generation(gpu.RTX40)
    check("hand-picked RTX 40 has sm_89", c.sm == 89)
    check("hand-picked card is marked not-detected", c.detected is False)
    check("hand-picked card is supported", c.supported is True)
    check("GTX 10 is refused", gpu.card_supported(61)[0] is False)
    check("no card is refused", gpu.card_supported(None)[0] is False)

    # CUBIN only, RTX 50 -> nothing older can run it
    a = gpu.Archs(cubin={120}, ptx={120})
    check("sm_120-only does not run on RTX 30", a.runs_on(86)[0] is False)
    check("sm_120-only runs on RTX 50", a.runs_on(120)[0] is True)
    gens = a.generations()
    check("RTX 50 only build reports one generation",
          [g for g, ok in gens.items() if ok] == [gpu.RTX50], str(gens))

    # The real nvngx_dlss shape: CUBIN up to sm_89, PTX at 89
    b = gpu.Archs(cubin={75, 80, 86, 89}, ptx={80, 89})
    ok, how = b.runs_on(120)
    check("sm_89 PTX runs on RTX 50 by JIT", ok is True and "JIT" in how, how)
    check("native where a CUBIN exists", b.runs_on(86) == (True, "native"))
    check("all four generations covered",
          all(b.generations().values()), str(b.generations()))

    # PTX newer than the card cannot help it
    c2 = gpu.Archs(cubin=set(), ptx={120})
    check("sm_120 PTX does not help an RTX 30", c2.runs_on(86)[0] is False)

    check("empty Archs is falsy", not gpu.Archs())
    check("summary mentions JIT", "JIT" in b.summary(), b.summary())


def test_native_dlss_veto():
    """Loose DLSS DLLs in a folder are not proof the game has DLSS.

    Batman: Arkham Knight (2015) had the whole Streamline set copied into it
    by hand. Its executable contains zero NGX references. A folder-only check
    routes it to bridge, which cannot work - there is no contract to mirror.
    """
    print("\n[loose DLSS files do not mean the game has DLSS]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for n in ("sl.interposer.dll", "sl.dlss.dll", "nvngx_dlss.dll"):
            (d / n).write_bytes(b"x")

        # Executable that never calls NGX: the files are leftovers.
        no_ngx = peinfo.ApiInfo(api=peinfo.DX11, confidence="high")
        present, ev, note = routes.detect_native_dlss(d, no_ngx)
        check("files alone do not count as native DLSS", present is False)
        check("the files are still reported as evidence", len(ev) == 3, str(ev))
        check("the reason is stated", "leftovers" in note, note)

        plan = routes.choose(d, no_ngx, 64)
        check("routed to feeder, not bridge", plan.route == routes.FEEDER,
              plan.route)
        check("feeder DLAA cost is warned about",
              any("DLAA" in w for w in plan.warnings) or "DLAA" in plan.reason)

        # Same folder, but an executable that really does call NGX.
        with_ngx = peinfo.ApiInfo(api=peinfo.DX11, ngx_d3d11=True,
                                  confidence="high", strings={"ngx_d3d11": 27})
        present2, _, note2 = routes.detect_native_dlss(d, with_ngx)
        check("with NGX in the exe it does count", present2 is True)
        check("no leftover note in that case", note2 == "")
        check("that one routes to bridge",
              routes.choose(d, with_ngx, 64).route == routes.BRIDGE)

        # No executable information at all: fall back to the files.
        present3, _, _ = routes.detect_native_dlss(d, None)
        check("without exe info the files are trusted", present3 is True)


def test_generation_blocks_install():
    print("\n[an unsupported card is refused]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        api = peinfo.ApiInfo(api=peinfo.DX12, ngx_d3d12=True, confidence="high")
        old = gpu.Card(name="GeForce GTX 1080", sm=61)
        plan = routes.choose(d, api, 64, old)
        check("Pascal is refused", plan.supported is False)
        check("the refusal names Tensor cores", "Tensor" in plan.blocker,
              plan.blocker)
        ok = routes.choose(d, api, 64, gpu.card_for_generation(gpu.RTX20))
        check("RTX 20 is accepted", ok.supported is True)


def test_bridge_private_device_does_not_flip_the_verdict():
    """Our own bridge creates a D3D12 device; that is not the game doing it.

    Measured on Crysis 3 Remastered after a successful bridge install: the log
    holds 12 D3D11CreateDevice calls (the game) and 1 D3D12CreateDevice call
    (dlss5-bridge's private session). Reading "any D3D12 call" as proof of a
    D3D12 game flips the next inspection to native and recommends the exact
    route that was already proven not to work.
    """
    print("\n[the bridge's own D3D12 device does not flip the verdict]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        exe = make_pe(d / "Game.exe", peinfo.PE_X64,
                      extra=b"d3d11.dll\0NVSDK_NGX_D3D11_CreateFeature\0")
        (d / "dlss5-bridge.addon64").write_bytes(b"bridge")
        # The game's own DLSS, as Crysis 3 has: without it the correct answer
        # would be feeder, and the test would prove nothing about the flip.
        (d / "sl.interposer.dll").write_bytes(b"x")
        (d / "ReShade.log").write_text(
            "INFO | Redirecting D3D11CreateDeviceAndSwapChain(...)\n" * 12
            + "INFO | Redirecting D3D12CreateDevice(...)\n", encoding="utf8")

        info = peinfo.detect_api(exe, d)
        check("still reported as DX11", info.api == peinfo.DX11,
              f"{info.api} / {info.reason}")
        check("the reason names the bridge", "dlss5-bridge" in info.reason,
              info.reason)
        check("routes back to bridge, not native",
              routes.choose(d, info, 64).route == routes.BRIDGE)

        # A genuine D3D12 game is unaffected: D3D12 calls dominate.
        d2 = Path(td) / "real12"
        d2.mkdir()
        exe2 = make_pe(d2 / "Game.exe", peinfo.PE_X64,
                       extra=b"d3d12.dll\0NVSDK_NGX_D3D12_CreateFeature\0")
        (d2 / "ReShade.log").write_text(
            "INFO | Redirecting D3D12CreateDevice(...)\n" * 3, encoding="utf8")
        info2 = peinfo.detect_api(exe2, d2)
        check("a real D3D12 game is still DX12", info2.api == peinfo.DX12,
              f"{info2.api} / {info2.reason}")
        (d2 / "sl.dlss.dll").write_bytes(b"x")
        check("and it routes to native",
              routes.choose(d2, info2, 64).route == routes.NATIVE)


def test_uppercase_module_names_and_ngx_evidence():
    """A game that stores "D3D12", not "d3d12.dll", must still be seen.

    Measured on Metro Exodus Enhanced Edition 2026-09-01: the executable has
    zero occurrences of "d3d12.dll" or "dxgi.dll" in any case. It carries the
    bare uppercase "D3D12" 26 times, D3D12CreateDevice once, and
    NVSDK_NGX_D3D12_* 21 times. A lower-case-filename-only scan reported "no
    graphics API could be identified" and the route fell through to the D3D11
    default, recommending bridge for a D3D12-only game.

    The second half guards the fix's own trap: NGX ships parameter names like
    NVSDK_NGX_Parameter_GetD3d11Resource in EVERY NGX game whatever its
    renderer, so a loose case-insensitive "d3d11" search counts those as D3D11
    evidence and flips the verdict straight back.
    """
    print("\n[uppercase module names and NGX as renderer evidence]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # The Metro shape: uppercase bare name + entry point + NGX D3D12, and
        # the NGX parameter strings that contain a lower-case "d3d11".
        metro = make_pe(d / "MetroExodus.exe", peinfo.PE_X64, extra=(
            b"\x00D3D12\x00" * 3
            + b"D3D12CreateDevice\x00"
            + b"CreateDXGIFactory\x00"
            + b"NVSDK_NGX_D3D12_CreateFeature\x00" * 4
            + b"NVSDK_NGX_Parameter_GetD3d11Resource\x00"
            + b"NVSDK_NGX_Parameter_SetD3d11Resource\x00"))
        s = peinfo.scan_strings(metro)
        check("D3D12 entry point counted", s.get("d3d12", 0) > 0, str(s))
        check("NGX D3D12 counted", s.get("ngx_d3d12", 0) == 4, str(s))
        check("NGX d3d11 parameter names are NOT counted as D3D11",
              s.get("d3d11", 0) == 0, str(s))

        info = peinfo.detect_api(metro, d)
        check("detected as DX12", info.api == peinfo.DX12,
              f"{info.api} / {info.reason}")
        check("confidence is high", info.confidence == "high", info.confidence)
        check("the reason cites the NGX entry point",
              "NVSDK_NGX_D3D12" in info.reason, info.reason)

        (d / "nvngx_dlss.dll").write_bytes(b"x")
        plan = routes.choose(d, info, 64)
        check("routed to native, not bridge", plan.route == routes.NATIVE,
              plan.route)

    # A truly unidentifiable executable must say so, not default to D3D11.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        blank = make_pe(d / "Mystery.exe", peinfo.PE_X64, extra=b"nothing here")
        info = peinfo.detect_api(blank, d)
        check("unknown stays unknown", info.api == peinfo.UNKNOWN, info.api)
        plan = routes.choose(d, info, 64)
        check("unknown API falls back to feeder, not bridge",
              plan.route == routes.FEEDER, plan.route)
        check("and it says the API is unknown",
              "could not be identified" in plan.reason, plan.reason)
        check("and it tells the user how to resolve it",
              any("ReShade.log" in w for w in plan.warnings), str(plan.warnings))


def test_dlss_runtime_is_always_updated():
    """The game's nvngx_dlss.dll is always refreshed, and always backed up.

    Games ship whatever DLSS build they were released with - Metro Exodus
    Enhanced Edition carries a 13.8 MB runtime from 2021 - and leaving that
    in place runs the neural pass against a years-old library. So the runtime
    is replaced unconditionally, with the original preserved so Remove can
    put the game back exactly as it was.

    Runs on the BRIDGE route deliberately: the feeder route downloads shader
    headers and LumeniteFX, and these tests must stay offline.
    """
    print("\n[the DLSS runtime is always updated, never left stale]")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "setup.exe")

        # Case 1: a game that never calls NGX; the file is somebody's leftover.
        game = base / "game"
        game.mkdir()
        make_pe(game / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        (game / installer.DLSS).write_bytes(b"SOMEBODY ELSES LEFTOVER")
        exe = game / "Game.exe"
        api = peinfo.detect_api(exe, None)
        check("the exe uses no NGX", api.uses_ngx is False)
        check("routed to feeder by default",
              routes.choose(game, api, 64).route == routes.FEEDER)

        plan = routes.choose(game, api, 64)
        rep = installer.install(game, exe, api, 64, plan,
                                installer.Options(route=routes.BRIDGE,
                                                  local_dir=local,
                                                  reshade_setup=setup,
                                                  card=TEST_CARD))
        check("the stray runtime was replaced",
              installer.DLSS not in rep.skipped, str(rep.skipped))
        check("the replacement really happened",
              (game / installer.DLSS).read_bytes() != b"SOMEBODY ELSES LEFTOVER")
        check("the original was backed up",
              (game / (installer.DLSS + installer.BACKUP_SUFFIX)).is_file())
        installer.uninstall(game)
        check("uninstall puts the original back",
              (game / installer.DLSS).read_bytes() == b"SOMEBODY ELSES LEFTOVER")

        # Case 2: a game that DOES call NGX. Its runtime is updated too - that
        # is the whole point - but preserved so it can be restored.
        game2 = base / "game2"
        game2.mkdir()
        make_pe(game2 / "Game.exe", peinfo.PE_X64,
                extra=b"d3d11.dll\0NVSDK_NGX_D3D11_CreateFeature\0")
        (game2 / installer.DLSS).write_bytes(b"THE GAMES OLD 2021 RUNTIME")
        exe2 = game2 / "Game.exe"
        api2 = peinfo.detect_api(exe2, None)
        check("the second exe does use NGX", api2.uses_ngx is True)
        before = snapshot(game2)

        rep2 = installer.install(game2, exe2, api2, 64,
                                 routes.choose(game2, api2, 64),
                                 installer.Options(route=routes.BRIDGE,
                                                   local_dir=local,
                                                   reshade_setup=setup,
                                                   card=TEST_CARD))
        check("a real game's runtime is updated, not skipped",
              installer.DLSS not in rep2.skipped, str(rep2.skipped))
        check("it now holds the new build",
              (game2 / installer.DLSS).read_bytes() != b"THE GAMES OLD 2021 RUNTIME")
        check("the game's original was backed up",
              (game2 / (installer.DLSS + installer.BACKUP_SUFFIX)).read_bytes()
              == b"THE GAMES OLD 2021 RUNTIME")
        check("and the log says it was updated",
              any("updating the game's own" in n or "backed up" in n
                  for n in rep2.notes), str(rep2.notes))

        installer.uninstall(game2)
        check("Remove restores the game's original runtime exactly",
              snapshot(game2) == before,
              f"diff: {set(snapshot(game2)) ^ set(before)}")


def test_bridge_log_is_read_and_ngx_errors_get_context():
    """dlss5-bridge.log is the ground truth on the bridge route.

    Two real reports drove this. Measured on Crysis 3 Remastered 2026-09-01:

      05:40:52  installed
      05:44:37  dlss5-bridge 1.3.0 attached
      05:44:40  game logs NVSDK_NGX_D3D11_CreateFeature ... 0xbad00002
      05:44-53  bridge delivers 12,600 frames at 1.23 ms/frame

    The old diagnoser read only the game's log, saw the 0xbad00002 line, and
    returned BAD for an install that was working perfectly. The error is the
    hand-over: the bridge took the contract, so the game's own direct call is
    expected to be refused.
    """
    print("\n[the bridge log is read, and NGX errors get their context]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ReShade.log").write_text(
            "INFO | Initializing crosire's ReShade version '6.8.0.2155'\n"
            "INFO | Redirecting D3D11CreateDeviceAndSwapChain(...)\n"
            'INFO | Registered add-on "DLSS 5 Neural Rendering" v0.1\n',
            encoding="utf8")
        (d / "dlss5-bridge.log").write_text(
            "05:44:37.188  dlss5-bridge 1.3.0 (built Aug 31 2026) attached.\n"
            "05:53:03.752  [bridge] frame 12600 delivered (864x486)\n"
            "05:53:03.752  [bridge] 600 frames: bridge CPU 1.23 ms/frame | "
            "frame interval 39.55 ms (25.3 fps) | spread 36.48-45.06 ms | "
            "bridge is 3% of the frame | d3d12 25203/25206\n"
            "05:53:18.207  shut down cleanly.\n", encoding="utf8")
        (d / "Game.log").write_text(
            "<05:44:40> Failed to NVSDK_NGX_D3D11_CreateFeature of "
            "NVSDK_NGX_Feature_SuperSampling,  dlaa = 0xbad00002\n",
            encoding="utf8")

        r = diagnose.diagnose(d, route="bridge")
        check("verdict is OK, not BAD", r.verdict == diagnose.OK,
              f"{r.verdict}: {r.summary}")
        check("the frame count is in the summary", "12,600" in r.summary,
              r.summary)
        check("the bridge finding reports the cost",
              any("1.23 ms/frame" in f.evidence for f in r.findings),
              str([f.evidence for f in r.findings]))
        handover = [f for f in r.findings if "hand-over" in f.evidence
                    or "taken the DLSS contract over" in f.text]
        check("the NGX refusal is explained as a hand-over", handover)
        check("and it is not marked bad",
              all(f.level != diagnose.BAD for f in handover), str(handover))

        # Without the bridge log the same NGX line IS a real failure.
        (d / "dlss5-bridge.log").unlink()
        r2 = diagnose.diagnose(d, route="bridge")
        check("with no bridge log the NGX error is a fault",
              r2.verdict == diagnose.BAD, f"{r2.verdict}: {r2.summary}")
        check("and the missing bridge log is reported",
              any("dlss5-bridge.log does not exist" in f.text
                  for f in r2.findings))

    # A missing ReShade.log is not fatal when the bridge proves frames ran.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "dlss5-bridge.log").write_text(
            "05:44:37.188  dlss5-bridge 1.3.0 attached.\n"
            "05:53:03.752  [bridge] frame 9000 delivered (1920x1080)\n",
            encoding="utf8")
        r = diagnose.diagnose(d, route="bridge")
        check("frames outrank a missing ReShade.log",
              r.verdict == diagnose.OK, f"{r.verdict}: {r.summary}")
        check("the summary says it works", "9,000" in r.summary, r.summary)


def test_renderer_in_a_neighbour_dll():
    """Engines that keep the renderer out of the executable.

    Measured on this workstation 2026-09-01: PlagueIncEvolved.exe and
    Bills Must Be Paid.exe are 0.7 MB launchers whose renderer lives in
    UnityPlayer.dll, and Battlefield 6 ships
    Engine.Render.Core2.PlatformPcDx12.retail.dll. Scanning the executable
    alone reported "no graphics API could be identified" for all three.

    The trap this also pins: a general-purpose engine module contains EVERY
    backend, so presence proves nothing. PlagueInc's UnityPlayer.dll holds
    d3d11 x79, d3d12 x103 and vulkan-1 x1; an early "any vulkan means Vulkan"
    rule reported Vulkan for a game that runs D3D11 by default.
    """
    print("\n[the renderer can live in a neighbouring DLL]")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # A module whose file name states the API.
        (d / "Engine.Render.Core2.PlatformPcDx12.retail.dll").write_bytes(b"x")
        api, why = peinfo.api_from_neighbour_dlls(d)
        check("a *Dx12*.dll neighbour means DX12", api == peinfo.DX12,
              f"{api} / {why}")
        check("and it names the module", "dx12" in why.lower(), why)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # The Unity shape: every backend present, Direct3D dominant.
        (d / "UnityPlayer.dll").write_bytes(
            b"d3d11.dll\0" * 8 + b"d3d12.dll\0" * 3 + b"dxgi.dll\0" * 5
            + b"vulkan-1.dll\0")
        api, why = peinfo.api_from_neighbour_dlls(d)
        check("Unity with every backend is read as Direct3D",
              api == peinfo.DX11, f"{api} / {why}")
        check("the reason admits it is a default, not proof",
              "default" in why, why)

        # And the executable-level detection picks it up.
        exe = make_pe(d / "Game.exe", peinfo.PE_X64, extra=b"nothing useful")
        info = peinfo.detect_api(exe, d)
        check("detect_api falls through to the neighbours",
              info.api == peinfo.DX11, f"{info.api} / {info.reason}")
        check("confidence is medium, not high", info.confidence == "medium",
              info.confidence)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # A genuinely Vulkan-only engine module.
        (d / "UnityPlayer.dll").write_bytes(b"vulkan-1.dll\0" * 6)
        api, why = peinfo.api_from_neighbour_dlls(d)
        check("a Vulkan-only module is read as Vulkan", api == peinfo.VULKAN,
              f"{api} / {why}")

    # Launcher and updater executables are not the game.
    for name in ("gaijin_downloader.exe", "GameUpdater.exe", "JiraBugTrap.exe",
                 "Patcher.exe", "CrashReporter.exe"):
        check(f"{name} is not treated as the game",
              not peinfo.looks_like_game(Path(name)))
    for name in ("aces.exe", "MetroExodus.exe", "BatmanAK.exe"):
        check(f"{name} still counts as a game",
              peinfo.looks_like_game(Path(name)))


def test_presets_module():
    """SR/RR preset hints: the exact NGX names, enum values, both sinks.

    Verified against nvngx_dlss.dll 310.8.0 strings and the NVIDIA headers:
    per-quality-slot names ("DLSS.Hint.Render.Preset.Quality" etc., there is
    no single un-slotted parameter), enum J=10 K=11 L=12 M=13, RR D=4 E=5 F=6.
    """
    print("\n[dlss render presets]")
    from dlss5kit import presets

    check("J maps to 10", presets.PRESET_VALUES["J"] == 10)
    check("K maps to 11", presets.PRESET_VALUES["K"] == 11)
    check("M maps to 13", presets.PRESET_VALUES["M"] == 13)
    check("D maps to 4", presets.PRESET_VALUES["D"] == 4)
    check("default maps to 0", presets.PRESET_VALUES["default"] == 0)

    pr = presets.Presets(sr="K", rr="E")
    pairs = dict(presets.hint_pairs(pr))
    check("all six SR slots written",
          sum(1 for k in pairs if k.startswith("DLSS.Hint")) == 6, str(pairs))
    check("all six RR slots written",
          sum(1 for k in pairs if k.startswith("RayReconstruction")) == 6)
    check("SR Quality slot carries K",
          pairs["DLSS.Hint.Render.Preset.Quality"] == 11)
    check("RR Performance slot carries E",
          pairs["RayReconstruction.Hint.Render.Preset.Performance"] == 5)

    try:
        presets.Presets(sr="Z").validate()
        check("bad SR letter refused", False)
    except presets.PresetError:
        check("bad SR letter refused", True)
    try:
        presets.Presets(rr="J").validate()   # J is an SR preset, not RR
        check("SR-only letter refused for RR", False)
    except presets.PresetError:
        check("SR-only letter refused for RR", True)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # bridge sink
        (d / config.BRIDGE_CFG).write_text("vk_mirror=1\n", encoding="utf8")
        presets.apply_to_bridge_cfg(d, pr)
        cfg = config.read_cfg(d / config.BRIDGE_CFG)
        check("bridge cfg carries the hint",
              cfg.get("DLSS.Hint.Render.Preset.Quality") == "11", str(cfg))
        check("existing bridge keys survive", cfg.get("vk_mirror") == "1")

        # native sink
        presets.apply_to_reshade_ini(d, pr)
        ini = config.Ini.load(d / "ReShade.ini")
        check("ReShade.ini carries the hint",
              ini.get("RenoDX.DLSS5", "DLSS.Hint.Render.Preset.Quality") == "11")

        # read-back round trip
        cur = presets.read_current(d)
        check("read_current sees K/E", (cur.sr, cur.rr) == ("K", "E"),
              f"{cur.sr}/{cur.rr}")

        # setting default OVERWRITES, not skips - that is how you get back
        presets.apply(d, "bridge", presets.Presets())
        cfg2 = config.read_cfg(d / config.BRIDGE_CFG)
        check("default resets the override to 0",
              cfg2.get("DLSS.Hint.Render.Preset.Quality") == "0", str(cfg2))


def test_runtime_report():
    """The fp8/fp16 story, pinned to what the binaries actually contain."""
    print("\n[nr runtime report]")
    local = Path(r"C:\Users\uguri\Downloads\Streamline\nvngx_dlssnr.dll")
    if not local.is_file():
        print("  (skipped: local runtime not present)")
        return
    rep = gpu.runtime_report(local, 86)
    check("sm_86 kernels present", 86 in rep["cubin_sms"], str(rep["cubin_sms"]))
    check("reported native for RTX 30", rep["native_for_card"] is True)
    check("precision identified as fp8", "fp8" in rep["precision"],
          rep["precision"])
    check("the note explains the SF fp16 story", "fp16" in rep["note"],
          rep["note"][:80])
    # Upscaling: every published dlssnr build refuses it, and the reason is
    # visible in the binary - zero upscaling code paths, against 95 in
    # nvngx_dlss.dll. Measured after the runtime answered 0xbad00005 on a
    # real 864x486 -> 2560x1440 attempt in Crysis 3, 2026-09-01.
    check("upscaling capability reported", rep["upscaling_capable"] is False,
          str(rep.get("upscaling_capable")))
    check("and explained", "output resolution" in rep.get("upscaling_note", ""),
          rep.get("upscaling_note", "")[:60])

    rep2 = gpu.runtime_report(Path("Z:/nowhere/nvngx_dlssnr.dll"), 86)
    check("missing file reported, not crashed", rep2["note"] == "file not found")


def test_kit_addon_step():
    """The control panel add-on: bundled -> installed + tracked in the
    manifest (so Remove deletes it); not bundled -> honest skip note."""
    print("\n[dlss5kit control panel add-on]")
    from dlss5kit import installer as inst

    # The step is in every route's plan, after the DLSS 5 add-on.
    for route in (inst.NATIVE, inst.BRIDGE, inst.FEEDER):
        steps = inst.plan_steps(route, provider=1)
        check(f"{route}: step planned", "DLSS5Kit control panel" in steps)
        check(f"{route}: ordered after the add-on",
              steps.index("DLSS5Kit control panel")
              == steps.index("DLSS 5 add-on") + 1)

    # bundled_kit_addon: from source it points at addon/dlss5kit.addon64
    # when built; the answer must be a real file or None, never a bogus path.
    found = inst.bundled_kit_addon()
    check("bundled_kit_addon returns a file or None",
          found is None or found.is_file(), str(found))
    if found is not None:
        check("it is the addon64", found.name == inst.KIT_ADDON)


def test_engine_tools_and_rtx_remix():
    """Half-Life 2 RTX, 2026-09-02: two real defects found by running the tool.

    1. bin/ scores +200, and Source keeps its map compilers there, so
       studiomdl_modified.exe (1.8 MB) outscored the real hl2.exe in the
       root and --check reported a map compiler as "the game".
    2. The plain "32-bit, not supported" verdict is misleading for RTX Remix:
       hl2.exe is 32-bit but rendering happens in a separate 64-bit process,
       and the renderer (bin/.trex/d3d9.dll, dxvk-remix) references
       NVSDK_NGX_VULKAN 43 times and D3D12 zero times.
    """
    print("\n[engine tools and rtx remix]")
    game = Path(r"D:\Games\Steam\steamapps\common\Half-Life 2 RTX")
    if not game.is_dir():
        print("  (skipped: Half-Life 2 RTX not installed)")
        return

    exes = peinfo.find_game_exes(game)
    check("hl2.exe wins over the bin/ tools",
          exes and exes[0].name.lower() == "hl2.exe",
          exes[0].name if exes else "none")
    tools = {"studiomdl", "studiomdl_modified", "vbsp", "vrad", "vvis",
             "hammer", "propper"}
    check("no map compiler is offered as a candidate",
          not any(p.stem.lower() in tools for p in exes),
          str([p.name for p in exes if p.stem.lower() in tools][:3]))

    # Crysis (2007): Bin64\ holds BOTH a 32-bit Crysis.exe (9.4 MB) and the
    # real 64-bit Crysis64.exe (53 KB). Path and size both point the wrong
    # way; only the PE header settles it, so bitness has to score.
    crysis = Path(r"D:\Games\Steam\steamapps\common\Crysis")
    # is_dir() is not enough: measured 2026-09-02, the game was uninstalled
    # and left behind an empty folder with only Game.log, which made
    # resolve_target raise and took the whole suite down.
    if crysis.is_dir() and peinfo.find_game_exes(crysis):
        pick, _ = peinfo.resolve_target(crysis)
        check("Crysis picks the 64-bit exe over the bigger 32-bit one",
              peinfo.exe_bitness(pick) == 64, f"{pick.name} is 32-bit")
        check("and it is Crysis64.exe", pick.name.lower() == "crysis64.exe",
              pick.name)
    else:
        print("  (Crysis not installed: bitness-preference case skipped)")

    bridge = peinfo.remix_bridge(game)
    check("the Remix bridge is found", bridge is not None and bridge.is_file())
    if bridge:
        check("and it is 64-bit", peinfo.exe_bitness(bridge) == 64)

    exe, _ = peinfo.resolve_target(game)
    api = peinfo.detect_api(exe, game)
    plan = routes.choose(game, api, peinfo.exe_bitness(exe), card=TEST_CARD)
    check("still refused", plan.supported is False)
    check("but the reason names RTX Remix", "RTX Remix" in plan.blocker,
          plan.blocker[:60])
    check("and does not just say 32-bit",
          "32-bit games are not supported" not in plan.blocker)

    # A folder with no bridge must keep the plain 32-bit message.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        check("plain 32-bit message survives elsewhere",
              peinfo.remix_bridge(d) is None)


def test_32bit_install_layout():
    """A 32-bit game gets the split install: game half + host64\\ half.

    DLSS5-Feeder solves 32-bit by splitting at the shared-NT-handle seam:
    dlss5-feed.addon32 runs inside the 32-bit game and copies frames into
    shared GPU textures; host64\\dlss5-feed-host64.exe is a separate 64-bit
    process with its OWN ReShade, renodx add-on and NGX runtimes, and does
    the DLSS work. Getting the split wrong is silent: 32-bit ReShade simply
    never loads a .addon64, and the helper never finds its runtimes.
    """
    print("\n[32-bit split install layout]")
    from dlss5kit import installer as inst

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # A stand-in for the ReShade setup: a zip with both DLLs, which is
        # exactly what the real installer is (PE + appended zip).
        setup = root / "ReShade_Setup_6.8.0_Addon.exe"
        import zipfile
        with zipfile.ZipFile(setup, "w") as z:
            z.writestr("ReShade32.dll", b"MZ" + b"\x00" * 600)   # x86 marker
            z.writestr("ReShade64.dll", b"MZ" + b"\x00" * 900)   # x64 marker
        got32 = inst.extract_member(setup, "ReShade32.dll")
        got64 = inst.extract_member(setup, "ReShade64.dll")
        check("the setup carries both bitnesses",
              len(got32) == 602 and len(got64) == 902,
              f"{len(got32)}/{len(got64)}")

        # The layout contract, asserted as data rather than by running a
        # network install: what goes beside the game, what goes in host64\.
        game_side = {inst.FEEDER_ADDON32, inst.PROXY_DLL, "ReShade.ini"}
        host_side = {inst.FEEDER_HOST64, inst.PROXY_DLL, inst.RENODX,
                     inst.DLSSNR, inst.DLSS, "ReShade.ini"}
        check("the game half loads a 32-bit add-on",
              inst.FEEDER_ADDON32.endswith(".addon32"))
        check("the 64-bit add-on is NOT in the game half",
              inst.FEEDER_ADDON not in game_side)
        check("the helper gets its own proxy DLL", inst.PROXY_DLL in host_side)
        check("and the NGX runtimes", {inst.DLSSNR, inst.DLSS} <= host_side)
        check("host64 dir name matches upstream", inst.HOST64_DIR == "host64")

        # status() must see an install whose 64-bit parts are in host64\.
        (root / inst.HOST64_DIR).mkdir()
        for name in (inst.RENODX, inst.DLSSNR, inst.DLSS):
            (root / inst.HOST64_DIR / name).write_bytes(b"x")
        st = inst.status(root)
        check("status finds the add-on in host64", st["present"]["DLSS 5 add-on"])
        check("status finds dlssnr in host64", st["present"]["nvngx_dlssnr"])
        check("status finds dlss in host64", st["present"]["nvngx_dlss"])


def test_maglumkaynak_older_games():
    """Five defects found by running v1.8.0 against C:\\Games\\MaglumKaynak.

    Far Cry (2004), Far Cry 3 (2012) and Watch Dogs 2 (2016), 2026-09-02.
    Each case is reproduced from the real layout that exposed it, so the
    fixes cannot silently regress.
    """
    print("\n[older games: exe ranking, neighbour DLLs, anti-cheat]")

    # 1. Far Cry (2004). Bin32/ holds the game, a configurator, and the whole
    #    CryEngine toolchain. Measured before the fix:
    #      FarCryConfigurator.exe 335.5   <- picked, and DX9 was read off it
    #      rc.exe                 335.1   <- "rc" is a substring of "farcry"
    #      FarCry.exe             335.0   <- the actual game, third
    with tempfile.TemporaryDirectory() as td:
        game = Path(td) / "Far Cry"
        b32 = game / "Bin32"
        for name, size in (("FarCry.exe", 30_000),
                           ("FarCryConfigurator.exe", 420_000),
                           ("rc.exe", 110_000),
                           ("cgc.exe", 800_000),
                           ("Editor.exe", 4_220_000),
                           ("FarCry_WinSV.exe", 30_000)):
            make_pe(b32 / name, peinfo.PE_X86, b"\0" * size)
        exes = peinfo.find_game_exes(game)
        top = exes[0].name if exes else "none"
        check("Far Cry picks the game, not the configurator",
              top == "FarCry.exe", top)
        offered = {p.stem.lower() for p in exes}
        check("the configurator is not even a candidate",
              "farcryconfigurator" not in offered, str(sorted(offered)))
        check("CryEngine tools (rc, cgc, editor) are not candidates",
              not ({"rc", "cgc", "editor"} & offered), str(sorted(offered)))
        check("the dedicated server does not win",
              top != "FarCry_WinSV.exe", top)
        # Score the real game ABOVE every impostor, so the ordering does not
        # depend on which one the filesystem happened to yield first. Without
        # this the assertions above can pass on tie-break luck alone.
        gs = peinfo._score(b32 / "FarCry.exe", game)
        for impostor in ("FarCryConfigurator.exe", "rc.exe", "cgc.exe",
                         "Editor.exe"):
            check(f"FarCry.exe outscores {impostor}",
                  gs > peinfo._score(b32 / impostor, game),
                  f"{gs:.1f} vs {peinfo._score(b32 / impostor, game):.1f}")

    # 2. A short stem must not collect the folder-name bonus by accident.
    #    Measure the bonus in ISOLATION: "rc" is also an engine-tool name now,
    #    and that -1200 penalty would mask whether the bonus still fires. Use
    #    a neutral two-letter stem that is a substring of the folder name but
    #    carries no other rule, so only the bonus can move the number.
    with tempfile.TemporaryDirectory() as td:
        g = Path(td) / "Far Cry"
        g.mkdir(parents=True)
        for n in ("ar.exe", "FarCry.exe", "Zqx.exe"):
            make_pe(g / n, peinfo.PE_X86, b"\0" * 50_000)
        s_short = peinfo._score(g / "ar.exe", g)      # "ar" is inside "farcry"
        s_game = peinfo._score(g / "FarCry.exe", g)
        s_none = peinfo._score(g / "Zqx.exe", g)      # unrelated name
        check("a 2-letter stem gets no folder-name bonus",
              abs(s_short - s_none) < 1,
              f"short={s_short:.1f} unrelated={s_none:.1f}")
        check("a real name match still gets the full bonus",
              s_game - s_none > 300, f"game={s_game:.1f} unrelated={s_none:.1f}")
        # And the tool name is penalised on top, independently of the bonus.
        check("an engine tool name is penalised",
              peinfo._score(g / "FarCry.exe", g)
              > peinfo._score(Path(str(g / "rc.exe")), g))
        make_pe(g / "rc.exe", peinfo.PE_X86, b"\0" * 50_000)
        check("and rc.exe scores below an unrelated executable",
              peinfo._score(g / "rc.exe", g) < s_none,
              f"rc={peinfo._score(g / 'rc.exe', g):.1f} unrelated={s_none:.1f}")

    # 3. Far Cry 3 ships two identical 0.2 MB shells in bin/, one per
    #    renderer, scored the same (535.2), so directory order decided it and
    #    the D3D9 shell won. The tie must be broken on what each binary
    #    actually pulls in.
    real_imports = peinfo.pe_imports
    try:
        table = {"farcry3.exe": ["fc3.dll", "msvcr100.dll", "kernel32.dll"],
                 "farcry3_d3d11.exe": ["fc3_d3d11.dll", "msvcr100.dll",
                                       "kernel32.dll"]}
        peinfo.pe_imports = lambda p: table.get(Path(p).name.lower(), [])
        with tempfile.TemporaryDirectory() as td:
            game = Path(td) / "Far Cry 3"
            for name in ("farcry3.exe", "farcry3_d3d11.exe"):
                make_pe(game / "bin" / name, peinfo.PE_X86, b"\0" * 200_720)
            exes = peinfo.find_game_exes(game)
            top = exes[0].name if exes else "none"
            check("Far Cry 3 prefers the D3D11 shell over the D3D9 one",
                  top == "farcry3_d3d11.exe", top)

        # 4. ...but a publisher's own module named after the API is NOT the
        #    Microsoft runtime. `"d3d11.dll" in "fc3_d3d11.dll"` is True, and
        #    a substring test reported "imports d3d11.dll statically" at HIGH
        #    confidence about a DLL Microsoft never shipped.
        with tempfile.TemporaryDirectory() as td:
            exe = make_pe(Path(td) / "farcry3_d3d11.exe", peinfo.PE_X86)
            peinfo.pe_imports = lambda p: ["fc3_d3d11.dll", "kernel32.dll"]
            info = peinfo.detect_api(exe)
            check("a publisher module is not credited as a static d3d11 import",
                  "statically" not in info.reason, info.reason)
            check("and such a claim is not made at high confidence",
                  info.confidence != "high", info.confidence)
            peinfo.pe_imports = lambda p: ["d3d11.dll", "kernel32.dll"]
            info = peinfo.detect_api(exe)
            check("a real d3d11.dll import is still high confidence",
                  info.api == peinfo.DX11 and info.confidence == "high",
                  f"{info.api} {info.confidence}")
    finally:
        peinfo.pe_imports = real_imports

    # 5. Watch Dogs 2: bin/ holds NVIDIA GameWorks helpers whose file names
    #    carry an API, beside the 138.9 MB engine, whose own name carries
    #    none. Returning on the first name match credited a shadow-mapping
    #    helper as the renderer.
    #
    #    The fixture must mirror that shape exactly: engine name WITHOUT an
    #    API in it, helpers WITH one. An earlier version of this test used a
    #    DX12 engine named Engine_DX12.dll, which the DX12 branch answered
    #    before the DX11 helpers were ever reached - so deleting the helper
    #    filter changed nothing and the test passed on a mutant. Measured, and
    #    it proved the test wrong rather than the code right.
    with tempfile.TemporaryDirectory() as td:
        b = Path(td) / "bin"
        b.mkdir(parents=True)
        (b / "GFSDK_ShadowLib_DX11.win64.dll").write_bytes(b"\0" * 2_500_000)
        (b / "GFSDK_SSAO_D3D11.win64.dll").write_bytes(b"\0" * 1_200_000)
        engine = b / "Disrupt_64.dll"
        engine.write_bytes(b"\0" * 40_000_000)
        real_imports = peinfo.pe_imports
        try:
            peinfo.pe_imports = lambda p: (
                ["d3d11.dll", "d3d9.dll", "dxgi.dll"]
                if Path(p).name.lower() == "disrupt_64.dll" else [])
            api, why = peinfo.api_from_neighbour_dlls(b)
        finally:
            peinfo.pe_imports = real_imports
        check("a GameWorks helper does not decide the renderer",
              "gfsdk" not in why.lower(), why)
        check("the engine module's own imports decide instead",
              api == peinfo.DX11 and "disrupt_64.dll" in why.lower(),
              f"{api}: {why}")

    # A module whose name really does announce the API still wins, and the
    # largest such module is preferred over a smaller one.
    with tempfile.TemporaryDirectory() as td:
        b = Path(td) / "bin"
        b.mkdir(parents=True)
        (b / "GFSDK_ShadowLib_DX11.win64.dll").write_bytes(b"\0" * 2_500_000)
        (b / "Engine_DX12.dll").write_bytes(b"\0" * 40_000_000)
        api, why = peinfo.api_from_neighbour_dlls(b)
        check("a named engine module still decides",
              api == peinfo.DX12 and "engine_dx12" in why.lower(), why)

    # 6. Anti-cheat lives at the INSTALL ROOT while the exe sits in bin/, so
    #    install() (which is handed the exe's folder) never saw it, and
    #    --check reported warnings: [] for a game shipping Easy Anti-Cheat.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Watch Dogs 2"
        (root / "EasyAntiCheat").mkdir(parents=True)
        (root / "bin").mkdir(parents=True)
        check("install_root climbs out of bin/",
              installer.install_root(root / "bin") == root,
              str(installer.install_root(root / "bin")))
        check("install_root leaves a real root alone",
              installer.install_root(root) == root)
        name, _ = installer.detect_anticheat(installer.install_root(root / "bin"))
        check("anti-cheat is found from the exe's folder",
              name == "Easy Anti-Cheat", name or "nothing")
        warn = routes.anticheat_warning(root / "bin")
        check("and it reaches the inspection as a warning",
              "Easy Anti-Cheat" in warn and "ban" in warn, warn[:60])
        plan = routes.choose(root / "bin", peinfo.ApiInfo(), 64)
        check("choose() carries the anti-cheat warning",
              any("Anti-Cheat" in w for w in plan.warnings),
              str(plan.warnings))

    # The INSTALL path must warn too, from the exe's own folder. This is the
    # case that reverting install_root() has to break: install() is handed
    # bin/, and the marker is one level up.
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "Watch Dogs 2"
        binp = root / "bin"
        binp.mkdir(parents=True)
        (root / "EasyAntiCheat").mkdir(parents=True)
        make_pe(binp / "Game.exe", peinfo.PE_X64, extra=b"d3d11.dll\0")
        local = build_local(base / "local")
        setup = make_reshade_setup(base / "ReShade_Setup_6.8.0_Addon.exe")
        api = peinfo.ApiInfo(api=peinfo.DX11, confidence="high")
        plan = routes.choose(binp, api, 64)
        opt = installer.Options(route=routes.FEEDER, local_dir=local,
                                reshade_setup=setup, card=TEST_CARD)
        rep = installer.install(binp, binp / "Game.exe", api, 64, plan, opt)
        check("install() warns about anti-cheat found one level up",
              any("Anti-Cheat" in w for w in rep.warnings),
              str(rep.warnings)[:80])
        check("and the install still completes (a caution, not a refusal)",
              rep.complete is True)

    # A game with no anti-cheat must not gain a warning.
    with tempfile.TemporaryDirectory() as td:
        clean = Path(td) / "Clean Game"
        (clean / "bin").mkdir(parents=True)
        check("a clean game gets no anti-cheat warning",
              routes.anticheat_warning(clean / "bin") == "")


def test_dx9_opengl_and_the_renodx_pin():
    """Capability refusals that were wrong, and a version that was unpinned.

    Read out of upstream (D:\\Hermes\\DLSS5-Feeder) on 2026-09-02 after Ugur
    pointed at the repo: "onlar da dx9 da kullanmislar bu feeder i".
    """
    print("\n[dx9 via dgVoodoo2, opengl, and the renodx 4.55 pin]")

    # 1. D3D9 is a supported route, not a refusal. Upstream verifies it on
    #    Fable Anniversary (32-bit D3D9 via dgVoodoo2, 1440p 60 fps).
    for bits in (32, 64):
        p = routes.choose(None, peinfo.ApiInfo(api=peinfo.DX9,
                                               confidence="high"), bits)
        check(f"{bits}-bit D3D9 is supported",
              p.supported and p.route == routes.FEEDER,
              f"{p.supported}/{p.route}")
        check(f"{bits}-bit D3D9 names dgVoodoo2",
              "dgVoodoo2" in p.reason, p.reason[:60])
        check(f"{bits}-bit D3D9 warns about the watermark check",
              any("watermark" in w for w in p.warnings), str(p.warnings)[:70])
        check(f"{bits}-bit D3D9 says ReShade goes in as dxgi.dll",
              "dxgi.dll" in p.reason, p.reason[:60])
    # A 64-bit D3D9 game should also hear that renodx-dlss covers it natively.
    p64 = routes.choose(None, peinfo.ApiInfo(api=peinfo.DX9), 64)
    check("64-bit D3D9 mentions the native renodx-dlss alternative",
          any("renodx-dlss" in w for w in p64.warnings), str(p64.warnings)[:80])

    # 2. The install plan gains a dgVoodoo2 step, first, and only for D3D9.
    steps = installer.plan_steps(routes.FEEDER, 3, peinfo.DX9)
    check("dgVoodoo2 is the first install step on D3D9",
          steps and "dgVoodoo2" in steps[0], str(steps[:2]))
    plain = installer.plan_steps(routes.FEEDER, 3, peinfo.DX11)
    check("and it is absent on D3D11",
          not any("dgVoodoo2" in s for s in plain), str(plain[:2]))

    # 3. dgVoodoo.conf: the four settings upstream calls out, applied to the
    #    real shipped file without destroying the rest of it.
    conf_text = "\n".join([
        "[General]", "OutputAPI = bestavailable", "Adapters = all",
        "", "[Glide]", "VideoCard = voodoo_2",
        "", "[DirectX]", "DisableAndPassThru = true", "VideoCard = internal3D",
        "VRAM = 256", "dgVoodooWatermark = false", "Antialiasing = appdriven",
    ])
    with tempfile.TemporaryDirectory() as td:
        conf = Path(td) / "dgVoodoo.conf"
        conf.write_text(conf_text, encoding="utf8")
        installer.write_dgvoodoo_conf(conf, watermark=True)
        got = conf.read_text(encoding="utf8")
        for want in ("DisableAndPassThru = false", "VRAM = 1GB",
                     "OutputAPI = d3d11_fl11_0", "dgVoodooWatermark = true",
                     "VideoCard = internal3D"):
            check(f"conf sets {want}", want in got, got[:120])
        check("the Glide section is left alone",
              "VideoCard                           = voodoo_2" in got
              or "VideoCard = voodoo_2" in got, "Glide VideoCard was rewritten")
        check("unrelated keys survive", "Antialiasing = appdriven" in got)
        installer.write_dgvoodoo_conf(conf, watermark=False)
        check("the watermark can be turned back off",
              "dgVoodooWatermark = false" in conf.read_text(encoding="utf8"))

    # A conf that does not carry the expected keys must fail loudly rather
    # than silently produce a passthrough install that does nothing.
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "dgVoodoo.conf"
        bad.write_text("[DirectX]\nSomethingElse = 1\n", encoding="utf8")
        try:
            installer.write_dgvoodoo_conf(bad)
            check("an unrecognised dgVoodoo.conf raises", False, "no raise")
        except installer.InstallError:
            check("an unrecognised dgVoodoo.conf raises", True)

    # 4. Archives ship several copies of a name; the wrong one is unusable.
    #    dgVoodoo2 2.87.3 carries Cpl/arm64/dgVoodooCpl.exe before the real
    #    x64 build in the root, so a suffix match picks an ARM64 binary.
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "a.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("Cpl/arm64/dgVoodooCpl.exe", b"ARM64")
            z.writestr("dgVoodooCpl.exe", b"X64")
            z.writestr("MS/x86/D3D9.dll", b"32BIT")
            z.writestr("MS/x64/D3D9.dll", b"64BIT")
        check("suffix match picks the first, which is the wrong one",
              installer.extract_member(zp, "dgVoodooCpl.exe") == b"ARM64")
        check("exact match picks the root binary",
              installer.extract_member(zp, "dgVoodooCpl.exe", exact=True) == b"X64")
        check("exact match honours the architecture folder",
              installer.extract_member(zp, "MS/x86/D3D9.dll", exact=True) == b"32BIT")

    # 5. The feeder route must not install renodx newer than 4.55: upstream
    #    states builds past it start building the synthetic contract
    #    themselves and conflict with the feeder doing the same.
    entries = [{"label": "4.70", "tag": "renodx-dlss5-4.70", "key": (4, 70)},
               {"label": "4.60", "tag": "renodx-dlss5-4.60", "key": (4, 60)},
               {"label": "4.55", "tag": "renodx-dlss5-4.55", "key": (4, 55)},
               {"label": "4.5", "tag": "renodx-dlss5-4.5", "key": (4, 5)}]
    check("the ceiling picks 4.55, not the newest",
          sources.pick_capped(entries, installer.FEEDER_RENODX_MAX) == "4.55",
          str(sources.pick_capped(entries, installer.FEEDER_RENODX_MAX)))
    check("the pin constant is 4.55", installer.FEEDER_RENODX_MAX == "4.55")
    check("a ceiling below everything returns None",
          sources.pick_capped(entries, "1.0") is None)
    check("an empty catalogue returns None",
          sources.pick_capped([], "4.55") is None)
    check("version order is numeric, not lexical",
          sources.pick_capped(entries, "4.60") == "4.60")
    # And the unconstrained pick still takes the newest, for native/bridge.
    check("without a ceiling the newest is still chosen",
          sources.pick(entries, None)["label"] == "4.70")


    # 6. A tool must not mistake its own output for its input. A previous
    #    32-bit install leaves host64\dlss5-feed-host64.exe beside the game;
    #    measured on Far Cry 2026-09-02 the ranker picked it as "the game"
    #    (64-bit +500, bin/ +200) over the real 32-bit FarCry.exe, and the
    #    verdict flipped from DX9/feeder to DX12/native.
    with tempfile.TemporaryDirectory() as td:
        game = Path(td) / "Far Cry"
        b32 = game / "Bin32"
        make_pe(b32 / "FarCry.exe", peinfo.PE_X86, b"\0" * 30_000)
        make_pe(b32 / "host64" / "dlss5-feed-host64.exe", peinfo.PE_X64,
                b"\0" * 66_048)
        make_pe(game / "_DLSS5_Backup" / "Bin32" / "FarCry.exe",
                peinfo.PE_X86, b"\0" * 30_000)
        exes = peinfo.find_game_exes(game)
        names = [p.name for p in exes]
        check("our own helper is not picked as the game",
              names and names[0] == "FarCry.exe", str(names[:3]))
        check("the helper is not even a candidate",
              not any("host64" in str(p) for p in exes), str(names[:3]))
        check("a leftover backup folder is not scanned",
              not any("_DLSS5_Backup" in str(p) for p in exes),
              str([str(p) for p in exes][:3]))


def main() -> int:
    print("DLSS5Kit offline tests")
    test_ini()
    test_preset_order()
    test_ini_preserves_user()
    test_pe()
    test_api_detection_from_strings()
    test_api_from_log_wins()
    test_uppercase_module_names_and_ngx_evidence()
    test_renderer_in_a_neighbour_dll()
    test_presets_module()
    test_runtime_report()
    test_kit_addon_step()
    test_engine_tools_and_rtx_remix()
    test_maglumkaynak_older_games()
    test_dx9_opengl_and_the_renodx_pin()
    test_32bit_install_layout()
    test_bridge_private_device_does_not_flip_the_verdict()
    test_generations_and_ptx()
    test_native_dlss_veto()
    test_generation_blocks_install()
    test_dlss_runtime_is_always_updated()
    test_routing()
    test_zip_extraction()
    test_install_uninstall_round_trip()
    test_double_install_then_remove()
    test_identical_file_skips_backup()
    test_route_switch_cleans_up()
    test_refuses_foreign_injector()
    test_anticheat_warning()
    test_manifest_survives_failure()
    test_diagnose()
    test_bridge_log_is_read_and_ngx_errors_get_context()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
