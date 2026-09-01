# My Skill-Agents Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new, separate repo `my-skill-agents-library` — a CRUD catalog of skill/plugin suggestions (status: candidata/aprovada/rejeitada + personal notes), seeded once from `my-Harness-Library`'s `Catalogo-de-Agent-Skills.md`, served as a static site with a sandboxed JSON backend, mirroring my-Harness-Library's architecture.

**Architecture:** `src/generator.py` renders `~/.claude/.skill-library/items.json` into a static 3-file site (`index.html`/`app.js`/`styles.css` as strings embedded in the generator). `src/api.py` is a Unix-socket JSON backend, sandboxed by its own systemd unit, doing CRUD on `items.json` — it never spawns processes; writes drop a regen request file that `src/regenerate.sh` (cron + on-demand) picks up. Auth reuses my-Harness-Library's existing password hash at `~/.claude/.inventory/auth.hash` (read-only).

**Tech Stack:** Python 3.9+ stdlib only (no pip/npm), bash, systemd, nginx, cron.

**Spec:** `/home/epaminondas/my-Harness-Library/docs/superpowers/specs/2026-09-01-my-skill-agents-library-design.md`

## Global Constraints

- Stdlib-only, offline-first — no third-party packages, no build step.
- State lives only under `~/.claude/.skill-library/` — the backend sandbox (systemd `ReadWritePaths`) sees nothing else writable.
- `api.py` must never spawn a process — regeneration goes only through the `regen.request` file consumed by cron/`regenerate.sh`.
- Auth: read (never write or duplicate) the password hash from `~/.claude/.inventory/auth.hash` — one shared password across both harness apps.
- `items.json` writes are atomic (write temp file + `os.replace`).
- `status` ∈ `candidata | aprovada | rejeitada` — validated server-side on every write.
- Verify each task with: `bash -n` on scripts, `python3 -m py_compile src/*.py`, `python3 -m unittest` for tests, `node --check` on the emitted `app.js`.

---

## File Structure

```
my-skill-agents-library/
├── CLAUDE.md
├── README.md
├── install.sh
├── src/
│   ├── generator.py          # renders items.json -> static site
│   ├── seed.py                # one-time markdown -> items.json importer
│   ├── api.py                 # CRUD backend over Unix socket
│   ├── regenerate.sh          # cron/on-demand regen (mirrors harness)
│   ├── setup.sh                # installs systemd unit, nginx snippet, dirs
│   ├── uninstall.sh
│   └── skill-agents-library.service.in
└── tests/
    ├── test_seed.py
    ├── test_generator.py
    └── test_api.py
```

- `seed.py` is its own file (not folded into `generator.py`) because it runs exactly once (markdown parsing, a distinct responsibility) while `generator.py` runs on every regen (JSON -> HTML).
- `api.py` owns all mutation logic; `generator.py` is read-only over `items.json`. This mirrors my-Harness-Library's api.py/inventory.py split.

---

### Task 1: Repo scaffold + CLAUDE.md

**Files:**
- Create: `/home/epaminondas/my-skill-agents-library/CLAUDE.md`
- Create: `/home/epaminondas/my-skill-agents-library/README.md`
- Create: `/home/epaminondas/my-skill-agents-library/.gitignore`

**Interfaces:**
- Produces: repo root that later tasks add `src/` and `tests/` into.

- [ ] **Step 1: Create the repo directory and git-init it**

```bash
mkdir -p /home/epaminondas/my-skill-agents-library
cd /home/epaminondas/my-skill-agents-library
git init
```

- [ ] **Step 2: Write CLAUDE.md**

