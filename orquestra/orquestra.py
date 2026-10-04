#!/usr/bin/env python3
"""Orquestra: coordena sessões do Claude Code que rodam na mesma máquina.

Hooks (``orquestra.py hook <nome>``) aplicam as regras em todas as sessões:
trava de deploy por produto, bloqueio de troca de branch em árvore
compartilhada, aviso de edição sobreposta e entrega de recados entre sessões.
Os demais subcomandos (painel, sobreposicao, liberar, status, configurar,
remover) são a interface da skill /orquestra e do instalador.

Só biblioteca padrão. Estado em ~/.claude/orquestra/. Qualquer erro interno de
um hook é registrado e o hook sai com 0: um defeito aqui nunca bloqueia uma
sessão.
"""
from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PADRAO = {
    "prazo_deploy_fundo_min": 15,
    "prazo_deploy_min": 15,
    "prazo_skill_min": 60,
    "prazo_sessao_sem_registro_min": 30,
    "skills_deploy": {"deploy-prod": "prod", "tarefa-finalizada": "dev"},
    "mcp_deploy": [
        "mcp__coolify__deploy",
        "mcp__coolify__redeploy_project",
        "mcp__coolify__restart_project_apps",
    ],
    "produtos": {},
}

MARCA_HOOK = "orquestra.py hook "
HOOKS = [
    ("PreToolUse", "Bash", "pre-bash"),
    (
        "PreToolUse",
        "Skill|mcp__coolify__deploy|mcp__coolify__redeploy_project|mcp__coolify__restart_project_apps",
        "pre-deploy",
    ),
    ("PreToolUse", "Edit|Write|NotebookEdit", "pre-edit"),
    ("PostToolUse", "Bash|mcp__coolify__deploy|mcp__coolify__redeploy_project|mcp__coolify__restart_project_apps", "post"),
    ("UserPromptSubmit", None, "prompt"),
    ("Stop", None, "stop"),
    ("SessionEnd", None, "session-end"),
]


# --------------------------------------------------------------------------
# estado e utilitários
# --------------------------------------------------------------------------

def claude_dir() -> Path:
    return Path(os.environ.get("ORQUESTRA_CLAUDE_DIR") or Path.home() / ".claude")


def estado() -> Path:
    return claude_dir() / "orquestra"


def agora() -> float:
    return time.time()


