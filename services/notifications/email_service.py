"""
Transactional email service backed by SendGrid.

Owner: Vasudha Jain (@jainvasudha)

All outbound email goes through this module.  Templates are managed in the
SendGrid dashboard and referenced by ID here — do NOT inline HTML in Python.
If you need a new template, create it in SendGrid first, then add the ID to
config/settings.py.

Emails are dispatched asynchronously via Celery so that the API never blocks
on a third-party HTTP call.  Failed sends are retried with exponential backoff
(max 4 attempts over ~15 minutes).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery import shared_task
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Asm, GroupId, GroupsToDisplay

from config.settings import (
    SENDGRID_API_KEY,
    SENDGRID_FROM_EMAIL,
    SENDGRID_TEMPLATE_ORDER_CONFIRMATION,
    SENDGRID_TEMPLATE_EVENT_REMINDER,
    SENDGRID_TEMPLATE_REFUND_PROCESSED,
)

logger = logging.getLogger(__name__)

# Template IDs for non-transactional emails that aren't in settings.py yet.
# (Vasudha) These were added quickly for the Feb launch — move to settings
# once the template names stabilise.
_TEMPLATE_WELCOME = "d-wlc901welcome"
_TEMPLATE_PASSWORD_RESET = "d-pwr802reset"

# SendGrid unsubscribe group IDs.  Transactional emails (booking confirmations,
# refund notifications) use the mandatory group so users can't accidentally
# suppress them.  Marketing / reminder emails use the optional group.
_ASM_TRANSACTIONAL_GROUP = 14320
_ASM_MARKETING_GROUP = 14321

# Maximum retry attempts for a single email (Celery will back off automatically).
MAX_RETRY_ATTEMPTS = 4


class EmailService:
    """High-level email API used by the rest of the application."""

    def __init__(self, client: Optional[SendGridAPIClient] = None):
        self._client = client or SendGridAPIClient(api_key=SENDGRID_API_KEY)

    # ------------------------------------------------------------------
    # Public methods — each queues a Celery task
    # ------------------------------------------------------------------

    def send_booking_confirmation(
        self,
        to_email: str,
        user_name: str,
        event_title: str,
        event_date: str,
        venue_name: str,
        ticket_count: int,
        total_amount_display: str,
        booking_id: str,
    ) -> None:
        """Queue a booking-confirmation email after successful payment."""
        dynamic_data = {
            "user_name": user_name,
            "event_title": event_title,
            "event_date": event_date,
            "venue_name": venue_name,
            "ticket_count": ticket_count,
            "total_amount": total_amount_display,
            "booking_id": booking_id,
        }
        _send_email_task.delay(
            to_email=to_email,
            template_id=SENDGRID_TEMPLATE_ORDER_CONFIRMATION,
            dynamic_data=dynamic_data,
            asm_group_id=_ASM_TRANSACTIONAL_GROUP,
            log_tag="booking_confirmation",
        )
        logger.info("Queued booking confirmation email to %s (booking %s)", to_email, booking_id)

    def send_refund_notification(
        self,
        to_email: str,
        user_name: str,
        refund_amount_display: str,
        event_title: str,
        refund_id: str,
        estimated_days: int,
    ) -> None:
        """Queue a refund-processed notification."""
        dynamic_data = {
            "user_name": user_name,
            "refund_amount": refund_amount_display,
            "event_title": event_title,
            "refund_id": refund_id,
            "estimated_days": estimated_days,
        }
        _send_email_task.delay(
            to_email=to_email,
            template_id=SENDGRID_TEMPLATE_REFUND_PROCESSED,
            dynamic_data=dynamic_data,
            asm_group_id=_ASM_TRANSACTIONAL_GROUP,
            log_tag="refund_notification",
        )
        logger.info("Queued refund notification to %s (refund %s)", to_email, refund_id)

    def send_event_reminder(
        self,
        to_email: str,
        user_name: str,
        event_title: str,
        event_date: str,
        venue_name: str,
        venue_address: str,
    ) -> None:
        """Queue a reminder email sent 24 hours before an event starts."""
        dynamic_data = {
            "user_name": user_name,
            "event_title": event_title,
            "event_date": event_date,
            "venue_name": venue_name,
            "venue_address": venue_address,
        }
        _send_email_task.delay(
            to_email=to_email,
            template_id=SENDGRID_TEMPLATE_EVENT_REMINDER,
            dynamic_data=dynamic_data,
            asm_group_id=_ASM_MARKETING_GROUP,
            log_tag="event_reminder",
        )

    def send_welcome_email(self, to_email: str, user_name: str) -> None:
        """Queue a welcome email after user registration."""
        dynamic_data = {"user_name": user_name}
        _send_email_task.delay(
            to_email=to_email,
            template_id=_TEMPLATE_WELCOME,
            dynamic_data=dynamic_data,
            asm_group_id=_ASM_MARKETING_GROUP,
            log_tag="welcome",
        )


# ------------------------------------------------------------------
# Celery task — runs in a worker process, NOT in the request path
# ------------------------------------------------------------------

@shared_task(
    bind=True,
    max_retries=MAX_RETRY_ATTEMPTS,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=900,
)
def _send_email_task(
    self,
    to_email: str,
    template_id: str,
    dynamic_data: Dict[str, Any],
    asm_group_id: int,
    log_tag: str,
) -> None:
    """
    Celery task that actually calls the SendGrid API.

    Retry schedule (with retry_backoff=True):
        attempt 1 — immediate
        attempt 2 — ~60 s
        attempt 3 — ~120 s
        attempt 4 — ~240 s
    After that the task is marked as failed and logged.
    """
    message = Mail(
        from_email=SENDGRID_FROM_EMAIL,
        to_emails=to_email,
    )
    message.template_id = template_id
    message.dynamic_template_data = {
        **dynamic_data,
        "current_year": datetime.now(timezone.utc).year,
    }

    # Attach unsubscribe group so SendGrid handles preference management.
    message.asm = Asm(
        group_id=GroupId(asm_group_id),
        groups_to_display=GroupsToDisplay([_ASM_TRANSACTIONAL_GROUP, _ASM_MARKETING_GROUP]),
    )

    client = SendGridAPIClient(api_key=SENDGRID_API_KEY)
    try:
        response = client.send(message)
        if response.status_code not in (200, 201, 202):
            logger.error(
                "SendGrid returned %d for %s email to %s: %s",
                response.status_code,
                log_tag,
                to_email,
                response.body,
            )
            raise RuntimeError(f"SendGrid HTTP {response.status_code}")
        logger.info(
            "Sent %s email to %s (status %d, attempt %d)",
            log_tag,
            to_email,
            response.status_code,
            self.request.retries + 1,
        )
    except Exception as exc:
        logger.warning(
            "Failed to send %s email to %s (attempt %d/%d): %s",
            log_tag,
            to_email,
            self.request.retries + 1,
            MAX_RETRY_ATTEMPTS,
            str(exc),
        )
        raise
# Template versioning
# Batch emails
# Email digest
# Push notifications
