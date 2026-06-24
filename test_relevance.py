import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from relevance import (  # noqa: E402
    Estimates, Savings, parse_savings,
)


def test_dollar_off():
    s = parse_savings("Clip for coupon: $1.50 off Tillamook", Estimates())
    assert (s.kind, s.value, s.estimated) == ("dollar", 1.5, False)


def test_save_dollar():
    s = parse_savings("Save $3 on 2 boxes", Estimates())
    assert (s.kind, s.value, s.estimated) == ("dollar", 3.0, False)


def test_cents_dollar():
    s = parse_savings("$0.75 off", Estimates())
    assert (s.kind, s.value) == ("dollar", 0.75)


def test_bogo_b1g1():
    s = parse_savings("Buy 1 Get 1 Free", Estimates(bogo=5.0))
    assert (s.kind, s.value, s.estimated) == ("bogo", 5.0, True)


def test_bogo_buy_n_get_m():
    s = parse_savings("buy 2 get 1 free", Estimates(bogo=5.0))
    assert s.kind == "bogo" and s.value == 5.0


def test_percent():
    s = parse_savings("20% off any item", Estimates(assumed_item_price=4.0))
    assert s.kind == "percent" and abs(s.value - 0.8) < 1e-9 and s.estimated is True


def test_unknown():
    s = parse_savings("Clip this great offer", Estimates(unknown=1.0))
    assert (s.kind, s.value, s.estimated) == ("unknown", 1.0, True)


@pytest.mark.parametrize("text", ["", None])
def test_empty_or_none_is_unknown(text):
    assert parse_savings(text, Estimates()).kind == "unknown"


def test_dollar_wins_when_both_present():
    s = parse_savings("Save 20% up to $5", Estimates())
    assert (s.kind, s.value) == ("dollar", 5.0)


from relevance import Candidate, rank_candidates  # noqa: E402


def _mk(label, value, kind, estimated):
    return Candidate(label=label, savings=Savings(value, kind, estimated))


def test_rank_orders_by_value_desc():
    cands = [_mk("a", 1.0, "dollar", False),
             _mk("b", 5.0, "bogo", True),
             _mk("c", 0.5, "dollar", False)]
    assert [c.label for c in rank_candidates(cands)] == ["b", "a", "c"]


def test_min_savings_floor():
    cands = [_mk("a", 1.0, "dollar", False), _mk("c", 0.5, "dollar", False)]
    assert [c.label for c in rank_candidates(cands, min_savings=0.75)] == ["a"]


def test_exclude_nondollar():
    cands = [_mk("a", 1.0, "dollar", False), _mk("b", 5.0, "bogo", True)]
    ranked = rank_candidates(cands, include_nondollar=False)
    assert [c.label for c in ranked] == ["a"]


def test_stable_for_ties():
    cands = [_mk("a", 2.0, "dollar", False), _mk("b", 2.0, "dollar", False)]
    assert [c.label for c in rank_candidates(cands)] == ["a", "b"]


from relevance import match_departments  # noqa: E402


def test_match_case_insensitive_and_trim():
    matched, missing = match_departments(
        [" dairy ", "PRODUCE"], ["Dairy", "Produce", "Frozen"])
    assert matched == ["Dairy", "Produce"] and missing == []


def test_missing_reported_with_original_spelling():
    matched, missing = match_departments(
        ["Dairy", "Diary"], ["Dairy", "Produce"])
    assert matched == ["Dairy"] and missing == ["Diary"]


from relevance import Config, load_config  # noqa: E402


def test_defaults_when_no_file():
    cfg = load_config(None)
    assert cfg.departments == []
    assert cfg.max_clips == 150
    assert cfg.min_savings == 0.0
    assert cfg.include_nondollar is True
    assert cfg.estimates.bogo == 5.0
    assert cfg.estimates.assumed_item_price == 4.0
    assert cfg.estimates.unknown == 1.0


def test_reads_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'departments = ["Dairy", "Produce"]\n'
        "max_clips = 50\n"
        "min_savings = 0.5\n"
        "include_nondollar = false\n"
        "[estimates]\n"
        "bogo = 7.0\n"
    )
    cfg = load_config(p)
    assert cfg.departments == ["Dairy", "Produce"]
    assert cfg.max_clips == 50
    assert cfg.min_savings == 0.5
    assert cfg.include_nondollar is False
    assert cfg.estimates.bogo == 7.0
    assert cfg.estimates.assumed_item_price == 4.0  # default preserved


def test_cli_overrides_win():
    cfg = load_config(None, {"departments": ["Produce"], "max_clips": 10,
                             "min_savings": 2.0})
    assert cfg.departments == ["Produce"]
    assert cfg.max_clips == 10
    assert cfg.min_savings == 2.0


def test_thousands_separator():
    s = parse_savings("$1,000 off appliances", Estimates())
    assert s.kind == "dollar" and s.value == 1000.0


def test_plain_thousands_no_comma():
    s = parse_savings("$1000 off", Estimates())
    assert s.kind == "dollar" and s.value == 1000.0


def test_dollar_wins_when_percent_first():
    s = parse_savings("50% off, $2 coupon", Estimates())
    assert s.kind == "dollar" and s.value == 2.0
