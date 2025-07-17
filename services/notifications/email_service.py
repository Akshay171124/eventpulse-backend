"""Email notification service using SendGrid. Author: Atharva Dhumal"""
import logging, os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
logger = logging.getLogger(__name__)
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "SG.placeholder")
FROM_EMAIL = "noreply@eventpulse.com"

class EmailService:
    def __init__(self): self.client = SendGridAPIClient(SENDGRID_API_KEY)
    async def send_booking_confirmation(self, to_email, event_name, ticket_count, total_amount):
        await self._send_email(to_email, f"Your EventPulse Booking: {event_name}",
            f"Booking confirmed. {ticket_count} ticket(s), total: ${total_amount:.2f}")
    async def send_refund_notification(self, to_email, event_name, amount):
        await self._send_email(to_email, f"Refund Processed: {event_name}", f"Refund of ${amount:.2f} processed.")
    async def send_welcome_email(self, to_email, full_name):
        await self._send_email(to_email, "Welcome to EventPulse!", f"Hi {full_name}, welcome!")
    async def _send_email(self, to_email, subject, body):
        msg = Mail(from_email=Email(FROM_EMAIL), to_emails=To(to_email), subject=subject, plain_text_content=Content("text/plain", body))
        self.client.send(msg)
        logger.info(f"Email sent to {to_email}")
# Email retry logic
# Notification preferences
# Unsubscribe handling
