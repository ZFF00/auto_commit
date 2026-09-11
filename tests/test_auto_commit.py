import datetime as dt
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import auto_commit
import email_notifier


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def commit_result(
    *,
    included: list[str],
    excluded: list[str] | None = None,
    manual: list[str] | None = None,
    cautions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    excluded = excluded or []
    manual = manual or []
    recommendations = [
        {
            "path": path,
            "reason": "可重新生成",
            "gitignore_pattern": "__pycache__/" if "__pycache__" in path else f"/{path}",
            "tracked": False,
            "confidence": "high",
        }
        for path in excluded
    ]
    return {
        "clean": not (included or excluded or manual),
        "branch": "main",
        "summary": "测试分析结果",
        "ignore_recommendations": recommendations,
        "commit": {
            "title": "chore: back up repository",
            "body": ["Back up reviewed files."],
            "full_message": "chore: back up repository\n\nBack up reviewed files.",
            "included_paths": included,
            "excluded_paths": excluded,
            "manual_review_paths": manual,
        },
        "cautions": cautions or [],
    }


def fake_analyzer(result: dict[str, object]):
    def analyze(repo: Path, **kwargs: object):
        return repo.resolve(), result

    return analyze


class AutoCommitWorkflowTests(unittest.TestCase):
    def test_notification_uses_committed_message_and_count_without_file_list(self):
        repo, _ = self.make_repo()
        (repo / "private-path.py").write_text("print('ok')\n", encoding="utf-8")
        result = commit_result(included=["private-path.py"])
        result["commit"]["full_message"] = "feat: 邮件回执\n\n真实提交说明"
        outcome = auto_commit.execute_once(auto_commit.RunConfig(repo), analyzer=fake_analyzer(result))
        notice = auto_commit._outcome_notification(outcome, "origin")
        self.assertEqual(notice.commit_message, git(repo, "show", "-s", "--format=%B").stdout.strip())
        self.assertEqual(notice.file_count, 1)
        self.assertEqual(notice.commit_kind, "created")
        self.assertEqual([s.state for s in notice.steps], ["已完成"] * 4)
        self.assertNotIn("private-path.py", notice.summary)
        self.assertIn("private-path.py", outcome.report)

    def test_push_failure_notification_retains_commit_and_stage(self):
        repo, _ = self.make_repo(with_remote=False)
        git(repo, "remote", "add", "origin", str(self.root / "missing.git"))
        (repo / "app.py").write_text("print('ok')", encoding="utf-8")
        config = auto_commit.RunConfig(repo)
        with self.assertRaises(auto_commit.RunFailure) as caught:
            auto_commit.execute_once(config, analyzer=fake_analyzer(commit_result(included=["app.py"])))
        notice = auto_commit._failure_notification(config, caught.exception)
        self.assertFalse(notice.success)
        self.assertFalse(notice.pushed)
        self.assertEqual(notice.commit_hash, git(repo, "rev-parse", "HEAD").stdout.strip())
        self.assertIn("Back up reviewed files.", notice.commit_message)
        self.assertEqual(notice.file_count, 1)
        self.assertEqual([s.state for s in notice.steps], ["已完成", "已完成", "已完成", "失败"])

    def test_analysis_failure_does_not_claim_old_commit_as_new(self):
        repo, _ = self.make_repo()
        (repo / "old.txt").write_text("old", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "existing")
        (repo / "new.txt").write_text("new", encoding="utf-8")
        config = auto_commit.RunConfig(repo)
        with self.assertRaises(auto_commit.RunFailure) as caught:
            auto_commit.execute_once(config, analyzer=mock.Mock(side_effect=RuntimeError("analysis failed")))
        notice = auto_commit._failure_notification(config, caught.exception)
        self.assertEqual(notice.commit_hash, "")
        self.assertEqual([s.state for s in notice.steps], ["已完成", "失败", "未执行", "未执行"])

    def test_dry_run_exposes_proposed_message_and_manual_review(self):
        repo, _ = self.make_repo()
        (repo / "app.py").write_text("print('ok')", encoding="utf-8")
        (repo / "unknown.bin").write_bytes(b"?")
        outcome = auto_commit.execute_once(auto_commit.RunConfig(repo, dry_run=True),
            analyzer=fake_analyzer(commit_result(included=["app.py"], manual=["unknown.bin"])))
        notice = auto_commit._outcome_notification(outcome, "origin")
        self.assertEqual(notice.commit_kind, "planned")
        self.assertEqual(notice.commit_hash, "")
        self.assertEqual(notice.file_count, 1)
        self.assertIn("人工确认 1 项", notice.summary)
        self.assertEqual([s.state for s in notice.steps], ["已完成", "需确认", "未执行", "未执行"])
        self.assertFalse(auto_commit.has_head(repo))

    def test_clean_and_no_push_notifications_use_real_head(self):
        repo, _ = self.make_repo()
        (repo / "app.py").write_text("print('ok')", encoding="utf-8")
        first = auto_commit.execute_once(auto_commit.RunConfig(repo, push=False),
            analyzer=fake_analyzer(commit_result(included=["app.py"])))
        self.assertEqual(first.steps[3].state, "未执行")
        self.assertFalse(first.pushed)
        for dry_run in (False, True):
            clean = auto_commit.execute_once(auto_commit.RunConfig(repo, dry_run=dry_run))
            self.assertEqual(clean.commit_hash, first.commit_hash)
            self.assertEqual(clean.commit_message, first.commit_message)
            self.assertEqual(clean.commit_kind, "head")
            self.assertEqual(clean.file_count, 0)
            self.assertEqual(clean.steps[1].state, "已跳过")
            self.assertEqual(clean.steps[3].state, "未执行" if dry_run else "已完成")

    def test_empty_repository_has_no_fabricated_commit(self):
        repo, _ = self.make_repo()
        outcome = auto_commit.execute_once(auto_commit.RunConfig(repo))
        self.assertEqual(outcome.commit_hash, "")
        self.assertEqual(outcome.commit_message, "")
        self.assertEqual(outcome.steps[3].state, "已跳过")
        self.assertFalse(outcome.pushed)

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_repo(self, *, with_remote: bool = True) -> tuple[Path, Path | None]:
        repo = self.root / "work"
        repo.mkdir()
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.com")
        remote = None
        if with_remote:
            remote = self.root / "remote.git"
            remote.mkdir()
            git(remote, "init", "--bare")
            git(repo, "remote", "add", "origin", str(remote))
        return repo, remote

    def test_execute_once_updates_gitignore_commits_and_pushes(self):
        repo, remote = self.make_repo()
        (repo / "app.py").write_text("print('backup')\n", encoding="utf-8")
        cache = repo / "__pycache__" / "app.cpython-312.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"generated bytecode")
        result = commit_result(
            included=["app.py"],
            excluded=["__pycache__/app.cpython-312.pyc"],
        )

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo),
            analyzer=fake_analyzer(result),
        )

        self.assertEqual(outcome.status, "committed")
        self.assertTrue(outcome.pushed)
        self.assertEqual(outcome.added_ignore_patterns, ("__pycache__/",))
        self.assertIn("__pycache__/", (repo / ".gitignore").read_text(encoding="utf-8"))
        committed = set(
            git(repo, "show", "--pretty=format:", "--name-only", "HEAD").stdout.splitlines()
        )
        self.assertEqual(committed, {".gitignore", "app.py"})
        self.assertEqual(git(repo, "status", "--short").stdout, "")
        self.assertEqual(
            git(repo, "rev-parse", "HEAD").stdout.strip(),
            git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        )

    def test_staged_rename_exposes_both_paths_and_commits_completely(self):
        repo, _ = self.make_repo()
        (repo / "old_name.py").write_text("print('rename me')\n", encoding="utf-8")
        git(repo, "add", "old_name.py")
        git(repo, "commit", "-m", "init")
        git(repo, "mv", "old_name.py", "new_name.py")

        # Git's rename detection must not hide the deleted side from Codex.
        self.assertEqual(
            auto_commit.list_changed_paths(repo), {"old_name.py", "new_name.py"}
        )

        seen_required: list[tuple[str, ...]] = []

        def analyze(repo_path: Path, **kwargs: object):
            seen_required.append(tuple(kwargs["required_paths"]))  # type: ignore[arg-type]
            return repo_path.resolve(), commit_result(
                included=["new_name.py", "old_name.py"]
            )

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo), analyzer=analyze
        )

        self.assertEqual(seen_required, [("new_name.py", "old_name.py")])
        self.assertEqual(outcome.status, "committed")
        self.assertEqual(outcome.file_count, 2)
        committed = set(
            git(repo, "show", "--pretty=format:", "--no-renames", "--name-only", "HEAD")
            .stdout.splitlines()
        )
        self.assertEqual(committed, {"old_name.py", "new_name.py"})
        self.assertEqual(git(repo, "status", "--short").stdout, "")

    def test_staged_deletion_is_committed_without_git_rm_cached(self):
        repo, _ = self.make_repo()
        (repo / "obsolete.py").write_text("print('bye')\n", encoding="utf-8")
        (repo / "kept.py").write_text("print('hi')\n", encoding="utf-8")
        git(repo, "add", "obsolete.py", "kept.py")
        git(repo, "commit", "-m", "init")
        git(repo, "rm", "-q", "obsolete.py")
        (repo / "kept.py").write_text("print('changed')\n", encoding="utf-8")

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo),
            analyzer=fake_analyzer(commit_result(included=["kept.py", "obsolete.py"])),
        )

        self.assertEqual(outcome.status, "committed")
        self.assertEqual(outcome.file_count, 2)
        self.assertNotIn(
            "obsolete.py", git(repo, "ls-files").stdout.splitlines()
        )
        self.assertEqual(git(repo, "status", "--short").stdout, "")

    def test_clean_run_pushes_existing_unpushed_commit(self):
        repo, remote = self.make_repo()
        (repo / "note.txt").write_text("backup\n", encoding="utf-8")
        git(repo, "add", "note.txt")
        git(repo, "commit", "-m", "initial backup")
        analyze = mock.Mock(side_effect=AssertionError("clean run must not call Codex"))

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo),
            analyzer=analyze,
        )

        self.assertEqual(outcome.status, "clean")
        self.assertTrue(outcome.pushed)
        analyze.assert_not_called()
        self.assertEqual(
            git(repo, "rev-parse", "HEAD").stdout.strip(),
            git(remote, "rev-parse", "refs/heads/main").stdout.strip(),
        )

    def test_missing_remote_stops_before_codex_analysis(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "app.py").write_text("print('backup')\n", encoding="utf-8")
        analyze = mock.Mock(side_effect=AssertionError("Codex must not be called"))

        with self.assertRaisesRegex(auto_commit.AutoCommitError, "找不到 Git 远程仓库"):
            auto_commit.execute_once(
                auto_commit.RunConfig(repo=repo),
                analyzer=analyze,
            )

        analyze.assert_not_called()
        self.assertFalse(auto_commit.has_head(repo))

    def test_push_failure_keeps_local_commit(self):
        repo, _ = self.make_repo(with_remote=False)
        missing_remote = self.root / "missing-remote.git"
        git(repo, "remote", "add", "origin", str(missing_remote))
        (repo / "app.py").write_text("print('backup')\n", encoding="utf-8")
        result = commit_result(included=["app.py"])

        with self.assertRaisesRegex(auto_commit.AutoCommitError, "git push"):
            auto_commit.execute_once(
                auto_commit.RunConfig(repo=repo),
                analyzer=fake_analyzer(result),
            )

        self.assertTrue(auto_commit.has_head(repo))
        self.assertEqual(
            git(repo, "show", "--pretty=format:", "--name-only", "HEAD").stdout.strip(),
            "app.py",
        )

    def test_dry_run_does_not_modify_commit_or_gitignore(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "app.py").write_text("print('backup')\n", encoding="utf-8")
        result = commit_result(included=["app.py"])

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo, dry_run=True),
            analyzer=fake_analyzer(result),
        )

        self.assertEqual(outcome.status, "dry-run")
        self.assertFalse((repo / ".gitignore").exists())
        self.assertFalse(auto_commit.has_head(repo))
        self.assertEqual(git(repo, "status", "--short").stdout.strip(), "?? app.py")

    def test_manual_review_blocks_real_run_before_writing(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "archive.bin").write_bytes(b"unknown")
        result = commit_result(included=[], manual=["archive.bin"])

        with self.assertRaisesRegex(auto_commit.AutoCommitError, "需人工确认"):
            auto_commit.execute_once(
                auto_commit.RunConfig(repo=repo, push=False),
                analyzer=fake_analyzer(result),
            )

        self.assertFalse((repo / ".gitignore").exists())
        self.assertFalse(auto_commit.has_head(repo))

    def test_blocking_caution_stops_real_automatic_commit(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "settings.py").write_text("TOKEN = 'secret'\n", encoding="utf-8")
        result = commit_result(
            included=["settings.py"],
            cautions=[
                {
                    "path": "settings.py",
                    "reason": "疑似包含真实凭据",
                    "blocking": True,
                }
            ],
        )

        with self.assertRaisesRegex(auto_commit.AutoCommitError, "阻断风险"):
            auto_commit.execute_once(
                auto_commit.RunConfig(repo=repo, push=False),
                analyzer=fake_analyzer(result),
            )

    def test_dry_run_reports_manual_review_without_failing(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "archive.bin").write_bytes(b"unknown")
        result = commit_result(included=[], manual=["archive.bin"])

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo, dry_run=True),
            analyzer=fake_analyzer(result),
        )

        self.assertEqual(outcome.status, "dry-run")
        self.assertIn("需人工确认：\n- archive.bin", outcome.report)
        self.assertFalse(auto_commit.has_head(repo))

    def test_plan_must_cover_every_changed_path(self):
        result = commit_result(included=["app.py"])
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "未分类"):
            auto_commit.validate_plan(result, {"app.py", "README.md"})

    def test_execute_once_retries_incomplete_codex_classification(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "app.py").write_text("print('backup')\n", encoding="utf-8")
        incomplete = commit_result(included=[])
        incomplete["clean"] = False
        complete = commit_result(included=["app.py"])
        analyze = mock.Mock(
            side_effect=[
                (repo.resolve(), incomplete),
                (repo.resolve(), complete),
            ]
        )

        outcome = auto_commit.execute_once(
            auto_commit.RunConfig(repo=repo, dry_run=True),
            analyzer=analyze,
        )

        self.assertEqual(outcome.status, "dry-run")
        self.assertEqual(analyze.call_count, 2)
        first_kwargs = analyze.call_args_list[0].kwargs
        retry_kwargs = analyze.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["required_paths"], ("app.py",))
        self.assertIn("未分类：app.py", retry_kwargs["validation_feedback"])

    def test_plan_rejects_broad_ignore_pattern(self):
        result = commit_result(included=[], excluded=["cache.bin"])
        result["ignore_recommendations"][0]["gitignore_pattern"] = "*"
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "过宽"):
            auto_commit.validate_plan(result, {"cache.bin"})

    def test_plan_rejects_clean_result_when_changes_exist(self):
        result = commit_result(included=["app.py"])
        result["clean"] = True
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "clean=true"):
            auto_commit.validate_plan(result, {"app.py"})

    def test_plan_rejects_credential_in_commit_message(self):
        result = commit_result(included=["app.py"])
        result["commit"]["full_message"] = "chore: use TOKEN=do-not-commit"
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "疑似包含凭据"):
            auto_commit.validate_plan(result, {"app.py"})

    def test_ignore_rules_must_not_match_included_paths(self):
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "误伤"):
            auto_commit.validate_ignore_matches(
                ["*.py"],
                excluded_paths=["cache.py"],
                protected_paths=["app.py"],
            )

    def test_staged_excluded_file_blocks_commit(self):
        repo, _ = self.make_repo(with_remote=False)
        cache = repo / "__pycache__" / "app.cpython-312.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"generated bytecode")
        git(repo, "add", str(cache.relative_to(repo)))
        result = commit_result(
            included=[], excluded=["__pycache__/app.cpython-312.pyc"]
        )

        with self.assertRaisesRegex(auto_commit.AutoCommitError, "已经暂存"):
            auto_commit.execute_once(
                auto_commit.RunConfig(repo=repo, dry_run=True),
                analyzer=fake_analyzer(result),
            )

    def test_repository_lock_rejects_second_process(self):
        repo, _ = self.make_repo(with_remote=False)
        with auto_commit.RepositoryLock(repo):
            with self.assertRaisesRegex(auto_commit.AutoCommitError, "另一个自动提交任务"):
                with auto_commit.RepositoryLock(repo):
                    pass

    def test_stage_paths_treats_git_metacharacters_literally(self):
        repo, _ = self.make_repo(with_remote=False)
        (repo / "[ab].txt").write_text("literal\n", encoding="utf-8")
        (repo / "a.txt").write_text("must stay untracked\n", encoding="utf-8")

        auto_commit.stage_paths(repo, ["[ab].txt"])

        self.assertEqual(auto_commit.list_staged_paths(repo), {"[ab].txt"})
        self.assertIn("?? a.txt", git(repo, "status", "--short").stdout)


