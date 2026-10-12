import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestPluginManifest(unittest.TestCase):
    def test_plugin_json_is_valid_and_named(self):
        data = json.loads(read(".claude-plugin", "plugin.json"))
        self.assertEqual(data["name"], "cuelight")
        self.assertIn("description", data)
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")

    def test_marketplace_lists_the_plugin(self):
        data = json.loads(read(".claude-plugin", "marketplace.json"))
        names = [p["name"] for p in data["plugins"]]
        self.assertIn("cuelight", names)


class TestSlashCommand(unittest.TestCase):
    def test_frontmatter_pre_approves_no_tools(self):
        # The directory holds a plugin whose command pre-approves broad shell access.
        text = read("commands", "open.md")
        front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(front, "command file needs YAML frontmatter")
        self.assertNotIn("allowed-tools", front.group(1))

    def test_each_action_is_one_bare_cuelight_command(self):
        # Claude Code offers "don't ask again" as a rule on the command's first word:
        # `cuelight *` covers Cuelight only, where `python *` would cover every script.
        text = read("commands", "open.md")
        blocks = re.findall(r"```bash\n(.*?)\n\s*```", text, re.DOTALL)
        self.assertEqual(len(blocks), 4)
        for block in blocks:
            command = block.strip()
            self.assertRegex(command, r"^cuelight(\s|$)", command)
            for joiner in ("&&", ";", "|", "cd "):
                self.assertNotIn(joiner, command)

    def test_commands_have_no_shell_variables(self):
        # Claude Code cannot check a variable before the command runs, so it will
        # not offer "don't ask again" for a command that has one. The plugin root
        # is filled in by Claude Code itself, before the command is shown.
        text = read("commands", "open.md")
        for block in re.findall(r"```bash\n(.*?)\n\s*```", text, re.DOTALL):
            self.assertNotIn("$", block.replace("${CLAUDE_PLUGIN_ROOT}", ""), block)

    def test_the_cli_runs_as_a_file_from_any_directory(self):
        # How the slash command starts it: by path, from the user's project.
        import subprocess
        import sys
        import tempfile
        out = subprocess.run([sys.executable, os.path.join(ROOT, "orchestra", "__main__.py"), "--help"],
                             cwd=tempfile.gettempdir(), capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--session", out.stdout)

    def test_documents_all_three_invocations(self):
        text = read("commands", "open.md")
        for fragment in ("--stop", "--report", "--advise"):
            self.assertIn(fragment, text)


class TestLauncher(unittest.TestCase):
    """bin/cuelight: Claude Code puts a plugin's bin/ on the Bash tool's PATH."""

    PATH = os.path.join(ROOT, "bin", "cuelight")

    def test_is_stored_as_a_posix_script_with_unix_line_endings(self):
        # Read what the repository stores, not the checkout: Git for Windows (core.autocrlf)
        # rewrites the checkout's line endings, and installs on macOS and Linux get the stored
        # file, where a carriage return breaks sh. (Git for Windows' own sh runs either.)
        import subprocess
        try:
            proc = subprocess.run(["git", "show", ":bin/cuelight"], cwd=ROOT, capture_output=True, timeout=30)
        except OSError:
            self.skipTest("git is not available")
        if proc.returncode != 0:
            self.skipTest("not a git checkout")
        data = proc.stdout
        self.assertTrue(data.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", data, "a carriage return breaks the script under sh")

    def test_is_executable_in_git(self):
        import subprocess
        try:
            out = subprocess.run(["git", "ls-files", "-s", "bin/cuelight"], cwd=ROOT, capture_output=True,
                                 encoding="utf-8", timeout=30).stdout
        except OSError:
            self.skipTest("git is not available")
        if not out:
            self.skipTest("not a git checkout")
        self.assertTrue(out.startswith("100755"), out)

    def test_runs_the_cli_from_any_directory(self):
        import shutil
        import subprocess
        import tempfile
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("sh is not on PATH")
        out = subprocess.run([sh, self.PATH, "--help"], cwd=tempfile.gettempdir(),
                             capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--session", out.stdout)


class TestSessionLookup(unittest.TestCase):
    """The slash command passes no --session or --cwd; the CLI finds both itself."""

    def resolve(self, env_id, **flags):
        import argparse
        from unittest import mock
        from orchestra import __main__ as cli
        args = argparse.Namespace(session=flags.get("session", ""), cwd=flags.get("cwd", ""))
        seen = []
        env = {"CLAUDE_CODE_SESSION_ID": env_id} if env_id else {}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(cli, "_latest_session_in", lambda d: seen.append(d) or "newest"):
            if not env_id:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            return cli._resolve_session(args), seen

    def test_flag_then_environment(self):
        self.assertEqual(self.resolve("from-env", session="flag")[0], "flag")
        self.assertEqual(self.resolve("from-env"), ("from-env", []))

    def test_without_either_the_newest_session_of_the_start_directory(self):
        self.assertEqual(self.resolve("", cwd="E:/proj"), ("newest", ["E:/proj"]))
        self.assertEqual(self.resolve(""), ("newest", [os.getcwd()]))


class TestReadme(unittest.TestCase):
    def test_states_the_local_only_guarantee(self):
        text = read("README.md").lower()
        self.assertIn("127.0.0.1", text)
        self.assertIn("standard library", text)


if __name__ == "__main__":
    unittest.main()
