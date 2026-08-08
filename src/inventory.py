#!/usr/bin/env python3
# =============================================================================
# claude_inventory.py  (v2)
# -----------------------------------------------------------------------------
# Aplicacao : Inventario do ambiente Claude Code (estilo "Harness Library")
# Autor     : Epaminondas Lage
# Finalidade: Varrer o diretorio ~/.claude/ e o arquivo ~/.claude.json para
#             descobrir todos os recursos instalados no Claude Code e gerar
#             uma pagina HTML estatica (portal SPS) descrevendo o que cada
#             um faz — agora com deteccao do repositorio GitHub de origem.
#
# Recursos inventariados:
#   1. Skills    -> ~/.claude/skills/<nome>/SKILL.md   (frontmatter YAML)
#   2. Agents    -> ~/.claude/agents/*.md              (frontmatter YAML)
#   3. Commands  -> ~/.claude/commands/**/*.md         (frontmatter opcional)
#   4. Plugins   -> ~/.claude/plugins/**               (plugin.json)
#   5. MCPs      -> ~/.claude.json                     (mcpServers global/projeto)
#
# Deteccao de repositorio GitHub (em camadas, da mais confiavel para a menos):
#   a) OFFLINE - git remote : sobe do diretorio do recurso ate encontrar
#                             .git/config e extrai a URL do remote "origin".
#   b) OFFLINE - manifests  : campos "repository" / "homepage" do plugin.json
#                             e chaves "source"/"repository" do frontmatter.
#   c) ONLINE  - npm        : para MCPs executados via "npx <pacote>",
#                             consulta https://registry.npmjs.org/<pacote>
#                             e le o campo "repository.url".   [--online]
#   d) ONLINE  - GitHub API : busca "nome + claude skill" em
#                             api.github.com/search/repositories. [--online]
#                             Suporta GITHUB_TOKEN (env) para maior rate limit.
#                             Resultados de busca sao marcados como "provavel".
#
# Cache: consultas online sao gravadas em .claude_inventory_cache.json no
# diretorio atual, evitando repetir chamadas em execucoes futuras.
#
# Saida:
#   ./claude_inventory_site/index.html   (pagina principal)
#   ./claude_inventory_site/styles.css   (CSS separado - padrao SPS TIPO 2)
#   ./claude_inventory_site/app.js       (busca e filtros)
#
# Uso:
#   python3 claude_inventory.py                    # varre ~/.claude (offline)
#   python3 claude_inventory.py --online           # + npm registry + GitHub API
#   python3 claude_inventory.py /outro/dir --online
#   GITHUB_TOKEN=ghp_xxx python3 claude_inventory.py --online
#
# Sem dependencias externas: apenas biblioteca padrao do Python (urllib).
# =============================================================================

import json
import os
import re
import sys
import html
import time
import getpass
import socket
import configparser
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# -----------------------------------------------------------------------------
# Argumentos e configuracao de caminhos
# -----------------------------------------------------------------------------
args = [a for a in sys.argv[1:]]
ONLINE = "--online" in args                          # habilita consultas de rede
paths = [a for a in args if not a.startswith("--")]

BASE = Path(paths[0]).expanduser() if paths else Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"           # MCPs globais e por projeto
OUT_DIR = Path.cwd() / "claude_inventory_site"       # diretorio de saida do site
CACHE_FILE = Path.cwd() / ".claude_inventory_cache.json"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")    # opcional (rate limit maior)


# -----------------------------------------------------------------------------
# Cache de consultas online (npm / GitHub)
# -----------------------------------------------------------------------------
def load_cache() -> dict:
    """Carrega o cache de consultas online, se existir."""
    if CACHE_FILE.is_file():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_cache(cache: dict) -> None:
    """Persiste o cache de consultas online em disco."""
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass


CACHE = load_cache()


def http_get_json(url: str, headers: dict = None, timeout: int = 10):
    """
    GET simples retornando JSON (ou None em caso de erro).
    Usado nas consultas ao registry do npm e a API do GitHub.
    """
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Normalizacao e deteccao de repositorio GitHub
# -----------------------------------------------------------------------------
def normalize_github_url(url: str) -> str:
    """
    Converte formatos comuns de URL git para https://github.com/owner/repo:
      git@github.com:owner/repo.git
      git+https://github.com/owner/repo.git
      https://github.com/owner/repo.git
    Retorna string vazia se a URL nao for do GitHub.
    """
    if not url:
        return ""
    url = url.strip()
    url = re.sub(r"^git\+", "", url)                       # git+https -> https
    m = re.match(r"git@github\.com:(.+?)(\.git)?$", url)   # formato SSH
    if m:
        return f"https://github.com/{m.group(1)}"
    m = re.match(r"https?://github\.com/([^/]+/[^/#?]+)", url)
    if m:
        return f"https://github.com/{m.group(1).removesuffix('.git')}"
    return ""


def repo_from_git_config(start: Path) -> str:
    """
    Camada (a): sobe na arvore de diretorios a partir de 'start' procurando
    .git/config e extrai a URL do remote "origin". Cobre plugins clonados de
    marketplaces e skills mantidas em repositorios versionados.
    """
    d = start if start.is_dir() else start.parent
    for _ in range(8):                       # limite de subida na arvore
        gitcfg = d / ".git" / "config"
        if gitcfg.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(gitcfg, encoding="utf-8")
                url = cp.get('remote "origin"', "url", fallback="")
                return normalize_github_url(url)
            except configparser.Error:
                return ""
        if d.parent == d:                    # chegou na raiz do filesystem
            break
        d = d.parent
    return ""


def load_marketplace_repos() -> dict:
    """
    Camada (b2) — OFFLINE e exata: ~/.claude/plugins/known_marketplaces.json
    guarda o repositorio de origem de cada marketplace instalado. Cobre os
    recursos cujo diretorio nao tem .git (a maioria: o Claude Code copia a
    arvore para cache/ sem os metadados do git), sem precisar adivinhar.
    """
    f = BASE / "plugins" / "known_marketplaces.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}

    out = {}
    for name, info in data.items():
        src = (info or {}).get("source") or {}
        repo = src.get("repo", "")
        if src.get("source") == "github" and repo:
            out[name] = f"https://github.com/{repo}"
    return out


MARKETPLACE_REPOS = {}          # preenchido no main(), depois de BASE resolvido


def repo_from_marketplace(path: str) -> str:
    """Repositorio do marketplace dono do caminho, se o caminho for de plugin."""
    if not path or not MARKETPLACE_REPOS:
        return ""
    root = BASE / "plugins"
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return ""
    # plugins/<marketplaces|cache>/<marketplace>/...
    if len(parts) >= 2 and parts[0] in ("marketplaces", "cache"):
        return MARKETPLACE_REPOS.get(parts[1], "")
    return ""


