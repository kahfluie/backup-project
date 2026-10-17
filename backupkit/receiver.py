import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
import time

from .acceptance import publish_pending, remove_receipt, sync_directory
from .common import ARCHIVE, JOB_ID, LOG, archives, identifier, locked, logging_setup, number, read_json


def validate(config):
    number(config.get("default_keep", 3), "default_keep", integer=True)
    number(config.get("check_interval_hours", 24), "check_interval_hours")
    number(config.get("stale_partial_hours", 168), "stale_partial_hours")
    number(config.get("minimum_retention_hours", 168), "minimum_retention_hours")
    number(config.get("publish_interval_seconds", 2), "publish_interval_seconds")
    senders = config.get("senders")
    if not isinstance(senders, dict) or not senders:
        raise ValueError("Configure senders with separate authorized_keys_file and folders; see README migration")
    owned = set()
    key_files = set()
    for user, settings in senders.items():
        if not re.fullmatch(r"backup(?:_[a-z0-9][a-z0-9_]{0,23})?", user):
            raise ValueError("Sender usernames must be backup or backup_<lowercase_id>")
        if not isinstance(settings, dict):
            raise ValueError("Each sender must have authorized_keys_file and folders")
        keys = settings.get("authorized_keys_file")
        if not isinstance(keys, str) or not Path(keys).is_absolute() or keys in key_files:
            raise ValueError("Each sender needs a distinct absolute authorized_keys_file")
        key_files.add(keys)
        folders = settings.get("folders")
        if not isinstance(folders, list) or not folders:
            raise ValueError("Each sender needs a nonempty folders allowlist")
        for folder in folders:
            identifier(folder)
            if folder in owned:
                raise ValueError(f"Folder {folder} may belong to only one sender")
            owned.add(folder)
    backup_dir = Path(config.get("backup_dir", "/backups")).resolve()
    inbox_dir = Path(config.get("inbox_dir", "/srv/inboxes")).resolve()
    if backup_dir == Path("/srv/backups"):
        raise ValueError("Legacy /srv/backups must be migrated to /backups before starting this receiver")
    if backup_dir == inbox_dir or backup_dir in inbox_dir.parents or inbox_dir in backup_dir.parents:
        raise ValueError("Protected backup_dir and inbox_dir must not overlap")
    overrides = config.get("folders", {})
    if not isinstance(overrides, dict):
        raise ValueError("folders must map folder ids to retention settings")
    for name, settings in overrides.items():
        identifier(name)
        number(settings["keep"], f"folders.{name}.keep", integer=True)
    if config.get("max_total_backups") is not None:
        number(config["max_total_backups"], "max_total_backups", integer=True)
    return config


