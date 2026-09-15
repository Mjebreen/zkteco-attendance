#!/usr/bin/env sh
# Consistent online backup of the SQLite volume (safe while web/collector are running).
# Usage: scripts/backup_sqlite.sh [backup-dir]   (default ./backups)
set -eu

BACKUP_DIR="${1:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Uses sqlite3's .backup through the running web container, so WAL pages are included.
docker compose exec -T web python - <<'PY' > "$BACKUP_DIR/attendance-$STAMP.db"
import os, sqlite3, sys, tempfile
url = os.environ.get("DATABASE_URL", "sqlite:////data/attendance.db")
path = url.split("sqlite:///", 1)[-1]
src = sqlite3.connect(path)
fd, tmp = tempfile.mkstemp(suffix=".db")
os.close(fd)
dst = sqlite3.connect(tmp)
src.backup(dst)
dst.close(); src.close()
with open(tmp, "rb") as f:
    sys.stdout.buffer.write(f.read())
os.remove(tmp)
PY

gzip -f "$BACKUP_DIR/attendance-$STAMP.db"
echo "wrote $BACKUP_DIR/attendance-$STAMP.db.gz"

# Keep the last 30 backups.
ls -1t "$BACKUP_DIR"/attendance-*.db.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
