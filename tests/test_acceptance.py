import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from backupkit import acceptance, receiver, sender
from backupkit.common import archives, read_json


@unittest.skipUnless(os.name == "posix", "Secure inbox publication uses Linux directory descriptors")
class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {"backup_dir": str(self.root / "protected"),
                       "inbox_dir": str(self.root / "inboxes"),
                       "senders": {"backup": {"authorized_keys_file": "/keys/one", "folders": ["one"]},
                                   "backup_other": {"authorized_keys_file": "/keys/two", "folders": ["two"]}}}
        self.config = receiver.validate(self.config)
        self.inbox = self.root / "inboxes/backup/uploads/one"
        self.other = self.root / "inboxes/backup_other/uploads/two"
        self.inbox.mkdir(parents=True)
        self.other.mkdir(parents=True)
        self.name = "backup-20261017T120000000000Z-aaaaaaaaaaaa.tar.gz"
        self.source = self.inbox / self.name
        self.final = self.root / "protected/one" / self.name
        self.receipt = self.root / "inboxes/backup/receipts/one" / (self.name + ".json")

    def stage(self, data=b"original backup bytes"):
        self.source.write_bytes(data)
        manifest = acceptance.manifest_for(self.source)
        (self.inbox / (self.name + ".ready")).write_text(json.dumps(manifest))
        return manifest

    def publish(self):
        return acceptance.publish_pending(self.config)

    def test_copy_isolated_from_open_sender_handle_and_receipt_matches(self):
        manifest = self.stage()
        with self.source.open("r+b") as attacker:
            before = os.fstat(attacker.fileno())
            self.assertEqual(self.publish(), [])
            attacker.seek(0)
            attacker.write(b"destroyed")
            attacker.truncate()
            attacker.flush()
            self.assertNotEqual(before.st_ino, self.final.stat().st_ino)
        self.assertEqual(self.final.read_bytes(), b"original backup bytes")
        self.assertTrue(acceptance.matches_receipt(read_json(self.receipt), manifest, "one"))
        self.assertEqual(self.final.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.source.exists())
        self.assertFalse((self.inbox / (self.name + ".ready")).exists())

    def test_hash_mismatch_missing_archive_and_uncommitted_partial_never_acknowledged(self):
        self.stage()
        self.source.write_bytes(b"tampered")
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.assertFalse(self.final.exists())
        self.assertFalse(self.receipt.exists())
        self.source.unlink()
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        (self.inbox / (self.name + ".ready")).unlink()
        (self.inbox / (self.name + ".partial")).write_bytes(b"unfinished")
        self.assertEqual(self.publish(), [])
        self.assertFalse(self.receipt.exists())

    def test_existing_archive_cannot_be_replaced_and_same_upload_is_idempotent(self):
        original = self.stage()
        self.assertEqual(self.publish(), [])
        self.stage()
        self.assertEqual(self.publish(), [])
        self.stage(b"replacement")
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.assertEqual(self.final.read_bytes(), b"original backup bytes")
        self.assertTrue(acceptance.matches_receipt(read_json(self.receipt), original, "one"))

    def test_crash_after_publication_recovers_receipt_without_replacing_archive(self):
        self.stage()
        with patch.object(acceptance, "write_json", side_effect=OSError("simulated crash")):
            with self.assertLogs("backupkit", level="ERROR"):
                self.assertTrue(self.publish())
        inode = self.final.stat().st_ino
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.publish(), [])
        self.assertEqual(self.final.stat().st_ino, inode)
        self.assertTrue(self.receipt.exists())

    def test_symlinks_hardlinks_fifo_and_directory_replacement_are_rejected(self):
        self.stage()
        outside = self.root / "outside"
        outside.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(outside)
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.source.unlink()
        os.link(outside, self.source)
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.source.unlink()
        os.mkfifo(self.source)
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.source.unlink()
        (self.inbox / (self.name + ".ready")).unlink()
        self.inbox.rmdir()
        self.inbox.symlink_to(self.other, target_is_directory=True)
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertTrue(self.publish())
        self.assertFalse(self.final.exists())
        self.assertFalse(self.receipt.exists())

    def test_requests_outside_allowlist_are_not_published(self):
        self.stage()
        unallowed = self.inbox.parent / "two"
        self.inbox.rename(unallowed)
        self.inbox.mkdir()
        self.assertEqual(self.publish(), [])
        self.assertFalse(self.final.exists())

    def test_flood_cannot_prune_backups_inside_minimum_retention_window(self):
        self.stage()
        self.publish()
        folder = self.final.parent
        for i in range(5):
            (folder / f"backup-20990101T000000000000Z-{i:012x}.tar.gz").write_bytes(b"new")
        self.config.update(default_keep=1, max_total_backups=1)
        receiver.cleanup(self.config)
        self.assertEqual(len(archives(folder)), 6)
        old = time.time() - 8 * 86400
        os.utime(self.final, (old, old))
        receiver.cleanup(self.config)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(len(archives(folder)), 5)

    def test_source_timestamp_does_not_shorten_retention(self):
        self.stage()
        os.utime(self.source, (1, 1))
        self.assertEqual(self.publish(), [])
        self.assertGreater(self.final.stat().st_mtime, time.time() - 60)

    def test_pinned_directory_survives_parent_path_replacement(self):
        self.stage()
        fd = os.open(self.inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.inbox.rename(self.inbox.with_name("renamed"))
            self.inbox.symlink_to(self.other, target_is_directory=True)
            acceptance.accept_upload(fd, self.name, self.final.parent, self.receipt.parent, "one")
        finally:
            os.close(fd)
        self.assertEqual(self.final.read_bytes(), b"original backup bytes")
        self.assertEqual(list(self.other.iterdir()), [])


class ProtocolTests(unittest.TestCase):
    def test_receiver_requires_explicit_isolated_sender_configuration(self):
        with self.assertRaises(ValueError):
            receiver.validate({})
        for config in (
            {"senders": {"root": {"authorized_keys_file": "/one", "folders": ["a"]}}},
            {"senders": {"backup": {"authorized_keys_file": "/one", "folders": ["a", "a"]}}},
            {"senders": {"backup": {"authorized_keys_file": "/one", "folders": ["a"]},
                         "backup_other": {"authorized_keys_file": "/two", "folders": ["a"]}}},
            {"senders": {"backup": {"authorized_keys_file": "/one", "folders": ["../a"]}}},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                receiver.validate(config)

    def test_sender_requires_receipt_and_keeps_pending_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello").write_text("hello")
            key = root / "key"
            key.write_text("test key")
            host = root / "known_hosts"
            host.write_text("test host")
            remote = {"enabled": True, "host": "example.com", "identity_file": str(key),
                      "known_hosts_file": str(host), "acceptance_timeout_seconds": 1}
            config = {"remote": remote, "folders": [{"id": "one", "path": str(source)}]}
            backup_root, defaults, jobs = sender.validate(config)
            backup_root = root / "backups"
            with patch.object(sender.subprocess, "run"), patch.object(sender.time, "monotonic", side_effect=[0, 2]):
                with self.assertLogs("backupkit", level="ERROR"):
                    self.assertFalse(sender.run_job(config, defaults, jobs[0], backup_root, force=True))
            state = read_json(backup_root / "one/state.json")
            self.assertIn("pending", state)
            self.assertNotIn("last_success", state)
            self.assertTrue((backup_root / "one" / state["pending"]).exists())

    def test_receipt_binds_identity_filename_size_and_hash(self):
        manifest = {"version": 1, "name": "a", "size": 3, "sha256": "abc"}
        receipt = {**manifest, "folder": "one"}
        self.assertTrue(acceptance.matches_receipt(receipt, manifest, "one"))
        for key in receipt:
            self.assertFalse(acceptance.matches_receipt({**receipt, key: "wrong"}, manifest, "one"))


if __name__ == "__main__":
    unittest.main()
