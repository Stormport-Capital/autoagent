#!/usr/bin/env bash
# One-time setup of the tranche dashboard on a fresh Ubuntu server (DigitalOcean
# droplet). Run as root from the cloned repo:
#   bash /opt/tranche/autoagent/tranche-dashboard/deploy/install.sh
# Safe to re-run: it keeps an existing .env and databases.
set -euo pipefail

ROOT=/opt/tranche                 # home of the service user
REPO=$ROOT/autoagent              # git checkout
APP=$REPO/tranche-dashboard
VENV=$ROOT/venv
ENVFILE=$APP/.env
KEY_SRC=/root/.ssh/tranche_deploy # deploy key created before cloning

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Run as root (the DigitalOcean console logs you in as root)."; exit 1; }
[ -d "$APP" ] || { echo "Expected the repo at $REPO - see CLOUD.md step 3."; exit 1; }

say "1/6 System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q python3 python3-venv git ufw curl sqlite3 >/dev/null

say "2/6 Service user and code"
id tranche >/dev/null 2>&1 || useradd --system --home-dir "$ROOT" --shell /usr/sbin/nologin tranche
mkdir -p "$ROOT/.ssh" "$ROOT/backups"
if [ -f "$KEY_SRC" ]; then
  install -m 600 "$KEY_SRC" "$ROOT/.ssh/deploy_key"
  git -C "$REPO" config core.sshCommand \
    "ssh -i $ROOT/.ssh/deploy_key -o StrictHostKeyChecking=accept-new"
fi
git config --system --add safe.directory "$REPO" || true
chown -R tranche:tranche "$ROOT"
chmod 700 "$ROOT/.ssh"

say "3/6 Python environment"
[ -d "$VENV" ] || sudo -u tranche python3 -m venv "$VENV"
sudo -u tranche "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

say "4/6 Keys and settings (.env)"
if [ -f "$ENVFILE" ]; then
  echo "Keeping the existing $ENVFILE (edit it with: nano $ENVFILE)"
else
  echo "Paste each value and press Enter. Typing is hidden for secrets."
  echo "Leave a line empty to skip it."
  ask()  { local v; read -r -p "  $1: " v; printf '%s' "$v"; }
  asks() { local v; read -r -s -p "  $1: " v; echo >&2; printf '%s' "$v"; }
  FMP=$(asks "FMP_API_KEY (your FMP key)")
  POLY=$(asks "POLYGON_API_KEY (optional)")
  A1=$(asks "APCA_API_KEY_ID (hourly paper account)")
  S1=$(asks "APCA_API_SECRET_KEY (hourly paper account)")
  A2=$(asks "APCA_15M_API_KEY_ID (15-min paper account, optional)")
  S2=$(asks "APCA_15M_API_SECRET_KEY (15-min paper account, optional)")
  while :; do
    PW=$(asks "Choose a dashboard password")
    [ -n "$PW" ] && break
    echo "  A password is required on a server."
  done
  PROVIDER=fmp; [ -n "$FMP" ] || PROVIDER=polygon
  umask 077
  cat > "$ENVFILE" <<ENV
FMP_API_KEY=$FMP
POLYGON_API_KEY=$POLY
TRANCHE_PROVIDER=$PROVIDER
APCA_API_KEY_ID=$A1
APCA_API_SECRET_KEY=$S1
APCA_15M_API_KEY_ID=$A2
APCA_15M_API_SECRET_KEY=$S2
TRANCHE_PASSWORD=$PW
TRANCHE_BACKUP_DIR=$ROOT/backups
ENV
  umask 022
fi
chown tranche:tranche "$ENVFILE"
chmod 600 "$ENVFILE"

say "5/6 Tailscale (private network)"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "Open the login link below on your PC, sign in to Tailscale and approve this server."
  tailscale up --hostname=tranche
fi
echo "Tailscale address: $(tailscale ip -4 | head -1)"

say "6/6 Firewall and always-on service"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 >/dev/null
ufw --force enable >/dev/null
chmod +x "$APP/deploy/run.sh"
install -m 644 "$APP/deploy/tranche.service" /etc/systemd/system/tranche.service
install -m 644 "$APP/deploy/tranche-update.service" /etc/systemd/system/tranche-update.service
install -m 644 "$APP/deploy/tranche-update.timer" /etc/systemd/system/tranche-update.timer
systemctl daemon-reload
systemctl enable --now tranche.service tranche-update.timer
sleep 8
systemctl --no-pager --lines=25 status tranche.service || true

IP=$(tailscale ip -4 | head -1)
cat <<DONE

============================================================
 Done. The dashboard is running and will stay running.

 On a device signed in to the same Tailscale account, open:
     http://tranche:8050      (or http://$IP:8050)
 Log in with any user name and the password you chose.

 Useful commands (paste into this console):
   journalctl -u tranche -f          live log (Ctrl+C to leave)
   systemctl restart tranche         restart (also pulls updates)
   nano $ENVFILE                     edit keys, then restart
============================================================
DONE
