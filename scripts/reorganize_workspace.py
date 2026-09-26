"""
One-time move of existing workspace files into the readable layout (see core/workspace.py):

    projects/proj-<id>/products/<title>-<hash>/     -> projects/<date>_<name>_<id>/products/<title>/v<N>_<date>/
    projects/proj-<id>/campaigns/<name>-<hash>/     -> .../campaigns/<name>/v<N>_<date>/
    projects/proj-<id>/research/<goal>-<run>.md     -> .../research/<date>_<goal>-<run>.md
    anything else                                   -> same place under the new project folder

Versions are numbered by date per title. The `assets` table paths are updated to match
and every project gets an INDEX.md. The database is backed up first.

    python scripts/reorganize_workspace.py            # dry run: print the plan only
    python scripts/reorganize_workspace.py --apply    # stop the dashboard first
"""

import argparse
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backup import backup_database  # noqa: E402
from core.db import Database  # noqa: E402
from core.workspace import AssetStore, Workspace, slugify  # noqa: E402

VERSIONED = ("products", "campaigns")
_HASHED = re.compile(r"^(.+)-([0-9a-f]{6})$")


def first_date(db, rel_prefix: str, folder: Path) -> str:
    """Earliest asset date under this path, else the oldest file's date."""
    row = db.execute("SELECT MIN(ts) AS ts FROM assets WHERE path LIKE ?",
                     (rel_prefix + "%",)).fetchone()
    if row and row["ts"]:
        return row["ts"][:10]
    files = [f for f in folder.rglob("*") if f.is_file()] if folder.is_dir() else [folder]
    stamp = min((f.stat().st_mtime for f in files), default=folder.stat().st_mtime)
    return datetime.fromtimestamp(stamp).date().isoformat()


def plan(db, ws: Workspace) -> list[tuple[str, str]]:
    """(old relative path, new relative path) for every file or folder that moves."""
    moves = []
    projects_dir = ws.root / "projects"
    if not projects_dir.is_dir():
        return moves
    for pdir in sorted(d for d in projects_dir.iterdir() if d.is_dir()):
        m = re.match(r"^proj-([0-9a-f]+)$", pdir.name)
        if not m:
            continue                      # already in the new layout
        project_id = f"proj_{m.group(1)}"
        row = db.execute("SELECT name, created_at FROM projects WHERE id = ?",
                         (project_id,)).fetchone()
        if row is None:
            print(f"skip {pdir.name}: no such project in the database")
            continue
        new_root = f"projects/{row['created_at'][:10]}_{slugify(row['name'], 30)}_{m.group(1)}"
        for entry in sorted(pdir.iterdir()):
            old = f"projects/{pdir.name}/{entry.name}"
            if entry.is_dir() and entry.name in VERSIONED:
                groups = defaultdict(list)
                for sub in entry.iterdir():
                    hm = _HASHED.match(sub.name) if sub.is_dir() else None
                    if hm:
                        groups[hm.group(1)].append((first_date(db, f"{old}/{sub.name}/", sub), sub))
                    else:
                        moves.append((f"{old}/{sub.name}", f"{new_root}/{entry.name}/{sub.name}"))
                for title, subs in groups.items():
                    for n, (date, sub) in enumerate(sorted(subs, key=lambda x: (x[0], x[1].name)), 1):
                        moves.append((f"{old}/{sub.name}", f"{new_root}/{entry.name}/{title}/v{n}_{date}"))
            elif entry.is_dir() and entry.name == "research":
                for f in sorted(entry.iterdir()):
                    name = f.name
                    if not re.match(r"^\d{4}-\d{2}-\d{2}_", name):
                        name = f"{first_date(db, f'{old}/{f.name}', f)}_{name}"
                    moves.append((f"{old}/{f.name}", f"{new_root}/research/{name}"))
            else:
                moves.append((old, f"{new_root}/{entry.name}"))
    return moves


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/company.db")
    ap.add_argument("--workspace", default="workspace")
    ap.add_argument("--apply", action="store_true", help="really move files (default: dry run)")
    args = ap.parse_args()

    db = Database(args.db)
    ws = Workspace(args.workspace)
    moves = plan(db, ws)
    if not moves:
        print("Nothing to move: the workspace already uses the new layout.")
        return
    for old, new in moves:
        print(f"{old}\n    -> {new}")
    print(f"\n{len(moves)} moves.")
    if not args.apply:
        print("Dry run. Stop the dashboard, then run again with --apply.")
        return

    print(f"Database backup: {backup_database(db)}")
    done = []
    try:
        for old, new in moves:
            src, dst = ws.safe_path(*old.split("/")), ws.safe_path(*new.split("/"))
            if dst.exists():
                raise SystemExit(f"target exists, stopping: {new}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append((old, new))
    finally:
        with db.transaction():
            for old, new in done:
                db.execute("UPDATE assets SET path = ? || substr(path, ?) "
                           "WHERE path = ? OR path LIKE ?",
                           (new, len(old) + 1, old, old + "/%"))
        print(f"Moved {len(done)} of {len(moves)}; asset paths updated for those.")
    for d in (ws.root / "projects").iterdir():
        if d.is_dir() and re.match(r"^proj-[0-9a-f]+$", d.name) and not any(f.is_file() for f in d.rglob("*")):
            shutil.rmtree(d)               # old, now empty project folder
    store = AssetStore(db, ws)
    for row in db.execute("SELECT DISTINCT project_id FROM assets WHERE project_id IS NOT NULL"):
        store.write_index(row["project_id"])
    missing = [r["id"] for r in db.execute("SELECT id, path FROM assets")
               if not ws.safe_path(*r["path"].split("/")).is_file()]
    print(f"Check: {len(missing)} asset files missing" + (f": {missing[:10]}" if missing else "."))


if __name__ == "__main__":
    main()
