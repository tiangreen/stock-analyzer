"""TradingView MCP tools in analysis calls: CLI arguments, config checks, prompt text."""
import pandas as pd

from stock_analyzer import config, prompt, tradingview
from stock_analyzer.llm import codex_cli, get_provider

TV = {"enabled": True, "claude_server": "claude_ai_TradingView_MCP", "codex_server": "tradingview"}


def test_claude_without_tradingview_loads_no_mcp_servers():
    args = get_provider("claude").cli_args()
    assert "--strict-mcp-config" in args
    assert "--allowedTools" not in args


def test_claude_allows_only_read_only_tradingview_tools():
    provider = get_provider("claude", tradingview=TV)
    args = provider.cli_args()
    assert provider.tradingview_ready()
    assert "--strict-mcp-config" not in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__claude_ai_TradingView_MCP__{t}" for t in tradingview.READ_TOOLS]
    assert not any("alert" in t or "watchlist" in t or "pine" in t for t in allowed)


def test_switched_off_tradingview_is_ignored():
    assert not get_provider("claude", tradingview={**TV, "enabled": False}).tradingview_ready()


def test_codex_limits_the_configured_server_to_read_tools(monkeypatch):
    provider = get_provider("codex", tradingview=TV)
    monkeypatch.setattr(codex_cli, "_servers", {"tradingview": (0.0, True)})
    monkeypatch.setattr(codex_cli.time, "monotonic", lambda: 1.0)
    args = provider.cli_args("s.json", "a.txt")
    setting = next(a for a in args if a.startswith("mcp_servers."))
    assert setting.startswith("mcp_servers.tradingview.enabled_tools=['mcp-tv-get-symbol-data',")
    assert '"' not in setting  # double quotes would not survive the Windows .cmd shim
    assert args[-1] == "-"


def test_codex_without_the_server_adds_nothing(monkeypatch):
    provider = get_provider("codex", tradingview=TV)
    monkeypatch.setattr(codex_cli, "_servers", {"tradingview": (0.0, False)})
    monkeypatch.setattr(codex_cli.time, "monotonic", lambda: 1.0)
    assert not any(a.startswith("mcp_servers.") for a in provider.cli_args("s.json", "a.txt"))


def test_api_providers_never_get_tools():
    assert not get_provider("anthropic-api", tradingview=TV).tradingview_ready()


def test_bad_server_name_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("tradingview_mcp:\n  claude_server: 'x; rm -rf'\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    tv = config.load()["tradingview_mcp"]
    assert tv["claude_server"] == "claude_ai_TradingView_MCP"
    assert tv["enabled"] is True


def test_prompt_mentions_tools_only_when_given():
    prices = pd.DataFrame({"date": ["2026-11-20"] * 60, "open": 1.0, "high": 1.0, "low": 1.0,
                           "close": 1.0, "volume": 1})
    snap = {"close": 1.0, "signals": []}
    plain = prompt.build("SAP.DE", {}, prices, snap, [], [], [1, 5])
    with_tools = prompt.build("SAP.DE", {}, prices, snap, [], [], [1, 5],
                              tools=tradingview.prompt_section("SAP.DE"))
    assert "TradingView" not in plain and "All the data you may use is below." in plain
    assert "## TradingView tools" in with_tools and "XETR:SAP" in with_tools
    assert "search-symbols" in tradingview.prompt_section("^GSPC")
