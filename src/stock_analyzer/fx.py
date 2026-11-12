"""Exchange rates for showing prices in one display currency (Yahoo Finance FX pairs)."""
from __future__ import annotations

import time

import yfinance as yf

from . import db

TTL = 3600
# Yahoo quotes some markets in a minor unit: London in pence, Johannesburg in cents, etc.
MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}

_cache: dict[tuple[str, str], tuple[float, float]] = {}


def _fetch(pair: str) -> float | None:
    try:
        hist = yf.Ticker(f"{pair}=X").history(period="5d", interval="1d")
        closes = hist["Close"].dropna()
        value = float(closes.iloc[-1]) if len(closes) else None
        return value if value and value > 0 else None
    except Exception:
        return None


def _lookup(frm: str, to: str) -> float | None:
    direct = _fetch(f"{frm}{to}")
    if direct:
        return direct
    if "USD" in (frm, to):
        inverse = _fetch(f"{to}{frm}")
        return 1 / inverse if inverse else None
    a, b = rate(frm, "USD"), rate("USD", to)  # thinly quoted pair: go through the dollar
    return a * b if a and b else None


def rate(frm: str | None, to: str) -> float | None:
    """Units of `to` per one unit of `frm` at the latest daily close; None if unknown."""
    if not frm:
        return None
    if frm in MINOR_UNITS:
        major, divisor = MINOR_UNITS[frm]
        major_rate = rate(major, to)
        return major_rate / divisor if major_rate else None
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return 1.0
    key = (frm, to)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < TTL:
        return cached[1]
    value = _lookup(frm, to)
    stored_key = f"fx:{frm}{to}"
    if value:
        _cache[key] = (time.monotonic(), value)
        db.set_setting(stored_key, value)  # last known rate, for when Yahoo is unreachable
        return value
    return db.get_settings().get(stored_key)


def rates(currencies, to: str) -> dict[str, float | None]:
    return {c: rate(c, to) for c in sorted({c for c in currencies if c})}
