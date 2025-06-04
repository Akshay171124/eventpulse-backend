"""Stripe-specific configuration. Author: Atharva Dhumal"""
import os
from dotenv import load_dotenv
load_dotenv()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_test_placeholder")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
PAYMENT_CURRENCY = "usd"
PLATFORM_FEE_PERCENT = 5.5
MINIMUM_CHARGE_AMOUNT = 50
PAYOUT_DELAY_DAYS = 7
