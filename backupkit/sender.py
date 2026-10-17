import argparse
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

from .common import ARCHIVE, LOG, identifier, locked, logging_setup, number, prune, read_json, write_json
from .acceptance import manifest_for, matches_receipt


def apply_folder_environment(config, environ=None):
    """Compose supplies per-folder settings from .env; JSON remains the fallback."""
    environ = os.environ if environ is None else environ
    config = deepcopy(config)
    if not isinstance(config.get("folders"), list):
        raise ValueError("folders must be a list")
    active = []
    for job in config["folders"]:
        prefix = identifier(job["id"]).upper()
        if environ.get(f"{prefix}_PATH", "").strip().lower() == "none":
            LOG.info("Skipping disabled folder %s", job["id"])
            continue
        for suffix, key, convert in (("KEEP", "keep", int),
                                     ("INTERVAL_HOURS", "interval_hours", float),
                                     ("BANDWIDTH_KIB_PER_SECOND", "bandwidth_kib_per_second", float)):
            variable = f"{prefix}_{suffix}"
            value = environ.get(variable, "").strip()
            if not value:
                continue
            try:
                value = convert(value)
            except ValueError as exc:
                raise ValueError(f"{variable} must be {'an integer' if convert is int else 'a number'}") from exc
            if key == "bandwidth_kib_per_second":
                if job.get("remote") is not False:
                    job.setdefault("remote", {})[key] = value
            else:
                job[key] = value
        active.append(job)
    config["folders"] = active
    return config


def validate(config):
    defaults = {"interval_hours": 12, "keep": 3, "compression_level": 6,
                "retry_seconds": 300, **config.get("defaults", {})}
    jobs = config.get("folders")
    if not isinstance(jobs, list):
        raise ValueError("folders must be a list")
    root = Path(config.get("backup_dir", "/backups")).resolve()
    seen = set()
    for job in jobs:
        name = identifier(job["id"])
        if name in seen:
            raise ValueError(f"Duplicate folder id: {name}")
        seen.add(name)
        for key in ("interval_hours", "retry_seconds"):
            number(job.get(key, defaults[key]), key)
        number(job.get("keep", defaults["keep"]), "keep", integer=True)
        level = number(job.get("compression_level", defaults["compression_level"]),
                       "compression_level", minimum=0, integer=True)
        if level > 9:
            raise ValueError("compression_level must be 0..9")
        source = Path(job["path"]).resolve()
        if source == root or source in root.parents or root in source.parents:
            raise ValueError(f"Source and backup directory must not overlap: {source}")
        remote = remote_for(config, job)
        if remote:
            validate_remote(remote)
    return root, defaults, jobs


def remote_for(config, job):
    if job.get("remote") is False:
        return None
    remote = {**config.get("remote", {}), **job.get("remote", {})}
    return remote if remote.get("enabled", False) else None


def validate_remote(remote):
    host = remote.get("host", "")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host):
        raise ValueError("remote.host must be a DNS name or IP address")
    if ":" in host:
        ipaddress.IPv6Address(host)
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", remote.get("user", "backup")):
        raise ValueError("Invalid remote.user")
    for key, default in (("directory", "/uploads"), ("receipts_directory", "/receipts")):
        directory = remote.get(key, default)
        if (not isinstance(directory, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", directory)
                or ".." in PurePosixPath(directory).parts or directory.startswith("//")):
            raise ValueError(f"remote.{key} must be an absolute safe path without '..'")
    if remote.get("directory") == "/backups":
        raise ValueError("Legacy remote.directory=/backups must be changed to /uploads; upgrade receiver too")
    number(remote.get("port", 2222), "remote.port", integer=True)
    if remote.get("port", 2222) > 65535:
        raise ValueError("remote.port must be <= 65535")
    number(remote.get("bandwidth_kib_per_second", 1024), "bandwidth_kib_per_second", minimum=0)
    number(remote.get("transfer_timeout_seconds", 86400), "transfer_timeout_seconds")
    number(remote.get("acceptance_timeout_seconds", 300), "acceptance_timeout_seconds")


def create_archive(source, directory, compression_level=6):
    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Source must be a real readable directory: {source}")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = directory / f"backup-{stamp}-{uuid.uuid4().hex[:12]}.tar.gz"
    temporary = final.with_name(final.name + ".partial")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=compression_level, dereference=False) as archive:
            archive.add(source, arcname=source.name)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    LOG.info("Created %s (%s bytes)", final, final.stat().st_size)
    return final


