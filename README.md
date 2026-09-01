# DLSS5Kit

One-click DLSS 5 neural rendering setup for PC games. Windows, NVIDIA RTX 20
series or newer.

It looks at the game, decides how DLSS 5 can actually reach it, writes the
files and the configuration, and records everything it wrote so Remove puts
the folder back exactly as it was.

**[Download DLSS5Kit.exe](../../releases/latest)** - single file, no
installation, no Python needed.

```
DLSS5Kit.exe                                 open the window
DLSS5Kit.exe "D:\Games\Game"                 install
DLSS5Kit.exe "D:\Games\Game" --check         inspect only, write nothing
DLSS5Kit.exe "D:\Games\Game" --diagnose      read the logs back
DLSS5Kit.exe "D:\Games\Game" --remove        uninstall
DLSS5Kit.exe --gpu                           card + which builds support it
```

Running from source instead: `python -m dlss5kit.cli` takes the same
arguments.

Supported: Windows, NVIDIA RTX 20, 30, 40 and 50 series, 64-bit DX11/DX12 and
Vulkan games.

**Using an AI assistant instead of the window?** Point it at
**[docs/AI_AGENT_GUIDE.md](docs/AI_AGENT_GUIDE.md)**: the commands, the
`--check --json` schema, the exit codes, every failure and its fix, and how to
read `--diagnose` when nothing seems to have happened. You never have to open
the GUI. (`AGENTS.md` and `CLAUDE.md` at the root point at the same guide, for
assistants that look for those names.)

