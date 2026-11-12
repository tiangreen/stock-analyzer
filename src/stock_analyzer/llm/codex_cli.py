"""OpenAI GPT models through the Codex CLI, using the ChatGPT subscription login."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import subprocess
import time

from .base import NO_WINDOW, SYSTEM_PROMPT, LLMError, Provider

MODELS_TTL = 3600
_models_cache: tuple[float, list[dict]] | None = None


def parse_models(text: str) -> list[dict]:
    """Turn `codex debug models` JSON into [{'id', 'label', 'efforts'}], best models first."""
    listed = [m for m in json.loads(text).get("models", [])
              if m.get("slug") and m.get("visibility", "list") == "list"]
    listed.sort(key=lambda m: m.get("priority", 999))
    return [{
        "id": m["slug"],
        "label": m.get("display_name") or m["slug"],
        "efforts": [level["effort"] for level in m.get("supported_reasoning_levels", [])
                    if level.get("effort")],
    } for m in listed]


class CodexCLI(Provider):
    name = "codex"
    executable = "codex"
    login_hint = "Sign in with the dashboard's Sign in button or `codex login` (ChatGPT account)."
    status_args = ["login", "status"]
    login_args = ["login"]

    def _signed_in(self, proc) -> tuple[bool, str]:
        text = (proc.stdout + proc.stderr).strip()
        first = text.splitlines()[0] if text else ""
        if proc.returncode != 0 or not first.lower().startswith("logged in"):
            return False, "not signed in"
        method = first[len("Logged in using "):] if first.lower().startswith("logged in using ") else ""
        return True, f"signed in ({method})" if method else "signed in"

    def child_env(self) -> dict:
        # Drop API keys so usage goes to the ChatGPT subscription login.
        return {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}

    def options(self) -> list[dict]:
        """Models this ChatGPT account offers, from `codex debug models` (cached for an hour)."""
        global _models_cache
        if _models_cache and time.monotonic() - _models_cache[0] < MODELS_TTL:
            return _models_cache[1]
        try:
            proc = subprocess.run(
                [self.find(), "debug", "models"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(), env=self.child_env(), timeout=30,
                creationflags=NO_WINDOW,
            )
            models = parse_models(proc.stdout)
        except Exception:
            models = []
        if models:
            _models_cache = (time.monotonic(), models)
            return models
        # Listing failed (offline, signed out): keep the last known list; Settings still
        # offers "CLI default" and a custom model name.
        return _models_cache[1] if _models_cache else []

    def cli_args(self, schema_path: str, answer_path: str) -> list[str]:
        args = [
            "exec",
            "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "--sandbox", "read-only",
            "--output-schema", schema_path,
            "--output-last-message", answer_path,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            # Unquoted on purpose: Codex reads a non-TOML value as a plain string, and quotes
            # would not survive the Windows .cmd shim.
            args += ["-c", f"model_reasoning_effort={self.effort}"]
        args.append("-")  # prompt on stdin
        return args

    def complete(self, prompt: str, schema: dict) -> str:
        with tempfile.TemporaryDirectory(prefix="stock-analyzer-") as work:
            schema_path = Path(work) / "schema.json"
            answer_path = Path(work) / "answer.txt"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            args = [self.find(), *self.cli_args(str(schema_path), str(answer_path))]
            proc = self.run(args, f"{SYSTEM_PROMPT}\n\n{prompt}", cwd=work)
            answer = answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
        if not answer.strip():
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            lowered = detail.lower()
            hint = f" {self.login_hint}" if "login" in lowered or "auth" in lowered else ""
            raise LLMError(f"codex: exit {proc.returncode}: {detail or 'no answer'}.{hint}")
        return answer
