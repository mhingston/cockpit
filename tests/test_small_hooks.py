import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
ORIENTATION = ROOT / "hooks" / "session_start_orientation.py"
GUARD = ROOT / "hooks" / "optional" / "dotnet_nodereuse_guard.py"


class SmallHookTests(unittest.TestCase):
    def run_hook(self, script: Path, payload: dict) -> dict:
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return json.loads(result.stdout)

    def test_orientation_is_read_only_and_reports_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            result = self.run_hook(ORIENTATION, {"cwd": str(root)})
            context = result["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Repo orientation", context)
            self.assertNotIn("uncommitted changes", context)

    def test_dotnet_guard_denies_only_the_unqualified_command(self):
        denied = self.run_hook(GUARD, {"tool_input": {"command": "dotnet test"}})
        output = denied["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("node-reuse", output["permissionDecisionReason"])

        allowed = self.run_hook(
            GUARD,
            {"tool_input": {"command": "dotnet test /nodeReuse:false -maxcpucount:1"}},
        )
        self.assertEqual(allowed, {})

    def test_governance_hook_fails_open_on_invalid_input(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "agent_governance.py")],
            input="not json",
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
