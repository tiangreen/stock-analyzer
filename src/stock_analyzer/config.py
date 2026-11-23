"""Configuration: config.yaml defaults, overridden by settings saved from the dashboard."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(os.environ.get("STOCK_ANALYZER_HOME") or Path(__file__).resolve().parents[2])
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "analyzer.db"

PROVIDERS = ("claude", "codex")  # the subscription CLIs; API providers come from config.yaml
API_TYPES = {"anthropic": "Anthropic API", "openai": "OpenAI API"}  # type -> default label
API_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")
MCP_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # it becomes part of a CLI argument
ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESPONSE_FORMATS = ("none", "json_object", "json_schema")
log = logging.getLogger(__name__)
_warned: set[str] = set()
DISPLAY_CURRENCIES = ("EUR", "USD")  # first one is the default
# Dashboard languages (first one is the default) and the name the model is told to write in.
LANGUAGES = {"en": "English", "de": "German", "zh": "Simplified Chinese"}

DEFAULTS = {
    "watchlist": [],
    "provider": "claude",
    "display_currency": "EUR",
    "language": "en",
    "models": {"claude": "", "codex": ""},
    "efforts": {"claude": "", "codex": ""},
    "api_providers": {
        "anthropic-api": {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
        "openai-api": {"type": "openai", "api_key_env": "OPENAI_API_KEY"},
    },
    "horizons": [1, 5, 20],
    "history_period": "2y",
    "llm_timeout": 300,
    # TradingView MCP tools the model may call during an analysis (subscription CLIs only).
    # The server names are how each CLI calls the connected TradingView MCP server.
    "tradingview_mcp": {"enabled": True, "claude_server": "claude_ai_TradingView_MCP",
                        "codex_server": "tradingview"},
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
    "discovery": {
        "regions": ["us", "de", "fr", "nl", "ch", "gb", "jp", "kr"],
        "min_market_cap_usd": 2e9,
        "min_dollar_volume_usd": 5e6,
        "min_turnover": 0.00025,
        "universe_size": {"us": 300, "default": 100},
        "finalists": 40,
        "shortlist": 12,
        "max_shortlist_per_sector": 4,
        "picks": 5,
        "max_picks_per_sector": 2,
        "min_conviction": 0.5,
        "horizons": {"short": 20, "long": 120},
        "llm_timeout": 600,
        "weights": {
            "short": {"momentum": 30, "revisions": 25, "drift": 15, "volume": 10,
                      "catalyst": 10, "overextension": 10},
            "long": {"quality": 30, "valuation": 25, "growth": 20, "estimates": 10,
                     "trend": 10, "risk": 5},
        },
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


def _warn(message: str) -> None:
    if message not in _warned:  # load() runs on every request; say it once
        _warned.add(message)
        log.warning("config.yaml: %s", message)


def _url(value, what: str) -> str:
    """An https URL (http only for this machine), without a trailing slash. Raises ValueError."""
    parsed = urlparse(str(value or "").strip())
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
        raise ValueError(f"{what} must be an https:// URL")
    if parsed.scheme == "http" and not local:
        raise ValueError(f"{what} must use https:// (http:// is only accepted for localhost)")
    return parsed.geturl().rstrip("/")


def _env(value, what: str) -> str:
    """The NAME of an environment variable. Secrets themselves never belong in config.yaml."""
    name = str(value or "").strip()
    if not ENV_RE.fullmatch(name):
        raise ValueError(f"{what} must be the name of an environment variable, not the secret itself")
    return name


def _api_provider(name: str, raw) -> dict | None:
    """One checked `api_providers` entry, or None if it is switched off. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("must be a mapping")
    if raw.get("enabled") is False:
        return None
    if not API_NAME_RE.fullmatch(name) or name in PROVIDERS:
        raise ValueError("the name must be lowercase letters, digits, - or _ and not claude or codex")
    kind = str(raw.get("type", "")).lower()
    if kind not in API_TYPES:
        raise ValueError(f"type must be one of {tuple(API_TYPES)}")
    entry: dict = {"type": kind, "label": str(raw.get("label") or API_TYPES[kind]).strip()[:60],
                   "model": str(raw.get("model") or "").strip()}
    if raw.get("base_url"):
        entry["base_url"] = _url(raw["base_url"], "base_url")
        if not raw.get("label"):
            entry["label"] = name  # a custom endpoint is not the vendor's own API
    if raw.get("oauth"):
        oauth = raw["oauth"]
        if not isinstance(oauth, dict):
            raise ValueError("oauth must be a mapping")
        style = str(oauth.get("auth_style") or "basic").lower()
        if style not in ("basic", "body"):
            raise ValueError("oauth.auth_style must be basic or body")
        entry["oauth"] = {
            "token_url": _url(oauth.get("token_url"), "oauth.token_url"),
            "client_id_env": _env(oauth.get("client_id_env"), "oauth.client_id_env"),
            "client_secret_env": _env(oauth.get("client_secret_env"), "oauth.client_secret_env"),
            "scope": str(oauth.get("scope") or "").strip(),
            "audience": str(oauth.get("audience") or "").strip(),
            "auth_style": style,
        }
    elif raw.get("api_key_env"):
        entry["api_key_env"] = _env(raw["api_key_env"], "api_key_env")
    elif "base_url" not in entry:
        raise ValueError("needs api_key_env or oauth")  # only a custom endpoint may be keyless
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("headers must be a mapping")
    entry["headers"] = {str(k): str(v) for k, v in headers.items()}
    models = raw.get("models") or []
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    entry["models"] = [str(m).strip() for m in models if str(m).strip()]
    # Strict schemas are an OpenAI and Anthropic feature; other services often reject them.
    default = "none" if kind == "openai" and "base_url" in entry else "json_schema"
    entry["response_format"] = str(raw.get("response_format") or default).lower()
    if entry["response_format"] not in RESPONSE_FORMATS:
        raise ValueError(f"response_format must be one of {RESPONSE_FORMATS}")
    return entry


