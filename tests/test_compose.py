import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backupkit.compose import launch, prepare_project


class ComposeTests(unittest.TestCase):
    def project(self):
        return {"name": "folder-backup-sender", "services": {"sender": {
            "environment": {"FOLDER1_PATH": "D:/Documents", "FOLDER2_PATH": "none", "FOLDER3_PATH": "NONE"},
            "volumes": [
                {"type": "bind", "source": "/config/sender.json", "target": "/config/sender.json"},
                {"type": "bind", "source": "/local-backups", "target": "/backups"},
                {"type": "bind", "source": "/documents", "target": "/sources/folder1", "read_only": True,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": "/project/none", "target": "/sources/folder2"},
                {"type": "bind", "source": "/project/NONE", "target": "/sources/folder3"}]}}}

    def test_disabled_bind_mounts_are_omitted_and_enabled_mount_is_preserved(self):
        original = self.project()
        result = prepare_project(original)
        volumes = result["services"]["sender"]["volumes"]
        self.assertEqual([v["target"] for v in volumes], ["/config/sender.json", "/backups", "/sources/folder1"])
        self.assertEqual(volumes[-1], original["services"]["sender"]["volumes"][2])
        self.assertEqual(len(original["services"]["sender"]["volumes"]), 5)
        self.assertEqual(result["services"]["sender"]["environment"]["FOLDER2_PATH"], "none")

    def test_path_named_none_is_only_disabled_for_exact_marker(self):
        project = self.project()
        project["services"]["sender"]["environment"].update(FOLDER2_PATH="/data/none", FOLDER3_PATH="./none")
        self.assertEqual(prepare_project(project), project)

    def test_all_sources_can_be_disabled(self):
        project = self.project()
        project["services"]["sender"]["environment"]["FOLDER1_PATH"] = " none "
        volumes = prepare_project(project)["services"]["sender"]["volumes"]
        self.assertTrue(all(not v["target"].startswith("/sources/") for v in volumes))

    def test_launcher_passes_filtered_spec_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_paths = []

            def compose(command, **options):
                if command[-3:] == ["config", "--format", "json"]:
                    project = self.project()
                    project["services"]["sender"]["environment"]["FOLDER1_PATH"] = "/data/$$folder"
                    return subprocess.CompletedProcess(command, 0, json.dumps(project))
                path = Path(command[command.index("-f") + 1])
                temporary_paths.append(path)
                project = json.loads(path.read_text())
                self.assertEqual(project["services"]["sender"]["environment"]["FOLDER1_PATH"], "/data/$$folder")
                mounts = project["services"]["sender"]["volumes"]
                self.assertFalse(any(v["target"] == "/sources/folder2" for v in mounts))
                self.assertEqual(command[-3:], ["up", "-d", "--build"])
                return subprocess.CompletedProcess(command, 7)

            with patch("backupkit.compose.subprocess.run", side_effect=compose):
                self.assertEqual(launch(["up", "-d", "--build"], directory), 7)
            self.assertFalse(temporary_paths[0].exists())

    def test_alternative_compose_keeps_database_service_and_startup_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            def compose(command, **options):
                if command[-3:] == ["config", "--format", "json"]:
                    self.assertEqual(Path(command[command.index("-f") + 1]).name, "compose.sender.mysql.yml")
                    project = self.project()
                    project["services"]["mysql-backup"] = {"image": "databack/mysql-backup:1.5.0"}
                    project["services"]["sender"]["depends_on"] = {"mysql-backup": {"condition": "service_healthy"}}
                    return subprocess.CompletedProcess(command, 0, json.dumps(project))
                project = json.loads(Path(command[command.index("-f") + 1]).read_text())
                self.assertIn("mysql-backup", project["services"])
                self.assertEqual(project["services"]["sender"]["depends_on"]["mysql-backup"]["condition"], "service_healthy")
                self.assertFalse(any(mount["target"] == "/sources/folder2"
                                     for mount in project["services"]["sender"]["volumes"]))
                return subprocess.CompletedProcess(command, 0)

            with patch("backupkit.compose.subprocess.run", side_effect=compose):
                self.assertEqual(launch(["up", "-d"], directory, compose_file="compose.sender.mysql.yml"), 0)


if __name__ == "__main__":
    unittest.main()
