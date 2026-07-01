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
    monkeypatch.setattr(clipper, "scroll_to_load_all",
                        lambda page, debug=False: None)
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    cfg = SimpleNamespace(estimates=None, min_savings=0.0, include_nondollar=True)
    args = SimpleNamespace(dry_run=False, debug=False, max=0, min_delay=0, max_delay=0)

    result = clipper._clip_relevant(_FakePage(), cfg, budget=5, args=args)

    assert result.clipped == 3
    assert result.exhausted is True
    assert result.limit_hit is False
    # budget is 5 and collection repeats, yet each unique coupon is clicked once.
    assert [b.clicks for b in btns] == [1, 1, 1]


def test_clip_relevant_rescans_once_before_declaring_exhaustion(monkeypatch):
    from types import SimpleNamespace
    from relevance import Candidate, Savings

    first = Candidate("Clip for coupon: first", Savings(2.0, "dollar", False),
                      _FakeBtn())
    later = Candidate("Clip for coupon: later", Savings(1.0, "dollar", False),
                      _FakeBtn())
    state = {"rescanned": False, "scrolls": 0}

    def fake_collect(page, estimates, debug=False):
        return [first, later] if state["rescanned"] else [first]

    def fake_scroll(page, debug=False):
        state["rescanned"] = True
        state["scrolls"] += 1

    monkeypatch.setattr(clipper, "collect_candidates", fake_collect)
    monkeypatch.setattr(clipper, "scroll_to_load_all", fake_scroll)
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    cfg = SimpleNamespace(estimates=None, min_savings=0.0, include_nondollar=True)
    args = SimpleNamespace(dry_run=False, debug=False, max=0, min_delay=0, max_delay=0)

    result = clipper._clip_relevant(_FakePage(), cfg, budget=3, args=args)

    assert result.clipped == 2
    assert result.exhausted is True
    assert state["scrolls"] == 2


def test_clip_relevant_shares_attempted_labels_between_phases(monkeypatch):
    from types import SimpleNamespace
    from relevance import Candidate, Savings

    preferred = Candidate("Clip for coupon: preferred",
                          Savings(2.0, "dollar", False), _FakeBtn())
    fallback = Candidate("Clip for coupon: fallback",
                         Savings(1.0, "dollar", False), _FakeBtn())
    candidates = {"value": [preferred]}

    monkeypatch.setattr(
        clipper, "collect_candidates",
        lambda page, estimates, debug=False: list(candidates["value"]))
    monkeypatch.setattr(clipper, "scroll_to_load_all",
                        lambda page, debug=False: None)
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    cfg = SimpleNamespace(estimates=None, min_savings=0.0, include_nondollar=True)
    args = SimpleNamespace(dry_run=False, debug=False, max=0, min_delay=0, max_delay=0)
    attempted = set()

    first = clipper._clip_relevant(
        _FakePage(), cfg, budget=2, args=args, clicked_keys=attempted)
    candidates["value"] = [preferred, fallback]
    second = clipper._clip_relevant(
        _FakePage(), cfg, budget=1, args=args, clicked_keys=attempted)

    assert first.clipped == 1
    assert second.clipped == 1
    assert preferred.locator.clicks == 1
    assert fallback.locator.clicks == 1


def test_clip_relevant_dry_run_deduplicates_phase_plans(monkeypatch):
    from types import SimpleNamespace
    from relevance import Candidate, Savings

    candidates = [
        Candidate("Clip for coupon: preferred", Savings(2.0, "dollar", False)),
        Candidate("Clip for coupon: fallback", Savings(1.0, "dollar", False)),
    ]
    monkeypatch.setattr(
        clipper, "collect_candidates",
        lambda page, estimates, debug=False: list(candidates))
    monkeypatch.setattr(clipper, "dismiss_modal", lambda page, debug=False: False)

    cfg = SimpleNamespace(estimates=None, min_savings=0.0, include_nondollar=True)
    args = SimpleNamespace(dry_run=True, debug=False, min_delay=0, max_delay=0)
    attempted = set()

    preferred = clipper._clip_relevant(
        _FakePage(), cfg, budget=1, args=args, clicked_keys=attempted,
        phase="preferred")
    fill = clipper._clip_relevant(
        _FakePage(), cfg, budget=1, args=args, clicked_keys=attempted,
        phase="fill")

    assert preferred.planned == 1
    assert fill.planned == 1
    assert attempted == {candidate.label for candidate in candidates}


# --- clear_filters ----------------------------------------------------------

class _ClearButton:
    def __init__(self, visible=True, enabled=True):
        self.visible = visible
        self.enabled = enabled
        self.clicks = 0

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.clicks += 1


class _ButtonList:
    def __init__(self, buttons):
        self.buttons = buttons

    def count(self):
        return len(self.buttons)

    def nth(self, index):
        return self.buttons[index]


class _ClearPage:
    def __init__(self, buttons):
        self.buttons = buttons

    def get_by_role(self, role, name=None):
        assert (role, name) == ("button", "Clear All")
        return _ButtonList(self.buttons)


def test_clear_filters_clicks_each_visible_enabled_control(monkeypatch):
    buttons = [_ClearButton(), _ClearButton(visible=False), _ClearButton()]
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    assert clipper.clear_filters(_ClearPage(buttons)) is True
    assert [button.clicks for button in buttons] == [1, 0, 1]