def provider_names(cfg: dict) -> list[str]:
    """Every provider of a loaded config: the subscription CLIs, then the API providers."""
    return [*PROVIDERS, *cfg.get("api_providers", {})]


def load() -> dict:
    """File config merged over defaults, then dashboard-saved settings merged over that."""
    file_cfg = {}
    if CONFIG_PATH.exists():
        file_cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, file_cfg)

    checked = {}
    for name, raw in (cfg.get("api_providers") or {}).items():
        try:
            entry = _api_provider(str(name), raw)
        except ValueError as exc:
            _warn(f"api_providers.{name} ignored: {exc}")
            continue
        if entry:
            checked[str(name)] = entry
    cfg["api_providers"] = checked
    names = provider_names(cfg)
    for key in ("models", "efforts"):
        cfg[key] = {n: str((cfg.get(key) or {}).get(n) or "") for n in names}
    if cfg["provider"] not in names:
        cfg["provider"] = PROVIDERS[0]

    from . import db  # late import: db imports this module

    saved = db.get_settings()
    if "provider" in saved and saved["provider"] in names:
        cfg["provider"] = saved["provider"]
    for key in ("models", "efforts"):  # chosen in the dashboard's Settings tab
        if isinstance(saved.get(key), dict):
            cfg[key] = {**cfg[key], **{k: str(v) for k, v in saved[key].items() if k in names}}
    if saved.get("display_currency") in DISPLAY_CURRENCIES:
        cfg["display_currency"] = saved["display_currency"]
    if str(cfg["display_currency"]).upper() not in DISPLAY_CURRENCIES:
        cfg["display_currency"] = DISPLAY_CURRENCIES[0]
    cfg["display_currency"] = str(cfg["display_currency"]).upper()
    if saved.get("language") in LANGUAGES:
        cfg["language"] = saved["language"]
    cfg["language"] = str(cfg["language"]).lower()
    if cfg["language"] not in LANGUAGES:
        cfg["language"] = next(iter(LANGUAGES))
    if "schedule" in saved:
        cfg["schedule"] = _merge(cfg["schedule"], saved["schedule"])
    # Discover: config.yaml lists the markets on offer, the dashboard picks among them.
    discovery = cfg["discovery"] = dict(cfg["discovery"])
    offered = list(dict.fromkeys(str(r).lower() for r in discovery["regions"] or []))
    discovery["available_regions"] = offered
    chosen = saved.get("discovery_regions")
    chosen = [r for r in offered if r in chosen] if isinstance(chosen, list) else []
    discovery["regions"] = chosen or offered
    tv = cfg["tradingview_mcp"] = dict(cfg["tradingview_mcp"] or {})
    tv["enabled"] = bool(tv.get("enabled"))
    for key in ("claude_server", "codex_server"):
        name = str(tv.get(key) or "")
        if not MCP_NAME_RE.fullmatch(name):
            _warn(f"tradingview_mcp.{key} '{name}' ignored: use letters, digits, _ and -")
            name = DEFAULTS["tradingview_mcp"][key]
        tv[key] = name
    cfg["horizons"] = sorted({int(h) for h in cfg["horizons"]})
    return cfg
