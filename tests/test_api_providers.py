"""API providers: config checks, OAuth2 client credentials, request shapes. No network."""
import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from stock_analyzer import config, db
from stock_analyzer.llm import LLMError, api, get_provider, oauth, provider_names

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["trend"],
          "properties": {"trend": {"type": "string"}}}
OAUTH = {"token_url": "https://login.example.com/oauth2/token", "client_id_env": "T_CLIENT_ID",
         "client_secret_env": "T_CLIENT_SECRET"}
GATEWAY = {"type": "openai", "base_url": "https://llm.example.com/v1/", "oauth": OAUTH,
           "headers": {"X-Tenant": "abc"}, "models": ["model-a"], "model": "model-a"}


def entry(raw: dict, name: str = "gateway") -> dict:
    return config._api_provider(name, raw)


@pytest.fixture
def web(monkeypatch):
    """Route httpx through a handler list: web.handler = lambda request: httpx.Response(...)."""
    state = SimpleNamespace(requests=[], handler=None)

    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.handler(request)

    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(handle))
    monkeypatch.setattr(oauth, "_cache", {})
    monkeypatch.setattr(api, "_models", {})
    monkeypatch.setattr(api, "_sleep", lambda seconds: None)
    monkeypatch.setenv("T_CLIENT_ID", "my-client")
    monkeypatch.setenv("T_CLIENT_SECRET", "s3cret-value")
    return state


def token_response(token="tok-1", **extra):
    return httpx.Response(200, json={"access_token": token, "token_type": "Bearer",
                                     "expires_in": 3600, **extra})


def chat_response(text='{"trend": "bullish"}', **choice):
    return httpx.Response(200, json={"choices": [{"message": {"content": text},
                                                  "finish_reason": "stop", **choice}]})


# --- config -----------------------------------------------------------------

def test_builtin_api_providers_are_offered(temp_db):
    cfg = config.load()
    assert {"anthropic-api", "openai-api"} <= set(cfg["api_providers"])
    assert provider_names(cfg)[:2] == ["claude", "codex"]
    assert cfg["api_providers"]["anthropic-api"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert cfg["models"]["openai-api"] == "" and cfg["efforts"]["anthropic-api"] == ""


def test_entries_are_checked():
    checked = entry(GATEWAY)
    assert checked["base_url"] == "https://llm.example.com/v1"      # trailing slash dropped
    assert checked["label"] == "gateway" and checked["response_format"] == "none"
    assert checked["oauth"]["auth_style"] == "basic" and "api_key_env" not in checked
    assert entry({"type": "openai", "api_key_env": "OPENAI_API_KEY"})["response_format"] == "json_schema"
    assert entry({"type": "openai", "base_url": "http://localhost:11434/v1"})["label"] == "gateway"
    assert entry({"type": "openai", "api_key_env": "K", "enabled": False}) is None
    for bad in (
        {"type": "mistral", "api_key_env": "K"},
        {"type": "openai"},                                              # no key, no endpoint
        {"type": "openai", "api_key_env": "sk-live-not-a-variable-name"},
        {"type": "openai", "base_url": "http://llm.example.com/v1"},     # plain http, not local
        {"type": "openai", "base_url": "https://user:pw@llm.example.com/v1"},
        {"type": "openai", "base_url": "https://x.example", "oauth": {**OAUTH, "token_url": "ftp://x"}},
        {"type": "openai", "api_key_env": "K", "response_format": "xml"},
        "not a mapping",
    ):
        with pytest.raises(ValueError):
            entry(bad)
    for name in ("claude", "Has Space", "UPPER", ""):
        with pytest.raises(ValueError):
            entry({"type": "openai", "api_key_env": "K"}, name=name)


def test_custom_provider_from_the_file_and_its_saved_choice(temp_db, tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"api_providers": {
        "gateway": GATEWAY, "broken": {"type": "nope"}, "openai-api": {"enabled": False}}}))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    db.set_setting("provider", "gateway")
    db.set_setting("models", {"gateway": "model-b", "broken": "x"})
    cfg = config.load()
    assert provider_names(cfg) == ["claude", "codex", "anthropic-api", "gateway"]
    assert cfg["provider"] == "gateway" and cfg["models"]["gateway"] == "model-b"
    assert "broken" not in cfg["models"]
    provider = get_provider("gateway", cfg=cfg)
    assert (provider.kind, provider.label, provider.default_model) == ("api", "gateway", "model-a")
    with pytest.raises(LLMError):
        get_provider("broken", cfg=cfg)
    db.set_setting("provider", "broken")     # a provider that is gone falls back
    assert config.load()["provider"] == "claude"


# --- OAuth2 client credentials -------------------------------------------------