def repo_from_npm(package: str) -> str:
    """
    Camada (c): consulta o registry do npm para pacotes de MCPs executados
    via npx e extrai o campo repository.url. Resultado e cacheado.
    """
    key = f"npm:{package}"
    if key in CACHE:
        return CACHE[key]
    repo = ""
    if ONLINE:
        data = http_get_json(f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@/')}")
        if data:
            rep = data.get("repository")
            if isinstance(rep, dict):
                repo = normalize_github_url(rep.get("url", ""))
            elif isinstance(rep, str):
                repo = normalize_github_url(rep)
        CACHE[key] = repo
    return repo


def repo_from_github_search(name: str, kind: str) -> str:
    """
    Camada (d): busca o nome do recurso na API de busca do GitHub.
    Retorna o primeiro resultado apenas se o nome do repositorio ou o
    texto casarem bem com o nome buscado (evita falsos positivos).
    O link e marcado como "provavel" pelo chamador. Resultado cacheado.
    """
    key = f"gh:{kind}:{name}"
    if key in CACHE:
        return CACHE[key]
    repo = ""
    if ONLINE:
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "claude-inventory"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        q = urllib.parse.quote(f"{name} claude {kind}")
        data = http_get_json(
            f"https://api.github.com/search/repositories?q={q}&per_page=3",
            headers=headers)
        if data and data.get("items"):
            wanted = name.lower().lstrip("/").replace(":", "-")
            for item in data["items"]:
                rname = item.get("name", "").lower()
                # Aceita apenas se o nome do repo contem o nome do recurso
                # (ou vice-versa), reduzindo resultados irrelevantes.
                if wanted in rname or rname in wanted:
                    repo = item.get("html_url", "")
                    break
        CACHE[key] = repo
        # Rate limit da busca sem token: 10 req/min -> pausa preventiva
        time.sleep(2 if not GITHUB_TOKEN else 0.5)
    return repo


def resolve_repo(item: dict) -> None:
    """
    Aplica as camadas de deteccao na ordem de confiabilidade e preenche:
      item["repo"]       -> URL https://github.com/owner/repo (ou "")
      item["repo_guess"] -> True quando veio de busca (camada d, "provavel")
    """
    item["repo"] = item.get("repo", "")
    item["repo_guess"] = False

    # (a) git remote do diretorio do recurso
    if not item["repo"] and item.get("path"):
        item["repo"] = repo_from_git_config(Path(item["path"]))

    # (b2) marketplace declarado em known_marketplaces.json — exato e offline
    if not item["repo"] and item.get("path"):
        item["repo"] = repo_from_marketplace(item["path"])

    # (c) npm registry para MCPs "npx <pacote>"
    if not item["repo"] and item["type"] == "mcp" and item.get("npm_pkg"):
        item["repo"] = repo_from_npm(item["npm_pkg"])

    # (d) busca na API do GitHub (apenas skills e plugins, nomes mais unicos)
    if not item["repo"] and ONLINE and item["type"] in ("skill", "plugin"):
        found = repo_from_github_search(item["name"], item["type"])
        if found:
            item["repo"] = found
            item["repo_guess"] = True


# -----------------------------------------------------------------------------
# Mini-parser de frontmatter YAML
# -----------------------------------------------------------------------------
def parse_frontmatter(text: str) -> dict:
    """
    Extrai o bloco YAML entre as duas primeiras linhas '---' de um arquivo .md.
    Suporta:
      - chave: valor            (valor simples, com ou sem aspas)
      - chave: >- / > / |       (valor multilinha nas linhas indentadas seguintes)
    Retorna dicionario {chave: valor}. Se nao houver frontmatter, retorna {}.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not m:
        return {}

    data = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        km = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            # Valor multilinha (>- , > , |): agrega linhas indentadas seguintes
            if val in (">", ">-", "|", "|-", ""):
                buf = []
                j = i + 1
                while j < len(lines) and (lines[j].startswith("  ") or lines[j].strip() == ""):
                    buf.append(lines[j].strip())
                    j += 1
                if buf:
                    data[key] = " ".join(b for b in buf if b)
                    i = j
                    continue
                data[key] = val
            else:
                data[key] = val.strip("\"'")
        i += 1
    return data


def first_paragraph(text: str) -> str:
    """
    Fallback: quando o .md nao possui frontmatter com 'description',
    usa o primeiro paragrafo de texto (ignorando titulos '#') como descricao.
    """
    body = re.sub(r"^---\s*\n.*?\n---\s*\n?", "", text, flags=re.DOTALL)
    for block in body.split("\n\n"):
        block = block.strip()
        if block and not block.startswith("#"):
            return re.sub(r"\s+", " ", block)[:300]
    return ""


# -----------------------------------------------------------------------------
# Coletores por tipo de recurso
# -----------------------------------------------------------------------------
def collect_skills() -> list:
    """Varre ~/.claude/skills/<nome>/SKILL.md e retorna lista de skills."""
    items = []
    root = BASE / "skills"
    if not root.is_dir():
        return items
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        items.append({
            "type": "skill",
            "name": fm.get("name", skill_md.parent.name),
            "desc": fm.get("description") or first_paragraph(text) or "Sem descricao.",
            "meta": str(skill_md.parent.relative_to(BASE)),
            "path": str(skill_md.parent),
            # Caminho relativo a BASE — chave usada pelo editor (api.php)
            "rel": str(skill_md.relative_to(BASE)),
            "scope": "meu",
            "origin": "",
            "cat_declared": fm.get("category", ""),
            # Camada (b): frontmatter pode declarar a origem diretamente
            "repo": normalize_github_url(fm.get("repository") or fm.get("source") or ""),
        })
    return items


def collect_agents() -> list:
    """Varre ~/.claude/agents/*.md (subagentes personalizados)."""
    items = []
    root = BASE / "agents"
    if not root.is_dir():
        return items
    for md in sorted(root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        extra = []
        if fm.get("model"):
            extra.append(f"modelo: {fm['model']}")
        if fm.get("tools"):
            extra.append(f"tools: {fm['tools']}")
        items.append({
            "type": "agent",
            "name": fm.get("name", md.stem),
            "desc": fm.get("description") or first_paragraph(text) or "Sem descricao.",
            "meta": " · ".join(extra) if extra else str(md.relative_to(BASE)),
            "path": str(md),
            "rel": str(md.relative_to(BASE)),
            "scope": "meu",
            "origin": "",
            "cat_declared": fm.get("category", ""),
            "repo": normalize_github_url(fm.get("repository") or fm.get("source") or ""),
        })
    return items


def collect_commands() -> list:
    """Varre ~/.claude/commands/**/*.md (slash commands personalizados)."""
    items = []
    root = BASE / "commands"
    if not root.is_dir():
        return items
    for md in sorted(root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        # O nome do comando e o caminho relativo sem extensao: sub/cmd -> /sub:cmd
        rel = md.relative_to(root).with_suffix("")
        cmd_name = "/" + ":".join(rel.parts)
        items.append({
            "type": "command",
            "name": cmd_name,
            "desc": fm.get("description") or first_paragraph(text) or "Sem descricao.",
            "meta": str(md.relative_to(BASE)),
            "path": str(md),
            "rel": str(md.relative_to(BASE)),
            "scope": "meu",
            "origin": "",
            "cat_declared": fm.get("category", ""),
            "repo": normalize_github_url(fm.get("repository") or fm.get("source") or ""),
        })
    return items


def collect_plugins() -> list:
    """
    Varre ~/.claude/plugins em busca de manifests (plugin.json) de plugins
    instalados via marketplace. Le tambem "repository"/"homepage" do manifest.
    """
    items = []
    root = BASE / "plugins"
    if not root.is_dir():
        return items
    seen = set()
    for pj in sorted(root.rglob("plugin.json")):
        try:
            data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name", pj.parent.name)
        if name in seen:
            continue
        seen.add(name)

        # Camada (b): manifest do plugin pode declarar o repositorio
        rep = data.get("repository")
        if isinstance(rep, dict):
            rep = rep.get("url", "")
        repo = normalize_github_url(rep or data.get("homepage", ""))

        author = data.get("author")
        author = author.get("name", "") if isinstance(author, dict) else (author or "")
        items.append({
            "type": "plugin",
            "name": name,
            "desc": data.get("description", "Sem descricao."),
            "meta": f"versao: {data.get('version', '?')} · {author}".strip(" ·"),
            "path": str(pj.parent),
            "scope": "pacote",
            "origin": "",
            "cat_declared": data.get("category", ""),
            "repo": repo,
        })
    return items


# -----------------------------------------------------------------------------
# Recursos DENTRO dos plugins e dos projetos
# -----------------------------------------------------------------------------
# O inventario original listava o pacote ("superpowers") mas nao o que ele
# traz dentro — e e justamente a capacidade individual que se procura. Aqui
# varremos os mesmos tres tipos (skills, agents, commands) em dois escopos
# adicionais:
#
#   escopo "plugin"  -> ~/.claude/plugins/**   (clone de marketplace)
#   escopo "projeto" -> <raiz>/.claude/**      (versionado em git no repo)
#
# Nenhum dos dois e editavel pela pagina: plugin e sobrescrito na proxima
# atualizacao do marketplace, e projeto sujaria a arvore do repositorio.
# A allowlist do api.php ja barra ambos por construcao.

# Projetos varridos. /opt/helpdesk fica de fora de proposito: e projeto de
# outra pessoa, e a regra da casa e nao tocar nele — nem para listar.
PROJECT_ROOTS = sorted(
    p for p in Path("/opt").glob("*/.claude")
    if p.is_dir() and p.parent.name != "helpdesk"
)


def plugin_owner(path: Path, plugins_root: Path) -> str:
    """
    Nome do plugin dono de um arquivo: sobe a arvore ate achar
    .claude-plugin/plugin.json. Sem manifest, cai no primeiro diretorio
    abaixo de marketplaces/ (o nome do marketplace).
    """
    d = path.parent
    while d != plugins_root and d.parent != d:
        manifest = d / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                return json.loads(manifest.read_text(encoding="utf-8",
                                                     errors="replace")).get("name", d.name)
            except (OSError, json.JSONDecodeError):
                return d.name
        d = d.parent
    try:
        rel = path.relative_to(plugins_root).parts
        return rel[1] if len(rel) > 1 else rel[0]
    except ValueError:
        return "?"


def md_item(md: Path, kind: str, scope: str, origin: str, root: Path) -> dict:
    """Monta um item a partir de um .md de skill, agent ou command."""
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fm = parse_frontmatter(text)

    if kind == "skill":
        name = fm.get("name", md.parent.name)
    elif kind == "command":
        # /sub:cmd a partir do caminho relativo dentro de commands/
        try:
            rel = md.relative_to(root).with_suffix("")
            name = "/" + ":".join(rel.parts)
        except ValueError:
            name = "/" + md.stem
    else:
        name = fm.get("name", md.stem)

    return {
        "type": kind,
        "name": name,
        "desc": fm.get("description") or first_paragraph(text) or "Sem descricao.",
        "meta": str(md.parent if kind == "skill" else md),
        "path": str(md.parent if kind == "skill" else md),
        "scope": scope,
        "origin": origin,
        "cat_declared": fm.get("category", ""),
        "repo": normalize_github_url(fm.get("repository") or fm.get("source") or ""),
    }


def collect_from_claude_dir(base: Path, scope: str, origin: str) -> list:
    """
    Varre skills/, agents/ e commands/ de um diretorio no formato .claude.
    Usada tanto para plugins quanto para projetos.
    """
    items = []
    for md in sorted((base / "skills").glob("*/SKILL.md")):
        it = md_item(md, "skill", scope, origin, base / "skills")
        if it:
            items.append(it)
    for md in sorted((base / "agents").rglob("*.md")):
        it = md_item(md, "agent", scope, origin, base / "agents")
        if it:
            items.append(it)
    for md in sorted((base / "commands").rglob("*.md")):
        it = md_item(md, "command", scope, origin, base / "commands")
        if it:
            items.append(it)
    return items


def collect_plugin_resources() -> list:
    """
    Skills, agents e commands que moram dentro dos plugins instalados.
    Varre por arquivo (nao por diretorio .claude) porque o layout do
    marketplace varia: alguns plugins expoem skills/ na raiz, outros
    dentro de plugins/<nome>/.
    """
    root = BASE / "plugins"
    if not root.is_dir():
        return []

    items = []
    seen = set()

    def add(md: Path, kind: str, anchor: Path) -> None:
        key = str(md.resolve())
        if key in seen:
            return
        seen.add(key)
        it = md_item(md, kind, "plugin", plugin_owner(md, root), anchor)
        if it:
            items.append(it)

    for md in sorted(root.rglob("SKILL.md")):
        add(md, "skill", md.parent.parent)
    for md in sorted(root.rglob("*.md")):
        parts = md.parts
        if "/agents/" in str(md):
            add(md, "agent", md.parent)
        elif "/commands/" in str(md):
            # ancora = o diretorio commands/ mais proximo, para o nome /sub:cmd
            anchor = md.parent
            while anchor.name != "commands" and anchor.parent != anchor:
                anchor = anchor.parent
            add(md, "command", anchor)
    return items


def collect_project_resources() -> list:
    """Skills, agents e commands versionados nos repositorios de projeto."""
    items = []
    for claude_dir in PROJECT_ROOTS:
        items += collect_from_claude_dir(claude_dir, "projeto", claude_dir.parent.name)
    return items


def collect_mcps() -> list:
    """
    Le ~/.claude.json e extrai os servidores MCP:
      - "mcpServers" no nivel raiz  -> escopo global (user)
      - "projects": {caminho: {"mcpServers": {...}}} -> escopo por projeto
    Para MCPs executados via "npx <pacote>", guarda o nome do pacote npm
    para posterior resolucao do repositorio (camada c).
    """
    items = []
    if not CLAUDE_JSON.is_file():
        return items
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return items

    def make(name: str, cfg: dict, scope: str) -> dict:
        """Monta o item MCP com descricao curta e pacote npm (se houver)."""
        npm_pkg = ""
        if cfg.get("url"):
            desc = f"{cfg.get('type', 'http')} → {cfg['url']}"
        else:
            argv = [cfg.get("command", "")] + list(cfg.get("args", []))
            desc = "stdio → " + " ".join(a for a in argv if a)
            # Identifica o pacote npm em chamadas "npx [-y] <pacote> ..."
            if cfg.get("command") == "npx":
                for a in cfg.get("args", []):
                    if not a.startswith("-"):
                        npm_pkg = a
                        break
        return {"type": "mcp", "name": name, "desc": desc or "Configuracao nao reconhecida.",
                "meta": scope, "path": "", "npm_pkg": npm_pkg, "repo": "",
                "scope": "pacote", "origin": ""}

    for name, cfg in (data.get("mcpServers") or {}).items():
        items.append(make(name, cfg, "escopo: global (user)"))

    for proj_path, proj in (data.get("projects") or {}).items():
        for name, cfg in (proj.get("mcpServers") or {}).items():
            items.append(make(name, cfg, f"escopo: projeto · {proj_path}"))
    return items


# -----------------------------------------------------------------------------
# Geracao do site (index.html + styles.css + app.js)
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Classificacao por finalidade (badge de categoria)
# -----------------------------------------------------------------------------
# Precedencia, da mais forte para a mais fraca:
#   1. campo "category" no frontmatter (.md) ou no plugin.json  -> decisao manual
#   2. CATEGORY_MAP  -> nome exato, para o acervo ja conhecido
#   3. CATEGORY_RULES -> palavras-chave, pega itens novos automaticamente
#   4. "outros"      -> nao casou com nada
# Manual sempre vence: para corrigir um item, basta escrever
#   category: devops
# no frontmatter — o editor da propria pagina permite isso.

CATEGORY_LABEL = {
    "geral":       "Uso Geral",
    "devops":      "DevOps",
    "spec-ops":    "Spec-Driven Ops",
    "qualidade":   "Qualidade",
    "seguranca":   "Segurança",
    "integracoes": "Integrações",
    "ferramental": "Ferramental",
    "frontend":    "Frontend",
    "outros":      "Outros",
}

# Acervo conhecido: nome exato -> categoria.
CATEGORY_MAP = {
    # Uso geral
    "brainstorm": "geral", "docs": "geral", "caveman": "geral",
    "math-olympiad": "geral", "cv-linkedin": "geral",
    "learning-output-style": "geral", "explanatory-output-style": "geral",
    # DevOps
    "deploy-prod": "devops", "terraform": "devops", "firebase": "devops",
    "mcp-tunnels": "devops", "cost-tracker": "devops",
    # Spec-driven ops
    "superpowers": "spec-ops", "tarefa-finalizada": "spec-ops",
    "workflow": "spec-ops", "ralph-loop": "spec-ops", "task-agent": "spec-ops",
    "scaffold": "spec-ops", "feature-dev": "spec-ops",
    "auto-board-task": "spec-ops", "memplan": "spec-ops",
    # Qualidade
    "code-review": "qualidade", "pr-review-toolkit": "qualidade",
    "code-audit": "qualidade", "code-simplifier": "qualidade",
    "code-modernization": "qualidade", "greptile": "qualidade",
    "index-audit": "qualidade", "site-audit": "qualidade",
    # Seguranca
    "claude-security": "seguranca", "security-review": "seguranca",
    "security-guidance": "seguranca", "dependency-audit": "seguranca",
    # Integracoes
    "github": "integracoes", "gitlab": "integracoes", "asana": "integracoes",
    "linear": "integracoes", "discord": "integracoes", "telegram": "integracoes",
    "imessage": "integracoes", "playwright": "integracoes",
    "firecrawl": "integracoes", "context7": "integracoes",
    "serena": "integracoes", "laravel-boost": "integracoes",
    "commit-commands": "integracoes",
    # Ferramental do proprio Claude
    "plugin-dev": "ferramental", "skill-creator": "ferramental",
    "hookify": "ferramental", "mcp-server-dev": "ferramental",
    "agent-sdk-dev": "ferramental", "claude-code-setup": "ferramental",
    "claude-md-management": "ferramental", "claude-mem": "ferramental",
    "find-skills": "ferramental", "cwc-makers": "ferramental",
    "example-plugin": "ferramental", "playground": "ferramental",
    "fakechat": "ferramental",
    # Frontend
    "frontend-design": "frontend", "project-artifact": "frontend",
    # Famílias que so aparecem como recurso interno de plugin
    "cavecrew": "ferramental", "mem-search": "ferramental",
    "smart-explore": "ferramental", "timeline-report": "ferramental",
    "what-the": "ferramental", "ralph": "spec-ops",
    "using-git-worktrees": "devops", "clean_gone": "devops",
    "verification-before-completion": "qualidade",
    "find-dead-code": "qualidade", "legacy-analyst": "qualidade",
    "anti-pattern-czar": "qualidade", "grader": "qualidade",
    "analyzer": "qualidade",
}

# Heuristica para itens que ainda nao estao no mapa. Ordem importa: a
# primeira regra que casar vence, entao o mais especifico vem antes
# ("security-review" precisa cair em seguranca, nao em qualidade).
CATEGORY_RULES = [
    ("seguranca",   r"secur|vulnerab|owasp|exploit|cve\b|secret|hardening|pentest"),
    ("spec-ops",    r"\bspec|plan(o|ning)?\b|roadmap|workflow|task|backlog|scaffold|tdd|brainstorm|ralph"),
    ("devops",      r"deploy|infra|terraform|docker|kubernet|ansible|ci[/-]?cd|pipeline|tunnel|cron|monitor|observab|cost"),
    ("qualidade",   r"review|audit|lint|refactor|simplif|moderniz|coverage|\btest|verific|valida|dead code|anti-pattern|legacy"),
    ("devops",      r"\bgit\b|worktree|\bcommit|branch|merge|pull request|\bpr\b|rebase"),
    ("integracoes", r"\bmcp server\b|connector|integrac|integrat|\bapi\b|webhook|crawl|scrap|browser"),
    ("ferramental", r"plugin|\bskill|\bhook|agent sdk|marketplace|claude code|slash command|subagent"),
    ("frontend",    r"frontend|front-end|\bui\b|\bux\b|design|css|componente|component|artifact"),
    ("geral",       r"document|explica|explain|learn|ensin|escrit|writing|traduz|chat"),
]


def classify(item: dict, declared: str = "") -> str:
    """
    Decide a categoria de um item seguindo a precedencia descrita acima.
    'declared' e o valor manual vindo do frontmatter/manifest, se houver.
    """
    if declared:
        slug = declared.strip().lower().replace(" ", "-")
        # Aceita tanto o slug ("spec-ops") quanto o rotulo ("Spec-Driven Ops")
        if slug in CATEGORY_LABEL:
            return slug
        for s, label in CATEGORY_LABEL.items():
            if slug == label.lower().replace(" ", "-"):
                return s

    # Commands chegam como "/caveman-commit": a barra nao faz parte do nome.
    name = item["name"].lstrip("/").lower()

    if name in CATEGORY_MAP:
        return CATEGORY_MAP[name]

    # Heranca por familia: "caveman-compress" segue "caveman", e
    # "cavecrew-investigator" segue "cavecrew". Vence a chave mais longa,
    # para "code-review-x" nao ser capturado por "code".
    for key in sorted(CATEGORY_MAP, key=len, reverse=True):
        if name.startswith(key + "-"):
            return CATEGORY_MAP[key]

    haystack = f"{name} {item.get('desc', '')}".lower()
    for slug, pattern in CATEGORY_RULES:
        if re.search(pattern, haystack):
            return slug
    return "outros"


def host_ip() -> str:
    """
    IP da maquina que gerou o inventario. Abre um socket UDP para um destino
    externo (nada e enviado) so para descobrir qual interface o SO usaria —
    mais confiavel que gethostbyname(hostname), que em maquina com varias
    bridges Docker costuma devolver 127.0.1.1.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


def host_user() -> str:
    """Usuario dono do ambiente Claude inventariado (dono do diretorio BASE)."""
    try:
        import pwd
        return pwd.getpwuid(BASE.stat().st_uid).pw_name
    except (KeyError, OSError, ImportError):
        return getpass.getuser()


TYPE_LABEL = {
    "skill":   "Skill",
    "agent":   "Agent",
    "command": "Command",
    "plugin":  "Plugin",
    "mcp":     "MCP",
}


# -----------------------------------------------------------------------------
# Bilingue (pt-BR / en)
# -----------------------------------------------------------------------------
# A pagina e estatica: em vez de gerar dois arquivos ou depender de chaves e
# de um dicionario paralelo, cada elemento de texto carrega as duas versoes
# em data-pt / data-en. O JS so troca o textContent — sem recarregar, sem
# rota /en, e impossivel a traducao ficar dessincronizada do HTML.

def bi(pt: str, en: str) -> str:
    """Atributos com as duas versoes do texto de um elemento."""
    return f'data-pt="{html.escape(pt, quote=True)}" data-en="{html.escape(en, quote=True)}"'


def bit(pt: str, en: str) -> str:
    """Idem, para o atributo title (tooltip)."""
    return (f'data-pt-title="{html.escape(pt, quote=True)}" '
            f'data-en-title="{html.escape(en, quote=True)}"')


# Rotulo das categorias nos dois idiomas.
CATEGORY_EN = {
    "geral":       "General",
    "devops":      "DevOps",
    "spec-ops":    "Spec-Driven Ops",
    "qualidade":   "Quality",
    "seguranca":   "Security",
    "integracoes": "Integrations",
    "ferramental": "Tooling",
    "frontend":    "Frontend",
    "outros":      "Other",
}

SCOPE_EN = {"meu": "Mine", "plugin": "From plugin", "projeto": "From project", "pacote": "Packages"}


def build_site(items: list) -> None:
    """Gera os tres arquivos estaticos do portal no diretorio de saida."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    counts = {t: sum(1 for i in items if i["type"] == t) for t in TYPE_LABEL}
    with_repo = sum(1 for i in items if i.get("repo"))
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Procedencia do inventario: onde rodou, o que varreu e de quem e o ambiente
    ip_str   = html.escape(host_ip())
    host_str = html.escape(socket.gethostname())
    base_str = html.escape(str(BASE))
    user_str = html.escape(host_user())

    # ---- Cards HTML -----------------------------------------------------
    cards = []
    for it in items:
        # Link do repositorio (quando detectado); busca por API e marcada
        # como "provavel" para deixar clara a menor confiabilidade.
        repo_html = ""
        if it.get("repo"):
            guess = (' <span class="repo-guess" ' + bi("provável", "likely") + '>provável</span>'
                     if it.get("repo_guess") else "")
            repo_html = (f'\n        <a class="repo-link" href="{html.escape(it["repo"])}" '
                         f'target="_blank" rel="noopener">GitHub ↗</a>{guess}')

        # Cards de arquivo .md sob ~/.claude viram clicaveis: abrem o editor.
        # Os demais (plugin de marketplace, MCP) seguem estaticos.
        rel = it.get("rel", "")
        if rel:
            edit_attrs = (f' data-rel="{html.escape(rel, quote=True)}" tabindex="0" role="button"'
                          f' aria-label="Editar {html.escape(it["name"], quote=True)}"')
            edit_cls = " editable"
            # Mesma linha e mesmo formato de pilula do link do GitHub.
            edit_hint = ('\n        <span class="edit-link" '
                         + bi("✎ Editar", "✎ Edit") + '>✎ Editar</span>')
        else:
            edit_attrs, edit_cls, edit_hint = "", "", ""

        # A categoria entra tambem no texto buscavel: procurar por "devops"
        # passa a listar tudo daquela finalidade.
        cat = it.get("cat", "outros")
        cat_label = CATEGORY_LABEL[cat]
        searchable = f"{it['name']} {it['desc']} {cat_label} {CATEGORY_EN[cat]}".lower()

        # Procedencia: de qual plugin ou projeto o recurso veio. Recursos
        # proprios (~/.claude) nao levam chip — a ausencia ja diz que sao seus.
        scope = it.get("scope", "meu")
        origin = it.get("origin", "")
        origin_html = (f'<span class="origin-chip scope-{scope}">{html.escape(origin)}</span>'
                       if origin else "")
        searchable = f"{searchable} {origin}".strip().lower()

        cards.append(f"""
      <article class="card{edit_cls}" data-type="{it['type']}" data-cat="{cat}"
               data-scope="{scope}"{edit_attrs}
               data-search="{html.escape(searchable, quote=True)}">
        <div class="card-head">
          <span class="card-name">{html.escape(it['name'])}{origin_html}</span>
          <span class="badges">
            <span class="badge badge-cat cat-{cat}" {bi(cat_label, CATEGORY_EN[cat])}>{html.escape(cat_label)}</span>
            <span class="badge badge-{it['type']}">{TYPE_LABEL[it['type']]}</span>
          </span>
        </div>
        <p class="card-desc">{html.escape(it['desc'])}</p>
        <p class="card-meta">{html.escape(it['meta'])}{repo_html}{edit_hint}</p>
      </article>""")

    # ---- Abas de filtro por tipo ----------------------------------------
    tabs = [f'<button class="tab active" data-filter="all">'
            f'<span {bi("Todas", "All")}>Todas</span> <span>{len(items)}</span></button>']
    for t, label in TYPE_LABEL.items():
        if counts[t]:
            tabs.append(f'<button class="tab" data-filter="{t}">'
                        f'<span {bi(label + "s", label + "s")}>{label}s</span> '
                        f'<span>{counts[t]}</span></button>')

    # ---- Chips de filtro por escopo (de onde o recurso vem) -------------
    SCOPE_LABEL = {"meu": "Meus", "plugin": "De plugin", "projeto": "De projeto", "pacote": "Pacotes"}
    scope_counts = {s: sum(1 for i in items if i.get("scope", "meu") == s) for s in SCOPE_LABEL}
    scopes = [f'<button class="chip active" data-scope="all">'
              f'<span {bi("Todos", "All")}>Todos</span></button>']
    for s, label in SCOPE_LABEL.items():
        if scope_counts[s]:
            scopes.append(f'<button class="chip chip-{s}" data-scope="{s}">'
                          f'<span {bi(label, SCOPE_EN[s])}>{label}</span> '
                          f'<span>{scope_counts[s]}</span></button>')

    # ---- Chips de filtro por categoria ----------------------------------
    cat_counts = {c: sum(1 for i in items if i.get("cat") == c) for c in CATEGORY_LABEL}
    cat_chips = [f'<button class="chip active" data-catf="all">'
                 f'<span {bi("Todas", "All")}>Todas</span></button>']
    for c, label in CATEGORY_LABEL.items():
        if cat_counts[c]:
            cat_chips.append(f'<button class="chip cat-{c}" data-catf="{c}">'
                             f'<span {bi(label, CATEGORY_EN[c])}>{label}</span> '
                             f'<span>{cat_counts[c]}</span></button>')

    index_html = f"""<!DOCTYPE html>
<!-- =====================================================================
     Claude Code - Inventario de Recursos (skills, agents, commands,
     plugins e MCPs) com links para os repositorios GitHub de origem.
     Pagina estatica gerada por claude_inventory.py (v2).
     Padrao visual: SPS Design System (TIPO 2).
     Gerado em: {generated}
====================================================================== -->
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>My Harness Library</title>
  <!-- Favicon embutido: evita o 404 de /favicon.ico em toda carga de pagina
       e nao adiciona arquivo nenhum ao webroot. -->
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230d9488'/%3E%3Cpath d='M9 10h14M9 16h14M9 22h9' stroke='white' stroke-width='2.6' stroke-linecap='round'/%3E%3C/svg%3E">
  <!-- Aplica o tema salvo ANTES do CSS pintar, evitando flash de tema errado. -->
  <script>
    (function () {{
      try {{
        var t = localStorage.getItem('inv-theme');
        if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t;
      }} catch (e) {{}}   /* localStorage bloqueado: cai no prefers-color-scheme */
    }})();
  </script>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="topbar">
    <h1>My Harness Library</h1>
    <div class="topbar-right">
      <span class="topbar-info"
            {bi(f"{len(items)} recursos · {with_repo} com repositório · gerado em {generated}",
                f"{len(items)} resources · {with_repo} with repository · generated {generated}")}>
        {len(items)} recursos · {with_repo} com repositório · gerado em {generated}</span>
      <button id="lang-btn" class="icon-btn" type="button"
              {bit("Switch to English", "Mudar para português")}
              title="Switch to English"><span id="lang-flag">🇺🇸</span></button>
      <button id="pw-btn" class="icon-btn" type="button"
              {bit("Trocar a senha de gravação", "Change the write password")}
              title="Trocar a senha de gravação">🔑</button>
      <button id="regen-btn" class="icon-btn" type="button"
              {bit("Regenerar o inventário agora", "Regenerate the inventory now")}
              title="Regenerar o inventário agora">↻</button>
      <button id="new-btn" class="new-btn" type="button"
              {bit("Criar skill, agent ou command", "Create skill, agent or command")}
              title="Criar skill, agent ou command"><span {bi("＋ Novo", "＋ New")}>＋ Novo</span></button>
      <button id="theme-toggle" class="theme-toggle" type="button"
              aria-label="Alternar tema claro/escuro"
              {bit("Alternar tema claro/escuro", "Toggle light/dark theme")}
              title="Alternar tema claro/escuro">
        <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
        <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4"/>
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>
        </svg>
      </button>
    </div>
  </header>

  <main class="container">
    <!-- Procedencia: de qual maquina, diretorio e usuario veio este inventario -->
    <section class="origin">
      <h2 class="origin-title">My Harness Library</h2>
      <dl class="origin-list">
        <div><dt {bi("Host", "Host")}>Host</dt><dd>{ip_str} <span class="origin-alt">({host_str})</span></dd></div>
        <div><dt {bi("Diretório", "Directory")}>Diretório</dt><dd>{base_str}</dd></div>
        <div><dt {bi("Usuário", "User")}>Usuário</dt><dd>{user_str}</dd></div>
      </dl>
    </section>

    <input id="search" class="search" type="text"
           data-pt-ph="Buscar por nome ou descrição..."
           data-en-ph="Search by name or description..."
           placeholder="Buscar por nome ou descrição...">

    <nav class="tabs">{''.join(tabs)}</nav>

    <div class="filters">
      <div class="filter-row">
        <span class="filter-label" {bi("Origem", "Source")}>Origem</span>
        <div class="chips">{''.join(scopes)}</div>
      </div>
      <div class="filter-row">
        <span class="filter-label" {bi("Finalidade", "Purpose")}>Finalidade</span>
        <div class="chips">{''.join(cat_chips)}</div>
      </div>
    </div>

    <section id="grid" class="grid">{''.join(cards)}
    </section>

    <p id="empty" class="empty" hidden
       {bi("Nenhum recurso encontrado para este filtro.", "No resources match this filter.")}>
       Nenhum recurso encontrado para este filtro.</p>
  </main>

  <!-- ===================================================================
       Editor de markdown (modal). Abre ao clicar num card de arquivo .md
       sob ~/.claude. Grava via api.php, que exige senha.
  ==================================================================== -->
  <div id="editor" class="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="ed-title">
      <header class="modal-head">
        <div>
          <h3 id="ed-title">—</h3>
          <span id="ed-path" class="modal-path"></span>
        </div>
        <button id="ed-close" class="icon-btn" type="button" aria-label="Fechar">✕</button>
      </header>

      <div class="modal-tools">
        <button class="tool" data-md="bold"   type="button" title="Negrito (Ctrl+B)"><b>B</b></button>
        <button class="tool" data-md="italic" type="button" title="Itálico (Ctrl+I)"><i>I</i></button>
        <button class="tool" data-md="head"   type="button" title="Título">H</button>
        <button class="tool" data-md="list"   type="button" title="Lista">•</button>
        <button class="tool" data-md="code"   type="button" title="Código">&lt;/&gt;</button>
        <button class="tool" data-md="link"   type="button" title="Link">🔗</button>
        <span class="tool-sep"></span>
        <button id="ed-preview-toggle" class="tool tool-wide" type="button"
                aria-pressed="true" {bit("Mostrar/ocultar preview", "Show/hide preview")}
                title="Mostrar/ocultar preview">◧ Preview</button>
      </div>

      <div class="modal-body">
        <textarea id="ed-text" class="ed-text" spellcheck="false"
                  aria-label="Conteúdo markdown"></textarea>
        <div id="ed-preview" class="ed-preview" aria-live="polite"></div>
        <!-- Painel de revisoes: sobrepoe o preview quando aberto -->
        <aside id="ed-hist" class="ed-hist" hidden>
          <div class="hist-head">
            <strong {bi("Histórico", "History")}>Histórico</strong>
            <button id="hist-close" class="btn btn-sm" type="button" {bi("Voltar", "Back")}>Voltar</button>
          </div>
          <ul id="hist-list" class="hist-list"></ul>
          <div id="hist-diff" class="hist-diff" hidden></div>
        </aside>
      </div>

      <footer class="modal-foot">
        <span id="ed-status" class="ed-status"></span>
        <button id="ed-hist-btn" class="btn btn-sm" type="button"
                {bi("Histórico", "History")}>Histórico</button>
        <input id="ed-pass" class="ed-pass" type="password" autocomplete="current-password"
               data-pt-ph="Senha para salvar" data-en-ph="Password to save"
               placeholder="Senha para salvar">
        <button id="ed-cancel" class="btn" type="button" {bi("Cancelar", "Cancel")}>Cancelar</button>
        <button id="ed-save" class="btn btn-primary" type="button" {bi("Salvar", "Save")}>Salvar</button>
      </footer>
    </div>
  </div>

  <!-- Dialogo de troca de senha -->
  <div id="pwdlg" class="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-box modal-sm" role="dialog" aria-modal="true" aria-labelledby="pw-title">
      <header class="modal-head">
        <h3 id="pw-title" {bi("Trocar senha de gravação", "Change write password")}>Trocar senha de gravação</h3>
        <button id="pw-close" class="icon-btn" type="button" aria-label="Fechar">✕</button>
      </header>
      <div class="nw-body">
        <label class="nw-field">
          <span {bi("Senha atual", "Current password")}>Senha atual</span>
          <input id="pw-old" type="password" autocomplete="current-password">
        </label>
        <label class="nw-field">
          <span {bi("Senha nova", "New password")}>Senha nova</span>
          <input id="pw-new" type="password" autocomplete="new-password">
        </label>
        <label class="nw-field">
          <span {bi("Repita a senha nova", "Repeat new password")}>Repita a senha nova</span>
          <input id="pw-new2" type="password" autocomplete="new-password">
        </label>
        <p class="nw-note" {bi("Mínimo de 8 caracteres. Vale na hora, sem reiniciar nada. Não há recuperação por aqui: perdendo a senha, reescreva o hash no servidor.", "Minimum 8 characters. Takes effect immediately, no restart. No recovery here: if you lose it, rewrite the hash on the server.")}>Mínimo de 8 caracteres. Vale na hora, sem reiniciar nada.
           Não há recuperação por aqui: perdendo a senha, reescreva o hash no servidor.</p>
        <p id="pw-msg" class="nw-err" hidden></p>
      </div>
      <footer class="modal-foot">
        <span class="ed-status"></span>
        <button id="pw-cancel" class="btn" type="button" {bi("Cancelar", "Cancel")}>Cancelar</button>
        <button id="pw-ok" class="btn btn-primary" type="button" {bi("Trocar", "Change")}>Trocar</button>
      </footer>
    </div>
  </div>

  <!-- Dialogo de regeneracao -->
  <div id="regendlg" class="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-box modal-sm" role="dialog" aria-modal="true" aria-labelledby="rg-title">
      <header class="modal-head">
        <h3 id="rg-title" {bi("Regenerar inventário", "Regenerate inventory")}>Regenerar inventário</h3>
        <button id="rg-close" class="icon-btn" type="button" aria-label="Fechar">✕</button>
      </header>
      <div class="nw-body">
        <p class="nw-note" {bi("Refaz a varredura de ~/.claude, dos plugins e dos projetos, e republica a página. O pedido é executado pelo cron em até 60 segundos.", "Rescans ~/.claude, the plugins and the projects, then republishes the page. Cron runs the request within 60 seconds.")}>Refaz a varredura de <code>~/.claude</code>, dos plugins e dos
           projetos, e republica a página. O pedido é executado pelo cron em até 60 segundos.</p>
        <label class="nw-field">
          <span {bi("Senha", "Password")}>Senha</span>
          <input id="rg-pass" type="password" autocomplete="current-password">
        </label>
        <p id="rg-msg" class="nw-note"></p>
      </div>
      <footer class="modal-foot">
        <span class="ed-status"></span>
        <button id="rg-cancel" class="btn" type="button" {bi("Fechar", "Close")}>Fechar</button>
        <button id="rg-ok" class="btn btn-primary" type="button" {bi("Regenerar", "Regenerate")}>Regenerar</button>
      </footer>
    </div>
  </div>

  <!-- Dialogo de criacao: escolhe tipo e nome, depois abre o editor -->
  <div id="newdlg" class="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-box modal-sm" role="dialog" aria-modal="true" aria-labelledby="nw-title">
      <header class="modal-head">
        <h3 id="nw-title" {bi("Novo recurso", "New resource")}>Novo recurso</h3>
        <button id="nw-close" class="icon-btn" type="button" aria-label="Fechar">✕</button>
      </header>
      <div class="nw-body">
        <label class="nw-field">
          <span {bi("Tipo", "Type")}>Tipo</span>
          <select id="nw-kind">
            <option value="skills">Skill</option>
            <option value="agents">Agent</option>
            <option value="commands">Command</option>
          </select>
        </label>
        <label class="nw-field">
          <span {bi("Nome", "Name")}>Nome</span>
          <input id="nw-name" type="text" placeholder="minha-skill" autocomplete="off">
        </label>
        <p class="nw-path"><span {bi("Será criado em", "Will be created at")}>Será criado em</span>
           <code id="nw-preview">skills/minha-skill/SKILL.md</code></p>
        <p class="nw-note" {bi("Minúsculas, números e hífen. O arquivo nasce com o frontmatter obrigatório preenchido — edite antes de salvar.", "Lowercase, digits and hyphen. The file starts with the required frontmatter filled in — edit before saving.")}>Minúsculas, números e hífen. O arquivo nasce com o frontmatter
           obrigatório preenchido — edite antes de salvar.</p>
        <p id="nw-err" class="nw-err" hidden></p>
      </div>
      <footer class="modal-foot">
        <span class="ed-status"></span>
        <button id="nw-cancel" class="btn" type="button" {bi("Cancelar", "Cancel")}>Cancelar</button>
        <button id="nw-ok" class="btn btn-primary" type="button" {bi("Criar e editar", "Create and edit")}>Criar e editar</button>
      </footer>
    </div>
  </div>

  <script src="app.js"></script>
</body>
</html>
"""

    styles_css = """/* =====================================================================
   styles.css - Inventario Claude Code (v2)
   Padrao SPS Design System (TIPO 2): header branco 56px, fundo #f0f0f0,
   cards brancos, acento teal #0d9488, botoes raio 8px.

   Tema claro/escuro por tokens CSS. Tres estados:
     :root                      -> paleta clara (padrao)
     @media prefers-color-scheme -> escuro quando o SO pede e o usuario
                                    nao escolheu (guardado por :not([data-theme="light"]))
     :root[data-theme="dark"]   -> escolha explicita no botao do topo
   Nenhuma cor pode existir SO dentro do media query: o toggle precisa
   vencer nos dois sentidos.
====================================================================== */

:root {
  --bg:          #f0f0f0;
  --fg:          #1f2937;
  --surface:     #ffffff;
  --border:      #e5e7eb;
  --border-str:  #d1d5db;
  --muted:       #6b7280;
  --muted-2:     #9ca3af;
  --strong:      #111827;
  --body-txt:    #374151;
  --accent:      #0d9488;
  --accent-soft: #ccfbf1;
  --shadow:      rgba(0, 0, 0, .06);
  --warn:        #d97706;
  /* badges */
  --b-agent-bg:   #ede9fe; --b-agent-fg:   #7c3aed;
  --b-command-bg: #dbeafe; --b-command-fg: #2563eb;
  --b-plugin-bg:  #ffedd5; --b-plugin-fg:  #ea580c;
  --b-mcp-bg:     #fee2e2; --b-mcp-fg:     #dc2626;
  /* categorias (badge de finalidade) */
  --c-geral-bg:       #e5e7eb; --c-geral-fg:       #4b5563;
  --c-devops-bg:      #dcfce7; --c-devops-fg:      #15803d;
  --c-spec-ops-bg:    #e0e7ff; --c-spec-ops-fg:    #4338ca;
  --c-qualidade-bg:   #cffafe; --c-qualidade-fg:   #0e7490;
  --c-seguranca-bg:   #fee2e2; --c-seguranca-fg:   #b91c1c;
  --c-integracoes-bg: #fef3c7; --c-integracoes-fg: #a16207;
  --c-ferramental-bg: #f3e8ff; --c-ferramental-fg: #7e22ce;
  --c-frontend-bg:    #fce7f3; --c-frontend-fg:    #be185d;
  --c-outros-bg:      #f3f4f6; --c-outros-fg:      #6b7280;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:          #111827;
    --fg:          #e5e7eb;
    --surface:     #1f2937;
    --border:      #374151;
    --border-str:  #4b5563;
    --muted:       #9ca3af;
    --muted-2:     #6b7280;
    --strong:      #f9fafb;
    --body-txt:    #d1d5db;
    --accent:      #2dd4bf;
    --accent-soft: #134e4a;
    --shadow:      rgba(0, 0, 0, .4);
    --warn:        #fbbf24;
    --b-agent-bg:   #3b2a63; --b-agent-fg:   #c4b5fd;
    --b-command-bg: #1e3a5f; --b-command-fg: #93c5fd;
    --b-plugin-bg:  #5c2e0e; --b-plugin-fg:  #fdba74;
    --b-mcp-bg:     #5c1f1f; --b-mcp-fg:     #fca5a5;
    --c-geral-bg:       #374151; --c-geral-fg:       #d1d5db;
    --c-devops-bg:      #14532d; --c-devops-fg:      #86efac;
    --c-spec-ops-bg:    #312e81; --c-spec-ops-fg:    #a5b4fc;
    --c-qualidade-bg:   #164e63; --c-qualidade-fg:   #67e8f9;
    --c-seguranca-bg:   #5c1f1f; --c-seguranca-fg:   #fca5a5;
    --c-integracoes-bg: #4a3410; --c-integracoes-fg: #fcd34d;
    --c-ferramental-bg: #4a1d6b; --c-ferramental-fg: #d8b4fe;
    --c-frontend-bg:    #61123b; --c-frontend-fg:    #f9a8d4;
    --c-outros-bg:      #2d3748; --c-outros-fg:      #9ca3af;
  }
}

