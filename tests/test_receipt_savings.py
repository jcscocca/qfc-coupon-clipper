from dataclasses import replace

import pytest

import qfc_receipt_savings as receipt_importer
from qfc_receipt_savings import (
    is_login_page,
    missing_purchases_error,
    select_purchase,
    wait_for_purchases,
)
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


def test_login_page_detection_supports_current_kroger_identity_url():
    class Page:
        url = "https://login.kroger.com/eciamp.onmicrosoft.com/oauth2/authorize"

    assert is_login_page(Page()) is True


def test_receipt_wait_returns_immediately_for_noninteractive_login(monkeypatch):
    class Page:
        url = "https://login.kroger.com/signin"

    monkeypatch.setattr(receipt_importer, "discover_purchase_urls", lambda page: [])
    sleeps = []
    monkeypatch.setattr(receipt_importer.time, "sleep", sleeps.append)
    monkeypatch.setattr(receipt_importer.time, "monotonic", lambda: 0.0)

    assert wait_for_purchases(
        Page(), timeout=10, allow_interactive_login=False) == []
    assert sleeps == []


def test_receipt_wait_prompts_once_then_continues_after_login(monkeypatch, capsys):
    class Page:
        url = "https://login.kroger.com/signin"

    results = iter([[], [("2026-08-09", "https://www.qfc.com/detail")]])
    monkeypatch.setattr(
        receipt_importer, "discover_purchase_urls", lambda page: next(results))
    monkeypatch.setattr(receipt_importer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(receipt_importer.time, "monotonic", lambda: 0.0)

    purchases = wait_for_purchases(Page(), timeout=10)

    assert purchases[0][0] == "2026-08-09"
    assert capsys.readouterr().out.count("requires a fresh sign-in") == 1


def test_missing_purchases_error_distinguishes_incomplete_authentication():
    class LoginPage:
        url = "https://login.kroger.com/signin"

    class PurchasesPage:
        url = "https://www.qfc.com/mypurchases"

    assert "reauthentication was not completed" in str(
        missing_purchases_error(LoginPage()))
    assert "ledger was not updated" in str(missing_purchases_error(LoginPage()))
    assert str(missing_purchases_error(PurchasesPage())) == (
        "no purchases were found on My Purchases")


def test_receipt_wait_keeps_polling_through_login_navigation(monkeypatch):
    from playwright.sync_api import Error as PWError

    class Page:
        url = "https://login.kroger.com/signin"

        def is_closed(self):
            return False

    def discover(page):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    results = iter([
        PWError("Locator.evaluate_all: Execution context was destroyed, "
                "most likely because of a navigation"),
        [("2026-09-16", "https://www.qfc.com/detail")],
    ])
    monkeypatch.setattr(receipt_importer, "discover_purchase_urls", discover)
    monkeypatch.setattr(receipt_importer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(receipt_importer.time, "monotonic", lambda: 0.0)

    assert wait_for_purchases(Page(), timeout=10)[0][0] == "2026-09-16"


def test_receipt_wait_stops_when_browser_is_closed(monkeypatch):
    from playwright.sync_api import Error as PWError

    class Page:
        url = "https://login.kroger.com/signin"

        def is_closed(self):
            return True

    def discover(page):
        raise PWError("Target page, context or browser has been closed")

    monkeypatch.setattr(receipt_importer, "discover_purchase_urls", discover)
    sleeps = []
    monkeypatch.setattr(receipt_importer.time, "sleep", sleeps.append)
    monkeypatch.setattr(receipt_importer.time, "monotonic", lambda: 0.0)

    with pytest.raises(PWError):
        wait_for_purchases(Page(), timeout=10)
    assert sleeps == []


@pytest.mark.parametrize("label, expected", [
    ("July 20, 2026", "2026-07-20"),
    ("Aug. 30, 2026", "2026-08-30"),
    ("Sept. 16, 2026", "2026-09-16"),
    ("Dec. 1, 2026", "2026-12-01"),
    ("September 16, 2026", "2026-09-16"),
    ("Sept.16,2026", "2026-09-16"),
    ("Sep.16,2026", "2026-09-16"),
    ("July20,2026", "2026-07-20"),
])
def test_parse_receipt_accepts_abbreviated_order_dates(label, expected):
    text = RECEIPT_TEXT.replace("Order Date: July 20, 2026", f"Order Date: {label}")
    assert parse_receipt_text(text).order_date == expected


@pytest.mark.parametrize("label", ["Sept.31,2026", "Unknown.16,2026"])
def test_parse_receipt_rejects_invalid_compact_dates(label):
    text = RECEIPT_TEXT.replace("July 20, 2026", label)
    with pytest.raises(ReceiptParseError, match="unrecognized order date"):
        parse_receipt_text(text)


def test_repeated_import_with_compact_date_does_not_double_count(tmp_path):
    path = tmp_path / "receipt_savings.json"
    save_receipt(path, parse_receipt_text(RECEIPT_TEXT))
    compact = RECEIPT_TEXT.replace("July 20, 2026", "Jul.20,2026")
    ledger, was_new = save_receipt(path, parse_receipt_text(compact))
    assert was_new is False
    assert len(ledger["receipts"]) == 1
    assert ledger_total_savings_cents(load_ledger(path)) == 2349
