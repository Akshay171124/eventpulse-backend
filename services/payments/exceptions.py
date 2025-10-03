"""Custom exceptions for the payments service."""


class PaymentProcessingError(Exception):
    """Raised when a payment operation fails after exhausting retries."""
    pass


class DuplicatePaymentError(PaymentProcessingError):
    """Raised when a duplicate payment is detected."""
    pass


class PaymentTimeoutError(PaymentProcessingError):
    """Raised when a payment operation times out."""
    pass


class WebhookSignatureError(Exception):
    """Raised when webhook signature verification fails."""
    pass


class WebhookProcessingError(Exception):
    """Raised when a webhook event cannot be processed."""
    pass


class RefundError(Exception):
    """Raised when a refund operation fails."""
    pass


class RefundTimeoutError(RefundError):
    """Raised when a refund exceeds the expected processing window."""
    pass


class InvoiceGenerationError(Exception):
    """Raised when invoice generation fails."""
    pass
