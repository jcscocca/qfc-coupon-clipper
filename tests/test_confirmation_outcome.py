"""A confirmation-blocked run is incomplete even after some successful clips."""
from types import SimpleNamespace

import pytest

import qfc_coupon_clipper as clipper


@pytest.mark.parametrize("confirmed", [0, 2])
def test_confirmation_block_returns_incomplete_after_preserving_count(monkeypatch, capsys, confirmed):
    monkeypatch.setattr(clipper, "wait_for_department_filters", lambda *a, **kw: True)
    monkeypatch.setattr(clipper, "clear_filters", lambda *a, **kw: True)
    monkeypatch.setattr(clipper, "_load_full_coupon_list", lambda *a: None)
    monkeypatch.setattr(clipper, "scan_coupon_buttons", lambda page: (5, 116))
    monkeypatch.setattr(clipper, "select_departments", lambda *a, **kw: (["Dairy"], []))
    monkeypatch.setattr(clipper, "_clip_relevant", lambda *a, **kw:
                        clipper.ClipResult(clipped=confirmed, exhausted=False,
                                           failed=3, confirmation_blocked=True))
    cfg = SimpleNamespace(departments=["Dairy"], max_clips=249, min_savings=0,
                          include_nondollar=True, fill_to_limit=False)
    args = SimpleNamespace(dry_run=False, debug=False, max=0)
    assert clipper._run_relevance_mode(object(), cfg, args) == 3
    output = capsys.readouterr().out
    assert f"Confirmed {confirmed} coupon(s)" in output
    assert "failed to confirm 3 attempted clip(s)" in output
