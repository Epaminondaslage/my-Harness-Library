---
name: orquestra
description: Painel de coordenação entre sessões do Claude Code na mesma máquina. Use quando o usuário pedir "/orquestra", quiser saber quem está trabalhando onde, se há deploy em andamento, se duas branches/PRs mexem nos mesmos arquivos, ou pedir para liberar uma trava de deploy.
---

# Orquestra

As regras (trava de deploy, bloqueio de troca de branch em árvore compartilhada,
aviso de edição sobreposta, recados entre sessões) são aplicadas por **hooks**
que já rodam em todas as sessões. Esta skill é só o painel e a intervenção
manual. Todo o trabalho é feito pelo programa; rode e mostre a saída.

```bash
python3 ~/.claude/hooks/orquestra.py <subcomando>
```

| Pedido do usuário | Comando |
|---|---|
| quem está onde, travas, arquivos em comum | `painel` |
| branches/worktrees (e PRs) que mexem nos mesmos arquivos | `sobreposicao [produto]`, com `--prs` para incluir PRs abertos via `gh` |
| liberar uma trava | `liberar <produto>.<ambiente> --motivo "<texto>"` |
| saúde da instalação e últimas decisões | `status` |

`painel`, `sobreposicao` e `status` aceitam `--json`.

## Regras de uso

- **Nunca libere uma trava por conta própria.** `liberar` só a pedido explícito
  do usuário, depois de mostrar quem a segura (`painel`) e desde quando.
  Trava de sessão morta ou vencida sai sozinha na próxima consulta.
- Se um hook negou um comando seu, não tente contornar (renomear o comando,
  outro caminho, apagar arquivos do estado). Conte ao usuário o que a mensagem
  diz e espere ou proponha o caminho certo (aguardar o deploy; criar worktree).
- Para vigiar sobreposição de PR de forma contínua, use `/loop` sobre
  `/orquestra sobreposicao --prs`. Não há daemon.
- `~/.claude/orquestra/DESLIGADO` (arquivo vazio) desliga todos os hooks; só o
  usuário decide criá-lo ou removê-lo.
- Produtos, caminhos e prazos ficam em `~/.claude/orquestra/produtos.json`.
  Produto não listado ali não tem trava de deploy.