:root[data-theme="dark"] {
  --bg:          #111827;
  --fg:          #e5e7eb;
  --surface:     #1f2937;
  --border:      #374151;
  --border-str:  #4b5563;
  --muted:       #9ca3af;
  --muted-2:     #6b7280;
  --strong:      #f9fafb;
  --body-txt:    #d1d5db;
  --accent:      #2dd4bf;
  --accent-soft: #134e4a;
  --shadow:      rgba(0, 0, 0, .4);
  --warn:        #fbbf24;
  --b-agent-bg:   #3b2a63; --b-agent-fg:   #c4b5fd;
  --b-command-bg: #1e3a5f; --b-command-fg: #93c5fd;
  --b-plugin-bg:  #5c2e0e; --b-plugin-fg:  #fdba74;
  --b-mcp-bg:     #5c1f1f; --b-mcp-fg:     #fca5a5;
  --c-geral-bg:       #374151; --c-geral-fg:       #d1d5db;
  --c-devops-bg:      #14532d; --c-devops-fg:      #86efac;
  --c-spec-ops-bg:    #312e81; --c-spec-ops-fg:    #a5b4fc;
  --c-qualidade-bg:   #164e63; --c-qualidade-fg:   #67e8f9;
  --c-seguranca-bg:   #5c1f1f; --c-seguranca-fg:   #fca5a5;
  --c-integracoes-bg: #4a3410; --c-integracoes-fg: #fcd34d;
  --c-ferramental-bg: #4a1d6b; --c-ferramental-fg: #d8b4fe;
  --c-frontend-bg:    #61123b; --c-frontend-fg:    #f9a8d4;
  --c-outros-bg:      #2d3748; --c-outros-fg:      #9ca3af;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: var(--bg);
  color: var(--fg);
}

