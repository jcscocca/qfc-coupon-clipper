"""Pure receipt parsing and persistence for QFC purchase-history savings.

QFC's online receipt reports a single ``Total Savings`` / ``Item Coupons/Sales``
amount.  It does not distinguish digital coupons from weekly sales, BOGO offers,
or other promotions, so this module deliberately calls the metric *receipt
savings*, not savings caused by this clipper.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


SCHEMA_VERSION = 1
SAVINGS_SCOPE = "all QFC promotions, coupons, and sale pricing shown on the receipt"
_MONEY_TOKEN = r"[+-]?\$[\d,]+(?:\.\d{2})?"


class ReceiptParseError(ValueError):
    """Raised when a purchase-history receipt is missing or contradicts itself."""


def money_to_cents(value: str) -> int:
    """Convert a QFC money label such as ``-$23.49`` to signed cents."""
    cleaned = value.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReceiptParseError(f"invalid money value: {value!r}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _inline_value(text: str, label: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(label)}:\s*(.+?)\s*$", text)
    if not match:
        raise ReceiptParseError(f"receipt is missing {label!r}")
    return match.group(1).strip()


def _money_after_label(text: str, label: str) -> int:
    match = re.search(
        rf"(?m)^\s*{re.escape(label)}\s*$\s*^\s*({_MONEY_TOKEN})\s*$",
        text,
    )
    if not match:
        raise ReceiptParseError(f"receipt is missing summary value {label!r}")
    return money_to_cents(match.group(1))


def _inline_money(text: str, label: str) -> int:
    match = re.search(
        rf"(?m)^\s*{re.escape(label)}:\s*({_MONEY_TOKEN})\s*$", text
    )
    if not match:
        raise ReceiptParseError(f"receipt is missing {label!r}")
    return money_to_cents(match.group(1))


@dataclass(frozen=True)
class ReceiptSavings:
    order_number: str
    order_date: str
    order_type: str
    source_url: str
    original_item_total_cents: int
    savings_cents: int
    other_fees_cents: int
    tax_cents: int
    order_total_cents: int
    item_count: int | None = None
    scope: str = SAVINGS_SCOPE

    @property
    def savings_rate(self) -> float:
        if not self.original_item_total_cents:
            return 0.0
        return self.savings_cents / self.original_item_total_cents * 100.0

    def validate(self) -> None:
        if self.savings_cents < 0:
            raise ReceiptParseError("savings must be stored as a positive amount")
        expected = (
            self.original_item_total_cents
            - self.savings_cents
            + self.other_fees_cents
            + self.tax_cents
        )
        if abs(expected - self.order_total_cents) > 1:
            raise ReceiptParseError(
                "receipt summary does not reconcile: "
                f"{format_cents(self.original_item_total_cents)} - "
                f"{format_cents(self.savings_cents)} + "
                f"{format_cents(self.other_fees_cents)} + "
                f"{format_cents(self.tax_cents)} != "
                f"{format_cents(self.order_total_cents)}"
            )


def parse_receipt_text(text: str, source_url: str = "") -> ReceiptSavings:
    """Parse the rendered text from a QFC ``/mypurchases/image/...`` page."""
    if not text or not text.strip():
        raise ReceiptParseError("receipt text is empty")

    date_label = _inline_value(text, "Order Date")
    # QFC uses AP-style abbreviations ("Aug. 30", "Sept. 16") for some months.
    normalized = date_label.replace(".", "").replace("Sept ", "Sep ")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            order_date = datetime.strptime(normalized, fmt).date().isoformat()
            break
        except ValueError:
            continue
    else:
        raise ReceiptParseError(f"unrecognized order date: {date_label!r}")

    savings = abs(_inline_money(text, "Total Savings"))
    item_coupon_sales = abs(_money_after_label(text, "Item Coupons/Sales"))
    if abs(savings - item_coupon_sales) > 1:
        raise ReceiptParseError(
            "Total Savings disagrees with Item Coupons/Sales: "
            f"{format_cents(savings)} vs {format_cents(item_coupon_sales)}"
        )

    item_count_match = re.search(r"(?m)^\s*(\d+)\s+Items\s*$", text)
    receipt = ReceiptSavings(
        order_number=_inline_value(text, "Order Number"),
        order_date=order_date,
        order_type=_inline_value(text, "Order Type"),
        source_url=source_url,
        original_item_total_cents=_money_after_label(text, "Original Item Total"),
        savings_cents=savings,
        other_fees_cents=_money_after_label(text, "Other Fees"),
        tax_cents=_money_after_label(text, "Sales Tax"),
        order_total_cents=_money_after_label(text, "Order Total"),
        item_count=int(item_count_match.group(1)) if item_count_match else None,
    )
    receipt.validate()
    return receipt


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "receipts": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptParseError(f"could not read savings ledger {path}: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ReceiptParseError(
            f"unsupported savings ledger schema: {data.get('schema_version')!r}"
        )
    if not isinstance(data.get("receipts"), list):
        raise ReceiptParseError("savings ledger receipts must be a list")
    return data


def save_receipt(path: Path, receipt: ReceiptSavings) -> tuple[dict, bool]:
    """Insert or update one receipt atomically; return ``(ledger, was_new)``."""
    ledger = load_ledger(path)
    receipts = ledger["receipts"]
    encoded = asdict(receipt)
    was_new = True
    for index, existing in enumerate(receipts):
        if existing.get("order_number") == receipt.order_number:
            receipts[index] = encoded
            was_new = False
            break
    if was_new:
        receipts.append(encoded)
    receipts.sort(key=lambda item: (item.get("order_date", ""), item.get("order_number", "")))
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)
    return ledger, was_new


def ledger_total_savings_cents(ledger: dict) -> int:
    return sum(int(item["savings_cents"]) for item in ledger.get("receipts", []))


def format_indicator(receipt: ReceiptSavings, ledger: dict, was_new: bool) -> str:
    action = "Recorded" if was_new else "Updated"
    cumulative = ledger_total_savings_cents(ledger)
    return "\n".join(
        [
            f"{action} receipt {receipt.order_date} ({receipt.order_type})",
            f"Paid: {format_cents(receipt.order_total_cents)}",
            (
                f"Verified receipt savings: {format_cents(receipt.savings_cents)} "
                f"({receipt.savings_rate:.1f}% of original item total)"
            ),
            (
                f"Ledger: {len(ledger.get('receipts', []))} receipt(s), "
                f"{format_cents(cumulative)} cumulative verified receipt savings"
            ),
            f"Scope: {receipt.scope}.",
        ]
    )
