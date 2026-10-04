# Orquestra: coordenação entre sessões do Claude Code na mesma máquina

Data: 2026-10-04 · Estado: implementado (ver "Medições")

## Problema

Várias sessões do Claude Code rodam ao mesmo tempo na mesma máquina, muitas no
mesmo diretório ou em worktrees do mesmo repositório. Elas não se enxergam.
Três acidentes se repetem:

1. Duas sessões disparam deploy do mesmo produto ao mesmo tempo e uma derruba
   o build da outra.
2. Uma sessão troca de branch (`git checkout`) num diretório em que outra
   sessão está trabalhando; o HEAD é compartilhado e a outra passa a editar a
   branch errada.
3. Duas sessões editam os mesmos arquivos, na mesma árvore ou em branches
   diferentes, e o conflito só aparece no PR.

## Objetivo

Toda sessão fica sabendo, antes de agir, que outra sessão viva já ocupa
aquela área. Deploy concorrente e troca de branch em diretório compartilhado
são **negados**. Edição sobreposta gera **aviso**, sem bloqueio.

Critério de sucesso: com duas sessões abertas no mesmo produto, a segunda não
consegue iniciar um deploy enquanto o da primeira está em andamento, não
consegue trocar a branch do diretório compartilhado, e recebe aviso ao editar
um arquivo que a primeira já editou.

## Restrição que define a arquitetura

Uma skill só age dentro da sessão que a invocou. O que alcança todas as
sessões são hooks de nível de usuário (`~/.claude/settings.json`), que o
harness executa sem depender de o modelo lembrar. Portanto:

- **hooks** aplicam as regras, sempre ligados, em todas as sessões;
- a **skill `/orquestra`** é o painel e o ponto de intervenção manual.

Fatos do harness que o desenho assume (documentação de hooks):

- `PreToolUse` com `exit 2` nega a chamada e nenhum outro hook desfaz; a
  mensagem em stderr vai para o modelo.
- Hooks do mesmo matcher rodam em paralelo sobre a entrada original. O hook
  do Orquestra não depende do hook `rtk` nem o altera.
- Hook que quebra, estoura o tempo ou sai com código diferente de 2 não
  bloqueia.
- `PreToolUse` pode devolver `additionalContext` sem bloquear.
- `UserPromptSubmit` injeta contexto a cada turno (limite de 10.000
  caracteres).
- Todo hook recebe `session_id` e `cwd` em stdin; hooks rodam também para
  chamadas de subagentes, com o `session_id` da sessão dona.

## Medições

Três pontos não eram documentados e foram medidos com uma sessão `claude -p`
descartável (hooks de sondagem que gravam o stdin cru), em 04/10/2026,
Claude Code 2.1.28x:

| Ponto | Medido | Efeito no desenho |
|---|---|---|
| `tool_input` da ferramenta `Skill` | `{"skill": "<nome>", "args": "<texto>"}`; `PreToolUse` e `PostToolUse` disparam | usado como previsto |
| `PostToolUse` de Bash com `run_in_background` | dispara **na hora do lançamento** (`duration_ms` ínfimo), não no fim do comando | deploy em segundo plano nunca solta no `post` nem no `Stop`; só por prazo, `SessionEnd`, morte da sessão ou liberação manual |
| `$PPID` do hook | é o shell intermediário, não o Claude | não usado; a sessão é identificada por `session_id` ↔ `sessions/*.json` |
| Sessões `claude -p` em `~/.claude/sessions/` | **não aparecem** | sessão sem registro vale pela atividade do arquivo de claim (`prazo_sessao_sem_registro_min`) e é limpa pelo `SessionEnd` |
| `mcp__coolify__deploy` | recebe `tag_or_uuid`, nunca o nome do produto | o produto é resolvido pelo diretório de trabalho, não pelo argumento |
| `additionalContext` do `PreToolUse` sem `permissionDecision` | entregue ao modelo, e a edição segue o fluxo normal de permissão | R3 usa exatamente isso; nunca `allow` |
| `Stop` | traz `background_tasks` e `last_assistant_message` | não usados |

Ao vivo, com duas sessões `claude -p` reais, `coolify` de mentira e hooks
isolados por `--settings`: a segunda sessão foi negada com a mensagem da R1
enquanto a primeira rodava, e só um processo do deploy executou; a trava saiu
no fim do comando; a segunda edição do mesmo arquivo recebeu o aviso da R3
dentro do contexto do modelo; `git checkout -b` foi negado pela R2 e a branch
não mudou.