/* ---- Header fixo de 56px (padrao TIPO 2) ---- */
.topbar {
  height: 56px;
  background: var(--surface);
  border-bottom: 3px solid var(--accent);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: .75rem;
  padding: 0 1.25rem;
  position: sticky;
  top: 0;
  z-index: 10;
}
.topbar h1 { font-size: 1.05rem; color: var(--accent); }
.topbar-right { display: flex; align-items: center; gap: .75rem; }
.topbar-info { font-size: .78rem; color: var(--muted); }

/* ---- Botao de tema (canto superior direito) ---- */
.theme-toggle {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--muted);
  cursor: pointer;
  line-height: 0;
}
.theme-toggle:hover { color: var(--accent); border-color: var(--accent); }
.theme-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.theme-toggle svg { width: 18px; height: 18px; }
/* Mostra o sol no escuro e a lua no claro (o icone indica o destino). */
.icon-sun  { display: none; }
:root[data-theme="dark"] .icon-sun  { display: block; }
:root[data-theme="dark"] .icon-moon { display: none; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .icon-sun  { display: block; }
  :root:not([data-theme="light"]) .icon-moon { display: none; }
}
:root[data-theme="light"] .icon-sun  { display: none; }
:root[data-theme="light"] .icon-moon { display: block; }

@media (max-width: 640px) { .topbar-info { display: none; } }

