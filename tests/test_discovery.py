import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_analyzer import config, db, discovery, runner, scheduler, server
from stock_analyzer.llm import LLMError

LIMITS = config.DEFAULTS["discovery"]
USD = {"USD": 1.0, "EUR": 1.1, "GBP": 1.3}.get
TODAY = date(2026, 11, 16)


def quote(symbol, name, price, volume, cap, **extra):
    return {"symbol": symbol, "longName": name, "quoteType": "EQUITY", "currency": "EUR",
            "regularMarketPrice": price, "averageDailyVolume3Month": volume, "marketCap": cap,
            "trailingPE": 20.0, **extra}


# --- universe ---------------------------------------------------------------

def test_cross_listings_are_dropped_by_their_turnover():
    quotes = [quote("NVD.DE", "NVIDIA Corporation", 150, 300_000, 4e12),   # 1e-5 a day
              quote("SAP.DE", "SAP SE", 210, 2_000_000, 2.4e11)]           # 1.7e-3 a day
    rows = discovery.universe_rows(quotes, "de", USD, LIMITS)
    assert [r["ticker"] for r in rows] == ["SAP.DE"]
    assert rows[0]["market_cap_usd"] == pytest.approx(2.64e11)
    assert rows[0]["earnings_yield"] == 5.0


def test_thinly_traded_and_non_equity_listings_are_dropped():
    quotes = [quote("TINY.DE", "Tiny AG", 10, 20_000, 2e9),                # 220k USD a day
              quote("FUND.DE", "Some Fund", 100, 5_000_000, 5e9, quoteType="ETF"),
              quote("NONAME.DE", None, 100, 5_000_000, 5e9)]
    assert discovery.universe_rows(quotes, "de", USD, LIMITS) == []


def test_london_prices_in_pence_are_compared_with_a_cap_in_pounds():
    hsbc = quote("HSBA.L", "HSBC Holdings plc", 900, 25_000_000, 1.6e11, currency="GBp")
    row = discovery.universe_rows([hsbc], "gb", USD, LIMITS)[0]
    assert row["turnover"] == pytest.approx(900 / 100 * 25e6 / 1.6e11)
    assert row["dollar_volume"] == pytest.approx(900 / 100 * 25e6 * 1.3)


def test_share_classes_collapse_to_the_most_traded_line():
    quotes = [quote("GOOGL", "Alphabet Inc.", 200, 30e6, 2.4e12, currency="USD"),
              quote("GOOG", "Alphabet Inc.", 201, 20e6, 2.4e12, currency="USD"),
              quote("MSFT", "Microsoft Corporation", 500, 25e6, 3.8e12, currency="USD")]
    rows = discovery.universe_rows(quotes, "us", USD, LIMITS)
    assert [r["ticker"] for r in rows] == ["MSFT", "GOOGL"]   # largest first


def test_a_loss_making_company_ranks_below_profitable_ones_on_earnings_yield():
    loser = quote("LOSS.DE", "Loss AG", 100, 5e6, 5e9, trailingPE=None, epsTrailingTwelveMonths=-2)
    assert discovery.universe_rows([loser], "de", USD, LIMITS)[0]["earnings_yield"] < 0


def test_an_open_session_is_recognized():
    stamp = pd.Timestamp("2026-11-16 15:00", tz="Europe/Berlin").timestamp()
    q = {"marketState": "REGULAR", "regularMarketTime": stamp, "exchangeTimezoneName": "Europe/Berlin"}
    assert discovery._open_session(q) == "2026-11-16"
    assert discovery._open_session({**q, "marketState": "CLOSED"}) is None


# --- metrics ----------------------------------------------------------------

ROW = {"ticker": "ACME", "name": "Acme Inc.", "region": "us", "currency": "USD",
       "market_cap_usd": 5e10, "dollar_volume": 2e8}
