"""Real SFTP adversarial checks; used only with disposable integration volumes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from backupkit import sender
from backupkit.common import archives, read_json


def remote_checks():
    document = archives(Path("/backups/documents"))[0]
    remote = {"host": "receiver", "port": 22, "acceptance_timeout_seconds": 30}
    # Retry after acceptance uses the existing matching protected receipt.
    sender.upload(document, "documents", remote)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for source, target in (("/run/secrets/backup_key", "key"), ("/run/secrets/other_key", "other")):
            shutil.copyfile(source, root / target)
            (root / target).chmod(0o600)
        payload = root / document.name
        payload.write_bytes(b"replacement bytes must never reach accepted storage")
        try:
            sender.upload(payload, "documents", remote)
        except ValueError:
            pass
        else:
            raise AssertionError("Sender accepted a mismatching receipt")

        def sftp(batch, user="backup", key="key", success=False):
            result = subprocess.run(
                ["sftp", "-F", "/dev/null", "-i", str(root / key), "-o", "BatchMode=yes",
                 "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "UserKnownHostsFile=/run/secrets/known_hosts", "-b", "-", f"{user}@receiver"],
                input=batch + "\n", text=True, capture_output=True, timeout=15)
            assert (result.returncode == 0) == success, (batch, result.stdout, result.stderr)

        # The second account can publish its own folder with its own key.
        sender.upload(document, "other", {**remote, "user": "backup_other", "identity_file": str(root / "other")})
        sftp('ls /uploads\nls /receipts', success=True)
        for batch in (
            f'get /backups/documents/{document.name} {root}/stolen',
            f'rm /backups/documents/{document.name}',
            f'put {payload} /backups/documents/{document.name}',
            f'get /../../backups/documents/{document.name} {root}/stolen',
            f'put {payload} /receipts/documents/{document.name}.json',
            f'rm /receipts/documents/{document.name}.json',
            'rename /receipts/documents /uploads/stolen',
            'mkdir /escape',
            'mkdir /uploads/other',
            f'get /../backup_other/receipts/other/{document.name}.json {root}/stolen',
            f'put {payload} /uploads/other/{document.name}',
            'ln -s /backups /uploads/documents/escape',
            f'ln /receipts/documents/{document.name}.json /uploads/documents/linked',
        ):
            sftp(batch)
        sftp(f'put {payload} /uploads/documents/{document.name}', user="backup_other", key="other")
        sftp("ls /uploads", user="backup_other", key="key")  # wrong identity key

        # Direct re-upload with a conflicting manifest also cannot replace an
        # accepted archive, even when a malicious client ignores sender checks.
        request = root / "request.json"
        from backupkit.acceptance import manifest_for
        request.write_text(json.dumps(manifest_for(payload)))
        sftp(f'put {payload} /uploads/documents/{document.name}\n'
             f'put {request} /uploads/documents/{document.name}.ready', success=True)
        time.sleep(3)
        receipt = root / "receipt.json"
        sftp(f'get /receipts/documents/{document.name}.json {receipt}', success=True)
        from backupkit.acceptance import matches_receipt
        assert matches_receipt(read_json(receipt), manifest_for(document), "documents")
    print("PASS: separate SSH identities, archive isolation, receipt permissions, replay rejection, and retry receipts")


def receiver_checks():
    root = Path("/backups")
    assert root.stat().st_uid == 0 and root.stat().st_mode & 0o777 == 0o700
    for folder in ("documents", "photos"):
        assert len(archives(root / folder)) == 5
    assert len(archives(root / "other")) == 1
    with tarfile.open(archives(root / "documents")[0]) as archive:
        assert archive.extractfile("documents/hello.txt").read() == b"hello backup\n"
    assert not list(root.rglob("*.partial"))
    for user in ("backup", "backup_other"):
        jail = Path("/srv/inboxes") / user
        assert jail.stat().st_uid == 0
        assert (jail / "uploads").stat().st_uid == 0
        assert (jail / "receipts").stat().st_uid == 0
        assert (jail / "receipts").stat().st_mode & 0o022 == 0
    print("PASS: protected copies remain intact after real SFTP attacks")


def age_backups():
    # Trusted operator action in disposable test storage, to test age-gated
    # retention without waiting seven days. Sender credentials cannot do this.
    for directory in Path("/backups").iterdir():
        if directory.is_dir():
            for i, path in enumerate(sorted(archives(directory))):
                stamp = time.time() - 8 * 86400 + i
                os.utime(path, (stamp, stamp))


def retention_checks():
    for folder, keep in (("documents", 3), ("photos", 2), ("other", 1)):
        files = archives(Path("/backups") / folder)
        assert len(files) == keep, (folder, len(files))
        user = "backup_other" if folder == "other" else "backup"
        receipts = list((Path("/srv/inboxes") / user / "receipts" / folder).glob("*.json"))
        assert {p.name for p in receipts} == {p.name + ".json" for p in files}
    print("PASS: trusted retention prunes expired archives and their receipts")


if __name__ == "__main__":
    {"sender": remote_checks, "receiver": receiver_checks,
     "age": age_backups, "retention": retention_checks}[sys.argv[1]]()
