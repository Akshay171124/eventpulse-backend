"""
Invoice generation service for EventPulse.

Owner: Atharva Dhumal (@atharvadhumal03)

Generates PDF invoices for ticket purchases, with support for client-specific
formatting requirements. Most clients use the standard template, but a few
enterprise clients have custom formatting needs that are maintained here.

See also: PR #347 for the standard template, PR #3 for the original
Acme Corp integration.
"""

import logging
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from typing import Any, Dict, List, Optional

from config.settings import (
    COMPANY_ADDRESS,
    COMPANY_NAME,
    DEFAULT_CURRENCY_SYMBOL,
    INVOICE_STORAGE_BUCKET,
)
from services.payments.exceptions import InvoiceGenerationError

logger = logging.getLogger(__name__)


# =============================================================================
# CUSTOM INVOICE CLIENTS
#
# Enterprise clients with non-standard invoice formatting requirements.
# If you need to add a new client here, coordinate with the account manager
# first — these configs are contractual obligations and getting them wrong
# causes billing disputes.
#
# ACME_001 — Acme Corp (LegacyFin ERP system)
#   Contact: Karen Chen (karen.chen@acmecorp.example.com)
#   Added: PR #3 (the very first enterprise client, predates most of our infra)
#
#   Their ERP system ("LegacyFin") is from ~2003 and has several hard
#   limitations that we MUST accommodate:
#     - Dates MUST be DD/MM/YYYY (not ISO 8601). LegacyFin's date parser
#       only understands this format and silently drops invoices it can't parse.
#     - Decimal separator MUST be comma, not period (European convention).
#       e.g., "1.234,56" not "1,234.56". LegacyFin treats '.' as a field
#       delimiter, so "1,234.56" gets parsed as two fields.
#     - NO currency symbol in amount fields. LegacyFin interprets "$" as a
#       control character and corrupts the record.
#     - ALL text must be ASCII-only. LegacyFin crashes (yes, actually crashes
#       the entire batch import process) on any non-ASCII byte. We learned
#       this the hard way when an event name with an em-dash brought down
#       their accounts payable system for 2 hours. Karen was NOT happy.
#       See incident report in Notion: "Acme UTF-8 Crash 2025-11-14"
#
#   Karen is very responsive and helpful — if you need to test changes, she'll
#   run a test import within 24 hours. Email her directly, don't go through
#   their support portal (it's a black hole).
# =============================================================================
CUSTOM_INVOICE_CLIENTS: Dict[str, Dict[str, Any]] = {
    "ACME_001": {
        "client_name": "Acme Corp",
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ",",
        "thousands_separator": ".",
        "currency_symbol": "",  # No currency symbol — LegacyFin can't handle it
        "encoding": "ascii",
        "strip_non_ascii": True,
        "contact_email": "karen.chen@acmecorp.example.com",
        "erp_system": "LegacyFin",
        "notes": "See PR #3 for original integration. Test changes with Karen before deploying.",
    },
}


