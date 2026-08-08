#!/bin/bash
# =============================================================================
# setup.sh — installs My Harness Library on this machine.
#
# Usage:
#   bash setup.sh --check      # diagnose only, changes nothing, no sudo needed
#   sudo bash setup.sh         # install missing packages and configure everything
#
# Idempotent: running it twice does not duplicate anything. Backs up the nginx
# vhost before touching it. Nothing is hardcoded — user, home directory and the
# nginx site file are all discovered at run time.
#
# Password: with a TTY you are prompted. Piped through `curl | sudo bash`
# there is no TTY, so the documented initial password is used and a warning is
# printed. Override with HARNESS_PASSWORD=... in the environment.
# =============================================================================
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

# Documented in the README. Deliberately memorable and deliberately weak:
# it exists so an unattended install still produces a working system, and the
# UI nags you to change it.
DEFAULT_PASSWORD="change-me-now"

# The environment owner is whoever called sudo, not root.
TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
WEBROOT="/var/www/html/claude-inventory"
STATE="$TARGET_HOME/.claude/.inventory"
UNIT=/etc/systemd/system/harness-library.service

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; DIM=$'\e[2m'; OFF=$'\e[0m'
ok()   { echo "  ${GRN}ok${OFF}      $*"; }
warn() { echo "  ${YEL}warn${OFF}    $*"; }
bad()  { echo "  ${RED}missing${OFF} $*"; }
step() { echo; echo "${DIM}== $* ==${OFF}"; }

MISSING=()

# ---------------------------------------------------------------------------
# 1. Diagnosis
# ---------------------------------------------------------------------------
step "Dependencies"

declare -A PKG=(
  [python3]=python3
  [nginx]=nginx
  [curl]=curl
  [flock]=util-linux
  [crontab]=cron
  [systemctl]=systemd
)

for cmd in python3 nginx curl flock crontab systemctl; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd  ${DIM}($(command -v "$cmd"))${OFF}"
  else
    bad "$cmd  ${DIM}-> package ${PKG[$cmd]}${OFF}"
    MISSING+=("${PKG[$cmd]}")
  fi
done

# Standard library only — no pip, no virtualenv, nothing vendored.
if python3 -c 'import json,re,html,hashlib,hmac,secrets,socketserver,http.server,configparser,urllib.request,pathlib' 2>/dev/null; then
  ok "python modules  ${DIM}(standard library only)${OFF}"
else
  bad "python modules — incomplete python3 installation"
fi

PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
if python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
  ok "python $PYVER  ${DIM}(3.9+ required)${OFF}"
else
  bad "python $PYVER — 3.9 or newer required"
fi

step "Environment"
[[ -d "$TARGET_HOME/.claude" ]] \
  && ok "~/.claude of $TARGET_USER  ${DIM}($TARGET_HOME/.claude)${OFF}" \
  || warn "$TARGET_HOME/.claude does not exist — the inventory will be empty"

[[ -f "$DIR/inventory.py" && -f "$DIR/api.py" && -f "$DIR/regenerate.sh" \
   && -f "$DIR/harness-library.service.in" ]] \
  && ok "source files present in $DIR" \
  || { bad "missing files in $DIR"; exit 1; }

step "Current installation"
[[ -f "$UNIT" ]] && ok "systemd unit installed" || warn "systemd unit absent"
systemctl is-active --quiet harness-library 2>/dev/null && ok "backend running" || warn "backend not running"
grep -rqs 'claude-inventory/api' /etc/nginx/sites-available/ && ok "nginx route configured" || warn "nginx route absent"
if [[ -f "$STATE/auth.hash" ]]; then
  if head -c 3 "$STATE/auth.hash" | grep -q '^\$2'; then
    warn "password stored in the old bcrypt format — will be reset below"
  else
    ok "write password set"
  fi
else
  warn "write password not set"
fi
crontab -u "$TARGET_USER" -l 2>/dev/null | grep -q 'regenerate.sh' && ok "regeneration cron installed" || warn "cron absent"
[[ -f "$WEBROOT/index.html" ]] && ok "page published at $WEBROOT" || warn "page not published"

