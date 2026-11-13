"""Realtime (intraday) analyses: live quotes, separate storage, never graded."""
import sqlite3

import pandas as pd
import pytest

from stock_analyzer import config, data, db, indicators, prompt, scoring, server, signals

TZ = "Asia/Tokyo"
HORIZONS = [1, 5, 20]
FORECAST = [{"horizon_days": 1, "direction": "up", "confidence": 0.6,
             "target_low": 100, "target_high": 104}]


def frame(index, closes):
    return pd.DataFrame({"Open": closes, "High": [c + 1 for c in closes],
                         "Low": [c - 1 for c in closes], "Close": closes,
                         "Volume": [1000] * len(closes)}, index=index)


class FakeTicker:
    """Stands in for yfinance.Ticker: two finished days, then 2026-11-12 in progress."""

    def __init__(self, _ticker):
        self.history_metadata = {"currentTradingPeriod": {"regular": {
            "start": pd.Timestamp("2026-11-12 09:00", tz=TZ),
            "end": pd.Timestamp("2026-11-12 15:30", tz=TZ)}}}

    def history(self, period, interval, auto_adjust=True):
        if interval == "1d":
            return frame(pd.DatetimeIndex(["2026-11-10", "2026-11-11", "2026-11-12"], tz=TZ),
                         [100.0, 102.0, 105.06])
        return frame(pd.DatetimeIndex(["2026-11-12 09:00", "2026-11-12 09:15"], tz=TZ),
                     [103.0, 105.06])


def test_live_session_while_exchange_is_open(monkeypatch):
    monkeypatch.setattr(data.yf, "Ticker", FakeTicker)
    # Asked from Europe at 03:00, which is 11:00 in Tokyo: times are the exchange's own.
    live = data.live_session("7203.T", now=pd.Timestamp("2026-11-12 03:00", tz="Europe/Berlin"))
    assert live["date"] == "2026-11-12" and live["as_of"] == "2026-11-12 11:00"
    assert live["price"] == 105.06 and live["prev_close"] == 102.0
    assert live["change_pct"] == 3.0
    assert live["minutes_left"] == 270
    assert [b["time"] for b in live["bars"]] == ["09:00", "09:15"]


def test_no_live_session_after_the_close(monkeypatch):
    monkeypatch.setattr(data.yf, "Ticker", FakeTicker)
    assert data.live_session("7203.T", now=pd.Timestamp("2026-11-12 15:30", tz=TZ)) is None


def result(signal=None):
    return {"trend": "bullish", "summary": "s", "key_signals": [], "risks": [], "support": None,
            "resistance": None, "predictions": FORECAST, "signal": signal}


def save(mode, signal=None, snap=None):
    return db.save_analysis("TEST", "2026-01-05", "claude", "m", result(signal), snap or {}, "raw",
                            base_close=100.0, due_dates={}, mode=mode)


def test_daily_and_realtime_are_stored_side_by_side(temp_db):
    save("daily")
    save("realtime")
    save("realtime")  # a second realtime run in the same session replaces the first
    assert sorted(a["mode"] for a in db.analyses_for("TEST")) == ["daily", "realtime"]
    assert db.one("SELECT COUNT(*) AS n FROM predictions")["n"] == 2


def test_realtime_does_not_count_as_the_daily_analysis(temp_db):
    save("realtime")
    assert not db.has_analysis("TEST", "2026-01-05", "claude")
    save("daily")
    assert db.has_analysis("TEST", "2026-01-05", "claude")


def test_realtime_forecasts_are_never_graded(temp_db):
    save("realtime")
    with db.connect() as conn:
        for day, close in (("2026-01-05", 100), ("2026-01-06", 102)):
            conn.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?)",
                         ("TEST", day, close, close, close, close, 1))
    assert scoring.score_open() == 0
    acc = scoring.accuracy()
    assert acc["pending"] == 0 and acc["by_horizon"] == []


