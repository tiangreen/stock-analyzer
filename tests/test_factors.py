import numpy as np
import pandas as pd
import pytest

from stock_analyzer import factors

LONG_WEIGHTS = {"quality": 30, "valuation": 25, "growth": 20, "estimates": 10, "trend": 10, "risk": 5}
SHORT_WEIGHTS = {"momentum": 30, "revisions": 25, "drift": 15, "volume": 10, "catalyst": 10,
                 "overextension": 10}


def bars(closes, volume=1000.0):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range("2025-01-01", periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"close": closes, "high": closes * 1.01, "low": closes * 0.99,
                         "volume": volume}, index=dates)


# --- ranks ------------------------------------------------------------------

def test_rank_runs_from_0_to_100():
    assert factors.pct_rank({"a": 1, "b": 2, "c": 3}) == {"a": 0.0, "b": 50.0, "c": 100.0}


def test_rank_can_prefer_low_values():
    assert factors.pct_rank({"a": 1, "b": 2, "c": 3}, higher_is_better=False)["a"] == 100.0


def test_rank_leaves_missing_values_out_and_shares_ties():
    ranks = factors.pct_rank({"a": 5, "b": 5, "c": None, "d": float("nan")})
    assert ranks == {"a": 50.0, "b": 50.0}


def test_a_lone_value_is_neutral():
    assert factors.pct_rank({"a": 7, "b": None}) == {"a": 50.0}


# --- price factors ----------------------------------------------------------

def test_price_factors_of_a_steady_uptrend():
    f = factors.price_factors(bars(np.linspace(50, 100, 300)))
    assert f["trend"] == 3 and f["above_sma200"] is True
    assert f["ret_1m"] > 0 and f["ret_3m"] > f["ret_1m"] and f["mom_12_1"] > 0
    assert f["pct_from_high"] == 0
    assert f["drawdown_6m"] == 0


def test_price_factors_of_a_downtrend():
    f = factors.price_factors(bars(np.linspace(100, 50, 300)))
    assert f["trend"] == 0 and f["above_sma200"] is False
    assert f["pct_from_high"] < -20 and f["drawdown_6m"] < -10


def test_too_little_history_gives_no_factors():
    assert factors.price_factors(bars(np.linspace(50, 60, 100))) is None


def test_relative_strength_is_measured_against_the_index():
    stock, index = bars(np.linspace(50, 100, 300)), bars(np.full(300, 80.0))
    f = factors.price_factors(stock, index["close"])
    assert f["rs_3m"] == pytest.approx(f["ret_3m"])
    assert factors.price_factors(stock)["rs_3m"] is None


def test_volume_thrust_follows_the_price_direction():
    volume = np.r_[np.full(295, 1000.0), np.full(5, 3000.0)]
    up = factors.price_factors(bars(np.linspace(50, 100, 300), volume))
    down = factors.price_factors(bars(np.linspace(100, 50, 300), volume))
    assert up["volume_ratio"] > 1 and up["volume_thrust"] > 0 > down["volume_thrust"]


def test_overextension_penalties_add_up():
    assert factors.overextension({}) == 100
    assert factors.overextension({"rsi14": 75}) == 85
    assert factors.overextension({"rsi14": 85, "ext_atr": 3.5, "vol_60d": 70}) == 0


# --- red flags --------------------------------------------------------------

def test_red_flags():
    assert factors.red_flags({"name": "x"}) == ["no fundamentals data"]
    assert factors.red_flags({"roe": 12, "trailing_eps": 2, "debt_equity": 80}) == []
    assert factors.red_flags({"roe": -5, "trailing_eps": -1, "fcf_yield": -2})
    assert factors.red_flags({"roe": -5, "trailing_eps": -1, "fcf_yield": 3}) == []
    assert factors.red_flags({"roe": 12, "trailing_eps": 2, "debt_equity": 450})


def test_a_bank_is_not_flagged_for_its_debt():
    bank = {"roe": 12, "trailing_eps": 2, "debt_equity": 900, "sector": factors.FINANCIALS}
    assert factors.red_flags(bank) == []


