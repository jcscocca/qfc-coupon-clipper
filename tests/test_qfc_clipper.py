"""Regression tests for the pure selector helpers in qfc_coupon_clipper.

These guard the text-based clip/clipped detection — the part most likely to
silently break if QFC changes its button wording. They import the module
directly (no browser is launched: the Playwright work lives under main()).

Run with:  pytest
"""

import pytest

import qfc_coupon_clipper as clipper


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


# --- wait_until_ready -------------------------------------------------------

def _patch_ready(monkeypatch, scan_results, logged_out=False):
    """Drive wait_until_ready with canned scan_coupon_buttons results.

    scan_results: list of (n_clip, n_clipped) tuples returned in order; the
    last entry repeats if the loop polls more times than provided.
    """
    state = {"i": 0}

    def fake_scan(page):
        i = min(state["i"], len(scan_results) - 1)
        state["i"] += 1
        return scan_results[i]

    monkeypatch.setattr(clipper, "scan_coupon_buttons", fake_scan)
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "detect_logged_out", lambda page: logged_out)
    monkeypatch.setattr(clipper.time, "sleep", lambda s: None)


def test_wait_until_ready_immediate(monkeypatch, capsys):
    _patch_ready(monkeypatch, [(3, 0)])
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is True
    assert "Sign in" not in capsys.readouterr().out


def test_wait_until_ready_after_login(monkeypatch, capsys):
    # First poll: nothing visible + logged out. Second poll: coupons appear.
    _patch_ready(monkeypatch, [(0, 0), (1, 0)], logged_out=True)
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is True
    assert capsys.readouterr().out.count("Sign in to QFC") == 1


def test_wait_until_ready_timeout(monkeypatch, capsys):
    _patch_ready(monkeypatch, [(0, 0)], logged_out=False)
    times = iter([0.0, 999.0, 999.0])
    monkeypatch.setattr(clipper.time, "monotonic", lambda: next(times))
    assert clipper.wait_until_ready(page=None, timeout=180, poll=0) is False
    assert "Sign in" not in capsys.readouterr().out


# --- _clip_relevant: never double-click the same coupon ---------------------

class _FakeBtn:
    """Stand-in for a Playwright locator that counts how often it's clicked."""
    def __init__(self):
        self.clicks = 0

    def is_visible(self):
        return True

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        self.clicks += 1


class _NoText:
    def count(self):
        return 0


class _FakePage:
    """Only needs get_by_text for _clip_relevant's limit-message safety net."""
    def get_by_text(self, pattern):
        return _NoText()


def test_clip_relevant_never_double_clicks(monkeypatch):
    from types import SimpleNamespace
    from relevance import Candidate, Savings

    btns = [_FakeBtn() for _ in range(3)]
    cands = [
        Candidate(label=f"Clip for coupon: item {i}",
                  savings=Savings(value=float(3 - i), kind="dollar", estimated=False),
                  locator=btns[i])
        for i in range(3)
    ]
    # Worst case: every pass re-collects the same coupons (their labels never
    # flip to "Unclip"), which is exactly what triggered the duplicate clicks.
    monkeypatch.setattr(clipper, "collect_candidates",
                        lambda page, estimates, debug=False: list(cands))
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    cfg = SimpleNamespace(estimates=None, min_savings=0.0, include_nondollar=True)
    args = SimpleNamespace(dry_run=False, debug=False, max=0, min_delay=0, max_delay=0)

    rc = clipper._clip_relevant(_FakePage(), cfg, budget=5, args=args)

    assert rc == 0
    # budget is 5 and collection repeats, yet each unique coupon is clicked once.
    assert [b.clicks for b in btns] == [1, 1, 1]
