"""Factor scores for stock discovery. Pure functions: no network, no storage.

A candidate is scored on several pillars, each 0-100, and the pillars are combined with the
weights from config.yaml. A pillar is the mean of its components' percentile ranks among the
candidates being compared, so a score says "better than that share of the peers", never
"cheap" or "good" in absolute terms. A missing value is left out of its pillar (a pillar
with nothing left is a neutral 50) and lowers the candidate's `coverage`.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import indicators

MIN_BARS = 130          # about six months: below that the price factors mean little
FINANCIALS = "Financial Services"
MIN_SECTOR_PEERS = 4    # valuation is ranked inside the sector only with this many peers

# pillar -> [(metric, higher is better)]
LONG_PILLARS = {
    "quality": [("roe", True), ("op_margin", True), ("fcf_margin", True),
                ("debt_equity", False), ("current_ratio", True)],
    "valuation": [("fwd_earnings_yield", True), ("fcf_yield", True), ("ev_ebitda", False),
                  ("peg", False), ("price_to_book", False)],
    "growth": [("rev_growth", True), ("earn_growth", True), ("fwd_eps_growth", True),
               ("next_year_growth", True)],
    "estimates": [("eps_rev_90d", True), ("target_upside", True)],
    "trend": [("mom_12_1", True), ("trend", True)],
    "risk": [("vol_60d", False), ("beta", False), ("drawdown_6m", True)],
}
SHORT_PILLARS = {
    "momentum": [("rs_3m", True), ("ret_1m", True), ("pct_from_high", True), ("trend", True)],
    "revisions": [("eps_rev_30d", True), ("net_revisions_30d", True), ("rec_change", True)],
    "drift": [("drift", True)],
    "volume": [("volume_thrust", True), ("dollar_volume", True)],
    "catalyst": [("earnings_soon", True), ("news_recent", True)],
    "overextension": [],   # absolute penalties, see overextension()
}
PILLARS = {"short": SHORT_PILLARS, "long": LONG_PILLARS}
SECTOR_RELATIVE = {"valuation"}


def num(value) -> float | None:
    """A finite float, or None."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def pct_rank(values: dict[str, float | None], higher_is_better: bool = True) -> dict[str, float]:
    """Percentile rank 0-100 of each non-missing value among the others (ties share a rank).
    A lone value is a neutral 50. Missing values are left out of the result."""
    known = {k: v for k, v in values.items() if num(v) is not None}
    if not known:
        return {}
    if len(known) == 1:
        return {next(iter(known)): 50.0}
    ranks = pd.Series(known, dtype=float).rank(method="average", ascending=higher_is_better)
    return {k: round(float((r - 1) / (len(known) - 1) * 100), 1) for k, r in ranks.items()}


# --- price behaviour --------------------------------------------------------

def _ret(close: pd.Series, bars: int) -> float | None:
    if len(close) <= bars or not close.iloc[-bars - 1]:
        return None
    return float(close.iloc[-1] / close.iloc[-bars - 1] - 1) * 100


def price_factors(bars: pd.DataFrame, bench_close: pd.Series | None = None) -> dict | None:
    """Price-based factors from daily bars (columns close, high, low, volume; oldest first).
    Returns are in percent. `bench_close` is the home index, for relative strength.
    None if there is too little history."""
    bars = bars.dropna(subset=["close"])
    if len(bars) < MIN_BARS:
        return None
    close = bars["close"].astype(float)
    last = float(close.iloc[-1])
    sma20, sma50 = indicators.sma(close, 20).iloc[-1], indicators.sma(close, 50).iloc[-1]
    sma200 = indicators.sma(close, 200).iloc[-1] if len(close) >= 200 else np.nan
    atr = indicators.atr(bars).iloc[-1]
    volume = bars["volume"].astype(float)
    vol5, vol60 = volume.tail(5).mean(), volume.tail(60).mean()
    year = close.tail(252)
    half = close.tail(126)

    # 12-1 momentum: the past year without the latest month, which tends to reverse.
    skip = 21
    start = max(len(close) - 253, 0)
    mom = (float(close.iloc[-skip - 1] / close.iloc[start] - 1) * 100
           if len(close) > skip + 100 and close.iloc[start] else None)

    def bench(n: int) -> float | None:
        return None if bench_close is None else _ret(bench_close.dropna().astype(float), n)

    ret_1m, ret_3m, ret_5d = _ret(close, 21), _ret(close, 63), _ret(close, 5)
    ratio = float(vol5 / vol60) if vol60 else None
    trend = (int(last > sma50) + int(not np.isnan(sma200) and sma50 > sma200)
             + int(not np.isnan(sma200) and last > sma200))
    return {
        "close": round(last, 4),
        "ret_5d": _r(ret_5d), "ret_1m": _r(ret_1m), "ret_3m": _r(ret_3m),
        "ret_6m": _r(_ret(close, 126)), "mom_12_1": _r(mom),
        "rs_3m": _r(ret_3m - bench(63)) if ret_3m is not None and bench(63) is not None else None,
        "trend": trend,   # 0-3: close > SMA50, SMA50 > SMA200, close > SMA200
        "above_sma200": None if np.isnan(sma200) else bool(last > sma200),
        "pct_from_high": _r((last / float(year.max()) - 1) * 100),
        "vol_60d": _r(float(close.pct_change().tail(60).std(ddof=0)) * math.sqrt(252) * 100),
        "drawdown_6m": _r(float((half / half.cummax() - 1).min()) * 100),
        "rsi14": _r(indicators.rsi(close).iloc[-1]),
        "volume_ratio": _r(ratio),
        # Unusual volume counts for the stock when the price rose with it, against it otherwise.
        "volume_thrust": _r((ratio - 1) * (1 if (ret_5d or 0) > 0 else -1)) if ratio else None,
        "ext_atr": _r((last - float(sma20)) / float(atr)) if atr and not np.isnan(atr) else None,
    }