def test_token_is_fetched_once_and_cached(web):
    web.handler = lambda request: token_response()
    cfg = entry(GATEWAY)["oauth"]
    assert oauth.token(cfg) == "tok-1" and oauth.token(cfg) == "tok-1"
    assert len(web.requests) == 1
    request = web.requests[0]
    assert str(request.url) == OAUTH["token_url"]
    assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"]}
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"my-client:s3cret-value").decode()


def test_token_in_the_body_with_scope_and_audience(web):
    web.handler = lambda request: token_response()
    cfg = entry({**GATEWAY, "oauth": {**OAUTH, "auth_style": "body", "scope": "llm.read",
                                      "audience": "api://llm"}})["oauth"]
    oauth.token(cfg)
    form = parse_qs(web.requests[0].content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["llm.read"], "audience": ["api://llm"],
                    "client_id": ["my-client"], "client_secret": ["s3cret-value"]}
    assert "authorization" not in web.requests[0].headers


def test_token_is_renewed_before_it_expires(web, monkeypatch):
    tokens = iter(["tok-1", "tok-2"])
    web.handler = lambda request: token_response(next(tokens), expires_in=120)
    clock = [1000.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: clock[0])
    cfg = entry(GATEWAY)["oauth"]
    assert oauth.token(cfg) == "tok-1"
    clock[0] += 30
    assert oauth.token(cfg) == "tok-1"
    clock[0] += 40                       # inside the last minute of its lifetime
    assert oauth.token(cfg) == "tok-2"


def test_token_errors_name_the_reason_but_never_the_secret(web, monkeypatch):
    cfg = entry(GATEWAY)["oauth"]
    web.handler = lambda request: httpx.Response(401, json={"error": "invalid_client",
                                                            "error_description": "bad secret"})
    with pytest.raises(LLMError) as refused:
        oauth.token(cfg)
    assert "invalid_client" in str(refused.value) and "bad secret" in str(refused.value)
    assert "s3cret-value" not in str(refused.value)
    web.handler = lambda request: httpx.Response(200, json={"token_type": "Bearer"})
    with pytest.raises(LLMError, match="no access_token"):
        oauth.token(cfg)

    def down(request):
        raise httpx.ConnectError("refused")
    web.handler = down
    with pytest.raises(LLMError, match="unreachable"):
        oauth.token(cfg)
    monkeypatch.delenv("T_CLIENT_SECRET")
    with pytest.raises(LLMError, match="T_CLIENT_SECRET not set"):
        oauth.token(cfg)


# --- OpenAI-compatible ---------------------------------------------------------

def gateway(**choice):
    return api.OpenAICompatAPI("gateway", entry(GATEWAY), **choice)


def test_gateway_call_sends_the_token_and_returns_the_text(web):
    web.handler = lambda r: token_response() if r.url.host == "login.example.com" else chat_response()
    assert gateway(effort="high").complete("the prompt", SCHEMA) == '{"trend": "bullish"}'
    call = web.requests[-1]
    assert str(call.url) == "https://llm.example.com/v1/chat/completions"
    assert call.headers["authorization"] == "Bearer tok-1" and call.headers["x-tenant"] == "abc"
    body = json.loads(call.content)
    assert body["model"] == "model-a" and body["reasoning_effort"] == "high"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"] == "the prompt" and "response_format" not in body


def test_response_format_follows_the_entry(web):
    strict = api.OpenAICompatAPI("openai-api", entry({"type": "openai", "api_key_env": "K"}, "openai-api"))
    body = strict.request_body("gpt-x", "p", SCHEMA)
    assert body["response_format"]["json_schema"] == {"name": "answer", "strict": True, "schema": SCHEMA}
    assert "reasoning_effort" not in body
    loose = api.OpenAICompatAPI("g", entry({**GATEWAY, "response_format": "json_object"}))
    assert loose.request_body("m", "p", SCHEMA)["response_format"] == {"type": "json_object"}


def test_a_rejected_token_is_replaced_once(web):
    tokens = iter(["tok-old", "tok-new"])

    def handle(request):
        if request.url.host == "login.example.com":
            return token_response(next(tokens))
        ok = request.headers["authorization"] == "Bearer tok-new"
        return chat_response() if ok else httpx.Response(401, json={"error": {"message": "expired"}})
    web.handler = handle
    assert gateway().complete("p", SCHEMA)
    assert [r.url.host for r in web.requests] == ["login.example.com", "llm.example.com"] * 2

    web.handler = lambda r: token_response() if r.url.host == "login.example.com" else httpx.Response(401)
    with pytest.raises(LLMError, match="401"):
        gateway().complete("p", SCHEMA)


