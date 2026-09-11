import unittest
from email_notifier import (
    EmailConfig,
    EmailNotificationError,
    TaskNotification,
    TaskStep,
    build_message,
    send_notification,
    should_send,
)
from pathlib import Path
from unittest import mock


def email_config(**overrides: object) -> EmailConfig:
    values = {
        "recipients": ("backup@example.com",),
        "host": "smtp.example.com",
        "port": 465,
        "username": "sender@example.com",
        "password": "authorization-code",
        "security": "ssl",
        "sender": "sender@example.com",
        "sender_name": "Git Backup",
        "policy": "always",
        "timeout": 20,
    }
    values.update(overrides)
    return EmailConfig(**values)


def notification(**overrides: object) -> TaskNotification:
    values = {
        "success": True,
        "status": "committed",
        "repository": Path("C:/work/demo"),
        "branch": "main",
        "occurred_at": "2026-09-10T09:30:00+08:00",
        "summary": "提交并推送成功",
        "commit_hash": "0123456789abcdef",
        "pushed": True,
        "ignore_patterns": ("__pycache__/",),
    }
    values.update(overrides)
    return TaskNotification(**values)


class EmailMessageTests(unittest.TestCase):
    def test_pipeline_headers_follow_task_status(self):
        for status, success, color in (("committed", True, "#246448"), ("failed", False, "#982e43"),
                                        ("clean", True, "#4d5d72"), ("dry-run", True, "#265b91")):
            with self.subTest(status=status):
                message = build_message(email_config(), notification(status=status, success=success))
                body = message.get_body(preferencelist=("html",)).get_content()
                self.assertIn(f'bgcolor="{color}"', body)
                self.assertNotIn("<script", body)
                self.assertNotIn("<svg", body)
                self.assertNotIn("display:flex", body)

    def test_commit_title_body_hash_and_details_are_escaped(self):
        message = build_message(email_config(), notification(
            commit_message="feat: <标题>\n\n说明 & 内容\n第二行", commit_kind="created",
            file_count=12, steps=(TaskStep("检查仓库", "已完成", "<detail>"),)))
        plain = message.get_body(preferencelist=("plain",)).get_content()
        body = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("feat: <标题>", plain)
        self.assertIn("feat: &lt;标题&gt;", body)
        self.assertIn("说明 &amp; 内容", body)
        self.assertIn("第二行", body)
        self.assertIn("&lt;detail&gt;", body)
        self.assertIn("本次提交文件：12", plain)
        self.assertIn("0123456789abcdef", body)

    def test_dry_run_labels_proposed_commit_and_ignore_rules(self):
        notice = notification(status="dry-run", commit_hash="", commit_message="拟提交标题\n说明",
                              commit_kind="planned", pushed=False)
        message = build_message(email_config(), notice)
        for part in ("html", "plain"):
            body = message.get_body(preferencelist=(part,)).get_content()
            self.assertIn("拟提交信息 · 尚未创建", body)
            self.assertIn("建议忽略规则", body)
            self.assertNotIn("新增忽略规则", body)

    def test_failure_and_local_only_headlines_do_not_claim_push_success(self):
        notice = notification(success=False, status="failed", pushed=False, commit_kind="created",
            steps=(TaskStep("推送远程", "失败", "连接失败"),))
        failed = build_message(email_config(), notice).get_body(preferencelist=("html",)).get_content()
        self.assertIn("提交已保存，推送待重试", failed)
        local = build_message(email_config(), notification(pushed=False)).get_body(preferencelist=("html",)).get_content()
        self.assertIn("改动已保存到本地", local)
        self.assertNotIn("改动已安全送达", local)

    def test_large_details_are_bounded(self):
        message = build_message(email_config(), notification(summary="x" * 100_000,
            ignore_patterns=tuple(f"cache{i}/" for i in range(1000))))
        body = message.get_body(preferencelist=("html",)).get_content()
        self.assertLess(len(body), 30_000)
        self.assertIn("内容已截断", body)
        self.assertIn("另有 970 条", body)

    def test_build_message_contains_plain_and_html_parts(self):
        message = build_message(
            email_config(),
            notification(remote_repository="git@github.com:ZFF00/auto_commit.git"),
        )

        self.assertIn("提交成功", message["Subject"])
        self.assertIn("Git 自动推送", message["Subject"])
        self.assertNotIn("自动备份", message["Subject"])
        self.assertIn("ZFF00/auto_commit", message["Subject"])
        self.assertEqual(message["To"], "backup@example.com")
        self.assertEqual(len(message.get_payload()), 2)
        plain = message.get_body(preferencelist=("plain",)).get_content()
        rendered_html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("0123456789abcdef", plain)
        self.assertIn(f"本地仓库：{Path('C:/work/demo')}", plain)
        self.assertIn("远程仓库：git@github.com:ZFF00/auto_commit.git", plain)
        self.assertIn("__pycache__/", rendered_html)
        self.assertIn("本地仓库", rendered_html)
        self.assertIn("远程仓库", rendered_html)
        self.assertIn("Git 自动推送", rendered_html)
        self.assertIn('width="94%"', rendered_html)
        self.assertIn("width:94%;max-width:none", rendered_html)
        self.assertNotIn("max-width:720px", rendered_html)
        self.assertEqual(message["Auto-Submitted"], "auto-generated")

    def test_subject_supports_https_remote_and_strips_credentials(self):
        message = build_message(
            email_config(),
            notification(
                remote_repository="https://user:secret@github.com/ZFF00/auto_commit.git"
            ),
        )

        self.assertIn("ZFF00/auto_commit", message["Subject"])
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("https://github.com/ZFF00/auto_commit.git", plain)
        self.assertNotIn("secret", plain)

    def test_subject_falls_back_to_local_name_without_remote(self):
        message = build_message(email_config(), notification())

        self.assertIn("demo", message["Subject"])
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("远程仓库：未配置或无法读取", plain)

    def test_build_message_escapes_repository_report_html(self):
        message = build_message(
            email_config(), notification(summary="<script>alert('x')</script>")
        )
        rendered_html = message.get_body(preferencelist=("html",)).get_content()
        self.assertNotIn("<script>", rendered_html)
        self.assertIn("&lt;script&gt;", rendered_html)

    def test_policy_changes_sends_commits_and_all_failures(self):
        config = email_config(policy="changes")
        self.assertTrue(should_send(config, notification(status="committed")))
        self.assertFalse(should_send(config, notification(status="clean")))
        self.assertTrue(
            should_send(config, notification(success=False, status="failed"))
        )

    def test_policy_failures_suppresses_success(self):
        config = email_config(policy="failures")
        self.assertFalse(should_send(config, notification()))
        self.assertTrue(
            should_send(config, notification(success=False, status="failed"))
        )

    def test_validation_rejects_missing_authorization_code(self):
        with self.assertRaisesRegex(EmailNotificationError, "MAIL_AUTH_CODE"):
            email_config(password="").validate()


