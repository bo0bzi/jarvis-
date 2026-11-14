"""Backup / restore: one zip with Friday's memory (data folder), learned skills and settings (.env).
Saved in the backups folder next to Friday; the newest BACKUP_KEEP (default 14) are kept. Nightly runs make one."""
from __future__ import annotations

import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from . import activity
from .env import data_dir, env, env_bool, env_int
from .tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", "backups", "_pending"}


def backups_dir() -> Path:
    d = Path(env("BACKUP_DIR") or PROJECT_ROOT / "backups")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_file() -> Path:
    from . import config

    return Path(config.ENV_PATH)


def _skills_dir() -> Path:
    from .skills import skills_dir

    return skills_dir()


def _add_tree(z: zipfile.ZipFile, folder: Path, prefix: str) -> int:
    n = 0
    if not folder.exists():
        return 0
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not (set(p.relative_to(folder).parts) & SKIP_DIRS):
            z.write(p, f"{prefix}/{p.relative_to(folder).as_posix()}")
            n += 1
    return n


def make_backup(include_env: bool | None = None, label: str = "") -> Path:
    # Off by default: a backup zip is easy to copy around, so it must not carry your API keys unless you ask.
    include_env = env_bool("BACKUP_INCLUDE_ENV", False) if include_env is None else include_env
    name = f"friday-backup-{datetime.now():%Y%m%d-%H%M%S}{('-' + label) if label else ''}.zip"
    path = backups_dir() / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        n = _add_tree(z, data_dir(), "data") + _add_tree(z, _skills_dir(), "skills")
        if include_env and _env_file().exists():
            z.write(_env_file(), ".env")
            n += 1
    keep = max(1, env_int("BACKUP_KEEP", 14))
    old = sorted(backups_dir().glob("friday-backup-*.zip"))[:-keep]
    for p in old:
        p.unlink(missing_ok=True)
    activity.log("backup", f"Backup saved: {name} ({n} files)")
    return path


def list_all() -> list[Path]:
    return sorted(backups_dir().glob("friday-backup-*.zip"), reverse=True)


def restore(name: str = "latest") -> str:
    backups = list_all()
    if not backups:
        raise FileNotFoundError("There are no backups yet.")
    src = backups[0] if name in ("", "latest", "newest") else next((p for p in backups if name in p.name), None)
    if src is None:
        raise FileNotFoundError(f"No backup matching '{name}'.")
    safety = make_backup(label="before-restore")  # so a restore can itself be undone
    targets = {"data": data_dir(), "skills": _skills_dir()}
    n = 0
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if info.is_dir() or not parts or ".." in parts or Path(info.filename).is_absolute():
                continue
            if info.filename == ".env":
                dest = _env_file()
            elif parts[0] in targets and len(parts) > 1:
                dest = targets[parts[0]].joinpath(*parts[1:])
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fsrc, open(dest, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            n += 1
    activity.log("backup", f"Restored {src.name} ({n} files); safety copy {safety.name}")
    return f"Restored {n} files from {src.name}. (A safety copy of how things were is {safety.name}.) " \
           "Restart Friday so everything reloads."


@tool("backup_now", "Make a backup of Friday's memory, notes, shop jobs, learned skills and settings.")
def backup_now(ctx):
    p = make_backup()
    return f"Backup saved: {p.name} in the backups folder."


@tool("list_backups", "List Friday's backups (newest first).")
def list_backups(ctx):
    items = list_all()
    return "\n".join(f"{p.name} ({p.stat().st_size // 1024} KB)" for p in items[:15]) or "No backups yet."


@tool("restore_backup", "Restore Friday's memory, skills and settings from a backup (default: the newest). "
      "Replaces current files - a safety backup is made first.",
      {"type": "object", "properties": {"name": {"type": "string", "description": "backup file name or 'latest'"}}},
      dangerous=True, risk="delete")
def restore_backup(ctx, name: str = "latest"):
    return restore(name)
