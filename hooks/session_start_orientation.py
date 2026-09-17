#!/usr/bin/env python3
"""Inject read-only Git branch and status context at session start."""

import json
from pathlib import Path
import subprocess
import sys


def run(args: list[str], cwd: str):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    requested_cwd = payload.get("cwd") or "."
    if not isinstance(requested_cwd, str):
        requested_cwd = "."
    probe_cwd = requested_cwd
    missing_cwd = False
    try:
        if not Path(probe_cwd).is_dir():
            probe_cwd = str(Path.home())
            missing_cwd = True
    except (OSError, ValueError):
        probe_cwd = str(Path.home())
        missing_cwd = True

    inside = run(["git", "rev-parse", "--is-inside-work-tree"], probe_cwd)
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        context = (
            "Repo orientation (auto-injected): the current directory is not inside "
            "a Git repository. Locate the actual checkout before running Git commands."
        )
    else:
        branch_res = run(["git", "branch", "--show-current"], probe_cwd)
        branch = (branch_res.stdout.strip() if branch_res else "") or "(detached HEAD)"
        status_res = run(["git", "status", "--short"], probe_cwd)
        status = status_res.stdout.strip() if status_res else ""
        if status:
            context = (
                f"Repo orientation (auto-injected): on branch '{branch}' with "
                f"uncommitted changes:\n{status}"
            )
        else:
            context = f"Repo orientation (auto-injected): on branch '{branch}', working tree clean."

    if missing_cwd:
        context += (
            f" Requested workspace path '{requested_cwd}' does not exist; used "
            f"'{probe_cwd}' for this read-only check."
        )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
