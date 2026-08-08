#!/bin/bash
# =============================================================================
# setup.sh — installs Harness Library on this machine.
#
# Usage:
#   bash setup.sh --check      # diagnose only, changes nothing, no sudo needed
#   sudo bash setup.sh         # install missing packages and configure everything
#
# Idempotent: running it twice does not duplicate anything. Backs up the nginx
# vhost before touching it. Nothing is hardcoded — PHP version, user, home
# directory and the nginx site file are all discovered at run time.
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

# command -> apt package that provides it
declare -A PKG=(
  [python3]=python3
  [php]=php-cli
  [nginx]=nginx
  [curl]=curl
  [flock]=util-linux
  [crontab]=cron
)

for cmd in python3 php nginx curl flock crontab; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd  ${DIM}($(command -v "$cmd"))${OFF}"
  else
    bad "$cmd  ${DIM}-> package ${PKG[$cmd]}${OFF}"
    MISSING+=("${PKG[$cmd]}")
  fi
done

# PHP-FPM: the version differs per distro, so discover instead of hardcoding.
PHPVER="$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null || true)"
if [[ -n "$PHPVER" ]] && [[ -d "/etc/php/$PHPVER/fpm/pool.d" ]]; then
  ok "php-fpm $PHPVER  ${DIM}(/etc/php/$PHPVER/fpm/pool.d)${OFF}"
else
  bad "php-fpm  ${DIM}-> package php${PHPVER:+$PHPVER-}fpm${OFF}"
  MISSING+=("php${PHPVER:+$PHPVER-}fpm")
fi

# Python: standard library only. Just confirm the interpreter is usable.
if python3 -c 'import json,re,html,configparser,urllib.request,getpass,socket,pathlib' 2>/dev/null; then
  ok "python modules  ${DIM}(standard library only)${OFF}"
else
  bad "python modules — incomplete python3 installation"
fi

step "Environment"
[[ -d "$TARGET_HOME/.claude" ]] \
  && ok "~/.claude of $TARGET_USER  ${DIM}($TARGET_HOME/.claude)${OFF}" \
  || warn "$TARGET_HOME/.claude does not exist — the inventory will be empty"

[[ -f "$DIR/inventory.py" && -f "$DIR/api.php" && -f "$DIR/regenerate.sh" ]] \
  && ok "source files present in $DIR" \
  || { bad "missing files in $DIR (inventory.py, api.php, regenerate.sh)"; exit 1; }

step "Current installation"
[[ -f "/etc/php/$PHPVER/fpm/pool.d/claude-inventory.conf" ]] && ok "PHP-FPM pool configured" || warn "PHP-FPM pool absent"
grep -rqs 'claude-inventory/api.php' /etc/nginx/sites-available/ && ok "nginx route configured" || warn "nginx route absent"
[[ -f "$STATE/auth.hash" ]] && ok "write password set" || warn "write password not set"
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
    PHPVER="$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')"
  else
    echo "${RED}Package manager is not apt.${OFF} Install manually: ${MISSING[*]}"
    exit 1
  fi
else
  ok "nothing to install"
fi

step "PHP-FPM pool (runs as $TARGET_USER)"
POOL="/etc/php/$PHPVER/fpm/pool.d/claude-inventory.conf"
cat > "$POOL" <<EOF
; Dedicated pool for the Harness Library editor.
; Runs as $TARGET_USER so it can reach ~/.claude without widening any
; permission for www-data. Serves ONLY /claude-inventory/api.php.
[claude-inventory]
user  = $TARGET_USER
group = $TARGET_USER
listen       = /run/php/claude-inventory.sock
listen.owner = www-data
listen.group = www-data
listen.mode  = 0660
pm                      = ondemand
pm.max_children         = 5
pm.process_idle_timeout = 30s
pm.max_requests         = 200
env[HOME] = $TARGET_HOME
env[PATH] = /usr/local/bin:/usr/bin:/bin
php_admin_value[open_basedir] = $TARGET_HOME/.claude:$WEBROOT
php_admin_value[disable_functions] = exec,passthru,shell_exec,system,proc_open,popen
php_admin_flag[allow_url_fopen] = off
php_admin_value[upload_max_filesize] = 2M
php_admin_value[post_max_size] = 2M
EOF
ok "$POOL"

step "nginx route"
VHOST="$(grep -rls 'root */var/www/html' /etc/nginx/sites-available/ 2>/dev/null | head -1)"
if [[ -z "$VHOST" ]]; then
  warn "no site serving /var/www/html; falling back to the default site"
  VHOST=/etc/nginx/sites-available/default
