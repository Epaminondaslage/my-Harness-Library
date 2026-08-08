#!/bin/bash
# =============================================================================
# uninstall.sh — removes Harness Library from this machine.
#
#   sudo bash uninstall.sh          # removes the service, keeps your data
#   sudo bash uninstall.sh --purge  # also removes password, audit log, backups
#
# Never touches your skills, agents or commands — only what this tool created.
# =============================================================================
set -uo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
WEBROOT="/var/www/html/claude-inventory"
STATE="$TARGET_HOME/.claude/.inventory"

[[ $EUID -eq 0 ]] || { echo "Root required: sudo bash $0"; exit 1; }

echo "== Removing cron =="
crontab -u "$TARGET_USER" -l 2>/dev/null | grep -v 'regenerate.sh' \
  | grep -v '# Harness Library' | grep -v "# Serves the page's Regenerate" \
  | crontab -u "$TARGET_USER" - && echo "  ok"

echo "== Stopping and removing the backend service =="
systemctl disable --now harness-library >/dev/null 2>&1
rm -f /etc/systemd/system/harness-library.service
systemctl daemon-reload
echo "  ok"

echo "== Removing nginx route =="
VHOST="$(grep -rls 'claude-inventory/api' /etc/nginx/sites-available/ 2>/dev/null | head -1)"
if [[ -n "$VHOST" ]]; then
  cp -a "$VHOST" "$VHOST.bak-$(date +%Y%m%d-%H%M%S)"
  # Drops the location block and the two comment lines above it.
  awk '
    /# My Harness Library backend/        { skip=1 }
    skip && /^[[:space:]]*\}/             { skip=0; next }
    !skip                                 { print }
  ' "$VHOST.bak-"* > "$VHOST"
  echo "  ok (backup kept next to it)"
else
  echo "  not found"
fi

echo "== Removing the published page =="
rm -rf "$WEBROOT" && echo "  ok"

if (( PURGE )); then
  echo "== Purging state (password, audit log, status) =="
  rm -rf "$STATE" && echo "  ok"
  echo "== Removing file revisions (.inventory-backups) =="
  find "$TARGET_HOME/.claude" -type d -name '.inventory-backups' -exec rm -rf {} + 2>/dev/null
  echo "  ok"
else
  echo "== Keeping state =="
  echo "  $STATE  (password, audit log)"
  echo "  file revisions in ~/.claude/**/.inventory-backups"
  echo "  use --purge to remove these too"
fi

echo "== Reloading nginx =="
nginx -t && systemctl reload nginx

echo
echo "Done. Your skills, agents and commands were not touched."
