# AGENTS.md - operating DLSS5Kit from an AI agent

You are installing DLSS 5 neural rendering into a PC game on someone's
Windows machine, on their behalf, without them opening a GUI. This file is the
contract: what to run, what the output means, and what to do when it fails.

Read the whole file before the first command. The failure mode of this domain
is **silent**: the game launches, nothing crashes, and the picture is
unchanged. Guessing produces exactly that outcome, so every step here is
evidence-driven.

---

## 0. The three rules

1. **Never invent a route.** Run `--check --json` and use the `route` field.
   The tool decides from the executable's own bytes and the game's logs; you
   do not have better information than it does.
2. **The game must be closed** before install or remove. Files are in use
   while it runs, and a partial write is worse than no write.
3. **Tell the user about anti-cheat.** If `warnings` mentions BattlEye, EAC,
   Vanguard or GameGuard, say so before installing and let them decide. This
   is a ban risk in multiplayer, and it is their account.

---

## 1. Get the tool

Single file, no installation, no Python:

```
https://github.com/UgurInanc12/DLSS5Kit/releases/latest  ->  DLSS5Kit.exe
```

From source instead (a checkout of this repo, Python 3.10+):

```bash
python -m dlss5kit.cli <same arguments>
```

Every example below writes `DLSS5Kit.exe`; substitute the source form freely,
the arguments and exit codes are identical.

Requirements: Windows, NVIDIA RTX 20 series or newer, a 64-bit DX11/DX12 or
Vulkan game.

---

## 2. The five commands

| Command | Writes anything? | Use it for |
|---|---|---|
| `DLSS5Kit.exe --gpu` | no | which card is present, and which builds support it |
| `DLSS5Kit.exe "<game folder>" --check --json` | no | inspect and get the route as data |
| `DLSS5Kit.exe "<game folder>"` | **yes** | install |
| `DLSS5Kit.exe "<game folder>" --diagnose --json` | no | after playing: did it work? |
| `DLSS5Kit.exe "<game folder>" --remove` | **yes** | uninstall, restoring backups |

Pass the folder the game's `.exe` actually sits in. A game whose binary lives
in `Bin64\` or `Binaries\Win64\` needs that subfolder, not the library root.
The tool will search downward and pick a candidate, but pointing straight at
the executable's folder is always safer.

**Quote the path.** Game folders contain spaces almost without exception.

---

## 3. Exit codes

Branch on these, not on parsed text.

| Code | Meaning | What to do |
|---|---|---|
| 0 | success | continue |
| 1 | install failed | read stderr; see section 7 |
| 2 | bad path, or the executable could not be read | check the path exists and points at a game folder |
| 3 | game not supported | read `blocker`; stop, do not retry |
| 4 | `--diagnose` returned a BAD verdict | see section 8 |

---

## 4. The standard flow

```bash
# 1. what card is this, and can it run DLSS 5 at all
DLSS5Kit.exe --gpu

# 2. inspect - writes nothing, returns the route as JSON
DLSS5Kit.exe "D:\Games\Steam\steamapps\common\Some Game" --check --json

# 3. install (only after the user has closed the game)
DLSS5Kit.exe "D:\Games\Steam\steamapps\common\Some Game"

