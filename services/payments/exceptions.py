"""Custom exceptions for payments module."""
class PaymentError(Exception): pass
class PaymentIntentError(PaymentError): pass
class RefundError(PaymentError): pass
class WebhookVerificationError(PaymentError): pass
class InvoiceError(PaymentError): pass
