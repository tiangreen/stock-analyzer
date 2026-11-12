from .base import LLMError, Provider
from .claude_cli import ClaudeCLI
from .codex_cli import CodexCLI

PROVIDERS: dict[str, type[Provider]] = {"claude": ClaudeCLI, "codex": CodexCLI}


def get_provider(name: str, model: str = "", timeout: int = 300, effort: str = "") -> Provider:
    if name not in PROVIDERS:
        raise LLMError(f"unknown provider '{name}' (choose from {', '.join(PROVIDERS)})")
    return PROVIDERS[name](model=model, timeout=timeout, effort=effort)


__all__ = ["LLMError", "Provider", "PROVIDERS", "get_provider"]
