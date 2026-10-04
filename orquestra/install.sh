#!/bin/bash
# Instala o Orquestra no ~/.claude do usuário atual (sem sudo).
#
#   bash orquestra/install.sh              copia arquivos e registra os hooks
#   bash orquestra/install.sh --sem-hooks  só copia; registre depois com
#                                          python3 ~/.claude/hooks/orquestra.py configurar
#
# Sessões já abertas podem não enxergar os hooks novos; reinicie-as.
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE="${ORQUESTRA_CLAUDE_DIR:-$HOME/.claude}"

command -v python3 >/dev/null || { echo "python3 não encontrado" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "precisa de Python 3.9+" >&2; exit 1; }
python3 -m py_compile "$AQUI/orquestra.py"

mkdir -p "$CLAUDE/hooks" "$CLAUDE/skills/orquestra" "$CLAUDE/orquestra"
install -m 0755 "$AQUI/orquestra.py" "$CLAUDE/hooks/orquestra.py"
install -m 0644 "$AQUI/SKILL.md" "$CLAUDE/skills/orquestra/SKILL.md"

if [[ ! -f "$CLAUDE/orquestra/produtos.json" ]]; then
  install -m 0644 "$AQUI/produtos.exemplo.json" "$CLAUDE/orquestra/produtos.json"
  echo "criado $CLAUDE/orquestra/produtos.json (exemplo): edite com seus produtos"
fi

if [[ "${1:-}" == "--sem-hooks" ]]; then
  echo "arquivos instalados; hooks NÃO registrados"
else
  ORQUESTRA_CLAUDE_DIR="$CLAUDE" python3 "$CLAUDE/hooks/orquestra.py" configurar
fi
echo "ok. Estado: $CLAUDE/orquestra/   Desligar tudo: touch $CLAUDE/orquestra/DESLIGADO"