fi
if grep -q 'claude-inventory/api.php' "$VHOST"; then
  ok "already present in $VHOST"
else
  BACKUP="$VHOST.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "$VHOST" "$BACKUP"
  awk '
    !done && /^[[:space:]]*root[[:space:]]+\/var\/www\/html;/ {
      print; print ""
      print "    # Harness Library editor. An exact (=) match takes priority"
      print "    # over any regex location block."
      print "    location = /claude-inventory/api.php {"
      print "        include snippets/fastcgi-php.conf;"
      print "        fastcgi_pass unix:/run/php/claude-inventory.sock;"
      print "    }"
      done = 1; next
    }
    { print }
  ' "$BACKUP" > "$VHOST"
  ok "route added to $VHOST  ${DIM}(backup: $BACKUP)${OFF}"
fi

step "Publishing files"
install -d -o "$TARGET_USER" -g www-data -m 2775 "$WEBROOT"
install -o "$TARGET_USER" -g www-data -m 644 "$DIR/api.php" "$WEBROOT/api.php"
install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 700 "$STATE"
ok "$WEBROOT"

step "Write password"
if [[ -f "$STATE/auth.hash" ]]; then
  ok "already set — change it from the page (🔑 button)"
elif [[ -n "${HARNESS_PASSWORD:-}" ]]; then
  php -r 'file_put_contents($argv[1], password_hash($argv[2], PASSWORD_BCRYPT)."\n");' \
      "$STATE/auth.hash" "$HARNESS_PASSWORD"
  ok "taken from \$HARNESS_PASSWORD"
elif [[ -t 0 ]]; then
  while :; do
    read -rsp "  Set the write password (min 8 chars): " PW1; echo
    read -rsp "  Repeat: " PW2; echo
    [[ "$PW1" == "$PW2" ]] || { warn "they do not match"; continue; }
    (( ${#PW1} >= 8 )) || { warn "too short"; continue; }
    break
  done
  php -r 'file_put_contents($argv[1], password_hash($argv[2], PASSWORD_BCRYPT)."\n");' \
      "$STATE/auth.hash" "$PW1"
  unset PW1 PW2
  ok "stored as a bcrypt hash"
else
  # No TTY: this is the `curl | sudo bash` path.
  php -r 'file_put_contents($argv[1], password_hash($argv[2], PASSWORD_BCRYPT)."\n");' \
      "$STATE/auth.hash" "$DEFAULT_PASSWORD"
  warn "no terminal available — initial password set to \"$DEFAULT_PASSWORD\""
  warn "${RED}CHANGE IT NOW${OFF} from the page (🔑 button in the header)"
fi
chown "$TARGET_USER:$TARGET_USER" "$STATE/auth.hash"
chmod 600 "$STATE/auth.hash"

step "Regeneration cron"
if crontab -u "$TARGET_USER" -l 2>/dev/null | grep -q 'regenerate.sh'; then
  ok "already installed"
else
  install -d -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/logs"
  ( crontab -u "$TARGET_USER" -l 2>/dev/null
    echo "# Harness Library — daily regeneration"
    echo "22 6 * * * $DIR/regenerate.sh >> $TARGET_HOME/logs/harness-library.log 2>&1"
    echo "# Serves the page's Regenerate button"
    echo "* * * * * $DIR/regenerate.sh --watch >> $TARGET_HOME/logs/harness-library.log 2>&1"
  ) | crontab -u "$TARGET_USER" -
  ok "daily at 06:22 + 1-minute watcher"
fi

step "Reloading services"
nginx -t || { echo "${RED}nginx -t failed — route NOT applied${OFF}"; exit 1; }
systemctl reload "php$PHPVER-fpm" && ok "php$PHPVER-fpm"
systemctl reload nginx && ok "nginx"

step "Generating the inventory"
sudo -u "$TARGET_USER" bash "$DIR/regenerate.sh" || warn "generation failed (check the log)"

step "Verification"
IP="$(hostname -I | awk '{print $1}')"
CODE="$(curl -sk -o /dev/null -w '%{http_code}' "http://$IP/claude-inventory/")"
API="$(curl -sk -X POST "http://$IP/claude-inventory/api.php" \
        -H 'Content-Type: application/json' -d '{"action":"status"}' -o /dev/null -w '%{http_code}')"
[[ "$CODE" =~ ^(200|301)$ ]] && ok "page responds ($CODE)" || bad "page responded $CODE"
[[ "$API" == "200" ]] && ok "api.php responds (200)" || bad "api.php responded $API"

echo
echo "${GRN}Done.${OFF}  http://$IP/claude-inventory/"
