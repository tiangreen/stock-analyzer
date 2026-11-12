"""Grades matured forecasts against the prices that actually followed."""
from __future__ import annotations

import math

from . import db

FLAT_BAND_1D = 0.005


def flat_band(horizon_days: int) -> float:
    """Return below which a move counts as 'flat': 0.5% for one day, widening with sqrt(time)."""
    return FLAT_BAND_1D * math.sqrt(max(horizon_days, 1))


def actual_direction(ret: float, horizon_days: int) -> str:
    band = flat_band(horizon_days)
    return "up" if ret > band else "down" if ret < -band else "flat"


def grade(prediction: dict, actual_close: float) -> dict:
    ret = actual_close / prediction["base_close"] - 1
    hit = actual_direction(ret, prediction["horizon_days"]) == prediction["direction"]
    in_range = prediction["target_low"] <= actual_close <= prediction["target_high"]
    return {"actual_close": actual_close, "actual_return": ret,
            "outcome": "hit" if hit else "miss", "in_range": int(in_range)}


def score_open(ticker: str | None = None) -> int:
    """Grade every ungraded forecast whose horizon has elapsed in trading bars. Returns count."""
    sql = "SELECT * FROM predictions WHERE outcome IS NULL"
    params: tuple = ()
    if ticker:
        sql += " AND ticker = ?"
        params = (ticker,)
    graded = 0
    for p in db.rows(sql, params):
        # The Nth bar after the forecast date: counts real sessions, so exchange holidays
        # are handled without a calendar.
        bar = db.one(
            "SELECT date, close FROM prices WHERE ticker = ? AND date > ? "
            "ORDER BY date LIMIT 1 OFFSET ?",
            (p["ticker"], p["run_date"], p["horizon_days"] - 1),
        )
        if not bar:
            continue
        g = grade(p, bar["close"])
        db.execute(
            "UPDATE predictions SET actual_date = ?, actual_close = ?, actual_return = ?, "
            "outcome = ?, in_range = ? WHERE id = ?",
            (bar["date"], g["actual_close"], g["actual_return"], g["outcome"], g["in_range"],
             p["id"]),
        )
        graded += 1
    return graded


def accuracy() -> dict:
    """Hit rates by provider and horizon, and by provider and ticker."""
    select = (
        "COUNT(*) AS graded, SUM(outcome = 'hit') AS hits, SUM(in_range) AS in_range, "
        "AVG(confidence) AS avg_confidence FROM predictions WHERE outcome IS NOT NULL"
    )
    by_horizon = db.rows(
        f"SELECT provider, horizon_days, {select} GROUP BY provider, horizon_days "
        "ORDER BY provider, horizon_days")
    by_ticker = db.rows(
        f"SELECT provider, ticker, {select} GROUP BY provider, ticker ORDER BY ticker, provider")
    pending = db.one("SELECT COUNT(*) AS n FROM predictions WHERE outcome IS NULL")["n"]
    for row in by_horizon + by_ticker:
        row["hit_rate"] = row["hits"] / row["graded"]
        row["range_rate"] = row["in_range"] / row["graded"]
    return {"by_horizon": by_horizon, "by_ticker": by_ticker, "pending": pending}
