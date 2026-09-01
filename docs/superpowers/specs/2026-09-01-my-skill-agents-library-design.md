# My Skill-Agents Library — Design

## Purpose

CRUD catalog of skill/plugin *suggestions* the user comes across, so they can
be triaged (candidata / aprovada / rejeitada) and decided on later — not an
inventory of what's already installed (that's my-Harness-Library's job).

## Repository

New, separate GitHub repo: `my-skill-agents-library`. Same conventions as
`my-Harness-Library` (see that repo's CLAUDE.md): Python 3.9+ stdlib only,
bash, systemd, nginx, cron. No pip, no npm, no build step.

## Data

State lives in `~/.claude/.skill-library/items.json` — a JSON array, one
object per catalog item:

```json
{
  "id": "uuid",
  "name": "superpowers",
  "repo": "obra/superpowers",
  "stars": "280,1K",
  "function": "Framework de skills agentic e metodologia de dev",
  "dev_note": "9/10",
  "status": "candidata",
  "personal_note": "",
  "decided_at": null,
  "created_at": "2026-09-01T00:00:00Z",
  "updated_at": "2026-09-01T00:00:00Z"
}
```

- `status` ∈ `candidata | aprovada | rejeitada`.
- `dev_note` carries the seed's existing "Nota Dev" rating as free text (not
  reinterpreted).
- `personal_note` is the user's own free-text reasoning, editable anytime.
- `decided_at` set when status moves away from `candidata`.

### Seed

On first run (`items.json` absent), `src/generator.py` parses
`my-Harness-Library`'s `Catalogo-de-Agent-Skills.md` table (rows 1-30 as of
this writing) and creates `items.json` with every row as `status: "candidata"`.
After that one-time import, the markdown file is not read again — the JSON
file is the sole source of truth, mutated only through the CRUD backend.

## Components

- `src/generator.py` — reads `items.json`, emits a static 3-file site
  (`index.html` shell + `app.js` + `styles.css`, both embedded as strings in
  the generator, matching my-Harness-Library's approach) to
  `/var/www/html/my-skill-agents-library`.
- `src/api.py` — JSON backend over a Unix socket, sandboxed by its own
  systemd unit so it cannot execute anything. Endpoints: list, add, edit,
  delete, set-status, set-note. Auth reuses the harness's existing password
  hash at `~/.claude/.inventory/` (read-only from this project — the secret
  is not duplicated or re-hashed here).
- `src/regenerate.sh` — regenerates the static site from cron (daily) or
  on-demand via a request file dropped by `api.py` after a write (never a
  direct process spawn from the backend, matching the harness's sandbox
  rule).
- `install.sh` — curl bootstrap, mirrors my-Harness-Library's: deploys to
  `/opt/skill-agents-library` (root-owned), sets up its own systemd unit,
  socket, and an nginx location block on a path/port distinct from the
  harness (exact path TBD at implementation time, e.g. `/skill-library`).

## Data flow

1. User visits page → static HTML/JS/CSS, login form (shared harness
   password).
2. Authenticated session → `app.js` calls `api.py` over the socket via
   nginx.
3. Add/edit/delete/status-change → `api.py` validates, writes
   `items.json`, drops a regen request file.
4. Cron (or immediate regen trigger) runs `regenerate.sh` →
   `generator.py` re-renders the static site.

## Error handling

- `api.py` rejects malformed payloads with a JSON error body, no partial
  writes (write to temp file + atomic rename).
- `generator.py` fails loudly (non-zero exit, message to stderr/log) if
  `items.json` is corrupt, rather than emitting a broken site.
- Missing `Catalogo-de-Agent-Skills.md` at first run → generator starts with
  an empty `items.json` and logs a warning; seed can be added manually later.

## Testing

Same checklist as my-Harness-Library's CLAUDE.md:
- `bash -n` on all scripts
- `python3 -m py_compile src/*.py`
- Run `generator.py` against a fake `~/.claude/.skill-library`
- `node --check` the emitted `app.js`

## Out of scope (YAGNI)

- No re-fetching of live GitHub stars/data (seed values are a snapshot;
  editing them is a manual CRUD action like any other field).
- No multi-user accounts — single shared harness password, same as today.
- No automatic sync back into `Catalogo-de-Agent-Skills.md` — that file
  stays as a one-time seed source, not a two-way mirror.
