"""Models reached over HTTPS with an API key or an OAuth2 token, instead of a subscription CLI.

Each provider is one entry of `api_providers` in config.yaml (checked by config.load). The
entry names the environment variables that hold the secrets; this module reads them only at
the moment of a call and never returns, stores or logs them.

  type: anthropic  Anthropic Messages API through the official SDK
  type: openai     POST {base_url}/chat/completions, the format OpenAI and every
                   OpenAI-compatible service or gateway speaks
"""
from __future__ import annotations

import importlib.util
import os
import re
import time

from . import oauth
from .base import SYSTEM_PROMPT, LLMError, Provider
from .claude_cli import EFFORTS as ANTHROPIC_EFFORTS

MODELS_TTL = 3600        # a successful model listing is kept this long
MODELS_RETRY = 300       # a failed one is not retried before this
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
ANTHROPIC_FALLBACK_LIST = [("claude-fable-5-2", "Claude Fable 5.2"), ("claude-opus-5-5", "Claude Opus 5.5"),
                           ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5")]
ANTHROPIC_MAX_TOKENS = 64000  # thinking counts towards it; streamed, so no HTTP timeout
# A request these models decline for safety is re-run on Anthropic's recommended fallback
# model inside the same call, instead of failing.
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
REFUSAL_FALLBACK_MODELS = ("claude-fable-5-1", "claude-fable-5-2", "claude-opus-5", "claude-sonnet-5-5")
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|image|audio|realtime|moderation|transcribe|"
                             r"sora|babbage|davinci|search", re.I)
RETRY_STATUS = (429, 502, 503, 504)
RETRIES = 2

_sleep = time.sleep  # tests replace it
_models: dict[str, tuple[float, list[dict]]] = {}  # provider name -> (valid until, models)


class ApiProvider(Provider):
    kind = "api"
    package = ""  # the importable package the calls need

    def __init__(self, name: str, entry: dict, model: str = "", timeout: int = 300, effort: str = ""):
        super().__init__(model, timeout, effort)
        self.name = name
        self.entry = entry
        self.label = entry["label"]
        self.default_model = entry.get("model") or self.default_model

    # --- credentials ------------------------------------------------------

    def _missing(self) -> list[str]:
        """Environment variables this provider needs that are not set."""
        if self.entry.get("oauth"):
            return oauth.missing_env(self.entry["oauth"])
        env = self.entry.get("api_key_env")
        return [env] if env and not os.environ.get(env, "").strip() else []

    @property
    def login_hint(self) -> str:
        names = " and ".join(self._missing()) or "the credentials"
        return f"Set {names} in the environment the app is started from, then restart it."

    def credential(self) -> str:
        """The API key or a current OAuth2 access token ('' for an endpoint without one)."""
        missing = self._missing()
        if missing:
            raise LLMError(f"{self.name}: {' and '.join(missing)} not set. {self.login_hint}")
        if self.entry.get("oauth"):
            try:
                return oauth.token(self.entry["oauth"])
            except LLMError as exc:
                raise LLMError(f"{self.name}: {exc}") from None
        env = self.entry.get("api_key_env")
        return os.environ[env].strip() if env else ""

    def available(self) -> bool:
        return importlib.util.find_spec(self.package) is not None

    def find(self) -> str:
        raise LLMError(f"{self.name} is an API provider and has no CLI")

    def login_status(self) -> dict:
        if not self.available():
            return {"installed": False, "logged_in": False,
                    "detail": f"Python package `{self.package}` is missing: run `pip install -e .`"}
        missing = self._missing()
        if missing:
            return {"installed": True, "logged_in": False, "detail": f"{' and '.join(missing)} not set"}
        if self.entry.get("oauth"):
            try:  # proves the client credentials work; the token is cached, so this is cheap
                oauth.token(self.entry["oauth"])
            except LLMError as exc:
                return {"installed": True, "logged_in": False, "detail": str(exc)}
            return {"installed": True, "logged_in": True,
                    "detail": f"OAuth2 token OK ({self.entry['oauth']['client_id_env']})"}
        env = self.entry.get("api_key_env")
        return {"installed": True, "logged_in": True,
                "detail": f"API key from {env}" if env else "no key needed"}

    def start_login(self, wait: float = 20) -> dict:
        raise LLMError(f"{self.label} has no browser sign-in. {self.login_hint}")

    # --- models -----------------------------------------------------------

    def chosen_model(self) -> str:
        model = self.model or self.default_model
        if not model:
            raise LLMError(f"{self.name}: no model chosen. Pick one in Settings or set `model` "
                           "for this provider in config.yaml.")
        return model

    def _list_models(self) -> list[dict]:
        """Ask the API which models it offers. May raise anything."""
        raise NotImplementedError

    def _unlisted(self) -> list[dict]:
        """What Settings offers while the API cannot be asked."""
        return []

    def _option(self, model_id: str, label: str = "") -> dict:
        return {"id": model_id, "label": label or model_id, "efforts": list(self.default_efforts)}

    def options(self) -> list[dict]:
        if self.entry.get("models"):
            return [self._option(m) for m in self.entry["models"]]
        cached = _models.get(self.name)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        if self._missing() or not self.available():
            return self._unlisted()
        try:
            models, ttl = self._list_models(), MODELS_TTL
        except Exception:  # offline, wrong key, no /models route: Settings still takes a custom name
            models, ttl = (cached[1] if cached else self._unlisted()), MODELS_RETRY
        _models[self.name] = (time.monotonic() + ttl, models)
        return models


