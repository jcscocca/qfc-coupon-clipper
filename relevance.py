"""Pure (no-browser) logic for relevance-based coupon selection.

Imported by qfc_coupon_clipper.py and exercised directly by test_relevance.py.
Deliberately free of any Playwright import so it runs anywhere.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- value parsing --------------------------------------------------------

_AMOUNT_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
_DOLLAR_RE = re.compile(
    rf"(?:\bsave\s+\$\s*(?P<save>{_AMOUNT_PATTERN})"
    rf"|\$\s*(?P<offer>{_AMOUNT_PATTERN})\s*(?:off|coupon)\b"
    rf"|\bup\s+to\s+\$\s*(?P<cap>{_AMOUNT_PATTERN}))",
    re.I,
)
_PERCENT_RE = re.compile(
    r"(?:\bsave\s+)?(\d+(?:\.\d+)?)\s*%\s*(?:off|on)\b",
    re.I,
)
_PRICE_RE = re.compile(rf"\$\s*({_AMOUNT_PATTERN})")
_BOGO_RE = re.compile(
    r"\b(?:b1g1|bogo|buy\s+(?:one|\d+)\b.{0,120}?\bget\s+"
    r"(?:one|\d+)\s+free|buy\s+one\s+get\s+one)\b"
)


@dataclass
class Estimates:
    """Assumed dollar values for coupons without an explicit '$X off'."""
    bogo: float = 5.00
    assumed_item_price: float = 4.00
    unknown: float = 1.00


@dataclass
class Savings:
    value: float          # comparable dollar figure used for ranking
    kind: str             # "dollar" | "bogo" | "percent" | "price" | "unknown"
    estimated: bool


def parse_savings(text: str | None, estimates: Estimates) -> Savings:
    """Map a coupon label / tile text to a comparable dollar Savings.

    First match wins: explicit dollar savings > BOGO > percent > fixed price >
    unknown.
    """
    t = (text or "").lower()
    m = _DOLLAR_RE.search(t)
    if m:
        amount = m.group("save") or m.group("offer") or m.group("cap")
        return Savings(value=float(amount.replace(",", "")), kind="dollar", estimated=False)
    if _BOGO_RE.search(t):
        return Savings(value=estimates.bogo, kind="bogo", estimated=True)
    m = _PERCENT_RE.search(t)
    if m:
        pct = float(m.group(1))
        return Savings(
            value=estimates.assumed_item_price * pct / 100.0,
            kind="percent",
            estimated=True,
        )
    if _PRICE_RE.search(t):
        # A label such as "$2.99 QFC Butter" is the resulting sale price, not
        # $2.99 of savings. Without the regular price, use the neutral fallback
        # estimate rather than promoting it above known dollar-off coupons.
        return Savings(value=estimates.unknown, kind="price", estimated=True)
    return Savings(value=estimates.unknown, kind="unknown", estimated=True)


@dataclass
class Candidate:
    label: str
    savings: Savings
    locator: Any = None   # Playwright locator at runtime; None in unit tests


def rank_candidates(candidates: list[Candidate], min_savings: float = 0.0,
                    include_nondollar: bool = True) -> list[Candidate]:
    """Filter by non-dollar policy + floor, then sort by value descending.

    Python's sort is stable, so equal-value coupons keep their input order.
    """
    out = []
    for c in candidates:
        if not include_nondollar and c.savings.kind != "dollar":
            continue
        if c.savings.value < min_savings:
            continue
        out.append(c)
    out.sort(key=lambda c: c.savings.value, reverse=True)
    return out


def filter_excluded(candidates: list[Candidate], terms: list[str]) -> list[Candidate]:
    """Drop candidates whose label contains any excluded term (case-insensitive).

    Applied before ranking so exclusions hold in every phase, including the
    fill phase that otherwise preserves QFC's own ordering.
    """
    return [c for c in candidates if matching_term(c.label, terms) is None]


def matching_term(label: str, terms: list[str]) -> str | None:
    """Return the first excluded term found in `label`, or None."""
    lowered_label = label.lower()
    for term in terms:
        term = (term or "").strip().lower()
        if term and term in lowered_label:
            return term
    return None


def match_departments(wanted: list[str], available: list[str]) -> tuple[list[str], list[str]]:
    """Match wanted department names against the panel's available names.

    Case-insensitive and whitespace-trimmed. Returns (matched, missing):
    matched uses the panel's canonical spelling; missing uses the input spelling.
    """
    avail_norm = {a.strip().lower(): a for a in available}
    matched, missing = [], []
    for w in wanted:
        canonical = avail_norm.get(w.strip().lower())
        if canonical is not None:
            matched.append(canonical)
        else:
            missing.append(w)
    return matched, missing


@dataclass
class Config:
    departments: list = field(default_factory=list)
    max_clips: int = 249
    min_savings: float = 0.0
    include_nondollar: bool = True
    fill_to_limit: bool = False
    exclude_terms: list = field(default_factory=list)
    estimates: Estimates = field(default_factory=Estimates)


def load_config(path: "str | Path | None", overrides: "dict | None" = None) -> Config:
    """Load config from a TOML file (if it exists) then apply CLI overrides.

    A missing path/file is NOT an error — it yields defaults (legacy behavior).
    """
    data = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    est = data.get("estimates", {})
    exclude_terms = data.get("exclude_terms", [])
    if isinstance(exclude_terms, str):
        exclude_terms = [exclude_terms]
    cfg = Config(
        departments=list(data.get("departments", [])),
        max_clips=int(data.get("max_clips", 249)),
        min_savings=float(data.get("min_savings", 0.0)),
        include_nondollar=bool(data.get("include_nondollar", True)),
        fill_to_limit=bool(data.get("fill_to_limit", False)),
        exclude_terms=list(exclude_terms),
        estimates=Estimates(
            bogo=float(est.get("bogo", 5.0)),
            assumed_item_price=float(est.get("assumed_item_price", 4.0)),
            unknown=float(est.get("unknown", 1.0)),
        ),
    )

    for key in ("departments", "max_clips", "min_savings"):
        if overrides and overrides.get(key) is not None:
            setattr(cfg, key, overrides[key])
    return cfg
