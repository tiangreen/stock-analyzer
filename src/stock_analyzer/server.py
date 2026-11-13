"""FastAPI app: JSON API plus the static dashboard."""
from __future__ import annotations

import math
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (analyzer, config, data, db, fx, indicators, runner, scheduler, scoring, signals,
               tradingview)
from .llm import PROVIDERS, LLMError
from .llm.base import cancel_login, submit_login_code

STATIC = Path(__file__).parent / "static"
TICKER_RE = re.compile(r"[A-Z0-9^][A-Z0-9.\-=^]{0,14}")
CHART_SERIES = ("sma20", "sma50", "sma200", "bb_upper", "bb_lower", "rsi14",
                "macd", "macd_signal", "macd_hist")

_single_lock = threading.Lock()
_single_errors: dict[str, str] = {}
_single_active: set[str] = set()


def _refresh_prices() -> None:
    """Bring cached prices up to date so charts are current before the first run."""
    period = config.load()["history_period"]
    for w in db.watchlist():
        try:
            data.update_prices(w["ticker"], period)
            if not w["name"]:
                info = data.fetch_info(w["ticker"])
                if info:
                    db.add_ticker(w["ticker"], info)
        except Exception:
            continue  # offline or bad symbol: the run log reports it later


@asynccontextmanager
async def lifespan(app: FastAPI):
    runner.mark_interrupted()
    runner.seed_watchlist(config.load())
    threading.Thread(target=_refresh_prices, daemon=True, name="price-refresh").start()
    scheduler.start()
    yield
    scheduler.stop()
    for name in PROVIDERS:
        cancel_login(name)


app = FastAPI(title="Stock Analyzer", lifespan=lifespan)


def _ticker(raw: str) -> str:
    ticker = data.normalize(raw)
    if not TICKER_RE.fullmatch(ticker):
        raise HTTPException(400, "Invalid ticker symbol")
    return ticker


def _provider(name: str | None) -> str:
    name = name or config.load()["provider"]
    if name not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{name}'")
    return name


class TickerIn(BaseModel):
    ticker: str


class ScheduleIn(BaseModel):
    enabled: bool
    time: str
    weekdays_only: bool = True


class SettingsIn(BaseModel):
    provider: str | None = None
    currency: str | None = None
    language: str | None = None


# --- status and settings --------------------------------------------------

