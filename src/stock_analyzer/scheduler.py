"""Daily trigger that lives inside the dashboard server process."""
from __future__ import annotations

import re
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, db, runner

JOB_ID = "daily-run"
_scheduler: BackgroundScheduler | None = None


def parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    if not match:
        raise ValueError("time must be HH:MM (24h)")
    return int(match.group(1)), int(match.group(2))


def _fire() -> None:
    runner.run_all(trigger="schedule")


def apply(schedule: dict) -> None:
    """(Re)register the job from a schedule dict {enabled, time, weekdays_only}."""
    if _scheduler is None:
        return
    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)
    if not schedule.get("enabled"):
        return
    hour, minute = parse_time(schedule["time"])
    days = "mon-fri" if schedule.get("weekdays_only", True) else "*"
    _scheduler.add_job(
        _fire, CronTrigger(day_of_week=days, hour=hour, minute=minute), id=JOB_ID,
        misfire_grace_time=3600, coalesce=True, max_instances=1,
    )


def next_run() -> str | None:
    job = _scheduler.get_job(JOB_ID) if _scheduler else None
    return job.next_run_time.isoformat(timespec="minutes") if job and job.next_run_time else None


def missed_today(schedule: dict, now: datetime | None = None) -> bool:
    """True if today's scheduled time has passed and no run has started since then."""
    now = now or datetime.now()
    if not schedule.get("enabled"):
        return False
    if schedule.get("weekdays_only", True) and now.weekday() >= 5:
        return False
    hour, minute = parse_time(schedule["time"])
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < due:
        return False
    ran = db.one("SELECT 1 AS x FROM runs WHERE started_at >= ? AND trigger != 'single'",
                 (due.isoformat(timespec="seconds"),))
    return ran is None


def start() -> None:
    global _scheduler
    schedule = config.load()["schedule"]
    _scheduler = BackgroundScheduler()
    _scheduler.start()
    apply(schedule)
    if missed_today(schedule):
        runner.start_background(trigger="catch-up")


def stop() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
