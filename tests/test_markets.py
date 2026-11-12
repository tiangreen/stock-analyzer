import pandas as pd
import pytest

from stock_analyzer import data, tradingview
from stock_analyzer.server import TICKER_RE


def session(day, start="09:00", end="15:30", tz="Asia/Tokyo"):
    return {"start": pd.Timestamp(f"{day} {start}", tz=tz), "end": pd.Timestamp(f"{day} {end}", tz=tz)}


def test_bar_from_open_session_is_unfinished():
    s = session("2026-11-12")
    bar = pd.Timestamp("2026-11-12", tz="Asia/Tokyo")
    assert data.session_unfinished(bar, s, now=pd.Timestamp("2026-11-12 11:00", tz="Asia/Tokyo"))
    # Same instant seen from Europe: still mid-session in Tokyo.
    assert data.session_unfinished(bar, s, now=pd.Timestamp("2026-11-12 03:00", tz="Europe/Berlin"))


def test_bar_is_final_after_close():
    s = session("2026-11-12")
    bar = pd.Timestamp("2026-11-12", tz="Asia/Tokyo")
    assert not data.session_unfinished(bar, s, now=pd.Timestamp("2026-11-12 15:30", tz="Asia/Tokyo"))


def test_yesterdays_bar_is_final_before_todays_open():
    # 22:30 in Europe: Tokyo's next session is "current" in Yahoo's metadata but has no bar yet.
    s = session("2026-11-12")
    bar = pd.Timestamp("2026-11-11", tz="Asia/Tokyo")
    assert not data.session_unfinished(bar, s, now=pd.Timestamp("2026-11-12 06:30", tz="Asia/Tokyo"))


def test_missing_session_metadata_keeps_bar():
    assert not data.session_unfinished(pd.Timestamp("2026-11-12"), None)
    assert not data.session_unfinished(pd.Timestamp("2026-11-12"), {})


@pytest.mark.parametrize("ticker, expected", [
    ("AAPL", "AAPL"),
    ("BRK-B", "BRK.B"),
    ("005930.KS", "KRX:005930"),
    ("035720.KQ", "KRX:035720"),
    ("7203.T", "TSE:7203"),
    ("SAP.DE", "XETR:SAP"),
    ("ASML.AS", "EURONEXT:ASML"),
    ("SHEL.L", "LSE:SHEL"),
    ("0700.HK", "HKEX:700"),
    ("^GSPC", None),
    ("ABC.ZZ", None),
])
def test_tradingview_symbol(ticker, expected):
    assert tradingview.symbol(ticker) == expected


def test_tradingview_url_is_encoded():
    assert tradingview.chart_url("005930.KS") == "https://www.tradingview.com/chart/?symbol=KRX%3A005930"
    assert tradingview.chart_url("^GSPC") is None


@pytest.mark.parametrize("ticker", ["005930.KS", "7203.T", "035720.KQ", "AAPL", "BRK-B", "SAP.DE"])
def test_ticker_pattern_accepts_asian_codes(ticker):
    assert TICKER_RE.fullmatch(ticker)