def _r(value, digits: int = 2) -> float | None:
    value = num(value)
    return None if value is None else round(value, digits)


def overextension(m: dict) -> float:
    """100 = not stretched. Fixed penalties: a stock is overbought by its own measure,
    whatever its peers do."""
    score = 100.0
    rsi, ext, vol = num(m.get("rsi14")), num(m.get("ext_atr")), num(m.get("vol_60d"))
    if rsi is not None:
        score -= 40 if rsi > 80 else 15 if rsi > 70 else 0
    if ext is not None:
        score -= 30 if ext > 3 else 10 if ext > 2 else 0
    if vol is not None:
        score -= 30 if vol > 60 else 15 if vol > 45 else 0
    return max(score, 0.0)


# --- fundamentals -----------------------------------------------------------

def is_financial(m: dict) -> bool:
    return m.get("sector") == FINANCIALS


def red_flags(m: dict) -> list[str]:
    """Reasons to keep a stock off both lists whatever it scores."""
    flags = []
    basics = ("roe", "op_margin", "trailing_eps", "rev_growth", "fwd_earnings_yield")
    if all(num(m.get(k)) is None for k in basics):
        return ["no fundamentals data"]
    eps, fcf = num(m.get("trailing_eps")), num(m.get("fcf_yield"))
    if eps is not None and eps < 0 and (fcf is None or fcf < 0):
        flags.append("losses and no free cash flow")
    debt = num(m.get("debt_equity"))
    if not is_financial(m) and debt is not None and debt > 300:
        flags.append(f"debt/equity {debt:.0f}%")
    return flags


def _applies(m: dict, metric: str) -> bool:
    """False where a metric says nothing about this kind of company."""
    if is_financial(m):
        # A bank's debt is its raw material and it has no free cash flow in the usual sense.
        return metric not in ("debt_equity", "current_ratio", "fcf_margin", "fcf_yield", "ev_ebitda")
    return metric != "price_to_book"   # book value says little outside financials


def _usable(m: dict, metric: str) -> float | None:
    """The metric's value, or None if it is missing or meaningless for this company."""
    value = num(m.get(metric))
    if value is None or not _applies(m, metric):
        return None
    if metric in ("ev_ebitda", "peg", "price_to_book") and value <= 0:
        return None   # negative multiples are not "cheap"
    return value


def score(candidates: dict[str, dict], category: str, weights: dict) -> dict[str, dict]:
    """Pillar scores and the weighted total for every candidate of one category.

    `candidates` maps ticker -> metrics. Returns ticker -> {'score', 'pillars', 'coverage'}.
    """
    pillars = PILLARS[category]
    tickers = list(candidates)
    by_sector: dict[str, list[str]] = {}
    for t in tickers:
        by_sector.setdefault(candidates[t].get("sector") or "", []).append(t)

    parts: dict[str, dict[str, list[float]]] = {t: {p: [] for p in pillars} for t in tickers}
    known = dict.fromkeys(tickers, 0)
    # Coverage counts only the inputs that apply to the company's kind.
    total = {t: sum(_applies(candidates[t], metric) for components in pillars.values()
                    for metric, _ in components) for t in tickers}
    for pillar, components in pillars.items():
        for metric, higher in components:
            values = {t: _usable(candidates[t], metric) for t in tickers}
            ranks = pct_rank(values, higher)
            if pillar in SECTOR_RELATIVE:
                for members in by_sector.values():
                    inside = {t: values[t] for t in members if values[t] is not None}
                    if len(inside) >= MIN_SECTOR_PEERS:
                        ranks.update(pct_rank(inside, higher))
            for t, rank in ranks.items():
                parts[t][pillar].append(rank)
                known[t] += 1

    out = {}
    weight_sum = sum(float(weights.get(p, 0)) for p in pillars) or 1.0
    for t in tickers:
        scores = {p: round(sum(v) / len(v), 1) if v else 50.0 for p, v in parts[t].items()}
        if "overextension" in pillars:
            scores["overextension"] = overextension(candidates[t])
        composite = sum(scores[p] * float(weights.get(p, 0)) for p in pillars) / weight_sum
        out[t] = {"score": round(composite, 1), "pillars": scores,
                  "coverage": round(known[t] / total[t], 2) if total[t] else 1.0}
    return out