class SchedulingTests(unittest.TestCase):
    def test_load_email_config_reads_mail_environment(self):
        config = auto_commit.load_email_config(
            {
                "MAIL_SENDER": "sender@qq.com",
                "MAIL_AUTH_CODE": "authorization-code",
                "MAIL_RECIPIENTS": "first@example.com, second@example.com",
            }
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.host, "smtp.qq.com")
        self.assertEqual(config.port, 465)
        self.assertEqual(config.security, "ssl")
        self.assertEqual(config.policy, "always")
        self.assertEqual(config.sender_name, "Codex Git 推送")
        self.assertEqual(config.timeout, 30)
        self.assertEqual(
            config.recipients, ("first@example.com", "second@example.com")
        )

    def test_load_email_config_explicit_values_override_environment(self):
        config = auto_commit.load_email_config(
            {
                "MAIL_SENDER": "environment@qq.com",
                "MAIL_AUTH_CODE": "environment-code",
                "MAIL_RECIPIENTS": "environment@example.com",
            },
            sender="explicit@qq.com",
            auth_code="explicit-code",
            recipients="first@example.com;second@example.com",
        )
        self.assertEqual(config.username, "explicit@qq.com")
        self.assertEqual(config.password, "explicit-code")
        self.assertEqual(
            config.recipients, ("first@example.com", "second@example.com")
        )

    def test_load_email_config_falls_back_per_missing_explicit_value(self):
        config = auto_commit.load_email_config(
            {
                "MAIL_SENDER": "environment@qq.com",
                "MAIL_AUTH_CODE": "environment-code",
                "MAIL_RECIPIENTS": "environment@example.com",
            },
            recipients="explicit@example.com",
        )
        self.assertEqual(config.username, "environment@qq.com")
        self.assertEqual(config.password, "environment-code")
        self.assertEqual(config.recipients, ("explicit@example.com",))

    def test_load_email_config_returns_none_when_unconfigured(self):
        self.assertIsNone(auto_commit.load_email_config({}))

    def test_load_email_config_required_rejects_unconfigured(self):
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "MAIL_SENDER"):
            auto_commit.load_email_config({}, required=True)

    def test_load_email_config_rejects_partial_configuration(self):
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "MAIL_AUTH_CODE"):
            auto_commit.load_email_config(
                {
                    "MAIL_SENDER": "sender@qq.com",
                    "MAIL_RECIPIENTS": "backup@example.com",
                }
            )

    def test_load_email_config_unknown_provider_requires_host(self):
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "MAIL_SMTP_HOST"):
            auto_commit.load_email_config(
                {
                    "MAIL_SENDER": "sender@example.com",
                    "MAIL_AUTH_CODE": "authorization-code",
                    "MAIL_RECIPIENTS": "backup@example.com",
                }
            )

    def test_execute_and_notify_sends_success(self):
        outcome = auto_commit.RunOutcome(
            repository=Path.cwd(),
            branch="main",
            status="committed",
            report="提交成功",
            commit_hash="abc123",
            pushed=True,
        )
        mail_config = email_notifier.EmailConfig(
            recipients=("backup@example.com",),
            host="smtp.example.com",
            port=465,
            username="sender@example.com",
            password="authorization-code",
        )
        sender = mock.Mock(return_value=True)
        with mock.patch.object(auto_commit, "execute_once", return_value=outcome):
            result = auto_commit.execute_and_notify(
                auto_commit.RunConfig(repo=Path.cwd()),
                mail_config,
                email_sender=sender,
            )
        self.assertEqual(result, outcome)
        sent_notification = sender.call_args.args[1]
        self.assertTrue(sent_notification.success)
        self.assertEqual(sent_notification.commit_hash, "abc123")

    def test_remote_repository_preserves_configured_ssh_url(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory)
            git(repo, "init")
            git(
                repo,
                "remote",
                "add",
                "origin",
                "git@github.com:ZFF00/auto_commit.git",
            )

            self.assertEqual(
                auto_commit._remote_repository(repo, "origin"),
                "git@github.com:ZFF00/auto_commit.git",
            )

    def test_execute_and_notify_sends_failure_then_reraises(self):
        mail_config = email_notifier.EmailConfig(
            recipients=("backup@example.com",),
            host="smtp.example.com",
            port=465,
            username="sender@example.com",
            password="authorization-code",
        )
        sender = mock.Mock(return_value=True)
        with mock.patch.object(
            auto_commit,
            "execute_once",
            side_effect=auto_commit.AutoCommitError("push failed"),
        ):
            with self.assertRaisesRegex(auto_commit.AutoCommitError, "push failed"):
                auto_commit.execute_and_notify(
                    auto_commit.RunConfig(repo=Path.cwd()),
                    mail_config,
                    email_sender=sender,
                )
        sent_notification = sender.call_args.args[1]
        self.assertFalse(sent_notification.success)
        self.assertIn("push failed", sent_notification.summary)

    def test_parse_schedule_times_accepts_repeated_and_comma_values(self):
        result = auto_commit.parse_schedule_times(["18:30,09:00", "18:30"])
        self.assertEqual(result, (dt.time(9, 0), dt.time(18, 30)))

    def test_parse_schedule_times_rejects_invalid_time(self):
        with self.assertRaisesRegex(auto_commit.AutoCommitError, "无效的执行时间"):
            auto_commit.parse_schedule_times(["25:00"])

    def test_default_schedule_time_matches_help_text(self):
        self.assertEqual(
            auto_commit.parse_schedule_times([auto_commit.DEFAULT_SCHEDULE_TIME]),
            (dt.time(23, 30),),
        )
        help_text = auto_commit.build_parser().format_help()
        self.assertIn(f"默认 {auto_commit.DEFAULT_SCHEDULE_TIME}", help_text)

    def test_next_run_time_uses_today_or_tomorrow(self):
        schedule = (dt.time(9, 0), dt.time(18, 0))
        morning = auto_commit.next_run_time(dt.datetime(2026, 9, 9, 10), schedule)
        evening = auto_commit.next_run_time(dt.datetime(2026, 9, 9, 19), schedule)
        self.assertEqual(morning, dt.datetime(2026, 9, 9, 18))
        self.assertEqual(evening, dt.datetime(2026, 9, 10, 9))

    def test_main_once_prints_outcome(self):
        outcome = auto_commit.RunOutcome(
            repository=Path.cwd(),
            branch="main",
            status="clean",
            report="工作区干净",
        )
        stdout = io.StringIO()
        with mock.patch.object(auto_commit, "execute_and_notify", return_value=outcome):
            with redirect_stdout(stdout):
                return_code = auto_commit.main(["--once", "--dry-run"])
        self.assertEqual(return_code, 0)
        self.assertIn("工作区干净", stdout.getvalue())

    def test_main_passes_explicit_email_values_to_configuration(self):
        outcome = auto_commit.RunOutcome(
            repository=Path.cwd(),
            branch="main",
            status="clean",
            report="工作区干净",
        )
        with mock.patch.object(
            auto_commit, "load_email_config", return_value=None
        ) as load_email:
            with mock.patch.object(
                auto_commit, "execute_and_notify", return_value=outcome
            ):
                with redirect_stdout(io.StringIO()):
                    return_code = auto_commit.main(
                        [
                            "--once",
                            "--mail-sender",
                            "sender@qq.com",
                            "--mail-auth-code",
                            "authorization-code",
                            "--mail-recipients",
                            "receiver@example.com",
                        ]
                    )
        self.assertEqual(return_code, 0)
        load_email.assert_called_once_with(
            required=False,
            sender="sender@qq.com",
            auth_code="authorization-code",
            recipients="receiver@example.com",
        )

    def test_main_reports_invalid_timeout(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            return_code = auto_commit.main(["--once", "--timeout", "0"])
        self.assertEqual(return_code, 2)
        self.assertIn("必须大于 0", stderr.getvalue())

    def test_scheduler_run_now_executes_once_before_waiting(self):
        outcome = auto_commit.RunOutcome(
            repository=Path.cwd(),
            branch="main",
            status="clean",
            report="工作区干净",
        )
        sleeper = mock.Mock(side_effect=KeyboardInterrupt)
        stdout = io.StringIO()
        with mock.patch.object(auto_commit, "execute_once", return_value=outcome) as execute:
            with redirect_stdout(stdout):
                with self.assertRaises(KeyboardInterrupt):
                    auto_commit.run_scheduler(
                        auto_commit.RunConfig(repo=Path.cwd()),
                        (dt.time(23, 30),),
                        run_now=True,
                        clock=lambda: dt.datetime(2026, 9, 9, 12),
                        sleeper=sleeper,
                    )
        execute.assert_called_once()
        sleeper.assert_called_once()
        self.assertIn("工作区干净", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