## Componentes

Tudo é um único programa Python, só biblioteca padrão, mais a skill e o
instalador. No repositório:

```
orquestra/
  orquestra.py            hooks e CLI (subcomandos)
  SKILL.md                skill /orquestra
  install.sh              instala em ~/.claude e registra os hooks
  uninstall.sh            remove hooks e arquivos; preserva o estado
  produtos.exemplo.json   modelo do mapa de produtos
  tests/test_orquestra.py unittest
```

Instalado:

```
~/.claude/hooks/orquestra.py
~/.claude/skills/orquestra/SKILL.md
~/.claude/orquestra/                estado (ver abaixo)
```

### Estado: `~/.claude/orquestra/`

| Caminho | Conteúdo |
|---|---|
| `produtos.json` | configuração: produtos, caminhos, nomes no Coolify, prazos |
| `locks/<produto>.<ambiente>.json` | trava de deploy |
| `claims/<session_id>.json` | árvore de trabalho, branch e arquivos editados pela sessão |
| `avisos/<session_id>.jsonl` | recados pendentes para a sessão |
| `orquestra.log` | uma linha por decisão: negou, avisou, liberou, erro interno |
| `DESLIGADO` | se existir, todo hook sai com 0 sem fazer nada |

Um arquivo por sessão em `claims/` e `avisos/`: duas sessões nunca escrevem
o mesmo arquivo de reivindicação. Escritas são atômicas (arquivo temporário +
`os.replace`). A trava é criada com `os.open(O_CREAT | O_EXCL)`, de modo que
duas tentativas simultâneas nunca vencem as duas.

### Configuração: `produtos.json`

```json
{
  "prazo_deploy_fundo_min": 15,
  "prazo_sessao_sem_registro_min": 30,
  "produtos": {
    "exemplo": {
      "caminhos": ["/opt/exemplo", "/opt/exemplo-worktrees", "~/worktrees/exemplo-*"],
      "coolify": ["exemplo", "exemplo-backend"],
      "branch_base": "origin/main"
    }
  }
}
```

Nenhum nome de produto, caminho ou prazo fica no código. Arquivo ausente ou
inválido: os hooks de deploy não reconhecem produto nenhum e deixam passar;
as regras de `git checkout` e de edição continuam valendo, porque não
dependem do mapa.

### Identidade e vida de uma sessão

O hook recebe `session_id`. O processo dono é achado em
`~/.claude/sessions/*.json` pelo campo `sessionId`, que traz `pid`, `cwd`,
`name` e `status`.

Uma sessão está **viva** quando existe `sessions/<pid>.json` com aquele
`sessionId` e `/proc/<pid>` existe. Se a sessão não tem registro em
`sessions/` (formato mudou, sessão sem registro), vale a data de modificação
do seu arquivo em `claims/`: viva se tocado há menos de
`prazo_sessao_sem_registro_min`.

Não há heartbeat. Trava ou reivindicação de sessão morta é removida pela
primeira chamada de hook ou de painel que a encontrar, com linha no log. Um
registro cujo processo acabou é morto de imediato; só a sessão sem nenhum
registro usa o prazo.

### Resolução de produto e de árvore de trabalho

- **Árvore de trabalho**: `git rev-parse --show-toplevel` a partir do
  diretório efetivo do comando (o `cwd` do hook, ajustado por `cd X &&` e
  `git -C X` quando presentes). Fora de repositório git, o próprio diretório.
- **Produto**: o diretório efetivo casado com `caminhos` (prefixo ou glob);
  para `coolify deploy <nome>`, o nome casado com a lista `coolify`; para
  skill, o primeiro argumento que seja nome de produto, senão o diretório.
- **Ambiente**: `prod` para a skill `deploy-prod` e para comandos que
  atravessam `ssh`/`pct exec`; `dev` para o resto.

Produto não reconhecido: sem trava, comando passa, linha no log.

## Regras

### R1. Trava de deploy (nega)

Dispara em `PreToolUse` para:

- Bash contendo `coolify deploy <nome>`;
- Bash contendo `docker compose` (ou `docker-compose`) com `up`, `build` ou
  `restart`;
- ferramenta `Skill` com `deploy-prod` ou `tarefa-finalizada`;
- ferramenta de deploy do MCP do Coolify.

Comportamento:

