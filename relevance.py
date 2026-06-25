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

_DOLLAR_RE = re.compile(r"\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*off")
_BOGO_RE = re.compile(
    r"\b(?:b1g1|bogo|buy\s+\w+\s+get\s+\w+\s+free|buy\s+one\s+get\s+one)\b"
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
    kind: str             # "dollar" | "bogo" | "percent" | "unknown"
    estimated: bool


def parse_savings(text: str | None, estimates: Estimates) -> Savings:
    """Map a coupon label / tile text to a comparable dollar Savings.

    First match wins: explicit dollar > BOGO > percent > unknown.
    """
    t = (text or "").lower()
    m = _DOLLAR_RE.search(t)
    if m:
        return Savings(value=float(m.group(1).replace(",", "")), kind="dollar", estimated=False)
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
    max_clips: int = 250
    min_savings: float = 0.0
    include_nondollar: bool = True
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
    cfg = Config(
        departments=list(data.get("departments", [])),
        max_clips=int(data.get("max_clips", 250)),
        min_savings=float(data.get("min_savings", 0.0)),
        include_nondollar=bool(data.get("include_nondollar", True)),
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