DEEP = {
    "info": {"sector": "Technology", "industry": "Software", "returnOnEquity": 0.25,
             "operatingMargins": 0.3, "totalRevenue": 1e10, "freeCashflow": 2e9, "marketCap": 5e10,
             "debtToEquity": 40.0, "forwardPE": 20.0, "trailingEps": 4.0, "forwardEps": 5.0,
             "enterpriseToEbitda": 15.0, "numberOfAnalystOpinions": 12, "targetMeanPrice": 120.0,
             "currentPrice": 100.0, "financialCurrency": "USD",
             "earningsTimestamp": pd.Timestamp("2026-10-27 12:00").timestamp()},
    "eps_trend": {"0q": {"current": 1.10, "30daysAgo": 1.00, "90daysAgo": 1.00},
                  "0y": {"current": 4.40, "30daysAgo": 4.00, "90daysAgo": 4.00},
                  "+1y": {"current": 5.50, "30daysAgo": 5.20, "90daysAgo": 5.00}},
    "eps_revisions": {"0q": {"upLast30days": 6, "downLast30days": 2},
                      "0y": {"upLast30days": 8, "downLast30Days": 0}},
    "earnings_history": {"2026-03-31": {"surprisePercent": -0.02},
                         "2026-06-30": {"surprisePercent": 0.05},
                         "2026-09-30": {"surprisePercent": 0.10}},
    "recommendations": [{"period": "0m", "strongBuy": 4, "buy": 4, "hold": 2, "sell": 0, "strongSell": 0},
                        {"period": "-1m", "strongBuy": 3, "buy": 3, "hold": 4, "sell": 0, "strongSell": 0}],
    "calendar": {"Earnings Date": [date(2026, 11, 30)]},
    "news": [{"title": "Acme raises guidance", "publisher": "Wire", "date": "2026-11-14"},
             {"title": "Old story", "publisher": "Wire", "date": "2026-09-01"}],
}


def acme(deep=DEEP, row=ROW, convert=lambda a, b: 1.0):
    return discovery.metrics(row, {"close": 100.0, "rsi14": 55.0}, deep, TODAY, 20, convert)


def test_metrics_from_fundamentals():
    m = acme()
    assert (m["roe"], m["op_margin"], m["fcf_margin"], m["fcf_yield"]) == (25.0, 30.0, 20.0, 4.0)
    assert m["fwd_earnings_yield"] == 5.0 and m["fwd_eps_growth"] == 25.0
    assert m["target_upside"] == 20.0 and m["ev_ebitda"] == 15.0
    assert m["sector"] == "Technology" and m["close"] == 100.0


def test_metrics_from_analyst_estimates():
    m = acme()
    assert m["eps_rev_30d"] == 10.0          # both the quarter and the year were raised 10%
    assert m["eps_rev_90d"] == 10.0          # next year: 5.00 -> 5.50
    assert m["next_year_growth"] == 25.0     # 4.40 -> 5.50
    assert m["net_revisions_30d"] == 75.0    # 14 up, 2 down
    assert m["buy_share"] == 80.0 and m["rec_change"] == 20.0


def test_earnings_surprise_fades_with_time():
    m = acme()
    assert m["last_surprise"] == 10.0 and m["beats_4q"] == 2
    assert m["days_since_report"] == 20
    assert m["drift"] == 7.5                 # 10% x (1 - 20/80)
    stale = {**DEEP, "info": {**DEEP["info"],
                              "earningsTimestamp": pd.Timestamp("2026-06-01").timestamp()}}
    assert acme(stale)["drift"] == 2.5       # never less than a quarter of it


def test_upcoming_earnings_and_fresh_headlines():
    m = acme()
    assert m["days_to_earnings"] == 14 and m["earnings_soon"] == 1
    assert m["news_recent"] == 1
    later = {**DEEP, "calendar": {"Earnings Date": [date(2027, 2, 1)]}}
    assert acme(later)["earnings_soon"] == 0


def test_statements_in_another_currency_are_converted_or_left_out():
    # Listed in pence, reports in dollars: 2bn USD of cash flow against a cap in pounds.
    row = {**ROW, "currency": "GBp"}
    m = acme(row=row, convert=lambda a, b: {("USD", "GBP"): 0.8}[(a, b)])
    assert m["fcf_yield"] == pytest.approx(2e9 * 0.8 / 5e10 * 100)
    assert m["ev_ebitda"] is None and m["price_to_book"] is None
    assert m["fwd_earnings_yield"] == 5.0    # Yahoo's own ratio is still used


