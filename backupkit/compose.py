"""Launch Compose after removing disabled source bind mounts."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def prepare_project(project):
    project = deepcopy(project)
    sender = project["services"]["sender"]
    disabled = set()
    for key, value in sender.get("environment", {}).items():
        match = re.fullmatch(r"(FOLDER[0-9]+)_PATH", key)
        if match and isinstance(value, str) and value.strip().lower() == "none":
            disabled.add(f"/sources/{match[1].lower()}")
    sender["volumes"] = [mount for mount in sender.get("volumes", [])
                         if mount["target"] not in disabled]
    return project


def launch(arguments, root, env_file=None, command=("docker", "compose"), compose_file="compose.sender.yml"):
    root = Path(root).resolve()
    options = [*command, "--project-directory", str(root)]
    if env_file:
        options += ["--env-file", str(Path(env_file).resolve())]
    resolved = subprocess.run([*options, "-f", str(root / compose_file),
                               "config", "--format", "json"], cwd=root,
                              check=True, capture_output=True, text=True, encoding="utf-8")
    # Compose's rendered config already escapes literal dollar signs for reuse.
    project = prepare_project(json.loads(resolved.stdout))
    with tempfile.TemporaryDirectory(prefix="backup-compose-") as directory:
        path = Path(directory) / "compose.json"
        path.write_text(json.dumps(project), encoding="utf-8")
        return subprocess.run([*options, "-f", str(path), *arguments], cwd=root).returncode


def main():
    parser = argparse.ArgumentParser(description="Sender Compose launcher; omit mounts with FOLDERn_PATH=none")
    parser.add_argument("--env-file", help="Alternative .env file")
    parser.add_argument("--compose-file", default="compose.sender.yml", help="Sender Compose file, relative to the project")
    args, command = parser.parse_known_args()
    if not command:
        parser.error("Supply a Compose command, for example: up -d")
    try:
        return launch(command, Path(__file__).resolve().parent.parent, args.env_file, compose_file=args.compose_file)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        return exc.returncode
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
