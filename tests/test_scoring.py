from datetime import datetime

import pytest

from stock_analyzer import db, scheduler, scoring


def pred(direction, horizon=1, base=100.0, low=99.0, high=103.0):
    return {"direction": direction, "horizon_days": horizon, "base_close": base,
            "target_low": low, "target_high": high}


def test_flat_band_widens_with_horizon():
    assert scoring.flat_band(1) == pytest.approx(0.005)
    assert scoring.flat_band(4) == pytest.approx(0.01)


def test_grade_up_hit_in_range():
    g = scoring.grade(pred("up"), 102.0)
    assert g["outcome"] == "hit" and g["in_range"] == 1
    assert g["actual_return"] == pytest.approx(0.02)


def test_grade_small_move_counts_as_flat():
    assert scoring.grade(pred("up"), 100.3)["outcome"] == "miss"
    assert scoring.grade(pred("flat"), 100.3)["outcome"] == "hit"
    assert scoring.grade(pred("flat"), 99.7)["outcome"] == "hit"


def test_grade_down_and_out_of_range():
    g = scoring.grade(pred("down"), 95.0)
    assert g["outcome"] == "hit" and g["in_range"] == 0


def test_grade_band_depends_on_horizon():
    # +1.5% is "up" over one day but "flat" over 20 days (band ~2.2%).
    assert scoring.grade(pred("up", horizon=1), 101.5)["outcome"] == "hit"
    assert scoring.grade(pred("flat", horizon=20), 101.5)["outcome"] == "hit"


def _seed(result_predictions):
    result = {"trend": "bullish", "summary": "s", "key_signals": [], "risks": [],
              "support": None, "resistance": None, "predictions": result_predictions}
    return db.save_analysis("TEST", "2026-01-05", "claude", "m", result, {}, "raw",
                            base_close=100.0, due_dates={})


def _bars(closes, start_day=5):
    with db.connect() as conn:
        for i, close in enumerate(closes):
            conn.execute("INSERT INTO prices VALUES(?,?,?,?,?,?,?)",
                         ("TEST", f"2026-01-{start_day + i:02d}", close, close, close, close, 1))


def test_score_open_uses_nth_trading_bar(temp_db):
    _seed([
        {"horizon_days": 1, "direction": "up", "confidence": 0.6, "target_low": 100, "target_high": 104},
        {"horizon_days": 3, "direction": "down", "confidence": 0.5, "target_low": 90, "target_high": 99},
        {"horizon_days": 5, "direction": "up", "confidence": 0.5, "target_low": 100, "target_high": 110},
    ])
    _bars([100, 102, 101, 97])  # run date bar + three later sessions
    assert scoring.score_open() == 2
    got = {p["horizon_days"]: p for p in db.rows("SELECT * FROM predictions")}
    assert got[1]["outcome"] == "hit" and got[1]["actual_close"] == 102
    assert got[3]["outcome"] == "hit" and got[3]["actual_close"] == 97 and got[3]["in_range"] == 1
    assert got[5]["outcome"] is None  # not enough sessions yet
    assert scoring.score_open() == 0  # idempotent

    acc = scoring.accuracy()
    assert acc["pending"] == 1
    assert {r["horizon_days"]: r["hit_rate"] for r in acc["by_horizon"]} == {1: 1.0, 3: 1.0}


def test_rerun_same_day_replaces_analysis(temp_db):
    p = [{"horizon_days": 1, "direction": "up", "confidence": 0.6, "target_low": 1, "target_high": 2}]
    _seed(p)
    _seed(p)
    assert db.one("SELECT COUNT(*) AS n FROM analyses")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM predictions")["n"] == 1


def test_parse_time():
    assert scheduler.parse_time("22:30") == (22, 30)
    assert scheduler.parse_time("7:05") == (7, 5)
    for bad in ("24:00", "12:60", "noon", ""):
        with pytest.raises(ValueError):
            scheduler.parse_time(bad)


def test_missed_today(temp_db):
    sched = {"enabled": True, "time": "22:30", "weekdays_only": True}
    wed_late = datetime(2026, 11, 11, 23, 0)
    assert scheduler.missed_today(sched, wed_late) is True
    assert scheduler.missed_today(sched, datetime(2026, 11, 11, 21, 0)) is False
    assert scheduler.missed_today(sched, datetime(2026, 11, 14, 23, 0)) is False  # Saturday
    assert scheduler.missed_today({**sched, "enabled": False}, wed_late) is False
    db.execute("INSERT INTO runs(started_at, trigger, status) VALUES(?, 'schedule', 'ok')",
               ("2026-11-11T22:30:05",))
    assert scheduler.missed_today(sched, wed_late) is False
