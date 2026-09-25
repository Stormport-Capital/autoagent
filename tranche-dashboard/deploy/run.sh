#!/usr/bin/env bash
# Started by systemd (tranche.service) as the "tranche" user.
cd /opt/tranche/autoagent || exit 1
git pull --ff-only -q || echo "update check failed - starting the current version"
cd tranche-dashboard
/opt/tranche/venv/bin/pip install -q --disable-pip-version-check -r requirements.txt \
  || echo "dependency install failed - starting anyway"
# Listen only on the Tailscale address, never on the public internet.
HOST=""
for _ in $(seq 1 60); do
  HOST=$(tailscale ip -4 2>/dev/null | head -1)
  [ -n "$HOST" ] && break
  sleep 5
done
if [ -z "$HOST" ]; then
  echo "Tailscale is not up - refusing to start on a public address"
  exit 1
fi
exec /opt/tranche/venv/bin/python -u app.py --host "$HOST" --port 8050
