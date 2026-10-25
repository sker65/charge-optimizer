"""Zeitumstellung und Lastprofil."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from batterieoptimizer.decide import decide
from batterieoptimizer.models import parse_state
from batterieoptimizer.profile import ProfileError, build_profile
from batterieoptimizer.service import next_run

from .helpers import TZ, cfg, flat_profile, make_state


@pytest.mark.parametrize(
    ("start", "day_hours"),
    [
        (datetime(2026, 10, 24, 14, 0, tzinfo=TZ), 25),  # Sommer -> Winter am 25.10.
        (datetime(2027, 3, 27, 14, 0, tzinfo=TZ), 23),  # Winter -> Sommer am 28.03.
    ],
)
def test_dst_horizon(start, day_hours):
    hours = 34
    out = decide(parse_state(make_state(start, hours)), flat_profile(), cfg(), start)
    assert out.decision.decision != "error"
    steps = out.plan.steps
    # lückenloses UTC-Raster über die Umstellung
    assert len(steps) == hours * 4
    for a, b in itertools.pairwise(steps):
        assert a.slot.end == b.slot.start
    # Verbrauch des Kalendertags mit Umstellung entspricht seiner Stundenzahl
    day = start.date() + timedelta(days=1)
    load = sum(s.slot.load_kwh for s in steps if s.slot.start.astimezone(TZ).date() == day)
    hours_in_window = sum(1 for s in steps if s.slot.start.astimezone(TZ).date() == day) / 4
    assert hours_in_window <= day_hours
    assert load == pytest.approx(hours_in_window * 4 * 0.15)


def _history(days: int, skip_every: int = 0) -> list[dict]:
    data = []
    t = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(days * 96):
        if skip_every and i % skip_every == 0:
            t += timedelta(minutes=15)
            continue
        local = t.astimezone(TZ)
        energy = 0.3 if 18 <= local.hour < 21 else 0.1
        data.append(
            {
                "start": local.isoformat(),
                "end": (t + timedelta(minutes=15)).astimezone(TZ).isoformat(),
                "energy": energy,
                "returnEnergy": 0,
            }
        )
        t += timedelta(minutes=15)
    return [{"title": "home", "group": "home", "data": data}]


def test_profile_by_local_time():
    profile = build_profile(_history(14), TZ)
    assert profile.kwh_per_quarter(datetime(2026, 11, 3, 19, 0, tzinfo=TZ)) == pytest.approx(0.3)
    assert profile.kwh_per_quarter(datetime(2026, 11, 3, 3, 0, tzinfo=TZ)) == pytest.approx(0.1)


def test_profile_tolerates_gaps():
    profile = build_profile(_history(14, skip_every=7), TZ)
    assert profile.kwh_per_quarter(datetime(2026, 11, 3, 19, 0, tzinfo=TZ)) == pytest.approx(0.3)


def test_profile_needs_enough_data():
    with pytest.raises(ProfileError):
        build_profile(_history(1), TZ)
    with pytest.raises(ProfileError):
        build_profile([{"group": "loadpoint", "data": []}], TZ)


def test_next_run():
    now = datetime(2026, 10, 25, 14, 7, 3, tzinfo=UTC)
    assert next_run(now, 20) == datetime(2026, 10, 25, 14, 15, 20, tzinfo=UTC)
    now = datetime(2026, 10, 25, 14, 15, 5, tzinfo=UTC)
    assert next_run(now, 20) == datetime(2026, 10, 25, 14, 15, 20, tzinfo=UTC)
