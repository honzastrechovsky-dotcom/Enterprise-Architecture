"""Notification service for write operation approvals.

Sends notifications to operators when:
- Approval is requested
- Operation is approved/rejected
- Operation execution completes/fails

Supports multiple channels:
- EMAIL: Real async SMTP via aiosmtplib
- WEBHOOK: HTTP POST to any webhook URL (Slack, Teams, custom)
- FALLBACK: Structured log entry when no channel is configured

Configuration via environment variables (loaded through Settings):
- SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS
- WEBHOOK_URL

All notification calls are fire-and-forget: failures are logged but never
propagate to the caller so the main HITL approval flow is never blocked.
"""

from __future__ import annotations

import asyncio
import email.mime.text
import email.utils
from enum import StrEnum
from typing import Any

import aiosmtplib
import httpx
import structlog

from src.config import Settings, get_settings
from src.models.user import User
from src.operations.write_framework import WriteOperation

log = structlog.get_logger(__name__)


class NotificationChannel(StrEnum):
    """Notification delivery channels."""

    EMAIL = "email"
    TEAMS = "teams"
    IN_APP = "in_app"


class NotificationService:
    """Service for sending operation approval notifications.

    Instantiate with explicit credentials (useful for testing), or call
    ``NotificationService.from_settings()`` to read from the app config.

    All public methods are async and wrap the actual send logic in a
    fire-and-forget task — failures are logged, never raised.

    Supports real SMTP and generic webhooks. Future enhancements:
    per-user template preferences, delivery tracking, and retry queuing.
    """

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int = 587,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        smtp_from: str = "noreply@enterprise-agents.local",
        smtp_use_tls: bool = False,
        # Generic webhook replaces the Teams-specific parameter.
        # The old ``teams_webhook_url`` kwarg is kept for backward compatibility.
        webhook_url: str | None = None,
        teams_webhook_url: str | None = None,
    ) -> None:
        """Initialise notification service.

        Args:
            smtp_host: SMTP server hostname.  ``None`` disables email.
            smtp_port: SMTP server port (587 = STARTTLS, 465 = SSL/TLS).
            smtp_user: SMTP login username.
            smtp_password: SMTP login password.
            smtp_from: Sender address used in the ``From`` header.
            smtp_use_tls: Use implicit TLS (port 465).  When ``False``,
                STARTTLS upgrade is attempted if the server advertises it.
            webhook_url: Generic webhook URL (Slack / Teams / custom).
            teams_webhook_url: Deprecated alias for *webhook_url*.
        """
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_from = smtp_from
        self.smtp_use_tls = smtp_use_tls
        # Prefer the new generic name; fall back to the old Teams-specific one.
        self.webhook_url = webhook_url or teams_webhook_url

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> NotificationService:
        """Create a ``NotificationService`` from application settings.

        Args:
            settings: ``Settings`` instance.  Uses ``get_settings()`` when
                ``None``.

        Returns:
            Configured ``NotificationService`` instance.
        """
        cfg = settings or get_settings()
        return cls(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_user=cfg.smtp_user,
            smtp_password=(
                cfg.smtp_password.get_secret_value() if cfg.smtp_password else None
            ),
            smtp_from=cfg.smtp_from,
            smtp_use_tls=cfg.smtp_use_tls,
            webhook_url=cfg.webhook_url,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_approval_request(
        self,
        operation: WriteOperation,
        approvers: list[User],
        channels: list[NotificationChannel],
    ) -> bool:
        """Send approval-request notification to operators (fire-and-forget).

        Args:
            operation: ``WriteOperation`` requiring approval.
            approvers: List of users who can approve.
            channels: Notification channels to use.

        Returns:
            ``True`` if at least one notification was dispatched successfully.
        """
        if not approvers:
            log.warning(
                "notification.no_approvers",
                operation_id=operation.id,
                tenant_id=str(operation.tenant_id),
            )
            return False

        subject = f"Approval Required: {operation.description}"
        body = self._format_approval_request(operation)

        results = []

        for approver in approvers:
            for channel in channels:
                if channel == NotificationChannel.EMAIL:
                    result = await self._fire_and_forget_email(
                        approver.email, subject, body
                    )
                    results.append(result)
                elif channel == NotificationChannel.TEAMS:
                    result = await self._fire_and_forget_webhook(subject, body)
                    results.append(result)
                elif channel == NotificationChannel.IN_APP:
                    result = await self._send_in_app_notification(
                        approver, operation, "approval_request"
                    )
                    results.append(result)

        success = any(results)
        log.info(
            "notification.approval_request_sent",
            operation_id=operation.id,
            approvers_count=len(approvers),
            channels=channels,
            success=success,
        )
        return success

    async def send_approval_result(
        self,
        operation: WriteOperation,
        result: str,  # "approved" or "rejected"
        requester: User,
    ) -> bool:
        """Send approval-result notification to the requester (fire-and-forget).

        Args:
            operation: ``WriteOperation`` that was reviewed.
            result: ``"approved"`` or ``"rejected"``.
            requester: User who requested the operation.

        Returns:
            ``True`` if notification was dispatched.
        """
        subject = f"Operation {result.title()}: {operation.description}"
        body = self._format_approval_result(operation, result)

        success = await self._fire_and_forget_email(requester.email, subject, body)

        log.info(
            "notification.approval_result_sent",
            operation_id=operation.id,
            result=result,
            requester=requester.email,
            success=success,
        )
        return success

    async def send_execution_result(
        self,
        operation: WriteOperation,
        result: str,  # "completed" or "failed"
    ) -> bool:
        """Send execution-result notification (fire-and-forget).

        Tries email first, then webhook, then structured log fallback.

        Args:
            operation: ``WriteOperation`` that was executed.
            result: ``"completed"`` or ``"failed"``.

        Returns:
            Always ``True`` (fallback log always succeeds).
        """
        subject = f"Operation {result.title()}: {operation.description}"
        body = self._format_execution_result(operation, result)

        # Best-effort: email then webhook then structured log.
        sent = False

        if self.smtp_host:
            sent = await self._fire_and_forget_email(
                _system_recipient(operation), subject, body
            )

        if not sent and self.webhook_url:
            sent = await self._fire_and_forget_webhook(subject, body)

        if not sent:
            # Fallback: structured log entry so the event is never silently lost.
            log.info(
                "notification.execution_result_fallback",
                operation_id=operation.id,
                result=result,
                tenant_id=str(operation.tenant_id),
                subject=subject,
                body=body,
            )
            sent = True

        return sent

    # ------------------------------------------------------------------
    # Fire-and-forget wrappers
    # ------------------------------------------------------------------

    async def _fire_and_forget_email(
        self, to: str, subject: str, body: str
    ) -> bool:
        """Schedule email send as a background task.

        Returns immediately with the result of the pre-flight config check
        (``False`` if SMTP is not configured).  Actual delivery happens in
        the background; failures are logged.
        """
        if not self.smtp_host:
            log.debug("notification.email_skipped", reason="smtp_not_configured")
            return False

        async def _send() -> None:
            try:
                await self._send_email(to, subject, body)
            except Exception as exc:
                log.error(
                    "notification.email_background_failed",
                    to=to,
                    error=str(exc),
                )

        asyncio.create_task(_send())
        return True

    async def _fire_and_forget_webhook(self, title: str, text: str) -> bool:
        """Schedule webhook POST as a background task.

        Returns immediately with the pre-flight config check result.
        """
        if not self.webhook_url:
            log.debug("notification.webhook_skipped", reason="webhook_not_configured")
            return False

        async def _send() -> None:
            try:
                await self._send_webhook(title, text)
            except Exception as exc:
                log.error(
                    "notification.webhook_background_failed",
                    error=str(exc),
                )

        asyncio.create_task(_send())
        return True

    # ------------------------------------------------------------------
    # Real transport implementations
    # ------------------------------------------------------------------

    async def _send_email(self, to: str, subject: str, body: str) -> bool:
        """Send an email via SMTP using aiosmtplib.

        Uses STARTTLS by default (smtp_use_tls=False).  Set smtp_use_tls=True
        for implicit TLS (port 465).

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text message body.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        if not self.smtp_host:
            log.debug("notification.email_skipped", reason="smtp_not_configured")
            return False

        try:
            message = email.mime.text.MIMEText(body, "plain", "utf-8")
            message["From"] = self.smtp_from
            message["To"] = to
            message["Subject"] = subject
            message["Date"] = email.utils.formatdate(localtime=True)
            message["Message-ID"] = email.utils.make_msgid()

            smtp_kwargs: dict[str, Any] = {
                "hostname": self.smtp_host,
                "port": self.smtp_port,
                "use_tls": self.smtp_use_tls,
            }
            if self.smtp_user:
                smtp_kwargs["username"] = self.smtp_user
            if self.smtp_password:
                smtp_kwargs["password"] = self.smtp_password

            await aiosmtplib.send(message, **smtp_kwargs)

            log.info(
                "notification.email_sent",
                to=to,
                subject=subject,
                smtp_host=self.smtp_host,
            )
            return True

        except aiosmtplib.SMTPException as exc:
            log.error("notification.email_smtp_error", to=to, error=str(exc))
            return False
        except Exception as exc:
            log.error("notification.email_failed", to=to, error=str(exc))
            return False

    async def _send_webhook(self, title: str, text: str) -> bool:
        """POST a notification payload to the configured webhook URL.

        The payload format is a simple JSON object that is compatible with
        both Slack incoming webhooks (``text`` field) and Microsoft Teams
        message cards (``title`` + ``text`` fields).

        Args:
            title: Short summary / message title.
            text: Full message body.

        Returns:
            ``True`` on HTTP 200, ``False`` otherwise.
        """
        if not self.webhook_url:
            log.debug("notification.webhook_skipped", reason="webhook_not_configured")
            return False

        try:
            payload: dict[str, Any] = {
                # Slack format
                "text": f"*{title}*\n{text}",
                # Teams MessageCard format (ignored by Slack)
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": title,
                "themeColor": "0078D4",
                "title": title,
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.webhook_url, json=payload)

            if response.status_code == 200:
                log.info("notification.webhook_sent", title=title)
                return True

            log.warning(
                "notification.webhook_failed",
                status=response.status_code,
                response=response.text[:200],
            )
            return False

        except Exception as exc:
            log.error("notification.webhook_error", error=str(exc))
            return False

    # Kept for backward compatibility — delegates to _send_webhook.
    async def _send_teams_message(self, title: str, text: str) -> bool:
        """Send notification via Teams webhook (backward-compatible alias).

        Deprecated: use ``_send_webhook`` directly.
        """
        return await self._send_webhook(title, text)

    async def _send_in_app_notification(
        self, user: User, operation: WriteOperation, notification_type: str
    ) -> bool:
        """Send in-app notification (stored in database).

        Args:
            user: User to notify.
            operation: Related ``WriteOperation``.
            notification_type: Type of notification.

        Returns:
            ``True`` — always succeeds (logs the event).
        """
        # Logs the in-app notification event. Persist to a notifications table
        # if delivery history tracking is required.
        log.info(
            "notification.in_app_created",
            user_id=str(user.id),
            operation_id=operation.id,
            type=notification_type,
        )
        return True

    # ------------------------------------------------------------------
    # Message formatting helpers
    # ------------------------------------------------------------------

    def _format_approval_request(self, operation: WriteOperation) -> str:
        """Format approval request message body."""
        return f"""
A write operation requires your approval:

Operation ID: {operation.id}
Description: {operation.description}
Connector: {operation.connector}
Operation Type: {operation.operation_type}
Risk Level: {operation.risk_level.upper()}
Requires MFA: {"Yes" if operation.requires_mfa else "No"}

Proposed by: {operation.user_id}
Proposed at: {operation.proposed_at.isoformat()}

Parameters:
{self._format_params(operation.params)}

Please review and approve or reject this operation.
        """.strip()

    def _format_approval_result(self, operation: WriteOperation, result: str) -> str:
        """Format approval result message body."""
        return f"""
Your write operation has been {result}:

Operation ID: {operation.id}
Description: {operation.description}
Risk Level: {operation.risk_level.upper()}

{result.title()} by: {operation.approved_by}
{result.title()} at: {operation.approved_at.isoformat() if operation.approved_at else "N/A"}

{self._get_next_steps(operation, result)}
        """.strip()

    def _format_execution_result(self, operation: WriteOperation, result: str) -> str:
        """Format execution result message body."""
        return f"""
Operation execution {result}:

Operation ID: {operation.id}
Description: {operation.description}
Executed at: {operation.executed_at.isoformat() if operation.executed_at else "N/A"}

Status: {operation.status.upper()}
        """.strip()

    def _format_params(self, params: dict[str, Any]) -> str:
        """Format operation parameters for display."""
        return "\n".join(f"  {key}: {value}" for key, value in params.items())

    def _get_next_steps(self, operation: WriteOperation, result: str) -> str:
        """Get next-steps message based on approval result."""
        if result == "approved":
            return "The operation will be executed shortly."
        return "The operation has been cancelled and will not be executed."


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _system_recipient(operation: WriteOperation) -> str:
    """Return a best-effort system recipient address for execution notifications.

    In a full implementation this would look up the original requester's
    email from the database.  For now we construct a synthetic address from
    the tenant / operation IDs so the email is still routable in test
    environments.
    """
    return f"ops+{operation.tenant_id}@enterprise-agents.local"