class EmailDeliveryTests(unittest.TestCase):
    def test_send_notification_uses_ssl_login_and_send_message(self):
        client = mock.MagicMock()
        context_manager = mock.MagicMock()
        context_manager.__enter__.return_value = client
        with mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=context_manager) as smtp:
            sent = send_notification(email_config(), notification())

        self.assertTrue(sent)
        smtp.assert_called_once()
        client.login.assert_called_once_with("sender@example.com", "authorization-code")
        client.send_message.assert_called_once()

    def test_send_notification_uses_starttls_when_configured(self):
        client = mock.MagicMock()
        context_manager = mock.MagicMock()
        context_manager.__enter__.return_value = client
        config = email_config(port=587, security="starttls")
        with mock.patch("email_notifier.smtplib.SMTP", return_value=context_manager):
            sent = send_notification(config, notification())

        self.assertTrue(sent)
        client.starttls.assert_called_once()
        self.assertEqual(client.ehlo.call_count, 2)
        client.login.assert_called_once()

    def test_send_notification_wraps_smtp_error(self):
        with mock.patch(
            "email_notifier.smtplib.SMTP_SSL",
            side_effect=OSError("network unavailable"),
        ):
            with self.assertRaisesRegex(EmailNotificationError, "邮件发送失败"):
                send_notification(email_config(), notification())

    def test_suppressed_notification_does_not_connect(self):
        config = email_config(policy="failures")
        with mock.patch("email_notifier.smtplib.SMTP_SSL") as smtp:
            sent = send_notification(config, notification())
        self.assertFalse(sent)
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
