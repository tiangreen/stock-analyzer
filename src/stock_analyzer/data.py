"""Market data from Yahoo Finance (yfinance), cached in SQLite."""
from __future__ import annotations

import math

import pandas as pd
import yfinance as yf

from . import db


class DataError(Exception):
    pass


def normalize(ticker: str) -> str:
    return ticker.strip().upper()


def update_prices(ticker: str, period: str = "2y") -> int:
    """Refetch adjusted daily bars and replace the cached series. Returns the bar count."""
    try:
        source = yf.Ticker(ticker)
        hist = source.history(period=period, interval="1d", auto_adjust=True)
    except Exception as exc:  # yfinance raises many unrelated exception types
        raise DataError(f"{ticker}: download failed ({exc})") from exc
    if hist is None or hist.empty:
        raise DataError(f"{ticker}: no price data (check the symbol and exchange suffix)")
    hist = hist.dropna(subset=["Close"])
    try:
        session = (source.history_metadata or {}).get("currentTradingPeriod", {}).get("regular")
    except Exception:
        session = None
    if len(hist) > 1 and session_unfinished(hist.index[-1], session):
        # The exchange is still trading: today's bar is incomplete, so analysis and
        # forecast grading must not use it. Markets close at different times worldwide.
        hist = hist.iloc[:-1]
    records = [
        (ticker, idx.strftime("%Y-%m-%d"), float(r.Open), float(r.High), float(r.Low),
         float(r.Close), float(r.Volume or 0))
        for idx, r in hist.iterrows()
    ]
    with db.connect() as conn:
        conn.execute("DELETE FROM prices WHERE ticker = ?", (ticker,))
        conn.executemany("INSERT INTO prices VALUES(?,?,?,?,?,?,?)", records)
    return len(records)


def session_unfinished(last_bar, session: dict | None, now=None) -> bool:
    """True if `last_bar` belongs to the exchange's current session and that session has
    not closed yet. `session` is Yahoo's regular trading period ({'start', 'end'} timestamps
    in the exchange's own time zone)."""
    try:
        start, end = pd.Timestamp(session["start"]), pd.Timestamp(session["end"])
        now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=end.tz)
        return pd.Timestamp(last_bar).date() == start.date() and now < end
    except Exception:
        return False


def live_session(ticker: str, now=None, bars: bool = True) -> dict | None:
    """The exchange's current, unfinished session: the live price, today's bar so far and its
    15-minute bars, in the exchange's own time. None while the exchange is closed.

    Read straight from Yahoo and never cached: the stored daily series holds finished
    sessions only. Quotes can be delayed by the exchange (often 15 minutes).
    `bars=False` skips the 15-minute bars: one request, for the dashboard's live quote."""
    try:
        source = yf.Ticker(ticker)
        daily = source.history(period="5d", interval="1d", auto_adjust=True)
    except Exception as exc:
        raise DataError(f"{ticker}: download failed ({exc})") from exc
    if daily is None or daily.empty:
        return None
    daily = daily.dropna(subset=["Close"])
    try:
        session = (source.history_metadata or {}).get("currentTradingPeriod", {}).get("regular")
    except Exception:
        session = None
    if len(daily) < 2 or not session_unfinished(daily.index[-1], session, now):
        return None

    end = pd.Timestamp(session["end"])
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=end.tz)
    if now.tzinfo is not None and end.tzinfo is not None:
        now = now.tz_convert(end.tz)
    day = pd.Timestamp(daily.index[-1]).date()
    today, previous = daily.iloc[-1], daily.iloc[-2]
    intraday = None
    if bars:
        try:
            intraday = source.history(period="1d", interval="15m", auto_adjust=True)
            intraday = intraday.dropna(subset=["Close"])
        except Exception:  # the intraday bars are a bonus; the live price is what matters
            intraday = None
    bars = [] if intraday is None else [
        {"time": idx.strftime("%H:%M"), "open": float(r.Open), "high": float(r.High),
         "low": float(r.Low), "close": float(r.Close), "volume": float(r.Volume or 0)}
        for idx, r in intraday.iterrows() if idx.date() == day
    ]
    price, prev_close = round(float(today.Close), 4), round(float(previous.Close), 4)
    return {
        "date": day.strftime("%Y-%m-%d"),
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "timezone": str(end.tz) if end.tz is not None else "",
        "price": price,
        "open": round(float(today.Open), 4), "high": round(float(today.High), 4),
        "low": round(float(today.Low), 4),
        "volume": float(today.Volume or 0),
        "prev_close": prev_close,
        "change_pct": round((price / prev_close - 1) * 100, 2),
        "minutes_left": max(0, int((end - now).total_seconds() // 60)),
        "bars": bars,
    }


SEARCH_TYPES = {"EQUITY", "ETF", "INDEX"}


def search(query: str, limit: int = 8) -> list[dict]:
    """Find Yahoo symbols by company name or code, across all exchanges."""
    try:
        quotes = yf.Search(query, max_results=limit * 2, news_count=0).quotes
    except Exception as exc:
        raise DataError(f"symbol search failed ({exc})") from exc
    found = []
    for q in quotes:
        if q.get("quoteType") not in SEARCH_TYPES or not q.get("symbol"):
            continue
        found.append({"ticker": q["symbol"], "name": q.get("longname") or q.get("shortname") or "",
                      "exchange": q.get("exchDisp") or q.get("exchange") or "",
                      "type": q["quoteType"].lower()})
    return found[:limit]


def load_prices(ticker: str) -> pd.DataFrame:
    found = db.rows(
        "SELECT date, open, high, low, close, volume FROM prices WHERE ticker = ? ORDER BY date",
        (ticker,),
    )
    return pd.DataFrame(found, columns=["date", "open", "high", "low", "close", "volume"])


INFO_FIELDS = {
    "name": ("longName", "shortName"),
    "exchange": ("fullExchangeName", "exchange"),
    "currency": ("currency",),
    "sector": ("sector",),
    "industry": ("industry",),
    "market_cap": ("marketCap",),
    "trailing_pe": ("trailingPE",),
    "forward_pe": ("forwardPE",),
    "price_to_book": ("priceToBook",),
    "dividend_yield": ("dividendYield",),
    "beta": ("beta",),
    "profit_margin": ("profitMargins",),
    "revenue_growth": ("revenueGrowth",),
    "earnings_growth": ("earningsGrowth",),
    "analyst_target_mean": ("targetMeanPrice",),
    "analyst_recommendation": ("recommendationKey",),
}


def fetch_info(ticker: str) -> dict:
    """Company profile and headline fundamentals. Best effort: missing fields are dropped."""
    try:
        raw = yf.Ticker(ticker).info or {}
    except Exception:
        return {}
    info = {}
    for key, sources in INFO_FIELDS.items():
        for src in sources:
            value = raw.get(src)
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                value = None
            if value not in (None, ""):
                info[key] = round(value, 4) if isinstance(value, float) else value
                break
    return info


def fetch_news(ticker: str, limit: int = 8) -> list[dict]:
    """Recent headlines. Best effort: returns [] on any failure."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    news = []
    for item in items:
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title")
        if not title:
            continue
        provider = content.get("provider")
        publisher = provider.get("displayName") if isinstance(provider, dict) else content.get("publisher")
        published = content.get("pubDate") or content.get("displayTime")
        if not published and content.get("providerPublishTime"):
            published = pd.Timestamp(content["providerPublishTime"], unit="s").isoformat()
        news.append({"title": str(title)[:200], "publisher": publisher,
                     "date": str(published)[:10] if published else None})
        if len(news) >= limit:
            break
    return news
