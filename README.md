# My Harness Library

**A self-hosted, searchable inventory of everything your Claude Code installation can actually do — with an in-browser Markdown editor for the resources you own.**

Claude Code accumulates capability fast. You install a plugin and get thirty skills you never see listed. You write a skill for one project and forget it exists. `~/.claude` becomes a place you *have*, not a place you *know*.

My Harness Library scans that tree — plus every installed plugin and every project-local `.claude/` — and publishes a single static page: every skill, agent, command, plugin and MCP server, each classified by purpose, each linked to the GitHub repository it came from, all searchable in one box.

On a typical installation the difference is stark: the Claude Code plugin list shows **58 plugins**; My Harness Library shows the **310 individual resources** inside them.

---

## Table of contents

- [What it does](#what-it-does)
- [Screenshot tour](#screenshot-tour)
- [Requirements](#requirements)
- [Installation](#installation)
- [First run: the initial password](#first-run-the-initial-password)
- [How it works](#how-it-works)
- [Security model](#security-model)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Notes for contributors](#notes-for-contributors)
- [License](#license)

---

## What it does

### Discovers everything, not just the top level

Five sources are scanned:

| Source | What is found |
|---|---|
| `~/.claude/skills`, `/agents`, `/commands` | the resources you wrote yourself |
| `~/.claude/plugins/**` | every skill, agent and command *inside* each installed plugin |
| `~/.claude.json` | MCP servers, global and per-project |
| plugin manifests | the plugin packages themselves |
| `<project>/.claude/**` | skills versioned inside your repositories |

Resources are tagged by origin — **Mine**, **From plugin**, **From project** — because the distinction matters: plugin resources are overwritten on the next marketplace update, and project resources live in git.

### Classifies by purpose

Every resource gets a category badge: **General**, **DevOps**, **Spec-Driven Ops**, **Quality**, **Security**, **Integrations**, **Tooling**, **Frontend**.

Classification is deterministic and layered, strongest first:

1. a `category:` field in the resource's own frontmatter — your explicit decision, always wins
2. an exact-name map covering the common marketplace catalogue
3. keyword rules over name and description, so new resources classify themselves
4. `Other`, when nothing matches

No LLM call, no network, no cost. To correct a classification, write one line in the file — from the built-in editor, if you like:

```yaml
---
name: deploy-prod
category: devops
---
```

### Finds the source repository — offline

Four layers, again strongest first: the resource directory's `git remote`; a `repository` field in its manifest or frontmatter; the marketplace's declared origin from `known_marketplaces.json`; and, only with `--online`, the npm registry and GitHub search (results from search are labelled *likely*).

Layer three is what makes this work without a network: it resolves **307 of 310** resources on a typical installation, exactly, because the marketplace registry records the repository each plugin came from.

### Edits the resources you own

Click any Markdown file under your own `~/.claude` and a modal opens: Markdown editor on the left, live preview on the right, a formatting toolbar, `Ctrl+B`/`Ctrl+I`/`Ctrl+S`.

- **YAML frontmatter is preserved byte for byte** — it is displayed as a separate block and never reformatted, because that block is what Claude Code actually reads.
- **Frontmatter is validated on save.** A skill or agent missing `name` or `description` is rejected with an explanation. Claude Code fails *silently* on those — the resource simply never loads, and you find out much later.
- **Every save keeps a dated revision** (last 10 per file), listed in the modal with a line-by-line diff against your current text, and one-click restore.
- **Concurrent edits are detected**: if the file changed on disk since you opened it, the save is refused rather than clobbering.

Plugin and project resources are deliberately **read-only** — editing them would be overwritten by the next plugin update, or would dirty a git tree.

### Stays current

A daily cron regenerates the page. The `↻` button in the header regenerates on demand.

### Bilingual, themed, keyboard-friendly

English and Portuguese, switched by the flag in the header, no reload. Light and dark themes following your OS by default. Both choices persist in `localStorage`, independently.

---

## Screenshot tour

The header carries, left to right: the title, the resource count and generation time, then 🇺🇸/🇧🇷 (language), 🔑 (change password), ↻ (regenerate), ＋ New, and the theme toggle.

Below it, a provenance card shows which host, which directory and which user this inventory came from — so a page shared between machines is never ambiguous.

Then: a search box, type tabs (`All 310 · Skills 155 · Agents 52 · Commands 43 · Plugins 58 · MCPs 2`), and two rows of filter chips — **Source** and **Purpose**. Filters combine with each other and with the search.

---

## Requirements

- Linux with **systemd**, **nginx** and **cron**
- **PHP 8.0+** with CLI and FPM (any minor version — it is detected, not hardcoded)
- **Python 3.8+** — standard library only, no `pip install`
- `curl`, `tar`, `flock`
- A user account owning `~/.claude`

Tested on Ubuntu 24.04 with PHP 8.3 and nginx 1.24. The installer checks all of the above and installs what is missing via `apt`.

The page is served from `/var/www/html/claude-inventory` by your existing nginx. If nothing is serving `/var/www/html` yet, the installer falls back to the default site.

---

## Installation

### One line

```bash
curl -fsSL https://raw.githubusercontent.com/Epaminondaslage/my-Harness-Library/main/install.sh | sudo bash
```

This downloads the sources to `/opt/harness-library` and runs the setup: dependency check, PHP-FPM pool, nginx route, cron entries, first generation, smoke test.

To choose the password up front instead of accepting the default:

```bash
curl -fsSL https://raw.githubusercontent.com/Epaminondaslage/my-Harness-Library/main/install.sh \
  | sudo HARNESS_PASSWORD='your-strong-password' bash
```

### Check first, install after

Piping a script into `sudo bash` is a decision you should make with your eyes open. To read it first:

```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/Epaminondaslage/my-Harness-Library/main/install.sh
less install.sh
sudo bash install.sh
```

### From a clone

```bash
git clone https://github.com/Epaminondaslage/my-Harness-Library.git
cd my-Harness-Library

bash src/setup.sh --check      # diagnose only — changes nothing, no sudo
sudo bash src/setup.sh         # install
```

`--check` reports every dependency, tells you the `apt` package for anything missing, and shows what is already configured. It is safe to run at any time.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HARNESS_PASSWORD` | — | initial write password, skipping prompt and default |
| `HARNESS_REF` | `main` | git ref to install |
| `HARNESS_PREFIX` | `/opt/harness-library` | where sources are kept |

---

## First run: the initial password

Writing to `~/.claude` is password-protected. Reading is not — the page is browsable by anyone who can reach it.

**How the password gets set depends on how you installed:**

| Install method | Password |
|---|---|
| `HARNESS_PASSWORD=...` given | yours |
| Run from a terminal (clone, or downloaded script) | you are prompted, twice, hidden |
| Piped through `curl \| sudo bash` | **`change-me-now`** |

That last row exists for a reason. A piped install has no terminal: `stdin` is the script itself, so there is nothing to prompt on. Rather than fail, or generate a password you would never see scroll past, the installer sets a documented one and prints a warning.

### `change-me-now`

That is the literal initial password. It is deliberately weak and deliberately memorable, and it exists only to get you to a working system.

**Change it before doing anything else:**

1. open the page
2. click 🔑 in the header
3. current password: `change-me-now`
4. new password, twice, minimum 8 characters
5. click **Change**

It takes effect on the very next request — no restart, no reload, no service to bounce. The old password stops working immediately, and the change is recorded in the audit log.

Only a **bcrypt hash** is ever written to disk, at `~/.claude/.inventory/auth.hash`, mode `600`. The plaintext is never stored, never logged, and never leaves the request.

There is no password recovery, by design. If you lose it, you have shell access to the machine — rewrite the hash:

```bash
php -r 'file_put_contents(getenv("HOME")."/.claude/.inventory/auth.hash",
  password_hash($argv[1], PASSWORD_BCRYPT)."\n");' 'new-password'
chmod 600 ~/.claude/.inventory/auth.hash
```

> **Before exposing this beyond a trusted network**, put authentication in front of the whole directory (nginx `auth_basic`) and serve it over TLS. The built-in password protects *writes*; it does not protect *reads*, and it is not a substitute for network-level access control.

---

## How it works

Three pieces, deliberately kept apart:

**`inventory.py`** walks the filesystem and writes three static files — `index.html`, `styles.css`, `app.js`. It talks to nothing, needs no network, and imports only the standard library. Run it by hand anywhere; it drops a `claude_inventory_site/` next to you.

**`api.php`** is the only dynamic part, reached at exactly one URL. It reads and writes Markdown files under `~/.claude`, checks the password, validates frontmatter, keeps revisions and appends to the audit log.

**`regenerate.sh`** ties them together: runs the generator, copies the output to the web root, records status. Cron calls it daily; a one-minute watcher calls it when the page requests a regeneration.

### Why the Regenerate button takes up to 60 seconds

The PHP pool runs with `exec`, `system`, `shell_exec`, `proc_open` and `popen` **disabled**. That is the point: the web backend cannot spawn a process, so a bug in it cannot become command execution.

Regeneration therefore does not run in PHP. The button writes a request file; the one-minute cron consumes it and runs the generator as your user. The cost is up to a minute of latency. The benefit is that the web-facing code never gains the ability to execute anything.

---

## Security model

This tool writes to a directory whose contents Claude Code later executes. It is built accordingly.

**The web server user gets nothing.** A typical `$HOME` is `750` and `~/.claude` is `700` — `www-data` cannot even traverse them, and this tool does not change that. Instead, `api.php` runs in its own PHP-FPM pool as *you*. No `chmod`, no `setfacl`, no group membership. Zero permission changes anywhere on the filesystem.

**The pool is fenced.** `open_basedir` limits it to `~/.claude` and the web root. Process-spawning functions are disabled. `allow_url_fopen` is off. Uploads are capped.

**Paths are checked twice.** A request names a file relative to `~/.claude`. The path must start with `skills/`, `agents/` or `commands/`, must end in `.md`, and must contain no `..`. It is then resolved with `realpath()` and re-checked against the allowed root — which catches a symlink pointing outside.

**Writes are atomic and reversible.** Content goes to a temporary file in the same directory and is renamed into place, so a failure never leaves a half-written skill. The previous content is copied to a dated revision first, pruned to the last 10.

**Everything that writes is logged.** `~/.claude/.inventory/audit.log`, one JSON object per line: timestamp, action, file, byte count, client IP, user agent. Rejected password attempts are logged too.

**Only your own files are writable.** Plugin and project resources are read-only, enforced server-side by the allowlist rather than by hiding a button.

**What this does not do:** it does not authenticate readers, encrypt anything, rate-limit beyond a fixed delay on a bad password, or defend against someone who already has shell access as you. Treat it as a tool for a trusted network.

---

## Configuration

Most behaviour is deliberately not configurable — fewer knobs, fewer ways to get it wrong. What you can change:

| What | Where | Default |
|---|---|---|
| Revisions kept per file | `KEEP_REVISIONS` in `api.php` | 10 |
| Max file size | `MAX_BYTES` in `api.php` | 1 MB |
| Daily regeneration time | crontab of your user | 06:22 |
| Categories and rules | `CATEGORY_LABEL`, `CATEGORY_MAP`, `CATEGORY_RULES` in `inventory.py` | eight categories |
| Projects scanned | `PROJECT_ROOTS` in `inventory.py` | `/opt/*/.claude` |
| Scanned tree | first CLI argument | `~/.claude` |

To classify a single resource, prefer `category:` in its frontmatter over editing the rules.

---

## Command reference

```bash
# Diagnose the installation — safe, read-only, no sudo
bash /opt/harness-library/setup.sh --check

# Regenerate now, from the shell
bash /opt/harness-library/regenerate.sh

# Regenerate and also query npm/GitHub for the few remaining repositories
bash /opt/harness-library/regenerate.sh --online

# Generate into the current directory without publishing (works anywhere)
python3 /opt/harness-library/inventory.py
python3 /opt/harness-library/inventory.py /some/other/.claude --online

# Read the audit log
cat ~/.claude/.inventory/audit.log | jq .

# Last generation status
cat ~/.claude/.inventory/status.json
```

`--online` honours `GITHUB_TOKEN` for a higher search rate limit:

```bash
GITHUB_TOKEN="$(gh auth token)" bash /opt/harness-library/regenerate.sh --online
```

---

## Uninstalling

```bash
sudo bash /opt/harness-library/uninstall.sh            # remove the service, keep your data
sudo bash /opt/harness-library/uninstall.sh --purge    # also remove password, audit log, revisions
```

Neither form touches your skills, agents or commands. Without `--purge`, your password and file revisions survive a reinstall.

---

## Troubleshooting

**`Backend unavailable (api.php)` in the editor**
The nginx route or the PHP-FPM pool is missing. Run `bash setup.sh --check`.

**`Wrong password` right after changing it**
Should not happen: the hash is stored as plain data precisely so PHP's opcache is not in the path. If it does, check that `~/.claude/.inventory/auth.hash` was actually rewritten.

**The page shows old content**
It is a static file. Click ↻ and wait up to 60 seconds, or run `regenerate.sh` directly. If nothing changes, check the one-minute cron: `crontab -l`.

**`Skills: 2` when you have dozens**
You are looking at an old build. Current versions scan inside plugins; the count should be in the hundreds.

**Nothing is editable**
Only files under your own `~/.claude/skills|agents|commands` are. Plugin and project resources are read-only by design — filter by **Source → Mine** to see what you can edit.

**`Incomplete frontmatter` when saving**
Working as intended. Skills and agents need `name` and `description`; without them Claude Code silently refuses to load the resource.

**API returns HTTP 500**
Usually `open_basedir`: the pool can only see `~/.claude` and the web root. Anything the backend touches must live inside those.

---

## Project layout

```
my-Harness-Library/
├── install.sh              one-line bootstrap (curl target)
├── README.md
├── LICENSE
└── src/
    ├── inventory.py        scanner and static-site generator
    ├── api.php             read/write backend (the only dynamic endpoint)
    ├── regenerate.sh       generate + publish + record status
    ├── setup.sh            dependency check and installer
    └── uninstall.sh        clean removal
```

Nothing is generated at build time and no dependency is vendored. What you read is what runs.

---

## Notes for contributors

The generated page carries both languages in `data-pt` / `data-en` attributes and swaps `textContent` on the client. There is no translation catalogue to keep in sync and no `/en` route — a string and its translation are written on the same line of the generator, so they cannot drift apart. Backend errors carry a stable `code`, which the front end translates, falling back to the server's own text for anything it does not recognise.

Inline code comments are in Portuguese; the README, the UI and all installer output are bilingual or English. Pull requests are welcome in either language.

Before opening a PR:

```bash
php -l src/api.php
bash -n src/setup.sh && bash -n src/regenerate.sh && bash -n src/uninstall.sh
python3 -m py_compile src/inventory.py
node --check "$(python3 src/inventory.py >/dev/null && echo claude_inventory_site/app.js)"
```

CI runs exactly these checks.

---

## License

MIT — see [LICENSE](LICENSE).

Built for [Claude Code](https://claude.com/claude-code). Not affiliated with Anthropic.