# --- pillar scores ----------------------------------------------------------

def company(**overrides):
    base = {"sector": "Technology", "roe": 15, "op_margin": 20, "fcf_margin": 10,
            "debt_equity": 60, "current_ratio": 1.5, "fwd_earnings_yield": 5, "fcf_yield": 4,
            "ev_ebitda": 14, "peg": 1.5, "rev_growth": 8, "earn_growth": 10,
            "fwd_eps_growth": 10, "next_year_growth": 10, "eps_rev_90d": 1, "target_upside": 10,
            "mom_12_1": 10, "trend": 2, "vol_60d": 25, "beta": 1.0, "drawdown_6m": -10}
    return {**base, **overrides}


def test_the_better_company_scores_higher():
    scored = factors.score({
        "GOOD": company(roe=30, op_margin=35, rev_growth=20, fwd_earnings_yield=7),
        "MID": company(),
        "POOR": company(roe=4, op_margin=5, rev_growth=-5, fwd_earnings_yield=2),
    }, "long", LONG_WEIGHTS)
    assert scored["GOOD"]["score"] > scored["MID"]["score"] > scored["POOR"]["score"]
    assert scored["GOOD"]["pillars"]["quality"] > scored["POOR"]["pillars"]["quality"]
    assert scored["GOOD"]["coverage"] == 1.0


def test_missing_data_is_neutral_and_lowers_coverage():
    scored = factors.score({
        "FULL": company(roe=30), "OTHER": company(roe=5),
        "BLANK": {"sector": "Technology"},
    }, "long", LONG_WEIGHTS)
    blank = scored["BLANK"]
    assert blank["coverage"] == 0 and blank["score"] == 50
    assert set(blank["pillars"].values()) == {50.0}


def test_weights_decide_which_pillar_counts():
    pool = {"QUALITY": company(roe=40, op_margin=40, rev_growth=0, earn_growth=0),
            "GROWTH": company(roe=5, op_margin=5, rev_growth=40, earn_growth=40)}
    quality_first = factors.score(pool, "long", {"quality": 1})
    growth_first = factors.score(pool, "long", {"growth": 1})
    assert quality_first["QUALITY"]["score"] > quality_first["GROWTH"]["score"]
    assert growth_first["GROWTH"]["score"] > growth_first["QUALITY"]["score"]


def test_valuation_is_ranked_inside_the_sector():
    # Utilities all look cheap next to software. The priciest utility must not score as cheap.
    pool = {f"U{i}": {"sector": "Utilities", "fwd_earnings_yield": 8 + i} for i in range(4)}
    pool.update({f"T{i}": {"sector": "Technology", "fwd_earnings_yield": 2 + i} for i in range(4)})
    valuation = {t: s["pillars"]["valuation"]
                 for t, s in factors.score(pool, "long", {"valuation": 1}).items()}
    assert valuation["U0"] == 0 and valuation["T3"] == 100
    assert valuation["T3"] > valuation["U0"]


def test_valuation_falls_back_to_all_stocks_for_a_small_sector():
    pool = {"A": {"sector": "Energy", "fwd_earnings_yield": 9},
            "B": {"sector": "Technology", "fwd_earnings_yield": 3},
            "C": {"sector": "Healthcare", "fwd_earnings_yield": 6}}
    valuation = {t: s["pillars"]["valuation"]
                 for t, s in factors.score(pool, "long", {"valuation": 1}).items()}
    assert valuation == {"A": 100.0, "B": 0.0, "C": 50.0}


def test_metrics_that_mean_nothing_for_the_company_are_ignored():
    bank = {"sector": factors.FINANCIALS, "debt_equity": 900, "price_to_book": 0.8, "peg": -2}
    assert factors._usable(bank, "debt_equity") is None
    assert factors._usable(bank, "price_to_book") == 0.8
    assert factors._usable(bank, "peg") is None   # a negative multiple is not cheap
    assert factors._usable({"sector": "Technology", "price_to_book": 9}, "price_to_book") is None


