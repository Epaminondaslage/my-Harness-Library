import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

AQUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AQUI))
import orquestra as o  # noqa: E402

PROGRAMA = str(AQUI / "orquestra.py")
VIVO = os.getpid()
MORTO = 2 ** 22 + 12345  # acima de pid_max padrão; /proc/<pid> não existe


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd)] + list(a), check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(os.path.realpath(self.tmp.name))
        self.claude = self.raiz / "claude"
        (self.claude / "sessions").mkdir(parents=True)
        os.environ["ORQUESTRA_CLAUDE_DIR"] = str(self.claude)
        os.environ["ORQUESTRA_SETTINGS"] = str(self.claude / "settings.json")
        self.app = self.raiz / "opt" / "app"
        self.app.mkdir(parents=True)
        git(self.app, "init", "-q", "-b", "main")
        (self.app / "a.txt").write_text("a")
        git(self.app, "add", ".")
        git(self.app, "commit", "-q", "-m", "i")
        self.config({"produtos": {"app": {"caminhos": [str(self.app), str(self.raiz / "wt" / "app-*")],
                                           "coolify": ["app", "app-backend"], "branch_base": "main"}}})

    def tearDown(self):
        os.environ.pop("ORQUESTRA_CLAUDE_DIR", None)
        os.environ.pop("ORQUESTRA_SETTINGS", None)

    def config(self, cfg):
        (self.claude / "orquestra").mkdir(parents=True, exist_ok=True)
        (self.claude / "orquestra" / "produtos.json").write_text(json.dumps(cfg))

    def sessao(self, sid, nome, cwd=None, pid=VIVO):
        (self.claude / "sessions" / f"{pid}-{sid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "name": nome, "cwd": str(cwd or self.app), "status": "idle", "kind": "interactive"}))

    def payload(self, sid, cmd, cwd=None, fundo=False, tuid="t1"):
        ti = {"command": cmd}
        if fundo:
            ti["run_in_background"] = True
        return {"session_id": sid, "cwd": str(cwd or self.app), "tool_name": "Bash", "tool_input": ti, "tool_use_id": tuid}

    def rodar(self, nome, d):
        p = subprocess.run([sys.executable, PROGRAMA, "hook", nome], input=json.dumps(d), capture_output=True, text=True,
                           env=os.environ.copy())
        return p.returncode, p.stdout, p.stderr


class Analise(unittest.TestCase):
    def tipos(self, cmd, cwd="/x"):
        return [(e.tipo, e.sub, e.alvo, e.remoto) for e in o.analisar(cmd, cwd)]

    def test_deploy_reconhecido(self):
        casos = {
            "coolify deploy suporteone": [("deploy", "coolify", "suporteone", False)],
            "rtk coolify deploy suporteone 2>&1 | tail -3": [("deploy", "coolify", "suporteone", False)],
            "cd /opt/x && docker compose up -d --build": [("deploy", "compose", None, False)],
            "docker-compose -f a.yml restart web": [("deploy", "compose", None, False)],
            "FOO=1 docker compose build": [("deploy", "compose", None, False)],
            "bash -c 'coolify deploy x'": [("deploy", "coolify", "x", False)],
            "ssh root@h 'cd /opt/x && docker compose up -d'": [("deploy", "compose", None, True)],
            "pct exec 101 -- docker compose restart": [("deploy", "compose", None, True)],
        }
        for cmd, esperado in casos.items():
            self.assertEqual(self.tipos(cmd), esperado, cmd)

    def test_nao_deploy(self):
        for cmd in ["docker compose ps", "docker compose logs -f", "echo 'coolify deploy x'",
                    "git commit -m 'docker compose up e coolify deploy'", "coolify ps suporteone",
                    "grep deploy README.md", "ls compose", "docker compose down"]:
            self.assertEqual(self.tipos(cmd), [], cmd)

    def test_cd_muda_cwd(self):
        ev = o.analisar("cd /opt/y && docker compose up", "/x")[0]
        self.assertEqual(ev.cwd, "/opt/y")

    def test_git_eventos(self):
        self.assertEqual(self.tipos("git checkout main"), [("git", "checkout", None, False)])
        self.assertEqual(self.tipos("git -C /r switch -c n")[0][:2], ("git", "switch"))
        self.assertEqual(o.analisar("git -C /r checkout x", "/x")[0].dir, "/r")
        self.assertEqual(self.tipos("git status"), [])

    def test_aspas_desbalanceadas_nao_quebra(self):
        self.assertEqual(self.tipos("echo 'abc"), [])


class TrocaDeBranch(Base):
    def ev(self, cmd):
        return o.analisar(cmd, str(self.app))[0]

    def test_troca(self):
        git(self.app, "branch", "feat")
        for cmd in ["git checkout feat", "git checkout -b nova", "git switch feat", "git checkout -", "git checkout --detach", "git checkout main"]:
            self.assertTrue(o.troca_de_branch(self.ev(cmd), str(self.app)), cmd)

    def test_restauracao_nao_e_troca(self):
        for cmd in ["git checkout -- a.txt", "git checkout main -- a.txt", "git checkout a.txt", "git checkout -p", "git checkout HEAD a.txt", "git checkout"]:
            self.assertFalse(o.troca_de_branch(self.ev(cmd), str(self.app)), cmd)


class Deploy(Base):
    def test_primeiro_passa_segundo_nega(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.assertEqual(self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))[0], 0)
        rc, _, err = self.rodar("pre-bash", self.payload("s2", "coolify deploy app-backend", tuid="t2"))
        self.assertEqual(rc, 2)
        self.assertIn('"um"', err)
        self.assertIn("app (dev)", err)

    def test_mesma_sessao_reentra(self):
        self.sessao("s1", "um")
        self.assertEqual(self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))[0], 0)
        self.assertEqual(self.rodar("pre-bash", self.payload("s1", "docker compose up -d", tuid="t2"))[0], 0)

    def test_post_solta_so_o_proprio_comando(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", tuid="t1"))
        self.rodar("post", {**self.payload("s1", "coolify deploy app", tuid="OUTRO"), "tool_use_id": "OUTRO"})
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t3"))[0], 2)
        self.rodar("post", self.payload("s1", "coolify deploy app", tuid="t1"))
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t3"))[0], 0)

    def test_fundo_nao_solta_no_post(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", fundo=True))
        self.rodar("post", self.payload("s1", "coolify deploy app", fundo=True))
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 2)

    def test_fundo_vence_pelo_prazo(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", fundo=True))
        f = o.lock_path("app", "dev")
        t = json.loads(f.read_text())
        t["expira"] = time.time() - 1
        f.write_text(json.dumps(t))
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_sessao_morta_libera(self):
        self.sessao("s1", "um", pid=MORTO)
        self.sessao("s2", "dois")
        o.estado().joinpath("locks").mkdir(parents=True, exist_ok=True)
        o.lock_path("app", "dev").write_text(json.dumps(
            {"produto": "app", "ambiente": "dev", "session_id": "s1", "nome": "um", "origem": "bash",
             "desde": time.time(), "expira": time.time() + 900}))
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app"))[0], 0)
        self.assertIn("morta", (o.estado() / "orquestra.log").read_text())

    def test_stop_solta_bash_e_skill_mas_nao_fundo(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", fundo=True))
        self.rodar("stop", {"session_id": "s1"})
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 2)
        self.rodar("post", {"session_id": "s1"})
        o.liberar_lock(o.lock_path("app", "dev"), json.loads(o.lock_path("app", "dev").read_text()), "teste")
        self.rodar("pre-bash", self.payload("s1", "docker compose up -d"))
        self.rodar("stop", {"session_id": "s1"})
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "docker compose up -d", tuid="t2"))[0], 0)

    def test_produto_e_ambiente_separados(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "ssh root@h 'cd /opt/app && docker compose up -d'"))
        # prod não tem caminho local: sem produto reconhecido, não trava
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_skill_trava_ate_o_stop(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        sk = {"session_id": "s1", "cwd": str(self.app), "tool_name": "Skill", "tool_input": {"skill": "tarefa-finalizada", "args": "app"}, "tool_use_id": "k1"}
        self.assertEqual(self.rodar("pre-deploy", sk)[0], 0)
        # coolify deploy da própria sessão dentro da skill não solta a trava
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", tuid="b1"))
        self.rodar("post", self.payload("s1", "coolify deploy app", tuid="b1"))
        rc, _, err = self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))
        self.assertEqual(rc, 2)
        self.assertIn("tarefa-finalizada", err)
        self.rodar("stop", {"session_id": "s1"})
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_skill_outra_ou_sem_produto_passa(self):
        self.sessao("s1", "um")
        for ti in [{"skill": "caveman:caveman", "args": ""}, {"skill": "deploy-prod", "args": "desconhecido"}]:
            d = {"session_id": "s1", "cwd": "/tmp", "tool_name": "Skill", "tool_input": ti, "tool_use_id": "k"}
            self.assertEqual(self.rodar("pre-deploy", d)[0], 0)

    def test_mcp_deploy_usa_cwd(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        mcp = lambda sid, t: {"session_id": sid, "cwd": str(self.app), "tool_name": "mcp__coolify__deploy", "tool_input": {"tag_or_uuid": "abc"}, "tool_use_id": t}
        self.assertEqual(self.rodar("pre-deploy", mcp("s1", "m1"))[0], 0)
        self.assertEqual(self.rodar("pre-deploy", mcp("s2", "m2"))[0], 2)
        self.rodar("post", {**mcp("s1", "m1")})
        self.assertEqual(self.rodar("pre-deploy", mcp("s2", "m2"))[0], 0)

    def test_corrida_so_um_vence(self):
        for i in range(6):
            self.sessao(f"s{i}", f"n{i}")
        procs = [subprocess.Popen([sys.executable, PROGRAMA, "hook", "pre-bash"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=os.environ.copy()) for _ in range(6)]
        for i, p in enumerate(procs):
            p.stdin.write(json.dumps(self.payload(f"s{i}", "coolify deploy app", tuid=f"t{i}")))
            p.stdin.close()
        codigos = sorted(p.wait() for p in procs)
        self.assertEqual(codigos, [0, 2, 2, 2, 2, 2])

    def test_recados_do_bloqueio_e_da_liberacao(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))
        self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))
        rc, out, _ = self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})
        self.assertIn("tentou deployar app", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.rodar("post", self.payload("s1", "coolify deploy app"))
        rc, out, _ = self.rodar("prompt", {"session_id": "s2", "cwd": str(self.app)})
        self.assertIn("foi liberado", json.loads(out)["hookSpecificOutput"]["additionalContext"])


class FimDeSessao(Base):
    def test_session_end_limpa_tudo_da_sessao(self):
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app", fundo=True))
        self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})
        o.recado("s1", "pendente")
        self.assertTrue(o.claim_path("s1").exists())
        self.assertEqual(self.rodar("session-end", {"session_id": "s1"})[0], 0)
        self.assertFalse(o.claim_path("s1").exists())
        self.assertFalse(o.lock_path("app", "dev").exists())
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_sessao_encerrada_nao_bloqueia_checkout(self):
        git(self.app, "branch", "feat")
        self.sessao("s2", "dois")
        self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})  # headless: só claim
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "git checkout feat"))[0], 2)
        self.rodar("session-end", {"session_id": "s1"})
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "git checkout feat"))[0], 0)


