"""Tests for revertly.skill — the opt-in agent-facing instructions
(Claude Code SKILL.md + AGENTS.md snippet) and the `revertly skill` command.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

from revertly import skill


class TestSkillFiles(unittest.TestCase):
    def test_install_writes_skill_with_frontmatter(self):
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"HOME": home}):
            self.assertFalse(skill.skill_installed())
            p = skill.install_claude_skill()
            self.assertTrue(os.path.isfile(p))
            self.assertTrue(p.endswith(os.path.join("skills", "revertly", "SKILL.md")))
            self.assertTrue(skill.skill_installed())
            body = open(p, encoding="utf-8").read()
            self.assertIn("name: revertly", body)         # valid frontmatter key
            self.assertIn("revertly revert", body)         # teaches the core verb
            self.assertIn("revertly redo", body)
            self.assertTrue(skill.uninstall_claude_skill())
            self.assertFalse(skill.skill_installed())

    def test_agents_snippet_covers_core_commands(self):
        for cmd in ("revertly last", "revertly diff", "revertly restore",
                    "revertly revert", "revertly undo", "revertly redo"):
            self.assertIn(cmd, skill.AGENTS_MD)
        # must carry the honest guardrail: don't disable the safety net
        self.assertIn("Never", skill.AGENTS_MD)


class TestSkillCommand(unittest.TestCase):
    def _args(self, **kw):
        base = {"install": False, "print_snippet": False, "uninstall": False}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_install_and_uninstall_via_command(self):
        from revertly.cli import cmd_skill
        with tempfile.TemporaryDirectory() as home, \
             mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(cmd_skill(self._args(install=True)), 0)
            self.assertTrue(skill.skill_installed())
            self.assertEqual(cmd_skill(self._args(uninstall=True)), 0)
            self.assertFalse(skill.skill_installed())

    def test_print_snippet(self):
        from revertly.cli import cmd_skill
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_skill(self._args(print_snippet=True))
        self.assertEqual(rc, 0)
        self.assertIn("revertly", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