def test_metrics_survive_a_stock_without_any_data():
    m = acme(deep={"info": None, "news": []})
    assert m["roe"] is None and m["drift"] is None and m["days_to_earnings"] is None
    assert m["news_recent"] == 0 and m["close"] == 100.0


def test_too_few_analysts_give_no_target():
    few = {**DEEP, "info": {**DEEP["info"], "numberOfAnalystOpinions": 3}}
    assert acme(few)["target_upside"] is None


# --- regime -----------------------------------------------------------------

def index_bars(closes):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range("2025-06-01", periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"close": closes, "high": closes, "low": closes, "volume": 0.0}, index=dates)


def test_regime_labels():
    rising, falling = index_bars(np.linspace(80, 100, 260)), index_bars(np.linspace(100, 80, 260))
    calm = {"^GSPC": rising, "^GDAXI": rising, "^VIX": index_bars(np.full(260, 15.0)),
            "^TNX": index_bars(np.linspace(4.0, 4.5, 260))}
    market = discovery.regime(calm, ["us", "de"])
    assert market["label"] == "risk-on" and market["vix"] == 15.0
    assert market["us_10y_yield"] == 4.5 and market["us_10y_change_3m"] > 0
    assert [i["index"] for i in market["indexes"]] == ["S&P 500", "DAX"]
    assert discovery.regime({**calm, "^GSPC": falling, "^GDAXI": falling}, ["us", "de"])["label"] == "risk-off"
    assert discovery.regime({**calm, "^VIX": index_bars(np.full(260, 35.0))}, ["us"])["label"] == "risk-off"
    assert discovery.regime({**calm, "^GDAXI": falling}, ["us", "de"])["label"] == "neutral"
    assert discovery.regime({}, ["us"])["label"] == "neutral"


# --- the model's answer -----------------------------------------------------

def pick(ticker, conviction=0.7, **extra):
    return {"ticker": ticker, "conviction": conviction, "thesis": "Because.", "catalysts": ["a"],
            "risks": ["b"], "invalidation": "c", "entry_note": "d", **extra}


def test_schema_is_strict_everywhere():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
            for child in node["properties"].values():
                walk(child)
        elif node.get("type") == "array":
            walk(node["items"])
    schema = discovery.schema(["AAA", "BBB"])
    walk(schema)
    assert schema["properties"]["picks"]["items"]["properties"]["ticker"]["enum"] == ["AAA", "BBB"]


def test_answer_is_normalized():
    answer = discovery.validate({
        "market_view": " Calm. ",
        "picks": [pick("aaa", 72), pick("ZZZ"), "junk"],
        "passed_over": [{"ticker": "BBB", "reason": "pricey"}, {"ticker": "QQQ", "reason": "?"}],
    }, ["AAA", "BBB"])
    assert answer["market_view"] == "Calm."
    assert [(p["ticker"], p["conviction"]) for p in answer["picks"]] == [("AAA", 0.72)]
    assert answer["unknown"] == ["ZZZ"]
    assert answer["passed_over"] == [{"ticker": "BBB", "reason": "pricey"}]


def test_an_empty_list_of_picks_is_a_valid_answer():
    assert discovery.validate({"market_view": "Nothing convinces.", "picks": []}, ["AAA"])["picks"] == []


def test_unusable_answers_are_rejected():
    with pytest.raises(LLMError):
        discovery.validate({"market_view": "x"}, ["AAA"])
    with pytest.raises(LLMError):
        discovery.validate({"picks": [pick("AAA", thesis=" ")]}, ["AAA"])


# --- guard rails ------------------------------------------------------------

SECTORS = {"A": {"sector": "Technology"}, "B": {"sector": "Technology"},
           "C": {"sector": "Technology"}, "D": {"sector": "Energy"}, "E": {"sector": None},
           "F": {"sector": "Utilities"}, "G": {"sector": "Healthcare"}, "H": {"sector": "Industrials"}}