class Checkout(Base):
    def test_nega_com_outra_sessao_na_arvore(self):
        git(self.app, "branch", "feat")
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        rc, _, err = self.rodar("pre-bash", self.payload("s2", "git checkout feat"))
        self.assertEqual(rc, 2)
        self.assertIn("worktree", err)

    def test_sozinho_passa(self):
        git(self.app, "branch", "feat")
        self.sessao("s2", "dois")
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "git checkout feat"))[0], 0)

    def test_restaurar_arquivo_passa(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "git checkout -- a.txt"))[0], 0)

    def test_outra_arvore_passa(self):
        git(self.app, "branch", "feat")
        wt = self.raiz / "wt" / "app-x"
        git(self.app, "worktree", "add", "-q", str(wt), "-b", "x")
        self.sessao("s1", "um", cwd=wt)
        self.sessao("s2", "dois")
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "git checkout feat"))[0], 0)


class Edicao(Base):
    def edit(self, sid, arq):
        return self.rodar("pre-edit", {"session_id": sid, "cwd": str(self.app), "tool_name": "Edit", "tool_input": {"file_path": str(arq)}})

    def test_avisa_uma_vez_e_nao_bloqueia(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        arq = self.app / "a.txt"
        self.assertEqual(self.edit("s1", arq), (0, "", ""))
        rc, out, _ = self.edit("s2", arq)
        self.assertEqual(rc, 0)
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", ctx)
        self.assertIn('"um"', ctx["additionalContext"])
        self.assertEqual(self.edit("s2", arq)[1], "")

    def test_a_propria_sessao_nao_se_avisa(self):
        self.sessao("s1", "um")
        self.edit("s1", self.app / "a.txt")
        self.assertEqual(self.edit("s1", self.app / "a.txt")[1], "")

    def test_outro_arquivo_nao_avisa(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.edit("s1", self.app / "a.txt")
        self.assertEqual(self.edit("s2", self.app / "b.txt")[1], "")

    def test_worktrees_diferentes_nao_avisam(self):
        wt = self.raiz / "wt" / "app-x"
        git(self.app, "worktree", "add", "-q", str(wt), "-b", "x")
        self.sessao("s1", "um")
        self.sessao("s2", "dois", cwd=wt)
        self.edit("s1", self.app / "a.txt")
        self.assertEqual(self.edit("s2", wt / "a.txt")[1], "")

    def test_sessao_morta_nao_avisa_e_claim_sai(self):
        self.sessao("s1", "um", pid=MORTO)
        self.sessao("s2", "dois")
        self.edit("s1", self.app / "a.txt")
        os.utime(o.claim_path("s1"), (0, 0))
        self.assertEqual(self.edit("s2", self.app / "a.txt")[1], "")
        self.assertFalse(o.claim_path("s1").exists())

    def test_notebook_e_relativo(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.edit("s1", self.app / "n.ipynb")
        rc, out, _ = self.rodar("pre-edit", {"session_id": "s2", "cwd": str(self.app), "tool_name": "NotebookEdit", "tool_input": {"notebook_path": "n.ipynb"}})
        self.assertIn("n.ipynb", out)


class Recados(Base):
    def test_entrega_e_esvazia(self):
        o.recado("s1", "oi")
        rc, out, _ = self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})[1], "")

    def test_truncamento(self):
        for i in range(400):
            o.recado("s1", f"recado {i} " + "x" * 50)
        out = self.rodar("prompt", {"session_id": "s1", "cwd": str(self.app)})[1]
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(ctx), 8200)
        self.assertIn("recado 399", ctx)


