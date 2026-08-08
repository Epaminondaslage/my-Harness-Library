#!/usr/bin/env python3
# =============================================================================
# api.py — read/write backend for My Harness Library
# -----------------------------------------------------------------------------
# Replaces the previous PHP endpoint. Standard library only: no pip install,
# no virtualenv, nothing vendored. Speaks JSON over a Unix socket; nginx
# proxies exactly one URL to it.
#
# Actions (all POST, JSON in / JSON out):
#   read     {file}                        -> content + mtime
#   save     {file, content, password, mtime}
#   create   {file, content, password}     -> new .md inside the allowed scope
#   history  {file}                        -> revision list (.bak)
#   revision {file, rev}                   -> content of one revision
#   restore  {file, rev, password}         -> roll the file back to a revision
#   status   {}                            -> last generation + queued request
#   regen    {password}                    -> queue a regeneration for cron
#   passwd   {password, nova}              -> change the write password
#
# Isolation is enforced by systemd, not by the interpreter: ProtectSystem,
# ProtectHome with a single ReadWritePaths, NoNewPrivileges, PrivateTmp and a
# syscall filter. See harness-library.service.
# =============================================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socketserver
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

MAX_BYTES = 1_048_576                  # 1 MB per file
BACKUP_DIRNAME = ".inventory-backups"
KEEP_REVISIONS = 10                    # revisions kept per file
ALLOWED_ROOTS = ("skills", "agents", "commands")

HOME = Path(os.environ.get("HOME", "")).expanduser()
CLAUDE = HOME / ".claude"
STATE = CLAUDE / ".inventory"
AUTH_FILE = STATE / "auth.hash"
AUDIT_FILE = STATE / "audit.log"
STATUS_FILE = STATE / "status.json"
REQUEST_FILE = STATE / "regen.request"

SOCKET_PATH = os.environ.get("HARNESS_SOCKET", "/run/harness-library/sock")


class ApiError(Exception):
    """Error carrying the HTTP status and the stable code the UI translates."""

    def __init__(self, status: int, message: str, code: str = "", **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message}
        if code:
            self.payload["code"] = code
        self.payload.update(extra)


