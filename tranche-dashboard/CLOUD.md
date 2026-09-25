# Running the dashboard in the cloud (DigitalOcean + Tailscale)

The app runs 24/7 on a small DigitalOcean server, so your PC can be off.
Only devices signed in to your Tailscale account can reach it; it isn't visible
on the public internet. It also asks for a password.

About 20–30 minutes, one time. Cost: about $6/month for the server. Tailscale is
free for personal use. Check current prices on their sites.

---

## 1. Create a Tailscale account and put it on your PC and phone

1. Go to <https://tailscale.com> and sign up. "Sign in with Google" is simplest.
2. Install Tailscale on your **PC** (<https://tailscale.com/download>) and your
   **phone** (App Store or Google Play). Sign in to both with the same account.

## 2. Create the server (a "droplet")

1. Sign up at <https://www.digitalocean.com> and add a payment method.
2. Click **Create → Droplets**, then choose:
   - **Region:** New York (close to the exchanges and the data providers).
   - **Image:** **Ubuntu 24.04 (LTS) x64**.
   - **Size:** Basic → Regular → **$6/mo (1 GB RAM)**.
   - **Authentication:** **Password**. Pick a strong one and save it in your
     password manager.
   - Optional: tick **Automated backups** (a small extra cost; weekly
     snapshots of the whole server).
   - Hostname: `tranche`.
3. Click **Create Droplet** and wait about a minute until it's running.
4. Open the droplet and click **Console** (top right). A black terminal window
   opens, already logged in as `root`. You'll paste everything below into it.
   To paste, right-click → Paste, or Ctrl+Shift+V.

## 3. Give the server read-only access to the code

The repository is private, so the server needs its own read-only key.

**3a.** Paste this into the console and press Enter:

```
apt-get update -q && apt-get install -y -q git && ssh-keygen -t ed25519 -N "" -q -f /root/.ssh/tranche_deploy && cat /root/.ssh/tranche_deploy.pub
```

It prints one line starting with `ssh-ed25519`. Select and copy that whole line.

**3b.** On your PC, open
<https://github.com/Stormport-Capital/autoagent/settings/keys/new>
- Title: `tranche server`
- Key: paste the line
- **Leave "Allow write access" unticked.**
- Click **Add key**.

(You need admin rights on the repository. If the page says you don't, ask the
repository owner to add it.)

**3c.** Back in the console, paste:

```
mkdir -p /opt/tranche && GIT_SSH_COMMAND="ssh -i /root/.ssh/tranche_deploy -o StrictHostKeyChecking=accept-new" git clone -q -b claude/dashboard-stock-trading-9iy2hb git@github.com:Stormport-Capital/autoagent.git /opt/tranche/autoagent && bash /opt/tranche/autoagent/tranche-dashboard/deploy/install.sh
```

## 4. Answer the installer's questions

The installer sets everything up and stops twice for you:

1. **Keys.** It asks for each key one at a time. Paste each and press Enter.
   Your typing is hidden, which is normal. Press Enter on an empty line to skip
   an optional one. Use the **same keys as in your PC's `.env`**; your new FMP
   key goes in `FMP_API_KEY`. Then **choose a dashboard password.** You'll type
   it on your phone and PC, and it's separate from your DigitalOcean password.
2. **Tailscale login.** It prints a link like `https://login.tailscale.com/a/...`.
   Open it on your PC, sign in, and approve the new device `tranche`.

At the end it prints **Done** and the address.

## 5. Open the dashboard

On your PC or phone, with Tailscale switched on, go to:

**http://tranche:8050**

If that name doesn't resolve, use the `http://100.x.y.z:8050` address the
installer printed. Log in with any user name and your dashboard password.

## 6. Retire the PC copy

**Only one copy should run.** Both would try to trade the same Alpaca paper
accounts. Close the black window on your PC, and don't use `Start Dashboard.bat`
again unless the server is down. On the server, add your symbols and settings
and switch on **"Send the model's trades to Alpaca paper"** on each tab.

The server starts with empty books. Your PC's history stays in your PC's
`tranche.db` files and in your Google Drive backups.

---

## Everyday use

Nothing to do. The server:
- **Runs all the time** and restarts itself after a crash or a reboot.
- **Updates itself** every weekday at 8:45 AM ET: it restarts, pulls the
  latest code, then catches up.
- **Backs up both books.** Copies go into `/opt/tranche/backups` at startup,
  every trading day after the close, and before any Reset. You can also click
  **Download backup** in the dashboard header at any time to save a copy to
  your PC.

Handy console commands (DigitalOcean → droplet → **Console**):

| Command | What it does |
|---|---|
| `journalctl -u tranche -f` | Live log, including the startup connection check (Ctrl+C to leave) |
| `systemctl restart tranche` | Restart now (also pulls updates) |
| `systemctl status tranche` | Is it running? |
| `nano /opt/tranche/autoagent/tranche-dashboard/.env` | Edit keys or password (Ctrl+O, Enter, Ctrl+X), then restart |

## Google Drive backups (optional)

The server can't see your G: drive, so it uses **rclone** to copy the backup
folder to Google Drive every weekday at 4:30 PM ET, into a folder called
`Tranche backups`.

1. In the server console run:
   ```
   bash /opt/tranche/autoagent/tranche-dashboard/deploy/drive-backup.sh
   ```
2. It walks you through `rclone config` (the answers are printed on screen).
   When it asks *"Use web browser to automatically authenticate?"* answer
   **n**. It then shows a command that starts with `rclone authorize "drive"`.
3. On your **PC**:
   1. Download rclone for Windows from <https://rclone.org/downloads/> and
      unzip it.
   2. Open PowerShell in that folder and run the command it showed, starting
      with `.\rclone.exe` instead of `rclone`.
   3. A browser opens. Sign in to Google and allow access. PowerShell then
      prints a long code.
4. Paste that code back into the server console and finish the prompts.

It makes a first copy straight away. Check Google Drive for `Tranche backups`.

## If something's wrong

- **Can't open http://tranche:8050:** check that Tailscale is switched on on
  that device and signed in to the same account. Try the `100.x.y.z` address.
  In the console, `systemctl status tranche` should say **active (running)**.
- **Keys failing:** `journalctl -u tranche -n 50` shows the startup connection
  check. Fix the key with `nano` (see above) and `systemctl restart tranche`.
- **Starting over:** the installer is safe to run again. It keeps your `.env`
  and your databases:
  `bash /opt/tranche/autoagent/tranche-dashboard/deploy/install.sh`
