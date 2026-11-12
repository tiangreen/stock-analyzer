"""SQLite storage."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    name TEXT, exchange TEXT, currency TEXT,
    info_json TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, run_date TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT,
    created_at TEXT NOT NULL,
    trend TEXT, summary TEXT,
    support REAL, resistance REAL,
    signals_json TEXT, risks_json TEXT,
    indicators_json TEXT, raw_response TEXT,
    UNIQUE (ticker, run_date, provider)
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL, provider TEXT NOT NULL,
    run_date TEXT NOT NULL, horizon_days INTEGER NOT NULL,
    direction TEXT NOT NULL, confidence REAL,
    target_low REAL, target_high REAL,
    base_close REAL NOT NULL, due_date TEXT,
    actual_date TEXT, actual_close REAL, actual_return REAL,
    outcome TEXT, in_range INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pred_open ON predictions(outcome, ticker);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL, finished_at TEXT,
    trigger TEXT, provider TEXT,
    status TEXT NOT NULL, progress TEXT, log TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_initialized = False


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    global _initialized
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not _initialized:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            # Databases created before effort was recorded lack the column.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(analyses)")}
            if "effort" not in columns:
                conn.execute("ALTER TABLE analyses ADD COLUMN effort TEXT")
            if "signal_json" not in columns:  # trade signals came later still
                conn.execute("ALTER TABLE analyses ADD COLUMN signal_json TEXT")
            _initialized = True
        yield conn
        conn.commit()
    finally:
        conn.close()


def reset_for_tests() -> None:
    global _initialized
    _initialized = False


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    found = rows(sql, params)
    return found[0] if found else None


def execute(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        return conn.execute(sql, params).lastrowid


# --- settings -------------------------------------------------------------

def get_settings() -> dict:
    return {r["key"]: json.loads(r["value"]) for r in rows("SELECT key, value FROM settings")}


def set_setting(key: str, value) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# --- watchlist ------------------------------------------------------------

def watchlist() -> list[dict]:
    return rows("SELECT * FROM watchlist ORDER BY ticker")


def add_ticker(ticker: str, info: dict) -> None:
    execute(
        "INSERT INTO watchlist(ticker, name, exchange, currency, info_json, added_at) "
        "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
        "name = excluded.name, exchange = excluded.exchange, "
        "currency = excluded.currency, info_json = excluded.info_json",
        (ticker, info.get("name"), info.get("exchange"), info.get("currency"),
         json.dumps(info), now()),
    )


def remove_ticker(ticker: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM prices WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM analyses WHERE ticker = ?", (ticker,))


# --- analyses -------------------------------------------------------------

def save_analysis(ticker: str, run_date: str, provider: str, model: str, result: dict,
                  indicators: dict, raw: str, base_close: float,
                  due_dates: dict[int, str], effort: str = "") -> int:
    with connect() as conn:
        conn.execute(
            "DELETE FROM analyses WHERE ticker = ? AND run_date = ? AND provider = ?",
            (ticker, run_date, provider),
        )
        cur = conn.execute(
            "INSERT INTO analyses(ticker, run_date, provider, model, created_at, trend, summary,"
            " support, resistance, signals_json, risks_json, indicators_json, raw_response, effort,"
            " signal_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, run_date, provider, model, now(), result["trend"], result["summary"],
             result.get("support"), result.get("resistance"),
             json.dumps(result["key_signals"]), json.dumps(result["risks"]),
             json.dumps(indicators), raw, effort or None,
             json.dumps(result["signal"]) if result.get("signal") else None),
        )
        analysis_id = cur.lastrowid
        for p in result["predictions"]:
            conn.execute(
                "INSERT INTO predictions(analysis_id, ticker, provider, run_date, horizon_days,"
                " direction, confidence, target_low, target_high, base_close, due_date)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (analysis_id, ticker, provider, run_date, p["horizon_days"], p["direction"],
                 p["confidence"], p["target_low"], p["target_high"], base_close,
                 due_dates.get(p["horizon_days"])),
            )
        return analysis_id


def _decode(analysis: dict) -> dict:
    analysis["key_signals"] = json.loads(analysis.pop("signals_json") or "[]")
    analysis["risks"] = json.loads(analysis.pop("risks_json") or "[]")
    analysis["indicators"] = json.loads(analysis.pop("indicators_json") or "{}")
    analysis["signal"] = json.loads(analysis.pop("signal_json", None) or "null")
    analysis.pop("raw_response", None)
    analysis["predictions"] = rows(
        "SELECT * FROM predictions WHERE analysis_id = ? ORDER BY horizon_days", (analysis["id"],)
    )
    return analysis


def analyses_for(ticker: str, limit: int = 30) -> list[dict]:
    found = rows(
        "SELECT * FROM analyses WHERE ticker = ? ORDER BY run_date DESC, created_at DESC LIMIT ?",
        (ticker, limit),
    )
    return [_decode(a) for a in found]


def latest_analysis(ticker: str, provider: str | None = None) -> dict | None:
    if provider:
        found = one(
            "SELECT * FROM analyses WHERE ticker = ? AND provider = ? "
            "ORDER BY run_date DESC, created_at DESC LIMIT 1", (ticker, provider))
    else:
        found = one(
            "SELECT * FROM analyses WHERE ticker = ? "
            "ORDER BY run_date DESC, created_at DESC LIMIT 1", (ticker,))
    return _decode(found) if found else None


def signals(limit: int = 300) -> list[dict]:
    """Analyses that carry a trade signal, newest first: ticker, dates, close and the signal."""
    found = rows(
        "SELECT id, ticker, run_date, provider, model, indicators_json, signal_json FROM analyses "
        "WHERE signal_json IS NOT NULL ORDER BY run_date DESC, created_at DESC LIMIT ?", (limit,))
    for a in found:
        a["close"] = json.loads(a.pop("indicators_json") or "{}").get("close")
        a["signal"] = json.loads(a.pop("signal_json"))
    return found


def bars_after(ticker: str, date: str, count: int) -> list[dict]:
    """Up to `count` daily bars after `date`, oldest first."""
    return rows("SELECT date, high, low, close FROM prices WHERE ticker = ? AND date > ? "
                "ORDER BY date LIMIT ?", (ticker, date, count))


def has_analysis(ticker: str, run_date: str, provider: str) -> bool:
    return one(
        "SELECT 1 AS x FROM analyses WHERE ticker = ? AND run_date = ? AND provider = ?",
        (ticker, run_date, provider),
    ) is not None