```markdown
# My Skill-Agents Library

## Codebase Overview

Self-hosted, offline-first CRUD catalog for skill/plugin *suggestions* the
user comes across — separate from my-Harness-Library, which inventories
what's already installed. Each item carries a status
(`candidata`/`aprovada`/`rejeitada`) and a personal note, so decisions on
whether to install can be made later. `src/seed.py` imports the initial set
from my-Harness-Library's `Catalogo-de-Agent-Skills.md` (one-time only);
after that, `~/.claude/.skill-library/items.json` is the sole source of
truth, mutated only through `src/api.py`. `src/generator.py` renders it to a
static 3-file site; `src/regenerate.sh` runs it from cron or on-demand.

**Stack**: Python 3.9+ stdlib only, bash, systemd, nginx, cron. No pip, no
npm, no build step.

**Structure**: `src/` (generator, seed importer, backend, install/uninstall
scripts, systemd unit template), `install.sh` (bootstrap), `tests/`.

Runtime on the server: `/opt/skill-agents-library` (root-owned, deploy via
`install.sh | sudo bash`, never edit there), `/var/www/html/my-skill-agents-library`
(generated), `~/.claude/.skill-library/` (state: items.json, audit log,
status, regen request).

Auth is shared with my-Harness-Library: this project reads (never writes)
`~/.claude/.inventory/auth.hash`.

## Working rules

- Keep it stdlib-only and offline-first.
- State lives only under `~/.claude/.skill-library/`; the backend sandbox
  sees nothing else writable.
- The backend must never spawn processes — regeneration goes through
  `regen.request`, consumed by cron.
- Verify before claiming done: `bash -n` on scripts, `python3 -m py_compile
  src/*.py`, `python3 -m unittest discover tests`, `node --check` the
  emitted `app.js`.
```

- [ ] **Step 3: Write README.md**

```markdown
# My Skill-Agents Library

Track skill/plugin suggestions you run across, decide later whether to
install them.

Seeded once from my-Harness-Library's `Catalogo-de-Agent-Skills.md`, then
edited through the web UI (add / edit / delete / change status / add a
personal note). See `CLAUDE.md` for architecture.
```

- [ ] **Step 4: Write .gitignore**

```
__pycache__/
*.pyc
.DS_Store
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: scaffold my-skill-agents-library repo"
```

---

### Task 2: items.json schema + seed importer

**Files:**
- Create: `src/seed.py`
- Test: `tests/test_seed.py`

**Interfaces:**
- Produces: `seed.parse_catalog_markdown(md_text: str) -> list[dict]` — each
  dict has keys `id, name, repo, stars, function, dev_note, status,
  personal_note, decided_at, created_at, updated_at`, `status` always
  `"candidata"`, `id` a `uuid4` hex string, timestamps ISO-8601 UTC.
- Produces: `seed.import_catalog(md_path: Path, items_path: Path,
  now: datetime | None = None) -> list[dict]` — writes `items.json`
  atomically only if it doesn't already exist; returns the list written (or
  the existing list, untouched, if the file was already there).
- Consumes: nothing from other tasks (this is the first Python module).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_seed.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import seed  # noqa: E402

SAMPLE_MD = """\
# Catálogo de Agent Skills. 31-08-2026

| # | Skill | Repositório GitHub | ⭐ Stars | 📦 Instalações | Função principal | Nota Dev |
|---:|---|---|---:|---:|---|:---:|
| 1 | `grill-me` | `mattpocock/skills` | ⭐ 242,8K | 1,0M | Questionar e validar arquitetura | 10/10 |
| 2 | `caveman` | `juliusbrussee/caveman` | ⭐ ~102K | 467,2K | Comunicação técnica compacta | 7/10 |

## Ranking recomendado para desenvolvimento

| Rank | Skill | Nota | Principal utilização |
|---:|---|:---:|---|
| 🥇 1 | `grill-me` | 10/10 | Validação de arquitetura |
"""


class TestParseCatalogMarkdown(unittest.TestCase):
    def test_parses_only_the_first_table(self):
        items = seed.parse_catalog_markdown(SAMPLE_MD)
        self.assertEqual(len(items), 2)

    def test_fields_mapped_correctly(self):
        items = seed.parse_catalog_markdown(SAMPLE_MD)
        first = items[0]
        self.assertEqual(first["name"], "grill-me")
        self.assertEqual(first["repo"], "mattpocock/skills")
        self.assertEqual(first["stars"], "242,8K")
        self.assertEqual(first["function"], "Questionar e validar arquitetura")
        self.assertEqual(first["dev_note"], "10/10")

    def test_defaults(self):
        items = seed.parse_catalog_markdown(SAMPLE_MD)
        first = items[0]
        self.assertEqual(first["status"], "candidata")
        self.assertEqual(first["personal_note"], "")
        self.assertIsNone(first["decided_at"])
        self.assertTrue(first["id"])
        self.assertIn("created_at", first)
        self.assertIn("updated_at", first)

    def test_ignores_header_and_alignment_rows(self):
        items = seed.parse_catalog_markdown(SAMPLE_MD)
        names = [i["name"] for i in items]
        self.assertNotIn("Skill", names)


class TestImportCatalog(unittest.TestCase):
    def test_writes_items_json_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "catalog.md"
            md_path.write_text(SAMPLE_MD, encoding="utf-8")
            items_path = Path(tmp) / "items.json"

            result = seed.import_catalog(md_path, items_path)

            self.assertTrue(items_path.exists())
            on_disk = json.loads(items_path.read_text(encoding="utf-8"))
            self.assertEqual(len(on_disk), 2)
            self.assertEqual(result, on_disk)

    def test_does_not_overwrite_existing_items_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "catalog.md"
            md_path.write_text(SAMPLE_MD, encoding="utf-8")
            items_path = Path(tmp) / "items.json"
            existing = [{"id": "keep-me", "name": "already-here"}]
            items_path.write_text(json.dumps(existing), encoding="utf-8")

            result = seed.import_catalog(md_path, items_path)

            self.assertEqual(result, existing)
            on_disk = json.loads(items_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk, existing)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_seed -v`
Expected: `ModuleNotFoundError: No module named 'seed'` (src/seed.py doesn't exist yet)

- [ ] **Step 3: Write src/seed.py**

```python
#!/usr/bin/env python3
# =============================================================================
# seed.py — one-time importer: Catalogo-de-Agent-Skills.md -> items.json
# -----------------------------------------------------------------------------
# Parses only the FIRST markdown table in the file (the numbered catalog
# table). Every row becomes an item with status "candidata". Never overwrites
# an existing items.json — the JSON file is the source of truth after the
# first import.
# =============================================================================

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROW_RE = re.compile(r"^\|(.+)\|\s*$")


def _clean_cell(cell: str) -> str:
    cell = cell.strip()
    cell = cell.strip("`")
    cell = re.sub(r"^⭐\s*", "", cell)
    return cell.strip()


def parse_catalog_markdown(md_text: str) -> list[dict]:
    """Parse the first markdown table in md_text into seed item dicts."""
    lines = md_text.splitlines()
    rows: list[list[str]] = []
    in_table = False
    for line in lines:
        m = ROW_RE.match(line)
        if not m:
            if in_table:
                break  # first table ended
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not in_table:
            in_table = True
            continue  # header row
        if set("".join(cells)) <= set("-: "):
            continue  # alignment row (---:|---|...)
        rows.append(cells)

    now = datetime.now(timezone.utc).isoformat()
    items = []
    for cells in rows:
        # columns: # | Skill | Repo | Stars | Installs | Função | Nota Dev
        if len(cells) < 7:
            continue
        items.append(
            {
                "id": uuid.uuid4().hex,
                "name": _clean_cell(cells[1]),
                "repo": _clean_cell(cells[2]),
                "stars": _clean_cell(cells[3]),
                "function": _clean_cell(cells[5]),
                "dev_note": _clean_cell(cells[6]),
                "status": "candidata",
                "personal_note": "",
                "decided_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    return items


def import_catalog(md_path: Path, items_path: Path, now=None) -> list[dict]:
    """Seed items.json from md_path unless items_path already exists."""
    if items_path.exists():
        return json.loads(items_path.read_text(encoding="utf-8"))

    md_text = md_path.read_text(encoding="utf-8")
    items = parse_catalog_markdown(md_text)

    items_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = items_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, items_path)
    return items


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: seed.py <catalog.md> <items.json>", file=sys.stderr)
        raise SystemExit(2)
    result = import_catalog(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"seeded {len(result)} items -> {sys.argv[2]}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_seed -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/seed.py tests/test_seed.py
git commit -m "feat: add markdown catalog seed importer"
```

---

### Task 3: generator.py — static site renderer

**Files:**
- Create: `src/generator.py`
- Test: `tests/test_generator.py`

**Interfaces:**
- Consumes: nothing structural from `seed.py` beyond the item dict shape
  documented in Task 2 (`id, name, repo, stars, function, dev_note, status,
  personal_note, decided_at, created_at, updated_at`).
- Produces: `generator.render_index_html(items: list[dict]) -> str`,
  `generator.render_app_js() -> str`, `generator.render_styles_css() -> str`,
  `generator.build_site(items_path: Path, out_dir: Path) -> None` (writes
  `index.html`, `app.js`, `styles.css` into `out_dir`).
- Produces (consumed by Task 4/api.py indirectly via the site, not by
  Python): the JS `fetch` calls in `app.js` target `/skill-library/api`
  (nginx location proxied to the backend socket) with actions
  `list|add|edit|delete|set_status|set_note|passwd` — Task 4 must implement
  exactly these action names.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_generator.py
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import generator  # noqa: E402

ITEMS = [
    {
        "id": "abc123",
        "name": "grill-me",
        "repo": "mattpocock/skills",
        "stars": "242,8K",
        "function": "Questionar e validar arquitetura",
        "dev_note": "10/10",
        "status": "candidata",
        "personal_note": "",
        "decided_at": None,
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
]


class TestRenderIndexHtml(unittest.TestCase):
    def test_embeds_items_as_json(self):
        html = generator.render_index_html(ITEMS)
        self.assertIn("grill-me", html)
        self.assertIn('<script id="items-data" type="application/json">', html)

    def test_links_app_js_and_styles_css(self):
        html = generator.render_index_html(ITEMS)
        self.assertIn('src="app.js"', html)
        self.assertIn('href="styles.css"', html)

    def test_escapes_script_close_tag(self):
        evil = [{**ITEMS[0], "personal_note": "</script><script>alert(1)</script>"}]
        html = generator.render_index_html(evil)
        self.assertNotIn("</script><script>alert(1)</script>", html)


class TestBuildSite(unittest.TestCase):
    def test_writes_three_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.json"
            items_path.write_text(json.dumps(ITEMS), encoding="utf-8")
            out_dir = Path(tmp) / "site"

            generator.build_site(items_path, out_dir)

            self.assertTrue((out_dir / "index.html").exists())
            self.assertTrue((out_dir / "app.js").exists())
            self.assertTrue((out_dir / "styles.css").exists())

    def test_raises_on_corrupt_items_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.json"
            items_path.write_text("{not json", encoding="utf-8")
            out_dir = Path(tmp) / "site"

            with self.assertRaises(json.JSONDecodeError):
                generator.build_site(items_path, out_dir)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_generator -v`
Expected: `ModuleNotFoundError: No module named 'generator'`

- [ ] **Step 3: Write src/generator.py**

```python
#!/usr/bin/env python3
# =============================================================================
# generator.py — renders ~/.claude/.skill-library/items.json into a static
# 3-file site (index.html / app.js / styles.css). Read-only over items.json;
# all mutation happens through api.py.
# =============================================================================

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", "")).expanduser()
CLAUDE = HOME / ".claude"
STATE = CLAUDE / ".skill-library"
ITEMS_FILE = STATE / "items.json"
OUT_DIR = Path("/var/www/html/my-skill-agents-library")


def _json_for_script_tag(items: list[dict]) -> str:
    # Standard escape so a payload containing "</script>" cannot break out
    # of the embedding <script> tag.
    return json.dumps(items, ensure_ascii=False).replace("</", "<\\/")


def render_index_html(items: list[dict]) -> str:
    payload = _json_for_script_tag(items)
    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Skill-Agents Library</title>
<link rel="stylesheet" href="styles.css">
</head>
<body>
<div id="app"></div>
<script id="items-data" type="application/json">{payload}</script>
<script src="app.js"></script>
</body>
</html>
"""


def render_app_js() -> str:
    return """\
const API = "/skill-library/api";
const STATUSES = ["candidata", "aprovada", "rejeitada"];

function loadItems() {
  const raw = document.getElementById("items-data").textContent;
  return JSON.parse(raw);
}

function call(action, body) {
  return fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...body }),
  }).then((r) => r.json());
}

function render(items, filterStatus) {
  const app = document.getElementById("app");
  app.innerHTML = "";

  const filters = document.createElement("div");
  filters.className = "filters";
  ["todas", ...STATUSES].forEach((s) => {
    const btn = document.createElement("button");
    btn.textContent = s;
    btn.className = s === filterStatus ? "active" : "";
    btn.onclick = () => render(items, s === "todas" ? null : s);
    filters.appendChild(btn);
  });
  app.appendChild(filters);

  const list = document.createElement("div");
  list.className = "list";
  items
    .filter((i) => !filterStatus || i.status === filterStatus)
    .forEach((item) => list.appendChild(renderItem(item, items)));
  app.appendChild(list);
}

function renderItem(item, items) {
  const card = document.createElement("div");
  card.className = "card status-" + item.status;

  const title = document.createElement("h3");
  title.textContent = item.name + " — " + item.repo;
  card.appendChild(title);

  const fn = document.createElement("p");
  fn.textContent = item.function;
  card.appendChild(fn);

  const select = document.createElement("select");
  STATUSES.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    opt.selected = s === item.status;
    select.appendChild(opt);
  });
  select.onchange = () =>
    call("set_status", { id: item.id, status: select.value }).then(() =>
      window.location.reload()
    );
  card.appendChild(select);

  const note = document.createElement("textarea");
  note.placeholder = "nota pessoal";
  note.value = item.personal_note || "";
  note.onblur = () => call("set_note", { id: item.id, personal_note: note.value });
  card.appendChild(note);

  return card;
}

document.addEventListener("DOMContentLoaded", () => {
  render(loadItems(), null);
});
"""


def render_styles_css() -> str:
    return """\
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem; }
.filters button { margin-right: .5rem; }
.filters button.active { font-weight: bold; }
.list { display: grid; gap: 1rem; margin-top: 1rem; }
.card { border: 1px solid #8888; border-radius: 8px; padding: .75rem; }
.card textarea { width: 100%; min-height: 3rem; margin-top: .5rem; }
"""


def build_site(items_path: Path, out_dir: Path = OUT_DIR) -> None:
    items = json.loads(items_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_index_html(items), encoding="utf-8")
    (out_dir / "app.js").write_text(render_app_js(), encoding="utf-8")
    (out_dir / "styles.css").write_text(render_styles_css(), encoding="utf-8")


if __name__ == "__main__":
    build_site(ITEMS_FILE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_generator -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Check the emitted app.js is valid JS**

Run:
```bash
python3 -c "
import sys; sys.path.insert(0, 'src')
import generator
open('/tmp/app.js', 'w').write(generator.render_app_js())
"
node --check /tmp/app.js
```
Expected: no output (valid syntax)

- [ ] **Step 6: Commit**

```bash
git add src/generator.py tests/test_generator.py
git commit -m "feat: add static site generator"
```

---

### Task 4: api.py — CRUD backend (business logic, no socket yet)

**Files:**
- Create: `src/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: nothing from `generator.py`/`seed.py` at import time; shares the
  item dict shape from Task 2.
- Produces: a `SkillLibrary` class with methods `list_items() -> list[dict]`,
  `add_item(name, repo, stars, function, dev_note) -> dict`,
  `edit_item(id, **fields) -> dict`, `delete_item(id) -> None`,
  `set_status(id, status) -> dict`, `set_note(id, personal_note) -> dict`,
  each raising `ApiError(status, message, code)` on invalid input (mirrors
  my-Harness-Library's `api.py` `ApiError`). Also produces
  `check_password(plain: str) -> bool` reading
  `~/.claude/.inventory/auth.hash` read-only, and `queue_regen() -> None`
  that touches `~/.claude/.skill-library/regen.request`.
- This task does NOT implement the `socketserver`/HTTP wiring — that is
  Task 5, which imports `SkillLibrary` and dispatches actions to it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api.py
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import api  # noqa: E402


def make_lib(tmp):
    items_path = Path(tmp) / "items.json"
    items_path.write_text("[]", encoding="utf-8")
    request_path = Path(tmp) / "regen.request"
    return api.SkillLibrary(items_path=items_path, request_path=request_path)


class TestAddItem(unittest.TestCase):
    def test_adds_item_with_candidata_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(
                name="foo", repo="org/foo", stars="1K", function="does foo", dev_note="7/10"
            )
            self.assertEqual(item["status"], "candidata")
            self.assertEqual(len(lib.list_items()), 1)

    def test_rejects_empty_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            with self.assertRaises(api.ApiError):
                lib.add_item(name="", repo="org/foo", stars="1K", function="x", dev_note="1/10")


class TestSetStatus(unittest.TestCase):
    def test_updates_status_and_decided_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            updated = lib.set_status(item["id"], "aprovada")
            self.assertEqual(updated["status"], "aprovada")
            self.assertIsNotNone(updated["decided_at"])

    def test_rejects_invalid_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            with self.assertRaises(api.ApiError):
                lib.set_status(item["id"], "nope")

    def test_unknown_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            with self.assertRaises(api.ApiError):
                lib.set_status("missing-id", "aprovada")


class TestSetNote(unittest.TestCase):
    def test_updates_personal_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            updated = lib.set_note(item["id"], "vale a pena testar")
            self.assertEqual(updated["personal_note"], "vale a pena testar")


class TestEditItem(unittest.TestCase):
    def test_edits_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            updated = lib.edit_item(item["id"], name="bar", stars="2K")
            self.assertEqual(updated["name"], "bar")
            self.assertEqual(updated["stars"], "2K")
            self.assertEqual(updated["repo"], "o/f")  # untouched field kept


class TestDeleteItem(unittest.TestCase):
    def test_deletes_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            item = lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            lib.delete_item(item["id"])
            self.assertEqual(lib.list_items(), [])

    def test_unknown_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            with self.assertRaises(api.ApiError):
                lib.delete_item("missing-id")


class TestPersistence(unittest.TestCase):
    def test_writes_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            items_path = Path(tmp) / "items.json"
            items_path.write_text("[]", encoding="utf-8")
            request_path = Path(tmp) / "regen.request"

            lib1 = api.SkillLibrary(items_path=items_path, request_path=request_path)
            lib1.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")

            lib2 = api.SkillLibrary(items_path=items_path, request_path=request_path)
            self.assertEqual(len(lib2.list_items()), 1)


class TestQueueRegen(unittest.TestCase):
    def test_add_item_queues_regen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = make_lib(tmp)
            lib.add_item(name="foo", repo="o/f", stars="1K", function="x", dev_note="1/10")
            self.assertTrue(lib.request_path.exists())


class TestCheckPassword(unittest.TestCase):
    def test_returns_false_when_auth_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.hash"
            self.assertFalse(api.check_password("anything", auth_path=missing))

    def test_matches_hash_written_by_harness_scheme(self):
        import hashlib
        import secrets

        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.hash"
            salt = secrets.token_bytes(16)
            key = hashlib.scrypt(b"correct horse", salt=salt, n=2**14, r=8, p=1, dklen=32)
            auth_path.write_text(
                f"scrypt$16384$8$1${salt.hex()}${key.hex()}", encoding="utf-8"
            )
            self.assertTrue(api.check_password("correct horse", auth_path=auth_path))
            self.assertFalse(api.check_password("wrong", auth_path=auth_path))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_api -v`
Expected: `ModuleNotFoundError: No module named 'api'`

- [ ] **Step 3: Write src/api.py**

```python
#!/usr/bin/env python3
# =============================================================================
# api.py — CRUD backend for My Skill-Agents Library
# -----------------------------------------------------------------------------
# Standard library only. This module owns the business logic (SkillLibrary);
# the Unix-socket/HTTP wiring lives in serve.py (Task 5) so the logic here
# stays testable without a running server.
#
# Auth reuses my-Harness-Library's password hash at
# ~/.claude/.inventory/auth.hash — read-only, never written or duplicated
# here.
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("candidata", "aprovada", "rejeitada")

HOME = Path(os.environ.get("HOME", "")).expanduser()
CLAUDE = HOME / ".claude"
STATE = CLAUDE / ".skill-library"
ITEMS_FILE = STATE / "items.json"
REQUEST_FILE = STATE / "regen.request"
HARNESS_AUTH_FILE = CLAUDE / ".inventory" / "auth.hash"

EDITABLE_FIELDS = ("name", "repo", "stars", "function", "dev_note")


class ApiError(Exception):
    """Error carrying the HTTP status and the stable code the UI translates."""

    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message}
        if code:
            self.payload["code"] = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_password(plain: str, auth_path: Path = HARNESS_AUTH_FILE) -> bool:
    """Verify plain against the harness's scrypt$n$r$p$salt$key hash file."""
    if not auth_path.exists():
        return False
    try:
        scheme, n, r, p, salt_hex, key_hex = auth_path.read_text(encoding="utf-8").strip().split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        candidate = hashlib.scrypt(
            plain.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, OSError):
        return False


class SkillLibrary:
    def __init__(self, items_path: Path = ITEMS_FILE, request_path: Path = REQUEST_FILE):
        self.items_path = items_path
        self.request_path = request_path

    # -- persistence ---------------------------------------------------

    def _read(self) -> list[dict]:
        if not self.items_path.exists():
            return []
        return json.loads(self.items_path.read_text(encoding="utf-8"))

    def _write(self, items: list[dict]) -> None:
        self.items_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.items_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self.items_path)
        self._queue_regen()

    def _queue_regen(self) -> None:
        self.request_path.parent.mkdir(parents=True, exist_ok=True)
        self.request_path.write_text(_now(), encoding="utf-8")

    def _find(self, items: list[dict], item_id: str) -> dict:
        for item in items:
            if item["id"] == item_id:
                return item
        raise ApiError(404, f"item não encontrado: {item_id}", code="not_found")

    # -- reads -----------------------------------------------------------

    def list_items(self) -> list[dict]:
        return self._read()

    # -- writes ------------------------------------------------------------

    def add_item(self, name: str, repo: str, stars: str, function: str, dev_note: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ApiError(400, "name é obrigatório", code="invalid_name")
        now = _now()
        item = {
            "id": uuid.uuid4().hex,
            "name": name,
            "repo": (repo or "").strip(),
            "stars": (stars or "").strip(),
            "function": (function or "").strip(),
            "dev_note": (dev_note or "").strip(),
            "status": "candidata",
            "personal_note": "",
            "decided_at": None,
            "created_at": now,
            "updated_at": now,
        }
        items = self._read()
        items.append(item)
        self._write(items)
        return item

    def edit_item(self, item_id: str, **fields) -> dict:
        items = self._read()
        item = self._find(items, item_id)
        for key, value in fields.items():
            if key in EDITABLE_FIELDS and value is not None:
                item[key] = value
        item["updated_at"] = _now()
        self._write(items)
        return item

    def delete_item(self, item_id: str) -> None:
        items = self._read()
        self._find(items, item_id)  # raises if missing
        items = [i for i in items if i["id"] != item_id]
        self._write(items)

    def set_status(self, item_id: str, status: str) -> dict:
        if status not in STATUSES:
            raise ApiError(400, f"status inválido: {status}", code="invalid_status")
        items = self._read()
        item = self._find(items, item_id)
        item["status"] = status
        item["decided_at"] = _now() if status != "candidata" else None
        item["updated_at"] = _now()
        self._write(items)
        return item

    def set_note(self, item_id: str, personal_note: str) -> dict:
        items = self._read()
        item = self._find(items, item_id)
        item["personal_note"] = personal_note or ""
        item["updated_at"] = _now()
        self._write(items)
        return item
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_api -v`
Expected: all 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/api.py tests/test_api.py
git commit -m "feat: add SkillLibrary CRUD business logic"
```

---

### Task 5: serve.py — Unix-socket HTTP wiring

**Files:**
- Create: `src/serve.py`

**Interfaces:**
- Consumes: `api.SkillLibrary`, `api.check_password`, `api.ApiError` (Task 4).
- Produces: a `run(socket_path: str)` entry point started by the systemd
  unit (Task 6). Dispatches JSON `{"action": ..., "password": ..., ...}`
  POST bodies to `SkillLibrary` methods; `add/edit/delete/set_status/
  set_note` all require a valid `password` field (checked via
  `check_password`) before mutating; `list` requires no password.

- [ ] **Step 1: Write src/serve.py**

```python
#!/usr/bin/env python3
# =============================================================================
# serve.py — Unix-socket JSON server wiring for the SkillLibrary backend.
# One nginx location proxies exactly this socket. Never spawns a process;
# mutations only ever write items.json + regen.request (see api.py).
# =============================================================================

from __future__ import annotations

import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from api import ApiError, SkillLibrary, check_password

SOCKET_PATH = os.environ.get("SKILL_LIBRARY_SOCKET", "/run/skill-agents-library/sock")

WRITE_ACTIONS = {"add", "edit", "delete", "set_status", "set_note"}


class Handler(BaseHTTPRequestHandler):
    lib = SkillLibrary()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw or b"{}")
            action = req.get("action")

            if action in WRITE_ACTIONS and not check_password(req.get("password", "")):
                raise ApiError(401, "senha inválida", code="bad_password")

            if action == "list":
                result = self.lib.list_items()
            elif action == "add":
                result = self.lib.add_item(
                    name=req.get("name", ""),
                    repo=req.get("repo", ""),
                    stars=req.get("stars", ""),
                    function=req.get("function", ""),
                    dev_note=req.get("dev_note", ""),
                )
            elif action == "edit":
                fields = {k: req.get(k) for k in ("name", "repo", "stars", "function", "dev_note")}
                result = self.lib.edit_item(req.get("id"), **fields)
            elif action == "delete":
                self.lib.delete_item(req.get("id"))
                result = {"ok": True}
            elif action == "set_status":
                result = self.lib.set_status(req.get("id"), req.get("status"))
            elif action == "set_note":
                result = self.lib.set_note(req.get("id"), req.get("personal_note", ""))
            else:
                raise ApiError(400, f"ação desconhecida: {action}", code="unknown_action")

            self._send_json(200, result if isinstance(result, dict) else {"items": result})
        except ApiError as exc:
            self._send_json(exc.status, exc.payload)
        except Exception as exc:  # noqa: BLE001 — last-resort JSON error, never a stack trace to the client
            self._send_json(500, {"error": str(exc), "code": "internal_error"})

    def log_message(self, format, *args):  # noqa: A002 — silence default stderr logging
        pass


class UnixHTTPServer(socketserver.UnixStreamServer):
    allow_reuse_address = True


def run(socket_path: str = SOCKET_PATH) -> None:
    sock_dir = Path(socket_path).parent
    sock_dir.mkdir(parents=True, exist_ok=True)
    if Path(socket_path).exists():
        Path(socket_path).unlink()
    server = UnixHTTPServer(socket_path, Handler)
    os.chmod(socket_path, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Verify it compiles and imports cleanly**

Run: `cd src && python3 -m py_compile serve.py api.py generator.py seed.py`
Expected: no output, exit code 0

- [ ] **Step 3: Manual smoke test over a real Unix socket**

```bash
cd src
python3 - <<'EOF'
import threading, socket, json, tempfile, os
os.environ["SKILL_LIBRARY_SOCKET"] = tempfile.mktemp()
import serve

t = threading.Thread(target=serve.run, args=(os.environ["SKILL_LIBRARY_SOCKET"],), daemon=True)
t.start()
import time; time.sleep(0.3)

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(os.environ["SKILL_LIBRARY_SOCKET"])
body = json.dumps({"action": "list"}).encode()
req = f"POST / HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
s.sendall(req)
print(s.recv(4096))
EOF
```
Expected: response includes `200 OK` and a JSON `{"items": [...]}` body (empty list against a fresh temp state, since `ITEMS_FILE` defaults to `~/.claude/.skill-library/items.json` unless that already has data).

- [ ] **Step 4: Commit**

```bash
git add src/serve.py
git commit -m "feat: add Unix-socket HTTP wiring for the backend"
```

---

### Task 6: regenerate.sh, install.sh, systemd unit, nginx snippet

**Files:**
- Create: `src/regenerate.sh`
- Create: `src/skill-agents-library.service.in`
- Create: `install.sh`
- Create: `src/uninstall.sh`

**Interfaces:**
- Consumes: `src/generator.py build_site`, `src/seed.py import_catalog`,
  `src/serve.py run` (all as CLI entry points via `python3 -m` or direct
  invocation).
- Produces: a running systemd service `skill-agents-library.service`
  listening on `/run/skill-agents-library/sock`, an nginx `location
  /skill-library/` block proxying to it, and a cron entry running
  `regenerate.sh` daily.

- [ ] **Step 1: Write src/regenerate.sh**

```bash
#!/usr/bin/env bash
# regenerate.sh — regenerate the static site if a regen was requested, or
# unconditionally when run from cron. Never invoked by api.py/serve.py
# directly — only reads the request file they drop.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HOME:-/root}"
STATE_DIR="$HOME_DIR/.claude/.skill-library"
ITEMS_FILE="$STATE_DIR/items.json"
REQUEST_FILE="$STATE_DIR/regen.request"
CATALOG_MD="$HOME_DIR/my-Harness-Library/Catalogo-de-Agent-Skills.md"

mode="${1:-cron}"  # cron | force

if [[ "$mode" == "cron" && ! -f "$REQUEST_FILE" ]]; then
  echo "no regen requested, nothing to do"
  exit 0
fi

if [[ -f "$CATALOG_MD" ]]; then
  python3 "$REPO_ROOT/src/seed.py" "$CATALOG_MD" "$ITEMS_FILE" || true
fi

python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT/src')
import generator
generator.build_site(generator.ITEMS_FILE)
"

rm -f "$REQUEST_FILE"
echo "regenerated $(date -u +%FT%TZ)"
```

- [ ] **Step 2: Verify the script's shell syntax**

Run: `bash -n src/regenerate.sh`
Expected: no output

- [ ] **Step 3: Write src/skill-agents-library.service.in**

```ini
[Unit]
Description=My Skill-Agents Library backend
After=network.target

[Service]
Type=simple
User=@RUN_USER@
Group=@RUN_GROUP@
ExecStart=/usr/bin/python3 /opt/skill-agents-library/src/serve.py
Environment=SKILL_LIBRARY_SOCKET=/run/skill-agents-library/sock
RuntimeDirectory=skill-agents-library
RuntimeDirectoryMode=0770

ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=@USER_HOME@/.claude/.skill-library
NoNewPrivileges=yes
PrivateTmp=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM

Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Write src/uninstall.sh**

```bash
#!/usr/bin/env bash
# uninstall.sh — stop and remove the systemd unit, socket dir, and deployed
# code. Never touches ~/.claude/.skill-library (user state is kept).
set -euo pipefail

sudo systemctl stop skill-agents-library.service 2>/dev/null || true
sudo systemctl disable skill-agents-library.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/skill-agents-library.service
sudo systemctl daemon-reload
sudo rm -rf /opt/skill-agents-library
sudo rm -rf /var/www/html/my-skill-agents-library
echo "uninstalled. ~/.claude/.skill-library was left untouched."
```

- [ ] **Step 5: Verify uninstall.sh's shell syntax**

Run: `bash -n src/uninstall.sh`
Expected: no output

- [ ] **Step 6: Write install.sh**

```bash
#!/usr/bin/env bash
# install.sh — curl bootstrap for My Skill-Agents Library.
# Usage: curl -fsSL <raw-url>/install.sh | sudo bash
set -euo pipefail

RUN_USER="${SUDO_USER:-$(whoami)}"
RUN_GROUP="$(id -gn "$RUN_USER")"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO_URL="https://github.com/Epaminondaslage/my-skill-agents-library.git"
TARGET="/opt/skill-agents-library"

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo bash install.sh)" >&2
  exit 1
fi

if [[ -d "$TARGET/.git" ]]; then
  git -C "$TARGET" pull --ff-only
else
  rm -rf "$TARGET"
  git clone --depth 1 "$REPO_URL" "$TARGET"
fi

sudo -u "$RUN_USER" mkdir -p "$USER_HOME/.claude/.skill-library"

sed \
  -e "s/@RUN_USER@/$RUN_USER/" \
  -e "s/@RUN_GROUP@/$RUN_GROUP/" \
  -e "s#@USER_HOME@#$USER_HOME#" \
  "$TARGET/src/skill-agents-library.service.in" \
  > /etc/systemd/system/skill-agents-library.service

systemctl daemon-reload
systemctl enable --now skill-agents-library.service

sudo -u "$RUN_USER" env HOME="$USER_HOME" python3 "$TARGET/src/regenerate.sh" force || true

echo "add an nginx location block proxying /skill-library/ to"
echo "  unix:/run/skill-agents-library/sock  (see docs/nginx-snippet.conf)"
echo "add to crontab: 0 6 * * * bash $TARGET/src/regenerate.sh cron"
```

- [ ] **Step 7: Verify install.sh's shell syntax**

Run: `bash -n install.sh`
Expected: no output

- [ ] **Step 8: Write docs/nginx-snippet.conf (reference, not auto-applied)**

```nginx
location /skill-library/ {
    alias /var/www/html/my-skill-agents-library/;
    try_files $uri $uri/ /skill-library/index.html;
}

location = /skill-library/api {
    proxy_pass http://unix:/run/skill-agents-library/sock:/;
    proxy_set_header Content-Type "application/json";
}
```

- [ ] **Step 9: Commit**

```bash
mkdir -p docs
git add src/regenerate.sh src/skill-agents-library.service.in src/uninstall.sh install.sh docs/nginx-snippet.conf
git commit -m "feat: add deploy scripts, systemd unit, nginx snippet"
```

---

### Task 7: Full verification pass

**Files:** none created — this task only runs checks across everything built.

**Interfaces:** none.

- [ ] **Step 1: Syntax-check every script**

```bash
bash -n install.sh src/regenerate.sh src/uninstall.sh
```
Expected: no output

- [ ] **Step 2: Compile every Python module**

```bash
python3 -m py_compile src/*.py
```
Expected: no output

- [ ] **Step 3: Run the full test suite**

```bash
python3 -m unittest discover -s tests -v
```
Expected: all tests across `test_seed.py`, `test_generator.py`,
`test_api.py` PASS (21 tests total)

- [ ] **Step 4: Run generator.py against a fake ~/.claude and check app.js**

```bash
python3 - <<'EOF'
import sys, tempfile, json
sys.path.insert(0, "src")
import generator, seed
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    items_path = Path(tmp) / "items.json"
    items_path.write_text(json.dumps([{
        "id": "1", "name": "grill-me", "repo": "mattpocock/skills",
        "stars": "242,8K", "function": "Validar arquitetura",
        "dev_note": "10/10", "status": "candidata", "personal_note": "",
        "decided_at": None, "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }]), encoding="utf-8")
    out = Path(tmp) / "site"
    generator.build_site(items_path, out)
    assert (out / "index.html").exists()
    assert (out / "app.js").exists()
    assert (out / "styles.css").exists()
    print("build_site OK ->", out)
EOF
node --check <(python3 -c "import sys; sys.path.insert(0,'src'); import generator; print(generator.render_app_js())")
```
Expected: `build_site OK -> ...` printed, `node --check` produces no output

- [ ] **Step 5: Commit any fixes found during verification**

```bash
git add -A
git commit -m "chore: verification pass" --allow-empty
```

---

## After this plan

Not covered here (explicitly out of scope per the spec, or deferred):
- Actually creating the GitHub repo `Epaminondaslage/my-skill-agents-library`
  and pushing — the user does this manually and will ask to have the local
  working directory changed once it's cloned.
- Running `install.sh` on the real server — a separate, explicit deploy step
  once the repo exists on GitHub, same as my-Harness-Library's release flow.
- Wiring the real nginx config on the server (the snippet in Task 6 is a
  reference to paste in, not auto-applied).