> **This repository contains no game files, no NVIDIA binaries and no
> third-party redistributables.** It is installer logic only. Everything it
> needs is downloaded at run time from the original publishers. See
> [Where things come from](#where-things-come-from).

## Why this exists

The install itself is not hard. Choosing the right route is, and choosing
wrong fails silently: the game starts, nothing crashes, and it simply looks
unchanged.

There are three routes and the deciding question is not "does this game have
DLSS" but "which NGX entry point does it actually call".

| Route | What it does | When |
|---|---|---|
| **native** | The DLSS 5 add-on detours the game's own NGX D3D12 calls | D3D12 renderer AND the game calls `NVSDK_NGX_D3D12_*` |
| **bridge** | `dlss5-bridge` reproduces the DLSS contract on a private D3D12 session | D3D11 and Vulkan games, with or without their own DLSS |
| **feeder** | DLSS5-Feeder builds a synthetic DLAA contract from ReShade's depth buffer and shader motion vectors | Everything else. Always DLAA, never upscaling |

### The case that motivated the design

Crysis 3 Remastered, measured 2026-09-01:

```
Folder contains : sl.interposer.dll, sl.dlss.dll, nvngx_dlss.dll  ->  "it has DLSS"
Executable      : NVSDK_NGX_D3D11 x27, NVSDK_NGX_D3D12 x1
Static imports  : opengl32.dll, d3d10.dll, vulkan-1.dll   (no d3d11, no d3d12)
ReShade.log     : 12 x D3D11CreateDevice, 0 x D3D12CreateDevice
Game.log        : Failed to NVSDK_NGX_D3D11_CreateFeature ... = 0xbad00005
```

Every naive detector says "has DLSS, D3D12 add-on, native route". The add-on
then hooks `NVSDK_NGX_D3D12_CreateFeature` and waits for a call the game never
makes. An import-table-only API detector is worse still: it reads `d3d10.dll`
and calls this a DX10 game, because the real renderer is loaded with
`LoadLibrary` and never appears in the import table.

DLSS5Kit reads three tiers of evidence, strongest first:

1. `ReShade.log` in the game folder, which records the device calls the game
   really made
2. the static import table
3. DLL name strings inside the executable, which are the `LoadLibrary` targets

and routes on the NGX entry point, not on the presence of DLSS files. On this
game it picks **bridge**, with the reason spelled out.

## What it verifies rather than assumes

**Your graphics card, and every build against it.** The CUDA code inside
`nvngx_dlssnr.dll` is compiled per architecture, and a build with no code for
your card produces exactly the silent nothing described above. The card is
detected from the registry (desktop, laptop, Titan and RTX A-series names all
handled) and can be overridden with the generation picker or `--generation`.
Every candidate build is then read and matched against it. Measured
2026-09-01 by parsing the files themselves:

| nvngx_dlssnr build | RTX 20 | RTX 30 | RTX 40 | RTX 50 |
|---|---|---|---|---|
| `310.8.SF-v2` | yes | yes | yes | yes |
| `310.8.SF` | yes | yes | yes | yes |
| `310.8.0-RTX40` | - | - | yes | yes |
| `310.8.0` | - | - | - | yes |

On Auto the list is walked newest-first and the first build that supports your
card wins. If the mirror publishes an RTX-50-only build at the top, an RTX 30
machine skips it and takes the newest SF build instead, saying so in the log.
`--gpu` prints this table for your own machine.

**CUBIN and PTX, not just CUBIN.** A fatbin holds machine code (CUBIN) for
exact architectures and portable assembly (PTX) the driver JIT-compiles for
anything newer. `nvngx_dlss.dll` 310.8.0 carries no sm_120 CUBIN at all, yet
runs on RTX 50 through its sm_89 PTX. A CUBIN-only check calls that file
incompatible with an RTX 50 card, which is wrong, so both record kinds are
read and the difference is reported ("native code present" vs "the driver will
JIT-compile it").

**Whether the game really has DLSS.** Loose `sl.*.dll` files are evidence
somebody copied files, not evidence the game calls them. Batman: Arkham Knight
(2015) had the whole Streamline set sitting in it from a manual attempt; its
executable contains zero occurrences of `NVSDK_NGX`, `nvngx`, `DLSS` or
`streamline`. When the executable references no NGX entry point at all, the
executable wins and the files are reported as leftovers. The same veto stops a
stray `nvngx_dlss.dll` being mistaken for the game's own and left in place.

**Technique ordering.** On the feeder route the motion-vector provider's
technique must sit ABOVE `DLSS5_Feed` or the feed never receives vectors. This
is written correctly and your other shaders are left where they are.

**Your own files.** Anything overwritten is copied to `<name>.dlss5kit-backup`
first and restored on Remove. A file byte-identical to what is about to be
written is not backed up, because copying a 165 MB file onto itself is waste.

**Foreign injectors.** If `dxgi.dll` already exists and is not ReShade (DXVK,
Special K, an ENB), the install refuses rather than destroying it.

## Graphics card selection

The card is detected automatically. The picker exists for the cases detection
cannot cover: a machine with two GPUs, a card whose driver reports an unusual
name, or preparing an install for a different machine.

```
dlss5kit --gpu                          the card, and which builds support it
dlss5kit "D:\Games\Game" --generation 40    force the RTX 40 build choice
```

Forcing a generation your machine does not have is allowed, and the GUI says
so before installing. Anything below RTX 20 is refused with the reason: DLSS 5
neural rendering needs Tensor cores.

## Uninstall really uninstalls

`dlss5kit-manifest.json` records every path written and every backup taken.
Remove restores backups first, then deletes only what is listed, then prunes
directories it created while they are empty. Two failure modes seen in other
tools are covered by tests:

- installing twice must still uninstall cleanly (the second install must not
  back up the first install's files, or Remove restores them and the folder
  comes out still set up)
- switching routes removes the previous route first (ReShade loads every
  `.addon64` in the folder, so a leftover from another route hooks the same
  calls)

The manifest is written even when an install fails partway, so a partial
install is still removable rather than orphaned.

## "Did it work?"

`--diagnose` reads `ReShade.log`, `dlss5-feed.log` and the game's own log back
and answers in words. Real output from the case above:

```
Verdict: BAD
[ok]   ReShade 6.8.0.2155 loaded.
[ok]   The game creates a D3D11 device (12 call(s)).
[ok]   The DLSS 5 add-on registered v0.2026.828.2110.
[ok]   The add-on hooked NVSDK_NGX_D3D12_CreateFeature.
[BAD]  MISMATCH: the add-on hooked the D3D12 NGX entry points, but this game
       renders with D3D11 and calls the D3D11 ones.
[BAD]  The game itself reports: NVSDK_NGX_D3D11_CreateFeature failed with 0xbad00005.
```

## Where things come from

Nothing is bundled. Each component is fetched from its own publisher at run
time, and a local copy is preferred whenever one exists (`--local <folder>`).

| Component | Source | Licence |
|---|---|---|
| ReShade | reshade.me | BSD-3-Clause |
| Shader headers | crosire/reshade-shaders | per file |
| DLSS5-Feeder | jlrouzies-fr/DLSS5-Feeder | see repository |
| LumeniteFX | umar-afzaal/LumeniteFX | AGNYA |
| dlss5-bridge | NIGos/dlss5-bridge | see repository |
| RenoDX DLSS 5 add-on, NVIDIA NGX runtimes | RankFTW/rhi-repo mirror | **proprietary, no public licence** |

The DLSS 5 add-on and the NVIDIA NGX runtimes are closed-source with no
published licence. They are not in this repository and not redistributed by
it; they are downloaded from a public community mirror, exactly as a person
would by hand. If you are not comfortable with that, pass `--local` with your
own copies, or do not use this tool.

Not affiliated with NVIDIA, ReShade or RenoDX. Use at your own risk.

## Antivirus false positives

A few engines on VirusTotal flag the released `DLSS5Kit.exe`. Measured on
VirusTotal, same source, only the build metadata changed:

| Build | Detections | Microsoft |
|---|---|---|
| v1.6.0, no version resource | **6/70** | `Trojan:Win32/Wacatac.B!ml` |
| v1.6.1, version resource added | **3-4/70** | varies between runs |

Adding proper file metadata removed Skyhigh's `BehavesLike.Win64.Injector`,
Elastic and SecureAge outright. What remains is Bkav, Zillya and APEX, which
bucket PyInstaller output generically, plus Microsoft's `Wacatac.B!ml`, which
is unstable: two v1.6.1 binaries built from the same commit scored 3/70 with
Microsoft clean and 4/70 with Microsoft flagging. The `!ml` suffix is
Microsoft's own marker for "machine-learning guess", and it behaves like one.
A locally installed, fully updated Defender reports **no threats** on the same
files (`MpCmdRun.exe -Scan -ScanType 3 -File DLSS5Kit.exe`).

Here is what causes the detections, and how to check the claim yourself
rather than taking anyone's word for it.

**Why it happens**

1. **PyInstaller.** The exe is a Python interpreter plus a compressed archive
   that unpacks itself to a temp folder at startup. Self-extracting behaviour
   is what packers do, so heuristic and ML engines score it. This is a
   long-standing, well-documented PyInstaller problem, not something specific
   to this tool.
2. **It is an injector, and says so.** The tool's entire job is to place a
   proxy DLL (`dxgi.dll`) and ReShade add-ons next to a game executable so the
   game loads them. `BehavesLike.Win64.Injector` is a fair description of the
   advertised purpose. The difference from malware is that it is the whole
   point of the program, it happens only in a folder you choose, and every
   file written is listed in `dlss5kit-manifest.json` and removed by `Remove`.
3. **No code-signing certificate.** Unsigned executables start from a worse
   reputation score. A certificate costs money and would not change a single
   line of behaviour.

**What was fixed.** v1.6.0 shipped with an entirely empty version resource:
no CompanyName, no FileDescription, no FileVersion. To an ML classifier that
is an anonymous packed binary, which is most of what it was trained to catch.
v1.6.1 embeds proper metadata (publisher, description, version, licence, the
repository URL), generated from the package version at build time. That alone
removed half the detections, including the injector-behaviour flag. It is not
a trick to evade scanners: it is the file telling the truth about itself,
which it previously did not. The honest limit: it does not reliably silence
Microsoft's ML classifier, because nothing short of code signing does.

**What it is not:** there is no process injection into running programs, no
persistence, no obfuscated payload and no telemetry. Grepping the source for
`CreateRemoteThread`, `WriteProcessMemory`, `VirtualAllocEx`, `OpenProcess`,
`SetWindowsHookEx`, run-key/scheduled-task persistence, and `exec`/`eval`
returns zero hits. `subprocess` is used only to open Explorer. The only hosts
contacted are `api.github.com`, `codeload.github.com`,
`raw.githubusercontent.com` and `reshade.me`, all for downloading the
components listed in Credits.

**Check it yourself**

- Every release binary is built by GitHub Actions from this repository, in
  public: the workflow run that produced it is linked on the release, and the
  artifact is uploaded by CI, not from anyone's machine.
- Run the tool from source instead of the exe (`python dlss5kit.py`) - same
  program, no PyInstaller wrapper, no detections.
- Scan the exe with an up-to-date local Defender: measured on v1.6.0,
  `MpCmdRun.exe -Scan -ScanType 3 -File DLSS5Kit.exe` returns
  "found no threats", while VirusTotal's Microsoft engine reports the `!ml`
  (machine-learning guess) verdict. A `!ml` suffix means exactly that: a
  prediction, not a signature match.
- Check the file's properties (right-click, Details): from v1.6.1 the exe
  names its publisher, description, version and licence. Compare hashes with
  the releases page; if a file claiming to be DLSS5Kit does not match one,
  it did not come from here.

If you are not comfortable with any of this, use the source checkout. That is
a reasonable choice and the tool works identically.

## Warnings

- **Do not use this in online games.** ReShade with add-ons is detected by
  anti-cheat. BattlEye, EAC, Vanguard and GameGuard are detected in the game
  folder and reported before the install, but the install is not blocked: it
  is your machine.
- **Set your resolution before turning neural rendering on.** The DLSS feature
  is created for one backbuffer size; changing resolution or display mode
  while it runs forces a rebuild that can black-screen or freeze the game.
- Prefer borderless over exclusive fullscreen.
- Neural rendering costs several milliseconds. On the feeder route the
  performance dial is `work_resolution` (50-100%), not a DLSS quality mode.
- 32-bit games, DirectX 9 and DirectX 10 are refused with a reason rather than
  half-attempted.

## After installing

Press **Home** for the ReShade overlay.

- native / bridge: open the **DLSS 5 Neural Rendering** panel and turn neural
  rendering ON. It is off by default. **F6** toggles it.
- feeder: tick **LUMENITE: Kernel 2.0** and **DLSS 5 Feed**, with the kernel
  ABOVE the feed, then enable neural rendering. Turn the game's own MSAA/SSAA
  off.

## Building from source

```
python build.py
```

Needs Python 3.10+ on Windows; PyInstaller is installed automatically if it is
missing. Produces `dist/DLSS5Kit.exe`. Every push also builds and tests it on
GitHub Actions, and tagged pushes attach the executable to the release.

## Tests

```
python tests/test_all.py
```

142 offline checks: no network, no game, no GPU. They cover the ini and preset
logic (including ordering and not disturbing your settings), PE parsing, API
detection from strings and from logs, GPU generation mapping and CUBIN/PTX
compatibility, the loose-DLSS-files veto, route selection including the D3D11
mismatch case, the install/uninstall round trip verified by hashing the whole
folder before and after, double installs, route switching, refusing foreign
injectors, anti-cheat detection, manifest survival on failure, and the
diagnoser.

## Layout

```
dlss5kit/peinfo.py     PE parsing, API detection, NGX usage, exe ranking
dlss5kit/gpu.py        card detection, CUDA fatbin architecture check
dlss5kit/routes.py     route selection and the reasons for it
dlss5kit/sources.py    every download URL, in one place
dlss5kit/config.py     ReShade ini/preset and add-on cfg writing
dlss5kit/installer.py  install engine, manifest, uninstall
dlss5kit/diagnose.py   reading the logs back into an answer
dlss5kit/gui.py        tkinter window
dlss5kit/cli.py        command line
```