def test_review_keeps_the_strongest_and_explains_the_rest():
    picks = [pick("C", 0.6), pick("A", 0.9), pick("B", 0.8), pick("D", 0.4), pick("A", 0.55)]
    kept, dropped = discovery.review(picks, SECTORS, LIMITS)
    assert [p["ticker"] for p in kept] == ["A", "B"]
    assert {d["ticker"]: d["code"] for d in dropped} == {"C": "sector_cap", "D": "low_conviction",
                                                          "A": "duplicate"}
    assert dropped[-1]["value"] == 40 and dropped[-1]["limit"] == 50


def test_review_caps_the_number_of_picks():
    picks = [pick(t, 0.9 - i / 100) for i, t in enumerate("ADEFGH")]
    kept, dropped = discovery.review(picks, SECTORS, LIMITS)
    assert [p["ticker"] for p in kept] == list("ADEFG")
    assert dropped == [{"ticker": "H", "code": "too_many", "limit": 5}]


# --- prompt -----------------------------------------------------------------

def test_prompt_carries_the_evidence_and_the_rules():
    m = acme()
    listed = [{"ticker": "ACME", "name": "Acme Inc.", "region": "us", "currency": "USD",
               "rank": 1, "score": 81.5, "pillars": {"quality": 90.0}, "coverage": 0.95}]
    market = {"label": "risk-on", "indexes": [], "vix": 15.0}
    text = discovery.build_prompt("long", listed, {"ACME": m}, market, LIMITS, "2026-11-16")
    assert "### 1. ACME - Acme Inc. (S&P 500 market, Technology / Software, trades in USD)" in text
    assert '"roe": 25.0' in text and "risk-on" in text and "Acme raises guidance" in text
    assert "untrusted" in text and "no more than 2 stocks from one sector" in text.replace("\n", " ")
    assert "6 to 24 months" in text and "120 trading days" in text
    assert "German" not in text
    short = discovery.build_prompt("short", listed, {"ACME": m}, market, LIMITS, "2026-11-16", "German")
    assert "1 to 4 weeks" in short and "in German" in short and "20 trading days" in short
    assert '"summary"' not in text and '"headlines"' not in text   # shown as text, not twice


# --- a whole run ------------------------------------------------------------

class FakeProvider:
    """Recommends the two best-ranked stocks of whatever shortlist it is shown."""
    prompts: list[str] = []

    def ask(self, prompt, schema, check):
        FakeProvider.prompts.append(prompt)
        tickers = schema["properties"]["picks"]["items"]["properties"]["ticker"]["enum"]
        answer = {"market_view": "Steady.", "picks": [pick(tickers[0], 0.8), pick(tickers[1], 0.3)],
                  "passed_over": [{"ticker": tickers[-1], "reason": "weakest"}]}
        return check(json.loads(json.dumps(answer))), "raw"


def fake_market(monkeypatch, tickers=("AAA", "BBB", "CCC", "DDD")):
    universe = [{"ticker": t, "name": f"{t} Corp", "region": "us", "currency": "USD",
                 "market_cap_usd": 1e10, "market_cap": 1e10, "dollar_volume": 1e8, "turnover": 1e-3,
                 "earnings_yield": 5.0, "open_session": None, "quality_screen": i % 2 == 0,
                 "growth_screen": False} for i, t in enumerate(tickers)]
    dates = pd.bdate_range(end="2026-11-13", periods=300).strftime("%Y-%m-%d")

    def history(symbols, start=None):
        out = {}
        for i, s in enumerate(symbols):
            closes = np.linspace(50, 100 + 10 * i, 300)
            out[s] = pd.DataFrame({"close": closes, "high": closes * 1.01, "low": closes * 0.99,
                                   "volume": 1000.0}, index=dates)
        return out

    def deep(symbols):
        return {s: {**DEEP, "info": {**DEEP["info"], "returnOnEquity": 0.1 + 0.05 * i,
                                     "sector": f"Sector {i}"}} for i, s in enumerate(symbols)}

    monkeypatch.setattr(discovery, "build_universe", lambda cfg, note=None: universe)
    monkeypatch.setattr(discovery, "fetch_history", history)
    monkeypatch.setattr(discovery, "fetch_deep_all", deep)
    monkeypatch.setattr(discovery.fx, "rate", lambda a, b: 1.0)
    monkeypatch.setattr(discovery, "get_provider", lambda *a, **k: FakeProvider())
    FakeProvider.prompts = []


