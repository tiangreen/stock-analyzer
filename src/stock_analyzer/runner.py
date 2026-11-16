"""Daily run over the watchlist. Also the command-line entry point."""
from __future__ import annotations

import argparse
import sys
import threading

from . import analyzer, config, data, db, discovery, scoring
from .llm import get_provider

_lock = threading.Lock()


def seed_watchlist(cfg: dict) -> None:
    """Fill an empty watchlist from config.yaml."""
    if db.watchlist():
        return
    for ticker in cfg["watchlist"]:
        db.add_ticker(data.normalize(str(ticker)), {})


def mark_interrupted() -> None:
    """Runs left 'running' by a process that died can never finish."""
    db.execute("UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
               (db.now(),))


def is_running() -> bool:
    return _lock.locked()


def run_all(trigger: str = "manual", provider: str | None = None, force: bool = False,
            tickers: list[str] | None = None, echo: bool = False) -> int | None:
    """Analyze every watchlist ticker. Returns the run id, or None if a run is in progress."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        cfg = config.load()
        provider = provider or cfg["provider"]
        tickers = tickers or [w["ticker"] for w in db.watchlist()]
        run_id = db.execute(
            "INSERT INTO runs(started_at, trigger, provider, status, progress, log) "
            "VALUES(?, ?, ?, 'running', ?, '')",
            (db.now(), trigger, provider, f"0/{len(tickers)}"),
        )
        log: list[str] = []
        failed = 0

        auth = get_provider(provider).login_status()
        if auth["logged_in"] is False:
            message = f"{provider}: {auth['detail']}. Sign in from the dashboard header and run again."
            if echo:
                print(message, flush=True)
            db.execute("UPDATE runs SET status = 'failed', finished_at = ?, log = ? WHERE id = ?",
                       (db.now(), f"FAILED - {message}", run_id))
            return run_id

        def note(line: str, progress: str) -> None:
            log.append(line)
            if echo:
                print(line, flush=True)
            db.execute("UPDATE runs SET progress = ?, log = ? WHERE id = ?",
                       (progress, "\n".join(log), run_id))

        for i, ticker in enumerate(tickers, 1):
            db.execute("UPDATE runs SET progress = ? WHERE id = ?",
                       (f"{i - 1}/{len(tickers)} - analyzing {ticker}", run_id))
            try:
                out = analyzer.analyze_ticker(ticker, provider, cfg, force=force)
                line = (f"{ticker}: {out['trend']}, signal {out['signal'] or 'none'} "
                        f"({out['run_date']})" if out["status"] == "done"
                        else f"{ticker}: already analyzed for {out['run_date']}, skipped")
            except Exception as exc:  # one bad ticker must not stop the run
                failed += 1
                line = f"{ticker}: FAILED - {exc}"
            note(line, f"{i}/{len(tickers)}")

        graded = scoring.score_open()
        if graded:
            log.append(f"Graded {graded} matured forecasts")
        status = "ok" if not failed else "failed" if failed == len(tickers) else "partial"
        db.execute("UPDATE runs SET status = ?, finished_at = ?, log = ? WHERE id = ?",
                   (status, db.now(), "\n".join(log), run_id))
        return run_id
    finally:
        _lock.release()


def run_discovery(trigger: str = "manual", provider: str | None = None,
                  echo: bool = False, regions: list[str] | None = None) -> int | None:
    """Find stock ideas (discovery.run) and log it as a run with trigger 'discover'. Shares
    the lock with the daily run. `regions` overrides the markets selected in the dashboard
    for this run. Returns the run id, or None if a run is in progress."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        cfg = config.load()
        if regions:
            cfg["discovery"]["regions"] = regions
        provider = provider or cfg["provider"]
        run_id = db.execute(
            "INSERT INTO runs(started_at, trigger, provider, status, progress, log) "
            "VALUES(?, 'discover', ?, 'running', '0/6', '')", (db.now(), provider))
        log: list[str] = []

        def note(line: str | None, progress: str | None = None) -> None:
            if line:
                log.append(line)
                if echo:
                    print(line, flush=True)
            if progress:
                db.execute("UPDATE runs SET progress = ? WHERE id = ?", (progress, run_id))
            db.execute("UPDATE runs SET log = ? WHERE id = ?", ("\n".join(log), run_id))

        status = "failed"
        try:
            auth = get_provider(provider).login_status()
            if auth["logged_in"] is False:
                raise discovery.LLMError(f"{provider}: {auth['detail']}. Sign in from the "
                                         "dashboard header and run again.")
            discovery.run(provider, cfg, note)
            discovery.reset_cache()
            status = "partial" if any("FAILED" in line for line in log) else "ok"
            note(None, "6/6")
        except Exception as exc:  # the run log must say why, whatever went wrong
            note(f"FAILED - {exc}")
        db.execute("UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                   (status, db.now(), run_id))
        return run_id
    finally:
        _lock.release()


def start_background(**kwargs) -> bool:
    """Start run_all on a thread. False if a run is already in progress."""
    if is_running():
        return False
    threading.Thread(target=run_all, kwargs=kwargs, daemon=True, name="daily-run").start()
    return True


def start_discovery(**kwargs) -> bool:
    """Start run_discovery on a thread. False if a run is already in progress."""
    if is_running():
        return False
    threading.Thread(target=run_discovery, kwargs=kwargs, daemon=True, name="discovery").start()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the stock analysis now.")
    parser.add_argument("--ticker", action="append", help="only this ticker (repeatable); "
                        "added to the watchlist if missing")
    parser.add_argument("--provider", choices=config.PROVIDERS)
    parser.add_argument("--force", action="store_true", help="redo tickers already analyzed today")
    args = parser.parse_args()

    cfg = config.load()
    seed_watchlist(cfg)
    tickers = None
    if args.ticker:
        tickers = [data.normalize(t) for t in args.ticker]
        known = {w["ticker"] for w in db.watchlist()}
        for t in tickers:
            if t not in known:
                db.add_ticker(t, {})
    run_id = run_all(trigger="cli", provider=args.provider, force=args.force, tickers=tickers,
                     echo=True)
    status = db.one("SELECT status FROM runs WHERE id = ?", (run_id,))["status"]
    print(f"Run {run_id}: {status}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