if (( CHECK_ONLY )); then
  echo
  if (( ${#MISSING[@]} )); then
    echo "${YEL}Missing packages:${OFF} ${MISSING[*]}"
    echo "To install and configure:  sudo bash $0"
    exit 1
  fi
  echo "${GRN}All dependencies satisfied.${OFF} To configure:  sudo bash $0"
  exit 0
fi

# ---------------------------------------------------------------------------
# Everything below changes the system — root required
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo; echo "${RED}Root required to install.${OFF}  Run:  sudo bash $0"
  exit 1
fi

step "Installing missing packages"
if (( ${#MISSING[@]} )); then
  if command -v apt-get >/dev/null 2>&1; then
    echo "  apt-get install -y ${MISSING[*]}"
    apt-get update -qq
    apt-get install -y "${MISSING[@]}" || { echo "${RED}apt failed${OFF}"; exit 1; }
  else
    echo "${RED}Package manager is not apt.${OFF} Install manually: ${MISSING[*]}"
    exit 1
  fi
else
  ok "nothing to install"
fi

step "Backend service (runs as $TARGET_USER)"
sed -e "s|@USER@|$TARGET_USER|g" \
    -e "s|@HOME@|$TARGET_HOME|g" \
    -e "s|@PREFIX@|$DIR|g" \
    "$DIR/harness-library.service.in" > "$UNIT"
chmod 644 "$UNIT"
systemctl daemon-reload
ok "$UNIT"

step "Removing the old PHP backend, if present"
# Upgrade path from the PHP version: drop its nginx location block and its
# FPM pool. Harmless no-op on a fresh install.
LEGACY_VHOST="$(grep -rls 'claude-inventory/api\.php' /etc/nginx/sites-available/ 2>/dev/null | head -1)"
if [[ -n "$LEGACY_VHOST" ]]; then
  cp -a "$LEGACY_VHOST" "$LEGACY_VHOST.bak-php-$(date +%Y%m%d-%H%M%S)"
  awk '
    /claude-inventory\/api\.php \{/ { skip=1; next }
    skip && /^[[:space:]]*\}/       { skip=0; next }
    skip                            { next }
    /# Editor do inventario Claude Code|# Harness Library editor|# Match exato|# over any regex location block|# prioridade sobre qualquer regex/ { next }
    { print }
  ' "$LEGACY_VHOST.bak-php-"* > "$LEGACY_VHOST"
  ok "old api.php route removed from $LEGACY_VHOST"
fi
for pool in /etc/php/*/fpm/pool.d/claude-inventory.conf; do
  [[ -f "$pool" ]] || continue
  rm -f "$pool"
  ok "old FPM pool removed: $pool"
  PHPOLD="$(basename "$(dirname "$(dirname "$pool")")")"
  systemctl reload "php$PHPOLD-fpm" 2>/dev/null
done

step "nginx route"
VHOST="$(grep -rls 'root */var/www/html' /etc/nginx/sites-available/ 2>/dev/null | head -1)"
if [[ -z "$VHOST" ]]; then
  warn "no site serving /var/www/html; falling back to the default site"
  VHOST=/etc/nginx/sites-available/default
fi
if grep -q 'claude-inventory/api' "$VHOST"; then
  ok "already present in $VHOST"
else
  BACKUP="$VHOST.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "$VHOST" "$BACKUP"
  awk '
    !done && /^[[:space:]]*root[[:space:]]+\/var\/www\/html;/ {
      print; print ""
      print "    # My Harness Library backend. An exact (=) match takes priority"
      print "    # over any regex location block."
      print "    location = /claude-inventory/api {"
      print "        proxy_pass http://unix:/run/harness-library/sock:/;"
      print "        proxy_set_header X-Real-IP $remote_addr;"
      print "        proxy_set_header Host $host;"
      print "        proxy_read_timeout 30s;"
      print "    }"
      done = 1; next
    }
    { print }
  ' "$BACKUP" > "$VHOST"
  ok "route added to $VHOST  ${DIM}(backup: $BACKUP)${OFF}"
fi

step "Publishing files"
install -d -o "$TARGET_USER" -g www-data -m 2775 "$WEBROOT"
install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 700 "$STATE"
# The old PHP endpoint, if this is an upgrade.
rm -f "$WEBROOT/api.php"
ok "$WEBROOT"

step "Write password"
set_password() {   # $1 = plaintext
  python3 - "$STATE/auth.hash" "$1" <<'PY'
import hashlib, secrets, sys, os
path, plain = sys.argv[1], sys.argv[2]
salt = secrets.token_bytes(16)
n, r, p = 2**14, 8, 1
key = hashlib.scrypt(plain.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write(f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

LEGACY=0
[[ -f "$STATE/auth.hash" ]] && head -c 3 "$STATE/auth.hash" | grep -q '^\$2' && LEGACY=1

if [[ -f "$STATE/auth.hash" ]] && (( ! LEGACY )); then
  ok "already set — change it from the page (🔑 button)"
else
  (( LEGACY )) && warn "the old bcrypt hash cannot be verified without extra
          dependencies; setting the password again with scrypt"
  if [[ -n "${HARNESS_PASSWORD:-}" ]]; then
    set_password "$HARNESS_PASSWORD"
    ok "taken from \$HARNESS_PASSWORD"
  elif [[ -t 0 ]]; then
    while :; do
      read -rsp "  Set the write password (min 8 chars): " PW1; echo
      read -rsp "  Repeat: " PW2; echo
      [[ "$PW1" == "$PW2" ]] || { warn "they do not match"; continue; }
      (( ${#PW1} >= 8 )) || { warn "too short"; continue; }
      break
    done
    set_password "$PW1"; unset PW1 PW2
    ok "stored as an scrypt hash"
  else
    # No TTY: this is the `curl | sudo bash` path.
    set_password "$DEFAULT_PASSWORD"
    warn "no terminal available — initial password set to \"$DEFAULT_PASSWORD\""
    warn "${RED}CHANGE IT NOW${OFF} from the page (🔑 button in the header)"
  fi
fi
chown "$TARGET_USER:$TARGET_USER" "$STATE/auth.hash"
chmod 600 "$STATE/auth.hash"

step "Regeneration cron"
if crontab -u "$TARGET_USER" -l 2>/dev/null | grep -q 'regenerate.sh'; then
  ok "already installed"
else
  install -d -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/logs"
  ( crontab -u "$TARGET_USER" -l 2>/dev/null
    echo "# My Harness Library — daily regeneration"
    echo "22 6 * * * $DIR/regenerate.sh >> $TARGET_HOME/logs/harness-library.log 2>&1"
    echo "# Serves the page's Regenerate button"
    echo "* * * * * $DIR/regenerate.sh --watch >> $TARGET_HOME/logs/harness-library.log 2>&1"
  ) | crontab -u "$TARGET_USER" -
  ok "daily at 06:22 + 1-minute watcher"
fi

step "Starting services"
systemctl enable --now harness-library >/dev/null 2>&1
sleep 1
systemctl is-active --quiet harness-library && ok "harness-library" \
  || { echo "${RED}backend failed to start${OFF}"; journalctl -u harness-library -n 20 --no-pager; exit 1; }
nginx -t || { echo "${RED}nginx -t failed — route NOT applied${OFF}"; exit 1; }
systemctl reload nginx && ok "nginx"

step "Generating the inventory"
sudo -u "$TARGET_USER" bash "$DIR/regenerate.sh" || warn "generation failed (check the log)"

step "Verification"
IP="$(hostname -I | awk '{print $1}')"
CODE="$(curl -sk -o /dev/null -w '%{http_code}' "http://$IP/claude-inventory/")"
API="$(curl -sk -X POST "http://$IP/claude-inventory/api" \
        -H 'Content-Type: application/json' -d '{"action":"status"}' -o /dev/null -w '%{http_code}')"
[[ "$CODE" =~ ^(200|301)$ ]] && ok "page responds ($CODE)" || bad "page responded $CODE"
[[ "$API" == "200" ]] && ok "backend responds (200)" || bad "backend responded $API"

echo
echo "${GRN}Done.${OFF}  http://$IP/claude-inventory/"