def test_a_run_stores_picks_shortlists_and_the_regime(temp_db, monkeypatch):
    fake_market(monkeypatch)
    lines = []
    discovery_id = discovery.run("claude", config.load(), lambda line, progress=None: lines.append(line))
    found = db.latest_discovery()
    assert found["id"] == discovery_id and found["provider"] == "claude"
    assert found["stats"]["universe"] == 4 and found["regime"]["indexes"][0]["index"] == "S&P 500"
    assert len(FakeProvider.prompts) == 2

    for category, horizon in (("short", 20), ("long", 120)):
        result = found[category]
        assert len(result["shortlist"]) == 4 and result["market_view"] == "Steady."
        assert [c["rank"] for c in result["shortlist"]] == [1, 2, 3, 4]
        best, second = result["shortlist"][0]["ticker"], result["shortlist"][1]["ticker"]
        picks = [p for p in found["picks"] if p["category"] == category]
        assert [p["ticker"] for p in picks] == [best]          # 0.3 conviction was dropped
        assert result["dropped"][0] == {"ticker": second, "code": "low_conviction",
                                        "value": 30, "limit": 50}
        p = picks[0]
        assert p["horizon_days"] == horizon and p["bench_ticker"] == "^GSPC"
        assert p["base_date"] == "2026-11-13" and p["base_close"] > 0
        assert p["thesis"] == "Because." and p["pillars"] and p["metrics"]["roe"]
    assert any("short-term picks" in (line or "") for line in lines)


def test_a_bar_of_a_session_still_open_is_left_out(temp_db, monkeypatch):
    fake_market(monkeypatch)
    universe = discovery.build_universe(None)
    universe[0]["open_session"] = "2026-11-13"
    discovery.run("claude", config.load())
    dates = {p["ticker"]: p["base_date"] for c in ("short", "long")
             for p in db.latest_discovery()["picks"]}
    assert all(d == ("2026-11-12" if t == "AAA" else "2026-11-13") for t, d in dates.items())


def test_one_failed_category_keeps_the_other(temp_db, monkeypatch):
    fake_market(monkeypatch)

    class Flaky(FakeProvider):
        def ask(self, prompt, schema, check):
            if "1 to 4 weeks" in prompt:
                raise LLMError("claude: no answer within 600s")
            return super().ask(prompt, schema, check)

    monkeypatch.setattr(discovery, "get_provider", lambda *a, **k: Flaky())
    discovery.run("claude", config.load())
    found = db.latest_discovery()
    assert "no answer" in found["short"]["error"] and found["long"]["error"] is None
    assert {p["category"] for p in found["picks"]} == {"long"}


def test_a_run_fails_when_the_model_answers_nothing(temp_db, monkeypatch):
    fake_market(monkeypatch)

    class Down:
        def ask(self, prompt, schema, check):
            raise LLMError("claude: not signed in")

    monkeypatch.setattr(discovery, "get_provider", lambda *a, **k: Down())
    with pytest.raises(LLMError):
        discovery.run("claude", config.load())
    assert db.latest_discovery() is None


# --- grading ----------------------------------------------------------------

def stored_pick(**overrides):
    base = {"category": "short", "ticker": "AAA", "name": "AAA Corp", "sector": "Technology",
            "currency": "USD", "region": "us", "rank": 1, "score": 80.0, "conviction": 0.7,
            "base_date": "2026-01-05", "base_close": 100.0, "bench_ticker": "^GSPC",
            "horizon_days": 2, "thesis": "Because."}
    return {**base, **overrides}


def series(values):
    dates = pd.bdate_range("2026-01-05", periods=len(values)).strftime("%Y-%m-%d")
    return pd.DataFrame({"close": np.asarray(values, dtype=float)}, index=dates)


