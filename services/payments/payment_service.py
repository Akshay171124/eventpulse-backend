"""Core payment processing service. Author: Atharva Dhumal"""
import stripe, logging
from config.stripe_config import STRIPE_SECRET_KEY, PAYMENT_CURRENCY, PLATFORM_FEE_PERCENT
logger = logging.getLogger(__name__)
stripe.api_key = STRIPE_SECRET_KEY

class PaymentService:
    def __init__(self, db_session, redis_client):
        self.db = db_session
        self.redis = redis_client

    async def create_payment_intent(self, user_id: str, event_id: str, amount: int, currency: str = PAYMENT_CURRENCY):
        platform_fee = int(amount * PLATFORM_FEE_PERCENT / 100)
        intent = stripe.PaymentIntent.create(amount=amount, currency=currency,
            metadata={"user_id": user_id, "event_id": event_id, "platform_fee": platform_fee})
        logger.info(f"Created payment intent {intent.id} for user={user_id} event={event_id}")
        return {"client_secret": intent.client_secret, "intent_id": intent.id}

    async def confirm_payment(self, intent_id: str):
        intent = stripe.PaymentIntent.retrieve(intent_id)
        return {"status": intent.status, "amount": intent.amount}

    async def get_payment_status(self, intent_id: str):
        intent = stripe.PaymentIntent.retrieve(intent_id)
        return {"intent_id": intent_id, "status": intent.status, "amount": intent.amount}