class AnthropicAPI(ApiProvider):
    package = "anthropic"
    default_model = ANTHROPIC_DEFAULT_MODEL
    default_efforts = ANTHROPIC_EFFORTS

    def _client(self, timeout: float | None = None):
        import anthropic

        kwargs: dict = {"timeout": timeout or self.timeout, "max_retries": RETRIES}
        if self.entry.get("oauth"):
            kwargs["auth_token"] = self.credential()
        else:
            kwargs["api_key"] = self.credential()
        if self.entry.get("base_url"):
            kwargs["base_url"] = self.entry["base_url"]
        if self.entry.get("headers"):
            kwargs["default_headers"] = self.entry["headers"]
        return anthropic.Anthropic(**kwargs)

    def _unlisted(self) -> list[dict]:
        return [self._option(m, label) for m, label in ANTHROPIC_FALLBACK_LIST]

    def _list_models(self) -> list[dict]:
        return [self._option(m.id, getattr(m, "display_name", "") or m.id)
                for m in self._client(timeout=20).models.list(limit=100)]

    def _request(self, model: str, prompt: str, schema: dict) -> tuple[str, dict]:
        """('beta' or 'stable' endpoint, keyword arguments of the Messages request)."""
        output: dict = {}
        if self.entry.get("response_format") != "none":
            output["format"] = {"type": "json_schema", "schema": schema}
        if self.effort:
            output["effort"] = self.effort
        kwargs: dict = {
            "model": model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        if output:
            kwargs["output_config"] = output
        if not self.entry.get("base_url") and model.startswith(REFUSAL_FALLBACK_MODELS):
            return "beta", {**kwargs, "betas": [REFUSAL_FALLBACK_BETA], "fallbacks": "default"}
        return "stable", kwargs

    def _send(self, endpoint: str, kwargs: dict):
        client = self._client()
        messages = client.beta.messages if endpoint == "beta" else client.messages
        deadline = time.monotonic() + self.timeout
        with messages.stream(**kwargs) as stream:
            for _ in stream:  # streamed so a long answer cannot hit an idle HTTP timeout
                if time.monotonic() > deadline:
                    raise LLMError(f"{self.name}: no answer within {self.timeout}s")
            return stream.get_final_message()

    def complete(self, prompt: str, schema: dict) -> str:
        import anthropic

        model = self.chosen_model()
        endpoint, kwargs = self._request(model, prompt, schema)
        try:
            try:
                message = self._send(endpoint, kwargs)
            except anthropic.AuthenticationError:
                if not self.entry.get("oauth"):
                    raise
                oauth.invalidate(self.entry["oauth"])  # the token may have been revoked early
                message = self._send(endpoint, kwargs)
        except anthropic.AuthenticationError:
            raise LLMError(f"{self.name}: the API rejected the credentials (HTTP 401)") from None
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"{self.name}: not allowed (HTTP 403): {exc.message[:300]}") from None
        except anthropic.NotFoundError:
            raise LLMError(f"{self.name}: model '{model}' not found (HTTP 404)") from None
        except anthropic.RateLimitError:
            raise LLMError(f"{self.name}: rate limit reached (HTTP 429), try again later") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"{self.name}: HTTP {exc.status_code}: {exc.message[:300]}") from None
        except anthropic.APITimeoutError:
            raise LLMError(f"{self.name}: no answer within {self.timeout}s") from None
        except anthropic.APIConnectionError:
            raise LLMError(f"{self.name}: could not reach the API") from None
        if message.stop_reason == "refusal":
            category = getattr(message.stop_details, "category", None) or "safety"
            raise LLMError(f"{self.name}: the model declined the request ({category})")
        if message.stop_reason == "max_tokens":
            raise LLMError(f"{self.name}: the answer was cut off at {ANTHROPIC_MAX_TOKENS} tokens")
        return "".join(block.text for block in message.content if block.type == "text")