.container { max-width: 1100px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }

/* ---- Bloco de procedencia (titulo + host/diretorio/usuario) ---- */
.origin {
  background: var(--surface);
  border-radius: 8px;
  border-left: 4px solid var(--accent);
  box-shadow: 0 1px 2px var(--shadow);
  padding: 1rem 1.1rem;
  margin-bottom: 1rem;
}
.origin-title {
  font-size: 1.35rem;
  font-weight: 600;
  color: var(--strong);
  margin-bottom: .7rem;
}
.origin-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: .55rem 1.5rem;
}
.origin-list dt {
  font-size: .68rem;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted-2);
  margin-bottom: .12rem;
}
.origin-list dd {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .85rem;
  color: var(--body-txt);
  word-break: break-all;
}
.origin-alt { color: var(--muted-2); font-size: .78rem; }

/* ---- Campo de busca ---- */
.search {
  width: 100%;
  padding: .7rem 1rem;
  border: 1px solid var(--border-str);
  border-radius: 8px;
  font-size: .95rem;
  margin-bottom: 1rem;
  background: var(--surface);
  color: var(--fg);
}
.search::placeholder { color: var(--muted-2); }
.search:focus { outline: 2px solid var(--accent); border-color: transparent; }

/* ---- Abas de filtro por tipo ---- */
.tabs { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.25rem; }
.tab {
  background: var(--surface);
  color: var(--body-txt);
  padding: .45rem 1rem;
  border-radius: 8px;
  font-size: .85rem;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--border);
}
.tab span { color: var(--muted-2); margin-left: .3rem; font-weight: 400; }
.tab.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.tab.active span { color: var(--bg); opacity: .7; }

/* ---- Chips de filtro (origem e finalidade) ---- */
.filters { margin-bottom: 1.25rem; display: flex; flex-direction: column; gap: .5rem; }
.filter-row { display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; }
.filter-label {
  font-size: .68rem;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted-2);
  min-width: 68px;
}
.chips { display: flex; flex-wrap: wrap; gap: .35rem; }
.chip {
  background: var(--surface);
  color: var(--body-txt);
  border: 1px solid var(--border);
  border-radius: 99px;
  padding: .25rem .7rem;
  font-size: .76rem;
  cursor: pointer;
}
.chip span { color: var(--muted-2); margin-left: .25rem; }
.chip:hover { border-color: var(--accent); }
.chip.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--bg);
  font-weight: 500;
}
.chip.active span { color: var(--bg); opacity: .75; }

/* Chip de origem no titulo do card */
.origin-chip {
  margin-left: .45rem;
  padding: .05rem .45rem;
  border-radius: 99px;
  font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  font-size: .62rem;
  font-weight: 500;
  vertical-align: middle;
  white-space: nowrap;
}
.origin-chip.scope-plugin  { background: var(--c-ferramental-bg); color: var(--c-ferramental-fg); }
.origin-chip.scope-projeto { background: var(--c-devops-bg);      color: var(--c-devops-fg); }

/* ---- Grade de cards brancos ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 1rem;
}
.card {
  background: var(--surface);
  border-radius: 8px;
  padding: 1rem 1.1rem;
  border-left: 4px solid var(--accent);
  box-shadow: 0 1px 2px var(--shadow);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: .5rem;
  margin-bottom: .45rem;
}
.card-name {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .95rem;
  font-weight: 600;
  color: var(--strong);
  word-break: break-word;
}
.card-desc { font-size: .85rem; line-height: 1.45; color: var(--body-txt); }
.card-meta {
  margin-top: .6rem;
  font-size: .72rem;
  color: var(--muted-2);
  word-break: break-all;
}

/* ---- Link do repositorio GitHub ---- */
.repo-link {
  display: inline-block;
  margin-left: .5rem;
  padding: .1rem .55rem;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 99px;
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
}
.repo-link:hover { background: var(--accent); color: var(--bg); }
.repo-guess {                     /* marca resultados vindos de busca (camada d) */
  margin-left: .3rem;
  font-size: .65rem;
  color: var(--warn);
  font-style: italic;
}

/* ---- Badges por tipo de recurso ---- */
.badge {
  flex-shrink: 0;
  font-size: .68rem;
  font-weight: 600;
  padding: .18rem .55rem;
  border-radius: 99px;
}
/* ---- Badge de categoria (finalidade) ---- */
.badges { display: flex; align-items: center; gap: .3rem; flex-shrink: 0; }
.badge-cat { font-weight: 500; }
.cat-geral       { background: var(--c-geral-bg);       color: var(--c-geral-fg); }
.cat-devops      { background: var(--c-devops-bg);      color: var(--c-devops-fg); }
.cat-spec-ops    { background: var(--c-spec-ops-bg);    color: var(--c-spec-ops-fg); }
.cat-qualidade   { background: var(--c-qualidade-bg);   color: var(--c-qualidade-fg); }
.cat-seguranca   { background: var(--c-seguranca-bg);   color: var(--c-seguranca-fg); }
.cat-integracoes { background: var(--c-integracoes-bg); color: var(--c-integracoes-fg); }
.cat-ferramental { background: var(--c-ferramental-bg); color: var(--c-ferramental-fg); }
.cat-frontend    { background: var(--c-frontend-bg);    color: var(--c-frontend-fg); }
.cat-outros      { background: var(--c-outros-bg);      color: var(--c-outros-fg); }

.badge-skill   { background: var(--accent-soft);   color: var(--accent); }
.badge-agent   { background: var(--b-agent-bg);    color: var(--b-agent-fg); }
.badge-command { background: var(--b-command-bg);  color: var(--b-command-fg); }
.badge-plugin  { background: var(--b-plugin-bg);   color: var(--b-plugin-fg); }
.badge-mcp     { background: var(--b-mcp-bg);      color: var(--b-mcp-fg); }

.empty { text-align: center; color: var(--muted); padding: 2rem 0; }

/* ---- Card clicavel (arquivo .md editavel) ---- */
.card.editable { cursor: pointer; transition: box-shadow .12s, transform .12s; }
.card.editable:hover { box-shadow: 0 3px 10px var(--shadow); transform: translateY(-1px); }
.card.editable:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* Pilula "Editar": mesma linha e mesma forma do link do GitHub, sempre
   visivel — sinaliza a acao sem depender de hover (que nao existe no touch). */
.edit-link {
  display: inline-block;
  margin-left: .5rem;
  padding: .1rem .55rem;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 99px;
  font-weight: 500;
  white-space: nowrap;
}
.card.editable:hover .edit-link { background: var(--accent); color: var(--bg); }

/* =====================================================================
   Modal do editor markdown
====================================================================== */
.modal { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; }
.modal[hidden] { display: none; }
.modal-backdrop { position: absolute; inset: 0; background: rgba(0, 0, 0, .55); }

.modal-box {
  position: relative;
  display: flex;
  flex-direction: column;
  width: min(1000px, 94vw);
  height: min(80vh, 800px);
  background: var(--surface);
  color: var(--fg);
  border-radius: 10px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, .35);
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: .8rem 1rem;
  border-bottom: 1px solid var(--border);
}
.modal-head h3 {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .98rem;
  color: var(--strong);
}
.modal-path { font-size: .72rem; color: var(--muted-2); word-break: break-all; }

.icon-btn {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--muted);
  width: 30px; height: 30px;
  cursor: pointer;
  flex-shrink: 0;
}
.icon-btn:hover { color: var(--accent); border-color: var(--accent); }

/* ---- Barra de ferramentas markdown ---- */
.modal-tools {
  display: flex;
  align-items: center;
  gap: .35rem;
  padding: .5rem 1rem;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.tool {
  min-width: 32px;
  height: 30px;
  padding: 0 .5rem;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--body-txt);
  font-size: .85rem;
  cursor: pointer;
}
.tool:hover { border-color: var(--accent); color: var(--accent); }
.tool-wide { margin-left: auto; }
.tool[aria-pressed="false"] { opacity: .55; }
.tool-sep { flex: 1; }

/* ---- Corpo: textarea + preview lado a lado ---- */
.modal-body { flex: 1; display: grid; grid-template-columns: 1fr 1fr; min-height: 0; }
.modal-body.no-preview { grid-template-columns: 1fr; }
.modal-body.no-preview .ed-preview { display: none; }

.ed-text {
  border: none;
  border-right: 1px solid var(--border);
  padding: 1rem;
  resize: none;
  background: var(--bg);
  color: var(--fg);
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .82rem;
  line-height: 1.6;
  tab-size: 2;
}
.ed-text:focus { outline: none; }