# ---------------------------------------------------------------------------
# Password — scrypt from the standard library
# ---------------------------------------------------------------------------
# Stored as: scrypt$n$r$p$<salt hex>$<key hex>
# The old PHP backend used bcrypt, which the standard library cannot verify.
# A bcrypt hash is therefore reported as unusable and setup.sh asks for a new
# password once. Trading one re-entry for zero dependencies.

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(plain.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    parts = stored.strip().split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return False
    got = hashlib.scrypt(plain.encode(), salt=salt, n=n, r=r, p=p,
                         dklen=len(expected))
    return hmac.compare_digest(got, expected)      # constant time


def check_password(sent: str, ctx: tuple = ("?", "")) -> None:
    if not AUTH_FILE.is_file():
        raise ApiError(500, "Write password not configured on the server.")
    stored = AUTH_FILE.read_text(encoding="utf-8").strip()

    if stored.startswith("$2"):        # legacy bcrypt from the PHP backend
        raise ApiError(500, "Password stored in the old bcrypt format. "
                            "Run setup.sh to set it again.", "senha_legado")

    if not sent or not verify_password(sent, stored):
        time.sleep(0.4)                # slow down bulk guessing
        audit("senha-recusada", "", 0, *ctx)
        raise ApiError(401, "Senha incorreta.", "senha")


# ---------------------------------------------------------------------------
# Paths — the allowlist is the security boundary
# ---------------------------------------------------------------------------
def check_shape(rel: str) -> str:
    """Validate the shape of a relative path without requiring it to exist."""
    if not rel or "\0" in rel or rel.startswith("/") or ".." in rel:
        raise ApiError(400, "Caminho invalido.")
    if not rel.lower().endswith(".md"):
        raise ApiError(403, "Apenas arquivos .md sao editaveis.")
    first = rel.split("/")[0]
    if first not in ALLOWED_ROOTS:
        raise ApiError(403, "Fora do escopo editavel (skills, agents, commands).",
                       "escopo")
    return first


def resolve_target(rel: str) -> Path:
    """Absolute path of a file that MUST already exist, inside the allowlist."""
    first = check_shape(rel)
    try:
        target = (CLAUDE / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ApiError(404, "Arquivo nao encontrado.", "naoexiste")
    if not target.is_file():
        raise ApiError(404, "Arquivo nao encontrado.", "naoexiste")

    # Re-check after resolving: this is what catches a symlink pointing out.
    root = (CLAUDE / first).resolve()
    if not target.is_relative_to(root):
        raise ApiError(403, "Caminho resolvido fora do escopo permitido.", "escopo")
    return target


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------
FM_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(\r?\n|\Z)", re.DOTALL)


def validate_frontmatter(rel: str, content: str) -> None:
    """
    A skill or agent without `name` and `description` is silently ignored by
    Claude Code — the resource simply never loads. Refusing the save is the
    only way the author finds out at the time it matters.
    Commands are exempt: the format allows a bare file.
    """
    if rel.split("/")[0] == "commands":
        return

    m = FM_RE.match(content)
    if not m:
        raise ApiError(422, "Frontmatter ausente. O arquivo precisa comecar com "
                            "um bloco --- ... --- contendo name e description.",
                       "fm_ausente")
    yaml = m.group(1)
    missing = [k for k in ("name", "description")
               if not re.search(rf"^{k}:\s*(\S.*)$", yaml, re.MULTILINE | re.IGNORECASE)]
    if missing:
        raise ApiError(422, "Frontmatter incompleto: falta " + " e ".join(missing) +
                            ". Sem esse campo o Claude Code nao carrega o recurso.",
                       "fm_falta", faltando=missing)


# ---------------------------------------------------------------------------
# Revisions and audit
# ---------------------------------------------------------------------------
def backup_dir(target: Path) -> Path:
    return target.parent / BACKUP_DIRNAME


def revisions(target: Path) -> list[dict]:
    """Existing revisions, newest first."""
    d = backup_dir(target)
    if not d.is_dir():
        return []
    prefix = target.name + "."
    out = []
    for f in d.iterdir():
        if f.is_file() and f.name.startswith(prefix) and f.name.endswith(".bak"):
            st = f.stat()
            out.append({"rev": f.name, "mtime": int(st.st_mtime), "bytes": st.st_size})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def prune(target: Path) -> None:
    for r in revisions(target)[KEEP_REVISIONS:]:
        try:
            (backup_dir(target) / r["rev"]).unlink()
        except OSError:
            pass


def backup(target: Path) -> None:
    """Keep a dated copy of the current content, then prune the excess."""
    d = backup_dir(target)
    try:
        d.mkdir(mode=0o755, parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (d / f"{target.name}.{stamp}.bak").write_bytes(target.read_bytes())
    except OSError:
        return                          # backup is extra defence, never blocking
    prune(target)


def resolve_revision(target: Path, rev: str) -> Path:
    if "/" in rev or "\0" in rev:
        raise ApiError(400, "Revisao invalida.")
    if any(r["rev"] == rev for r in revisions(target)):
        return backup_dir(target) / rev
    raise ApiError(404, "Revisao nao encontrada.")


def audit(action: str, file: str, size: int, client: str = "?", ua: str = "", **extra) -> None:
    """
    Append-only trail. This tool writes into a directory Claude Code later
    executes, so every write leaves a record.
    """
    line = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "action": action, "file": file, "bytes": size, "ip": client,
        "ua": ua[:120],
    }
    line.update(extra)
    try:
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def write_atomic(target: Path, content: str) -> None:
    """Temporary file in the same directory, then rename — never a half file."""
    tmp = target.with_name(target.name + ".tmp-" + secrets.token_hex(6))
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise ApiError(500, "Falha ao gravar o arquivo.")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def act_read(req, ctx) -> dict:
    target = resolve_target(req.get("file", ""))
    st = target.stat()
    return {
        "file": req["file"],
        "content": target.read_text(encoding="utf-8", errors="replace"),
        "mtime": int(st.st_mtime),
        "writable": os.access(target, os.W_OK),
        "revisions": len(revisions(target)),
    }


def act_history(req, ctx) -> dict:
    target = resolve_target(req.get("file", ""))
    return {"file": req["file"], "revisions": revisions(target), "keep": KEEP_REVISIONS}


def act_revision(req, ctx) -> dict:
    target = resolve_target(req.get("file", ""))
    path = resolve_revision(target, str(req.get("rev", "")))
    return {"file": req["file"], "rev": req["rev"],
            "content": path.read_text(encoding="utf-8", errors="replace")}


def act_save(req, ctx) -> dict:
    check_password(str(req.get("password", "")), ctx)
    rel = req.get("file", "")
    target = resolve_target(rel)
    content = str(req.get("content", ""))

    if len(content.encode()) > MAX_BYTES:
        raise ApiError(413, "Conteudo acima de 1 MB.")
    if not os.access(target, os.W_OK):
        raise ApiError(403, "Arquivo sem permissao de escrita.")

    # The browser sends the mtime it read: this detects a concurrent edit.
    known = req.get("mtime")
    if isinstance(known, int) and int(target.stat().st_mtime) != known:
        raise ApiError(409, "O arquivo mudou no disco desde que voce abriu. "
                            "Recarregue antes de salvar.", "conflito")

    validate_frontmatter(rel, content)
    backup(target)
    write_atomic(target, content)
    audit("save", rel, len(content.encode()), *ctx)
    return {"ok": True, "file": rel, "mtime": int(target.stat().st_mtime),
            "bytes": len(content.encode()), "revisions": len(revisions(target))}


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def act_create(req, ctx) -> dict:
    check_password(str(req.get("password", "")), ctx)
    rel = req.get("file", "")
    first = check_shape(rel)
    content = str(req.get("content", ""))

    if len(content.encode()) > MAX_BYTES:
        raise ApiError(413, "Conteudo acima de 1 MB.")

    # Directories must be plain slugs; the file follows the convention of its
    # type — a skill is always SKILL.md, everything else is <slug>.md.
    segs = rel.split("/")
    leaf = segs.pop()
    for seg in segs:
        if not SLUG_RE.match(seg):
            raise ApiError(400, f'Nome de pasta invalido: "{seg}". '
                                "Use minusculas, numeros e hifen.")
    if first == "skills":
        if leaf != "SKILL.md":
            raise ApiError(400, "Skill precisa terminar em <nome>/SKILL.md.")
        if len(segs) != 2:
            raise ApiError(400, "Skill vai em skills/<nome>/SKILL.md.")
    elif not re.match(r"^[a-z0-9][a-z0-9._-]*\.md$", leaf):
        raise ApiError(400, f'Nome invalido: "{leaf}". Use minusculas, numeros e hifen.')

    target = CLAUDE / rel
    if target.exists():
        raise ApiError(409, "Ja existe um arquivo nesse caminho.", "existe")

    validate_frontmatter(rel, content)
    try:
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError:
        raise ApiError(500, "Falha ao criar o diretorio.")

    # Confirm the directory we just created really sits under the allowed root.
    root = (CLAUDE / first).resolve()
    if not target.parent.resolve().is_relative_to(root):
        raise ApiError(403, "Caminho resolvido fora do escopo permitido.", "escopo")

    write_atomic(target, content)
    audit("create", rel, len(content.encode()), *ctx)
    return {"ok": True, "file": rel, "mtime": int(target.stat().st_mtime),
            "bytes": len(content.encode())}


def act_restore(req, ctx) -> dict:
    check_password(str(req.get("password", "")), ctx)
    rel = req.get("file", "")
    target = resolve_target(rel)
    rev = str(req.get("rev", ""))
    content = resolve_revision(target, rev).read_text(encoding="utf-8", errors="replace")

    validate_frontmatter(rel, content)
    backup(target)                       # the current state becomes a revision too
    write_atomic(target, content)
    audit("restore", rel, len(content.encode()), *ctx, rev=rev)
    return {"ok": True, "file": rel, "mtime": int(target.stat().st_mtime),
            "bytes": len(content.encode()), "content": content}


def act_status(req, ctx) -> dict:
    data = None
    if STATUS_FILE.is_file():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
    return {"status": data, "pending": REQUEST_FILE.is_file()}


def act_regen(req, ctx) -> dict:
    # This service cannot spawn processes: the systemd unit forbids it. Instead
    # it drops a request file that the one-minute cron consumes. The cost is up
    # to 60s of latency; the gain is a web-facing process that can never exec.
    check_password(str(req.get("password", "")), ctx)
    try:
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        REQUEST_FILE.write_text(datetime.now().isoformat() + "\n", encoding="utf-8")
    except OSError:
        raise ApiError(500, "Nao foi possivel registrar o pedido.")
    audit("regen-pedido", "", 0, *ctx)
    return {"ok": True, "queued": True,
            "msg": "Pedido registrado. A regeneracao roda em ate 60 segundos."}


def act_passwd(req, ctx) -> dict:
    check_password(str(req.get("password", "")), ctx)
    nova = str(req.get("nova", ""))
    if len(nova) < 8:
        raise ApiError(422, "A senha nova precisa de pelo menos 8 caracteres.")
    if nova == str(req.get("password", "")):
        raise ApiError(422, "A senha nova e igual a atual.")

    try:
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = AUTH_FILE.with_name(AUTH_FILE.name + ".tmp-" + secrets.token_hex(6))
        tmp.write_text(hash_password(nova) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, AUTH_FILE)
    except OSError:
        raise ApiError(500, "Falha ao gravar a senha nova.")
    audit("senha-trocada", "", 0, *ctx)
    return {"ok": True, "msg": "Senha trocada."}


ACTIONS = {
    "read": act_read, "history": act_history, "revision": act_revision,
    "save": act_save, "create": act_create, "restore": act_restore,
    "status": act_status, "regen": act_regen, "passwd": act_passwd,
}


# ---------------------------------------------------------------------------
# HTTP over a Unix socket
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HarnessLibrary"
    sys_version = ""                    # do not advertise the Python version

    def log_message(self, fmt, *args):  # journald already has what matters
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(405, {"error": "Use POST."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BYTES + 4096:
            self._send(413, {"error": "Requisicao acima do limite."})
            return

        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send(400, {"error": "JSON invalido."})
            return

        # nginx forwards the real client; the socket peer tells us nothing.
        ctx = (self.headers.get("X-Real-IP", "?"),
               self.headers.get("User-Agent", ""))

        fn = ACTIONS.get(str(req.get("action", "")))
        if fn is None:
            self._send(400, {"error": "Acao desconhecida."})
            return

        try:
            self._send(200, fn(req, ctx))
        except ApiError as e:
            self._send(e.status, e.payload)
        except Exception as e:                       # never leak a traceback
            print(f"unhandled: {e!r}", file=sys.stderr, flush=True)
            self._send(500, {"error": "Erro interno."})


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def get_request(self):
        # BaseHTTPRequestHandler expects an (address, port) tuple; a Unix
        # socket gives an empty string. Substitute a placeholder.
        conn, _ = super().get_request()
        return conn, ("unix", 0)


def main() -> int:
    if not HOME or not CLAUDE.is_dir():
        print(f"~/.claude not found (HOME={HOME})", file=sys.stderr)
        return 1

    sock = Path(SOCKET_PATH)
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.unlink(missing_ok=True)

    server = Server(str(sock), Handler)
    # nginx (www-data) must be able to connect. The unit runs with
    # Group=www-data, so group access on the socket is enough.
    os.chmod(sock, 0o660)

    print(f"listening on {sock} as uid={os.getuid()} gid={os.getgid()}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        sock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
