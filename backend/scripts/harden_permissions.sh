#!/usr/bin/env sh
# Restrict secrets and data to the service account (Linux/macOS). Run from backend/.
set -eu
umask 077
[ -f .env ] && chmod 600 .env
[ -d data ] && chmod 700 data && find data -type f -exec chmod 600 {} +
[ -d backups ] && chmod 700 backups && find backups -type f -exec chmod 600 {} +
echo "Permissions restricted (.env 600, data/ and backups/ 700)."
