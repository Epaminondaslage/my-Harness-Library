# My Harness Library

## Codebase Overview

Self-hosted, offline-first inventory and editor for a Claude Code installation. `src/inventory.py` scans `~/.claude` (skills, agents, commands, plugins, MCPs, `/opt/*/.claude` project resources) and emits a static 3-file site; `src/api.py` is the only dynamic part: a JSON backend over a Unix socket for editing the user's own resources, sandboxed by systemd so it cannot execute anything; `src/regenerate.sh` runs the generator from cron (daily, on the page's ↻ request, or automatically when the harness changes).

**Stack**: Python 3.9+ stdlib only, bash, systemd, nginx, cron. No pip, no npm, no build step: `app.js` and `styles.css` are raw strings inside `inventory.py`.
**Structure**: `src/` (generator, backend, install/uninstall/regenerate scripts, systemd unit template), `install.sh` (curl bootstrap), `docs/`, `.github/workflows/ci.yml`.

Runtime on the server: `/opt/harness-library` (what runs, root-owned; deploy via `install.sh | sudo bash` after commit+push, never edit there), `/var/www/html/my-harness-library` (generated), `~/.claude/.inventory/` (state: password hash, audit log, status, caches).

For detailed architecture, data flows, gotchas and a navigation guide, see [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md).

## Working rules

- Keep it stdlib-only and offline-first; network only behind `--online`.
- State lives only under `~/.claude/`; the backend sandbox sees nothing else writable.
- The backend must never spawn processes; regeneration goes through request files consumed by cron.
- Any new page text needs `data-pt`/`data-en` pairs (and a `MSG` entry for runtime strings).
- Verify before claiming done: `bash -n` on scripts, `python3 -m py_compile src/*.py`, run `inventory.py` against a fake `.claude`, `node --check` the emitted `app.js` (this is what CI does).
- Release: bump `VERSION`, commit `chore: release X.Y.Z`, tag `vX.Y.Z`, `gh release create`.
