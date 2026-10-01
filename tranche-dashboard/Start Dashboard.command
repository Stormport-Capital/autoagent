#!/bin/bash
# Double-click on macOS to start the dashboard. Close the window to stop it.
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python) || { echo "Install Python from https://www.python.org/downloads/"; read -r; exit 1; }
git pull --ff-only -q 2>/dev/null || echo "Could not check for updates - starting the current version."
"$PY" -m pip install -q --disable-pip-version-check -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - paste your keys after the = signs, save, and close TextEdit."
  open -W -e .env
fi
"$PY" app.py --open
