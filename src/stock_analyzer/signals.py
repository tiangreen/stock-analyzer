"""Trade signals: the model proposes buy / sell / hold, fixed rules here decide what is shown.

Signals are for placing orders by hand at a broker; nothing in this app sends an order.
The model's proposal is never trusted as is: `review` downgrades it to "hold" unless it
passes every limit in the `signals` section of config.yaml.
"""
from __future__ import annotations

import math

ACTIONS = ("buy", "sell", "hold")
PRICE_FIELDS = ("limit_price", "stop_loss", "take_profit")


def schema(horizons: list[int]) -> dict:
    """Strict JSON schema of the signal object inside the model's answer."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "horizon_days", "confidence", *PRICE_FIELDS, "reasoning"],
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "horizon_days": {"type": "integer", "enum": horizons},
            "confidence": {"type": "number"},
            "limit_price": {"type": "number"},
            "stop_loss": {"type": "number"},
            "take_profit": {"type": "number"},
            "reasoning": {"type": "string"},
        },
    }


def example(horizons: list[int]) -> dict:
    return {"action": "buy|sell|hold", "horizon_days": horizons[-1], "confidence": 0.0,
            "limit_price": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "reasoning": "1-3 sentences"}


def instructions(horizons: list[int], live: bool = False) -> str:
    """`live`: the analysis runs during the session, so the order is placed at the live price."""
    near = "the live price" if live else "today's close"
    return f"""Finally give one trade signal for a long-only retail investor who places orders by hand
(no short selling, no leverage, no derivatives):
- action: "buy" (open or add to a position), "sell" (exit or reduce a position already held) or
  "hold" (do nothing). Most days the right answer is "hold"; propose a trade only when the
  evidence converges.
- horizon_days: the horizon from {horizons} the signal is based on. It must agree with your
  forecast for that horizon.
- confidence: probability from 0 to 1 that acting on the signal beats doing nothing.
- limit_price: the limit for the order, close to {near}.
- stop_loss and take_profit: for "buy", the exit prices below and above the limit. The distance
  to take_profit should be clearly larger than the distance to stop_loss.
- Use 0 for prices that do not apply: all three for "hold", stop_loss and take_profit for "sell".
- reasoning: 1-3 sentences on why, and what would invalidate the signal."""


def parse(raw, horizons: list[int]) -> dict | None:
    """Normalize the model's signal object. None if it is missing or unusable."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action", "")).lower().strip()
    if action not in ACTIONS:
        return None

    def price(field: str) -> float | None:
        try:
            value = float(raw.get(field))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    try:
        horizon = int(raw.get("horizon_days"))
    except (TypeError, ValueError):
        horizon = None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    if confidence > 1:  # tolerate 0-100 answers
        confidence /= 100
    return {
        "action": action,
        "horizon_days": horizon if horizon in horizons else None,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        **{field: price(field) for field in PRICE_FIELDS},
        "reasoning": str(raw.get("reasoning", "")).strip()[:1000],
    }


