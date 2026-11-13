"""Pipeline for one ticker: prices -> indicators -> model forecast -> storage."""
from __future__ import annotations

import json

import numpy as np

from . import config, data, db, indicators, prompt, scoring, signals
from .llm import get_provider

MIN_BARS = 60


def analyze_ticker(ticker: str, provider_name: str, cfg: dict, force: bool = False,
                   realtime: bool = False) -> dict:
    """Analyze one ticker. Returns {'status': 'done'|'skipped', ...}; raises on failure.

    `realtime` analyzes during the trading session: the indicators still come from the
    finished daily bars, and the model also gets the live price and today's bars so far.
    Such an analysis is dated on the live session, stored beside the after-close one and
    never graded. It needs an open exchange."""
    ticker = data.normalize(ticker)
    data.update_prices(ticker, cfg["history_period"])
    scoring.score_open(ticker)

    prices = data.load_prices(ticker)
    if len(prices) < MIN_BARS:
        raise data.DataError(f"{ticker}: only {len(prices)} daily bars, need {MIN_BARS}")
    live = None
    if realtime:
        live = data.live_session(ticker)
        if live is None:
            raise data.DataError(f"{ticker}: the exchange is closed. Realtime analysis needs an "
                                 "open trading session; switch Realtime off to analyze the last close.")
    run_date = live["date"] if live else prices["date"].iloc[-1]
    if not live and not force and db.has_analysis(ticker, run_date, provider_name):
        return {"status": "skipped", "ticker": ticker, "run_date": run_date}

    snap = indicators.snapshot(indicators.compute(prices))
    if live:
        snap["live"] = {k: v for k, v in live.items() if k != "bars"}
    reference = live["price"] if live else float(prices["close"].iloc[-1])

    info = data.fetch_info(ticker)
    stored = db.one("SELECT info_json FROM watchlist WHERE ticker = ?", (ticker,))
    if info and stored:
        db.add_ticker(ticker, info)
    elif stored:
        info = json.loads(stored["info_json"] or "{}")

    news = data.fetch_news(ticker)
    past = db.rows(
        "SELECT * FROM predictions WHERE ticker = ? AND provider = ? AND run_date < ? "
        "AND mode = 'daily' ORDER BY run_date DESC, horizon_days LIMIT 12",
        (ticker, provider_name, run_date),
    )

    horizons = cfg["horizons"]
    model = cfg["models"].get(provider_name, "")
    effort = cfg["efforts"].get(provider_name, "")
    provider = get_provider(provider_name, model, cfg["llm_timeout"], effort)
    text = prompt.build(ticker, info, prices, snap, news, past, horizons,
                        config.LANGUAGES[cfg.get("language", "en")], live=live)
    result, raw = provider.analyze(text, prompt.schema(horizons), horizons)
    result["signal"] = signals.review(result["signal"], reference, result["predictions"],
                                      cfg["signals"], live=bool(live))

    # During a session today's close is the first horizon; after the close it is the next one.
    first = 1 if live else 0
    due = {h: str(np.busday_offset(run_date, h - first, roll="forward")) for h in horizons}
    analysis_id = db.save_analysis(
        ticker, run_date, provider_name, model or "default", result, snap, raw,
        base_close=reference, due_dates=due, effort=effort,
        mode="realtime" if live else "daily",
    )
    return {"status": "done", "ticker": ticker, "run_date": run_date,
            "analysis_id": analysis_id, "trend": result["trend"],
            "signal": (result["signal"] or {}).get("action")}