.ed-preview {
  padding: 1rem 1.2rem;
  overflow-y: auto;
  font-size: .86rem;
  line-height: 1.55;
  color: var(--body-txt);
}
.ed-preview h1, .ed-preview h2, .ed-preview h3 {
  color: var(--strong);
  margin: 1rem 0 .5rem;
  line-height: 1.3;
}
.ed-preview h1 { font-size: 1.25rem; }
.ed-preview h2 { font-size: 1.08rem; }
.ed-preview h3 { font-size: .96rem; }
.ed-preview p, .ed-preview ul, .ed-preview ol, .ed-preview blockquote { margin: .55rem 0; }
.ed-preview ul, .ed-preview ol { padding-left: 1.3rem; }
.ed-preview a { color: var(--accent); }
.ed-preview code {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .05rem .3rem;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .8rem;
}
.ed-preview pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: .7rem .8rem;
  overflow-x: auto;
  margin: .6rem 0;
}
.ed-preview pre code { background: none; border: none; padding: 0; }
.ed-preview blockquote {
  border-left: 3px solid var(--accent);
  padding-left: .8rem;
  color: var(--muted);
}
.ed-preview hr { border: none; border-top: 1px solid var(--border); margin: 1rem 0; }
.ed-preview table { border-collapse: collapse; width: 100%; margin: .6rem 0; font-size: .8rem; }
.ed-preview th, .ed-preview td { border: 1px solid var(--border); padding: .35rem .5rem; text-align: left; }
.ed-preview th { background: var(--bg); color: var(--strong); }
.ed-preview .fm {                    /* bloco de frontmatter YAML no preview */
  background: var(--bg);
  border: 1px dashed var(--border-str);
  border-radius: 6px;
  padding: .5rem .7rem;
  margin-bottom: .8rem;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .74rem;
  color: var(--muted);
  white-space: pre-wrap;
}

/* ---- Rodape: status, senha e acoes ---- */
.modal-foot {
  display: flex;
  align-items: center;
  gap: .6rem;
  padding: .7rem 1rem;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.ed-status { flex: 1; font-size: .78rem; color: var(--muted); min-width: 140px; }
.ed-status.err { color: #dc2626; }
.ed-status.ok  { color: var(--accent); }
.ed-pass {
  width: 170px;
  padding: .4rem .6rem;
  border: 1px solid var(--border-str);
  border-radius: 8px;
  background: var(--bg);
  color: var(--fg);
  font-size: .82rem;
}
.btn {
  padding: .45rem 1rem;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--body-txt);
  font-size: .85rem;
  cursor: pointer;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: var(--bg); font-weight: 500; }
.btn-primary:hover { color: var(--bg); opacity: .9; }
.btn[disabled] { opacity: .5; cursor: not-allowed; }

/* ---- Botao "Novo" no header ---- */
.new-btn {
  flex-shrink: 0;
  height: 34px;
  padding: 0 .8rem;
  background: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 8px;
  color: var(--bg);
  font-size: .82rem;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}
.new-btn:hover { opacity: .9; }

/* ---- Painel de historico (sobrepoe o preview) ---- */
.ed-hist {
  grid-column: 2;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--surface);
  border-left: 1px solid var(--border);
}
.modal-body.no-preview .ed-hist:not([hidden]) { grid-column: 1; }
.hist-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: .6rem .9rem;
  border-bottom: 1px solid var(--border);
  font-size: .85rem;
  color: var(--strong);
}
.hist-list { list-style: none; overflow-y: auto; max-height: 45%; }
.hist-list li {
  display: flex;
  align-items: center;
  gap: .5rem;
  padding: .45rem .9rem;
  border-bottom: 1px solid var(--border);
  font-size: .76rem;
}
.hist-when { flex: 1; font-family: "SF Mono", Menlo, Consolas, monospace; color: var(--body-txt); }
.hist-size { color: var(--muted-2); font-size: .7rem; }
.hist-empty { padding: 1rem .9rem; color: var(--muted); font-size: .8rem; }
.hist-diff {
  flex: 1;
  overflow: auto;
  padding: .6rem .9rem;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: .74rem;
  line-height: 1.5;
  border-top: 1px solid var(--border);
  white-space: pre-wrap;
  word-break: break-word;
}
.d-add { background: var(--c-devops-bg);    color: var(--c-devops-fg);    display: block; }
.d-del { background: var(--c-seguranca-bg); color: var(--c-seguranca-fg); display: block; }
.d-ctx { color: var(--muted); display: block; }
.d-none { color: var(--muted); font-style: italic; }

.btn-sm { padding: .25rem .6rem; font-size: .76rem; }

/* ---- Dialogo de criacao ---- */
.modal-sm { width: min(460px, 94vw); height: auto; }
.nw-body { padding: 1rem; display: flex; flex-direction: column; gap: .8rem; }
.nw-field { display: flex; flex-direction: column; gap: .25rem; }
.nw-field span {
  font-size: .68rem;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted-2);
}
.nw-field select, .nw-field input {
  padding: .5rem .7rem;
  border: 1px solid var(--border-str);
  border-radius: 8px;
  background: var(--bg);
  color: var(--fg);
  font-size: .88rem;
}
.nw-path { font-size: .78rem; color: var(--muted); }
.nw-path code {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  color: var(--accent);
  word-break: break-all;
}
.nw-note { font-size: .74rem; color: var(--muted-2); line-height: 1.45; }
.nw-err { font-size: .78rem; color: #dc2626; }

@media (max-width: 760px) {
  .modal-body { grid-template-columns: 1fr; }
  .ed-preview { display: none; }        /* tela estreita: so o editor */
  .ed-hist { grid-column: 1; }
}
"""

    # String RAW: o JS usa \n, \s, \w e \d em regex e em split/join. Sem o r"",
    # o Python converteria \n em quebra de linha real e quebraria a regex.
    app_js = r"""/* =====================================================================
   app.js - Inventario Claude Code (v2)
   Logica de busca textual e filtro por tipo (skills, agents, commands,
   plugins, MCPs). Sem dependencias externas.
====================================================================== */

const search = document.getElementById('search');
const tabs   = document.querySelectorAll('.tab');
const cards  = document.querySelectorAll('.card');
const empty  = document.getElementById('empty');

/* Os tres filtros sao independentes e se combinam (E logico). */
let activeFilter = 'all';   // tipo   (aba)
let activeScope  = 'all';   // origem (chip)
let activeCat    = 'all';   // finalidade (chip)

/**
 * Reaplica busca + os tres filtros sobre todos os cards e controla a
 * mensagem de "nenhum resultado".
 */
function apply() {
  const q = search.value.trim().toLowerCase();
  let visible = 0;
  cards.forEach(card => {
    const show =
      (activeFilter === 'all' || card.dataset.type  === activeFilter) &&
      (activeScope  === 'all' || card.dataset.scope === activeScope)  &&
      (activeCat    === 'all' || card.dataset.cat   === activeCat)    &&
      (!q || card.dataset.search.includes(q));
    card.hidden = !show;
    if (show) visible++;
  });
  empty.hidden = visible > 0;
}

/* =====================================================================
   Idioma (pt-BR / en)
   Os textos estaticos ja vem no HTML em data-pt / data-en: trocar e so
   reescrever o textContent. As mensagens que o JS monta em tempo de
   execucao ficam no dicionario MSG abaixo.
====================================================================== */

const MSG = {
  pt: {
    loading:       'Carregando...',
    ready:         'Pronto.',
    readonly:      'Arquivo somente leitura no servidor.',
    backendDown:   'Backend indisponível (api.php).',
    needPass:      'Informe a senha para salvar.',
    needPassRest:  'Informe a senha para restaurar.',
    saving:        'Salvando...',
    savedFmt:      'Salvo — {b} bytes. Revisão anterior guardada no histórico.',
    createdFmt:    'Criado — {b} bytes. Recarregue a página para vê-lo na lista.',
    newFile:       'Arquivo novo — preencha e salve.',
    unsaved:       'Há alterações não salvas. Fechar mesmo assim?',
    noRevisions:   'Nenhuma revisão ainda. A primeira aparece depois da próxima gravação.',
    identical:     'Idêntico ao texto atual.',
    diff:          'Diff',
    restore:       'Restaurar',
    restoreAsk:    'Restaurar a versão de {w}? O conteúdo atual vira uma revisão.',
    restoring:     'Restaurando...',
    restoredFmt:   'Restaurado de {w}.',
    fail:          'Falha.',
    lastGenFmt:    'Última geração: {d} — {n} itens.',
    queued:        'Na fila. O cron executa em até 60 segundos...',
    regenOkFmt:    'Pronto — {n} itens em {s}s.',
    reload:        'Recarregar página',
    regenTimeout:  'Sem resposta em 2 minutos. Verifique o cron.',
    registering:   'Registrando pedido...',
    passMismatch:  'As duas senhas novas não conferem.',
    passShort:     'Mínimo de 8 caracteres.',
    passChanging:  'Trocando...',
    passChanged:   'Senha trocada. Use a nova daqui em diante.',
    nameBad:       'Nome inválido. Use minúsculas, números e hífen.',
    needPassAny:   'Informe a senha.',
    // erros devolvidos pelo backend, por codigo
    e_senha:       'Senha incorreta.',
    e_conflito:    'O arquivo mudou no disco desde que você abriu. Recarregue antes de salvar.',
    e_existe:      'Já existe um arquivo nesse caminho.',
    e_fm_ausente:  'Frontmatter ausente. O arquivo precisa começar com um bloco --- ... --- contendo name e description.',
    e_fm_falta:    'Frontmatter incompleto: sem name/description o Claude Code não carrega o recurso.',
    e_escopo:      'Fora do escopo editável (skills, agents, commands).',
    e_naoexiste:   'Arquivo não encontrado.'
  },
  en: {
    loading:       'Loading...',
    ready:         'Ready.',
    readonly:      'File is read-only on the server.',
    backendDown:   'Backend unavailable (api.php).',
    needPass:      'Enter the password to save.',
    needPassRest:  'Enter the password to restore.',
    saving:        'Saving...',
    savedFmt:      'Saved — {b} bytes. Previous revision kept in history.',
    createdFmt:    'Created — {b} bytes. Reload the page to see it listed.',
    newFile:       'New file — fill it in and save.',
    unsaved:       'There are unsaved changes. Close anyway?',
    noRevisions:   'No revisions yet. The first one appears after the next save.',
    identical:     'Identical to the current text.',
    diff:          'Diff',
    restore:       'Restore',
    restoreAsk:    'Restore the version from {w}? The current content becomes a revision.',
    restoring:     'Restoring...',
    restoredFmt:   'Restored from {w}.',
    fail:          'Failed.',
    lastGenFmt:    'Last generated: {d} — {n} items.',
    queued:        'Queued. Cron runs it within 60 seconds...',
    regenOkFmt:    'Done — {n} items in {s}s.',
    reload:        'Reload page',
    regenTimeout:  'No response in 2 minutes. Check cron.',
    registering:   'Registering request...',
    passMismatch:  'The two new passwords do not match.',
    passShort:     'Minimum 8 characters.',
    passChanging:  'Changing...',
    passChanged:   'Password changed. Use the new one from now on.',
    nameBad:       'Invalid name. Use lowercase, digits and hyphen.',
    needPassAny:   'Enter the password.',
    e_senha:       'Wrong password.',
    e_conflito:    'The file changed on disk since you opened it. Reload before saving.',
    e_existe:      'A file already exists at that path.',
    e_fm_ausente:  'Missing frontmatter. The file must start with a --- ... --- block containing name and description.',
    e_fm_falta:    'Incomplete frontmatter: without name/description Claude Code will not load the resource.',
    e_escopo:      'Outside the editable scope (skills, agents, commands).',
    e_naoexiste:   'File not found.'
  }
};

/** Idioma salvo; sem escolha previa, segue o do navegador. */
let LANG = (() => {
  try {
    const saved = localStorage.getItem('inv-lang');
    if (saved === 'pt' || saved === 'en') return saved;
  } catch (e) {}
  return (navigator.language || 'pt').toLowerCase().startsWith('pt') ? 'pt' : 'en';
})();

/** Texto de runtime, com substituicao de {placeholders}. */
function t(key, vars) {
  let s = (MSG[LANG] && MSG[LANG][key]) || MSG.pt[key] || key;
  if (vars) {
    for (const k in vars) s = s.replace('{' + k + '}', vars[k]);
  }
  return s;
}

/** Mensagem de erro do backend: usa o codigo quando vier, senao o texto. */
function apiErr(d) {
  if (d && d.code && (MSG[LANG]['e_' + d.code] || MSG.pt['e_' + d.code])) {
    return t('e_' + d.code);
  }
  return (d && d.error) || t('fail');
}

/** Aplica o idioma a todo texto estatico marcado no HTML. */
function applyLang() {
  const attr = LANG === 'en' ? 'en' : 'pt';
  document.querySelectorAll('[data-pt]').forEach(el => {
    const v = el.dataset[attr];
    if (v !== undefined) el.textContent = v;
  });
  document.querySelectorAll('[data-pt-title]').forEach(el => {
    const v = LANG === 'en' ? el.dataset.enTitle : el.dataset.ptTitle;
    if (v !== undefined) el.title = v;
  });
  document.querySelectorAll('[data-pt-ph]').forEach(el => {
    const v = LANG === 'en' ? el.dataset.enPh : el.dataset.ptPh;
    if (v !== undefined) el.placeholder = v;
  });
  document.documentElement.lang = LANG === 'en' ? 'en' : 'pt-BR';
  // A bandeira mostra o idioma de DESTINO, como o icone do tema.
  const flag = document.getElementById('lang-flag');
  if (flag) flag.textContent = LANG === 'en' ? '🇧🇷' : '🇺🇸';
}

document.getElementById('lang-btn').addEventListener('click', () => {
  LANG = LANG === 'en' ? 'pt' : 'en';
  try { localStorage.setItem('inv-lang', LANG); } catch (e) {}
  applyLang();
});

applyLang();


/* Busca em tempo real conforme o usuario digita */
search.addEventListener('input', apply);

/* Troca de aba: marca a aba ativa e reaplica o filtro */
tabs.forEach(tab => {
  tab.addEventListener('click', () => {
    tabs.forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    activeFilter = tab.dataset.filter;
    apply();
  });
});

/**
 * Liga um grupo de chips a uma variavel de filtro. Os grupos sao
 * exclusivos entre si — clicar num chip desmarca os irmaos.
 */
function wireChips(attr, set) {
  const group = document.querySelectorAll('.chip[data-' + attr + ']');
  group.forEach(chip => {
    chip.addEventListener('click', () => {
      group.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      set(chip.dataset[attr === 'catf' ? 'catf' : 'scope']);
      apply();
    });
  });
}

wireChips('scope', v => { activeScope = v; });
wireChips('catf',  v => { activeCat   = v; });

/* ---------------------------------------------------------------------
   Tema claro/escuro
   Sem data-theme no <html>, o CSS segue o prefers-color-scheme do SO.
   O primeiro clique grava a escolha explicita, que passa a vencer o SO.
   O estado inicial ja foi aplicado pelo script inline do <head>.
--------------------------------------------------------------------- */
const themeBtn = document.getElementById('theme-toggle');

/** Tema em vigor agora — o explicito, ou o que o SO pede. */
function currentTheme() {
  return document.documentElement.dataset.theme
      || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

themeBtn.addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('inv-theme', next); } catch (e) {}
});