def test_picks_are_graded_against_their_index(temp_db):
    discovery.reset_cache()
    db.save_discovery("claude", "fable", "", {}, {}, {}, [
        stored_pick(),                                             # horizon passed
        stored_pick(ticker="BBB", category="long", horizon_days=120),   # still running
    ])
    calls = []

    def fetch(tickers, start):
        calls.append((tickers, start))
        return {"AAA": series([100, 105, 110, 130]), "BBB": series([50, 49, 48, 47]),
                "^GSPC": series([1000, 1010, 1020, 1030])}

    perf = discovery.performance(fetch)
    assert calls == [(["AAA", "BBB", "^GSPC"], "2025-12-26")]
    by_ticker = {r["ticker"]: r for r in perf["picks"]}
    done = by_ticker["AAA"]
    assert (done["return_pct"], done["bench_pct"], done["excess_pct"]) == (10.0, 2.0, 8.0)
    assert done["final"] and done["as_of"] == "2026-01-07" and done["index"] == "S&P 500"
    running = by_ticker["BBB"]
    assert running["return_pct"] == -6.0 and not running["final"] and running["bars"] == 3

    short = next(s for s in perf["summary"] if s["category"] == "short")
    assert short == {"category": "short", "provider": "claude", "picks": 1, "finished": 1,
                     "beat_index": 1, "avg_return_pct": 10.0, "avg_excess_pct": 8.0}
    assert next(s for s in perf["summary"] if s["category"] == "long")["finished"] == 0

    # The finished pick is stored: a later look does not need its prices again.
    discovery.reset_cache()
    again = discovery.performance(lambda tickers, start: calls.append(tickers) or {})
    assert calls[-1] == ["BBB", "^GSPC"]
    assert {r["ticker"]: r["return_pct"] for r in again["picks"]} == {"AAA": 10.0, "BBB": None}


def test_performance_is_cached_until_a_new_discovery(temp_db):
    discovery.reset_cache()
    db.save_discovery("claude", "fable", "", {}, {}, {}, [stored_pick(horizon_days=20)])
    calls = []
    fetch = lambda tickers, start: calls.append(1) or {"AAA": series([100, 101])}  # noqa: E731
    discovery.performance(fetch)
    discovery.performance(fetch)
    assert len(calls) == 1
    db.save_discovery("claude", "fable", "", {}, {}, {}, [stored_pick(ticker="CCC", horizon_days=20)])
    discovery.performance(fetch)
    assert len(calls) == 2
    discovery.reset_cache()


# --- runner, schedule, API --------------------------------------------------

def signed_in(monkeypatch, logged_in=True):
    status = {"installed": True, "logged_in": logged_in, "detail": "signed in" if logged_in else "not signed in"}
    monkeypatch.setattr(runner, "get_provider",
                        lambda *a, **k: type("P", (), {"login_status": lambda self: status})())


def test_a_discovery_is_logged_as_a_run(temp_db, monkeypatch):
    fake_market(monkeypatch)
    signed_in(monkeypatch)
    run_id = runner.run_discovery(provider="claude")
    run = db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert (run["trigger"], run["status"], run["progress"]) == ("discover", "ok", "6/6")
    assert "Universe: 4 stocks" in run["log"] and "long-term picks" in run["log"]
    assert not runner.is_running()


def test_a_failed_discovery_says_why(temp_db, monkeypatch):
    fake_market(monkeypatch)
    signed_in(monkeypatch, logged_in=False)
    run = db.one("SELECT * FROM runs WHERE id = ?", (runner.run_discovery(provider="claude"),))
    assert run["status"] == "failed" and "not signed in" in run["log"]
    assert db.latest_discovery() is None


def test_a_discovery_does_not_count_as_the_daily_run(temp_db):
    from datetime import datetime
    schedule = {"enabled": True, "time": "22:30", "weekdays_only": True}
    now = datetime(2026, 11, 16, 23, 0)
    db.execute("INSERT INTO runs(started_at, trigger, provider, status) VALUES(?, 'discover', 'claude', 'ok')",
               ("2026-11-16T22:45:00",))
    assert scheduler.missed_today(schedule, now)
    db.execute("INSERT INTO runs(started_at, trigger, provider, status) VALUES(?, 'schedule', 'claude', 'ok')",
               ("2026-11-16T22:50:00",))
    assert not scheduler.missed_today(schedule, now)


