#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
command -v docker >/dev/null
command -v ssh-keygen >/dev/null
docker compose version
TEST_DIR=$(mktemp -d /tmp/backupkit-test.XXXXXX)
TEST_PROJECT="backupkit-test-$$"
export TEST_DIR TEST_PROJECT

dc() { docker compose -f tests/compose.integration.yml "$@"; }
finish() {
    code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then dc logs || true; fi
    dc down --volumes --remove-orphans || true
    # The path comes exclusively from the fixed-prefix mktemp above.
    case "$TEST_DIR" in /tmp/backupkit-test.*) rm -rf -- "$TEST_DIR" ;; esac
    exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$TEST_DIR/source/documents" "$TEST_DIR/source/photos"
printf 'hello backup\n' > "$TEST_DIR/source/documents/hello.txt"
printf 'photo sample\n' > "$TEST_DIR/source/photos/photo.txt"
ssh-keygen -q -t ed25519 -N '' -f "$TEST_DIR/backup_key"
ssh-keygen -q -t ed25519 -N '' -f "$TEST_DIR/other_key"
cp "$TEST_DIR/backup_key.pub" "$TEST_DIR/authorized_keys"
cp "$TEST_DIR/other_key.pub" "$TEST_DIR/authorized_other_keys"
: > "$TEST_DIR/known_hosts"
cat > "$TEST_DIR/sender.json" <<'JSON'
{
  "backup_dir": "/backups",
  "remote": {
    "enabled": true, "host": "receiver", "port": 22,
    "bandwidth_kib_per_second": 512,
    "transfer_timeout_seconds": 60
  },
  "folders": [
    {"id": "documents", "path": "/sources/documents"},
    {"id": "photos", "path": "/sources/photos", "keep": 2, "interval_hours": 24},
    {"id": "disabled", "path": "/sources/not-mounted"}
  ]
}
JSON
cat > "$TEST_DIR/receiver.json" <<'JSON'
{"default_keep":3,"folders":{"photos":{"keep":2}},"senders":{
  "backup":{"authorized_keys_file":"/run/secrets/authorized_keys","folders":["documents","photos"]},
  "backup_other":{"authorized_keys_file":"/run/secrets/authorized_other_keys","folders":["other"]}
}}
JSON

dc build
dc up -d receiver
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    if dc exec -T receiver python -c 'import socket; socket.create_connection(("127.0.0.1", 22), 1).close()' 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [ "$ready" -ne 1 ]; then echo 'Receiver did not become ready' >&2; exit 1; fi
public_key=$(dc exec -T receiver cat /host-keys/ssh_host_ed25519_key.pub)
printf 'receiver %s\n' "$public_key" > "$TEST_DIR/known_hosts"

dc run --rm sender --check-config
dc exec -T receiver python -m backupkit.receiver --check-config
for iteration in 1 2 3 4 5; do
    dc run --rm sender --once
done

dc run --rm --entrypoint python sender -c '
from pathlib import Path
from backupkit.common import archives
assert len(archives(Path("/backups/documents"))) == 3
assert len(archives(Path("/backups/photos"))) == 2
assert not Path("/backups/disabled").exists()
assert not list(Path("/backups").rglob("*.partial"))
'
dc run --rm --entrypoint python sender /tests/security_probe.py sender
dc exec -T receiver python /tests/security_probe.py receiver
dc exec -T receiver python -m backupkit.receiver --dry-run
dc stop receiver
dc run --rm --entrypoint python receiver /tests/security_probe.py age
dc run --rm receiver --once
dc run --rm --entrypoint python receiver /tests/security_probe.py retention
printf 'SCP transfer, protected acceptance, identity isolation, attacks, and retention passed.\n'
