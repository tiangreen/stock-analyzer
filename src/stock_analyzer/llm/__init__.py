from .. import config
from .base import LLMError, Provider
from .claude_cli import ClaudeCLI
from .codex_cli import CodexCLI

# The subscription CLIs. API providers are the `api_providers` entries of config.yaml.
PROVIDERS: dict[str, type[Provider]] = {"claude": ClaudeCLI, "codex": CodexCLI}


def provider_names(cfg: dict | None = None) -> list[str]:
    return config.provider_names(cfg or config.load())


def get_provider(name: str, model: str = "", timeout: int = 300, effort: str = "",
                 cfg: dict | None = None) -> Provider:
    """The provider called `name`. `cfg` (a loaded config) saves reading it again."""
    if name in PROVIDERS:
        return PROVIDERS[name](model=model, timeout=timeout, effort=effort)
    entry = (cfg or config.load()).get("api_providers", {}).get(name)
    if not entry:
        raise LLMError(f"unknown provider '{name}' (choose from {', '.join(provider_names(cfg))})")
    from .api import TYPES  # late import: only needed once an API provider is used

    return TYPES[entry["type"]](name, entry, model=model, timeout=timeout, effort=effort)


__all__ = ["LLMError", "Provider", "PROVIDERS", "get_provider", "provider_names"]
