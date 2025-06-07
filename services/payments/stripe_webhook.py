"""Stripe webhook handler. Author: Atharva Dhumal"""
import stripe, logging
from config.stripe_config import STRIPE_WEBHOOK_SECRET
logger = logging.getLogger(__name__)

class StripeWebhookHandler:
    def __init__(self, db_session, redis_client):
        self.db = db_session
        self.redis = redis_client

    async def handle_webhook(self, payload: bytes, sig_header: str):
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        event_type = event["type"]
        if event_type == "payment_intent.succeeded":
            await self._handle_payment_success(event["data"]["object"])
        elif event_type == "payment_intent.payment_failed":
            await self._handle_payment_failure(event["data"]["object"])
        elif event_type == "charge.refunded":
            await self._handle_refund(event["data"]["object"])
        return {"status": "processed"}

    async def _handle_payment_success(self, pi):
        logger.info(f"Payment succeeded for user={pi['metadata']['user_id']}")
    async def _handle_payment_failure(self, pi):
        logger.warning(f"Payment failed: {pi['id']}")
    async def _handle_refund(self, charge):
        logger.info(f"Refund processed for charge: {charge['id']}")
