"""Technical indicators computed locally from daily OHLCV bars."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder(-delta.clip(upper=0), n)
    out = 100 - 100 / (1 + gain / loss)
    return out.where(loss != 0, 100.0).where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return _wilder(tr, n)


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3):
    lowest = df["low"].rolling(n).min()
    highest = df["high"].rolling(n).max()
    k = 100 * (df["close"] - lowest) / (highest - lowest).replace(0, np.nan)
    return k, k.rolling(d).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    return (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to a frame with open/high/low/close/volume columns."""
    out = df.copy()
    close = out["close"]
    for n in (20, 50, 200):
        out[f"sma{n}"] = sma(close, n)
    out["ema12"], out["ema26"] = ema(close, 12), ema(close, 26)
    out["rsi14"] = rsi(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(close)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger(close)
    out["atr14"] = atr(out)
    out["stoch_k"], out["stoch_d"] = stochastic(out)
    out["obv"] = obv(out)
    out["vol_avg20"] = out["volume"].rolling(20).mean()
    return out


def _num(value, digits: int = 4):
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) or math.isinf(value) else round(value, digits)


def _pct(now: float, then: float):
    return _num((now / then - 1) * 100, 2) if then else None


def _crossed(a: pd.Series, b: pd.Series, lookback: int = 5) -> str | None:
    """'up' if a crossed above b within the last `lookback` bars, 'down' if below."""
    diff = (a - b).dropna().tail(lookback + 1)
    if len(diff) < 2:
        return None
    signs = np.sign(diff.to_numpy())
    for i in range(len(signs) - 1, 0, -1):
        if signs[i] > 0 >= signs[i - 1]:
            return "up"
        if signs[i] < 0 <= signs[i - 1]:
            return "down"
    return None


def snapshot(ind: pd.DataFrame) -> dict:
    """Latest indicator values plus plain-language signal flags for the model and the UI."""
    last = ind.iloc[-1]
    close = float(last["close"])
    year = ind.tail(252)
    recent = ind.tail(20)

    def back(n: int):
        return _pct(close, float(ind["close"].iloc[-n - 1])) if len(ind) > n else None

    values = {
        "close": _num(close),
        "change_1d_pct": back(1), "change_5d_pct": back(5),
        "change_20d_pct": back(20), "change_60d_pct": back(60),
        "sma20": _num(last["sma20"]), "sma50": _num(last["sma50"]), "sma200": _num(last["sma200"]),
        "ema12": _num(last["ema12"]), "ema26": _num(last["ema26"]),
        "rsi14": _num(last["rsi14"], 2),
        "macd": _num(last["macd"]), "macd_signal": _num(last["macd_signal"]),
        "macd_hist": _num(last["macd_hist"]),
        "bb_upper": _num(last["bb_upper"]), "bb_lower": _num(last["bb_lower"]),
        "atr14": _num(last["atr14"]),
        "atr_pct": _num(last["atr14"] / close * 100, 2) if close else None,
        "stoch_k": _num(last["stoch_k"], 2), "stoch_d": _num(last["stoch_d"], 2),
        "volume": _num(last["volume"], 0),
        "volume_ratio_20d": _num(last["volume"] / last["vol_avg20"], 2)
        if last["vol_avg20"] else None,
        "obv_change_20d_pct": _pct(float(last["obv"]), float(ind["obv"].iloc[-21]))
        if len(ind) > 21 and ind["obv"].iloc[-21] else None,
        "high_52w": _num(year["high"].max()), "low_52w": _num(year["low"].min()),
        "pct_from_52w_high": _pct(close, float(year["high"].max())),
        "recent_support_20d": _num(recent["low"].min()),
        "recent_resistance_20d": _num(recent["high"].max()),
    }

    signals: list[str] = []

    def above(key: str, label: str):
        if values[key] is not None:
            signals.append(f"Price {'above' if close > values[key] else 'below'} {label}")

    above("sma20", "SMA20")
    above("sma50", "SMA50")
    above("sma200", "SMA200")

    cross = _crossed(ind["sma50"], ind["sma200"], 10)
    if cross:
        signals.append("Golden cross (SMA50 over SMA200) in last 10 bars" if cross == "up"
                       else "Death cross (SMA50 under SMA200) in last 10 bars")
    cross = _crossed(ind["macd"], ind["macd_signal"], 5)
    if cross:
        signals.append(f"MACD crossed {'above' if cross == 'up' else 'below'} signal in last 5 bars")
    elif values["macd_hist"] is not None:
        signals.append(f"MACD histogram {'positive' if values['macd_hist'] > 0 else 'negative'}")

    r = values["rsi14"]
    if r is not None:
        zone = "overbought (>70)" if r > 70 else "oversold (<30)" if r < 30 else "neutral"
        signals.append(f"RSI14 {r:.0f}, {zone}")
    k = values["stoch_k"]
    if k is not None and (k > 80 or k < 20):
        signals.append(f"Stochastic %K {k:.0f}, {'overbought' if k > 80 else 'oversold'}")

    upper, lower = values["bb_upper"], values["bb_lower"]
    if upper is not None and lower is not None and upper > lower:
        pos = (close - lower) / (upper - lower)
        values["bb_position"] = _num(pos, 2)
        if pos > 1:
            signals.append("Close above upper Bollinger band")
        elif pos < 0:
            signals.append("Close below lower Bollinger band")
    vr = values["volume_ratio_20d"]
    if vr is not None and vr >= 1.5:
        signals.append(f"Volume spike, {vr:.1f}x the 20-day average")

    values["signals"] = signals
    return values