# --- coarse ranking of the whole universe -----------------------------------

COARSE = {
    "short": [("rs_3m", 0.35), ("ret_1m", 0.25), ("trend", 0.20), ("pct_from_high", 0.10),
              ("volume_thrust", 0.10)],
    # Fundamentals lead here: a long-term candidate must not need recent momentum to get in.
    "long": [("quality_screen", 0.25), ("earnings_yield", 0.20), ("growth_screen", 0.15),
             ("mom_12_1", 0.15), ("low_vol", 0.15), ("trend", 0.10)],
}


def coarse_scores(rows: list[dict], category: str) -> dict[str, float]:
    """A first, cheap ranking of every stock in the universe from price factors and the
    screener's own fields. Ranks are taken inside each region, so a stock competes with its
    own market. Returns ticker -> score 0-100."""
    regions: dict[str, list[dict]] = {}
    for row in rows:
        regions.setdefault(row.get("region") or "", []).append(row)
    out: dict[str, float] = {}
    for members in regions.values():
        ranks = {}
        for metric, _ in COARSE[category]:
            if metric == "low_vol":
                ranks[metric] = pct_rank({r["ticker"]: r.get("vol_60d") for r in members}, False)
            elif metric.endswith("_screen"):
                ranks[metric] = {r["ticker"]: 100.0 if r.get(metric) else 0.0 for r in members}
            else:
                ranks[metric] = pct_rank({r["ticker"]: r.get(metric) for r in members})
        for r in members:
            t = r["ticker"]
            out[t] = round(sum(ranks[m].get(t, 50.0) * w for m, w in COARSE[category]), 1)
    return out


def shortlist(scored: dict[str, dict], candidates: dict[str, dict], size: int,
              max_per_sector: int) -> list[str]:
    """The best-scored tickers without red flags, at most `max_per_sector` from one sector."""
    chosen: list[str] = []
    per_sector: dict[str, int] = {}
    for t in sorted(scored, key=lambda t: -scored[t]["score"]):
        if red_flags(candidates[t]):
            continue
        sector = candidates[t].get("sector") or t   # an unknown sector caps nothing
        if per_sector.get(sector, 0) >= max_per_sector:
            continue
        per_sector[sector] = per_sector.get(sector, 0) + 1
        chosen.append(t)
        if len(chosen) >= size:
            break
    return chosen


# --- grading a pick ---------------------------------------------------------

def pick_return(close: pd.Series, base_date: str, horizon: int) -> dict | None:
    """How a pick has done since `base_date`, on closes indexed by 'YYYY-MM-DD' (oldest
    first). The return runs to the horizon's last bar once it exists, else to the latest
    bar. It is final only when a later bar exists too: the newest bar can belong to a
    session still trading. None if the series does not reach back to the pick."""
    close = close.dropna()
    at = close[close.index <= base_date]
    after = close[close.index > base_date]
    if at.empty or not float(at.iloc[-1]):
        return None
    final = len(after) > horizon
    end = after.iloc[:horizon]
    last = float(end.iloc[-1]) if len(end) else float(at.iloc[-1])
    return {"bars": min(len(after), horizon), "final": final,
            "date": str(end.index[-1]) if len(end) else str(at.index[-1]),
            "return_pct": round((last / float(at.iloc[-1]) - 1) * 100, 2)}


def return_between(close: pd.Series, start: str, end: str) -> float | None:
    """Percent change from the last close on or before `start` to the one on or before `end`."""
    close = close.dropna()
    a, b = close[close.index <= start], close[close.index <= end]
    if a.empty or b.empty or not float(a.iloc[-1]):
        return None
    return round((float(b.iloc[-1]) / float(a.iloc[-1]) - 1) * 100, 2)
