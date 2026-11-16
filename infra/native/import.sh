#!/usr/bin/env bash
# Import segmented texts into the database.
#
#   sudo bash /opt/wbac/infra/native/import.sh --all
#   sudo bash /opt/wbac/infra/native/import.sh --slug bden-gnyis
#
# Needs root because the database URL lives in /etc/wbac.env, which is readable only by
# root. Running `npm run import` directly as `ubuntu` cannot see it and would silently
# fall back to the default connection string, then fail to authenticate.
#
# Nothing is written to disk; this only reads texts/ and talks to Postgres.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/wbac}

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0 $*" >&2
  exit 1
fi

# Read only the line needed rather than sourcing the file — it also holds passwords, and
# sourcing would try to execute whatever characters happen to be in them.
DATABASE_URL=$(grep -E '^DATABASE_URL=' /etc/wbac.env | head -1 | cut -d= -f2-)
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set in /etc/wbac.env" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "usage: sudo bash $0 --all | --slug <slug>" >&2
  exit 1
fi

cd "$APP_DIR"
DATABASE_URL="$DATABASE_URL" npm run import -w @wbac/server -- "$@"
