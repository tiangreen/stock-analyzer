import json

import pytest

from stock_analyzer import prompt
from stock_analyzer.llm import LLMError, get_provider
from stock_analyzer.llm.base import Provider, extract_json, validate

HORIZONS = [1, 5, 20]


def answer(**overrides):
    base = {
        "trend": "bullish", "summary": "Looks strong.", "key_signals": ["a"], "risks": ["b"],
        "support": 95.0, "resistance": 110.0,
        "predictions": [
            {"horizon_days": h, "direction": "up", "confidence": 0.6,
             "target_low": 99.0, "target_high": 105.0} for h in HORIZONS],
    }
    base.update(overrides)
    return base


def test_extract_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_fenced_with_prose():
    text = 'Here you go:\n```json\n{"a": {"b": [1, 2]}}\n```\nHope that helps {not json}.'
    assert extract_json(text) == {"a": {"b": [1, 2]}}


def test_extract_skips_non_json_brace():
    assert extract_json('set {x} then {"ok": true}') == {"ok": True}


def test_extract_none():
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_validate_ok():
    out = validate(answer(), HORIZONS)
    assert [p["horizon_days"] for p in out["predictions"]] == HORIZONS


def test_validate_normalizes():
    raw = answer(trend="Bullish")
    raw["predictions"][0].update(confidence=65, target_low=105, target_high=99, direction="UP")
    out = validate(raw, HORIZONS)
    first = out["predictions"][0]
    assert out["trend"] == "bullish"
    assert first["confidence"] == 0.65
    assert (first["target_low"], first["target_high"]) == (99, 105)
    assert first["direction"] == "up"


@pytest.mark.parametrize("broken", [
    {"trend": "sideways"},
    {"summary": ""},
    {"predictions": []},
    {"predictions": [{"horizon_days": 1, "direction": "up", "confidence": 0.5,
                      "target_low": 1, "target_high": 2}]},
])
def test_validate_rejects(broken):
    with pytest.raises(LLMError):
        validate(answer(**broken), HORIZONS)


def test_validate_rejects_bad_direction_and_price():
    raw = answer()
    raw["predictions"][1]["direction"] = "moon"
    with pytest.raises(LLMError):
        validate(raw, HORIZONS)
    raw = answer()
    raw["predictions"][1]["target_low"] = -3
    with pytest.raises(LLMError):
        validate(raw, HORIZONS)


def test_analyze_retries_once_then_succeeds():
    class Flaky(Provider):
        name = "flaky"
        calls = 0

        def complete(self, prompt_text, schema):
            self.calls += 1
            return "garbage" if self.calls == 1 else json.dumps(answer())

    provider = Flaky()
    result, raw = provider.analyze("p", {}, HORIZONS)
    assert provider.calls == 2 and result["trend"] == "bullish"


def test_analyze_gives_up_after_two():
    class Bad(Provider):
        name = "bad"

        def complete(self, prompt_text, schema):
            return "garbage"

    with pytest.raises(LLMError):
        Bad().analyze("p", {}, HORIZONS)


def test_unknown_provider():
    with pytest.raises(LLMError):
        get_provider("gemini")


def test_schema_is_strict():
    s = prompt.schema(HORIZONS)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    item = s["properties"]["predictions"]["items"]
    assert set(item["required"]) == set(item["properties"])


def test_login_status_parsing():
    from types import SimpleNamespace as NS

    from stock_analyzer.llm.claude_cli import ClaudeCLI
    from stock_analyzer.llm.codex_cli import CodexCLI

    assert ClaudeCLI()._signed_in(NS(stdout='{"loggedIn": false, "authMethod": "none"}')) == (False, "not signed in")
    assert ClaudeCLI()._signed_in(NS(stdout='{"loggedIn": true, "subscriptionType": "max"}')) == (True, "signed in (max)")
    assert CodexCLI()._signed_in(NS(stdout="Logged in using ChatGPT\n", stderr="", returncode=0)) == (True, "signed in (ChatGPT)")
    assert CodexCLI()._signed_in(NS(stdout="", stderr="Not logged in\n", returncode=1)) == (False, "not signed in")


def test_login_url_extraction():
    from stock_analyzer.llm.base import ANSI_RE, URL_RE

    line = ANSI_RE.sub("", "\x1b[1mVisit: https://auth.example.com/oauth/authorize?client_id=a&state=b\x1b[0m now")
    assert URL_RE.search(line).group(0) == "https://auth.example.com/oauth/authorize?client_id=a&state=b"