1. Resolve `<produto>.<ambiente>`. Sem produto, sai com 0.
2. Trava inexistente, ou de sessão morta, ou vencida: cria e sai com 0.
3. Trava da própria sessão: renova e sai com 0 (uma skill que roda
   `coolify deploy` não se bloqueia).
4. Trava de outra sessão viva: `exit 2` com mensagem
   `Deploy de <produto> (<ambiente>) em andamento pela sessão "<nome>" desde
   <hora>: <comando>. Aguarde terminar ou use /orquestra para ver o estado.`
   Grava recado para a sessão dona: outra sessão tentou deployar.

**Ciclo da trava**

| Origem | Solta em |
|---|---|
| Bash em primeiro plano (e deploy via MCP) | `PostToolUse` daquela chamada, casada por `tool_use_id` |
| Bash com `run_in_background` | prazo `prazo_deploy_fundo_min`, ou morte da sessão, ou liberação manual |
| Bash em primeiro plano ou skill | também no `Stop` (fim do turno): cobre `Esc` e comando interrompido, em que o `PostToolUse` não vem |
| Qualquer | `SessionEnd` ou morte da sessão dona; `orquestra.py liberar` |

A trava guarda a origem que a criou. Reentrada da mesma sessão não muda a
origem: o `PostToolUse` de um `coolify deploy` rodado dentro de uma skill não
solta a trava da skill, que só sai no `Stop`.

Ao soltar, grava recado para as sessões que foram negadas enquanto a trava
valia: deploy liberado.

### R2. Troca de branch em diretório compartilhado (nega)

Dispara em `PreToolUse` Bash para `git checkout <ref>`, `git checkout -b`,
`git switch`. Não dispara para restauração de arquivo (`git checkout -- <caminho>`,
`git checkout <ref> -- <caminho>`, `git restore`).

Se existe outra sessão viva cuja árvore de trabalho (pelo `cwd` em
`sessions/`) é a mesma: `exit 2` com mensagem
`A sessão "<nome>" também está em <árvore>. Trocar de branch aqui muda o
HEAD dela. Crie uma worktree: git worktree add <caminho> <branch>.`

Sem outra sessão viva na mesma árvore, passa.

### R3. Edição sobreposta (avisa)

Dispara em `PreToolUse` para `Edit`, `Write` e `NotebookEdit`.

1. Registra o caminho real do arquivo em `claims/<session_id>.json`.
2. Se outra sessão viva tem o mesmo caminho real nas suas reivindicações:
   devolve `additionalContext` com
   `Atenção: a sessão "<nome>" editou <arquivo> às <hora>. Releia o arquivo
   antes de alterar e confirme com o usuário se as mudanças se sobrepõem.`
   e grava recado para a outra sessão. Nunca bloqueia.
3. O mesmo par sessão/arquivo é avisado uma vez só.

Worktrees diferentes têm caminhos reais diferentes: a mesma edição em duas
branches não dispara R3. Esse caso é sobreposição de PR e aparece no painel.

### R4. Recados

`UserPromptSubmit` lê `avisos/<session_id>.jsonl`, devolve o conteúdo como
contexto (truncado a 8.000 caracteres, o mais recente primeiro) e esvazia o
arquivo. Sem recado, sai sem saída.

### R5. Limpeza

`Stop` solta as travas de Bash em primeiro plano e de skill da sessão; `SessionEnd` solta todas e apaga claim e recados. Reivindicações de arquivo ficam até
a sessão morrer: uma sessão ociosa ainda tem alterações não commitadas
naquela árvore.

## Skill `/orquestra`

A skill manda rodar `python3 ~/.claude/hooks/orquestra.py <subcomando>` e
apresentar a saída. Subcomandos:

| Subcomando | Saída |
|---|---|
| `painel` | sessões vivas agrupadas por produto e árvore (nome, estado, branch), travas ativas, arquivos reivindicados por mais de uma sessão |
| `sobreposicao [produto]` | por repositório: arquivos alterados em cada worktree/branch contra `branch_base` (commits + não commitados) e as interseções entre branches. Com `--prs`, inclui PRs abertos via `gh`; sem rede ou sem `gh`, segue só com o local e diz isso |
| `liberar <produto>.<ambiente> --motivo "<texto>"` | remove a trava, registra no log e avisa a sessão dona |
| `status` | hooks instalados? `DESLIGADO` presente? últimas 20 linhas do log |

