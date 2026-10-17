#!/bin/sh
set -eu

keep=${MYSQL_DUMP_KEEP:-3}
case "$keep" in
    ''|*[!0-9]*|0*) echo 'MYSQL_DUMP_KEEP must be a positive integer without leading zeros' >&2; exit 1 ;;
esac

# The hook runs after compression, before mysql-backup copies to its target.
# Stage outside ready/ on the same volume, then publish with an atomic rename.
ready=/mysql-state/ready
mkdir -p "$ready"
staged=$(mktemp /mysql-state/publish.XXXXXX)
trap 'rm -f "$staged"' EXIT
cp "${DUMPFILE:?mysql-backup must provide DUMPFILE}" "$staged"
chmod 644 "$staged"
mv "$staged" "$ready/$(basename "$DUMPFILE")"

# Names are generated UTC timestamps without whitespace, ordered newest first.
# Only this producer writes or prunes this directory; the sender mounts it read-only.
find "$ready" -maxdepth 1 -type f -name 'db_backup_*.tgz' |
    sort -r | tail -n "+$((keep + 1))" |
    while IFS= read -r old; do
        rm -f "$old"
    done
