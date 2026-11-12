"""Configuration: config.yaml defaults, overridden by settings saved from the dashboard."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("STOCK_ANALYZER_HOME") or Path(__file__).resolve().parents[2])
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "analyzer.db"

PROVIDERS = ("claude", "codex")
DISPLAY_CURRENCIES = ("EUR", "USD")  # first one is the default

DEFAULTS = {
    "watchlist": [],
    "provider": "claude",
    "display_currency": "EUR",
    "models": {"claude": "", "codex": ""},
    "efforts": {"claude": "", "codex": ""},
    "horizons": [1, 5, 20],
    "history_period": "2y",
    "llm_timeout": 300,
    "schedule": {"enabled": True, "time": "22:30", "weekdays_only": True},
    "server": {"host": "127.0.0.1", "port": 8765},
    "signals": {
        "min_confidence": 0.6,
        "max_entry_gap_pct": 3.0,
        "min_reward_risk": 1.5,
        "max_stop_pct": 10.0,
        "risk_per_trade_pct": 1.0,
        "max_position_pct": 10.0,
        "portfolio_value": 0,
        "portfolio_currency": "EUR",
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def load() -> dict:
    """File config merged over defaults, then dashboard-saved settings merged over that."""
    file_cfg = {}
    if CONFIG_PATH.exists():
        file_cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, file_cfg)

    from . import db  # late import: db imports this module

    saved = db.get_settings()
    if "provider" in saved and saved["provider"] in PROVIDERS:
        cfg["provider"] = saved["provider"]
    for key in ("models", "efforts"):  # chosen in the dashboard's Settings tab
        if isinstance(saved.get(key), dict):
            cfg[key] = {**cfg[key], **{k: str(v) for k, v in saved[key].items() if k in PROVIDERS}}
    if saved.get("display_currency") in DISPLAY_CURRENCIES:
        cfg["display_currency"] = saved["display_currency"]
    if str(cfg["display_currency"]).upper() not in DISPLAY_CURRENCIES:
        cfg["display_currency"] = DISPLAY_CURRENCIES[0]
    cfg["display_currency"] = str(cfg["display_currency"]).upper()
    if "schedule" in saved:
        cfg["schedule"] = _merge(cfg["schedule"], saved["schedule"])
    cfg["horizons"] = sorted({int(h) for h in cfg["horizons"]})
    return cfg
