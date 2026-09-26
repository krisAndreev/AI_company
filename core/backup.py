"""
Database backups: consistent online copies (SQLite backup API, safe while the
dashboard and work loop are writing), rotated to keep the newest N.
"""

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from core.db import Database


def backup_dir_for(db: Database) -> Path | None:
    if db.path == ":memory:":
        return None
    return Path(db.path).resolve().parent / "backups"


def backup_database(db: Database, keep: int = 14, dest_dir: Path | None = None) -> Path:
    dest_dir = dest_dir or backup_dir_for(db)
    if dest_dir is None:
        raise ValueError("an in-memory database cannot be backed up")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(db.path).stem
    path = dest_dir / f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
    target = sqlite3.connect(str(path))
    try:
        db.conn.backup(target)
    finally:
        target.close()
    for old in sorted(dest_dir.glob(f"{stem}-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return path


def latest_backup_age_hours(db: Database) -> float | None:
    folder = backup_dir_for(db)
    if folder is None or not folder.is_dir():
        return None
    files = sorted(folder.glob(f"{Path(db.path).stem}-*.db"), key=lambda p: p.stat().st_mtime)
    return (time.time() - files[-1].stat().st_mtime) / 3600 if files else None


def maybe_backup(db: Database, keep: int, every_hours: float = 24) -> Path | None:
    """Back up if the newest backup is older than every_hours (or none exists)."""
    if db.path == ":memory:":
        return None
    age = latest_backup_age_hours(db)
    if age is not None and age < every_hours:
        return None
    return backup_database(db, keep)