class FalhaAberta(Base):
    def test_desligado(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))
        (o.estado() / "DESLIGADO").write_text("")
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_estado_corrompido(self):
        self.sessao("s1", "um")
        (o.estado() / "locks").mkdir(parents=True, exist_ok=True)
        o.lock_path("app", "dev").write_text("{nao é json")
        self.assertEqual(self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))[0], 0)
        (self.claude / "orquestra" / "claims").mkdir(exist_ok=True)
        o.claim_path("s1").write_text("lixo")
        self.assertEqual(self.rodar("pre-edit", {"session_id": "s1", "cwd": str(self.app), "tool_input": {"file_path": "x"}})[0], 0)

    def test_sem_produtos_json(self):
        (self.claude / "orquestra" / "produtos.json").unlink()
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))
        self.assertEqual(self.rodar("pre-bash", self.payload("s2", "coolify deploy app", tuid="t2"))[0], 0)

    def test_produtos_json_invalido(self):
        (self.claude / "orquestra" / "produtos.json").write_text("{")
        self.assertEqual(self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))[0], 0)

    def test_entrada_invalida_e_excecao_interna(self):
        for entrada in ["", "não é json", "[]", "{}"]:
            p = subprocess.run([sys.executable, PROGRAMA, "hook", "pre-bash"], input=entrada, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, entrada)
        # exceção interna: tool_input com tipo errado
        self.assertEqual(self.rodar("pre-bash", {"session_id": "s", "tool_input": {"command": 5}})[0], 0)
        self.assertIn("erro interno", (o.estado() / "orquestra.log").read_text())


