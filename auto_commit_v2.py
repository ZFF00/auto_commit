#!/usr/bin/env python3
"""Automatically review, commit, and push Git repository backups."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

import codex_git_advisor as advisor
import email_notifier


VERSION = "2.6.2"
LOGGER = logging.getLogger("auto_commit_v2")

__all__ = [
    "AutoCommitError",
    "RunConfig",
    "RunOutcome",
    "execute_once",
    "execute_and_notify",
    "load_email_config",
    "next_run_time",
    "parse_schedule_times",
    "run_scheduler",
]


class AutoCommitError(RuntimeError):
    """An expected automation error that should be shown without a traceback."""


@dataclass(frozen=True)
class RunConfig:
    repo: Path
    remote: str = "origin"
    branch: str | None = None
    push: bool = True
    dry_run: bool = False
    codex_command: str = "codex"
    model: str | None = None
    timeout: int = 300
    live: bool = False
    language: str = "zh"


@dataclass(frozen=True)
class CommitPlan:
    included_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    manual_review_paths: tuple[str, ...]
    ignore_patterns: tuple[str, ...]
    commit_message: str
    cautions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RunOutcome:
    repository: Path
    branch: str
    status: str
    report: str
    commit_hash: str = ""
    pushed: bool = False
    added_ignore_patterns: tuple[str, ...] = ()


Analyzer = Callable[..., tuple[Path, dict[str, Any]]]
EmailSender = Callable[
    [email_notifier.EmailConfig, email_notifier.TaskNotification], bool
]


class RepositoryLock:
    """Prevent two automation processes from committing the same repository."""

    def __init__(self, repo: Path) -> None:
        git_dir_result = run_git(repo, "rev-parse", "--git-dir")
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = repo / git_dir
        self.path = git_dir.resolve() / "auto_commit_v2.lock"
        self._acquired = False

    def __enter__(self) -> RepositoryLock:
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise AutoCommitError(
                f"另一个自动提交任务可能正在运行；锁文件仍存在：{self.path}"
            ) from exc
        with os.fdopen(descriptor, "w", encoding="ascii") as lock_file:
            lock_file.write(f"pid={os.getpid()}\n")
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._acquired = False


def run_git(
    repo: Path,
    *arguments: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise AutoCommitError("找不到 Git 命令") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "没有错误详情"
        raise AutoCommitError(f"Git 命令失败：git {' '.join(arguments)}\n{detail}")
    return result


def _nul_paths(output: str) -> set[str]:
    return {path.replace("\\", "/") for path in output.split("\0") if path}


def list_changed_paths(repo: Path) -> set[str]:
    """Return staged, unstaged, deleted, and untracked non-ignored paths."""
    changed: set[str] = set()
    for arguments in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        changed.update(_nul_paths(run_git(repo, *arguments).stdout))
    return changed


def list_staged_paths(repo: Path) -> set[str]:
    return _nul_paths(run_git(repo, "diff", "--cached", "--name-only", "-z").stdout)


def stage_paths(repo: Path, paths: Sequence[str]) -> None:
    if paths:
        run_git(repo, "--literal-pathspecs", "add", "-A", "--", *paths)


def current_branch(repo: Path, requested: str | None) -> str:
    branch = requested
    if branch is None:
        result = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if result.returncode != 0:
            raise AutoCommitError("当前处于 detached HEAD；请使用 --branch 指定推送分支")
        branch = result.stdout.strip()
    validation = run_git(repo, "check-ref-format", "--branch", branch, check=False)
    if validation.returncode != 0:
        raise AutoCommitError(f"无效的分支名称：{branch}")
    return branch


def ensure_preconditions(repo: Path, *, remote: str, push: bool) -> None:
    run_git(repo, "var", "GIT_AUTHOR_IDENT")
    if push:
        if not remote or remote.startswith("-") or any(char.isspace() for char in remote):
            raise AutoCommitError(f"无效的 Git 远程仓库名：{remote!r}")
        remotes = set(run_git(repo, "remote").stdout.splitlines())
        if remote not in remotes:
            raise AutoCommitError(f"找不到 Git 远程仓库：{remote}")


def normalize_repo_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0].endswith(":")
        or "\0" in normalized
        or "\n" in normalized
        or "\r" in normalized
    ):
        raise AutoCommitError(f"Codex 返回了不安全的仓库路径：{value!r}")
    return path.as_posix()


def validate_ignore_pattern(value: str) -> str:
    pattern = value.strip().replace("\\", "/")
    broad_patterns = {"*", "**", "/*", "/**", "**/*", "*.*", "/**/*"}
    if (
        not pattern
        or pattern in broad_patterns
        or pattern.startswith("!")
        or "\0" in pattern
        or "\n" in pattern
        or "\r" in pattern
        or ".." in PurePosixPath(pattern.lstrip("/")).parts
    ):
        raise AutoCommitError(f"Codex 返回了不安全或过宽的 .gitignore 规则：{value!r}")
    return pattern


def _path_tuple(values: Any, field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise AutoCommitError(f"Codex 结果中的 {field} 不是字符串列表")
    normalized = tuple(normalize_repo_path(item) for item in values)
    if len(normalized) != len(set(normalized)):
        raise AutoCommitError(f"Codex 结果中的 {field} 含有重复路径")
    return normalized


def validate_plan(result: dict[str, Any], changed_paths: set[str]) -> CommitPlan:
    if result.get("clean") is not False:
        raise AutoCommitError("工作区存在改动，但 Codex 错误地返回 clean=true")
    commit = result.get("commit")
    if not isinstance(commit, dict):
        raise AutoCommitError("Codex 结果缺少 commit 对象")

    included = _path_tuple(commit.get("included_paths"), "included_paths")
    excluded = _path_tuple(commit.get("excluded_paths"), "excluded_paths")
    manual = _path_tuple(commit.get("manual_review_paths"), "manual_review_paths")
    groups = [set(included), set(excluded), set(manual)]
    if groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2]:
        raise AutoCommitError("Codex 返回的包含、排除和人工确认路径发生重叠")

    classified = groups[0] | groups[1] | groups[2]
    if classified != changed_paths:
        missing = sorted(changed_paths - classified)
        extra = sorted(classified - changed_paths)
        details = []
        if missing:
            details.append(f"未分类：{', '.join(missing)}")
        if extra:
            details.append(f"不存在的分类：{', '.join(extra)}")
        raise AutoCommitError("Codex 没有准确覆盖当前改动；" + "；".join(details))

    recommendations = result.get("ignore_recommendations")
    if not isinstance(recommendations, list):
        raise AutoCommitError("Codex 结果中的 ignore_recommendations 不是列表")
    recommendation_paths: set[str] = set()
    patterns: list[str] = []
    for item in recommendations:
        if not isinstance(item, dict):
            raise AutoCommitError("Codex 返回了无效的忽略建议")
        path = normalize_repo_path(str(item.get("path", "")))
        if path not in groups[1]:
            raise AutoCommitError(f"忽略建议路径未被归入 excluded_paths：{path}")
        if item.get("confidence") != "high":
            raise AutoCommitError(f"自动忽略只接受 high 置信度：{path}")
        if item.get("tracked") is not False:
            raise AutoCommitError(f"不能自动忽略已跟踪或状态不明的路径：{path}")
        recommendation_paths.add(path)
        pattern = validate_ignore_pattern(str(item.get("gitignore_pattern", "")))
        if pattern not in patterns:
            patterns.append(pattern)
    if recommendation_paths != groups[1]:
        raise AutoCommitError("excluded_paths 与 ignore_recommendations 不一致")

    cautions_value = result.get("cautions")
    if not isinstance(cautions_value, list):
        raise AutoCommitError("Codex 结果中的 cautions 不是对象列表")
    cautions_list: list[dict[str, Any]] = []
    for item in cautions_value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("reason"), str)
            or not isinstance(item.get("blocking"), bool)
        ):
            raise AutoCommitError("Codex 返回了格式无效的风险提示")
        cautions_list.append(item)
    cautions = tuple(cautions_list)

    message = commit.get("full_message")
    if not isinstance(message, str):
        raise AutoCommitError("Codex 结果中的 full_message 不是字符串")
    message = message.strip()
    if (included or patterns) and not message:
        raise AutoCommitError("Codex 没有生成 commit message")
    if "\0" in message or len(message) > 20_000:
        raise AutoCommitError("Codex 返回的 commit message 无效或过长")
    if advisor.redact_sensitive_output(message) != message:
        raise AutoCommitError("Codex 返回的 commit message 疑似包含凭据，已停止")

    return CommitPlan(included, excluded, manual, tuple(patterns), message, cautions)


def validate_ignore_matches(
    patterns: Sequence[str],
    *,
    excluded_paths: Sequence[str],
    protected_paths: Sequence[str],
) -> None:
    """Use Git itself to ensure suggested rules cover only the intended changed paths."""
    if not patterns:
        return
    with tempfile.TemporaryDirectory(prefix="auto_commit_ignore_check_") as directory:
        temp_repo = Path(directory)
        run_git(temp_repo, "init", "--quiet")
        (temp_repo / ".gitignore").write_text(
            "\n".join(patterns) + "\n", encoding="utf-8", newline="\n"
        )
        candidates = [*excluded_paths, *protected_paths]
        input_text = "\0".join(candidates) + "\0"
        result = run_git(
            temp_repo,
            "check-ignore",
            "--no-index",
            "-z",
            "--stdin",
            input_text=input_text,
            check=False,
        )
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or "无法验证规则"
            raise AutoCommitError(f".gitignore 规则验证失败：{detail}")
        matched = _nul_paths(result.stdout)
    uncovered = set(excluded_paths) - matched
    affected = set(protected_paths) & matched
    if uncovered:
        raise AutoCommitError(
            ".gitignore 规则没有覆盖建议排除路径：" + ", ".join(sorted(uncovered))
        )
    if affected:
        raise AutoCommitError(
            ".gitignore 规则会误伤应提交或人工确认的路径："
            + ", ".join(sorted(affected))
        )


def update_gitignore(repo: Path, patterns: Sequence[str]) -> tuple[str, ...]:
    if not patterns:
        return ()
    path = repo / ".gitignore"
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    additions = tuple(pattern for pattern in patterns if pattern not in existing_lines)
    if not additions:
        return ()
    prefix = existing
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += "\n"
    if prefix and prefix.strip():
        prefix += "\n"
    content = prefix + "# Added by auto_commit_v2.py\n" + "\n".join(additions) + "\n"
    path.write_text(content, encoding="utf-8", newline="\n")
    return additions


def has_head(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def push_head(repo: Path, remote: str, branch: str) -> bool:
    if not has_head(repo):
        return False
    run_git(repo, "push", "--porcelain", "--", remote, f"HEAD:refs/heads/{branch}")
    return True


def execute_once(config: RunConfig, *, analyzer: Analyzer | None = None) -> RunOutcome:
    """Analyze and perform one complete local commit/push cycle."""
    repo = advisor.find_repository(config.repo)
    branch = current_branch(repo, config.branch)
    if not config.dry_run:
        ensure_preconditions(repo, remote=config.remote, push=config.push)
    analyze = analyzer or advisor.analyze_repository

    with RepositoryLock(repo):
        changed_paths = list_changed_paths(repo)
        if not changed_paths:
            pushed = False
            if config.push and not config.dry_run:
                pushed = push_head(repo, config.remote, branch)
            status = "dry-run" if config.dry_run else "clean"
            report = f"仓库：{repo}\n分支：{branch}\n摘要：工作区没有待提交改动。"
            return RunOutcome(repo, branch, status, report, pushed=pushed)

        analyzed_repo, result = analyze(
            repo,
            codex_command=config.codex_command,
            model=config.model,
            timeout=config.timeout,
            live=config.live,
            language=config.language,
        )
        if analyzed_repo.resolve() != repo.resolve():
            raise AutoCommitError("Codex 分析结果来自另一个仓库")
        report = advisor.render_report(repo, result)
        plan = validate_plan(result, changed_paths)

        validate_ignore_matches(
            plan.ignore_patterns,
            excluded_paths=plan.excluded_paths,
            protected_paths=(*plan.included_paths, *plan.manual_review_paths),
        )
        staged_before = list_staged_paths(repo)
        unsafe_staged = staged_before & (set(plan.excluded_paths) | set(plan.manual_review_paths))
        if unsafe_staged:
            raise AutoCommitError(
                "建议排除或人工确认的文件已经暂存，已停止："
                + ", ".join(sorted(unsafe_staged))
            )
        if config.dry_run:
            return RunOutcome(repo, branch, "dry-run", report)

        if plan.manual_review_paths:
            raise AutoCommitError(
                "存在需人工确认的文件，已停止自动提交："
                + ", ".join(plan.manual_review_paths)
            )
        blocking = [
            advisor.redact_sensitive_output(str(item.get("reason", "未知风险")))
            for item in plan.cautions
            if item.get("blocking") is True
        ]
        if blocking:
            raise AutoCommitError("存在阻断风险，已停止自动提交：" + "；".join(blocking))

        if list_changed_paths(repo) != changed_paths:
            raise AutoCommitError("Codex 分析期间仓库内容发生变化，请重新执行")

        added_patterns = update_gitignore(repo, plan.ignore_patterns)
        paths_to_stage = list(plan.included_paths)
        if added_patterns and ".gitignore" not in paths_to_stage:
            paths_to_stage.append(".gitignore")
        stage_paths(repo, paths_to_stage)

        staged_after = list_staged_paths(repo)
        allowed_staged = set(paths_to_stage)
        unexpected_staged = staged_after - allowed_staged
        if unexpected_staged:
            raise AutoCommitError(
                "暂存区出现未经 Codex 批准的文件，已停止提交："
                + ", ".join(sorted(unexpected_staged))
            )
        if not staged_after:
            pushed = push_head(repo, config.remote, branch) if config.push else False
            return RunOutcome(
                repo,
                branch,
                "clean",
                report,
                pushed=pushed,
                added_ignore_patterns=added_patterns,
            )

        run_git(repo, "commit", "--file=-", input_text=plan.commit_message + "\n")
        commit_hash = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        pushed = push_head(repo, config.remote, branch) if config.push else False
        return RunOutcome(
            repo,
            branch,
            "committed",
            report,
            commit_hash=commit_hash,
            pushed=pushed,
            added_ignore_patterns=added_patterns,
        )


def parse_schedule_times(values: Sequence[str]) -> tuple[dt.time, ...]:
    parsed: set[dt.time] = set()
    for value in values:
        for item in value.split(","):
            candidate = item.strip()
            try:
                parsed.add(dt.datetime.strptime(candidate, "%H:%M").time())
            except ValueError as exc:
                raise AutoCommitError(f"无效的执行时间：{candidate}，应为 HH:MM") from exc
    if not parsed:
        raise AutoCommitError("至少需要一个执行时间")
    return tuple(sorted(parsed))


def next_run_time(now: dt.datetime, schedule: Sequence[dt.time]) -> dt.datetime:
    candidates = [dt.datetime.combine(now.date(), item) for item in schedule]
    future = [candidate for candidate in candidates if candidate > now]
    if future:
        return min(future)
    return min(candidates) + dt.timedelta(days=1)


def format_outcome(outcome: RunOutcome) -> str:
    lines = [outcome.report]
    if outcome.added_ignore_patterns:
        lines.extend(
            [
                "",
                "已写入 .gitignore：",
                *[f"- {pattern}" for pattern in outcome.added_ignore_patterns],
            ]
        )
    if outcome.commit_hash:
        lines.extend(["", f"本地提交：{outcome.commit_hash}"])
    if outcome.pushed:
        lines.append(f"已推送到：{outcome.branch}")
    elif outcome.status == "dry-run":
        lines.extend(["", "演练模式：未修改文件、未提交、未推送。"])
    elif outcome.status == "clean":
        lines.extend(["", "没有新的可提交改动。"])
    return "\n".join(lines).rstrip()


def _default_smtp_settings(sender: str) -> tuple[str, int, str] | None:
    domain = sender.rpartition("@")[2].lower()
    known = {
        "qq.com": ("smtp.qq.com", 465, "ssl"),
        "163.com": ("smtp.163.com", 465, "ssl"),
        "126.com": ("smtp.126.com", 465, "ssl"),
        "gmail.com": ("smtp.gmail.com", 465, "ssl"),
        "outlook.com": ("smtp.office365.com", 587, "starttls"),
        "hotmail.com": ("smtp.office365.com", 587, "starttls"),
        "live.com": ("smtp.office365.com", 587, "starttls"),
    }
    return known.get(domain)


def load_email_config(
    environment: dict[str, str] | None = None,
    *,
    required: bool = False,
    sender: str | None = None,
    auth_code: str | None = None,
    recipients: str | None = None,
) -> email_notifier.EmailConfig | None:
    """Load email settings, preferring explicit values over the environment."""
    values = os.environ if environment is None else environment
    resolved_sender = (
        sender if sender is not None else values.get("MAIL_SENDER", "")
    ).strip()
    resolved_auth_code = (
        auth_code if auth_code is not None else values.get("MAIL_AUTH_CODE", "")
    ).strip()
    recipient_value = (
        recipients if recipients is not None else values.get("MAIL_RECIPIENTS", "")
    ).strip()
    core_present = any((resolved_sender, resolved_auth_code, recipient_value))
    if not core_present and not required:
        return None

    missing = [
        name
        for name, value in (
            ("MAIL_SENDER/--mail-sender", resolved_sender),
            ("MAIL_AUTH_CODE/--mail-auth-code", resolved_auth_code),
            ("MAIL_RECIPIENTS/--mail-recipients", recipient_value),
        )
        if not value
    ]
    if missing:
        raise AutoCommitError("邮件配置不完整，缺少环境变量：" + ", ".join(missing))

    recipients = tuple(
        dict.fromkeys(
            item.strip()
            for item in recipient_value.replace(";", ",").split(",")
            if item.strip()
        )
    )
    inferred = _default_smtp_settings(resolved_sender)
    host = values.get("MAIL_SMTP_HOST", "").strip()
    if not host:
        if inferred is None:
            raise AutoCommitError(
                "无法根据发件邮箱推断 SMTP 服务器，请设置 MAIL_SMTP_HOST"
            )
        host = inferred[0]
    security = values.get(
        "MAIL_SMTP_SECURITY", inferred[2] if inferred else "ssl"
    ).strip().lower()
    default_port = inferred[1] if inferred and security == inferred[2] else (
        587 if security == "starttls" else 465
    )
    try:
        port = int(values.get("MAIL_SMTP_PORT", str(default_port)))
    except ValueError as exc:
        raise AutoCommitError("MAIL_SMTP_PORT 必须是整数") from exc

    config = email_notifier.EmailConfig(
        recipients=recipients,
        host=host,
        port=port,
        username=resolved_sender,
        password=resolved_auth_code,
        security=security,  # type: ignore[arg-type]
        sender=resolved_sender,
        sender_name="Codex Git 推送",
        policy="always",
        timeout=30,
    )
    try:
        config.validate()
    except email_notifier.EmailNotificationError as exc:
        raise AutoCommitError(str(exc)) from exc
    return config


def _remote_repository(repo: Path, remote: str) -> str:
    """Return the raw configured URL without applying Git URL rewrite rules."""
    try:
        return run_git(repo, "config", "--get", f"remote.{remote}.url").stdout.strip()
    except AutoCommitError:
        return ""


def _outcome_notification(
    outcome: RunOutcome, remote: str
) -> email_notifier.TaskNotification:
    return email_notifier.TaskNotification(
        success=True,
        status=outcome.status,
        repository=outcome.repository,
        branch=outcome.branch,
        occurred_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        summary=advisor.redact_sensitive_output(format_outcome(outcome)),
        remote_repository=_remote_repository(outcome.repository, remote),
        commit_hash=outcome.commit_hash,
        pushed=outcome.pushed,
        ignore_patterns=outcome.added_ignore_patterns,
    )


def _failure_notification(
    config: RunConfig, error: Exception
) -> email_notifier.TaskNotification:
    return email_notifier.TaskNotification(
        success=False,
        status="failed",
        repository=config.repo.expanduser().resolve(),
        branch=config.branch or "未知",
        occurred_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        summary=advisor.redact_sensitive_output(str(error)),
        remote_repository=_remote_repository(
            config.repo.expanduser().resolve(), config.remote
        ),
    )


def execute_and_notify(
    config: RunConfig,
    email_config: email_notifier.EmailConfig | None,
    *,
    analyzer: Analyzer | None = None,
    email_sender: EmailSender = email_notifier.send_notification,
) -> RunOutcome:
    """Execute one task and send a success or failure notification."""
    try:
        outcome = execute_once(config, analyzer=analyzer)
    except Exception as task_error:
        if email_config is not None:
            try:
                email_sender(email_config, _failure_notification(config, task_error))
            except email_notifier.EmailNotificationError as mail_error:
                raise AutoCommitError(
                    f"{task_error}\n另外，失败通知也未能发送：{mail_error}"
                ) from task_error
        raise

    if email_config is not None:
        try:
            email_sender(email_config, _outcome_notification(outcome, config.remote))
        except email_notifier.EmailNotificationError as exc:
            raise AutoCommitError(
                f"Git 任务已经完成，但邮件通知失败：{exc}"
            ) from exc
    return outcome


def run_scheduler(
    config: RunConfig,
    schedule: Sequence[dt.time],
    *,
    email_config: email_notifier.EmailConfig | None = None,
    run_now: bool = False,
    clock: Callable[[], dt.datetime] = dt.datetime.now,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    def run_task() -> None:
        started = clock().astimezone().isoformat(timespec="seconds")
        LOGGER.info("开始自动提交任务：%s", started)
        try:
            outcome = execute_and_notify(config, email_config)
        except Exception as exc:
            LOGGER.error("自动提交任务失败：%s", exc)
        else:
            print(format_outcome(outcome), flush=True)
            LOGGER.info("自动提交任务完成：%s", outcome.status)

    if run_now:
        run_task()
    while True:
        now = clock()
        next_run = next_run_time(now, schedule)
        delay = max(0.0, (next_run - now).total_seconds())
        LOGGER.info("下次执行时间：%s", next_run.astimezone().isoformat(timespec="minutes"))
        sleeper(delay)
        run_task()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="调用本机 Codex 审查改动，自动更新 .gitignore、提交并推送。"
    )
    parser.add_argument("-r", "--repo", type=Path, default=Path.cwd(), help="Git 仓库路径")
    parser.add_argument("--once", action="store_true", help="立即执行一次后退出")
    parser.add_argument(
        "-t",
        "--time",
        action="append",
        dest="times",
        help="每日执行时间 HH:MM；可重复或用逗号分隔（默认 23:30）",
    )
    parser.add_argument("--run-now", action="store_true", help="启动定时器时先立即执行一次")
    parser.add_argument("--remote", default="origin", help="推送的远程仓库名")
    parser.add_argument("--branch", help="推送分支；默认使用当前分支")
    parser.add_argument("--no-push", action="store_true", help="只提交到本地，不推送")
    parser.add_argument("--dry-run", action="store_true", help="仅分析和验证，不修改仓库")
    parser.add_argument("--codex", default="codex", help="Codex CLI 命令或完整路径")
    parser.add_argument("-m", "--model", help="可选的 Codex 模型名称")
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--live", action="store_true", help="实时显示 Codex 执行过程")
    parser.add_argument("--timeout", type=int, default=300, help="Codex 超时秒数")
    parser.add_argument("--log-file", type=Path, help="同时写入日志文件")
    email_group = parser.add_mutually_exclusive_group()
    email_group.add_argument(
        "--email",
        action="store_true",
        help="要求启用邮件；配置不完整时立即报错",
    )
    email_group.add_argument(
        "--no-email",
        action="store_true",
        help="本次运行不发送邮件",
    )
    parser.add_argument("--mail-sender", help="发件邮箱；优先于 MAIL_SENDER")
    parser.add_argument(
        "--mail-auth-code",
        help="SMTP 授权码；优先于 MAIL_AUTH_CODE（使用环境变量更安全）",
    )
    parser.add_argument(
        "--mail-recipients",
        help="收件邮箱，多个用逗号或分号分隔；优先于 MAIL_RECIPIENTS",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def configure_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def configure_console_encoding() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    args = build_parser().parse_args(argv)
    configure_logging(args.log_file)
    if args.timeout < 1:
        print("错误：--timeout 必须大于 0", file=sys.stderr)
        return 2
    if args.once and args.run_now:
        print("错误：--once 已经会立即执行，不能同时使用 --run-now", file=sys.stderr)
        return 2

    config = RunConfig(
        repo=args.repo,
        remote=args.remote,
        branch=args.branch,
        push=not args.no_push,
        dry_run=args.dry_run,
        codex_command=args.codex,
        model=args.model,
        timeout=args.timeout,
        live=args.live,
        language=args.language,
    )
    try:
        email_config = (
            None
            if args.no_email
            else load_email_config(
                required=args.email,
                sender=args.mail_sender,
                auth_code=args.mail_auth_code,
                recipients=args.mail_recipients,
            )
        )
        if args.once:
            outcome = execute_and_notify(config, email_config)
            print(format_outcome(outcome))
            return 0
        schedule = parse_schedule_times(args.times or ["23:30"])
        run_scheduler(
            config,
            schedule,
            email_config=email_config,
            run_now=args.run_now,
        )
        return 0
    except (AutoCommitError, advisor.AdvisorError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止定时任务。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
