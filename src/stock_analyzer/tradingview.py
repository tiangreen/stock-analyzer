"""Links from a Yahoo ticker to the same instrument on TradingView.

TradingView has no data API for subscribers and forbids automated data collection, so the
subscription cannot feed this app. What it does give is better charts in the browser: these
links open the stock in the user's own logged-in TradingView session.
"""
from __future__ import annotations

from urllib.parse import quote

# Yahoo suffix -> TradingView exchange prefix
EXCHANGES = {
    "KS": "KRX", "KQ": "KRX",          # Korea: KOSPI, KOSDAQ
    "T": "TSE",                         # Tokyo
    "DE": "XETR", "F": "FWB",
    "PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT", "LS": "EURONEXT",
    "L": "LSE", "MI": "MIL", "MC": "BME", "SW": "SIX", "VI": "VIE",
    "ST": "OMXSTO", "HE": "OMXHEX", "CO": "OMXCOP", "OL": "OSL",
    "HK": "HKEX", "TO": "TSX", "AX": "ASX",
}


def symbol(ticker: str) -> str | None:
    """TradingView symbol for a Yahoo ticker, or None for instruments it cannot map."""
    if ticker.startswith("^") or "=" in ticker:
        return None
    base, dot, suffix = ticker.rpartition(".")
    if not dot:
        return ticker.replace("-", ".")  # US listing; class shares: BRK-B -> BRK.B
    exchange = EXCHANGES.get(suffix)
    if not exchange:
        return None
    if exchange == "HKEX":
        base = base.lstrip("0") or "0"   # 0700.HK -> HKEX:700
    return f"{exchange}:{base.replace('-', '.')}"


def chart_url(ticker: str) -> str | None:
    tv = symbol(ticker)
    return f"https://www.tradingview.com/chart/?symbol={quote(tv, safe='')}" if tv else None