Todos aceitam `--json`. A skill nunca libera trava sem pedido explícito do
usuário.

Vigilância contínua de sobreposição de PR é `/loop` sobre
`/orquestra sobreposicao`; não há daemon.

## Tratamento de erro

- Todo o corpo do hook fica dentro de um `try`; qualquer exceção é registrada
  em `orquestra.log` e o hook sai com 0. Um defeito do Orquestra nunca
  bloqueia uma sessão.
- Só há `exit 2` quando uma trava foi lida com sucesso e a sessão dona foi
  confirmada viva (R1), ou quando outra sessão viva foi confirmada na mesma
  árvore (R2).
- Estado ilegível (JSON corrompido): tratado como ausente, com linha no log.
- Timeout de 5 s por hook no `settings.json`.
- Atalho de desempenho: o hook de Bash sai antes de carregar o estado quando
  o comando não contém `deploy`, `compose`, `checkout` nem `switch`.

## Instalação

`install.sh` copia o programa e a skill, cria `~/.claude/orquestra/` e
`produtos.json` a partir do exemplo se não existir, e acrescenta os hooks em
`~/.claude/settings.json`: faz cópia de segurança datada, não altera entradas
existentes, e é idempotente (rodar de novo não duplica). Não usa sudo.

Hooks registrados:

| Evento | Matcher | Comando |
|---|---|---|
| `PreToolUse` | `Bash` | `orquestra.py hook pre-bash` |
| `PreToolUse` | `Skill` e deploy do MCP Coolify (`deploy`, `redeploy_project`, `restart_project_apps`) | `orquestra.py hook pre-deploy` |
| `PreToolUse` | `Edit\|Write\|NotebookEdit` | `orquestra.py hook pre-edit` |
| `PostToolUse` | `Bash` e deploy do MCP Coolify | `orquestra.py hook post` |
| `UserPromptSubmit` | (todos) | `orquestra.py hook prompt` (entrega recados e renova o claim) |
| `Stop` | (todos) | `orquestra.py hook stop` |
| `SessionEnd` | (todos) | `orquestra.py hook session-end` (remove claim, recados e travas da sessão) |

Sessões já abertas só passam a usar os hooks depois de reiniciadas.

## Testes

1. **Sondagem** (primeira tarefa): hook temporário que grava o stdin cru,
   para medir os três pontos não documentados. Resultado anotado nesta spec.
2. **Unitários** (`unittest`, sem rede, diretório de estado temporário):
   - trava: adquirir, reentrar, negar para outra sessão, sessão morta libera,
     prazo de fundo vence, duas aquisições simultâneas só uma vence;
   - reconhecimento de comando: tabela de comandos reais e seus resultados
     esperados, incluindo `rtk`-prefixado, `cd X && …`, `git -C`, restauração
     de arquivo que não deve negar;
   - edição: registra, avisa uma vez, não avisa a própria sessão, não avisa
     entre worktrees;
   - recados: entrega e esvazia; truncamento;
   - falha aberta: estado corrompido, `produtos.json` ausente, exceção
     interna, `DESLIGADO`;
   - sobreposição: repositório temporário com duas worktrees.
3. **Ao vivo**: duas sessões reais num repositório descartável com um
   produto fictício cujo "deploy" é um `sleep`; confere R1, R2, R3 e R4.
4. CI: `python3 -m py_compile` e `python3 -m unittest` do diretório
   `orquestra/`, junto das verificações já existentes.

## Limitações conhecidas

- Heredoc: uma linha de corpo de heredoc que comece com `coolify deploy` é lida como comando.
- `sudo -u <usuário> cmd` e `timeout` com opções incomuns não são decompostos; o comando passa sem trava.
- Deploy via MCP só é travado quando o diretório de trabalho da sessão está num produto configurado.
- Duas remoções simultâneas de uma trava vencida têm uma janela mínima de corrida (renomear, conferir, devolver); o caso comum, duas aquisições simultâneas, é atômico (`os.link`) e testado.
- As travas e claims só enxergam sessões da mesma máquina e do mesmo usuário.

## Fora do escopo

- Mensagens livres entre sessões.
- Bloqueio de edição.
- Coordenação entre máquinas diferentes.
- Deploy em produção feito fora das skills e fora de `ssh`/`pct exec`
  reconhecíveis.
- Integração com a página do My Harness Library (o inventário já lista a
  skill sozinho).
