#!/usr/bin/env python3
"""Import verified savings from QFC's signed-in My Purchases receipt page."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from receipt_savings import (
    ReceiptParseError,
    format_indicator,
    parse_receipt_text,
    save_receipt,
)


BASE_URL = "https://www.qfc.com"
PURCHASES_URL = f"{BASE_URL}/mypurchases"
PROFILE_DIR = Path.home() / ".qfc_clipper_profile"
DEFAULT_LEDGER = Path(__file__).parent / "data" / "receipt_savings.json"
_PURCHASE_DATE_RE = re.compile(r"/mypurchases/detail/[^~]+~[^~]+~(\d{4}-\d{2}-\d{2})~")


def is_login_page(page) -> bool:
    """Return whether QFC redirected My Purchases to its identity provider."""
    url = (getattr(page, "url", "") or "").lower()
    return "login.kroger.com" in url or "/signin" in url


def discover_purchase_urls(page) -> list[tuple[str, str]]:
    """Return unique ``(ISO date, absolute detail URL)`` pairs from the page."""
    hrefs = page.locator('a[href^="/mypurchases/detail/"]').evaluate_all(
        "elements => elements.map(element => element.getAttribute('href'))"
    )
    purchases = {}
    for href in hrefs:
        match = _PURCHASE_DATE_RE.search(href or "")
        if match:
            purchases[urljoin(BASE_URL, href)] = match.group(1)
    return sorted(((when, url) for url, when in purchases.items()), reverse=True)


def wait_for_purchases(
    page, *, timeout: float = 180.0, allow_interactive_login: bool = True
) -> list[tuple[str, str]]:
    deadline = time.monotonic() + timeout
    prompted = False
    while time.monotonic() < deadline:
        purchases = discover_purchase_urls(page)
        if purchases:
            return purchases
        if is_login_page(page) and not allow_interactive_login:
            return []
        if not prompted and is_login_page(page):
            print(
                "QFC My Purchases requires a fresh sign-in even though coupon "
                "access may still work. Sign in in the opened browser; the "
                "receipt import will continue automatically."
            )
            prompted = True
        time.sleep(2)
    return []


def missing_purchases_error(page) -> ReceiptParseError:
    """Explain whether purchases are absent or authentication was incomplete."""
    if is_login_page(page):
        return ReceiptParseError(
            "QFC My Purchases reauthentication was not completed; the receipt "
            "ledger was not updated. Run qfc_receipt_savings.py interactively "
            "and sign in in the opened browser."
        )
    return ReceiptParseError("no purchases were found on My Purchases")


def select_purchase(
    purchases: list[tuple[str, str]], requested_date: str | None
) -> tuple[str, str]:
    if not purchases:
        raise ReceiptParseError("no purchases were found on My Purchases")
    if requested_date is None:
        return purchases[0]
    for purchase in purchases:
        if purchase[0] == requested_date:
            return purchase
    raise ReceiptParseError(f"no purchase was found for {requested_date}")


def receipt_url_from_detail(page) -> str:
    link = page.get_by_role("link", name="View Receipt", exact=True)
    try:
        link.wait_for(state="visible", timeout=30000)
    except PWTimeout as exc:
        raise ReceiptParseError("purchase detail did not expose a View Receipt link") from exc
    href = link.get_attribute("href")
    if not href:
        raise ReceiptParseError("View Receipt link had no destination")
    return urljoin(BASE_URL, href)


def import_receipt(page, detail_url: str):
    page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("heading", name="Purchase Details", exact=True).wait_for(
        state="visible", timeout=30000
    )
    receipt_url = receipt_url_from_detail(page)
    page.goto(receipt_url, wait_until="domcontentloaded", timeout=60000)
    savings = page.get_by_text(re.compile(r"^Total Savings: \$"))
    savings.wait_for(state="visible", timeout=30000)
    main_text = page.locator("main").inner_text(timeout=30000)
    return parse_receipt_text(main_text, source_url=receipt_url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record actual receipt savings from QFC My Purchases."
    )
    parser.add_argument(
        "--date",
        help="purchase date in YYYY-MM-DD form (default: latest purchase)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_LEDGER,
        help=f"local JSON ledger (default: {DEFAULT_LEDGER})",
    )
    parser.add_argument(
        "--no-wait-login",
        action="store_true",
        help="exit instead of waiting for an interactive QFC sign-in",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without a visible browser (works only while the saved session is valid)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.date:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
                raise ValueError
            date.fromisoformat(args.date)
        except ValueError:
            print("--date must use YYYY-MM-DD", file=sys.stderr)
            return 2

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=args.headless,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(PURCHASES_URL, wait_until="domcontentloaded", timeout=60000)
            timeout = 10.0 if args.no_wait_login else 180.0
            purchases = wait_for_purchases(
                page, timeout=timeout,
                allow_interactive_login=not args.no_wait_login)
            if not purchases:
                raise missing_purchases_error(page)
            _, detail_url = select_purchase(purchases, args.date)
            receipt = import_receipt(page, detail_url)
            ledger, was_new = save_receipt(args.output, receipt)
            print(format_indicator(receipt, ledger, was_new))
            context.close()
            return 0
    except PWTimeout as exc:
        print(f"QFC page timed out: {exc}", file=sys.stderr)
        return 2
    except ReceiptParseError as exc:
        print(f"Could not import receipt savings: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
