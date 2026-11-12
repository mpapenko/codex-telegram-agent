"""Prepare local private runtime files. Never print or copy credential contents."""

import json
import os
import shutil
import tomllib
from pathlib import Path

os.umask(0o077)
state = Path("/home/maksym/.local/share/codex-telegram")
codex = state / "codex"
codex.mkdir(parents=True, exist_ok=True, mode=0o700)
state.chmod(0o700)
codex.chmod(0o700)
auth = Path("/home/maksym/.codex/auth.json")
if not auth.is_file():
    raise SystemExit("Sign in to Codex CLI first.")
link = codex / "auth.json"
if not link.exists():
    link.symlink_to(auth)
source = Path(__file__).with_name("codex-config.toml")
config_path = codex / "config.toml"
previous = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
text = source.read_text()
# The app-server persists project trust separately from our permission profile.
for project, settings in previous.get("projects", {}).items():
    if set(settings) != {"trust_level"} or not isinstance(settings["trust_level"], str):
        raise SystemExit("Unknown project settings; inspect configuration before provisioning.")
    text += (
        f"\n[projects.{json.dumps(project, ensure_ascii=False)}]\n"
        f"trust_level = {json.dumps(settings['trust_level'])}\n"
    )
config_path.write_text(text)
config_path.chmod(0o600)
projects = Path("/home/maksym/projects")
for name in (".codex", ".agents"):
    (projects / name).mkdir(exist_ok=True, mode=0o700)
workspaces = projects / "codex-workspaces"
workspaces.mkdir(exist_ok=True, mode=0o700)
for name in (
    "luxpower-automation",
    "luxpower-telegram-bot",
    "codex-telegram-agent",
    "codex-root-broker",
):
    target = workspaces / name
    if not target.exists():
        shutil.copytree(
            projects / name,
            target,
            ignore=shutil.ignore_patterns(
                ".venv",
                ".git",
                "state",
                "config.toml",
                ".env",
                ".env.*",
                ".vscode",
                "__pycache__",
                ".pytest_cache",
                ".ruff_cache",
                "*.egg-info",
            ),
        )
print("Private Codex worker configuration prepared; credential contents untouched.")
