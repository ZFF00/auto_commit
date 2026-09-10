"""SMTP email notifications for automatic Git backup tasks."""

from __future__ import annotations

import html
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


EmailPolicy = Literal["always", "changes", "failures"]
EmailSecurity = Literal["ssl", "starttls", "none"]


class EmailNotificationError(RuntimeError):
    """An expected email configuration or delivery error."""


@dataclass(frozen=True)
class EmailConfig:
    recipients: tuple[str, ...]
    host: str
    port: int
    username: str
    password: str
    security: EmailSecurity = "ssl"
    sender: str | None = None
    sender_name: str = "Codex 自动推送"
    policy: EmailPolicy = "always"
    timeout: int = 30

    def validate(self) -> None:
        if not self.recipients:
            raise EmailNotificationError("邮件收件人不能为空")
        for address in (*self.recipients, self.sender or self.username):
            if not _looks_like_email(address):
                raise EmailNotificationError(f"无效的邮件地址：{address!r}")
        if not self.host or any(char.isspace() for char in self.host):
            raise EmailNotificationError(f"无效的 SMTP 服务器：{self.host!r}")
        if not 1 <= self.port <= 65535:
            raise EmailNotificationError("SMTP 端口必须在 1 到 65535 之间")
        if not self.username:
            raise EmailNotificationError("SMTP 用户名不能为空")
        if not self.password:
            raise EmailNotificationError(
                "缺少 SMTP 授权码，请设置环境变量 MAIL_AUTH_CODE"
            )
        if any(char in self.sender_name for char in "\r\n\0"):
            raise EmailNotificationError("发件人显示名称含有非法字符")
        if self.security not in {"ssl", "starttls", "none"}:
            raise EmailNotificationError(f"不支持的 SMTP 安全模式：{self.security}")
        if self.policy not in {"always", "changes", "failures"}:
            raise EmailNotificationError(f"不支持的邮件通知策略：{self.policy}")
        if self.timeout < 1:
            raise EmailNotificationError("SMTP 超时必须大于 0")


@dataclass(frozen=True)
class TaskNotification:
    success: bool
    status: str
    repository: Path
    branch: str
    occurred_at: str
    summary: str
    remote_repository: str = ""
    commit_hash: str = ""
    pushed: bool = False
    ignore_patterns: tuple[str, ...] = ()


def _looks_like_email(value: str) -> bool:
    if any(char in value for char in "\r\n\0"):
        return False
    local, separator, domain = value.rpartition("@")
    return bool(local and separator and "." in domain and not domain.startswith("."))


def should_send(config: EmailConfig, notification: TaskNotification) -> bool:
    if not notification.success:
        return True
    if config.policy == "failures":
        return False
    if config.policy == "changes":
        return notification.status == "committed"
    return True