/* =====================================================================
   Editor de markdown
   Cards com data-rel apontam para um .md sob ~/.claude. Clicar abre o
   modal, que le e grava pelo api.php (gravacao exige senha).
====================================================================== */

const API = 'api';        // proxied by nginx to the Python backend

const modal    = document.getElementById('editor');
const edTitle  = document.getElementById('ed-title');
const edPath   = document.getElementById('ed-path');
const edText   = document.getElementById('ed-text');
const edPrev   = document.getElementById('ed-preview');
const edPass   = document.getElementById('ed-pass');
const edStatus = document.getElementById('ed-status');
const edSave   = document.getElementById('ed-save');
const edBody   = modal.querySelector('.modal-body');
const prevBtn  = document.getElementById('ed-preview-toggle');

let openFile   = null;   // caminho relativo do arquivo aberto
let openMtime  = null;   // mtime lido — detecta edicao concorrente ao salvar
let savedText  = '';     // conteudo como veio do disco (detecta alteracao)
let lastFocus  = null;   // elemento que tinha foco antes de abrir o modal
let isNew      = false;  // true entre 'Criar e editar' e a primeira gravacao

/* ---------------------------------------------------------------------
   Renderizador markdown minimo
   Cobre o que aparece em SKILL.md: frontmatter, titulos, listas, tabelas,
   blocos de codigo, citacao, enfase e link. Escapa HTML ANTES de aplicar
   qualquer regra — o conteudo vem de arquivo local, mas o preview nao
   pode virar vetor de injecao.
--------------------------------------------------------------------- */
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Enfase, codigo inline e links — aplicado dentro de uma linha ja escapada. */
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderMarkdown(src) {
  let out = '';

  // Frontmatter YAML: mostrado como bloco proprio, nunca interpretado.
  const fm = src.match(/^---\n([\s\S]*?)\n---\n?/);
  if (fm) {
    out += '<div class="fm">' + escapeHtml(fm[1]) + '</div>';
    src = src.slice(fm[0].length);
  }

  const lines = escapeHtml(src).split('\n');
  let i = 0, listOpen = null, para = [];

  const flushPara = () => {
    if (para.length) { out += '<p>' + inline(para.join(' ')) + '</p>'; para = []; }
  };
  const closeList = () => {
    if (listOpen) { out += '</' + listOpen + '>'; listOpen = null; }
  };

  while (i < lines.length) {
    const line = lines[i];

    // Bloco de codigo cercado — conteudo sai literal, sem inline()
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushPara(); closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out += '<pre><code>' + buf.join('\n') + '</code></pre>';
      continue;
    }

    // Tabela: linha com | seguida de separador |---|
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushPara(); closeList();
      const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      out += '<table><thead><tr>' +
             cells(line).map(c => '<th>' + inline(c) + '</th>').join('') +
             '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        out += '<tr>' + cells(lines[i]).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>';
        i++;
      }
      out += '</tbody></table>';
      continue;
    }

    if (/^\s*$/.test(line))            { flushPara(); closeList(); i++; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushPara(); closeList(); out += '<hr>'; i++; continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      const lvl = Math.min(h[1].length, 3);
      out += '<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>';
      i++; continue;
    }

    const qt = line.match(/^&gt;\s?(.*)$/);       // '>' ja virou &gt; no escape
    if (qt) {
      flushPara(); closeList();
      out += '<blockquote>' + inline(qt[1]) + '</blockquote>';
      i++; continue;
    }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const want = ul ? 'ul' : 'ol';
      if (listOpen !== want) { closeList(); out += '<' + want + '>'; listOpen = want; }
      out += '<li>' + inline((ul || ol)[1]) + '</li>';
      i++; continue;
    }

    closeList();
    para.push(line);
    i++;
  }
  flushPara(); closeList();
  return out;
}

function refreshPreview() { edPrev.innerHTML = renderMarkdown(edText.value); }

function setStatus(msg, kind) {
  edStatus.textContent = msg;
  edStatus.className = 'ed-status' + (kind ? ' ' + kind : '');
}

/* ---------------------------------------------------------------------
   Abrir / fechar
--------------------------------------------------------------------- */
async function openEditor(rel, name) {
  lastFocus = document.activeElement;
  openFile = rel;
  isNew = false;
  document.getElementById('ed-hist').hidden = true;
  edTitle.textContent = name;
  edPath.textContent = rel;
  edText.value = '';
  edPrev.innerHTML = '';
  edPass.value = '';
  setStatus(t('loading'));
  modal.hidden = false;
  edSave.disabled = true;

  try {
    const r = await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'read', file: rel })
    });
    const data = await r.json();
    if (!r.ok) { setStatus(apiErr(data), 'err'); return; }

    edText.value = data.content;
    savedText = data.content;
    openMtime = data.mtime;
    refreshPreview();
    edSave.disabled = !data.writable;
    setStatus(data.writable ? t('ready') : t('readonly'), data.writable ? null : 'err');
    edText.focus();
  } catch (e) {
    setStatus(t('backendDown'), 'err');
  }
}

function closeEditor(force) {
  if (!force && edText.value !== savedText && !confirm(t('unsaved'))) return;
  modal.hidden = true;
  openFile = null;
  if (lastFocus) lastFocus.focus();
}

/* Clique e Enter/Espaco nos cards editaveis */
cards.forEach(card => {
  if (!card.dataset.rel) return;
  const name = card.querySelector('.card-name').textContent;
  card.addEventListener('click', ev => {
    if (ev.target.closest('a')) return;      // link do GitHub nao abre o editor
    openEditor(card.dataset.rel, name);
  });
  card.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openEditor(card.dataset.rel, name); }
  });
});

document.getElementById('ed-close').addEventListener('click', () => closeEditor(false));
document.getElementById('ed-cancel').addEventListener('click', () => closeEditor(false));
modal.querySelector('[data-close]').addEventListener('click', () => closeEditor(false));
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && !modal.hidden) closeEditor(false);
});

edText.addEventListener('input', refreshPreview);

/* ---------------------------------------------------------------------
   Barra de ferramentas: envolve a selecao ou insere marcacao no cursor
--------------------------------------------------------------------- */
const WRAP = {
  bold:   ['**', '**', 'texto'],
  italic: ['*',  '*',  'texto'],
  code:   ['`',  '`',  'codigo'],
  head:   ['## ', '',  'Título'],
  list:   ['- ',  '',  'item'],
  link:   ['[',  '](https://)', 'texto']
};

function applyMd(kind) {
  const [pre, post, ph] = WRAP[kind];
  const s = edText.selectionStart, e = edText.selectionEnd;
  const sel = edText.value.slice(s, e) || ph;
  edText.setRangeText(pre + sel + post, s, e, 'select');
  edText.focus();
  refreshPreview();
}

document.querySelectorAll('.tool[data-md]').forEach(b => {
  b.addEventListener('click', () => applyMd(b.dataset.md));
});

edText.addEventListener('keydown', ev => {
  if (!(ev.ctrlKey || ev.metaKey)) return;
  const k = ev.key.toLowerCase();
  if (k === 'b') { ev.preventDefault(); applyMd('bold'); }
  if (k === 'i') { ev.preventDefault(); applyMd('italic'); }
  if (k === 's') { ev.preventDefault(); save(); }
});

/* Alterna o painel de preview */
prevBtn.addEventListener('click', () => {
  const on = prevBtn.getAttribute('aria-pressed') === 'true';
  prevBtn.setAttribute('aria-pressed', String(!on));
  edBody.classList.toggle('no-preview', on);
});

/* ---------------------------------------------------------------------
   Salvar
--------------------------------------------------------------------- */
async function save() {
  if (!openFile || edSave.disabled) return;
  if (!edPass.value) { setStatus(t('needPass'), 'err'); edPass.focus(); return; }

  edSave.disabled = true;
  setStatus(t('saving'));
  try {
    const r = await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: isNew ? 'create' : 'save', file: openFile,
        content: edText.value, password: edPass.value,
        mtime: isNew ? null : openMtime
      })
    });
    const data = await r.json();
    if (!r.ok) { setStatus(apiErr(data), 'err'); edSave.disabled = false; return; }

    savedText = edText.value;
    openMtime = data.mtime;
    if (isNew) {
      isNew = false;
      edPath.textContent = openFile;
      setStatus(t('createdFmt', { b: data.bytes }), 'ok');
    } else {
      setStatus(t('savedFmt', { b: data.bytes }), 'ok');
    }
    edSave.disabled = false;
  } catch (e) {
    setStatus(t('backendDown'), 'err');
    edSave.disabled = false;
  }
}

edSave.addEventListener('click', save);
edPass.addEventListener('keydown', ev => { if (ev.key === 'Enter') save(); });


/* =====================================================================
   Historico de revisoes
   O backend guarda uma copia datada a cada gravacao (as 10 ultimas).
   Aqui elas viram lista, diff contra o texto atual e restauracao.
====================================================================== */

const histPanel = document.getElementById('ed-hist');
const histList  = document.getElementById('hist-list');
const histDiff  = document.getElementById('hist-diff');

function fmtWhen(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function openHistory() {
  if (!openFile) return;
  histPanel.hidden = false;
  histDiff.hidden = true;
  histList.innerHTML = '<li class="hist-empty">' + t('loading') + '</li>';

  const r = await fetch(API, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'history', file: openFile })
  });
  const data = await r.json();
  if (!r.ok) { histList.innerHTML = '<li class="hist-empty">' + apiErr(data) + '</li>'; return; }

  if (!data.revisions.length) {
    histList.innerHTML = '<li class="hist-empty">' + t('noRevisions') + '</li>';
    return;
  }

  histList.innerHTML = '';
  data.revisions.forEach(rev => {
    const li = document.createElement('li');
    li.innerHTML = '<span class="hist-when"></span><span class="hist-size"></span>';
    li.querySelector('.hist-when').textContent = fmtWhen(rev.mtime);
    li.querySelector('.hist-size').textContent = rev.bytes + ' B';

    const bDiff = document.createElement('button');
    bDiff.className = 'btn btn-sm';
    bDiff.textContent = t('diff');
    bDiff.onclick = () => showDiff(rev.rev);

    const bRest = document.createElement('button');
    bRest.className = 'btn btn-sm';
    bRest.textContent = t('restore');
    bRest.onclick = () => restore(rev.rev, fmtWhen(rev.mtime));

    li.append(bDiff, bRest);
    histList.appendChild(li);
  });
}

/**
 * Diff por linha entre a revisao e o texto no editor.
 * Guloso e simples: nao e o algoritmo de menor edicao, mas para um .md
 * editado a mao mostra exatamente o que entrou e o que saiu.
 */