class OpenAICompatAPI(ApiProvider):
    package = "httpx"

    @property
    def base_url(self) -> str:
        return self.entry.get("base_url") or OPENAI_BASE_URL

    def _headers(self) -> dict:
        headers = dict(self.entry.get("headers") or {})
        secret = self.credential()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        import httpx

        url = self.base_url + path
        host = httpx.URL(url).host
        refreshed = False
        for attempt in range(RETRIES + 1):
            headers = self._headers()
            try:
                with oauth.http_client(timeout or self.timeout) as client:
                    response = client.request(method, url, json=body, headers=headers)
            except httpx.TimeoutException:
                raise LLMError(f"{self.name}: no answer within {timeout or self.timeout}s") from None
            except httpx.HTTPError as exc:
                raise LLMError(f"{self.name}: could not reach {host} ({type(exc).__name__})") from None
            status = response.status_code
            if status == 401 and self.entry.get("oauth") and not refreshed:
                oauth.invalidate(self.entry["oauth"])  # the token may have been revoked early
                refreshed = True
                continue
            if status in RETRY_STATUS and attempt < RETRIES:
                _sleep(_retry_delay(response, attempt))
                continue
            if status >= 400:
                raise LLMError(f"{self.name}: HTTP {status} from {host}: {_error_text(response)}")
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                raise LLMError(f"{self.name}: {host} did not answer with a JSON object")
            return data
        raise LLMError(f"{self.name}: HTTP 401 from {host}: the token was not accepted")

    def _list_models(self) -> list[dict]:
        listed = self._call("GET", "/models", timeout=20).get("data") or []
        ids = sorted(str(m["id"]) for m in listed if isinstance(m, dict) and m.get("id"))
        if self.base_url == OPENAI_BASE_URL:
            ids = [i for i in ids if not OPENAI_NOT_CHAT.search(i)]
        return [self._option(i) for i in ids]

    def request_body(self, model: str, prompt: str, schema: dict) -> dict:
        body: dict = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        wanted = self.entry.get("response_format")
        if wanted == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "strict": True, "schema": schema}}
        elif wanted == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def complete(self, prompt: str, schema: dict) -> str:
        data = self._call("POST", "/chat/completions",
                          self.request_body(self.chosen_model(), prompt, schema))
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if message.get("refusal"):
            raise LLMError(f"{self.name}: the model declined the request: {str(message['refusal'])[:200]}")
        content = message.get("content")
        if isinstance(content, list):  # some services answer in parts
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.name}: the answer was cut off by the token limit")
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"{self.name}: the answer has no text")
        return content


def _retry_delay(response, attempt: int) -> float:
    try:
        return min(max(float(response.headers.get("retry-after", "")), 0.0), 30.0)
    except ValueError:
        return 2.0 * (attempt + 1)


def _error_text(response) -> str:
    """The service's own error message, shortened; never the request or its headers."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return (str(error) if error else response.text or "no details").strip()[:300]


TYPES: dict[str, type[ApiProvider]] = {"anthropic": AnthropicAPI, "openai": OpenAICompatAPI}