def test_markets_are_selectable_alone_or_combined(temp_db):
    from fastapi import HTTPException

    everything = config.DEFAULTS["discovery"]["regions"]
    assert config.load()["discovery"]["regions"] == everything
    assert all(m["selected"] for m in server.get_discovery()["markets"])

    # One market, then a combination; the order of config.yaml is kept.
    assert server.put_settings(server.SettingsIn(discovery_regions=["jp"]))["discovery_regions"] == ["jp"]
    assert config.load()["discovery"]["regions"] == ["jp"]
    server.put_settings(server.SettingsIn(discovery_regions=["KR", "us", "de"]))
    cfg = config.load()["discovery"]
    assert cfg["regions"] == ["us", "de", "kr"] and cfg["available_regions"] == everything
    shown = {m["region"]: m for m in server.get_discovery()["markets"]}
    assert [r for r, m in shown.items() if m["selected"]] == ["us", "de", "kr"]
    assert list(shown) == everything and shown["jp"]["index"] == "Nikkei 225"

    # Nothing, or only markets that are not on offer, is refused and changes nothing.
    for bad in ([], ["mars"]):
        with pytest.raises(HTTPException) as refused:
            server.put_settings(server.SettingsIn(discovery_regions=bad))
        assert refused.value.status_code == 400
    assert config.load()["discovery"]["regions"] == ["us", "de", "kr"]

    # Other settings leave the selection alone.
    server.put_settings(server.SettingsIn(currency="USD"))
    assert config.load()["discovery"]["regions"] == ["us", "de", "kr"]


def test_a_search_covers_the_selected_markets(temp_db, monkeypatch):
    fake_market(monkeypatch)
    signed_in(monkeypatch)
    seen = []
    universe = discovery.build_universe(None)
    monkeypatch.setattr(discovery, "build_universe",
                        lambda cfg, note=None: seen.append(cfg["discovery"]["regions"]) or universe)
    db.set_setting("discovery_regions", ["us", "jp"])
    runner.run_discovery(provider="claude")
    runner.run_discovery(provider="claude", regions=["de"])   # the command line's --region
    assert seen == [["us", "jp"], ["de"]]
    assert config.load()["discovery"]["regions"] == ["us", "jp"]   # an override is not saved


def test_the_screener_is_asked_only_for_selected_markets(monkeypatch):
    asked = []
    monkeypatch.setattr(discovery.fx, "rate", lambda a, b: 1.0)
    monkeypatch.setattr(discovery, "_screen", lambda region, cap, extra=(): asked.append(region) or [
        quote("AAA", "Aaa Corp", 100, 5e6, 5e9), quote("BBB", "Bbb Corp", 100, 5e6, 4e9)])
    cfg = {"discovery": {**LIMITS, "regions": ["de", "mars"]}}
    notes = []
    rows = discovery.build_universe(cfg, notes.append)
    assert set(asked) == {"de"} and {r["region"] for r in rows} == {"de"}
    assert any("mars" in n for n in notes)


def test_discover_endpoints(temp_db, monkeypatch):
    from fastapi import HTTPException

    empty = server.get_discovery()
    assert empty["latest"] is None and empty["limits"]["horizons"] == {"short": 20, "long": 120}

    started = []
    monkeypatch.setattr(runner, "start_discovery", lambda **kw: started.append(kw) or True)
    assert server.discover() == {"started": True, "provider": "claude"}
    assert started == [{"trigger": "manual", "provider": "claude"}]
    monkeypatch.setattr(runner, "start_discovery", lambda **kw: False)   # a run is in progress
    with pytest.raises(HTTPException) as refused:
        server.discover()
    assert refused.value.status_code == 409

    db.add_ticker("AAA", {"currency": "USD"})
    db.save_discovery("claude", "fable", "high", {"label": "neutral"}, {"universe": 9},
                      {"short": {"shortlist": []}, "long": None}, [stored_pick(currency="CHF")])
    shown = server.get_discovery()
    assert shown["watchlist"] == ["AAA"] and shown["latest"]["regime"] == {"label": "neutral"}
    assert shown["latest"]["picks"][0]["thesis"] == "Because."
    # A pick's currency is converted on the dashboard even when no watchlist stock uses it.
    monkeypatch.setattr(server.fx, "rates", lambda currencies, to: {c: 1.0 for c in currencies})
    assert set(server.get_fx()["rates"]) == {"USD", "CHF"}
