"""OAuth2 client-credentials tokens for model APIs behind machine-to-machine authorization.

The client ID and secret are read from the environment variables that config.yaml names.
Tokens are cached in memory until shortly before they expire and never logged or stored.
"""
from __future__ import annotations

import os
import threading
import time

from .base import LLMError

TOKEN_TIMEOUT = 15   # seconds for one request to the token endpoint
EXPIRY_MARGIN = 60   # fetch a new token this long before the old one expires
DEFAULT_LIFETIME = 3600  # used when the token endpoint does not say

transport = None  # tests put an httpx.MockTransport here
_lock = threading.Lock()
_cache: dict[tuple, tuple[str, float]] = {}


def http_client(timeout: float):
    """An httpx client for model and token endpoints. Redirects are not followed, so a
    bearer token or key is only ever sent to the host named in config.yaml."""
    try:
        import httpx
    except ImportError:
        raise LLMError("The httpx package is missing. Run `pip install -e .` in the project.") from None
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)


def _key(cfg: dict) -> tuple:
    return (cfg["token_url"], os.environ.get(cfg["client_id_env"], ""), cfg.get("scope", ""),
            cfg.get("audience", ""))


def missing_env(cfg: dict) -> list[str]:
    """Names of the credential variables that are not set."""
    return [env for env in (cfg["client_id_env"], cfg["client_secret_env"])
            if not os.environ.get(env, "").strip()]


def invalidate(cfg: dict) -> None:
    """Forget the cached token, for example after the API answered 401."""
    with _lock:
        _cache.pop(_key(cfg), None)


def token(cfg: dict) -> str:
    """A valid access token for the `oauth` section of an API provider (cached)."""
    missing = missing_env(cfg)
    if missing:
        raise LLMError(f"{' and '.join(missing)} not set")
    with _lock:  # held while fetching, so parallel callers share one token request
        key = _key(cfg)
        cached = _cache.get(key)
        if cached and time.monotonic() < cached[1] - EXPIRY_MARGIN:
            return cached[0]
        value, lifetime = _fetch(cfg)
        _cache[key] = (value, time.monotonic() + lifetime)
        return value


def _fetch(cfg: dict) -> tuple[str, float]:
    import httpx

    client_id = os.environ[cfg["client_id_env"]].strip()
    secret = os.environ[cfg["client_secret_env"]].strip()
    form = {"grant_type": "client_credentials"}
    for field in ("scope", "audience"):
        if cfg.get(field):
            form[field] = cfg[field]
    auth = None
    if cfg.get("auth_style", "basic") == "body":
        form.update(client_id=client_id, client_secret=secret)
    else:
        auth = httpx.BasicAuth(client_id, secret)
    host = httpx.URL(cfg["token_url"]).host
    try:
        with http_client(TOKEN_TIMEOUT) as client:
            response = client.post(cfg["token_url"], data=form, auth=auth,
                                   headers={"Accept": "application/json"})
    except httpx.TimeoutException:
        raise LLMError(f"token endpoint {host} did not answer within {TOKEN_TIMEOUT}s") from None
    except httpx.HTTPError as exc:
        raise LLMError(f"token endpoint {host} unreachable ({type(exc).__name__})") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code >= 400:
        reason = " - ".join(str(body[k])[:200] for k in ("error", "error_description") if body.get(k))
        raise LLMError(f"token endpoint {host} refused the client credentials "
                       f"(HTTP {response.status_code}{': ' + reason if reason else ''})")
    value = body.get("access_token")
    if not isinstance(value, str) or not value:
        raise LLMError(f"token endpoint {host} returned no access_token")
    try:
        lifetime = float(body.get("expires_in") or DEFAULT_LIFETIME)
    except (TypeError, ValueError):
        lifetime = DEFAULT_LIFETIME
    return value, max(lifetime, 0.0)
