import contextlib
import json
import logging
import math
import os
from pathlib import Path
import re

LOG = logging.getLogger("backupkit")
JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
ARCHIVE = re.compile(r"backup-(\d{8}T\d{12}Z)-([a-f0-9]{12})\.tar\.gz\Z")


def number(value, name, minimum=1, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum or (integer and not isinstance(value, int)):
        raise ValueError(f"{name} must be {'an integer' if integer else 'a number'} >= {minimum}")
    return value


def identifier(value):
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise ValueError(f"Invalid folder id: {value!r}; use lowercase letters, digits, _ and -")
    return value


def read_json(path):
    with open(path, encoding="utf-8-sig") as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return result


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def archives(directory):
    """Only our completed, regular archive files, newest filename first."""
    return sorted((p for p in Path(directory).iterdir()
                   if ARCHIVE.fullmatch(p.name) and not p.is_symlink() and p.is_file()),
                  key=lambda p: p.name, reverse=True)


def prune(directory, keep, dry_run=False):
    removed = []
    for path in archives(directory)[keep:]:
        LOG.info("%s old backup %s", "Would remove" if dry_run else "Removing", path)
        if not dry_run:
            path.unlink()
        removed.append(path)
    return removed


@contextlib.contextmanager
def locked(path):
    """OS-owned lock; released even if the process/container is killed."""
    with open(path, "a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another process already owns {path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def logging_setup():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
