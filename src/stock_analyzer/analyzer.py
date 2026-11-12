"""Pipeline for one ticker: prices -> indicators -> model forecast -> storage."""
from __future__ import annotations

import json

import numpy as np

from . import data, db, indicators, prompt, scoring, signals
from .llm import get_provider

MIN_BARS = 60


def analyze_ticker(ticker: str, provider_name: str, cfg: dict, force: bool = False) -> dict:
    """Analyze one ticker. Returns {'status': 'done'|'skipped', ...}; raises on failure."""
    ticker = data.normalize(ticker)
    data.update_prices(ticker, cfg["history_period"])
    scoring.score_open(ticker)

    prices = data.load_prices(ticker)
    if len(prices) < MIN_BARS:
        raise data.DataError(f"{ticker}: only {len(prices)} daily bars, need {MIN_BARS}")
    run_date = prices["date"].iloc[-1]
    if not force and db.has_analysis(ticker, run_date, provider_name):
        return {"status": "skipped", "ticker": ticker, "run_date": run_date}

    snap = indicators.snapshot(indicators.compute(prices))

    info = data.fetch_info(ticker)
    stored = db.one("SELECT info_json FROM watchlist WHERE ticker = ?", (ticker,))
    if info and stored:
        db.add_ticker(ticker, info)
    elif stored:
        info = json.loads(stored["info_json"] or "{}")

    news = data.fetch_news(ticker)
    past = db.rows(
        "SELECT * FROM predictions WHERE ticker = ? AND provider = ? AND run_date < ? "
        "ORDER BY run_date DESC, horizon_days LIMIT 12",
        (ticker, provider_name, run_date),
    )

    horizons = cfg["horizons"]
    model = cfg["models"].get(provider_name, "")
    effort = cfg["efforts"].get(provider_name, "")
    provider = get_provider(provider_name, model, cfg["llm_timeout"], effort)
    text = prompt.build(ticker, info, prices, snap, news, past, horizons)
    result, raw = provider.analyze(text, prompt.schema(horizons), horizons)
    result["signal"] = signals.review(result["signal"], float(prices["close"].iloc[-1]),
                                      result["predictions"], cfg["signals"])

    due = {h: str(np.busday_offset(run_date, h, roll="forward")) for h in horizons}
    analysis_id = db.save_analysis(
        ticker, run_date, provider_name, model or "default", result, snap, raw,
        base_close=float(prices["close"].iloc[-1]), due_dates=due, effort=effort,
    )
    return {"status": "done", "ticker": ticker, "run_date": run_date,
            "analysis_id": analysis_id, "trend": result["trend"],
            "signal": (result["signal"] or {}).get("action")}