def test_busy_service_is_retried_and_errors_are_reported(web):
    answers = iter([httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(503), chat_response()])
    web.handler = lambda r: token_response() if r.url.host == "login.example.com" else next(answers)
    assert gateway().complete("p", SCHEMA)

    web.handler = lambda r: (token_response() if r.url.host == "login.example.com"
                             else httpx.Response(400, json={"error": {"message": "unknown model"}}))
    with pytest.raises(LLMError, match="HTTP 400 from llm.example.com: unknown model"):
        gateway().complete("p", SCHEMA)


@pytest.mark.parametrize("answer, problem", [
    (chat_response(""), "no text"),
    (chat_response("{", finish_reason="length"), "cut off"),
    (httpx.Response(200, json={"choices": []}), "no text"),
    (httpx.Response(200, json={"choices": [{"message": {"refusal": "cannot help"}}]}), "declined"),
    (httpx.Response(200, text="<html>"), "JSON object"),
])
def test_unusable_answers(web, answer, problem):
    web.handler = lambda r: token_response() if r.url.host == "login.example.com" else answer
    with pytest.raises(LLMError, match=problem):
        gateway().complete("p", SCHEMA)


def test_an_api_provider_needs_a_model(web, monkeypatch):
    monkeypatch.setenv("K", "key")
    plain = api.OpenAICompatAPI("openai-api", entry({"type": "openai", "api_key_env": "K"}, "openai-api"))
    with pytest.raises(LLMError, match="no model chosen"):
        plain.complete("p", SCHEMA)
    assert not web.requests


def test_models_come_from_the_entry_or_the_service(web, monkeypatch):
    assert [m["id"] for m in gateway().options()] == ["model-a"] and not web.requests
    monkeypatch.setenv("K", "key")
    listing = api.OpenAICompatAPI("openai-api", entry({"type": "openai", "api_key_env": "K"}, "openai-api"))
    web.handler = lambda r: httpx.Response(200, json={"data": [
        {"id": "gpt-b"}, {"id": "text-embedding-3"}, {"id": "gpt-a"}, {"id": "whisper-1"}]})
    assert [m["id"] for m in listing.options()] == ["gpt-a", "gpt-b"]
    assert web.requests[0].headers["authorization"] == "Bearer key"
    listing.options()
    assert len(web.requests) == 1                      # cached
    listing.check_choice("gpt-a", "high")
    with pytest.raises(LLMError):
        listing.check_choice("gpt-a", "ultra")


# --- status --------------------------------------------------------------------

def test_status_tells_what_is_missing(web, monkeypatch):
    monkeypatch.delenv("K", raising=False)
    keyed = api.OpenAICompatAPI("openai-api", entry({"type": "openai", "api_key_env": "K"}, "openai-api"))
    assert keyed.login_status() == {"installed": True, "logged_in": False, "detail": "K not set"}
    assert keyed.login_hint.startswith("Set K in the environment")
    with pytest.raises(LLMError, match="K not set"):
        keyed.credential()
    with pytest.raises(LLMError, match="no browser sign-in"):
        keyed.start_login()
    monkeypatch.setenv("K", "key")
    assert keyed.login_status() == {"installed": True, "logged_in": True, "detail": "API key from K"}

    web.handler = lambda request: token_response()
    assert gateway().login_status()["detail"] == "OAuth2 token OK (T_CLIENT_ID)"
    oauth.invalidate(entry(GATEWAY)["oauth"])
    web.handler = lambda request: httpx.Response(401, json={"error": "invalid_client"})
    status = gateway().login_status()
    assert status["logged_in"] is False and "invalid_client" in status["detail"]
    local = api.OpenAICompatAPI("local", entry({"type": "openai", "base_url": "http://localhost:11434/v1"}))
    assert local.login_status()["detail"] == "no key needed" and local.credential() == ""


def test_a_run_without_credentials_says_which_variable_to_set(temp_db, monkeypatch):
    from stock_analyzer import runner

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run = db.one("SELECT * FROM runs WHERE id = ?",
                 (runner.run_all(provider="openai-api", tickers=["AAPL"]),))
    assert run["status"] == "failed"
    assert "OPENAI_API_KEY not set. Set OPENAI_API_KEY in the environment" in run["log"]


