#!/usr/bin/env bash
# Starts the dashboard server (which also runs the daily schedule) on Linux and WSL2.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# On a Windows drive (/mnt/c, /mnt/g, ...) the project's .venv belongs to Windows and the
# mount is slow, so the Linux environment goes into the home directory instead.
if [ -n "${STOCK_ANALYZER_VENV:-}" ]; then
    venv="$STOCK_ANALYZER_VENV"
elif [ "$(stat -f -c %T .)" = "v9fs" ]; then
    venv="$HOME/.venvs/stock-analyzer"
else
    venv=".venv"
fi

if [ ! -x "$venv/bin/python" ]; then
    if ! python3 -c "import ensurepip" 2>/dev/null; then
        echo "Python's venv module is missing. Install it with: sudo apt install python3-venv" >&2
        exit 1
    fi
    python3 -m venv "$venv"
    "$venv/bin/python" -m pip install -q -e .
fi

# CLIs installed with npm or the native installers usually live here; a non-interactive
# shell (cron, systemd, `wsl -e`) does not get these from ~/.bashrc.
for dir in "$HOME/.local/bin" "$HOME/.npm-global/bin"; do
    case ":$PATH:" in *":$dir:"*) ;; *) [ -d "$dir" ] && PATH="$dir:$PATH" ;; esac
done
export PATH

exec "$venv/bin/python" -m stock_analyzer.server
