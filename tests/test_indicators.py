import numpy as np
import pandas as pd
import pytest

from stock_analyzer import indicators as ind


def frame(closes, spread=1.0, volume=1000):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=len(closes), freq="B").strftime("%Y-%m-%d"),
        "open": closes, "high": closes + spread, "low": closes - spread,
        "close": closes, "volume": float(volume),
    })


def test_sma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] == 2 and out.iloc[4] == 4


def test_rsi_extremes_and_reference():
    rising = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(rising).iloc[-1] == 100
    falling = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert ind.rsi(falling).iloc[-1] == pytest.approx(0)
    # Alternating +2 / -1 converges to avg gain 1, avg loss 0.5 -> RS 2 -> RSI 66.67
    alt = pd.Series(np.cumsum([2, -1] * 200), dtype=float) + 100
    assert ind.rsi(alt).iloc[-1] == pytest.approx(66.67, abs=2.5)


def test_rsi_first_value_is_simple_average_seeded():
    # Wilder smoothing needs n changes before the first value.
    close = pd.Series(np.arange(1, 30, dtype=float))
    out = ind.rsi(close, 14)
    assert out.iloc[:14].isna().all()
    assert not np.isnan(out.iloc[14])


def test_macd_constant_series_is_zero():
    line, sig, hist = ind.macd(pd.Series([50.0] * 60))
    assert line.iloc[-1] == pytest.approx(0)
    assert hist.iloc[-1] == pytest.approx(0)


def test_macd_positive_in_uptrend():
    line, _, _ = ind.macd(pd.Series(np.linspace(100, 200, 80)))
    assert line.iloc[-1] > 0


def test_bollinger():
    s = pd.Series([10.0, 12.0] * 10)
    upper, mid, lower = ind.bollinger(s, 20, 2)
    assert mid.iloc[-1] == pytest.approx(11)
    assert upper.iloc[-1] == pytest.approx(13)   # population std of alternating 10/12 is 1
    assert lower.iloc[-1] == pytest.approx(9)


def test_atr_constant_range():
    df = frame([100.0] * 40, spread=1.5)
    assert ind.atr(df).iloc[-1] == pytest.approx(3.0)


def test_stochastic_at_high():
    df = frame(np.arange(100, 140, dtype=float), spread=0)
    k, _ = ind.stochastic(df)
    assert k.iloc[-1] == pytest.approx(100)


def test_obv():
    df = frame([10, 11, 10, 10, 12], volume=100)
    assert ind.obv(df).tolist() == [0, 100, 0, 0, 100]


def test_snapshot_uptrend_signals_and_json_safe():
    import json

    df = frame(np.linspace(50, 150, 260))
    snap = ind.snapshot(ind.compute(df))
    assert snap["close"] == 150
    assert "Price above SMA200" in snap["signals"]
    assert any("overbought" in s for s in snap["signals"])
    assert snap["high_52w"] == 151
    json.dumps(snap, allow_nan=False)


def test_snapshot_short_history_has_nulls_not_nans():
    import json

    snap = ind.snapshot(ind.compute(frame(np.linspace(10, 12, 30))))
    assert snap["sma200"] is None
    json.dumps(snap, allow_nan=False)


def test_golden_cross_detected():
    closes = np.concatenate([np.linspace(200, 100, 230), np.linspace(100, 260, 60)])
    full = ind.compute(frame(closes))
    diff = (full["sma50"] - full["sma200"]).dropna()
    cross_at = diff.index[(diff > 0) & (diff.shift() <= 0)][0]
    snap = ind.snapshot(full.iloc[: cross_at + 3])
    assert any("Golden cross" in s for s in snap["signals"])
