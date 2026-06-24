"""Regression tests for the pure selector helpers in qfc_coupon_clipper.

These guard the text-based clip/clipped detection — the part most likely to
silently break if QFC changes its button wording. They import the module
directly (no browser is launched: the Playwright work lives under main()).

Run with:  pytest jobs/qfc_clipper
"""

import sys
from pathlib import Path

# Make the job's module importable no matter where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

import qfc_coupon_clipper as clipper  # noqa: E402


# --- looks_clipped ---------------------------------------------------------

CLIPPED_LABELS = [
    "Clipped",
    "Unclip",
    "Unclip coupon: $1.00 off eggs",
    "Added",
    "Remove coupon: 2x Milk",
    "ADDED TO CARD",          # case-insensitive
    "You clipped this offer",
]

NOT_CLIPPED_LABELS = [
    "Clip for coupon: $1.00 off eggs",
    "Add to card",
    "Add coupon",
    "Sign In",
    "View more info",
    "",
    None,
]


@pytest.mark.parametrize("label", CLIPPED_LABELS)
def test_looks_clipped_true(label):
    assert clipper.looks_clipped(label) is True


@pytest.mark.parametrize("label", NOT_CLIPPED_LABELS)
def test_looks_clipped_false(label):
    assert clipper.looks_clipped(label) is False


# --- looks_clippable -------------------------------------------------------

CLIPPABLE_LABELS = [
    "Clip for coupon: $1.00 off eggs",
    "clip",
    "Add coupon",
    "Load coupon",
    "Add to card",
    "CLIP FOR COUPON: BREAD",  # case-insensitive
]

NOT_CLIPPABLE_LABELS = [
    "Unclip coupon: $1.00 off eggs",  # already clipped -> not clippable
    "Unclip",
    "Clipped",
    "Added",
    "Remove coupon: 2x Milk",
    "Sign In",
    "View more info",
    "",
    None,
]


@pytest.mark.parametrize("label", CLIPPABLE_LABELS)
def test_looks_clippable_true(label):
    assert clipper.looks_clippable(label) is True


@pytest.mark.parametrize("label", NOT_CLIPPABLE_LABELS)
def test_looks_clippable_false(label):
    assert clipper.looks_clippable(label) is False


# --- the critical regression: the "clip" inside "unclip" trap --------------

def test_unclip_is_never_clippable():
    """'Unclip ...' contains the substring 'clip' but is an ALREADY-clipped
    coupon. The clipped check must win so we never re-toggle a clipped coupon."""
    for label in ["Unclip", "Unclip coupon: eggs", "UNCLIP FOR COUPON: X"]:
        assert clipper.looks_clipped(label) is True
        assert clipper.looks_clippable(label) is False


def test_clipped_implies_not_clippable():
    """Invariant: anything that looks clipped must never look clippable."""
    for label in CLIPPED_LABELS + CLIPPABLE_LABELS + NOT_CLIPPABLE_LABELS:
        if clipper.looks_clipped(label):
            assert not clipper.looks_clippable(label)