# --- fill-to-limit orchestration -------------------------------------------

class _RunMouse:
    def wheel(self, x, y):
        pass


class _RunPage:
    mouse = _RunMouse()


def test_relevance_mode_counts_unfiltered_then_fills_remaining_capacity(monkeypatch):
    from types import SimpleNamespace

    events = []
    clip_calls = []
    clear_results = iter([True, True])

    monkeypatch.setattr(
        clipper, "clear_filters",
        lambda page, debug=False: events.append("clear") or next(clear_results))
    monkeypatch.setattr(
        clipper, "scroll_to_load_all",
        lambda page, debug=False: events.append("scroll"))
    monkeypatch.setattr(
        clipper, "count_clipped_total",
        lambda page, debug=False: events.append("count") or 44)
    monkeypatch.setattr(clipper, "scan_coupon_buttons", lambda page: (206, 44))
    monkeypatch.setattr(
        clipper, "select_departments",
        lambda page, wanted, debug=False: events.append("select") or (["Dairy"], []))
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)

    def fake_clip(page, cfg, budget, args, **kwargs):
        events.append(f"clip:{kwargs['phase']}")
        clip_calls.append((budget, kwargs))
        if kwargs["phase"] == "preferred":
            return clipper.ClipResult(clipped=68, exhausted=True)
        return clipper.ClipResult(clipped=138, exhausted=False)

    monkeypatch.setattr(clipper, "_clip_relevant", fake_clip)

    cfg = SimpleNamespace(
        departments=["Dairy"], max_clips=250, min_savings=0.5,
        include_nondollar=False, fill_to_limit=True, estimates=None)
    args = SimpleNamespace(dry_run=False, debug=False, min_delay=0, max_delay=0)

    assert clipper._run_relevance_mode(_RunPage(), cfg, args) == 0
    assert events == [
        "clear", "scroll", "count", "select", "scroll", "clip:preferred",
        "clear", "scroll", "clip:fill",
    ]
    assert [call[0] for call in clip_calls] == [206, 138]
    assert clip_calls[0][1]["min_savings"] == 0.5
    assert clip_calls[0][1]["include_nondollar"] is False
    assert clip_calls[1][1]["min_savings"] == 0.0
    assert clip_calls[1][1]["include_nondollar"] is True
    assert (clip_calls[0][1]["clicked_keys"]
            is clip_calls[1][1]["clicked_keys"])


def test_relevance_mode_without_fill_reports_preferred_exhaustion(
        monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.setattr(clipper, "clear_filters",
                        lambda page, debug=False: True)
    monkeypatch.setattr(clipper, "scroll_to_load_all",
                        lambda page, debug=False: None)
    monkeypatch.setattr(clipper, "scan_coupon_buttons", lambda page: (206, 44))
    monkeypatch.setattr(clipper, "count_clipped_total",
                        lambda page, debug=False: 44)
    monkeypatch.setattr(clipper, "select_departments",
                        lambda page, wanted, debug=False: (["Dairy"], []))
    monkeypatch.setattr(clipper, "human_pause", lambda lo, hi: None)
    monkeypatch.setattr(
        clipper, "_clip_relevant",
        lambda *args, **kwargs: clipper.ClipResult(clipped=68, exhausted=True))

    cfg = SimpleNamespace(
        departments=["Dairy"], max_clips=250, min_savings=0.0,
        include_nondollar=True, fill_to_limit=False, estimates=None)
    args = SimpleNamespace(dry_run=False, debug=False, min_delay=0, max_delay=0)

    assert clipper._run_relevance_mode(_RunPage(), cfg, args) == 0
    assert "preferred coupons were exhausted" in capsys.readouterr().out


# --- _find_department_option: poll past the panel's lazy render -------------

class _Opt:
    def __init__(self, visible):
        self._visible = visible

    def is_visible(self):
        return self._visible


class _Opts:
    def __init__(self, visible):
        self._visible = visible

    def count(self):
        return 1

    def nth(self, i):
        return _Opt(self._visible)


class _LazyPanel:
    """Fake page whose department row becomes visible only on/after the
    `appear_on`-th lookup (None = never), modelling the panel's lazy render."""
    def __init__(self, appear_on):
        self.lookups = 0
        self.appear_on = appear_on

    def get_by_text(self, pattern):
        self.lookups += 1
        visible = self.appear_on is not None and self.lookups >= self.appear_on
        return _Opts(visible)


def test_find_department_option_polls_until_visible(monkeypatch):
    monkeypatch.setattr(clipper.time, "sleep", lambda s: None)
    panel = _LazyPanel(appear_on=3)            # row renders on the 3rd lookup
    opt = clipper._find_department_option(panel, "Dairy", timeout=10, poll=0)
    assert opt is not None
    assert panel.lookups >= 3                  # it kept polling instead of giving up


def test_find_department_option_times_out(monkeypatch):
    monkeypatch.setattr(clipper.time, "sleep", lambda s: None)
    times = iter([0.0, 999.0, 999.0])
    monkeypatch.setattr(clipper.time, "monotonic", lambda: next(times))
    panel = _LazyPanel(appear_on=None)         # never renders
    assert clipper._find_department_option(panel, "Nope", timeout=8, poll=0) is None