def test_short_term_score_penalizes_a_stretched_stock():
    base = {"rs_3m": 10, "ret_1m": 5, "pct_from_high": -2, "trend": 3}
    scored = factors.score({"CALM": {**base, "rsi14": 60}, "HOT": {**base, "rsi14": 88}},
                           "short", SHORT_WEIGHTS)
    assert scored["CALM"]["pillars"]["overextension"] == 100
    assert scored["HOT"]["pillars"]["overextension"] == 60
    assert scored["CALM"]["score"] > scored["HOT"]["score"]


# --- coarse ranking and shortlist -------------------------------------------

def test_coarse_ranks_inside_each_region():
    rows = [{"ticker": "US1", "region": "us", "rs_3m": 30, "ret_1m": 10, "trend": 3},
            {"ticker": "US2", "region": "us", "rs_3m": 20, "ret_1m": 8, "trend": 3},
            {"ticker": "JP1", "region": "jp", "rs_3m": 5, "ret_1m": 2, "trend": 3},
            {"ticker": "JP2", "region": "jp", "rs_3m": 1, "ret_1m": 1, "trend": 3}]
    coarse = factors.coarse_scores(rows, "short")
    # The best Japanese stock ties the best US one, although its numbers are lower.
    assert coarse["JP1"] == coarse["US1"] > coarse["US2"] == coarse["JP2"]


def test_fundamental_screens_lift_the_long_term_coarse_score():
    rows = [{"ticker": "A", "region": "us", "mom_12_1": 10, "quality_screen": True, "growth_screen": True},
            {"ticker": "B", "region": "us", "mom_12_1": 10, "quality_screen": False, "growth_screen": False}]
    coarse = factors.coarse_scores(rows, "long")
    assert coarse["A"] - coarse["B"] == pytest.approx(40)


def test_shortlist_skips_red_flags_and_caps_sectors():
    pool = {"A": company(), "B": company(), "C": company(), "D": company(sector="Energy"),
            "BAD": company(sector="Energy", trailing_eps=-1, fcf_yield=-3)}
    scored = {"A": {"score": 90}, "B": {"score": 80}, "C": {"score": 70}, "BAD": {"score": 95},
              "D": {"score": 10}}
    assert factors.shortlist(scored, pool, size=3, max_per_sector=2) == ["A", "B", "D"]


# --- grading ----------------------------------------------------------------

def closes(values, start="2026-01-05"):
    dates = pd.bdate_range(start, periods=len(values)).strftime("%Y-%m-%d")
    return pd.Series(values, index=dates, dtype=float)


def test_pick_return_while_the_horizon_is_running():
    series = closes([100, 102, 104, 110])
    result = factors.pick_return(series, "2026-01-05", horizon=20)
    assert result == {"bars": 3, "final": False, "date": "2026-01-08", "return_pct": 10.0}


def test_pick_return_stops_at_the_horizon():
    series = closes([100, 102, 104, 110, 150])
    result = factors.pick_return(series, "2026-01-05", horizon=2)
    assert result == {"bars": 2, "final": True, "date": "2026-01-07", "return_pct": 4.0}


def test_the_newest_bar_is_never_final():
    # The horizon's last bar may be a session still trading; a later bar proves it is done.
    result = factors.pick_return(closes([100, 102, 104]), "2026-01-05", horizon=2)
    assert result == {"bars": 2, "final": False, "date": "2026-01-07", "return_pct": 4.0}


def test_pick_return_on_the_day_of_the_pick_and_without_history():
    assert factors.pick_return(closes([100]), "2026-01-05", 20)["return_pct"] == 0
    assert factors.pick_return(closes([100, 101]), "2025-12-01", 20) is None


def test_index_return_uses_the_last_close_on_or_before_each_date():
    index = closes([200, 210, 220])            # 5, 6, 7 January
    assert factors.return_between(index, "2026-01-05", "2026-01-07") == 10.0
    assert factors.return_between(index, "2026-01-05", "2026-01-10") == 10.0  # holiday after
    assert factors.return_between(index, "2025-12-01", "2026-01-07") is None
