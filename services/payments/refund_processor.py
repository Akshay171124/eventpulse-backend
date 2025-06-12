"""Refund processing. Author: Atharva Dhumal"""
import stripe, logging
from config.stripe_config import STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)
stripe.api_key = STRIPE_SECRET_KEY

class RefundProcessor:
    def __init__(self, db_session, redis_client):
        self.db = db_session
        self.redis = redis_client

    async def initiate_refund(self, payment_intent_id: str, amount=None, reason="requested_by_customer"):
        params = {"payment_intent": payment_intent_id, "reason": reason}
        if amount: params["amount"] = amount
        refund = stripe.Refund.create(**params)
        return {"refund_id": refund.id, "status": refund.status}

    async def get_refund_status(self, refund_id: str):
        refund = stripe.Refund.retrieve(refund_id)
        return {"refund_id": refund_id, "status": refund.status}
