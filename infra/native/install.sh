#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 24.04 (arm64) server.
#
#   sudo bash infra/native/install.sh
#
# Installs Node 22, PostgreSQL and Caddy, creates the database, and installs the
# systemd service. It does not start the app — deploy.sh does that, and it needs
# /etc/wbac.env filled in first.
#
# Safe to re-run: every step checks whether it has already been done.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/wbac}
APP_USER=${APP_USER:-ubuntu}

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Updating package lists"
apt-get update -qq

echo "==> Installing basics"
apt-get install -y -qq ca-certificates curl gnupg git debian-keyring debian-archive-keyring apt-transport-https

# --- Node 22 -----------------------------------------------------------------
# Ubuntu 24.04 ships Node 18, which is older than this app supports.
if ! command -v node >/dev/null || [[ "$(node -v)" != v22.* ]]; then
  echo "==> Installing Node 22"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
else
  echo "==> Node $(node -v) already present"
fi

# --- PostgreSQL --------------------------------------------------------------
echo "==> Installing PostgreSQL"
apt-get install -y -qq postgresql postgresql-contrib
systemctl enable --now postgresql

# --- Caddy -------------------------------------------------------------------
if ! command -v caddy >/dev/null; then
  echo "==> Installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq caddy
else
  echo "==> Caddy already present"
fi

# --- Service account ---------------------------------------------------------
# The app runs as its own unprivileged user rather than as `ubuntu`, which on Ubuntu
# cloud images is granted passwordless sudo. The app needs no privileges at all: it
# writes nothing to disk, logging to journald and keeping everything else in Postgres.
# So a compromise of the Node process gets an account with no shell and no sudo.
if ! id -u wbac >/dev/null 2>&1; then
  echo "==> Creating service account wbac"
  useradd --system --no-create-home --shell /usr/sbin/nologin wbac
else
  echo "==> Service account wbac already exists"
fi

# --- Database ----------------------------------------------------------------
# A password is generated once and written into /etc/wbac.env. Re-running this
# script will not change it.
if ! sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='wbac'" | grep -q 1; then
  DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
  echo "==> Creating database role and database"
  sudo -u postgres psql -qc "create user wbac with password '${DB_PASSWORD}'"
  sudo -u postgres createdb -O wbac wbac
  NEW_DB=1
else
  echo "==> Database role already exists, leaving it alone"
  NEW_DB=0
fi

# --- Environment file --------------------------------------------------------
# Root-owned and unreadable by anyone else: it holds both passwords. systemd reads
# it before dropping to the app user.
if [[ ! -f /etc/wbac.env ]]; then
  echo "==> Writing /etc/wbac.env (you must fill in the blanks)"
  cat > /etc/wbac.env <<EOF
NODE_ENV=production
PORT=3000
DATABASE_URL=postgres://wbac:${DB_PASSWORD:-CHANGE_ME}@localhost:5432/wbac

# The address students will type, exactly as it appears in the browser including
# https://. This is the single place the site's name is configured: the app accepts
# connections from it, and deploy.sh derives Caddy's certificate hostname from it.
# deploy.sh refuses to run until this is changed.
WEB_ORIGIN=https://CHANGE_ME

# Secret for creating a room. Anyone with this can start and drive a class.
HOST_PASSWORD=CHANGE_ME
EOF
  chmod 600 /etc/wbac.env
  chown root:root /etc/wbac.env
else
  echo "==> /etc/wbac.env already exists, leaving it alone"
fi

# --- App directory -----------------------------------------------------------
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# Caddy serves the built client straight from this directory and runs as its own
# user, so the path has to be traversable by others.
chmod 755 "$APP_DIR"

# --- systemd -----------------------------------------------------------------
if [[ -f "$APP_DIR/infra/native/wbac.service" ]]; then
  echo "==> Installing systemd service"
  install -m 644 "$APP_DIR/infra/native/wbac.service" /etc/systemd/system/wbac.service
  systemctl daemon-reload
  systemctl enable wbac >/dev/null
fi

echo
echo "Done."
echo
if [[ "$NEW_DB" == "1" ]]; then
  echo "  A database password was generated and written into /etc/wbac.env."
fi
echo "  Next:"
echo "    1. sudo nano /etc/wbac.env       # set HOST_PASSWORD, check WEB_ORIGIN"
echo "    2. clone the repo into $APP_DIR"
echo "    3. sudo bash $APP_DIR/infra/native/deploy.sh"
