"""Prepare local private runtime files. Never print or copy credential contents."""

import os
import shutil
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
(codex / "config.toml").write_text(source.read_text())
(codex / "config.toml").chmod(0o600)
projects = Path("/home/maksym/projects")
for name in (".codex", ".agents"):
    (projects / name).mkdir(exist_ok=True, mode=0o700)
workspaces = projects / "codex-workspaces"
workspaces.mkdir(exist_ok=True, mode=0o700)
for name in ("luxpower-automation", "luxpower-telegram-bot"):
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
