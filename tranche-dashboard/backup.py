"""Safe copies of the book databases, e.g. into a Google Drive folder.

The live databases stay in the app folder: syncing a database that is being
written to (as Drive does) can corrupt it. Instead, a complete, consistent copy
is written with SQLite's online-backup API into TRANCHE_BACKUP_DIR (default:
./backups). Each copy is written under a temporary name and renamed when
complete, so a sync client never picks up a half-written file.

  on startup / daily after the close:  tranche_<book>_<YYYY-MM-DD>.db   (kept 30 days)
  right before a Reset portfolio:      tranche_<book>_pre-reset_<YYYY-MM-DD_HHMM>.db (kept)
"""

from __future__ import annotations

import os
import re
import threading
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path

from indicators import ET

DAILY_AFTER = time(16, 10)  # ET, weekdays
KEEP_DAYS = 30


class Backups(threading.Thread):
    def __init__(self, stores: dict, folder: Path, clock, keep_days: int = KEEP_DAYS):
        super().__init__(daemon=True)
        self.stores, self.folder, self.clock, self.keep_days = stores, Path(folder), clock, keep_days
        self.last: datetime | None = None
        self.last_error: str | None = None
        self.done_for: object = None  # date of the last after-close backup
        self.lock = threading.Lock()

    def run_backup(self, reason: str, only: list[str] | None = None) -> list[Path]:
        """Copy the named books (default all). Raises on failure."""
        now = self.clock.now().astimezone(ET)
        written = []
        with self.lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                for key in only or list(self.stores):
                    if reason == "pre-reset":
                        name = f"tranche_{key}_pre-reset_{now:%Y-%m-%d_%H%M}.db"
                    else:
                        name = f"tranche_{key}_{now:%Y-%m-%d}.db"
                    path = self.folder / name
                    self.stores[key].backup_to(str(path))
                    written.append(path)
                    self._prune(key, now.date())
                self.last, self.last_error = now, None
            except Exception as e:
                self.last_error = f"backup to {self.folder} failed: {e}"
                raise
        return written

    def _prune(self, key: str, today) -> None:
        pat = re.compile(rf"^tranche_{re.escape(key)}_(\d{{4}}-\d{{2}}-\d{{2}})\.db$")
        cutoff = today - timedelta(days=self.keep_days)
        for f in self.folder.iterdir():
            m = pat.match(f.name)
            if m and datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff:
                f.unlink(missing_ok=True)

    def status(self) -> dict:
        return {"folder": str(self.folder),
                "last": self.last.isoformat() if self.last else None,
                "error": self.last_error}

    def run(self) -> None:
        while True:
            now = self.clock.now().astimezone(ET)
            if (now.weekday() < 5 and now.time() >= DAILY_AFTER
                    and self.done_for != now.date()):
                try:
                    self.run_backup("daily")
                except Exception:
                    pass  # surfaced via status(); try again next minute
                else:
                    self.done_for = now.date()
            _time.sleep(60)


def backup_folder(here: Path) -> Path:
    raw = os.environ.get("TRANCHE_BACKUP_DIR", "").strip().strip('"')
    return Path(raw) if raw else here / "backups"
