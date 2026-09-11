"""SMTP email notifications for automatic Git push tasks."""

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

__all__ = [
    "EmailConfig",
    "EmailNotificationError",
    "TaskNotification",
    "build_message",
    "send_notification",
    "should_send",
]


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
    sender_name: str = "Codex Git 推送"
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
class TaskStep:
    title: str
    state: str
    detail: str


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
    commit_message: str = ""
    commit_kind: str = ""
    file_count: int | None = None
    steps: tuple[TaskStep, ...] = ()


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
    title, lead = _headline(notification)
    commit_label = {"planned": "拟提交信息 · 尚未创建", "head": "当前 HEAD · 本次未新增提交",
                    "created": "本次提交"}.get(notification.commit_kind, "提交信息")
    if not notification.success and notification.commit_kind == "created":
        commit_label = "本地提交 · 已保存"
    commit_message = _truncate(notification.commit_message, 20_000)
    commit_title, _, commit_body = commit_message.partition("\n")
    ignore_label = "建议忽略规则" if notification.status == "dry-run" else "新增忽略规则"
    ignore_text = "\n".join(notification.ignore_patterns[:30])
    if len(notification.ignore_patterns) > 30:
        ignore_text += f"\n另有 {len(notification.ignore_patterns) - 30} 条，详见运行报告。"
    ignore_text = _truncate(ignore_text, 3000)
    count_label = "预计包含文件" if notification.commit_kind == "planned" else "本次提交文件"
    steps = notification.steps or tuple(TaskStep(name, "未记录", "没有可用的阶段记录。")
        for name in ("检查仓库", "分析与筛选", "创建提交", "推送远程"))
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
    if commit_message:
        lines.extend(["", commit_label + "：", commit_message])
    if notification.file_count is not None:
        lines.append(f"{count_label}：{notification.file_count}")
    lines.append(f"推送：{'已完成' if notification.pushed else '未执行或未完成'}")
    if notification.ignore_patterns:
        lines.extend(["", ignore_label + "：", ignore_text])
    lines.extend(["", "执行记录："])
    for index, step in enumerate(steps, 1):
        lines.append(f"{index}. {step.title} · {step.state}\n{_truncate(step.detail, 1000)}")
    summary = _truncate(notification.summary, 4000)
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

    wrap = "overflow-wrap:anywhere;word-break:break-all;"
    table = 'role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0"'
    metadata = [("本地仓库", str(notification.repository)), ("远程仓库", remote_display),
                ("目标分支", notification.branch or "未知"), ("完成时间", notification.occurred_at)]
    if notification.file_count is not None:
        metadata.append((count_label, str(notification.file_count)))
    metadata.append(("推送结果", "已完成" if notification.pushed else "未执行或未完成"))
    metadata_html = "".join(
        f'<tr><td width="76" style="width:76px;padding:10px 8px;vertical-align:top;color:#627084;border-bottom:1px solid #e0e6ed">{html.escape(label)}</td>'
        f'<td style="padding:10px 8px;border-bottom:1px solid #e0e6ed;{wrap}">{html.escape(value)}</td></tr>'
        for label, value in metadata
    )
    commit_html = ""
    if commit_message or notification.commit_hash:
        hash_text = notification.commit_hash or ("未生成" if notification.commit_kind == "planned" else "未记录")
        commit_html = f'''<table {table} style="table-layout:fixed;background:#f8fafc;border:1px solid #e0e6ed;border-radius:5px;margin-bottom:24px"><tr><td style="padding:16px;{wrap}">
<div style="font-size:12px;color:#627084;margin-bottom:8px">{html.escape(commit_label)}</div>
<div style="font-size:16px;font-weight:600;line-height:1.6">{html.escape(commit_title or '提交说明未记录')}</div>
<div style="font-size:13px;line-height:1.9;white-space:pre-wrap;color:#627084;margin-top:8px">{html.escape(commit_body.strip())}</div>
<div style="font-family:Consolas,monospace;font-size:12px;color:#627084;margin-top:12px;{wrap}">提交号：{html.escape(hash_text)}</div>
</td></tr></table>'''
    timeline_rows = []
    for index, step in enumerate(steps, 1):
        failed = step.state == "失败"
        node_bg, node_ink = ("#fbe5e5", "#982e43") if failed else ("#e8eff5", "#345871")
        # Table cells form the connector; no positioning, pseudo-elements, or icons.
        detail = html.escape(_truncate(step.detail, 1000))
        if index == 2 and ignore_text:
            detail += f'<br><span style="color:#233044">{ignore_label}：</span><br>{html.escape(ignore_text).replace(chr(10), "<br>")}'
        connector = "border-left:1px solid #dce3ec;" if index < len(steps) else ""
        timeline_rows.append(f'''<tr>
<td width="36" style="width:36px;vertical-align:top"><table role="presentation" width="26" cellspacing="0" cellpadding="0"><tr><td height="26" align="center" bgcolor="{node_bg}" style="height:26px;border-radius:13px;color:{node_ink};font-size:12px">{index}</td></tr></table></td>
<td style="font-size:14px;font-weight:600;line-height:26px;{wrap}">{html.escape(step.title)} <span style="font-size:12px;font-weight:400;color:{'#982e43' if failed else '#627084'}">{html.escape(step.state)}</span></td></tr>
<tr><td width="36" valign="top"><table role="presentation" width="26" height="100%" cellspacing="0" cellpadding="0"><tr><td width="12"></td><td style="{connector}">&nbsp;</td></tr></table></td>
<td style="padding:3px 0 18px;font-size:13px;line-height:1.8;color:#627084;{wrap}">{detail}</td></tr>''')
    timeline_html = "".join(timeline_rows)
    html_body = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f2f4f7;color:#233044;font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.6">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:#f3f4f6">
    <tr>
      <td align="center" style="padding:24px 0">
        <table role="presentation" width="94%" cellspacing="0" cellpadding="0" border="0" style="width:94%;max-width:none;table-layout:fixed;background:#ffffff;border:1px solid #e0e6ed;border-radius:5px">
          <tr><td bgcolor="{color}" style="padding:18px 20px;background:{color};color:#ffffff;{wrap}">
            <div style="font-size:12px">Git 自动推送</div>
            <div style="font-size:23px;font-weight:600;line-height:1.4;margin:6px 0">{html.escape(title)}</div>
            <div style="font-size:13px">{html.escape(repository_label)}</div>
            <div style="font-size:13px;margin-top:4px">{html.escape(lead)}</div>
            <div style="font-size:12px;margin-top:10px">{html.escape(state)}</div>
          </td></tr>
          <tr><td style="padding:22px 18px">
            <table {table} style="table-layout:fixed;font-size:12px;border:1px solid #e0e6ed;border-radius:5px;margin-bottom:24px">{metadata_html}</table>
            {commit_html}
            <div style="font-size:12px;color:#627084;margin-bottom:14px">执行记录</div>
            <table {table} style="table-layout:fixed">{timeline_html}</table>
            <div style="border-top:1px solid #e0e6ed;margin-top:8px;padding-top:16px;font-size:13px">任务详情</div>
            <div style="font-size:13px;line-height:1.8;white-space:pre-wrap;color:#627084;margin-top:6px;{wrap}">{html.escape(summary)}</div>
          </td></tr>
          <tr><td style="padding:14px 18px;border-top:1px solid #e0e6ed;background:#f8fafc;color:#627084;font-size:11px">Codex Git 自动推送 · 自动生成的任务通知</td></tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    message.add_alternative(html_body, subtype="html")
    return message


def _display_state(notification: TaskNotification) -> tuple[str, str]:
    if not notification.success:
        return "任务失败", "#982e43"
    if notification.status == "committed":
        return "提交成功", "#246448"
    if notification.status == "dry-run":
        return "演练完成", "#265b91"
    return "仓库无变化", "#4d5d72"


def _headline(notification: TaskNotification) -> tuple[str, str]:
    if not notification.success:
        push_failed = any(s.title == "推送远程" and s.state == "失败" for s in notification.steps)
        if push_failed and notification.commit_hash:
            return "提交已保存，推送待重试", "本地提交仍然保留，远程推送未完成。"
        return "任务未完成", "请查看执行记录与错误详情。"
    if notification.status == "dry-run":
        return "演练检查完成", "仅完成分析与校验，未创建或推送提交。"
    if notification.status == "committed":
        return ("改动已安全送达", "本地提交与远程推送均已完成。") if notification.pushed else (
            "改动已保存到本地", "本地提交已完成，本次未推送远程。")
    return "没有新增提交", "远程推送已完成。" if notification.pushed else "本次未推送远程。"


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
