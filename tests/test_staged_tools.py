"""The agent's sandbox gets every tool its instructions name (config.PIPELINE_TOOLS), and only
tools that exist. On 10-15 four new tools were named in the skills and not staged: the agent
counted X and Y with awk where tools/sex_evidence.py should have been."""
from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "harness"))
from harness import config  # noqa: E402


class StagedTools(unittest.TestCase):
    def test_every_tool_the_instructions_name_is_staged_and_every_staged_tool_exists(self):
        instructions = list((REPO / "agent" / ".claude" / "skills").glob("*/SKILL.md"))
        instructions += [REPO / "agent" / "AGENT.md", REPO / "agent" / "JUDGMENT-CORE.md"]
        named = {name for path in instructions for name in re.findall(r"\btools/([\w.-]+\.(?:py|sh))\b", path.read_text())}
        self.assertTrue(named)
        self.assertEqual(sorted(named - set(config.PIPELINE_TOOLS)), [])
        self.assertEqual([t for t in config.PIPELINE_TOOLS if not (REPO / "agent" / "tools" / t).is_file()], [])

    def test_every_tool_in_the_repo_is_staged_for_the_agent_or_named_as_a_build_tool(self):
        import subprocess
        tracked = subprocess.run(["git", "ls-files", "agent/tools"], capture_output=True, text=True, cwd=REPO,
                                 check=True).stdout.split()
        names = {Path(path).name for path in tracked}
        self.assertEqual(sorted(names - set(config.PIPELINE_TOOLS) - set(config.EXCLUDED_TOOLS_REASON)), [])


if __name__ == "__main__":
    unittest.main()