def hora(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def log(msg: str) -> None:
    try:
        d = estado()
        d.mkdir(parents=True, exist_ok=True)
        arq = d / "orquestra.log"
        if arq.exists() and arq.stat().st_size > 1_000_000:
            os.replace(arq, d / "orquestra.log.1")
        with open(arq, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def carregar_json(caminho: Path, padrao=None):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return padrao


def gravar_atomico(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f"{caminho.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, caminho)


def pid_vivo(pid) -> bool:
    try:
        return os.path.exists(f"/proc/{int(pid)}")
    except (TypeError, ValueError):
        return False


def seguro(nome: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", nome)


def config() -> dict:
    cfg = json.loads(json.dumps(PADRAO))
    lido = carregar_json(estado() / "produtos.json", {})
    if isinstance(lido, dict):
        for k, v in lido.items():
            if k in cfg and type(v) is type(cfg[k]):
                cfg[k] = v
    return cfg


# --------------------------------------------------------------------------
# sessões
# --------------------------------------------------------------------------

def resolver_arvore(caminho) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(caminho), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(str(caminho))


def sessoes_registradas() -> dict:
    """sessionId -> registro de ~/.claude/sessions/<pid>.json, só as vivas."""
    saida = {}
    for f in (claude_dir() / "sessions").glob("*.json"):
        d = carregar_json(f)
        if not isinstance(d, dict) or "sessionId" not in d or "pid" not in d:
            continue
        if pid_vivo(d["pid"]):
            saida[d["sessionId"]] = d
    return saida


def sessoes_mortas() -> set:
    """sessionIds que têm registro em sessions/ mas cujo processo acabou."""
    mortas = set()
    for f in (claude_dir() / "sessions").glob("*.json"):
        d = carregar_json(f)
        if isinstance(d, dict) and "sessionId" in d and not pid_vivo(d.get("pid")):
            mortas.add(d["sessionId"])
    return mortas


def claim_path(sid: str) -> Path:
    return estado() / "claims" / f"{seguro(sid)}.json"


def sessao_viva(sid: str, registradas: dict, cfg: dict, ref_ts: float = 0.0) -> bool:
    """Registrada e com processo vivo; sem registro (ex.: claude -p), vale a
    atividade recente do seu arquivo de claim ou o carimbo de referência."""
    if sid in registradas:
        return True
    if sid in sessoes_mortas():
        return False
    prazo = cfg["prazo_sessao_sem_registro_min"] * 60
    try:
        ts = max(claim_path(sid).stat().st_mtime, ref_ts)
    except OSError:
        ts = ref_ts
    return ts > 0 and agora() - ts < prazo


def nome_sessao(sid: str, registradas: dict) -> str:
    return (registradas.get(sid) or {}).get("name") or sid[:8]


def sessoes_ativas(cfg: dict) -> dict:
    """sid -> {nome, arvore, status}. Une registro e claims."""
    reg = sessoes_registradas()
    saida = {}
    for sid, d in reg.items():
        saida[sid] = {
            "nome": d.get("name") or sid[:8],
            "arvore": resolver_arvore(d.get("cwd") or "/"),
            "status": d.get("status", "?"),
            "cwd": d.get("cwd", ""),
            "kind": d.get("kind", ""),
        }
    for f in (estado() / "claims").glob("*.json"):
        c = carregar_json(f)
        if not isinstance(c, dict) or not c.get("session_id"):
            continue
        sid = c["session_id"]
        if sid in reg:
            if c.get("arvore"):
                saida[sid]["arvore"] = c["arvore"]
        elif sessao_viva(sid, reg, cfg):
            saida[sid] = {
                "nome": c.get("nome") or sid[:8], "arvore": c.get("arvore", ""),
                "status": "?", "cwd": c.get("arvore", ""), "kind": "sem-registro",
            }
    return {s: v for s, v in saida.items() if "/.claude-mem/" not in v.get("cwd", "")}


def ler_claim(sid: str) -> dict:
    c = carregar_json(claim_path(sid))
    if not isinstance(c, dict):
        c = {}
    c.setdefault("session_id", sid)
    c.setdefault("arquivos", {})
    c.setdefault("avisados", [])
    return c


def gravar_claim(c: dict) -> None:
    arqs = c["arquivos"]
    if len(arqs) > 500:
        for k in sorted(arqs, key=arqs.get)[: len(arqs) - 500]:
            del arqs[k]
    gravar_atomico(claim_path(c["session_id"]), c)


def atualizar_claim(sid: str, cwd: str, registradas: dict) -> dict:
    c = ler_claim(sid)
    c["nome"] = nome_sessao(sid, registradas)
    c["arvore"] = resolver_arvore(cwd) if cwd else c.get("arvore", "")
    c["visto"] = agora()
    return c


# --------------------------------------------------------------------------
# recados
# --------------------------------------------------------------------------

def recado(sid: str, texto: str) -> None:
    try:
        p = estado() / "avisos" / f"{seguro(sid)}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        linha = json.dumps({"t": agora(), "texto": texto}, ensure_ascii=False) + "\n"
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, linha.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        log(f"recado falhou para {sid}: {e}")


def ler_recados(sid: str) -> list:
    p = estado() / "avisos" / f"{seguro(sid)}.jsonl"
    if not p.exists():
        return []
    tmp = p.with_name(f"{p.name}.lido.{os.getpid()}")
    try:
        os.rename(p, tmp)
    except OSError:
        return []
    itens = []
    try:
        with open(tmp, encoding="utf-8") as f:
            for linha in f:
                try:
                    itens.append(json.loads(linha))
                except ValueError:
                    continue
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return itens


# --------------------------------------------------------------------------
# produtos
# --------------------------------------------------------------------------

def _casa_caminho(caminho: str, padrao: str) -> bool:
    padrao = os.path.expanduser(padrao).rstrip("/")
    if any(c in padrao for c in "*?["):
        return fnmatch.fnmatchcase(caminho, padrao) or fnmatch.fnmatchcase(caminho, padrao + "/*")
    return caminho == padrao or caminho.startswith(padrao + "/")



def produto_por_caminho(caminho, cfg: dict):
    if not caminho:
        return None
    caminho = os.path.realpath(caminho)
    for nome, p in cfg["produtos"].items():
        for pad in (p or {}).get("caminhos", []):
            if _casa_caminho(caminho, pad):
                return nome
    return None


def produto_por_nome(nome: str, cfg: dict):
    for chave, p in cfg["produtos"].items():
        if nome == chave or nome in (p or {}).get("coolify", []):
            return chave
    return None


# --------------------------------------------------------------------------
# análise de comandos Bash
# --------------------------------------------------------------------------

class Evento:
    def __init__(self, tipo, sub, alvo=None, cwd=None, remoto=False, args=None, dir_=None):
        self.tipo, self.sub, self.alvo = tipo, sub, alvo
        self.cwd, self.remoto, self.args, self.dir = cwd, remoto, args or [], dir_

    def __repr__(self):
        return f"Evento({self.tipo},{self.sub},{self.alvo},cwd={self.cwd},remoto={self.remoto},dir={self.dir})"


PONTUACAO = set("();<>|&")
WRAP = {"rtk", "sudo", "time", "nohup", "exec", "command", "env", "nice", "stdbuf", "setsid"}
SSH_COM_VALOR = {"-i", "-p", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-S", "-W"}
COMPOSE_COM_VALOR = {"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile", "--ansi", "--parallel"}


def segmentar(cmd: str) -> list:
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        segs, cur = [], []
        for tok in lex:
            if tok and all(c in PONTUACAO for c in tok):
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(tok)
        if cur:
            segs.append(cur)
        return segs
    except ValueError:
        return []


def _limpar(seg: list) -> list:
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t in WRAP:
            i += 1
        elif t.startswith("-") and i > 0 and seg[i - 1] in WRAP:
            i += 1
        elif t == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
            i += 1
        else:
            break
    return seg[i:]


def _primeiro_nao_flag(toks: list, com_valor=frozenset()) -> int:
    i = 0
    while i < len(toks):
        if toks[i].startswith("-"):
            i += 2 if toks[i] in com_valor else 1
        else:
            return i
    return -1


def _resolver_cd(alvo: str, cwd):
    alvo = os.path.expanduser(alvo)
    if os.path.isabs(alvo):
        return alvo
    return os.path.join(cwd, alvo) if cwd else None


def analisar(cmd: str, cwd, remoto: bool = False, prof: int = 0) -> list:
    eventos = []
    if prof > 3:
        return eventos
    for seg in segmentar(cmd):
        seg = _limpar(seg)
        if not seg:
            continue
        cab, resto = seg[0], seg[1:]
        if cab == "cd" and resto:
            cwd = _resolver_cd(resto[0], cwd)
        elif cab in ("bash", "sh", "zsh") and "-c" in resto and resto.index("-c") + 1 < len(resto):
            eventos += analisar(resto[resto.index("-c") + 1], cwd, remoto, prof + 1)
        elif cab == "eval" and resto:
            eventos += analisar(" ".join(resto), cwd, remoto, prof + 1)
        elif cab == "ssh":
            i = _primeiro_nao_flag(resto, SSH_COM_VALOR)
            if i >= 0 and i + 1 < len(resto):
                eventos += analisar(" ".join(resto[i + 1:]), None, True, prof + 1)
        elif cab == "pct" and "--" in resto:
            eventos += analisar(" ".join(resto[resto.index("--") + 1:]), None, True, prof + 1)
        elif cab == "coolify" and resto[:1] == ["deploy"]:
            r = resto[1:]
            if r[:1] == ["name"]:
                r = r[1:]
            i = _primeiro_nao_flag(r)
            eventos.append(Evento("deploy", "coolify", r[i] if i >= 0 else None, cwd, remoto))
        elif cab == "docker-compose" or (cab == "docker" and resto[:1] == ["compose"]):
            r = resto[1:] if cab == "docker" else resto
            dir_ = None
            for j, t in enumerate(r):
                if t in ("-f", "--file") and j + 1 < len(r):
                    dir_ = os.path.dirname(_resolver_cd(r[j + 1], cwd) or "") or None
                elif t == "--project-directory" and j + 1 < len(r):
                    dir_ = _resolver_cd(r[j + 1], cwd)
            i = _primeiro_nao_flag(r, COMPOSE_COM_VALOR)
            if i >= 0 and r[i] in ("up", "build", "restart"):
                eventos.append(Evento("deploy", "compose", None, cwd, remoto, [r[i]], dir_))
        elif cab == "git":
            r, dir_, i = resto, None, 0
            while i < len(r) and r[i].startswith("-"):
                if r[i] == "-C" and i + 1 < len(r):
                    dir_ = _resolver_cd(r[i + 1], dir_ or cwd)
                    i += 2
                elif r[i] == "-c":
                    i += 2
                else:
                    i += 1
            if i < len(r) and r[i] in ("checkout", "switch"):
                eventos.append(Evento("git", r[i], None, cwd, remoto, r[i + 1:], dir_))
    return eventos


def _git(args: list, cwd) -> tuple:
    try:
        r = subprocess.run(["git", "-C", str(cwd)] + args, capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def troca_de_branch(ev: Evento, cwd) -> bool:
    args = ev.args
    if ev.sub == "switch":
        return not any(a in ("-h", "--help") for a in args)
    if "--" in args:
        return False
    flags = [a for a in args if a.startswith("-") and a != "-"]
    if any(f in ("-p", "--patch", "--ours", "--theirs", "-h", "--help") for f in flags):
        return False
    if any(f in ("-b", "-B", "--orphan", "--detach", "-d", "-t", "--track") or f.startswith(("-b", "-B")) for f in flags):
        return True
    pos = [a for a in args if not a.startswith("-") or a == "-"]
    if len(pos) != 1:
        return False
    if pos[0] == "-":
        return True
    base = ev.dir or cwd or "."
    rc, _ = _git(["rev-parse", "--verify", "--quiet", pos[0] + "^{commit}"], base)
    if rc == 0:
        return True
    rc, out = _git(["for-each-ref", "--count=1", f"refs/remotes/*/{pos[0]}"], base)
    return rc == 0 and bool(out.strip())


# --------------------------------------------------------------------------
# travas de deploy
# --------------------------------------------------------------------------

def lock_path(prod: str, amb: str) -> Path:
    return estado() / "locks" / f"{seguro(prod)}.{amb}.json"


def lock_morto(lock: dict, registradas: dict, cfg: dict) -> bool:
    exp = lock.get("expira")
    if exp and agora() > exp:
        return True
    return not sessao_viva(lock.get("session_id", ""), registradas, cfg, lock.get("visto") or lock.get("desde") or 0)


def _remover_lock(caminho: Path, atual: dict) -> None:
    """Remove só se ainda for o lock lido: renomeia, confere, devolve se errou."""
    stale = caminho.with_name(f"{caminho.name}.stale.{os.getpid()}")
    try:
        os.rename(caminho, stale)
    except OSError:
        return
    lido = carregar_json(stale, {})
    if isinstance(lido, dict) and atual and (lido.get("session_id"), lido.get("desde")) != (atual.get("session_id"), atual.get("desde")):
        try:
            os.link(stale, caminho)
        except OSError:
            pass
    try:
        os.unlink(stale)
    except OSError:
        pass


def _criar_exclusivo(caminho: Path, dados: dict) -> bool:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(f"{caminho.name}.novo.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)
    try:
        os.link(tmp, caminho)
        return True
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def adquirir(prod: str, amb: str, sid: str, nome: str, origem: str, comando: str,
             tool_use_id: str, prazo_min: float, cfg: dict, registradas: dict):
    """None se a trava ficou com a sessão; senão o dict da trava de outra sessão viva."""
    caminho = lock_path(prod, amb)
    novo = {
        "produto": prod, "ambiente": amb, "session_id": sid, "nome": nome,
        "origem": origem, "tool_use_id": tool_use_id, "comando": comando[:300],
        "desde": agora(), "visto": agora(), "expira": agora() + prazo_min * 60,
        "bloqueadas": [],
    }
    for _ in range(4):
        if _criar_exclusivo(caminho, novo):
            return None
        atual = carregar_json(caminho)
        if atual is None:
            if not caminho.exists():
                continue
            log(f"trava ilegível removida: {caminho.name}")
            _remover_lock(caminho, {})
            continue
        if not isinstance(atual, dict):
            _remover_lock(caminho, {})
            continue
        if atual.get("session_id") == sid:
            atual["visto"] = agora()
            atual["expira"] = max(atual.get("expira") or 0, novo["expira"])
            gravar_atomico(caminho, atual)
            return None
        if lock_morto(atual, registradas, cfg):
            log(f"trava de {prod}.{amb} liberada: sessão {atual.get('nome')} morta ou prazo vencido")
            _remover_lock(caminho, atual)
            continue
        return atual
    return None


def liberar_lock(caminho: Path, atual: dict, motivo: str, registradas=None) -> None:
    try:
        os.unlink(caminho)
    except OSError:
        return
    log(f"trava {caminho.stem} liberada ({motivo})")
    reg = registradas if registradas is not None else sessoes_registradas()
    for b in atual.get("bloqueadas", []):
        if b.get("sid") and b["sid"] != atual.get("session_id"):
            recado(b["sid"], f"O deploy de {atual.get('produto')} ({atual.get('ambiente')}) foi liberado; pode tentar de novo.")


def registrar_bloqueio(caminho: Path, atual: dict, sid: str, nome: str) -> None:
    bl = atual.setdefault("bloqueadas", [])
    if not any(b.get("sid") == sid for b in bl):
        bl.append({"sid": sid, "nome": nome})
        try:
            gravar_atomico(caminho, atual)
        except OSError:
            pass


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

class Resposta:
    def __init__(self, negar=None, contexto=None, evento="PreToolUse"):
        self.negar, self.contexto, self.evento = negar, contexto, evento


def _tentar_deploy(prod, amb, d, origem, comando, prazo_min, cfg, registradas, adquiridas):
    sid = d.get("session_id") or ""
    nome = nome_sessao(sid, registradas)
    dono = adquirir(prod, amb, sid, nome, origem, comando, d.get("tool_use_id", ""), prazo_min, cfg, registradas)
    if dono is None:
        adquiridas.append((prod, amb))
        return None
    registrar_bloqueio(lock_path(prod, amb), dono, sid, nome)
    recado(dono["session_id"], f'A sessão "{nome}" tentou deployar {prod} ({amb}) e foi bloqueada pela sua trava.')
    log(f'negado: {nome} em {prod}.{amb}, trava de {dono.get("nome")}')
    return (
        f'[orquestra] Deploy de {prod} ({amb}) em andamento pela sessão "{dono.get("nome")}" '
        f'desde {hora(dono.get("desde", 0))}: {dono.get("comando", "")[:120]}. '
        "Aguarde terminar ou use /orquestra para ver o estado."
    )


def h_pre_bash(d: dict):
    ti = d.get("tool_input") or {}
    cmd = ti.get("command") or ""
    sid = d.get("session_id") or ""
    if not sid or not re.search(r"deploy|compose|checkout|switch", cmd):
        return None
    cwd = d.get("cwd") or os.getcwd()
    cfg = config()
    registradas = sessoes_registradas()
    adquiridas = []
    fundo = bool(ti.get("run_in_background"))
    for ev in analisar(cmd, cwd):
        if ev.tipo == "deploy":
            prod = None
            if ev.sub == "coolify" and ev.alvo:
                prod = produto_por_nome(ev.alvo, cfg)
            if not prod:
                base = ev.dir or ev.cwd
                prod = produto_por_caminho(base, cfg) if base else None
            if not prod:
                log(f"deploy sem produto reconhecido: {cmd[:120]}")
                continue
            amb = "prod" if ev.remoto else "dev"
            msg = _tentar_deploy(
                prod, amb, d, "fundo" if fundo else "bash", cmd,
                cfg["prazo_deploy_fundo_min"] if fundo else cfg["prazo_deploy_min"],
                cfg, registradas, adquiridas,
            )
            if msg:
                for p, a in adquiridas:
                    _soltar_de_sessao(sid, p, a, d.get("tool_use_id", ""))
                return Resposta(negar=msg)
        elif ev.tipo == "git" and not ev.remoto:
            base = ev.dir or ev.cwd or cwd
            if not troca_de_branch(ev, base):
                continue
            arvore = resolver_arvore(base)
            outras = [v["nome"] for s, v in sessoes_ativas(cfg).items() if s != sid and v["arvore"] == arvore]
            if outras:
                log(f"negado git {ev.sub} em {arvore}: também em uso por {outras}")
                return Resposta(negar=(
                    f'[orquestra] A sessão "{outras[0]}" também está em {arvore}. '
                    "Trocar de branch aqui muda o HEAD dela. "
                    "Crie uma worktree: git worktree add <caminho> <branch>."
                ))
    return None


def _soltar_de_sessao(sid: str, prod: str, amb: str, tool_use_id: str) -> None:
    caminho = lock_path(prod, amb)
    atual = carregar_json(caminho)
    if isinstance(atual, dict) and atual.get("session_id") == sid and atual.get("tool_use_id") == tool_use_id:
        liberar_lock(caminho, atual, "negado em outro deploy do mesmo comando")


def h_pre_deploy(d: dict):
    cfg = config()
    sid = d.get("session_id") or ""
    ferramenta = d.get("tool_name") or ""
    ti = d.get("tool_input") or {}
    cwd = d.get("cwd") or os.getcwd()
    if not sid:
        return None
    if ferramenta == "Skill":
        nome_skill = str(ti.get("skill", "")).split(":")[-1]
        amb = cfg["skills_deploy"].get(nome_skill)
        if not amb:
            return None
        args = str(ti.get("args") or "").split()
        prod = None
        for a in args:
            prod = produto_por_nome(a.strip("\"'"), cfg)
            if prod:
                break
        prod = prod or produto_por_caminho(cwd, cfg)
        origem, prazo, desc = "skill", cfg["prazo_skill_min"], f"skill {nome_skill} {' '.join(args)}".strip()
    elif ferramenta in cfg["mcp_deploy"]:
        prod = produto_por_caminho(cwd, cfg)
        amb, origem, prazo, desc = "dev", "bash", cfg["prazo_deploy_min"], f"{ferramenta} {json.dumps(ti)[:100]}"
    else:
        return None
    if not prod:
        log(f"{ferramenta} sem produto reconhecido (cwd {cwd})")
        return None
    registradas = sessoes_registradas()
    msg = _tentar_deploy(prod, amb, d, origem, desc, prazo, cfg, registradas, [])
    return Resposta(negar=msg) if msg else None


def h_post(d: dict):
    ti = d.get("tool_input") or {}
    if ti.get("run_in_background"):
        return None
    sid, tuid = d.get("session_id") or "", d.get("tool_use_id") or ""
    if not sid or not tuid:
        return None
    for f in (estado() / "locks").glob("*.json"):
        atual = carregar_json(f)
        if isinstance(atual, dict) and atual.get("session_id") == sid and atual.get("origem") == "bash" and atual.get("tool_use_id") == tuid:
            liberar_lock(f, atual, "fim do comando")
    return None


def h_stop(d: dict):
    sid = d.get("session_id") or ""
    if not sid:
        return None
    for f in (estado() / "locks").glob("*.json"):
        atual = carregar_json(f)
        if isinstance(atual, dict) and atual.get("session_id") == sid and atual.get("origem") in ("bash", "skill"):
            liberar_lock(f, atual, "fim do turno")
    return None


def h_session_end(d: dict):
    """Sessão acabou: sai a reivindicação, os recados e qualquer trava dela."""
    sid = d.get("session_id") or ""
    if not sid:
        return None
    for f in (estado() / "locks").glob("*.json"):
        atual = carregar_json(f)
        if isinstance(atual, dict) and atual.get("session_id") == sid:
            liberar_lock(f, atual, "sessão encerrada")
    for p in (claim_path(sid), estado() / "avisos" / f"{seguro(sid)}.jsonl"):
        try:
            os.unlink(p)
        except OSError:
            pass
    return None


def h_pre_edit(d: dict):
    ti = d.get("tool_input") or {}
    p = ti.get("file_path") or ti.get("notebook_path")
    sid = d.get("session_id") or ""
    if not p or not sid:
        return None
    cwd = d.get("cwd") or os.getcwd()
    arq = os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))
    cfg = config()
    registradas = sessoes_registradas()
    meu = atualizar_claim(sid, cwd, registradas)
    meu["arquivos"][arq] = agora()
    avisos = []
    for f in (estado() / "claims").glob("*.json"):
        outro = carregar_json(f)
        if not isinstance(outro, dict) or outro.get("session_id") in (None, sid):
            continue
        osid = outro["session_id"]
        if not sessao_viva(osid, registradas, cfg):
            log(f"claim de sessão morta removida: {outro.get('nome')}")
            try:
                os.unlink(f)
            except OSError:
                pass
            continue
        ts = (outro.get("arquivos") or {}).get(arq)
        chave = f"{osid}|{arq}"
        if ts and chave not in meu["avisados"]:
            meu["avisados"].append(chave)
            avisos.append(
                f'Atenção: a sessão "{outro.get("nome", osid[:8])}" editou {arq} às {hora(ts)}. '
                "Releia o arquivo antes de alterar e confirme com o usuário se as mudanças se sobrepõem."
            )
            recado(osid, f'A sessão "{meu["nome"]}" também editou {arq} às {hora(agora())}.')
    gravar_claim(meu)
    if avisos:
        return Resposta(contexto="[orquestra] " + " ".join(avisos), evento="PreToolUse")
    return None


def h_prompt(d: dict):
    sid = d.get("session_id") or ""
    if not sid:
        return None
    registradas = sessoes_registradas()
    gravar_claim(atualizar_claim(sid, d.get("cwd") or "", registradas))
    itens = ler_recados(sid)
    if not itens:
        return None
    itens.sort(key=lambda i: i.get("t", 0), reverse=True)
    linhas, tam = [], 0
    for i in itens:
        l = f'- {hora(i.get("t", 0))} {i.get("texto", "")}'
        if tam + len(l) > 8000:
            break
        linhas.append(l)
        tam += len(l)
    return Resposta(contexto="[orquestra] Recados de outras sessões:\n" + "\n".join(linhas), evento="UserPromptSubmit")


HANDLERS = {
    "pre-bash": h_pre_bash, "pre-deploy": h_pre_deploy, "pre-edit": h_pre_edit,
    "post": h_post, "prompt": h_prompt, "stop": h_stop, "session-end": h_session_end,
}


def cmd_hook(nome: str) -> int:
    try:
        if (estado() / "DESLIGADO").exists():
            return 0
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
        if not isinstance(d, dict) or nome not in HANDLERS:
            return 0
        r = HANDLERS[nome](d)
    except Exception as e:  # noqa: BLE001 - falha aberta é requisito
        log(f"erro interno em {nome}: {e!r}")
        return 0
    if r is None:
        return 0
    if r.negar:
        sys.stderr.write(r.negar + "\n")
        return 2
    if r.contexto:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": r.evento, "additionalContext": r.contexto}}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# painel, sobreposição, liberar, status
# --------------------------------------------------------------------------

def _branch(arvore: str) -> str:
    rc, out = _git(["branch", "--show-current"], arvore)
    return out.strip() if rc == 0 and out.strip() else "-"


def dados_painel() -> dict:
    cfg = config()
    ativas = sessoes_ativas(cfg)
    grupos = {}
    for sid, v in ativas.items():
        prod = produto_por_caminho(v["arvore"], cfg) or "(sem produto)"
        grupos.setdefault(prod, {}).setdefault(v["arvore"], []).append(
            {"sid": sid, "nome": v["nome"], "status": v["status"], "kind": v["kind"]}
        )
    travas = []
    for f in sorted((estado() / "locks").glob("*.json")):
        t = carregar_json(f)
        if isinstance(t, dict):
            t["morta"] = lock_morto(t, sessoes_registradas(), cfg)
            travas.append(t)
    por_arquivo = {}
    for f in (estado() / "claims").glob("*.json"):
        c = carregar_json(f)
        if isinstance(c, dict) and c.get("session_id") in ativas:
            for arq in c.get("arquivos", {}):
                por_arquivo.setdefault(arq, []).append(c.get("nome", c["session_id"][:8]))
    comuns = {a: n for a, n in por_arquivo.items() if len(n) > 1}
    return {"sessoes": grupos, "travas": travas, "arquivos_em_comum": comuns}


def cmd_painel(args) -> int:
    p = dados_painel()
    if args.json:
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0
    print("SESSÕES VIVAS")
    if not p["sessoes"]:
        print("  nenhuma")
    for prod, arvores in sorted(p["sessoes"].items()):
        print(f"  {prod}")
        for arvore, ss in sorted(arvores.items()):
            print(f"    {arvore}  ({_branch(arvore)})")
            for s in ss:
                print(f"      {s['nome']}  [{s['status']}]")
    print("\nTRAVAS DE DEPLOY")
    if not p["travas"]:
        print("  nenhuma")
    for t in p["travas"]:
        marca = "  (morta, sai na próxima consulta)" if t["morta"] else ""
        exp = f", expira {hora(t['expira'])}" if t.get("expira") else ""
        print(f"  {t['produto']}.{t['ambiente']}  {t.get('nome')}  desde {hora(t.get('desde', 0))}{exp}{marca}")
        print(f"    {t.get('comando', '')[:100]}")
    print("\nARQUIVOS EDITADOS POR MAIS DE UMA SESSÃO")
    if not p["arquivos_em_comum"]:
        print("  nenhum")
    for a, n in sorted(p["arquivos_em_comum"].items()):
        print(f"  {a}  <- {', '.join(n)}")
    return 0


def _worktrees(repo: str) -> list:
    rc, out = _git(["worktree", "list", "--porcelain"], repo)
    if rc != 0:
        return []
    itens, cur = [], {}
    for linha in out.splitlines() + [""]:
        if not linha:
            if cur.get("worktree"):
                itens.append(cur)
            cur = {}
        elif " " in linha:
            k, v = linha.split(" ", 1)
            cur[k] = v
        else:
            cur[linha] = True
    return itens


def _arquivos_alterados(wt: str, base: str) -> set:
    rc, mb = _git(["merge-base", base, "HEAD"], wt)
    if rc != 0:
        return set()
    rc, out = _git(["diff", "--name-only", mb.strip()], wt)
    arqs = set(out.split()) if rc == 0 else set()
    rc, out = _git(["ls-files", "-o", "--exclude-standard"], wt)
    if rc == 0:
        arqs |= set(out.split())
    return arqs


def dados_sobreposicao(produto, com_prs: bool) -> dict:
    cfg = config()
    alvo = [produto] if produto else sorted(cfg["produtos"])
    saida = {"repos": [], "aviso": []}
    vistos = set()
    for prod in alvo:
        p = cfg["produtos"].get(prod)
        if p is None:
            saida["aviso"].append(f"produto desconhecido: {prod}")
            continue
        for pad in p.get("caminhos", []):
            for caminho in glob.glob(os.path.expanduser(pad)):
                if not os.path.isdir(caminho):
                    continue
                rc, comum = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], caminho)
                if rc != 0 or comum.strip() in vistos:
                    continue
                vistos.add(comum.strip())
                base = p.get("branch_base", "origin/main")
                fontes = {}
                for wt in _worktrees(caminho):
                    rotulo = (wt.get("branch") or "detached").replace("refs/heads/", "")
                    fontes[f"{rotulo} @ {wt['worktree']}"] = _arquivos_alterados(wt["worktree"], base)
                if com_prs:
                    try:
                        r = subprocess.run(
                            ["gh", "pr", "list", "--state", "open", "--limit", "50", "--json", "number,headRefName,files"],
                            capture_output=True, text=True, timeout=25, cwd=caminho,
                        )
                        if r.returncode == 0:
                            for pr in json.loads(r.stdout or "[]"):
                                fontes[f"PR #{pr['number']} ({pr['headRefName']})"] = {f["path"] for f in pr.get("files", [])}
                        else:
                            saida["aviso"].append("gh falhou; só dados locais")
                    except (OSError, subprocess.SubprocessError, ValueError):
                        saida["aviso"].append("gh indisponível; só dados locais")
                nomes = sorted(fontes)
                inter = []
                for i, a in enumerate(nomes):
                    for b in nomes[i + 1:]:
                        comuns = sorted(fontes[a] & fontes[b])
                        if comuns:
                            inter.append({"a": a, "b": b, "arquivos": comuns})
                saida["repos"].append({
                    "produto": prod, "repo": os.path.realpath(caminho), "base": base,
                    "alteracoes": {n: sorted(fontes[n]) for n in nomes if fontes[n]},
                    "intersecoes": inter,
                })
    return saida


def cmd_sobreposicao(args) -> int:
    s = dados_sobreposicao(args.produto, args.prs)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    for r in s["repos"]:
        print(f"{r['produto']}  {r['repo']}  (base {r['base']})")
        for n, arqs in r["alteracoes"].items():
            print(f"  {n}: {len(arqs)} arquivo(s)")
        if not r["intersecoes"]:
            print("  sem sobreposição")
        for i in r["intersecoes"]:
            print(f"  SOBREPÕE  {i['a']}  x  {i['b']}")
            for a in i["arquivos"][:20]:
                print(f"      {a}")
    for a in s["aviso"]:
        print(f"aviso: {a}")
    if not s["repos"] and not s["aviso"]:
        print("nenhum repositório encontrado (confira produtos.json)")
    return 0


def cmd_liberar(args) -> int:
    f = lock_path(*args.alvo.split(".", 1)) if "." in args.alvo else None
    if f is None or not f.exists():
        print(f"trava não encontrada: {args.alvo}", file=sys.stderr)
        return 1
    atual = carregar_json(f, {})
    liberar_lock(f, atual, f"manual: {args.motivo}")
    if atual.get("session_id"):
        recado(atual["session_id"], f"Sua trava de {args.alvo} foi liberada manualmente ({args.motivo}).")
    print(f"liberada: {args.alvo} (era de {atual.get('nome')})")
    return 0


def cmd_status(args) -> int:
    settings = carregar_json(Path(os.environ.get("ORQUESTRA_SETTINGS") or claude_dir() / "settings.json"), {})
    instalados = sorted({
        m.split(MARCA_HOOK)[1].split()[0]
        for ev in (settings.get("hooks") or {}).values() for g in ev for h in g.get("hooks", [])
        for m in [h.get("command", "")] if MARCA_HOOK in m
    })
    ultimas = []
    try:
        ultimas = (estado() / "orquestra.log").read_text(encoding="utf-8").splitlines()[-20:]
    except OSError:
        pass
    s = {
        "hooks_instalados": instalados,
        "hooks_faltando": sorted({h[2] for h in HOOKS} - set(instalados)),
        "desligado": (estado() / "DESLIGADO").exists(),
        "produtos_configurados": sorted(config()["produtos"]),
        "log": ultimas,
    }
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(f"hooks instalados: {', '.join(instalados) or 'nenhum'}")
    if s["hooks_faltando"]:
        print(f"hooks faltando: {', '.join(s['hooks_faltando'])}")
    print(f"DESLIGADO: {'sim' if s['desligado'] else 'não'}")
    print(f"produtos configurados: {', '.join(s['produtos_configurados']) or 'nenhum (edite produtos.json)'}")
    print("log:")
    for l in ultimas:
        print(f"  {l}")
    return 0


# --------------------------------------------------------------------------
# instalação dos hooks no settings.json
# --------------------------------------------------------------------------

def _settings_path() -> Path:
    return Path(os.environ.get("ORQUESTRA_SETTINGS") or claude_dir() / "settings.json")


def _tirar_nossos(hooks: dict) -> dict:
    novo = {}
    for ev, grupos in hooks.items():
        mantidos = []
        for g in grupos:
            hs = [h for h in g.get("hooks", []) if MARCA_HOOK not in h.get("command", "")]
            if hs:
                g = dict(g)
                g["hooks"] = hs
                mantidos.append(g)
            elif not g.get("hooks"):
                mantidos.append(g)
        if mantidos:
            novo[ev] = mantidos
    return novo


def cmd_configurar(args) -> int:
    caminho = _settings_path()
    if caminho.exists():
        settings = carregar_json(caminho)
        if not isinstance(settings, dict):
            print(f"{caminho} não é JSON válido; nada foi alterado.", file=sys.stderr)
            return 1
        bak = caminho.with_name(f"{caminho.name}.bak-orquestra-{time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'}")
        bak.write_bytes(caminho.read_bytes())
        print(f"backup: {bak}")
    else:
        settings = {}
    programa = os.path.abspath(args.programa or claude_dir() / "hooks" / "orquestra.py")
    hooks = _tirar_nossos(settings.get("hooks") or {})
    for ev, matcher, sub in HOOKS:
        grupo = {"hooks": [{"type": "command", "command": f"python3 {programa} hook {sub}", "timeout": 5}]}
        if matcher:
            grupo["matcher"] = matcher
        hooks.setdefault(ev, []).append(grupo)
    settings["hooks"] = hooks
    gravar_atomico(caminho, settings)
    print(f"hooks do Orquestra registrados em {caminho}")
    return 0


def cmd_remover(args) -> int:
    caminho = _settings_path()
    settings = carregar_json(caminho)
    if not isinstance(settings, dict):
        print(f"{caminho} ilegível ou ausente; nada a remover.", file=sys.stderr)
        return 1
    bak = caminho.with_name(f"{caminho.name}.bak-orquestra-{time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'}")
    bak.write_bytes(caminho.read_bytes())
    hooks = _tirar_nossos(settings.get("hooks") or {})
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    gravar_atomico(caminho, settings)
    print(f"hooks do Orquestra removidos de {caminho} (backup {bak.name}); estado preservado em {estado()}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="orquestra")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("nome", choices=sorted(HANDLERS))
    for nome in ("painel", "status"):
        s = sub.add_parser(nome)
        s.add_argument("--json", action="store_true")
    s = sub.add_parser("sobreposicao")
    s.add_argument("produto", nargs="?")
    s.add_argument("--prs", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("liberar")
    s.add_argument("alvo", help="produto.ambiente, ex.: exemplo.dev")
    s.add_argument("--motivo", required=True)
    s = sub.add_parser("configurar")
    s.add_argument("--programa", help="caminho do orquestra.py a registrar")
    sub.add_parser("remover")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return cmd_hook(args.nome)
    return {
        "painel": cmd_painel, "status": cmd_status, "sobreposicao": cmd_sobreposicao,
        "liberar": cmd_liberar, "configurar": cmd_configurar, "remover": cmd_remover,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
