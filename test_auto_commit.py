import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auto_commit_v2 as auto_commit


@unittest.skip("兼容占位；v2 测试位于 test_auto_commit_v2.py")
class AutoCommitTests(unittest.TestCase):
    def test_resolve_explicit_codex_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex.cmd"
            executable.touch()
            self.assertEqual(
                auto_commit.resolve_codex_command(str(executable)),
                str(executable.resolve()),
            )

    @unittest.skipUnless(auto_commit.os.name == "nt", "Windows fallback")
    def test_resolve_codex_from_windows_npm_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            app_data = Path(directory)
            executable = app_data / "npm" / "codex.cmd"
            executable.parent.mkdir()
            executable.touch()
            with mock.patch.object(auto_commit.shutil, "which", return_value=None):
                with mock.patch.dict(
                    auto_commit.os.environ,
                    {"APPDATA": str(app_data), "LOCALAPPDATA": str(app_data / "local")},
                ):
                    self.assertEqual(
                        auto_commit.resolve_codex_command("codex"),
                        str(executable.resolve()),
                    )

    def test_sensitive_path_detection(self):
        self.assertTrue(auto_commit.is_sensitive_path(".env"))
        self.assertTrue(auto_commit.is_sensitive_path("config/.env.production"))
        self.assertTrue(auto_commit.is_sensitive_path("certs/server.key"))
        self.assertFalse(auto_commit.is_sensitive_path(".env.example"))
        self.assertFalse(auto_commit.is_sensitive_path("src/tokenizer.py"))

    def test_sensitive_assignments_are_redacted(self):
        source = '+API_KEY="do-not-send"\n+normal = 1\n-password: old'
        result = auto_commit.redact_sensitive_lines(source)
        self.assertNotIn("do-not-send", result)
        self.assertNotIn("old", result)
        self.assertIn("normal = 1", result)

    def test_parse_fenced_json(self):
        payload = {
            "clean": False,
            "branch": "main",
            "summary": "ok",
            "ignore_recommendations": [],
            "commit": {},
            "cautions": [],
        }
        result = auto_commit.parse_codex_result(
            f"```json\n{json.dumps(payload)}\n```"
        )
        self.assertEqual(result["summary"], "ok")

    def test_snapshot_does_not_read_sensitive_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "app.py").write_text("print('old')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            self._git(repo, "commit", "-m", "initial")

            (repo / "app.py").write_text("print('new')\n", encoding="utf-8")
            (repo / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
            (repo / ".env").write_text("SECRET=must-not-leak\n", encoding="utf-8")

            snapshot = auto_commit.collect_snapshot(
                repo,
                max_input_chars=100_000,
                include_untracked_content=True,
            )
            prompt = auto_commit.build_prompt(snapshot, "zh")

            self.assertIn("app.py", snapshot.changed_paths)
            self.assertIn("new.py", snapshot.untracked_paths)
            self.assertIn("VALUE = 1", prompt)
            self.assertIn(".env", prompt)
            self.assertNotIn("must-not-leak", prompt)

    @staticmethod
    def _git(repo: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