function lineDiff(oldText, newText) {
  const a = oldText.split('\n'), b = newText.split('\n');
  const out = [];
  let i = 0, j = 0;

  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push(['ctx', a[i]]); i++; j++; continue;
    }
    // Procura o proximo ponto de reencontro dentro de uma janela curta.
    let found = -1;
    for (let k = 1; k <= 40 && found < 0; k++) {
      if (j + k < b.length && a[i] === b[j + k]) found = k;
    }
    if (found > 0) { for (let k = 0; k < found; k++) out.push(['add', b[j++]]); continue; }

    let found2 = -1;
    for (let k = 1; k <= 40 && found2 < 0; k++) {
      if (i + k < a.length && b[j] === a[i + k]) found2 = k;
    }
    if (found2 > 0) { for (let k = 0; k < found2; k++) out.push(['del', a[i++]]); continue; }

    if (i < a.length) out.push(['del', a[i++]]);
    if (j < b.length) out.push(['add', b[j++]]);
  }
  return out;
}

async function showDiff(rev) {
  histDiff.hidden = false;
  histDiff.textContent = t('loading');

  const r = await fetch(API, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'revision', file: openFile, rev })
  });
  const data = await r.json();
  if (!r.ok) { histDiff.textContent = apiErr(data); return; }

  const rows = lineDiff(data.content, edText.value);
  if (!rows.some(([k]) => k !== 'ctx')) {
    histDiff.innerHTML = '<span class="d-none">' + t('identical') + '</span>';
    return;
  }

  histDiff.innerHTML = '';
  rows.forEach(([kind, line]) => {
    // Contexto so aparece perto de mudanca; aqui mantemos tudo, mas
    // esmaecido — os .md sao curtos o bastante.
    const el = document.createElement('span');
    el.className = 'd-' + kind;
    el.textContent = (kind === 'add' ? '+ ' : kind === 'del' ? '- ' : '  ') + line;
    histDiff.appendChild(el);
  });
}

async function restore(rev, when) {
  if (!edPass.value) { setStatus(t('needPassRest'), 'err'); edPass.focus(); return; }
  if (!confirm(t('restoreAsk', { w: when }))) return;

  setStatus(t('restoring'));
  const r = await fetch(API, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'restore', file: openFile, rev, password: edPass.value })
  });
  const data = await r.json();
  if (!r.ok) { setStatus(apiErr(data), 'err'); return; }

  edText.value = data.content;
  savedText = data.content;
  openMtime = data.mtime;
  refreshPreview();
  setStatus(t('restoredFmt', { w: when }), 'ok');
  openHistory();
}

document.getElementById('ed-hist-btn').addEventListener('click', openHistory);
document.getElementById('hist-close').addEventListener('click', () => { histPanel.hidden = true; });


/* =====================================================================
   Criacao de recurso novo
====================================================================== */

const newDlg  = document.getElementById('newdlg');
const nwKind  = document.getElementById('nw-kind');
const nwName  = document.getElementById('nw-name');
const nwPrev  = document.getElementById('nw-preview');
const nwErr   = document.getElementById('nw-err');

/** Caminho de destino conforme o tipo escolhido. */
function newPath() {
  const n = nwName.value.trim() || 'nome';
  return nwKind.value === 'skills' ? `skills/${n}/SKILL.md` : `${nwKind.value}/${n}.md`;
}

/** Template com o frontmatter que o backend exige. */
function template(kind, name) {
  if (kind === 'commands') {
    return `---\ndescription: O que este comando faz\n---\n\n# /${name}\n\nDescreva os passos aqui.\n`;
  }
  const what = kind === 'agents' ? 'agent' : 'skill';
  return `---\nname: ${name}\ndescription: Use when ... (quando este ${what} deve ser acionado)\n---\n\n# ${name}\n\n## Visão geral\n\nO que faz e por quê.\n\n## Quando usar\n\n- situação 1\n- situação 2\n\n## Passos\n\n1. primeiro\n2. segundo\n`;
}

function syncPreview() { nwPrev.textContent = newPath(); }
nwKind.addEventListener('change', syncPreview);
nwName.addEventListener('input', syncPreview);

function openNew() {
  nwName.value = '';
  nwErr.hidden = true;
  syncPreview();
  newDlg.hidden = false;
  nwName.focus();
}
function closeNew() { newDlg.hidden = true; }

document.getElementById('new-btn').addEventListener('click', openNew);
document.getElementById('nw-close').addEventListener('click', closeNew);
document.getElementById('nw-cancel').addEventListener('click', closeNew);
newDlg.querySelector('[data-close]').addEventListener('click', closeNew);

document.getElementById('nw-ok').addEventListener('click', () => {
  const name = nwName.value.trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(name)) {
    nwErr.textContent = t('nameBad');
    nwErr.hidden = false;
    return;
  }
  const rel = nwKind.value === 'skills' ? `skills/${name}/SKILL.md` : `${nwKind.value}/${name}.md`;
  closeNew();

  // Abre o editor em modo criacao: o arquivo so nasce no primeiro Salvar.
  lastFocus = document.activeElement;
  openFile  = rel;
  openMtime = null;
  isNew     = true;
  edTitle.textContent = name;
  edPath.textContent  = rel + '  (novo)';
  edText.value = template(nwKind.value, name);
  savedText = '';                     // forca aviso se fechar sem salvar
  edPass.value = '';
  histPanel.hidden = true;
  refreshPreview();
  modal.hidden = false;
  edSave.disabled = false;
  setStatus(t('newFile'));
  edText.focus();
});

nwName.addEventListener('keydown', ev => {
  if (ev.key === 'Enter') document.getElementById('nw-ok').click();
});
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && !newDlg.hidden) closeNew();
});


/* =====================================================================
   Regeneracao do inventario
   O backend nao executa processo (exec/system desabilitados no pool):
   ele so registra o pedido. Quem regenera e o cron, de minuto em minuto.
   Aqui pedimos e ficamos observando o arquivo de status mudar.
====================================================================== */

const regenDlg  = document.getElementById('regendlg');
const rgPass    = document.getElementById('rg-pass');
const rgMsg     = document.getElementById('rg-msg');
const rgOk      = document.getElementById('rg-ok');

let pollTimer = null;

function closeRegen() {
  regenDlg.hidden = true;
  clearInterval(pollTimer);
  pollTimer = null;
}

document.getElementById('regen-btn').addEventListener('click', async () => {
  rgPass.value = '';
  rgMsg.textContent = '';
  regenDlg.hidden = false;
  rgOk.disabled = false;
  rgPass.focus();

  // Mostra de quando e a pagina que esta no ar.
  try {
    const r = await fetch(API, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'status' })
    });
    const d = await r.json();
    if (d.status && d.status.at) {
      rgMsg.textContent = t('lastGenFmt', {
        d: new Date(d.status.at).toLocaleString(LANG === 'en' ? 'en-US' : 'pt-BR'),
        n: d.status.items
      });
    }
  } catch (e) { /* backend fora do ar: o botao Regenerar dira o motivo */ }
});

document.getElementById('rg-close').addEventListener('click', closeRegen);
document.getElementById('rg-cancel').addEventListener('click', closeRegen);
regenDlg.querySelector('[data-close]').addEventListener('click', closeRegen);

rgOk.addEventListener('click', async () => {
  if (!rgPass.value) { rgMsg.textContent = t('needPassAny'); return; }
  rgOk.disabled = true;
  rgMsg.textContent = t('registering');

  let before = null;
  try {
    const s = await fetch(API, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'status' })
    });
    before = (await s.json()).status?.at ?? null;
  } catch (e) { /* segue: a comparacao apenas fica menos precisa */ }

  const r = await fetch(API, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'regen', password: rgPass.value })
  });
  const d = await r.json();
  if (!r.ok) { rgMsg.textContent = apiErr(d); rgOk.disabled = false; return; }

  rgMsg.textContent = t('queued');

  // Observa o status ate a data de geracao mudar (ou desistir em 2 min).
  let tries = 0;
  pollTimer = setInterval(async () => {
    tries++;
    try {
      const s = await fetch(API, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'status' })
      });
      const st = (await s.json()).status;
      if (st && st.at && st.at !== before) {
        clearInterval(pollTimer);
        rgMsg.innerHTML = t('regenOkFmt', { n: st.items, s: st.seconds }) +
          ' <a href="" onclick="location.reload();return false;">' + t('reload') + '</a>';
        return;
      }
    } catch (e) { /* tenta de novo no proximo tick */ }
    if (tries > 24) {
      clearInterval(pollTimer);
      rgMsg.textContent = t('regenTimeout');
      rgOk.disabled = false;
    }
  }, 5000);
});

rgPass.addEventListener('keydown', ev => { if (ev.key === 'Enter') rgOk.click(); });

/* =====================================================================
   Troca da senha de gravacao
====================================================================== */

const pwDlg  = document.getElementById('pwdlg');
const pwOld  = document.getElementById('pw-old');
const pwNew  = document.getElementById('pw-new');
const pwNew2 = document.getElementById('pw-new2');
const pwMsg  = document.getElementById('pw-msg');
const pwOk   = document.getElementById('pw-ok');

function closePw() { pwDlg.hidden = true; pwOld.value = pwNew.value = pwNew2.value = ''; }

function pwErr(msg, ok) {
  pwMsg.textContent = msg;
  pwMsg.hidden = false;
  pwMsg.className = ok ? 'nw-note' : 'nw-err';
}

document.getElementById('pw-btn').addEventListener('click', () => {
  pwMsg.hidden = true;
  pwOk.disabled = false;
  pwDlg.hidden = false;
  pwOld.focus();
});
document.getElementById('pw-close').addEventListener('click', closePw);
document.getElementById('pw-cancel').addEventListener('click', closePw);
pwDlg.querySelector('[data-close]').addEventListener('click', closePw);

pwOk.addEventListener('click', async () => {
  if (pwNew.value !== pwNew2.value) { pwErr(t('passMismatch')); return; }
  if (pwNew.value.length < 8)       { pwErr(t('passShort')); return; }

  pwOk.disabled = true;
  pwErr(t('passChanging'), true);
  try {
    const r = await fetch(API, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'passwd', password: pwOld.value, nova: pwNew.value })
    });
    const d = await r.json();
    if (!r.ok) { pwErr(apiErr(d)); pwOk.disabled = false; return; }
    pwErr(t('passChanged'), true);
    pwOld.value = pwNew.value = pwNew2.value = '';
    setTimeout(closePw, 1800);
  } catch (e) {
    pwErr(t('backendDown'));
    pwOk.disabled = false;
  }
});

[pwOld, pwNew, pwNew2].forEach(el => el.addEventListener('keydown', ev => {
  if (ev.key === 'Enter') pwOk.click();
}));
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && !pwDlg.hidden) closePw();
});

document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape' && !regenDlg.hidden) closeRegen();
});
"""

    (OUT_DIR / "index.html").write_text(index_html, encoding="utf-8")
    (OUT_DIR / "styles.css").write_text(styles_css, encoding="utf-8")
    (OUT_DIR / "app.js").write_text(app_js, encoding="utf-8")


# -----------------------------------------------------------------------------
# Programa principal
# -----------------------------------------------------------------------------
def main():
    print(f"[*] Varrendo: {BASE}")
    print(f"[*] Modo online (npm + GitHub API): {'SIM' if ONLINE else 'NAO (use --online)'}")

    items = (collect_skills() + collect_agents() + collect_commands()
             + collect_plugins() + collect_mcps()
             + collect_plugin_resources() + collect_project_resources())

    # Resolucao dos repositorios GitHub em camadas (ver cabecalho)
    global MARKETPLACE_REPOS
    MARKETPLACE_REPOS = load_marketplace_repos()
    print(f"[*] Resolvendo repositorios GitHub... ({len(MARKETPLACE_REPOS)} marketplaces mapeados)")
    for it in items:
        resolve_repo(it)
    save_cache(CACHE)

    # Classificacao por finalidade (badge de categoria)
    for it in items:
        it["cat"] = classify(it, it.get("cat_declared", ""))
    print("[*] Categorias:")
    for slug, label in CATEGORY_LABEL.items():
        n = sum(1 for i in items if i["cat"] == slug)
        if n:
            print(f"    - {label:<16}: {n:>3}")

    for t, label in TYPE_LABEL.items():
        n = sum(1 for i in items if i["type"] == t)
        r = sum(1 for i in items if i["type"] == t and i.get("repo"))
        print(f"    - {label + 's':<10}: {n:>3}  ({r} com repositorio)")

    build_site(items)
    print(f"[OK] Site gerado em: {OUT_DIR}/index.html")
    print("     Abra com: open claude_inventory_site/index.html  (Mac)")
    print("     Ou sirva: python3 -m http.server 8080 -d claude_inventory_site")


if __name__ == "__main__":
    main()