# 4. the user plays for a few minutes, then:
DLSS5Kit.exe "D:\Games\Steam\steamapps\common\Some Game" --diagnose --json
```

Between 3 and 4 the user has to do something in the game that you cannot do
for them. Tell them explicitly; see section 6.

---

## 5. Reading `--check --json`

`schema: 1` is the version of this shape. If it is not 1, the fields below may
have changed and you should re-read this file.

```jsonc
{
  "schema": 1,
  "executable": "MetroExodus.exe",
  "bitness": 64,

  "api": "DX12",                    // DX9 DX10 DX11 DX12 Vulkan OpenGL Unknown
  "api_confidence": "high",         // high | medium | low
  "api_reason": "calls NVSDK_NGX_D3D12_* 21 time(s), ...",

  "ngx": { "d3d11": false, "d3d12": true, "vulkan": false, "any": true },

  "native_dlss": true,              // does the GAME have DLSS (not: are files present)
  "dlss_evidence": ["nvngx_dlss.dll"],
  "dlss_note": "",                  // non-empty = the files are somebody's leftovers

  "gpu": { "generation": "RTX 30", "sm": 86, "detected": true, "supported": true },

  "supported": true,
  "blocker": "",                    // why not, when supported is false
  "route": "native",                // native | bridge | feeder | null
  "route_reason": "...",
  "route_options": ["native", "bridge", "feeder"],
  "warnings": [],

  "installed": true,                // is DLSS5Kit already installed here
  "installed_route": "native",
  "install_complete": true,
  "present": { "ReShade": true, "DLSS 5 add-on": true, ... }
}
```

### Fields that change what you should say

**`route`** decides the in-game instructions you give afterwards. The three
routes are genuinely different products:

| route | What the user gets | Cost |
|---|---|---|
| `native` | the game's own DLSS is upgraded; Quality/Balanced/Performance still work | cheapest |
| `bridge` | same contract rebuilt on a private D3D12 session; quality mode still applies | middling |
| `feeder` | a synthetic contract built from ReShade's depth buffer | **always DLAA, never upscaling; costs frame rate** |

Tell a `feeder` user plainly that this will *lower* their frame rate in
exchange for image quality, and that the `--work-resolution` dial (50-100) is
the only performance control. Users who expect "DLSS makes it faster" are
about to be disappointed, and they should hear it from you first.

**`dlss_note`** non-empty means DLSS DLLs are sitting in the folder but the
executable never calls NGX. They are leftovers from a manual attempt, and the
tool ignores them. Repeat this to the user: they may believe their game has
DLSS when it does not.

**`api_confidence: "low"`** means the renderer could not be pinned down. The
install still works, but tell the user to run the game once and re-check,
because `ReShade.log` will then settle it and the route may change.

**`installed: true`** with a different `route` than the recommendation means a
previous install used another route. Installing again switches routes and
removes the old one automatically; you do not need to uninstall first.

---

## 6. What to tell the user after installing

The tool prints this, but you must repeat it, because the install does nothing
visible until the user acts:

**Every route:** press **Home** in game to open the ReShade overlay.

- `native` / `bridge`: open the **DLSS 5 Neural Rendering** panel and turn
  neural rendering **ON**. It is off by default. **F6** toggles it.
- `feeder`: tick **LUMENITE: Kernel 2.0** *and* **DLSS 5 Feed**, with the
  kernel **above** the feed in the list, then enable neural rendering, then
  turn the game's own MSAA/SSAA off.

**Say this too, every time:** set the resolution *before* turning neural
rendering on. The DLSS feature is built for one backbuffer size; changing
resolution or display mode while it runs forces a rebuild that can black
screen or freeze the game. Prefer borderless over exclusive fullscreen.

---

## 7. When install fails (exit 1)

The message on stderr is the diagnosis. Match it:

| Message contains | Cause | Do this |
|---|---|---|
| `already exists here but it is not ReShade` | another injector owns `dxgi.dll` (DXVK, Special K, an ENB) | Do **not** delete it silently. Tell the user what is there and ask. |
| `will not run on a <gen> card` | pinned build has no code for this GPU | drop the pinned version and let Auto pick, or check `--gpu` |
| `No nvngx_dlssnr build supports a <gen> card` | no build covers this generation | stop; the card is too old for the current builds |
| `has no Tensor cores` | GTX 10 series or older | stop; DLSS 5 needs RTX 20+ |
| `Could not write` / `Could not back up` | file in use or no permission | the game is still running, or the folder needs elevation |
| `does not contain` | a downloaded archive was truncated | delete `%LOCALAPPDATA%\dlss5kit\cache` and retry |
| `GitHub is rate limiting` | 60 anonymous API calls/hour exhausted | wait, or supply local files with `--local` |

A failed install is **still removable**: the manifest is written even when the
run aborts partway, so `--remove` cleans up whatever landed. Always offer it.

### Exit 3 (not supported)

Read `blocker` and relay it. The three cases are 32-bit games, DirectX 9/10,
and a GPU below RTX 20. None of them are retryable, and none are bugs. Do not
try flags to force past them.

---

## 8. When the user says "nothing happened" (`--diagnose`)

This is the common case, and it is what `--diagnose` exists for. Run it with
`--json`, then act on `verdict`:

```jsonc
{
  "verdict": "bad",                 // ok | warn | bad | unknown
  "summary": "The game itself reports: NVSDK_NGX_D3D11_CreateFeature failed with 0xbad00005.",
  "installed_route": "bridge",
  "findings": [
    { "level": "ok",  "text": "ReShade 6.8.0.2155 loaded.", "evidence": "" },
    { "level": "bad", "text": "MISMATCH: ...", "evidence": "the fix is the bridge route" }
  ]
}
```

Read `findings` in order; each `bad` entry carries its own `evidence` naming
the fix. The ones you will actually meet:

| Finding | Meaning | Fix |
|---|---|---|
| `ReShade.log does not exist` | ReShade never loaded | the game has not been run since installing, or the proxy DLL is not being loaded |
| `MISMATCH: the add-on hooked the D3D12 NGX entry points` | wrong route installed | reinstall with `--route bridge` |
| `The DLSS 5 add-on never registered` | add-on missing or add-ons disabled | reinstall; check `present["DLSS 5 add-on"]` |
| `Motion vectors are all zero` | feeder technique order is wrong | the user must put LUMENITE **above** DLSS 5 Feed in the overlay |
| `CreateFeature raised exception` | add-on build and dlssnr build disagree | try another combination |
| `dlss5-feed.log does not exist` (feeder) | the feeder add-on never ran | reinstall |
| verdict `ok` but the user still sees nothing | neural rendering is off | it is **off by default** - press F6 or tick it in the panel |

That last row is the single most common outcome. Check it first before
touching anything.

---

## 9. Useful flags

| Flag | When |
|---|---|
| `--route native\|bridge\|feeder` | override after a `--diagnose` mismatch |
| `--generation 20\|30\|40\|50` | force a card generation (dual-GPU machines, or preparing for another PC) |
| `--work-resolution 50..100` | feeder only; the performance dial |
| `--local <folder>` | prefer components already on disk instead of downloading |
| `--reshade-setup <exe>` | use a `ReShade_Setup_*_Addon.exe` already downloaded |
| `--replace-game-dlss` | replace the game's own `nvngx_dlss.dll` (backed up, restored on remove) |
| `--provider 3\|4\|0\|1\|2` | feeder motion-vector shader; 3 (LumeniteFX Kernel) is the default and almost always right |
| `--json` | machine-readable output for `--check`, `--diagnose`, `--remove` |
| `--version` | print the version and exit |
| `--yes` / `-y` | accepted for scripting symmetry; this CLI never prompts, so it changes nothing |
| `--gui` | open the window instead; **do not use this from an agent**, it blocks until the user closes it |

Flags that exist but you should not reach for casually:
`--ignore-gpu-mismatch` installs a build with no code for the card, which is
the silent-failure setup this whole tool exists to prevent.

---

## 10. What the tool writes, and how it undoes it

Every path written is recorded in `dlss5kit-manifest.json` in the game folder.
`--remove` restores backups first, then deletes only what is listed, then
prunes directories it created. A file the game already had is copied to
`<name>.dlss5kit-backup` before being overwritten, and put back on removal.

So: **you never need to clean up by hand, and you should not.** Deleting files
yourself desynchronises the manifest and makes a later `--remove` unable to
restore the game's originals.

The install touches only the executable's folder. Game content archives are
never opened.

---

## 11. Honesty requirements

- **Do not claim it works because the install succeeded.** The install writing
  files and the feature running are different things. Only `--diagnose` after
  a real play session can say it works.
- **Do not hide the feeder trade-off.** It costs frame rate. Say so.
- **Do not promise upscaling on the feeder route.** It is DLAA only, and the
  reason is architectural: there is no low-resolution frame to upscale from.
- **Relay anti-cheat warnings before installing, not after.**
- If the user is on an unsupported configuration, say so once, plainly, and
  stop. Do not try to force it.

---

## 12. What this tool downloads

Nothing is bundled. At run time it fetches from `reshade.me`,
`raw.githubusercontent.com`, `codeload.github.com`, `api.github.com` and
`github.com`, caching under `%LOCALAPPDATA%\dlss5kit\cache`.

ReShade, DLSS5-Feeder, LumeniteFX, dlss5-bridge and the shader headers come
from their authors' own repositories. The DLSS 5 add-on and the NVIDIA NGX
runtimes are closed-source files with no published licence, mirrored by the
community; the tool downloads them from that public mirror exactly as a person
would by hand. **If the user has not been told this, tell them** before the
first install, and mention that `--local` lets them supply their own copies
instead.

There is no telemetry and no account. Administrator rights are never required
or requested.

<!-- graphify:begin -->
## graphify

This project has a code knowledge graph at `graphify-out/` (2134 nodes, 3246 edges).

**Use it first.** The moment a task touches an existing symbol, file, or module
here, the FIRST tool call is a graph read, as orientation rather than after it.

| Situation | Call |
|---|---|
| "What does this project do", first contact | `god_nodes` then `query_graph` |
| Architecture, data flow, "how does X work" | `query_graph` |
| About to edit a shared symbol | `get_neighbors` on it, to see every caller |
| Tracing a bug across modules | `shortest_path` (add `undirected: true` if no path) |
| "What touches this function" | `get_node` + `get_neighbors` |

These are native MCP tools from the `graphify` server. Pass
`project_path: "D:/Hermes/DLSS5Kit"` so they answer from THIS project's graph. The CLI
equivalents (`graphify query` / `path` / `explain`) work too and log the same way.

Opening files with read_file / search_files / grep to understand this code,
without a graph read first, is a violation of the standing rule, even when the
file read would also work.

Not in the graph: status, phases, plans, decisions. Those live in the `.md` files
and must be read directly. The graph is code-only by decision.

After finishing code changes here, run `graphify update .` (AST only, seconds, no
API cost). A stale graph that answers confidently is worse than no graph.
<!-- graphify:end -->
