"""SMS notification service using Twilio. Author: Atharva Dhumal"""
import logging, os
from twilio.rest import Client
logger = logging.getLogger(__name__)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "AC_placeholder")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "placeholder")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "+1234567890")

class SMSService:
    def __init__(self): self.client = Client(TWILIO_SID, TWILIO_TOKEN)
    async def send_ticket_confirmation(self, phone, event_name, ticket_count):
        await self._send_sms(phone, f"EventPulse: {ticket_count} ticket(s) for {event_name} confirmed!")
    async def send_event_reminder(self, phone, event_name, hours_until):
        await self._send_sms(phone, f"EventPulse: {event_name} starts in {hours_until} hours!")
    async def _send_sms(self, to_phone, body):
        self.client.messages.create(body=body, from_=TWILIO_FROM, to=to_phone)
        logger.info(f"SMS sent to {to_phone}")
