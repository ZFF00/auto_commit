import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import codex_git_advisor as auto_commit


class CodexGitAdvisorTests(unittest.TestCase):
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

    @unittest.skipUnless(auto_commit.os.name == "nt", "Windows preference")
    def test_resolve_codex_prefers_native_executable(self):
        native = r"C:\Program Files\Codex\codex.exe"
        with mock.patch.object(
            auto_commit.shutil,
            "which",
            side_effect=lambda value: native if value == "codex.exe" else "codex.cmd",
        ):
            self.assertEqual(auto_commit.resolve_codex_command("codex"), native)

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

    def test_direct_prompt_does_not_embed_repository_snapshot(self):
        prompt = auto_commit.build_direct_prompt("zh")
        self.assertIn("当前工作目录就是需要审查的 Git 仓库", prompt)
        self.assertIn("禁止执行 git add", prompt)
        self.assertIn("敏感文件", prompt)
        self.assertNotIn("--- BEGIN UNTRUSTED REPOSITORY DATA ---", prompt)

    def test_direct_prompt_can_require_exact_changed_paths(self):
        prompt = auto_commit.build_direct_prompt(
            "zh",
            required_paths=("README.md", "src/app.py"),
            validation_feedback="未分类：src/app.py",
        )
        self.assertIn('["README.md", "src/app.py"]', prompt)
        self.assertIn("未分类：src/app.py", prompt)
        self.assertIn("不是仓库快照", prompt)

    def test_direct_prompt_uses_backup_first_classification(self):
        prompt = auto_commit.build_direct_prompt("zh")
        for path in ("README.md", "auto_commit.py", "test_auto_commit.py"):
            self.assertIn(path, prompt)
        for path in ("__pycache__/", "nohup.out", "无扩展名 auto_commit"):
            self.assertIn(path, prompt)
        self.assertIn("有效信息默认包含", prompt)
        self.assertIn("旧版本", prompt)
        self.assertIn("测试失败", prompt)
        self.assertIn("只能写入 cautions", prompt)
        self.assertIn("blocking=true", prompt)
        self.assertIn("manual_review_paths", prompt)
        self.assertIn("不要仅凭体积直接归为 ignore", prompt)

    def test_invoke_codex_uses_repository_as_read_only_working_directory(self):
        payload = {
            "clean": True,
            "branch": "main",
            "summary": "工作区干净",
            "ignore_recommendations": [],
            "commit": {
                "title": "",
                "body": [],
                "full_message": "",
                "included_paths": [],
                "excluded_paths": [],
                "manual_review_paths": [],
            },
            "cautions": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def fake_run(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(command[command.index("--cd") + 1], str(repo))
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertNotIn("--skip-git-repo-check", command)
                self.assertIn("--json", command)
                self.assertIn("plugins", command)
                self.assertIn("remote_plugin", command)
                self.assertIn("browser_use", command)
                self.assertIn("computer_use", command)
                self.assertIn("multi_agent", command)
                self.assertIn("hooks", command)
                self.assertEqual(kwargs["input_text"], auto_commit.build_direct_prompt("zh"))
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(auto_commit, "run_process", side_effect=fake_run):
                result = auto_commit.invoke_codex(
                    auto_commit.build_direct_prompt("zh"),
                    repo=repo,
                    codex_command="codex",
                    model=None,
                    timeout=30,
                )

        self.assertTrue(result["clean"])

    def test_invoke_codex_live_enables_json_events(self):
        payload = {
            "clean": True,
            "branch": "main",
            "summary": "工作区干净",
            "ignore_recommendations": [],
            "commit": {
                "title": "",
                "body": [],
                "full_message": "",
                "included_paths": [],
                "excluded_paths": [],
                "manual_review_paths": [],
            },
            "cautions": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def fake_run_live(command, **kwargs):
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIn("--json", command)
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                auto_commit, "run_process_live", side_effect=fake_run_live
            ):
                result = auto_commit.invoke_codex(
                    auto_commit.build_direct_prompt("zh"),
                    repo=repo,
                    codex_command="codex",
                    model=None,
                    timeout=30,
                    live=True,
                )

        self.assertTrue(result["clean"])

    def test_invoke_codex_timeout_reports_stage_progress_and_no_proven_cause(self):
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "git status --short",
                            "exit_code": 0,
                        },
                    }
                ),
            ]
        )
        timeout_error = auto_commit.ProcessTimeoutError(
            ["codex", "exec"], 300, events, ""
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                auto_commit, "run_process", side_effect=timeout_error
            ):
                with self.assertRaises(auto_commit.AdvisorError) as raised:
                    auto_commit.invoke_codex(
                        auto_commit.build_direct_prompt("zh"),
                        repo=Path(directory),
                        codex_command="codex",
                        model=None,
                        timeout=300,
                    )

        message = str(raised.exception)
        self.assertIn("Codex 仓库分析超时（300 秒）", message)
        self.assertIn("尚未执行：修改 .gitignore", message)
        self.assertIn("git status --short", message)
        self.assertIn("无法确定是网络/API 故障还是任务分析耗时", message)

    def test_invoke_codex_timeout_includes_explicit_codex_error(self):
        event = json.dumps(
            {"type": "error", "message": "connection reset by peer"}
        )
        timeout_error = auto_commit.ProcessTimeoutError(
            ["codex", "exec"], 300, event, "ERROR retry exhausted"
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                auto_commit, "run_process", side_effect=timeout_error
            ):
                with self.assertRaises(auto_commit.AdvisorError) as raised:
                    auto_commit.invoke_codex(
                        auto_commit.build_direct_prompt("zh"),
                        repo=Path(directory),
                        codex_command="codex",
                        model=None,
                        timeout=300,
                    )

        message = str(raised.exception)
        self.assertIn("connection reset by peer", message)
        self.assertIn("retry exhausted", message)
        self.assertNotIn("无法确定是网络/API", message)

    def test_invoke_codex_recovers_final_result_emitted_before_timeout(self):
        payload = {
            "clean": False,
            "branch": "main",
            "summary": "已完成仓库分析",
            "ignore_recommendations": [],
            "commit": {
                "title": "更新自动提交工具",
                "body": [],
                "full_message": "更新自动提交工具",
                "included_paths": ["auto_commit.py"],
                "excluded_paths": [],
                "manual_review_paths": [],
            },
            "cautions": [],
        }
        event = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(payload, ensure_ascii=False),
                },
            },
            ensure_ascii=False,
        )
        timeout_error = auto_commit.ProcessTimeoutError(
            ["codex", "exec"], 300, event, ""
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                auto_commit, "run_process", side_effect=timeout_error
            ):
                result = auto_commit.invoke_codex(
                    auto_commit.build_direct_prompt("zh"),
                    repo=Path(directory),
                    codex_command="codex",
                    model=None,
                    timeout=300,
                )

        self.assertEqual(result, payload)

    def test_timeout_warning_is_diagnostic_not_proven_error(self):
        events = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps({"type": "turn.started"}),
            ]
        )
        message = auto_commit._codex_timeout_details(
            events,
            "WARN shell snapshot: Failed to create shell snapshot for PowerShell",
            1,
        )
        self.assertIn("未捕获到明确错误", message)
        self.assertIn("可能仍在等待模型响应", message)
        self.assertIn("原始诊断输出（可能仅包含警告）", message)

    def test_format_codex_command_event(self):
        line = json.dumps(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "git status --short"},
            }
        )
        self.assertEqual(
            auto_commit.format_codex_event(line),
            "[Codex] 执行：git status --short",
        )

    def test_format_codex_agent_message_shows_progress_text(self):
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "我先检查 Git 状态。"},
            },
            ensure_ascii=False,
        )
        self.assertEqual(
            auto_commit.format_codex_event(line),
            "[Codex] 我先检查 Git 状态。",
        )

    def test_live_command_output_redacts_sensitive_assignment(self):
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "rg pwd settings.py",
                    "exit_code": 0,
                    "aggregated_output": "settings.py:12: pwd = 'do-not-print'",
                },
            }
        )
        rendered = auto_commit.format_codex_event(line)
        self.assertNotIn("do-not-print", rendered)
        self.assertIn("pwd = [REDACTED]", rendered)

    def test_render_report_redacts_sensitive_assignment(self):
        result = {
            "clean": False,
            "branch": "main",
            "summary": "发现 TOKEN=do-not-print",
            "ignore_recommendations": [],
            "commit": {
                "title": "backup",
                "body": [],
                "full_message": "backup",
                "included_paths": ["app.py"],
                "excluded_paths": [],
                "manual_review_paths": [],
            },
            "cautions": [],
        }
        rendered = auto_commit.render_report(Path("repo"), result)
        self.assertNotIn("do-not-print", rendered)
        self.assertIn("TOKEN=[REDACTED]", rendered)

    def test_run_process_live_echoes_events(self):
        event = json.dumps({"type": "turn.started"})
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = auto_commit.run_process_live(
                [sys.executable, "-c", f"print({event!r})"],
                input_text="",
                timeout=5,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("开始分析仓库", stderr.getvalue())

    def test_main_json_output_uses_codex_result(self):
        result = {
            "clean": True,
            "branch": "main",
            "summary": "工作区干净",
            "ignore_recommendations": [],
            "commit": {
                "title": "",
                "body": [],
                "full_message": "",
                "included_paths": [],
                "excluded_paths": [],
                "manual_review_paths": [],
            },
            "cautions": [],
        }
        repo = Path.cwd()
        stdout = io.StringIO()
        with mock.patch.object(auto_commit, "find_repository", return_value=repo):
            with mock.patch.object(auto_commit, "resolve_codex_command", return_value="codex"):
                with mock.patch.object(auto_commit, "invoke_codex", return_value=result):
                    with redirect_stdout(stdout):
                        return_code = auto_commit.main(["--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(return_code, 0)
        self.assertEqual(payload["branch"], "main")
        self.assertTrue(payload["clean"])

    def test_render_report_shows_manual_review_paths(self):
        result = {
            "clean": False,
            "branch": "main",
            "summary": "发现一个用途不明的大文件",
            "ignore_recommendations": [],
            "commit": {
                "title": "chore: back up project files",
                "body": [],
                "full_message": "chore: back up project files",
                "included_paths": ["README.md"],
                "excluded_paths": [],
                "manual_review_paths": ["data/archive.bin"],
            },
            "cautions": [
                {
                    "path": "data/archive.bin",
                    "reason": "用途和来源无法确认",
                    "blocking": False,
                }
            ],
        }
        report = auto_commit.render_report(Path("repo"), result)
        self.assertIn("建议包含：\n- README.md", report)
        self.assertIn("需人工确认：\n- data/archive.bin", report)

    def test_decode_windows_chinese_output(self):
        expected = "建议忽略调试日志"
        self.assertEqual(auto_commit.decode_bytes(expected.encode("gb18030")), expected)

if __name__ == "__main__":
    unittest.main()