@app.get("/api/status")
def status():
    cfg = config.load()
    last = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    return {
        "provider": cfg["provider"],
        "currency": cfg["display_currency"],
        "currencies": list(config.DISPLAY_CURRENCIES),
        "language": cfg["language"],
        "languages": list(config.LANGUAGES),
        "providers": {n: {"installed": cls().available(), "model": cfg["models"].get(n) or "default"}
                      for n, cls in PROVIDERS.items()},
        "horizons": cfg["horizons"],
        "running": runner.is_running(),
        "analyzing": sorted(_single_active),
        "errors": dict(_single_errors),
        "last_run": last,
        "schedule": {**cfg["schedule"], "next_run": scheduler.next_run()},
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    if body.provider is not None:
        db.set_setting("provider", _provider(body.provider))
    if body.currency is not None:
        if body.currency.upper() not in config.DISPLAY_CURRENCIES:
            raise HTTPException(400, f"Currency must be one of {config.DISPLAY_CURRENCIES}")
        db.set_setting("display_currency", body.currency.upper())
    if body.language is not None:
        if body.language.lower() not in config.LANGUAGES:
            raise HTTPException(400, f"Language must be one of {tuple(config.LANGUAGES)}")
        db.set_setting("language", body.language.lower())
    cfg = config.load()
    return {"provider": cfg["provider"], "currency": cfg["display_currency"],
            "language": cfg["language"]}


@app.get("/api/fx")
def get_fx(to: str | None = None):
    """Rates from each watchlist currency into the display currency (None if unknown)."""
    to = (to or config.load()["display_currency"]).upper()
    if to not in config.DISPLAY_CURRENCIES:
        raise HTTPException(400, f"Currency must be one of {config.DISPLAY_CURRENCIES}")
    return {"to": to, "rates": fx.rates([w["currency"] for w in db.watchlist()], to)}


@app.put("/api/schedule")
def put_schedule(body: ScheduleIn):
    try:
        scheduler.parse_time(body.time)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    schedule = body.model_dump()
    db.set_setting("schedule", schedule)
    scheduler.apply(schedule)
    return {**schedule, "next_run": scheduler.next_run()}


# --- provider sign-in -----------------------------------------------------

AUTH_TTL = 60
_auth_cache: dict[str, tuple[float, dict]] = {}


def _auth(name: str, refresh: bool = False) -> dict:
    cached = _auth_cache.get(name)
    if cached and not refresh and time.monotonic() - cached[0] < AUTH_TTL:
        return cached[1]
    status = PROVIDERS[name]().login_status()
    _auth_cache[name] = (time.monotonic(), status)
    return status


@app.get("/api/providers")
def providers(refresh: bool = False):
    """Sign-in state plus the model and effort choices of every provider."""
    cfg = config.load()
    out = {}
    for name, cls in PROVIDERS.items():
        provider = cls()
        out[name] = {
            **_auth(name, refresh),
            "active": cfg["provider"] == name,
            "model": cfg["models"].get(name, ""),
            "effort": cfg["efforts"].get(name, ""),
            "models": provider.options(),
            "custom_efforts": provider.efforts_for(""),  # offered for a custom model name
        }
    return out


class ChoiceIn(BaseModel):
    model: str = ""
    effort: str = ""


@app.put("/api/providers/{name}")
def put_provider(name: str, body: ChoiceIn):
    """Save the model and effort this provider uses for analysis ('' = default)."""
    name = _provider(name)
    model, effort = body.model.strip(), body.effort.strip()
    try:
        PROVIDERS[name]().check_choice(model, effort)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from None
    saved = db.get_settings()
    db.set_setting("models", {**saved.get("models", {}), name: model})
    db.set_setting("efforts", {**saved.get("efforts", {}), name: effort})
    return {"model": model, "effort": effort}


@app.post("/api/providers/{name}/login")
def provider_login(name: str, request: Request):
    """Start the provider's browser sign-in and return its authorize page URL."""
    # Only the dashboard itself may start a sign-in, not other sites open in the browser.
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin request refused")
    provider = PROVIDERS[_provider(name)]()
    try:
        started = provider.start_login()
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from None
    _auth_cache.pop(name, None)
    return {**started, "login_command": provider.login_command()}


class CodeIn(BaseModel):
    code: str


@app.post("/api/providers/{name}/login/code")
def provider_login_code(name: str, body: CodeIn):
    """Forward the one-time code shown by the authorize page to the waiting CLI."""
    name = _provider(name)
    if not body.code.strip() or len(body.code) > 2000:
        raise HTTPException(400, "Paste the code shown on the authorize page")
    try:
        submit_login_code(name, body.code)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from None
    _auth_cache.pop(name, None)
    return {"submitted": True}


# --- watchlist ------------------------------------------------------------

@app.get("/api/watchlist")
def get_watchlist():
    out = []
    for w in db.watchlist():
        closes = [r["close"] for r in db.rows(
            "SELECT close FROM (SELECT date, close FROM prices WHERE ticker = ? "
            "ORDER BY date DESC LIMIT 30) ORDER BY date", (w["ticker"],))]
        latest = db.latest_analysis(w["ticker"])
        item = {
            "ticker": w["ticker"], "name": w["name"], "exchange": w["exchange"],
            "currency": w["currency"], "spark": closes,
            "close": closes[-1] if closes else None,
            "change_pct": (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None,
            "trend": None, "run_date": None, "provider": None, "predictions": [],
        }
        if latest:
            item.update(trend=latest["trend"], run_date=latest["run_date"],
                        provider=latest["provider"],
                        predictions=[{k: p[k] for k in ("horizon_days", "direction", "confidence")}
                                     for p in latest["predictions"]])
        out.append(item)
    return out


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if len(q) < 2 or len(q) > 60:
        return []
    try:
        return data.search(q)
    except data.DataError as exc:
        raise HTTPException(502, str(exc)) from None


@app.post("/api/watchlist")
def add_to_watchlist(body: TickerIn):
    ticker = _ticker(body.ticker)
    try:
        data.update_prices(ticker, config.load()["history_period"])
    except data.DataError as exc:
        raise HTTPException(404, str(exc)) from None
    db.add_ticker(ticker, data.fetch_info(ticker))
    return {"ticker": ticker}


@app.delete("/api/watchlist/{ticker}")
def remove_from_watchlist(ticker: str):
    db.remove_ticker(_ticker(ticker))
    return {"removed": ticker}


# --- stock detail ---------------------------------------------------------

def _series(frame, column: str) -> list[dict]:
    return [{"time": d, "value": v} for d, v in zip(frame["date"], frame[column])
            if v is not None and not (isinstance(v, float) and math.isnan(v))]


@app.get("/api/stocks/{ticker}")
def stock(ticker: str):
    ticker = _ticker(ticker)
    entry = db.one("SELECT ticker, name, exchange, currency, info_json FROM watchlist "
                   "WHERE ticker = ?", (ticker,))
    prices = data.load_prices(ticker)
    if entry is None:
        raise HTTPException(404, "Ticker is not on the watchlist")
    payload = {"ticker": ticker, "name": entry["name"], "exchange": entry["exchange"],
               "currency": entry["currency"], "tradingview_url": tradingview.chart_url(ticker),
               "candles": [], "volume": [], "series": {},
               "snapshot": None, "analyses": db.analyses_for(ticker, 40),
               "signal_limits": config.load()["signals"]}
    if len(prices) >= 2:
        ind = indicators.compute(prices)
        payload["candles"] = [
            {"time": r.date, "open": r.open, "high": r.high, "low": r.low, "close": r.close}
            for r in prices.itertuples()]
        payload["volume"] = [{"time": r.date, "value": r.volume, "up": r.close >= r.open}
                             for r in prices.itertuples()]
        payload["series"] = {c: _series(ind, c) for c in CHART_SERIES}
        payload["snapshot"] = indicators.snapshot(ind)
    return payload


# --- running analyses -----------------------------------------------------

def _analyze_one(ticker: str, provider: str, realtime: bool = False) -> None:
    try:
        analyzer.analyze_ticker(ticker, provider, config.load(), force=True, realtime=realtime)
        _single_errors.pop(ticker, None)
    except Exception as exc:
        _single_errors[ticker] = str(exc)
    finally:
        _single_active.discard(ticker)


@app.post("/api/analyze/{ticker}")
def analyze(ticker: str, provider: str | None = None, realtime: bool = False):
    """Analyze one stock now. `realtime` uses the live price of the session in progress
    (fails while the exchange is closed) and stores an ungraded analysis of its own."""
    ticker, provider = _ticker(ticker), _provider(provider)
    with _single_lock:
        if ticker in _single_active or runner.is_running():
            raise HTTPException(409, "An analysis is already in progress")
        _single_active.add(ticker)
    _single_errors.pop(ticker, None)
    threading.Thread(target=_analyze_one, args=(ticker, provider, realtime), daemon=True).start()
    return {"started": ticker, "provider": provider, "realtime": realtime}


@app.post("/api/run")
def run(provider: str | None = None, force: bool = False):
    provider = _provider(provider)
    if _single_active or not runner.start_background(trigger="manual", provider=provider,
                                                     force=force):
        raise HTTPException(409, "An analysis is already in progress")
    return {"started": True, "provider": provider}


@app.get("/api/runs")
def runs():
    return db.rows("SELECT * FROM runs ORDER BY id DESC LIMIT 20")


@app.get("/api/accuracy")
def accuracy():
    return scoring.accuracy()


@app.get("/api/signals")
def get_signals():
    """Latest trade signal per watchlist stock and model, plus past buy/sell signals and what
    followed. The models' rows for one stock stay together, so they can be compared."""
    listed = {w["ticker"]: w for w in db.watchlist()}
    latest: dict[tuple[str, str], dict] = {}
    history = []
    for row in db.signals():
        entry = listed.get(row["ticker"])
        if entry is None or row["close"] is None:
            continue
        row.update(name=entry["name"], currency=entry["currency"])
        latest.setdefault((row["ticker"], row["provider"]), row)
        # Realtime signals are shown as the latest view but never enter the track record.
        if row["signal"]["action"] != "hold" and row["mode"] == "daily":
            bars = db.bars_after(row["ticker"], row["run_date"], row["signal"]["horizon_days"])
            history.append({**row, "outcome": signals.outcome(row["signal"], row["close"], bars)})
    order = {"buy": 0, "sell": 1, "hold": 2}

    def rank(row: dict) -> tuple:
        return order[row["signal"]["action"]], -row["signal"]["confidence"]

    # Stocks are ordered by their most actionable signal; within a stock, newest model first.
    best: dict[str, tuple] = {}
    for row in latest.values():
        best[row["ticker"]] = min(best.get(row["ticker"], rank(row)), rank(row))
    return {
        "latest": sorted(latest.values(), key=lambda r: (best[r["ticker"]], r["ticker"])),
        "history": history,
        "summary": signals.summarize(history),
        "limits": config.load()["signals"],
    }


# --- static dashboard -----------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn

    server = config.load()["server"]
    print(f"Dashboard: http://{server['host']}:{server['port']}")
    uvicorn.run(app, host=server["host"], port=int(server["port"]), log_level="warning")


if __name__ == "__main__":
    main()
