#!/bin/bash
# =============================================================================
# regenerate.sh — rebuilds the inventory and publishes it to the web root.
#
# Called three ways:
#   1. daily cron            (keeps the page fresh on its own)
#   2. one-minute cron       (serves the page's "Regenerate" button)
#   3. by hand               (bash regenerate.sh)
#
# Runs as the user who owns ~/.claude and the web root. Never needs sudo.
# A flock prevents two runs at the same time.
# =============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HOME/.cache/harness-library"
DEST="/var/www/html/claude-inventory"
# Same directory api.php can see (open_basedir covers ~/.claude)
STATE="$HOME/.claude/.inventory"
STATUS="$STATE/status.json"
REQUEST="$STATE/regen.request"
LOCK="$HOME/.cache/harness-library.lock"

mkdir -p "$WORK" "$(dirname "$LOCK")" "$STATE"

# --watch: only acts when the page asked for a regeneration. This is what the
# one-minute cron runs, so it costs almost nothing when there is no request.
if [[ "${1:-}" == "--watch" ]]; then
  [[ -f "$REQUEST" ]] || exit 0
  rm -f "$REQUEST"
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "a regeneration is already running" >&2
  exit 0
fi

STARTED=$(date +%s)
cd "$WORK" || exit 1

# Offline by default: plugin origins come from known_marketplaces.json, which
# is exact and needs no network. Pass --online to also query the npm registry.
if ! python3 "$DIR/inventory.py" "$@" > "$WORK/last-run.log" 2>&1; then
  printf '{"ok":false,"error":"generation failed","at":"%s"}\n' "$(date -Iseconds)" > "$STATUS"
  echo "FAILED — see $WORK/last-run.log" >&2
  exit 1
fi

cp "$WORK/claude_inventory_site/index.html" \
   "$WORK/claude_inventory_site/styles.css" \
   "$WORK/claude_inventory_site/app.js" "$DEST/" || exit 1
chmod 644 "$DEST/index.html" "$DEST/styles.css" "$DEST/app.js"

ITEMS=$(grep -c '<article class="card' "$DEST/index.html")
ELAPSED=$(( $(date +%s) - STARTED ))

printf '{"ok":true,"at":"%s","items":%d,"seconds":%d}\n' \
  "$(date -Iseconds)" "$ITEMS" "$ELAPSED" > "$STATUS"
chmod 644 "$STATUS"

echo "OK — $ITEMS items in ${ELAPSED}s"