def test_dashboard_lists_api_providers_without_secrets(temp_db, web, monkeypatch):
    from stock_analyzer import server

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server, "_auth_cache", {})
    monkeypatch.setattr(server, "_auth", lambda name, refresh=False: (
        {"installed": True, "logged_in": None, "detail": "cli"} if name in ("claude", "codex")
        else get_provider(name).login_status()))
    monkeypatch.setattr(api.AnthropicAPI, "_list_models", lambda self: [self._option("claude-opus-5-5")])
    monkeypatch.setattr(server.PROVIDERS["codex"], "options", lambda self: [])  # no CLI call
    listed = server.providers()
    assert listed["anthropic-api"]["kind"] == "api" and listed["anthropic-api"]["logged_in"] is True
    assert listed["anthropic-api"]["default_model"] == "claude-opus-5-5"
    assert listed["openai-api"]["logged_in"] is False and "OPENAI_API_KEY" in listed["openai-api"]["hint"]
    assert listed["claude"]["kind"] == "cli" and listed["claude"]["hint"] == ""
    assert "sk-ant-secret-value" not in json.dumps(listed)
    assert server.status()["providers"]["anthropic-api"]["label"] == "Anthropic API"
    with pytest.raises(server.HTTPException) as refused:
        server.provider_login("openai-api", SimpleNamespace(headers={}))
    assert refused.value.status_code == 400
    assert server.put_provider("anthropic-api", server.ChoiceIn(model="claude-haiku-5-5", effort="low")) == {
        "model": "claude-haiku-5-5", "effort": "low"}
    assert config.load()["models"]["anthropic-api"] == "claude-haiku-5-5"


# --- Anthropic -------------------------------------------------------------------

def anthropic_api(monkeypatch, raw=None, **choice):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    raw = raw or {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}
    return api.AnthropicAPI("anthropic-api", entry(raw, "anthropic-api"), **choice)


def message(text='{"trend": "neutral"}', stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=stop_details, content=[
        SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])


def test_anthropic_request(monkeypatch):
    provider = anthropic_api(monkeypatch, effort="high")
    endpoint, kwargs = provider._request(provider.chosen_model(), "the prompt", SCHEMA)
    assert endpoint == "beta" and kwargs["fallbacks"] == "default"       # refusals fall back
    assert kwargs["betas"] == [api.REFUSAL_FALLBACK_BETA]
    assert kwargs["model"] == "claude-opus-5-5" and kwargs["max_tokens"] == api.ANTHROPIC_MAX_TOKENS
    assert kwargs["system"] and kwargs["messages"] == [{"role": "user", "content": "the prompt"}]
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "high"}
    assert "thinking" not in kwargs and "temperature" not in kwargs

    endpoint, kwargs = anthropic_api(monkeypatch, model="claude-haiku-5-5")._request(
        "claude-haiku-5-5", "p", SCHEMA)
    assert endpoint == "stable" and "fallbacks" not in kwargs and "effort" not in kwargs["output_config"]
    proxied = anthropic_api(monkeypatch, {"type": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
                                          "base_url": "https://proxy.example.com", "response_format": "none"})
    endpoint, kwargs = proxied._request("claude-opus-5-5", "p", SCHEMA)
    assert endpoint == "stable" and "output_config" not in kwargs


def test_anthropic_client_gets_the_key_from_the_named_variable(monkeypatch):
    client = anthropic_api(monkeypatch)._client()
    assert client.api_key == "sk-ant-test" and client.max_retries == api.RETRIES
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    provider = api.AnthropicAPI("anthropic-api", entry({"type": "anthropic",
                                                        "api_key_env": "ANTHROPIC_API_KEY"}, "anthropic-api"))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY not set"):
        provider.complete("p", SCHEMA)
    assert [m["id"] for m in provider.options()][:2] == ["claude-fable-5-2", "claude-opus-5-5"]
    assert provider.efforts_for("claude-opus-5-5") == ["low", "medium", "high", "xhigh", "max"]


def test_anthropic_answer_handling(monkeypatch):
    import anthropic

    provider = anthropic_api(monkeypatch)
    sent = []
    answers = [message()]
    monkeypatch.setattr(api.AnthropicAPI, "_send",
                        lambda self, endpoint, kwargs: sent.append(endpoint) or answers.pop(0))
    assert provider.complete("p", SCHEMA) == '{"trend": "neutral"}' and sent == ["beta"]
    answers.append(message(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))
    with pytest.raises(LLMError, match=r"declined the request \(cyber\)"):
        provider.complete("p", SCHEMA)
    answers.append(message(stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="cut off"):
        provider.complete("p", SCHEMA)

    def fail(error):
        def send(self, endpoint, kwargs):
            raise error
        monkeypatch.setattr(api.AnthropicAPI, "_send", send)
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    fail(anthropic.AuthenticationError("bad key", response=response, body=None))
    with pytest.raises(LLMError, match="rejected the credentials"):
        provider.complete("p", SCHEMA)
    fail(anthropic.APIConnectionError(request=response.request))
    with pytest.raises(LLMError, match="could not reach"):
        provider.complete("p", SCHEMA)
