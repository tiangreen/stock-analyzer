"""Shared provider plumbing: subprocess handling, JSON extraction, response validation."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

from .. import signals

SYSTEM_PROMPT = (
    "You are a disciplined technical and fundamental market analyst. "
    "Reply with a single JSON object only, no prose and no code fences."
)

MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}")  # it becomes a CLI argument
TRENDS = ("bullish", "bearish", "neutral")
DIRECTIONS = ("up", "down", "flat")


class LLMError(Exception):
    pass


class Provider:
    name = ""
    executable = ""
    login_hint = ""

    def __init__(self, model: str = "", timeout: int = 300, effort: str = ""):
        self.model = model or ""      # "" = let the CLI use its own default
        self.effort = effort or ""    # "" = the model's default reasoning effort
        self.timeout = timeout

    # --- model and effort choices -----------------------------------------

    default_efforts: list[str] = ["low", "medium", "high"]

    def options(self) -> list[dict]:
        """Models offered in Settings: [{'id', 'label', 'efforts': [...]}]."""
        return []

    def efforts_for(self, model: str) -> list[str]:
        """Effort levels valid for `model`; for an unlisted (custom) model, every known level."""
        listed = self.options()
        for option in listed:
            if option["id"] == model:
                return option["efforts"]
        known = [e for option in listed for e in option["efforts"]]
        return list(dict.fromkeys(known)) or self.default_efforts

    def check_choice(self, model: str, effort: str) -> None:
        """Raise LLMError unless model and effort are safe and valid to pass to the CLI."""
        if model and not MODEL_RE.fullmatch(model):
            raise LLMError("Model names may only contain letters, digits and . _ : - / [ ]")
        if effort and effort not in self.efforts_for(model):
            raise LLMError(f"Effort '{effort}' is not available for "
                           f"{model or 'the default model'} "
                           f"(choose from {', '.join(self.efforts_for(model))})")

    def find(self) -> str:
        path = which(self.executable)
        if not path:
            where = " inside WSL (the Windows install cannot be used from Linux)" if is_wsl() else ""
            raise LLMError(f"`{self.executable}` CLI not found on PATH{where}. {self.login_hint}")
        return path

    def available(self) -> bool:
        return which(self.executable) is not None

    def child_env(self) -> dict:
        return dict(os.environ)

    def run(self, args: list[str], prompt: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=cwd or tempfile.gettempdir(), env=self.child_env(),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"{self.name}: no answer within {self.timeout}s") from exc
        except OSError as exc:
            raise LLMError(f"{self.name}: could not start CLI ({exc})") from exc

    def complete(self, prompt: str, schema: dict) -> str:
        """Return the model's raw text answer."""
        raise NotImplementedError

    # --- sign-in ----------------------------------------------------------

    status_args: list[str] = []
    login_args: list[str] = []

    def _signed_in(self, proc: subprocess.CompletedProcess) -> tuple[bool, str]:
        """Interpret the CLI's status output as (signed in, short description)."""
        raise NotImplementedError

    def login_status(self) -> dict:
        """{'installed', 'logged_in' (None if unknown), 'detail'} from the CLI's own status command."""
        if not self.available():
            return {"installed": False, "logged_in": False, "detail": "CLI not installed"}
        try:
            proc = subprocess.run(
                [self.find(), *self.status_args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(), env=self.child_env(), timeout=30,
                creationflags=NO_WINDOW,
            )
            logged_in, detail = self._signed_in(proc)
        except Exception as exc:  # status is advisory; never let it break the dashboard
            return {"installed": True, "logged_in": None, "detail": f"status check failed: {exc}"}
        return {"installed": True, "logged_in": logged_in, "detail": detail}

    def login_command(self) -> str:
        return " ".join([self.executable, *self.login_args])

    def start_login(self, wait: float = 20) -> dict:
        """Start the CLI's browser sign-in in the background and return its authorize URL.

        The CLI opens the provider's authorize page in the default browser and receives the
        result on a local callback; this app never sees credentials. Returns
        {'url': authorize page or None, 'output': what the CLI printed so far}.
        """
        exe = self.find()
        cancel_login(self.name)
        try:
            proc = subprocess.Popen(
                [exe, *self.login_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                env={**self.child_env(), "NO_COLOR": "1"}, cwd=os.path.expanduser("~"),
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            raise LLMError(f"Could not start sign-in ({exc}). "
                           f"Run `{self.login_command()}` in a terminal instead.") from exc
        _logins[self.name] = proc
        lines: list[str] = []
        found = threading.Event()
        url: list[str] = []

        def read() -> None:
            for line in proc.stdout:
                line = ANSI_RE.sub("", line).strip()
                if line:
                    lines.append(line)
                match = URL_RE.search(line)
                if match and not url:
                    url.append(match.group(0))
                    found.set()
            found.set()  # process ended

        threading.Thread(target=read, daemon=True, name=f"{self.name}-login").start()
        timer = threading.Timer(LOGIN_TIMEOUT, _kill, args=(proc,))
        timer.daemon = True
        timer.start()
        found.wait(wait)
        return {"url": url[0] if url else None, "output": "\n".join(lines[-8:])}

    def analyze(self, prompt: str, schema: dict, horizons: list[int]) -> tuple[dict, str]:
        """Return (validated result, raw text). Retries once on a malformed answer."""
        last: Exception | None = None
        for _ in range(2):
            raw = self.complete(prompt, schema)
            try:
                return validate(extract_json(raw), horizons), raw
            except LLMError as exc:
                last = exc
        raise LLMError(f"{self.name}: unusable answer ({last})")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOGIN_TIMEOUT = 600  # an abandoned sign-in is stopped after this many seconds
URL_RE = re.compile(r"https://[^\s\"'<>]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_logins: dict[str, subprocess.Popen] = {}
WINDOWS_DRIVE_RE = re.compile(r"/mnt/[a-z](/|$)")


def is_wsl() -> bool:
    return os.name == "posix" and "microsoft" in os.uname().release.lower()


def which(executable: str) -> str | None:
    """shutil.which, except that under WSL the Windows drives on PATH are skipped.

    WSL appends the Windows PATH, so `claude` or `codex` can resolve to the Windows npm shim.
    That starts a Windows program which cannot read the Linux temp paths it is handed and
    uses the Windows login, so only a CLI installed inside WSL counts.
    """
    if not is_wsl():
        return shutil.which(executable)
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not WINDOWS_DRIVE_RE.match(d)]
    return shutil.which(executable, path=os.pathsep.join(dirs))


def _kill(proc: subprocess.Popen) -> None:
    """Stop a sign-in process and its children (the npm shim spawns the real CLI)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
    else:
        proc.kill()


def submit_login_code(name: str, code: str) -> None:
    """Hand the one-time code from the provider's authorize page to the waiting CLI."""
    proc = _logins.get(name)
    if not proc or proc.poll() is not None:
        raise LLMError("No sign-in is in progress. Click Sign in again.")
    try:
        proc.stdin.write(code.strip() + "\n")
        proc.stdin.flush()
    except OSError as exc:
        raise LLMError(f"Could not pass the code to the CLI ({exc})") from exc


def cancel_login(name: str) -> None:
    proc = _logins.pop(name, None)
    if proc:
        _kill(proc)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model answer, tolerating fences and surrounding prose."""
    text = (text or "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    raise LLMError("no JSON object in answer")


def _number(value, field: str, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LLMError(f"{field} is not a number") from None
    if number != number or (positive and number <= 0):
        raise LLMError(f"{field} out of range")
    return number


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list):
        raise LLMError(f"{field} is not a list")
    return [str(v).strip()[:400] for v in value if str(v).strip()][:10]


def validate(result: dict, horizons: list[int]) -> dict:
    """Check and normalize a model answer against the expected shape."""
    trend = str(result.get("trend", "")).lower().strip()
    if trend not in TRENDS:
        raise LLMError(f"trend must be one of {TRENDS}")
    summary = str(result.get("summary", "")).strip()
    if not summary:
        raise LLMError("summary missing")

    by_horizon = {}
    for raw in result.get("predictions") or []:
        if not isinstance(raw, dict):
            continue
        try:
            horizon = int(raw.get("horizon_days"))
        except (TypeError, ValueError):
            continue
        direction = str(raw.get("direction", "")).lower().strip()
        if direction not in DIRECTIONS:
            raise LLMError(f"direction must be one of {DIRECTIONS}")
        low = _number(raw.get("target_low"), "target_low")
        high = _number(raw.get("target_high"), "target_high")
        confidence = _number(raw.get("confidence"), "confidence", positive=False)
        if confidence > 1:  # tolerate 0-100 answers
            confidence /= 100
        by_horizon[horizon] = {
            "horizon_days": horizon, "direction": direction,
            "confidence": round(min(max(confidence, 0.0), 1.0), 3),
            "target_low": min(low, high), "target_high": max(low, high),
        }
    missing = [h for h in horizons if h not in by_horizon]
    if missing:
        raise LLMError(f"predictions missing for horizons {missing}")

    def optional(field: str):
        value = result.get(field)
        return None if value is None else _number(value, field)

    return {
        "trend": trend,
        "summary": summary[:4000],
        "key_signals": _strings(result.get("key_signals", []), "key_signals"),
        "risks": _strings(result.get("risks", []), "risks"),
        "support": optional("support"),
        "resistance": optional("resistance"),
        "predictions": [by_horizon[h] for h in horizons],
        # Lenient on purpose: a bad signal must not cost the forecast. None = no signal.
        "signal": signals.parse(result.get("signal"), horizons),
    }
