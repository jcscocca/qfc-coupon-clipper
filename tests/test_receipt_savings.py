from dataclasses import replace

import pytest

from qfc_receipt_savings import select_purchase
from receipt_savings import (
    ReceiptParseError,
    format_indicator,
    ledger_total_savings_cents,
    load_ledger,
    money_to_cents,
    parse_receipt_text,
    save_receipt,
)


RECEIPT_TEXT = """
Order Type: In Store
Order Date: July 20, 2026
Order Number: store~terminal~2026-07-20~lane~transaction
Total Savings: $23.49
Order Summary
Original Item Total
$82.16
Item Coupons/Sales
-$23.49
Other Fees
+$0.16
Sales Tax
+$0.99
Order Total
$59.82
Item Details
14 Items
"""


def test_money_to_cents_supports_sign_and_commas():
    assert money_to_cents("-$23.49") == -2349
    assert money_to_cents("+$0.16") == 16
    assert money_to_cents("$1,234.56") == 123456


def test_parse_receipt_text_and_reconcile():
    receipt = parse_receipt_text(RECEIPT_TEXT, "https://example.test/receipt")
    assert receipt.order_date == "2026-07-20"
    assert receipt.savings_cents == 2349
    assert receipt.order_total_cents == 5982
    assert receipt.item_count == 14
    assert receipt.savings_rate == pytest.approx(28.5893, rel=1e-4)


def test_rejects_summary_that_does_not_reconcile():
    broken = RECEIPT_TEXT.replace("$59.82", "$60.82")
    with pytest.raises(ReceiptParseError, match="does not reconcile"):
        parse_receipt_text(broken)


def test_rejects_disagreement_between_two_savings_totals():
    broken = RECEIPT_TEXT.replace("Item Coupons/Sales\n-$23.49", "Item Coupons/Sales\n-$22.49")
    with pytest.raises(ReceiptParseError, match="disagrees"):
        parse_receipt_text(broken)


def test_ledger_upsert_is_idempotent(tmp_path):
    path = tmp_path / "receipt_savings.json"
    receipt = parse_receipt_text(RECEIPT_TEXT)
    ledger, was_new = save_receipt(path, receipt)
    assert was_new is True
    assert ledger_total_savings_cents(ledger) == 2349

    updated = replace(receipt, source_url="https://example.test/updated")
    ledger, was_new = save_receipt(path, updated)
    assert was_new is False
    assert len(ledger["receipts"]) == 1
    assert ledger_total_savings_cents(ledger) == 2349
    assert load_ledger(path)["receipts"][0]["source_url"].endswith("updated")


def test_indicator_states_verified_scope(tmp_path):
    receipt = parse_receipt_text(RECEIPT_TEXT)
    ledger, was_new = save_receipt(tmp_path / "ledger.json", receipt)
    text = format_indicator(receipt, ledger, was_new)
    assert "Verified receipt savings: $23.49 (28.6%" in text
    assert "all QFC promotions, coupons, and sale pricing" in text


def test_select_purchase_defaults_to_latest_and_accepts_date():
    purchases = [
        ("2026-07-20", "https://example.test/today"),
        ("2026-07-05", "https://example.test/older"),
    ]
    assert select_purchase(purchases, None) == purchases[0]
    assert select_purchase(purchases, "2026-07-05") == purchases[1]
    with pytest.raises(ReceiptParseError, match="no purchase"):
        select_purchase(purchases, "2026-07-01")