def upload(path, job_id, remote):
    """Upload to the private inbox; succeed only on a matching receiver receipt."""
    validate_remote(remote)
    identifier(job_id)
    if not ARCHIVE.fullmatch(path.name):
        raise ValueError("Invalid archive name")
    manifest = manifest_for(path)
    host = remote["host"]
    if ":" in host:
        host = f"[{host}]"
    destination = f"{remote.get('user', 'backup')}@{host}"
    directory = str(PurePosixPath(remote.get("directory", "/uploads")) / job_id)
    final = f"{directory}/{path.name}"
    partial = final + ".partial"
    timeout = remote.get("transfer_timeout_seconds", 86400)
    # A private copy handles Windows bind mounts and Docker secret mode differences.
    with tempfile.TemporaryDirectory(prefix="backup-ssh-") as temporary:
        key = Path(temporary) / "identity"
        shutil.copyfile(remote.get("identity_file", "/run/secrets/backup_key"), key)
        key.chmod(0o600)
        known_hosts = Path(remote.get("known_hosts_file", "/run/secrets/known_hosts")).resolve()
        if not known_hosts.is_file() or known_hosts.stat().st_size == 0:
            raise ValueError("A nonempty verified known_hosts file is required")
        options = ["-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes",
                   "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=30",
                   "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
        sftp = ["sftp", *options, "-P", str(remote.get("port", 2222)), "-b", "-", destination]
        receipt = Path(temporary) / "receipt.json"
        receipt_remote = str(PurePosixPath(remote.get("receipts_directory", "/receipts")) / job_id / (path.name + ".json"))

        def accepted(remaining):
            receipt.unlink(missing_ok=True)
            subprocess.run(sftp, input=f'-get "{receipt_remote}" "{receipt.as_posix()}"\n', text=True,
                           check=True, timeout=min(timeout, remaining), capture_output=True)
            if not receipt.exists():
                return False
            if receipt.stat().st_size > 4096 or not matches_receipt(read_json(receipt), manifest, job_id):
                raise ValueError("Receiver receipt does not match this archive; refusing upload success")
            return True

        if accepted(remote.get("acceptance_timeout_seconds", 300)):
            LOG.info("Receiver already accepted %s", path.name)
            return
        subprocess.run(sftp, input=f'-mkdir "{directory}"\n', text=True, check=True, timeout=timeout)
        command = ["scp", *options, "-P", str(remote.get("port", 2222))]
        bandwidth = remote.get("bandwidth_kib_per_second", 1024)
        if bandwidth:
            command += ["-l", str(max(1, int(bandwidth * 8))) ]
        command += [str(path.resolve()), f"{destination}:{partial}"]
        subprocess.run(command, check=True, timeout=timeout)
        subprocess.run(sftp, input=f'rename "{partial}" "{final}"\n', text=True,
                       check=True, timeout=timeout)
        request = Path(temporary) / "request.json"
        request.write_text(json.dumps(manifest), encoding="utf-8")
        subprocess.run(sftp, input=(f'put "{request.as_posix()}" "{final}.ready.partial"\n'
                                   f'rename "{final}.ready.partial" "{final}.ready"\n'),
                       text=True, check=True, timeout=timeout)
        deadline = time.monotonic() + remote.get("acceptance_timeout_seconds", 300)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Receiver did not confirm protected publication; archive remains pending")
            if accepted(remaining):
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    LOG.info("Receiver accepted %s at %s", path.name, destination)


def run_job(config, defaults, job, root, force=False):
    directory = root / job["id"]
    if directory.is_symlink():
        raise ValueError(f"Backup directory cannot be a symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    state = read_json(state_path) if state_path.exists() else {}
    now = time.time()
    if not force and now < state.get("next_run", 0):
        return True
    settings = {**defaults, **job}
    try:
        if state.get("pending"):
            name = state["pending"]
            if not isinstance(name, str) or not ARCHIVE.fullmatch(name):
                raise ValueError("Invalid pending archive in state.json")
            archive = directory / name
            if archive.is_symlink() or not archive.is_file():
                raise ValueError(f"Pending backup missing: {archive}; restore it or clear pending in state.json")
            LOG.info("Retrying pending backup %s", archive)
        else:
            # A killed process may leave an unfinished local archive. No other writer owns this lock.
            for partial in directory.glob("*.tar.gz.partial"):
                if ARCHIVE.fullmatch(partial.name.removesuffix(".partial")) and not partial.is_symlink():
                    partial.unlink()
            archive = create_archive(Path(job["path"]), directory, settings["compression_level"])
            state["pending"] = archive.name
            write_json(state_path, state)
        remote = remote_for(config, job)
        if remote:
            upload(archive, job["id"], remote)
        state.pop("pending", None)
        state["last_success"] = time.time()
        state["next_run"] = state["last_success"] + settings["interval_hours"] * 3600
        write_json(state_path, state)
        prune(directory, settings["keep"])
        return True
    except Exception:
        LOG.exception("Backup failed for %s; retry in %s seconds", job["id"], settings["retry_seconds"])
        state["next_run"] = time.time() + settings["retry_seconds"]
        write_json(state_path, state)
        return False


def main():
    parser = argparse.ArgumentParser(description="Create scheduled tar.gz backups and upload with SCP")
    parser.add_argument("--config", default="/config/sender.json")
    parser.add_argument("--once", action="store_true", help="Run all folders now, then exit; retries reuse pending archives")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging_setup()
    config = apply_folder_environment(read_json(args.config))
    root, defaults, jobs = validate(config)
    if not jobs:
        LOG.info("No active folders; no backups will run")
    if args.check_config:
        LOG.info("Configuration valid: %s folders", len(jobs))
        return 0
    root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with locked(root / ".sender.lock"):
        while not stop.is_set():
            success = True
            for job in jobs:
                if stop.is_set():
                    break
                try:
                    success = run_job(config, defaults, job, root, force=args.once) and success
                except Exception:
                    LOG.exception("Cannot process folder %s", job["id"])
                    success = False
            if args.once:
                return 0 if success else 1
            stop.wait(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