def build_message(config: EmailConfig, notification: TaskNotification) -> EmailMessage:
    config.validate()
    state, color = _display_state(notification)
    remote_repository = _safe_remote_repository(notification.remote_repository)
    repository_label = _clean_header(
        _repository_label(notification.repository, remote_repository)
    )
    remote_display = remote_repository or "未配置或无法读取"
    subject = f"[{state}] Git 自动推送：{repository_label}"
    sender_address = config.sender or config.username
    sender_header = (
        f"{_clean_header(config.sender_name)} <{sender_address}>"
        if config.sender_name
        else sender_address
    )

    lines = [
        f"状态：{state}",
        f"本地仓库：{notification.repository}",
        f"远程仓库：{remote_display}",
        f"分支：{notification.branch or '未知'}",
        f"时间：{notification.occurred_at}",
    ]
    if notification.commit_hash:
        lines.append(f"提交：{notification.commit_hash}")
    lines.append(f"推送：{'已完成' if notification.pushed else '未执行或未完成'}")
    if notification.ignore_patterns:
        lines.extend(["", "新增 .gitignore 规则："])
        lines.extend(f"- {pattern}" for pattern in notification.ignore_patterns)
    summary = _truncate(notification.summary, 100_000)
    lines.extend(["", "任务详情：", summary])

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender_header
    message["To"] = ", ".join(config.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender_address.rpartition("@")[2])
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content("\n".join(lines))

    ignore_html = ""
    if notification.ignore_patterns:
        items = "".join(
            f"<li><code>{html.escape(pattern)}</code></li>"
            for pattern in notification.ignore_patterns
        )
        ignore_html = f"<h2>新增忽略规则</h2><ul>{items}</ul>"
    commit_html = ""
    if notification.commit_hash:
        commit_html = (
            "<tr><th>提交</th><td><code>"
            f"{html.escape(notification.commit_hash)}</code></td></tr>"
        )
    html_body = f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;background:#f3f4f6;color:#202124;font-family:Arial,'Microsoft YaHei',sans-serif">
  <div style="max-width:720px;margin:0 auto;padding:24px">
    <div style="background:#ffffff;border:1px solid #dfe1e5;border-radius:8px;overflow:hidden">
      <div style="padding:18px 24px;background:{color};color:#ffffff">
        <div style="font-size:13px">Git 自动推送</div>
        <div style="font-size:22px;font-weight:700;margin-top:4px">{html.escape(state)}</div>
      </div>
      <div style="padding:22px 24px">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr><th style="text-align:left;padding:6px 16px 6px 0">本地仓库</th><td>{html.escape(str(notification.repository))}</td></tr>
          <tr><th style="text-align:left;padding:6px 16px 6px 0">远程仓库</th><td>{html.escape(remote_display)}</td></tr>
          <tr><th style="text-align:left;padding:6px 16px 6px 0">分支</th><td>{html.escape(notification.branch or '未知')}</td></tr>
          <tr><th style="text-align:left;padding:6px 16px 6px 0">时间</th><td>{html.escape(notification.occurred_at)}</td></tr>
          {commit_html}
          <tr><th style="text-align:left;padding:6px 16px 6px 0">推送</th><td>{'已完成' if notification.pushed else '未执行或未完成'}</td></tr>
        </table>
        {ignore_html}
        <h2 style="font-size:16px;margin-top:22px">任务详情</h2>
        <pre style="white-space:pre-wrap;word-break:break-word;background:#f8f9fa;border:1px solid #e8eaed;border-radius:6px;padding:14px;font-family:Consolas,monospace;font-size:13px">{html.escape(summary)}</pre>
      </div>
    </div>
  </div>
</body>
</html>"""
    message.add_alternative(html_body, subtype="html")
    return message


def _display_state(notification: TaskNotification) -> tuple[str, str]:
    if not notification.success:
        return "任务失败", "#b3261e"
    if notification.status == "committed":
        return "提交成功", "#137333"
    if notification.status == "dry-run":
        return "演练完成", "#1967d2"
    return "仓库无变化", "#5f6368"


def _repository_label(repository: Path, remote_repository: str) -> str:
    """Return owner/repository for network remotes, otherwise the local name."""
    path = ""
    if "://" in remote_repository:
        path = urlsplit(remote_repository).path
    elif ":" in remote_repository and not (
        len(remote_repository) >= 2 and remote_repository[1] == ":"
    ):
        path = remote_repository.split(":", 1)[1]

    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) >= 2:
        parts[-1] = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
        return "/".join(parts[-2:])
    return repository.name or str(repository)


def _safe_remote_repository(value: str) -> str:
    """Remove control characters and embedded URL credentials from a remote."""
    cleaned = _clean_header(value)
    if "://" not in cleaned:
        return cleaned
    parsed = urlsplit(cleaned)
    if not parsed.hostname:
        return cleaned
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, "", ""))


def _clean_header(value: str) -> str:
    return " ".join(value.replace("\0", "").splitlines()).strip()[:200]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n\n[邮件内容已截断，原始字符数 {len(value)}]"


def send_notification(config: EmailConfig, notification: TaskNotification) -> bool:
    """Send one task notification and return False when policy suppresses it."""
    config.validate()
    if not should_send(config, notification):
        return False
    message = build_message(config, notification)
    context = ssl.create_default_context()
    try:
        if config.security == "ssl":
            with smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            ) as client:
                client.login(config.username, config.password)
                client.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as client:
                client.ehlo()
                if config.security == "starttls":
                    client.starttls(context=context)
                    client.ehlo()
                client.login(config.username, config.password)
                client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailNotificationError(f"邮件发送失败：{exc}") from exc
    return True