def cleanup(config, dry_run=False):
    root = Path(config.get("backup_dir", "/backups"))
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Backup root must be a real directory: {root}")
    total = []
    failures = []
    cutoff = time.time() - config.get("minimum_retention_hours", 168) * 3600

    def remove(path):
        if path.stat().st_mtime > cutoff:
            return False
        LOG.info("%s old backup %s", "Would remove" if dry_run else "Removing", path)
        if not dry_run:
            path.unlink()
            remove_receipt(config, path.parent.name, path.name)
        return True

    for directory in sorted(root.iterdir()):
        if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
            continue
        try:
            keep = config.get("folders", {}).get(directory.name, {}).get("keep", config.get("default_keep", 3))
            # Order by receiver-controlled acceptance time, never client names.
            files = sorted(archives(directory), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
            removed = {path for path in files[keep:] if remove(path)}
            total.extend(path for path in files if path not in removed)
        except OSError:
            LOG.exception("Retention failed for %s", directory)
            failures.append(directory.name)
    maximum = config.get("max_total_backups")
    if maximum is not None:
        for path in sorted(total, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)[maximum:]:
            remove(path)
    if failures:
        raise RuntimeError(f"Retention failed for folders: {', '.join(failures)}")
    LOG.info("Retention check complete%s", " (dry run)" if dry_run else "")


def owned_directory(path, uid=0, gid=0, mode=0o755):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Directory cannot be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    path.chmod(mode)
    # Publication receipts are meaningful only when the entire provisioned
    # directory chain, including newly-created folder entries, is durable.
    sync_directory(path)
    sync_directory(path.parent)
    return path


def prepare_ssh(config):
    import pwd

    if config.get("inbox_dir", "/srv/inboxes") != "/srv/inboxes":
        raise ValueError("SSH serving requires inbox_dir=/srv/inboxes to match sshd_config")
    # Accepted archives are outside every SSH chroot. Even legacy uid-1000
    # archives are protected by this root-owned, non-traversable mount root.
    backups = owned_directory(config.get("backup_dir", "/backups"), mode=0o700)
    owned_directory("/srv")
    root = owned_directory(config.get("inbox_dir", "/srv/inboxes"))
    Path("/run/sshd").mkdir(parents=True, exist_ok=True)
    authorized_dir = Path("/etc/ssh/authorized_keys")
    authorized_dir.mkdir(parents=True, exist_ok=True)
    seen_keys = set()
    for user, settings in config["senders"].items():
        source = Path(settings["authorized_keys_file"])
        if not source.is_file() or not source.read_text(encoding="utf-8-sig").strip():
            raise ValueError(f"A nonempty authorized_keys file is required for {user}")
        # Accept bare public keys only: authorized_keys options must not weaken
        # the receiver's account policy, and one key must not span identities.
        lines = []
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"):
                raise ValueError("Use bare SSH public key lines without authorized_keys options")
            if parts[1] in seen_keys:
                raise ValueError("A public key may authorize only one sender identity")
            seen_keys.add(parts[1])
            lines.append(line)
        if not lines:
            raise ValueError(f"No public keys for {user}")
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            subprocess.run(["useradd", "--no-create-home", "--user-group", "--home-dir", "/uploads",
                            "--shell", "/bin/sh", "--password", "*", user], check=True)
            account = pwd.getpwnam(user)
        if account.pw_uid < 1000:
            raise ValueError("Sender accounts must have unprivileged UIDs >= 1000")
        jail = owned_directory(root / user)
        owned_directory(jail / "uploads")
        owned_directory(jail / "receipts")
        for folder in settings["folders"]:
            owned_directory(jail / "uploads" / folder, account.pw_uid, account.pw_gid, 0o700)
            owned_directory(jail / "receipts" / folder)
            owned_directory(backups / folder, mode=0o700)
        target = authorized_dir / user
        target.write_text("\n".join(lines) + "\n")
        os.chown(target, 0, 0)
        target.chmod(0o644)
    # Explicit account list prevents stale keys/accounts from surviving revocation.
    Path("/run/backupkit-allow-users.conf").write_text("AllowUsers " + " ".join(config["senders"]) + "\n")
    host_dir = Path("/host-keys")
    host_dir.mkdir(exist_ok=True)
    host_dir.chmod(0o700)
    key = host_dir / "ssh_host_ed25519_key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    key.chmod(0o600)
    subprocess.run(["/usr/sbin/sshd", "-t", "-f", "/app/receiver/sshd_config"], check=True)


def cleanup_staging(config):
    """Remove only stale protocol files, using pinned inbox directory handles."""
    cutoff = time.time() - config.get("stale_partial_hours", 168) * 3600
    root = Path(config.get("inbox_dir", "/srv/inboxes"))
    for user, settings in config["senders"].items():
        for folder in settings["folders"]:
            try:
                fd = os.open(root / user / "uploads" / folder,
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    for name in os.listdir(fd):
                        base = name.removesuffix(".partial").removesuffix(".ready")
                        if not ARCHIVE.fullmatch(base):
                            continue
                        try:
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                                os.unlink(name, dir_fd=fd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(fd)
            except OSError:
                LOG.exception("Cannot clean staging folder %s/%s", user, folder)


def serve(config):
    prepare_ssh(config)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    child = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", "/app/receiver/sshd_config"])
    next_check = 0.0
    next_publish = 0.0
    try:
        while not stop.is_set():
            if child.poll() is not None:
                raise RuntimeError(f"SSH server exited: {child.returncode}")
            if time.monotonic() >= next_publish:
                publish_pending(config)
                cleanup_staging(config)
                next_publish = time.monotonic() + config.get("publish_interval_seconds", 2)
            if time.monotonic() >= next_check:
                try:
                    cleanup(config)
                    delay = config.get("check_interval_hours", 24) * 3600
                except Exception:
                    LOG.exception("Retention check failed; retrying in five minutes")
                    delay = 300
                next_check = time.monotonic() + delay
            stop.wait(1)
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    parser = argparse.ArgumentParser(description="Receive SCP uploads and enforce backup retention")
    parser.add_argument("--config", default="/config/receiver.json")
    parser.add_argument("--once", action="store_true", help="Run retention only, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview retention without deleting or serving SSH")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging_setup()
    config = validate(read_json(args.config))
    if args.check_config:
        LOG.info("Configuration valid")
        return 0
    if args.dry_run:
        cleanup(config, dry_run=True)
        return 0
    Path("/state").mkdir(parents=True, exist_ok=True)
    with locked("/state/receiver.lock"):
        if args.once:
            cleanup(config)
        else:
            serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
