import pytest

from stock_analyzer import config, db, signals

HORIZONS = [1, 5, 20]
LIMITS = config.DEFAULTS["signals"]
UP = [{"horizon_days": h, "direction": "up", "confidence": 0.7} for h in HORIZONS]


def buy(**overrides):
    base = {"action": "buy", "horizon_days": 5, "confidence": 0.7, "limit_price": 100.0,
            "stop_loss": 95.0, "take_profit": 110.0, "reasoning": "Breakout on volume."}
    base.update(overrides)
    return base


def reviewed(raw, close=100.0, predictions=UP):
    return signals.review(signals.parse(raw, HORIZONS), close, predictions, LIMITS)


def bar(low, high, close=None):
    return {"low": low, "high": high, "close": close if close is not None else (low + high) / 2}


# --- parse ------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "buy", [], {}, {"action": "short"}])
def test_parse_rejects_unusable(raw):
    assert signals.parse(raw, HORIZONS) is None


def test_parse_normalizes():
    out = signals.parse(buy(action=" BUY ", confidence=70, stop_loss=0, take_profit="x",
                            horizon_days=7), HORIZONS)
    assert out["action"] == "buy"
    assert out["confidence"] == 0.7
    assert out["stop_loss"] is None and out["take_profit"] is None
    assert out["horizon_days"] is None  # 7 is not one of the horizons


def test_parse_bad_confidence_is_zero():
    assert signals.parse(buy(confidence="high"), HORIZONS)["confidence"] == 0.0
    assert signals.parse(buy(confidence=float("nan")), HORIZONS)["confidence"] == 0.0


def test_schema_is_strict():
    s = signals.schema(HORIZONS)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert set(signals.example(HORIZONS)) == set(s["properties"])


# --- review -----------------------------------------------------------------

def test_missing_signal_stays_missing():
    assert signals.review(None, 100.0, UP, LIMITS) is None


def test_good_buy_passes_with_size():
    out = reviewed(buy())
    assert out["action"] == out["proposed_action"] == "buy"
    assert out["checks"] == []
    assert out["reward_risk"] == 2.0
    # 1% risk with a 5% stop would be 20% of the portfolio; capped at 10%.
    assert out["position_pct"] == 10.0


def test_position_size_follows_stop_distance():
    out = reviewed(buy(limit_price=100.0, stop_loss=92.0, take_profit=116.0), close=100.0)
    assert out["action"] == "buy"
    assert out["position_pct"] == 10.0  # 12.5% by risk, capped
    limits = {**LIMITS, "max_position_pct": 50.0}
    out = signals.review(signals.parse(buy(stop_loss=92.0, take_profit=116.0), HORIZONS),
                         100.0, UP, limits)
    assert out["position_pct"] == 12.5  # 1% / 8%


def test_hold_drops_prices():
    out = reviewed(buy(action="hold"))
    assert out["action"] == "hold" and out["checks"] == []
    assert out["limit_price"] is None and out["stop_loss"] is None and out["take_profit"] is None


@pytest.mark.parametrize("change, reason", [
    ({"confidence": 0.5}, "confidence"),
    ({"limit_price": 0}, "no limit price"),
    ({"limit_price": 104.0}, "away from the last close"),
    ({"stop_loss": 0}, "without a stop-loss"),
    ({"stop_loss": 101.0}, "not ordered"),
    ({"take_profit": 99.0}, "not ordered"),
    ({"stop_loss": 88.0, "take_profit": 130.0}, "below the limit"),
    ({"take_profit": 104.0}, "reward/risk"),
    ({"horizon_days": 7}, "no valid forecast horizon"),
])
def test_bad_buy_becomes_hold(change, reason):
    out = reviewed(buy(**change))
    assert out["action"] == "hold" and out["proposed_action"] == "buy"
    assert any(reason in check for check in out["checks"]), out["checks"]
    assert out["position_pct"] is None


def test_buy_against_forecast_becomes_hold():
    down = [{**p, "direction": "down"} for p in UP]
    out = reviewed(buy(), predictions=down)
    assert out["action"] == "hold"
    assert "5-day forecast points down" in out["checks"][0]


def test_sell_passes_and_drops_exit_prices():
    down = [{**p, "direction": "down"} for p in UP]
    out = reviewed(buy(action="sell", limit_price=99.5), predictions=down)
    assert out["action"] == "sell" and out["limit_price"] == 99.5
    assert out["stop_loss"] is None and out["take_profit"] is None and out["position_pct"] is None


def test_sell_against_forecast_becomes_hold():
    assert reviewed(buy(action="sell"))["action"] == "hold"


# --- outcome ----------------------------------------------------------------

