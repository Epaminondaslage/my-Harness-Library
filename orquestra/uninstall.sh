#!/bin/bash
# Remove os hooks do Orquestra do settings.json e os arquivos instalados.
# O estado (~/.claude/orquestra/: produtos.json, log) é preservado.
set -euo pipefail
CLAUDE="${ORQUESTRA_CLAUDE_DIR:-$HOME/.claude}"
if [[ -f "$CLAUDE/hooks/orquestra.py" ]]; then
  ORQUESTRA_CLAUDE_DIR="$CLAUDE" python3 "$CLAUDE/hooks/orquestra.py" remover
fi
rm -f "$CLAUDE/hooks/orquestra.py"
rm -rf "$CLAUDE/skills/orquestra"
echo "removido. Estado preservado em $CLAUDE/orquestra/"
