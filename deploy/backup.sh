#!/bin/sh
# Back up a Dovecote database safely while the server runs.
# The database is in WAL mode, so copy it with SQLite's backup command, not cp.
# Backups hold every letter in plain text: keep them somewhere private.
#   deploy/backup.sh /var/lib/dovecote/dovecote.db /var/backups/dovecote 8
set -eu
DB=${1:?database path}
DEST=${2:?backup folder}
KEEP=${3:-8}
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%d-%H%M%S)
python3 - "$DB" "$DEST/dovecote-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
chmod 600 "$DEST/dovecote-$STAMP.db"
ls -1t "$DEST"/dovecote-*.db | tail -n +"$((KEEP + 1))" | xargs -r rm --
echo "Backed up to $DEST/dovecote-$STAMP.db"
