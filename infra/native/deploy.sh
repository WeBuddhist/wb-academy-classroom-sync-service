#!/usr/bin/env bash
# Build and (re)start the app. Run after install.sh, and again for every update.
#
#   sudo bash /opt/wbac/infra/native/deploy.sh
#
# Does not touch the database or /etc/wbac.env.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/wbac}
APP_USER=${APP_USER:-ubuntu}

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

cd "$APP_DIR"

echo "==> Installing dependencies"
sudo -u "$APP_USER" npm ci

echo "==> Building the client"
# Empty means same-origin: the built client calls whatever host served it, so the
# same build works on the domain and on a bare IP.
sudo -u "$APP_USER" env VITE_API_URL="" npm run build -w @wbac/web

echo "==> Installing Caddy config"
# The site's hostname lives in one place, WEB_ORIGIN in /etc/wbac.env, so the app and
# the certificate cannot end up disagreeing about what the site is called.
# Read just that line rather than sourcing the file — it also holds passwords, and
# sourcing would execute whatever characters happen to be in them.
WEB_ORIGIN=$(grep -E '^WEB_ORIGIN=' /etc/wbac.env | head -1 | cut -d= -f2-)
if [[ -z "${WEB_ORIGIN:-}" ]]; then
  echo "WEB_ORIGIN is not set in /etc/wbac.env" >&2
  exit 1
fi
SITE_HOST=${WEB_ORIGIN%%,*}        # first entry, if several origins are listed
SITE_HOST=${SITE_HOST#https://}
SITE_HOST=${SITE_HOST#http://}
SITE_HOST=${SITE_HOST%%/*}         # drop any trailing path

# Caddy would otherwise ask Let's Encrypt for a certificate for a placeholder, fail, and
# count against the rate limit for doing so.
if [[ "$SITE_HOST" == *CHANGE_ME* || -z "$SITE_HOST" ]]; then
  echo "WEB_ORIGIN in /etc/wbac.env is still a placeholder." >&2
  echo "Set it to the address students will use, e.g. https://classroom.example.org" >&2
  exit 1
fi
echo "    site is ${SITE_HOST}"

sed "s|__SITE_HOST__|${SITE_HOST}|g" \
  "$APP_DIR/infra/native/Caddyfile.template" > /etc/caddy/Caddyfile
chmod 644 /etc/caddy/Caddyfile
# Fails loudly on a syntax error rather than leaving Caddy down.
caddy validate --config /etc/caddy/Caddyfile >/dev/null

echo "==> Installing systemd service"
install -m 644 "$APP_DIR/infra/native/wbac.service" /etc/systemd/system/wbac.service
systemctl daemon-reload
systemctl enable wbac >/dev/null

echo "==> Restarting"
systemctl restart wbac
systemctl reload-or-restart caddy

# Give the server a moment to run migrations and bind its port before reporting.
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:3000/api/health >/dev/null 2>&1; then
    echo
    echo "App is up. Health check passed."
    echo "  Logs:   journalctl -u wbac -f"
    echo "  Import: sudo bash $APP_DIR/infra/native/import.sh --all"
    exit 0
  fi
  sleep 1
done

echo
echo "The app did not come up within 30 seconds." >&2
echo "Check: journalctl -u wbac -n 50 --no-pager" >&2
exit 1
