"""Nightly encrypted backup of SQLite + OAuth tokens to iCloud Drive.

- Uses SQLite's online .backup API (safe while the app is running)
- Bundles DB + token files into a tar.gz
- Encrypts with openssl AES-256-CBC + PBKDF2 (built-in to macOS, no extra deps)
- Rotates: keeps last 30 daily backups (~1.5 MB total)
- iCloud Drive sync moves them off-Mac automatically

Restore:
  openssl enc -d -aes-256-cbc -pbkdf2 -in backup.tar.gz.enc -out backup.tar.gz \
    -pass pass:<BACKUP_PASSWORD>
  tar -xzf backup.tar.gz
"""
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "simply.db"

# iCloud Drive location — visible in Finder under iCloud Drive sidebar
BACKUP_DIR = Path(os.getenv(
    "BACKUP_DIR",
    Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    / "Simply Assistant Backups",
))

RETAIN_DAYS = int(os.getenv("BACKUP_RETAIN_DAYS", "30"))
ENV_PATH = Path(__file__).parent.parent / ".env"


def _ensure_password() -> str:
    """Return BACKUP_PASSWORD from env; generate + persist to .env if missing."""
    pwd = os.getenv("BACKUP_PASSWORD")
    if pwd:
        return pwd
    new_pwd = secrets.token_urlsafe(32)
    with ENV_PATH.open("a") as f:
        f.write(f"\nBACKUP_PASSWORD={new_pwd}\n")
    print(f"Generated BACKUP_PASSWORD and saved to .env. KEEP THIS SAFE: {new_pwd}",
          file=sys.stderr)
    return new_pwd


def _snapshot_db(target: Path) -> None:
    """Use SQLite's online .backup API — safe while app is running."""
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(target))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def _files_to_back_up(staging: Path) -> list[Path]:
    """Stage all sensitive artifacts under one directory for tarring."""
    staging.mkdir(parents=True, exist_ok=True)
    out = []
    # Database snapshot (taken via SQLite backup API)
    db_snap = staging / "simply.db"
    _snapshot_db(db_snap)
    out.append(db_snap)
    # Token files (small JSON; copy-as-is)
    for token_name in ("qb_tokens.json", "google_tokens.json", "telegram_owner.json"):
        src = DATA_DIR / token_name
        if src.exists():
            dst = staging / token_name
            shutil.copy2(src, dst)
            out.append(dst)
    return out


def _rotate(directory: Path) -> int:
    """Delete backups older than RETAIN_DAYS. Returns count deleted."""
    cutoff = datetime.now() - timedelta(days=RETAIN_DAYS)
    deleted = 0
    for f in directory.glob("simply-*.tar.gz.enc"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            deleted += 1
    return deleted


def run_backup() -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    password = _ensure_password()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    final_path = BACKUP_DIR / f"simply-{timestamp}.tar.gz.enc"

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        files = _files_to_back_up(tmp / "stage")
        # tar.gz the staged files (use stage dir name as archive root)
        archive_path = tmp / "bundle.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            for f in files:
                tar.add(f, arcname=f.name)

        # Encrypt with openssl AES-256-CBC + PBKDF2
        subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2",
             "-in", str(archive_path),
             "-out", str(final_path),
             "-pass", f"pass:{password}"],
            check=True,
        )

    deleted = _rotate(BACKUP_DIR)
    size_kb = round(final_path.stat().st_size / 1024, 1)
    total_count = len(list(BACKUP_DIR.glob("simply-*.tar.gz.enc")))
    return {
        "ok": True,
        "backup_path": str(final_path),
        "size_kb": size_kb,
        "rotated_out": deleted,
        "total_backups_kept": total_count,
        "retain_days": RETAIN_DAYS,
    }


if __name__ == "__main__":
    # Load .env so BACKUP_PASSWORD is available when run from launchd
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
    try:
        import json
        result = run_backup()
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        sys.exit(1)