def review(signal: dict | None, close: float, predictions: list[dict], limits: dict,
           live: bool = False) -> dict | None:
    """Apply the guard rails. `close` is the price the limit is measured against: the last
    close, or the live price when `live` is set. Returns the signal to store, with:

    action           what the dashboard shows (the proposal, or "hold" if a check failed)
    proposed_action  what the model asked for
    checks           why a proposal was downgraded (empty if it passed), as English text
    check_codes      the same reasons as codes with parameters, for the dashboard to translate
    reward_risk      (take_profit - limit) / (limit - stop_loss) for a buy
    position_pct     suggested position size as a share of the portfolio for a buy
    """
    if signal is None:
        return None
    out = {**signal, "proposed_action": signal["action"], "checks": [], "check_codes": [],
           "reward_risk": None, "position_pct": None}
    action = signal["action"]
    if action == "hold":
        out.update({field: None for field in PRICE_FIELDS})
        return out

    problems: list[str] = []
    codes: list[dict] = []

    def fail(text: str, code: str, **params) -> None:
        problems.append(text)
        codes.append({"code": code, **params})

    if signal["confidence"] < limits["min_confidence"]:
        fail(f"confidence {signal['confidence']:.0%} is below the "
             f"{limits['min_confidence']:.0%} minimum", "low_confidence",
             value=round(signal["confidence"] * 100), limit=round(limits["min_confidence"] * 100))

    horizon = signal["horizon_days"]
    forecast = next((p for p in predictions if p["horizon_days"] == horizon), None)
    if forecast is None:
        fail("the signal names no valid forecast horizon", "no_horizon")
    elif forecast["direction"] == {"buy": "down", "sell": "up"}[action]:
        fail(f"the {horizon}-day forecast points {forecast['direction']}", "forecast_against",
             horizon=horizon, direction=forecast["direction"])

    limit = signal["limit_price"]
    if limit is None:
        fail("no limit price", "no_limit")
    else:
        gap = abs(limit / close - 1) * 100
        if gap > limits["max_entry_gap_pct"]:
            fail(f"limit is {gap:.1f}% away from the {'live price' if live else 'last close'} "
                 f"(maximum {limits['max_entry_gap_pct']:g}%)",
                 "entry_gap_live" if live else "entry_gap",
                 value=round(gap, 1), limit=limits["max_entry_gap_pct"])

    if action == "sell":
        out.update(stop_loss=None, take_profit=None)
    elif limit is not None:
        stop, target = signal["stop_loss"], signal["take_profit"]
        if stop is None or target is None:
            fail("buy without a stop-loss and take-profit", "no_exits")
        elif not stop < limit < target:
            fail("prices are not ordered stop-loss < limit < take-profit", "bad_order")
        else:
            stop_pct = (limit - stop) / limit * 100
            reward_risk = (target - limit) / (limit - stop)
            out["reward_risk"] = round(reward_risk, 2)
            if stop_pct > limits["max_stop_pct"]:
                fail(f"stop-loss is {stop_pct:.1f}% below the limit "
                     f"(maximum {limits['max_stop_pct']:g}%)", "stop_too_far",
                     value=round(stop_pct, 1), limit=limits["max_stop_pct"])
            if reward_risk < limits["min_reward_risk"]:
                fail(f"reward/risk {reward_risk:.2f} is below the "
                     f"{limits['min_reward_risk']:g} minimum", "low_reward_risk",
                     value=round(reward_risk, 2), limit=limits["min_reward_risk"])
            # Size so that hitting the stop costs risk_per_trade_pct of the portfolio.
            out["position_pct"] = round(
                min(limits["max_position_pct"], limits["risk_per_trade_pct"] / stop_pct * 100), 1)

    if problems:
        out.update(action="hold", checks=problems, check_codes=codes, position_pct=None)
    return out


def outcome(signal: dict, base_close: float, bars: list[dict]) -> dict:
    """What followed a buy or sell signal, judged on the daily bars after it.

    `bars` are the sessions after the signal date, oldest first, each with high, low, close.
    A buy fills on the first day the low reaches the limit, then ends at the stop-loss, the
    take-profit or the close of the horizon's last day. When one bar touches both exits the
    stop is assumed, and a stop is assumed to fill at its price (a gap can fill it lower).
    A sell is "right" if the close after the horizon is below the close on the signal date.
    Returns {'status', 'return_pct'}; return_pct is None while nothing can be measured.
    """
    horizon = signal.get("horizon_days") or 0
    window = bars[:horizon]
    complete = len(bars) >= horizon > 0

    if signal["action"] == "sell":
        if not complete:
            return {"status": "pending", "return_pct": None}
        change = (window[-1]["close"] / base_close - 1) * 100
        return {"status": "right" if change < 0 else "wrong", "return_pct": round(change, 2)}

    limit, stop, target = (signal[field] for field in PRICE_FIELDS)
    filled = False
    for bar in window:
        if not filled:
            if bar["low"] > limit:
                continue
            filled = True  # the target is not checked on the fill day: the high may precede the fill
        elif bar["high"] >= target and bar["low"] > stop:
            return {"status": "target", "return_pct": round((target / limit - 1) * 100, 2)}
        if bar["low"] <= stop:
            return {"status": "stopped", "return_pct": round((stop / limit - 1) * 100, 2)}
    if not complete:
        return {"status": "open" if filled else "pending", "return_pct": None}
    if not filled:
        return {"status": "not filled", "return_pct": None}
    return {"status": "expired", "return_pct": round((window[-1]["close"] / limit - 1) * 100, 2)}


def summarize(history: list[dict]) -> dict:
    """Counts and average return of finished buy signals (each item carries an 'outcome')."""
    done = [h["outcome"] for h in history
            if h["signal"]["action"] == "buy" and h["outcome"]["return_pct"] is not None]
    wins = [o for o in done if o["return_pct"] > 0]
    sells = [h["outcome"] for h in history
             if h["signal"]["action"] == "sell" and h["outcome"]["status"] in ("right", "wrong")]
    return {
        "buys_finished": len(done),
        "buys_won": len(wins),
        "buy_avg_return_pct": round(sum(o["return_pct"] for o in done) / len(done), 2) if done else None,
        "sells_finished": len(sells),
        "sells_right": sum(o["status"] == "right" for o in sells),
    }
