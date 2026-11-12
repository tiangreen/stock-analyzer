"""Anthropic models through the Claude Code CLI, using the Claude subscription login."""
from __future__ import annotations

import json
import os

from .base import SYSTEM_PROMPT, LLMError, Provider

# Kept so a long-lived token from `claude setup-token` or a custom config dir still works.
KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
MODELS = [("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # from `claude --help`


class ClaudeCLI(Provider):
    name = "claude"
    executable = "claude"
    login_hint = "Sign in with the dashboard's Sign in button or `claude auth login`."
    status_args = ["auth", "status"]
    login_args = ["auth", "login", "--claudeai"]  # subscription login, not API billing

    def _signed_in(self, proc) -> tuple[bool, str]:
        info = json.loads(proc.stdout)
        if not info.get("loggedIn"):
            return False, "not signed in"
        plan = info.get("subscriptionType") or info.get("authMethod") or "account"
        return True, f"signed in ({plan})"

    def child_env(self) -> dict:
        # Drop API keys (so usage goes to the subscription, not API billing) and any
        # variables inherited from a parent Claude Code session.
        return {
            k: v for k, v in os.environ.items()
            if k in KEEP_ENV or not k.upper().startswith(("CLAUDE", "ANTHROPIC"))
        }

    def options(self) -> list[dict]:
        # The CLI has no command that lists models; these are the aliases it accepts,
        # each resolving to the newest model of that family.
        return [{"id": m, "label": label, "efforts": EFFORTS} for m, label in MODELS]

    def cli_args(self) -> list[str]:
        args = [
            "-p", "--output-format", "json",
            "--tools", "",                 # pure completion: no file, shell or web tools
            "--strict-mcp-config",         # no MCP servers
            "--no-session-persistence",
            "--system-prompt", SYSTEM_PROMPT,
        ]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        return args

    def complete(self, prompt: str, schema: dict) -> str:
        proc = self.run([self.find(), *self.cli_args()], prompt)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise LLMError(f"claude: exit {proc.returncode}: {detail or 'no output'}") from None
        text = str(envelope.get("result") or "")
        if envelope.get("is_error") or proc.returncode != 0:
            hint = f" {self.login_hint}" if "authenticate" in text.lower() or "login" in text.lower() else ""
            raise LLMError(f"claude: {text[:300] or 'CLI reported an error'}.{hint}")
        return text
