"""Publish untrusted inbox uploads into storage unavailable to SSH accounts.

Inbox paths are opened relative to pinned directory descriptors. Uploaded bytes
are copied into a new inode; moving/chowning an uploaded inode is not sufficient
because a client may still have an open writable handle to it.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .common import ARCHIVE, LOG, read_json, write_json


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def manifest_for(path):
    size, digest = digest_file(path)
    return {"version": 1, "name": Path(path).name, "size": size, "sha256": digest}


def validate_manifest(value, name):
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
            or value.get("name") != name or not ARCHIVE.fullmatch(name)
            or type(value.get("size")) is not int or value["size"] <= 0
            or not isinstance(value.get("sha256"), str)
            or len(value["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["sha256"])):
        raise ValueError("Invalid upload manifest")
    return value


def matches_receipt(receipt, manifest, folder):
    return (isinstance(receipt, dict) and receipt.get("folder") == folder
            and all(receipt.get(key) == manifest[key]
                    for key in ("version", "name", "size", "sha256")))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_regular(directory_fd, name, maximum=None):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Uploads must be regular files without hard links")
        if maximum is not None and info.st_size > maximum:
            raise ValueError("Upload exceeds size limit")
        return os.fdopen(fd, "rb"), info
    except BaseException:
        os.close(fd)
        raise


def accept_upload(directory_fd, name, destination, receipts, folder):
    """Accept one ready manifest. All destination paths are receiver-owned."""
    with open_regular(directory_fd, name + ".ready", 4096)[0] as stream:
        manifest = validate_manifest(json.loads(stream.read(4097)), name)
    destination = Path(destination)
    receipts = Path(receipts)
    final = destination / name
    receipt_path = receipts / (name + ".json")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipts.mkdir(parents=True, exist_ok=True, mode=0o755)

    if receipt_path.exists():
        if not matches_receipt(read_json(receipt_path), manifest, folder) or not final.is_file():
            raise ValueError("Previously accepted filename cannot be replaced")
    else:
        # Never preserve client-supplied mtimes or use an uploaded inode in the
        # protected store. A bounded read also prevents an append loop from
        # making publication run forever.
        source, before = open_regular(directory_fd, name, manifest["size"])
        temporary = None
        try:
            with source, tempfile.NamedTemporaryFile(dir=destination, prefix=".accept-", delete=False) as target:
                temporary = Path(target.name)
                digest = hashlib.sha256()
                remaining = manifest["size"]
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Incomplete upload")
                    target.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size != manifest["size"] or after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns
                        or digest.hexdigest() != manifest["sha256"]):
                    raise ValueError("Upload size, hash, or contents changed during acceptance")
                target.flush()
                os.fsync(target.fileno())
            if final.exists():
                # Recovery after a crash between archive publication and the
                # receipt. Never replace an existing accepted archive.
                if final.is_symlink() or digest_file(final) != (manifest["size"], manifest["sha256"]):
                    raise ValueError("Existing archive cannot be replaced")
            else:
                os.link(temporary, final)  # atomic publication, refuses overwrite
                sync_directory(destination)
            write_json(receipt_path, {**manifest, "folder": folder, "accepted_at": final.stat().st_mtime})
            receipt_path.chmod(0o644)
            sync_directory(receipts)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # These unlinks are relative to the pinned inbox directory, never a client
    # symlink. Clients can delete their staging files, but not accepted copies.
    os.unlink(name + ".ready", dir_fd=directory_fd)
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    LOG.info("Accepted %s/%s", folder, name)


def publish_pending(config):
    root = Path(config.get("inbox_dir", "/srv/inboxes"))
    backups = Path(config.get("backup_dir", "/backups"))
    failures = []
    # Only provisioned account/folder paths are considered. The uploader cannot
    # rename these directories because their parents belong to the receiver.
    for user, settings in config["senders"].items():
        for folder in settings["folders"]:
            inbox = root / user / "uploads" / folder
            try:
                fd = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for request in sorted(os.listdir(fd)):
                        if request.endswith(".ready") and ARCHIVE.fullmatch(request[:-6]):
                            try:
                                accept_upload(fd, request[:-6], backups / folder,
                                              root / user / "receipts" / folder, folder)
                            except (OSError, ValueError, TypeError):
                                LOG.exception("Rejected upload %s/%s/%s", user, folder, request)
                                failures.append((user, folder, request))
                finally:
                    os.close(fd)
            except OSError:
                LOG.exception("Cannot inspect inbox %s", inbox)
                failures.append((user, folder, ""))
    return failures


def remove_receipt(config, folder, name):
    root = Path(config.get("inbox_dir", "/srv/inboxes"))
    for user, settings in config.get("senders", {}).items():
        if folder in settings["folders"]:
            (root / user / "receipts" / folder / (name + ".json")).unlink(missing_ok=True)