def test_old_database_is_migrated_without_losing_forecasts(temp_db):
    """A database from before realtime analyses: no mode column, unique on three columns."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript("""
        CREATE TABLE analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, run_date TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL, trend TEXT, summary TEXT,
            support REAL, resistance REAL, signals_json TEXT, risks_json TEXT,
            indicators_json TEXT, raw_response TEXT, UNIQUE (ticker, run_date, provider));
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL, provider TEXT NOT NULL, run_date TEXT NOT NULL,
            horizon_days INTEGER NOT NULL, direction TEXT NOT NULL, confidence REAL,
            target_low REAL, target_high REAL, base_close REAL NOT NULL, due_date TEXT,
            actual_date TEXT, actual_close REAL, actual_return REAL, outcome TEXT, in_range INTEGER);
        INSERT INTO analyses(id, ticker, run_date, provider, model, created_at, trend, summary)
            VALUES(7, 'TEST', '2026-01-05', 'claude', 'm', '2026-01-05T22:30:00', 'bullish', 'old');
        INSERT INTO predictions(analysis_id, ticker, provider, run_date, horizon_days, direction,
            confidence, target_low, target_high, base_close, outcome, in_range)
            VALUES(7, 'TEST', 'claude', '2026-01-05', 1, 'up', 0.6, 100, 104, 100, 'hit', 1);
    """)
    conn.close()

    old = db.analyses_for("TEST")
    assert [(a["id"], a["summary"], a["mode"]) for a in old] == [(7, "old", "daily")]
    assert old[0]["predictions"][0]["outcome"] == "hit"
    assert scoring.accuracy()["by_horizon"][0]["graded"] == 1

    save("realtime")  # the old unique key would have refused this second row for the date
    assert len(db.analyses_for("TEST")) == 2
    # Forecasts still hang on their analysis: removing the stock removes them.
    db.remove_ticker("TEST")
    assert db.one("SELECT COUNT(*) AS n FROM predictions")["n"] == 0


def test_signals_view_keeps_realtime_out_of_the_track_record(temp_db):
    db.add_ticker("TEST", {"name": "Test", "currency": "USD"})
    buy = signals.review(
        {"action": "buy", "horizon_days": 1, "confidence": 0.8, "limit_price": 100.0,
         "stop_loss": 95.0, "take_profit": 110.0, "reasoning": "r"},
        100.0, FORECAST, config.DEFAULTS["signals"])
    save("realtime", buy, {"close": 98.0, "live": {"price": 100.0}})
    out = server.get_signals()
    assert [(r["mode"], r["close"]) for r in out["latest"]] == [("realtime", 100.0)]
    assert out["history"] == []
    save("daily", buy, {"close": 98.0})
    assert [r["mode"] for r in server.get_signals()["history"]] == ["daily"]


def test_entry_gap_is_measured_from_the_live_price():
    far = {"action": "buy", "horizon_days": 1, "confidence": 0.8, "limit_price": 110.0,
           "stop_loss": 105.0, "take_profit": 125.0, "reasoning": "r"}
    limits = config.DEFAULTS["signals"]
    assert signals.review(far, 100.0, FORECAST, limits, live=True)["check_codes"][0]["code"] == "entry_gap_live"
    assert signals.review(far, 100.0, FORECAST, limits)["check_codes"][0]["code"] == "entry_gap"


def test_prompt_adds_the_live_session_only_in_realtime():
    closes = [100 + i * 0.1 for i in range(80)]
    prices = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-07-01", periods=80)],
        "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": [1000.0] * 80})
    snap = indicators.snapshot(indicators.compute(prices))
    live = {"date": "2026-11-12", "as_of": "2026-11-12 11:00", "timezone": TZ, "price": 109.5,
            "open": 108.0, "high": 110.0, "low": 107.5, "volume": 5000.0, "prev_close": 107.9,
            "change_pct": 1.48, "minutes_left": 270,
            "bars": [{"time": "09:00", "open": 108.0, "high": 109.0, "low": 107.5, "close": 108.8,
                      "volume": 2000.0}]}

    daily = prompt.build("TEST", {}, prices, snap, [], [], HORIZONS)
    assert "as of the close on" in daily and "Live session" not in daily

    realtime = prompt.build("TEST", {}, prices, {**snap, "live": live}, [], [], HORIZONS, live=live)
    assert "as of 2026-11-12 11:00 Asia/Tokyo time" in realtime
    assert "## Live session on 2026-11-12" in realtime
    assert "09:00,108.00,109.00,107.50,108.80,2000" in realtime
    assert "versus the live price of 109.5" in realtime
    assert "close to the live price" in realtime
    assert realtime.count('"minutes_left"') == 1  # the stored live block is not repeated


def test_live_quote_endpoint(temp_db, monkeypatch):
    calls = []

    def fake(ticker, bars=True):
        calls.append(bars)
        return {"date": "2026-11-12", "price": 105.06, "bars": []}

    monkeypatch.setattr(data, "live_session", fake)
    server._live_cache.clear()
    db.add_ticker("TEST", {})
    out = server.live("TEST")
    assert out["open"] and out["quote"] == {"date": "2026-11-12", "price": 105.06}
    server.live("TEST")
    assert calls == [False]  # no intraday bars, and the second poll is served from the cache

    server._live_cache.clear()
    monkeypatch.setattr(data, "live_session", lambda ticker, bars=True: None)
    assert server.live("TEST") == {"ticker": "TEST", "open": False, "quote": None}
    with pytest.raises(server.HTTPException):
        server.live("NOPE")


def test_live_quote_skips_intraday_bars(monkeypatch):
    asked = []

    class Counting(FakeTicker):
        def history(self, period, interval, auto_adjust=True):
            asked.append(interval)
            return super().history(period, interval, auto_adjust)

    monkeypatch.setattr(data.yf, "Ticker", Counting)
    quote = data.live_session("7203.T", now=pd.Timestamp("2026-11-12 11:00", tz=TZ), bars=False)
    assert quote["price"] == 105.06 and quote["bars"] == [] and asked == ["1d"]
