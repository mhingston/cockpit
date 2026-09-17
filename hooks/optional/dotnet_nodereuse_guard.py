#!/usr/bin/env python3
"""Optionally deny bare dotnet build/test commands that omit node reuse flags."""

import json
import re
import sys


PATTERN = re.compile(r"dotnet\s+(build|test)\b")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        print("{}")
        return

    if not isinstance(payload, dict):
        print("{}")
        return
    command = (payload.get("tool_input") or {}).get("command") or ""
    match = PATTERN.search(command)
    if not match:
        print("{}")
        return

    segment_end = command.find("|", match.start())
    if segment_end == -1:
        segment_end = len(command)
    segment = command[match.start():segment_end]
    if "nodeReuse:false" in segment or "NODEREUSE" in command.upper():
        print("{}")
        return

    corrected_segment = segment.rstrip() + " /nodeReuse:false -maxcpucount:1"
    remainder = command[segment_end:]
    if remainder and not remainder[0].isspace():
        remainder = " " + remainder
    corrected_command = (
        command[:match.start()]
        + "MSBUILDDISABLENODEREUSE=1 "
        + corrected_segment
        + remainder
    )
    reason = (
        "This dotnet build/test command is missing the configured MSBuild "
        "node-reuse workaround. Retry with:\n\n"
        + corrected_command
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


if __name__ == "__main__":
    main()