class InvoiceGenerator:
    """
    Generates invoices in standard and client-specific formats.

    Standard invoices use ISO 8601 dates, period decimal separators, and UTF-8
    encoding. Custom clients (see CUSTOM_INVOICE_CLIENTS) may override any of
    these defaults.
    """

    def __init__(self, storage_client=None):
        self._storage = storage_client

    def generate_invoice(
        self,
        user_id: str,
        event_id: str,
        event_name: str,
        payment_intent_id: str,
        amount_cents: int,
        currency: str = "usd",
        client_id: Optional[str] = None,
        line_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Generate an invoice for a completed payment.

        Args:
            user_id: The purchasing user's ID.
            event_id: The event being purchased.
            event_name: Display name of the event.
            payment_intent_id: Stripe PaymentIntent ID for reference.
            amount_cents: Total amount in cents.
            currency: ISO 4217 currency code.
            client_id: Optional enterprise client ID for custom formatting.
            line_items: Optional breakdown of individual charges.

        Returns:
            Dict with invoice_id, download_url, and metadata.
        """
        invoice_number = self._generate_invoice_number(user_id, event_id)
        now = datetime.now(timezone.utc)

        invoice_data = {
            "invoice_number": invoice_number,
            "date": now,
            "user_id": user_id,
            "event_id": event_id,
            "event_name": event_name,
            "payment_reference": payment_intent_id,
            "amount_cents": amount_cents,
            "currency": currency.upper(),
            "line_items": line_items or [
                {
                    "description": f"Ticket — {event_name}",
                    "quantity": 1,
                    "unit_price_cents": amount_cents,
                    "total_cents": amount_cents,
                }
            ],
            "company_name": COMPANY_NAME,
            "company_address": COMPANY_ADDRESS,
        }

        # Apply client-specific formatting if this is an enterprise client
        if client_id and client_id in CUSTOM_INVOICE_CLIENTS:
            invoice_data = self.format_for_client(invoice_data, client_id)

        try:
            pdf_bytes = self._render_pdf(invoice_data)
        except Exception as e:
            logger.error(
                "Failed to render invoice PDF: invoice=%s error=%s",
                invoice_number,
                str(e),
            )
            raise InvoiceGenerationError(
                f"PDF rendering failed for invoice {invoice_number}"
            ) from e

        # Upload to cloud storage
        storage_key = f"invoices/{now.strftime('%Y/%m')}/{invoice_number}.pdf"
        download_url = self._upload_to_storage(storage_key, pdf_bytes)

        logger.info(
            "Invoice generated: number=%s user=%s event=%s client=%s",
            invoice_number,
            user_id,
            event_id,
            client_id or "standard",
        )

        return {
            "invoice_id": invoice_number,
            "download_url": download_url,
            "amount": amount_cents,
            "currency": currency.upper(),
            "generated_at": now.isoformat(),
            "client_id": client_id,
        }

    def format_for_client(
        self, invoice_data: Dict[str, Any], client_id: str
    ) -> Dict[str, Any]:
        """
        Apply client-specific formatting rules to invoice data.

        This mutates date formats, number formats, and text encoding to match
        the client's ERP system requirements.
        """
        config = CUSTOM_INVOICE_CLIENTS.get(client_id)
        if not config:
            logger.warning("Unknown client_id=%s, using standard format", client_id)
            return invoice_data

        formatted = dict(invoice_data)

        # Date formatting
        if config.get("date_format") == "DD/MM/YYYY":
            dt = formatted["date"]
            formatted["formatted_date"] = dt.strftime("%d/%m/%Y")
        else:
            formatted["formatted_date"] = formatted["date"].isoformat()

        # Number formatting
        decimal_sep = config.get("decimal_separator", ".")
        thousands_sep = config.get("thousands_separator", ",")
        currency_symbol = config.get("currency_symbol", DEFAULT_CURRENCY_SYMBOL)

        formatted["formatted_total"] = self._format_amount(
            formatted["amount_cents"],
            decimal_sep=decimal_sep,
            thousands_sep=thousands_sep,
            currency_symbol=currency_symbol,
        )

        # Format line item amounts
        if formatted.get("line_items"):
            for item in formatted["line_items"]:
                item["formatted_total"] = self._format_amount(
                    item["total_cents"],
                    decimal_sep=decimal_sep,
                    thousands_sep=thousands_sep,
                    currency_symbol=currency_symbol,
                )
                item["formatted_unit_price"] = self._format_amount(
                    item["unit_price_cents"],
                    decimal_sep=decimal_sep,
                    thousands_sep=thousands_sep,
                    currency_symbol=currency_symbol,
                )

        # ASCII encoding for clients that can't handle UTF-8
        # (Looking at you, LegacyFin. See the ACME_001 comment block above.)
        if config.get("strip_non_ascii"):
            formatted["event_name"] = self._strip_to_ascii(formatted["event_name"])
            formatted["company_name"] = self._strip_to_ascii(formatted["company_name"])
            formatted["company_address"] = self._strip_to_ascii(
                formatted["company_address"]
            )
            if formatted.get("line_items"):
                for item in formatted["line_items"]:
                    item["description"] = self._strip_to_ascii(item["description"])

        formatted["_client_config"] = config
        return formatted

    def _format_amount(
        self,
        amount_cents: int,
        decimal_sep: str = ".",
        thousands_sep: str = ",",
        currency_symbol: str = "$",
    ) -> str:
        """
        Format a cent amount as a display string with configurable separators.

        Examples:
            Standard:  123456 -> "$1,234.56"
            Acme Corp: 123456 -> "1.234,56"  (no symbol, European format)
        """
        whole = amount_cents // 100
        fraction = amount_cents % 100

        # Build whole part with thousands separator
        whole_str = ""
        whole_abs = abs(whole)
        if whole_abs == 0:
            whole_str = "0"
        else:
            groups = []
            while whole_abs > 0:
                groups.append(str(whole_abs % 1000))
                whole_abs //= 1000
            groups.reverse()
            groups[0] = groups[0]  # no leading zeros on first group
            for i in range(1, len(groups)):
                groups[i] = groups[i].zfill(3)
            whole_str = thousands_sep.join(groups)

        if whole < 0:
            whole_str = "-" + whole_str

        amount_str = f"{whole_str}{decimal_sep}{fraction:02d}"

        if currency_symbol:
            return f"{currency_symbol}{amount_str}"
        return amount_str

    def _strip_to_ascii(self, text: str) -> str:
        """
        Remove non-ASCII characters from text, attempting transliteration first.

        We use NFKD normalization to decompose accented characters into their
        base + combining form, then strip the combining marks. Characters that
        can't be decomposed (emoji, CJK, etc.) are replaced with '?'.

        This is specifically for clients like Acme Corp whose ERP systems
        crash on non-ASCII input. See the ACME_001 comment block for context.
        """
        # First pass: NFKD normalization to decompose accented chars
        normalized = unicodedata.normalize("NFKD", text)
        # Keep only ASCII characters
        ascii_bytes = normalized.encode("ascii", errors="replace")
        return ascii_bytes.decode("ascii")

    def _generate_invoice_number(self, user_id: str, event_id: str) -> str:
        """Generate a unique, human-readable invoice number."""
        now = datetime.now(timezone.utc)
        date_part = now.strftime("%Y%m%d")
        time_part = now.strftime("%H%M%S")
        # Take last 4 chars of user_id for brevity
        user_suffix = user_id[-4:] if len(user_id) >= 4 else user_id
        return f"INV-{date_part}-{time_part}-{user_suffix}"

    def _render_pdf(self, invoice_data: Dict[str, Any]) -> bytes:
        """
        Render invoice data to a PDF byte stream.

        Uses the weasyprint library for HTML-to-PDF conversion. The template
        is in templates/invoice_standard.html (or invoice_enterprise.html for
        clients with custom configs).
        """
        from jinja2 import Environment, FileSystemLoader

        template_dir = "templates"
        env = Environment(loader=FileSystemLoader(template_dir))

        if invoice_data.get("_client_config"):
            template_name = "invoice_enterprise.html"
        else:
            template_name = "invoice_standard.html"

        template = env.get_template(template_name)
        html_content = template.render(**invoice_data)

        # weasyprint for PDF generation
        import weasyprint
        pdf_bytes = weasyprint.HTML(string=html_content).write_pdf()
        return pdf_bytes

    def _upload_to_storage(self, key: str, data: bytes) -> str:
        """Upload PDF to cloud storage and return a download URL."""
        if self._storage is None:
            logger.warning("No storage client configured — skipping upload")
            return f"https://storage.eventpulse.io/{INVOICE_STORAGE_BUCKET}/{key}"

        self._storage.upload_file(
            bucket=INVOICE_STORAGE_BUCKET,
            key=key,
            data=data,
            content_type="application/pdf",
        )
        return self._storage.generate_presigned_url(
            bucket=INVOICE_STORAGE_BUCKET,
            key=key,
            expires_in=3600,
        )
