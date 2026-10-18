from pathlib import Path
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from backupkit import receiver, sender
from backupkit.acceptance import manifest_for
from backupkit.common import archives, locked, prune, read_json


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "selected folder"
        self.source.mkdir()
        (self.source / "hello.txt").write_text("hello backup", encoding="utf-8")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "binary.bin").write_bytes(bytes(range(256)))
        (self.source / "empty").mkdir()
        self.backups = self.root / "backups"
        self.config = {"backup_dir": str(self.backups), "folders": [
            {"id": "test", "path": str(self.source)}]}
        _, self.defaults, _ = sender.validate(self.config)

    def run_backup(self, force=True):
        return sender.run_job(self.config, self.defaults, self.config["folders"][0], self.backups, force)

    def make_archives(self, folder, count):
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(count):
            path = folder / f"backup-20260101T0000{i:02d}000000Z-aaaaaaaaaaaa.tar.gz"
            path.write_bytes(b"test archive")
            # Receiver retention uses trusted acceptance time, not filenames.
            old = time.time() - 9 * 86400 + i
            os.utime(path, (old, old))
            paths.append(path)
        return paths

    def test_archive_contents_and_default_retention(self):
        for _ in range(5):
            self.assertTrue(self.run_backup())
        files = archives(self.backups / "test")
        self.assertEqual(len(files), 3)
        with tarfile.open(files[0]) as archive:
            prefix = self.source.name
            self.assertEqual(archive.extractfile(f"{prefix}/hello.txt").read(), b"hello backup")
            self.assertEqual(archive.extractfile(f"{prefix}/nested/binary.bin").read(), bytes(range(256)))
            self.assertTrue(archive.getmember(f"{prefix}/empty").isdir())
            self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts
                                for name in archive.getnames()))

    def test_schedule_persists_and_per_folder_override(self):
        self.config["folders"][0].update(interval_hours=2, keep=1)
        self.assertTrue(self.run_backup())
        state = read_json(self.backups / "test" / "state.json")
        self.assertAlmostEqual(state["next_run"] - state["last_success"], 7200, places=2)
        with patch.object(sender, "create_archive", side_effect=AssertionError("must not archive")):
            self.assertTrue(self.run_backup(force=False))
        self.assertTrue(self.run_backup())
        self.assertEqual(len(archives(self.backups / "test")), 1)

    def test_environment_keep_count_controls_real_local_retention(self):
        self.config = sender.apply_folder_environment(self.config, {"TEST_KEEP": "2"})
        _, self.defaults, _ = sender.validate(self.config)
        for _ in range(4):
            self.assertTrue(self.run_backup())
        self.assertEqual(len(archives(self.backups / "test")), 2)

    def test_environment_overrides_are_independent_and_preserve_json_fallbacks(self):
        config = {"remote": {"enabled": True, "host": "example.com"}, "folders": [
            {"id": "folder1", "path": str(self.source), "keep": 7},
            {"id": "folder2", "path": str(self.source), "keep": 5},
            {"id": "folder3", "path": str(self.source), "remote": False}]}
        result = sender.apply_folder_environment(config, {
            "FOLDER1_KEEP": "2", "FOLDER1_INTERVAL_HOURS": "6",
            "FOLDER1_BANDWIDTH_KIB_PER_SECOND": "512",
            "FOLDER2_KEEP": " ", "FOLDER2_BANDWIDTH_KIB_PER_SECOND": "0",
            "FOLDER3_BANDWIDTH_KIB_PER_SECOND": "1024"})
        sender.validate(result)
        first, second, third = result["folders"]
        self.assertEqual(first["keep"], 2)
        self.assertEqual(first["interval_hours"], 6)
        self.assertEqual(sender.remote_for(result, first)["bandwidth_kib_per_second"], 512)
        self.assertEqual(second["keep"], 5)
        self.assertEqual(sender.remote_for(result, second)["bandwidth_kib_per_second"], 0)
        self.assertIsNone(sender.remote_for(result, third))
        self.assertEqual(config["folders"][0]["keep"], 7)

    def test_invalid_environment_limits_fail_before_backup(self):
        for variable, value in [("TEST_KEEP", "two"), ("TEST_KEEP", "1.5"),
                                ("TEST_KEEP", "0"), ("TEST_KEEP", "-1"),
                                ("TEST_INTERVAL_HOURS", "nan")]:
            with self.subTest(variable=variable, value=value), self.assertRaises(ValueError):
                sender.validate(sender.apply_folder_environment(self.config, {variable: value}))

    def test_local_only_environment_overrides_folder_remote_and_retains_backups(self):
        self.config["remote"] = {"enabled": True, "host": "-invalid-unused-host"}
        self.config["folders"][0]["remote"] = {"enabled": True}
        original = self.config
        self.config = sender.apply_folder_environment(self.config, {
            "SEND_TO_REMOTE": " FALSE ", "TEST_KEEP": "2",
            "TEST_BANDWIDTH_KIB_PER_SECOND": "512"})
        _, self.defaults, _ = sender.validate(self.config)
        with patch.object(sender, "upload", side_effect=AssertionError("must not upload")):
            for _ in range(4):
                self.assertTrue(self.run_backup())
        self.assertEqual(len(archives(self.backups / "test")), 2)
        state = read_json(self.backups / "test" / "state.json")
        self.assertNotIn("pending", state)
        self.assertAlmostEqual(state["next_run"] - state["last_success"], 12 * 3600, places=2)
        self.assertTrue(original["folders"][0]["remote"]["enabled"])

    def test_send_to_remote_default_honors_json_and_rejects_invalid_values(self):
        self.config["remote"] = {"enabled": True, "host": "example.com"}
        for value in (None, "", " True "):
            with self.subTest(value=value):
                environ = {} if value is None else {"SEND_TO_REMOTE": value}
                config = sender.apply_folder_environment(self.config, environ)
                self.assertIsNotNone(sender.remote_for(config, config["folders"][0]))
                config["folders"][0]["remote"] = False
                config = sender.apply_folder_environment(config, environ)
                self.assertIsNone(sender.remote_for(config, config["folders"][0]))
        with self.assertRaisesRegex(ValueError, "SEND_TO_REMOTE must be true or false"):
            sender.apply_folder_environment(self.config, {"SEND_TO_REMOTE": "flase"})

    def test_switching_to_local_only_finishes_pending_archive_without_upload(self):
        self.config["remote"] = {"enabled": True, "host": "example.com"}
        with patch.object(sender, "upload", side_effect=RuntimeError("offline")):
            with self.assertLogs("backupkit", level="ERROR"):
                self.assertFalse(self.run_backup())
        state_path = self.backups / "test" / "state.json"
        pending = read_json(state_path)["pending"]
        self.config = sender.apply_folder_environment(self.config, {"SEND_TO_REMOTE": "false"})
        with patch.object(sender, "upload", side_effect=AssertionError("must not upload")):
            with patch.object(sender, "create_archive", side_effect=AssertionError("must reuse pending")):
                self.assertTrue(self.run_backup())
        self.assertEqual(archives(state_path.parent)[0].name, pending)
        self.assertNotIn("pending", read_json(state_path))

    def test_none_path_skips_only_disabled_folder_and_its_settings(self):
        config = {"folders": [
            {"id": "folder1", "path": str(self.source)},
            {"id": "folder2", "path": "/missing-source"}]}
        for marker in ("none", "None", " NONE "):
            with self.subTest(marker=marker):
                result = sender.apply_folder_environment(config, {
                    "FOLDER2_PATH": marker, "FOLDER2_KEEP": "invalid"})
                _, _, jobs = sender.validate(result)
                self.assertEqual([job["id"] for job in jobs], ["folder1"])
                self.assertEqual(len(config["folders"]), 2)

    def test_disabled_folder_preserves_archives_and_pending_state_in_cli(self):
        directory = self.backups / "test"
        existing = self.make_archives(directory, 4)
        state_path = directory / "state.json"
        state = json.dumps({"pending": existing[0].name, "next_run": 0})
        state_path.write_text(state)
        self.config["folders"][0]["path"] = str(self.root / "does-not-exist")
        config_path = self.root / "sender.json"
        config_path.write_text(json.dumps(self.config))
        result = subprocess.run([sys.executable, "-m", "backupkit.sender", "--config", str(config_path), "--once"],
                                env={**os.environ, "TEST_PATH": "none", "TEST_KEEP": "invalid"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No active folders", result.stderr)
        self.assertEqual(len(archives(directory)), 4)
        self.assertEqual(state_path.read_text(), state)

    def test_failed_transfer_reuses_archive_and_preserves_old_backups(self):
        self.assertTrue(self.run_backup())
        self.config["remote"] = {"enabled": True, "host": "example.com"}
        with patch.object(sender, "upload", side_effect=RuntimeError("offline")):
            with self.assertLogs("backupkit", level="ERROR"):
                self.assertFalse(self.run_backup())
        state_path = self.backups / "test" / "state.json"
        pending = read_json(state_path)["pending"]
        self.assertEqual(len(archives(self.backups / "test")), 2)
        with patch.object(sender, "upload") as upload:
            with patch.object(sender, "create_archive", side_effect=AssertionError("must reuse pending")):
                self.assertTrue(self.run_backup())
            self.assertEqual(upload.call_args.args[0].name, pending)
        self.assertNotIn("pending", read_json(state_path))

    def test_incomplete_archive_not_published(self):
        destination = self.backups / "test"
        with patch.object(tarfile.TarFile, "add", side_effect=OSError("read failed")):
            with self.assertRaises(OSError):
                sender.create_archive(self.source, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_receiver_retention_is_per_folder_and_ignores_other_files(self):
        first = self.make_archives(self.backups / "one", 5)
        second = self.make_archives(self.backups / "two", 5)
        unrelated = self.backups / "one" / "manual.tar.gz"
        unrelated.touch()
        partial = self.backups / "one" / (first[0].name + ".partial")
        partial.touch()
        config = {"backup_dir": str(self.backups), "default_keep": 3,
                  "senders": {"backup": {"authorized_keys_file": "/test/keys", "folders": ["one", "two"]}},
                  "folders": {"two": {"keep": 1}}}
        receiver.cleanup(receiver.validate(config))
        self.assertEqual(archives(self.backups / "one"), list(reversed(first[-3:])))
        self.assertEqual(archives(self.backups / "two"), [second[-1]])
        self.assertTrue(unrelated.exists())
        self.assertTrue(partial.exists())

    def test_receiver_dry_run_and_global_cap(self):
        self.make_archives(self.backups / "one", 5)
        self.make_archives(self.backups / "two", 4)
        config = {"backup_dir": str(self.backups), "max_total_backups": 2,
                  "senders": {"backup": {"authorized_keys_file": "/test/keys", "folders": ["one", "two"]}}}
        receiver.cleanup(receiver.validate(config), dry_run=True)
        self.assertEqual(sum(len(archives(p)) for p in self.backups.iterdir()), 9)
        receiver.cleanup(config)
        self.assertEqual(sum(len(archives(p)) for p in self.backups.iterdir()), 2)

    def test_stale_partial_cleanup_leaves_active_upload(self):
        names = self.make_archives(self.backups / "one", 2)
        stale = names[0].with_name(names[0].name + ".partial")
        active = names[1].with_name(names[1].name + ".partial")
        stale.touch()
        active.touch()
        old = time.time() - 8 * 86400
        os.utime(stale, (old, old))
        inbox = self.root / "inboxes" / "backup" / "uploads" / "one"
        inbox.mkdir(parents=True)
        stale = stale.rename(inbox / stale.name)
        active = active.rename(inbox / active.name)
        receiver.cleanup_staging({"inbox_dir": str(self.root / "inboxes"), "senders": {"backup": {"folders": ["one"]}}})
        self.assertFalse(stale.exists())
        self.assertTrue(active.exists())

    def test_validation_rejects_unsafe_and_invalid_configuration(self):
        for value in [0, -1, True, 1.5, "3"]:
            config = {**self.config, "defaults": {"keep": value}}
            with self.subTest(keep=value), self.assertRaises(ValueError):
                sender.validate(config)
        for name in ["../escape", "bad/name", "UPPER", "", "-option"]:
            config = {**self.config, "folders": [{"id": name, "path": str(self.source)}]}
            with self.subTest(id=name), self.assertRaises(ValueError):
                sender.validate(config)
        with self.assertRaises(ValueError):
            sender.validate({**self.config, "backup_dir": str(self.source / "inside")})
        with self.assertRaises(ValueError):
            receiver.validate({"default_keep": 0})
        for remote in [{"host": "-bad"}, {"host": "ok", "directory": "/x/../escape"},
                       {"host": "ok", "bandwidth_kib_per_second": -1}]:
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                sender.validate_remote(remote)

    def test_upload_uses_scp_limit_strict_host_check_and_atomic_rename(self):
        key = self.root / "key"
        known = self.root / "known_hosts"
        key.write_text("fake test key")
        known.write_text("fake test host")
        file = self.make_archives(self.backups / "one", 1)[0]
        remote = {"host": "example.com", "identity_file": str(key),
                  "known_hosts_file": str(known), "bandwidth_kib_per_second": 512}
        receipt_reads = 0

        def transfer(command, **kwargs):
            nonlocal receipt_reads
            batch = kwargs.get("input", "")
            if batch.startswith("-get "):
                receipt_reads += 1
                if receipt_reads > 1:
                    local = Path(batch.split('"')[3])
                    local.write_text(json.dumps({**manifest_for(file), "folder": "one"}))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(subprocess, "run", side_effect=transfer) as run:
            sender.upload(file, "one", remote)
        self.assertEqual(run.call_count, 6)
        command = run.call_args_list[2].args[0]
        self.assertEqual(command[0], "scp")
        self.assertEqual(command[command.index("-l") + 1], "4096")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertTrue(command[-1].endswith(".tar.gz.partial"))
        rename = run.call_args_list[3].kwargs["input"]
        self.assertTrue(rename.startswith("rename "))
        self.assertIn('.tar.gz.partial"', rename)
        self.assertTrue(rename.endswith('.tar.gz"\n'))
        with patch.object(subprocess, "run", side_effect=[None, None, subprocess.CalledProcessError(1, "scp")]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                sender.upload(file, "one", remote)
            self.assertEqual(run.call_count, 3)  # Failed payload must never request publication.

    def test_pending_path_traversal_is_rejected(self):
        directory = self.backups / "test"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"pending": "../outside.tar.gz"}))
        with self.assertLogs("backupkit", level="ERROR"):
            self.assertFalse(self.run_backup())

    def test_lock_excludes_second_runner(self):
        with locked(self.root / "lock"):
            with self.assertRaises(RuntimeError):
                with locked(self.root / "lock"):
                    self.fail("second runner acquired lock")
        with locked(self.root / "lock"):
            pass

    def test_symlink_folders_are_not_pruned(self):
        outside = self.root / "outside"
        self.make_archives(outside, 5)
        self.backups.mkdir()
        try:
            (self.backups / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is unavailable on this host")
        receiver.cleanup({"backup_dir": str(self.backups)})
        self.assertEqual(len(archives(outside)), 5)


if __name__ == "__main__":
    unittest.main()
