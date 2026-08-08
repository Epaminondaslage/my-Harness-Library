#!/bin/bash
# =============================================================================
# install.sh — one-line bootstrap for Harness Library.
#
#   curl -fsSL https://raw.githubusercontent.com/Epaminondaslage/my-Harness-Library/main/install.sh | sudo bash
#
# Downloads the sources to /opt/harness-library and hands over to setup.sh,
# which does the actual work (dependency check, systemd service, nginx route,
# cron, first generation).
#
# Environment variables:
#   HARNESS_PASSWORD   initial write password (skips the default)
#   HARNESS_REF        git ref to install (default: main)
#   HARNESS_PREFIX     install directory   (default: /opt/harness-library)
# =============================================================================
set -euo pipefail

REPO="Epaminondaslage/my-Harness-Library"
REF="${HARNESS_REF:-main}"
PREFIX="${HARNESS_PREFIX:-/opt/harness-library}"
TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF"

RED=$'\e[31m'; GRN=$'\e[32m'; DIM=$'\e[2m'; OFF=$'\e[0m'

echo "${DIM}Harness Library — bootstrap${OFF}"

if [[ $EUID -ne 0 ]]; then
  echo "${RED}Root required.${OFF}  Pipe into 'sudo bash', or run with sudo."
  exit 1
fi

for c in curl tar; do
  command -v "$c" >/dev/null 2>&1 || { echo "${RED}missing: $c${OFF}"; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "  downloading $REPO@$REF"
curl -fsSL "$TARBALL" | tar -xz -C "$TMP" --strip-components=1

echo "  installing to $PREFIX"
mkdir -p "$PREFIX"
cp -a "$TMP/src/." "$PREFIX/"
# O VERSION mora na raiz do repo, mas precisa acompanhar os fontes: e ele que
# a pagina mostra e que a checagem de atualizacao compara.
cp -a "$TMP/VERSION" "$PREFIX/VERSION" 2>/dev/null || true
chmod +x "$PREFIX"/*.sh

# The setup runs as root but configures things for the invoking user, which
# sudo exposes as SUDO_USER. Preserve it across the call.
SUDO_USER="${SUDO_USER:-}" HARNESS_PASSWORD="${HARNESS_PASSWORD:-}" \
  bash "$PREFIX/setup.sh"

echo
echo "${GRN}Sources kept in $PREFIX${OFF} — re-run 'sudo bash $PREFIX/setup.sh' any time."
