#!/usr/bin/env bash
# OPTIONAL: copy the server's database backups to Google Drive every weekday
# evening. Run as root once; see CLOUD.md "Google Drive backups".
set -euo pipefail
command -v rclone >/dev/null || curl -fsSL https://rclone.org/install.sh | bash
if ! sudo -u tranche -H rclone listremotes | grep -q '^gdrive:$'; then
  cat <<'HELP'
Setting up the Google Drive connection. Answer the prompts like this:
  n  (new remote)   name: gdrive   storage: drive
  client_id / client_secret: press Enter      scope: 1 (full access)
  service_account_file: Enter   advanced config: n
  "Use web browser to automatically authenticate?": n
  -> it prints a command starting with: rclone authorize "drive" ...
     run that on your PC (see CLOUD.md), paste the result back here.
  shared drive: n    then y to keep, q to quit.
HELP
  sudo -u tranche -H rclone config
fi
cat > /etc/systemd/system/tranche-drive.service <<'UNIT'
[Unit]
Description=Copy tranche backups to Google Drive

[Service]
Type=oneshot
User=tranche
ExecStart=/usr/bin/rclone copy /opt/tranche/backups "gdrive:Tranche backups"
UNIT
cat > /etc/systemd/system/tranche-drive.timer <<'UNIT'
[Unit]
Description=Weekday evening copy of tranche backups to Google Drive

[Timer]
OnCalendar=Mon..Fri 16:30 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now tranche-drive.timer
systemctl start tranche-drive.service && echo "First copy to Google Drive done: folder 'Tranche backups'."
