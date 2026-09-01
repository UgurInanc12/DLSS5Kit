# Instructions for AI assistants working with DLSS5Kit

The full operating guide is in **[AGENTS.md](AGENTS.md)** at the root of this
repository. Read it before running anything.

It covers: the five commands, exit codes, the `--check --json` schema, what
each route means for the user, every install failure and its fix, and how to
read `--diagnose` output when the user says nothing happened.

Short version, so you do not start wrong:

```bash
DLSS5Kit.exe --gpu                                  # card + supported builds
DLSS5Kit.exe "<game folder>" --check --json         # inspect, writes nothing
DLSS5Kit.exe "<game folder>"                        # install (game closed!)
DLSS5Kit.exe "<game folder>" --diagnose --json      # after playing: did it work
DLSS5Kit.exe "<game folder>" --remove               # uninstall, restores backups
```

Three rules that prevent the usual failures:

1. Never choose the route yourself. Read the `route` field from
   `--check --json`.
2. The game must be closed before install or remove.
3. Neural rendering is **off by default** in game. Press Home for the ReShade
   overlay and turn it on, or the user sees no change and reports it broken.
