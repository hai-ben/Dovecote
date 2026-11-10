#!/bin/sh
# Back up a Dovecote database safely while the server runs.
# The database is in WAL mode, so copy it with SQLite's backup command, not cp.
# Backups hold every letter in plain text: keep them somewhere private.
#   deploy/backup.sh /var/lib/dovecote/dovecote.db /var/backups/dovecote 8
set -eu
DB=${1:?database path}
DEST=${2:?backup folder}
KEEP=${3:-8}
if [ ! -f "$DB" ]; then
    echo "No database at $DB. Nothing backed up." >&2
    exit 1
fi
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="$DEST/dovecote-$STAMP.db"
N=1
while [ -e "$OUT" ]; do
    OUT="$DEST/dovecote-$STAMP-$N.db"
    N=$((N + 1))
done
TMP="$OUT.partial"
trap 'rm -f "$TMP"' EXIT
# Open the source read-only, so a wrong path can never create a database,
# and only keep the copy if SQLite says it's sound.
python3 - "$DB" "$TMP" <<'PY'
import pathlib, sqlite3, sys
src = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
src.close()
result = dst.execute("PRAGMA integrity_check").fetchone()[0]
tables = dst.execute("SELECT count(*) FROM sqlite_master WHERE name = 'members'").fetchone()[0]
dst.close()
if result != "ok" or not tables:
    sys.exit(f"The copy failed its check ({result}, members table: {bool(tables)}).")
PY
chmod 600 "$TMP"
mv "$TMP" "$OUT"
ls -1t "$DEST"/dovecote-*.db | tail -n +"$((KEEP + 1))" | xargs -r rm --
echo "Backed up to $OUT"