SIGNAL = {"action": "buy", "horizon_days": 5, "limit_price": 100.0, "stop_loss": 95.0,
          "take_profit": 110.0}


def test_outcome_target():
    bars = [bar(101, 104), bar(99, 103), bar(102, 111)]
    assert signals.outcome(SIGNAL, 102.0, bars) == {"status": "target", "return_pct": 10.0}


def test_outcome_stopped_even_on_fill_day():
    assert signals.outcome(SIGNAL, 102.0, [bar(94, 101)]) == {"status": "stopped", "return_pct": -5.0}


def test_outcome_stop_wins_when_bar_touches_both():
    bars = [bar(99, 101), bar(94, 111)]
    assert signals.outcome(SIGNAL, 102.0, bars)["status"] == "stopped"


def test_outcome_target_not_counted_on_fill_day():
    bars = [bar(99, 111)] + [bar(101, 103, close=102)] * 4
    assert signals.outcome(SIGNAL, 102.0, bars) == {"status": "expired", "return_pct": 2.0}


def test_outcome_not_filled_and_pending():
    never = [bar(101, 105)] * 5
    assert signals.outcome(SIGNAL, 102.0, never)["status"] == "not filled"
    assert signals.outcome(SIGNAL, 102.0, never[:2])["status"] == "pending"
    assert signals.outcome(SIGNAL, 102.0, [bar(99, 103)])["status"] == "open"


def test_outcome_ignores_bars_after_horizon():
    bars = [bar(101, 105)] * 5 + [bar(90, 120)]
    assert signals.outcome(SIGNAL, 102.0, bars)["status"] == "not filled"


def test_outcome_sell():
    sell = {"action": "sell", "horizon_days": 1, "limit_price": 100.0, "stop_loss": None,
            "take_profit": None}
    assert signals.outcome(sell, 100.0, [bar(95, 99, close=97)]) == {"status": "right", "return_pct": -3.0}
    assert signals.outcome(sell, 100.0, [bar(99, 104, close=103)])["status"] == "wrong"
    assert signals.outcome(sell, 100.0, [])["status"] == "pending"


def test_summarize():
    history = [
        {"signal": {"action": "buy"}, "outcome": {"status": "target", "return_pct": 10.0}},
        {"signal": {"action": "buy"}, "outcome": {"status": "stopped", "return_pct": -5.0}},
        {"signal": {"action": "buy"}, "outcome": {"status": "not filled", "return_pct": None}},
        {"signal": {"action": "sell"}, "outcome": {"status": "right", "return_pct": -3.0}},
        {"signal": {"action": "sell"}, "outcome": {"status": "pending", "return_pct": None}},
    ]
    assert signals.summarize(history) == {
        "buys_finished": 2, "buys_won": 1, "buy_avg_return_pct": 2.5,
        "sells_finished": 1, "sells_right": 1}
    assert signals.summarize([])["buy_avg_return_pct"] is None


# --- storage ----------------------------------------------------------------

def result(signal):
    return {"trend": "bullish", "summary": "s", "key_signals": [], "risks": [], "support": 95.0,
            "resistance": 110.0, "signal": signal,
            "predictions": [{"horizon_days": 5, "direction": "up", "confidence": 0.7,
                             "target_low": 99.0, "target_high": 108.0}]}


def test_signal_round_trip(temp_db):
    signal = reviewed(buy())
    db.save_analysis("AAA", "2026-01-05", "claude", "m", result(signal), {"close": 100.0}, "raw",
                     base_close=100.0, due_dates={5: "2026-01-12"})
    db.save_analysis("BBB", "2026-01-05", "claude", "m", result(None), {"close": 50.0}, "raw",
                     base_close=50.0, due_dates={5: "2026-01-12"})
    assert db.latest_analysis("AAA")["signal"] == signal
    assert db.latest_analysis("BBB")["signal"] is None
    stored = db.signals()
    assert [s["ticker"] for s in stored] == ["AAA"]
    assert stored[0]["close"] == 100.0 and stored[0]["signal"]["action"] == "buy"


def test_bars_after(temp_db):
    for date, low in [("2026-01-05", 1), ("2026-01-06", 2), ("2026-01-07", 3), ("2026-01-08", 4)]:
        db.execute("INSERT INTO prices(ticker, date, open, high, low, close, volume) "
                   "VALUES('AAA', ?, 1, 9, ?, 5, 100)", (date, low))
    assert [b["low"] for b in db.bars_after("AAA", "2026-01-05", 2)] == [2, 3]


def test_old_database_gains_signal_column(temp_db):
    import sqlite3

    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(db.SCHEMA)  # as created before the effort and signal columns existed
    conn.close()
    assert db.signals() == []