class Painel(Base):
    def test_painel_e_liberar(self):
        self.sessao("s1", "um")
        self.sessao("s2", "dois")
        self.rodar("pre-bash", self.payload("s1", "coolify deploy app"))
        p = subprocess.run([sys.executable, PROGRAMA, "painel", "--json"], capture_output=True, text=True)
        d = json.loads(p.stdout)
        self.assertEqual(sorted(s["nome"] for s in d["sessoes"]["app"][str(self.app)]), ["dois", "um"])
        self.assertEqual(d["travas"][0]["produto"], "app")
        p = subprocess.run([sys.executable, PROGRAMA, "liberar", "app.dev", "--motivo", "teste"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(o.lock_path("app", "dev").exists())
        self.assertEqual(subprocess.run([sys.executable, PROGRAMA, "liberar", "app.dev", "--motivo", "x"], capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run([sys.executable, PROGRAMA, "painel"], capture_output=True).returncode, 0)

    def test_sobreposicao_entre_worktrees(self):
        wt = self.raiz / "wt" / "app-x"
        git(self.app, "worktree", "add", "-q", str(wt), "-b", "x")
        (wt / "a.txt").write_text("mudou em x")
        git(wt, "commit", "-qam", "x")
        (self.app / "a.txt").write_text("mudou no main (não commitado)")
        (self.app / "so-aqui.txt").write_text("novo")
        s = o.dados_sobreposicao("app", False)
        self.assertEqual(len(s["repos"]), 1)
        inter = s["repos"][0]["intersecoes"]
        self.assertEqual(len(inter), 1)
        self.assertEqual(inter[0]["arquivos"], ["a.txt"])


class Instalacao(Base):
    def test_configurar_idempotente_e_preserva(self):
        existente = {"theme": "dark", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}],
                                                 "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "x"}]}]}}
        sp = Path(os.environ["ORQUESTRA_SETTINGS"])
        sp.write_text(json.dumps(existente))
        for _ in range(2):
            self.assertEqual(subprocess.run([sys.executable, PROGRAMA, "configurar", "--programa", "/x/orquestra.py"], capture_output=True).returncode, 0)
        s = json.loads(sp.read_text())
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["hooks"]["SessionStart"], existente["hooks"]["SessionStart"])
        cmds = [h["command"] for g in s["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(cmds.count("rtk hook claude"), 1)
        self.assertEqual(len([c for c in cmds if "orquestra.py hook" in c]), 3)
        self.assertEqual(len(s["hooks"]["Stop"]), 1)
        baks = list(sp.parent.glob("settings.json.bak-orquestra-*"))
        self.assertEqual(len(baks), 2)
        self.assertIn(json.dumps(existente), [b.read_text() for b in baks])  # o original sobrevive
        st = subprocess.run([sys.executable, PROGRAMA, "status", "--json"], capture_output=True, text=True)
        self.assertEqual(json.loads(st.stdout)["hooks_faltando"], [])

    def test_remover_volta_ao_original(self):
        existente = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]}}
        sp = Path(os.environ["ORQUESTRA_SETTINGS"])
        sp.write_text(json.dumps(existente))
        subprocess.run([sys.executable, PROGRAMA, "configurar"], check=True, capture_output=True)
        subprocess.run([sys.executable, PROGRAMA, "remover"], check=True, capture_output=True)
        self.assertEqual(json.loads(sp.read_text()), existente)

    def test_settings_invalido_nao_e_tocado(self):
        sp = Path(os.environ["ORQUESTRA_SETTINGS"])
        sp.write_text("{quebrado")
        p = subprocess.run([sys.executable, PROGRAMA, "configurar"], capture_output=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(sp.read_text(), "{quebrado")


if __name__ == "__main__":
    unittest.main()
