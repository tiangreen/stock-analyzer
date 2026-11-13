import json
import sqlite3

import pytest

from stock_analyzer import config, db
from stock_analyzer.llm import LLMError, get_provider
from stock_analyzer.llm import codex_cli
from stock_analyzer.llm.claude_cli import ClaudeCLI
from stock_analyzer.llm.codex_cli import CodexCLI, parse_models

CODEX_MODELS = json.dumps({"models": [
    {"slug": "gpt-b", "display_name": "GPT-B", "visibility": "list", "priority": 2,
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
    {"slug": "hidden-one", "visibility": "hide", "priority": 1, "supported_reasoning_levels": []},
    {"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list", "priority": 1,
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "ultra"}]},
]})


@pytest.fixture
def codex(monkeypatch):
    monkeypatch.setattr(codex_cli, "_models_cache", (float("inf"), parse_models(CODEX_MODELS)))
    return CodexCLI


def test_claude_args_default_passes_no_model_or_effort():
    args = ClaudeCLI().cli_args()
    assert "--model" not in args and "--effort" not in args
    assert args[args.index("--tools") + 1] == ""  # tools stay disabled


def test_claude_args_with_model_and_effort():
    args = get_provider("claude", "opus", effort="high").cli_args()
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "high"


def test_codex_args_default_and_chosen():
    plain = CodexCLI().cli_args("s.json", "a.txt")
    assert "--model" not in plain and "-c" not in plain and plain[-1] == "-"
    chosen = get_provider("codex", "gpt-a", effort="medium").cli_args("s.json", "a.txt")
    assert chosen[chosen.index("--model") + 1] == "gpt-a"
    assert chosen[chosen.index("-c") + 1] == "model_reasoning_effort=medium"
    assert chosen[-1] == "-"  # the stdin marker stays last


def test_parse_codex_models_hides_internal_and_sorts_by_priority():
    models = parse_models(CODEX_MODELS)
    assert [m["id"] for m in models] == ["gpt-a", "gpt-b"]
    assert models[0] == {"id": "gpt-a", "label": "GPT-A", "efforts": ["low", "medium", "ultra"]}


def test_parse_codex_models_rejects_garbage():
    with pytest.raises(ValueError):
        parse_models("not json")
    assert parse_models("{}") == []


def test_codex_options_survive_a_failed_listing(monkeypatch):
    monkeypatch.setattr(codex_cli, "_models_cache", None)
    monkeypatch.setattr(CodexCLI, "find", lambda self: (_ for _ in ()).throw(LLMError("no cli")))
    assert CodexCLI().options() == []
    assert CodexCLI().efforts_for("anything") == ["low", "medium", "high"]


def test_efforts_follow_the_model(codex):
    assert codex().efforts_for("gpt-b") == ["low", "high"]
    # A custom model name may use any level some model offers.
    assert codex().efforts_for("my-custom") == ["low", "medium", "ultra", "high"]


def test_check_choice(codex):
    codex().check_choice("", "")
    codex().check_choice("gpt-a", "ultra")
    codex().check_choice("my-custom-model:v2", "high")
    with pytest.raises(LLMError):
        codex().check_choice("gpt-b", "ultra")          # not offered by that model
    with pytest.raises(LLMError):
        ClaudeCLI().check_choice("opus", "ultra")       # not a Claude level
    for bad in ("opus; rm -rf", "a b", "--dangerous", "x" * 90, 'm"q'):
        with pytest.raises(LLMError):
            ClaudeCLI().check_choice(bad, "")


def test_saved_choices_override_config_file(temp_db):
    file_default = config.load()["models"]["codex"]
    db.set_setting("models", {"claude": "opus", "bogus": "x"})
    db.set_setting("efforts", {"claude": "high"})
    cfg = config.load()
    assert cfg["models"] == {"claude": "opus", "codex": file_default}
    assert cfg["efforts"]["claude"] == "high" and cfg["efforts"]["codex"] == ""
    db.set_setting("models", {"claude": ""})            # back to "CLI default"
    assert config.load()["models"]["claude"] == ""


def test_effort_column_is_added_to_an_old_database(temp_db):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(config.DB_PATH)
    old.execute("CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,"
                " run_date TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL,"
                " trend TEXT, summary TEXT, support REAL, resistance REAL, signals_json TEXT,"
                " risks_json TEXT, indicators_json TEXT, raw_response TEXT,"
                " UNIQUE (ticker, run_date, provider))")
    old.execute("INSERT INTO analyses(ticker, run_date, provider, model, created_at, trend, summary)"
                " VALUES('OLD', '2026-01-02', 'claude', 'default', 'x', 'neutral', 's')")
    old.commit()
    old.close()

    assert db.latest_analysis("OLD")["effort"] is None  # old row readable, column present
    result = {"trend": "bullish", "summary": "s", "key_signals": [], "risks": [], "support": None,
              "resistance": None, "predictions": []}
    db.save_analysis("NEW", "2026-01-05", "codex", "gpt-a", result, {}, "raw", 100.0, {}, effort="ultra")
    assert db.latest_analysis("NEW")["effort"] == "ultra"


class _Request:
    def __init__(self, **headers):
        self.headers = headers


def test_shutdown_asks_the_running_server_to_exit(monkeypatch):
    import time
    from types import SimpleNamespace

    from fastapi import HTTPException

    from stock_analyzer import server

    running = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(server, "_server", running)
    with pytest.raises(HTTPException) as refused:  # another site open in the browser
        server.shutdown(_Request(origin="https://evil.example", host="127.0.0.1:8765"))
    assert refused.value.status_code == 403 and not running.should_exit

    assert server.shutdown(_Request(origin="http://127.0.0.1:8765", host="127.0.0.1:8765")) == {"stopping": True}
    deadline = time.monotonic() + 3
    while not running.should_exit and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.should_exit


def test_shutdown_needs_the_apps_own_launcher(monkeypatch):
    from fastapi import HTTPException

    from stock_analyzer import server

    monkeypatch.setattr(server, "_server", None)
    with pytest.raises(HTTPException) as refused:
        server.shutdown(_Request(host="127.0.0.1:8765"))
    assert refused.value.status_code == 409
