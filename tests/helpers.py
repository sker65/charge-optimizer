"""Synthetische evcc-States für Szenario-Tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from batterieoptimizer.config import Settings
from batterieoptimizer.profile import LoadProfile

TZ = ZoneInfo("Europe/Berlin")
Q = timedelta(minutes=15)


def cfg(**overrides) -> Settings:
    return Settings(_env_file=None, state_dir="/nonexistent", **overrides)


def flat_profile(kwh_per_quarter: float = 0.15) -> LoadProfile:
    buckets = {(w, q): kwh_per_quarter for w in (False, True) for q in range(96)}
    return LoadProfile(
        buckets=buckets,
        fallback={False: kwh_per_quarter, True: kwh_per_quarter},
        tz=TZ,
        samples=96 * 21,
    )


def winter_prices(local: datetime) -> float:
    h = local.hour
    if h >= 22 or h < 6:
        return 0.20
    if 6 <= h < 9:
        return 0.38
    if 17 <= h < 21:
        return 0.40
    return 0.33


def cloudy_pv(local: datetime) -> float:
    return 500.0 if 10 <= local.hour < 15 else 0.0


def sunny_pv(local: datetime) -> float:
    return 6000.0 if 9 <= local.hour < 16 else 0.0


def make_state(
    start: datetime,
    hours: float,
    *,
    price: Callable[[datetime], float] = winter_prices,
    pv: Callable[[datetime], float] = cloudy_pv,
    soc: float = 30,
    limit: float | None = None,
    hourly_after: float | None = None,
) -> dict:
    """evcc-State ab ``start`` (lokal, aware) über ``hours`` Stunden.

    hourly_after: ab so vielen Stunden nur noch 1-h-Preisslots (wie in echt).
    """
    start_utc = start.astimezone(ZoneInfo("UTC"))
    end_utc = start_utc + timedelta(hours=hours)
    grid = []
    t = start_utc
    while t < end_utc:
        hourly = hourly_after is not None and t >= start_utc + timedelta(hours=hourly_after)
        step = timedelta(hours=1) if hourly and t.minute == 0 else Q
        grid.append([int(t.timestamp()), int((t + step).timestamp()), price(t.astimezone(TZ))])
        t += step
    solar = []
    t = start_utc
    while t <= end_utc:
        solar.append([int(t.timestamp()), pv(t.astimezone(TZ))])
        t += Q
    feedin = [[a, b, 0.077] for a, b, _ in grid]
    return {
        "forecast": {"grid": grid, "feedin": feedin, "solar": {"timeseries": solar}},
        "battery": {"soc": soc, "capacity": 12.5},
        "batteryGridChargeLimit": limit,
        "batteryMode": "normal",
        "tariffGrid": grid[0][2],
        "tariffFeedIn": 0.077,
        "pvPower": 0,
    }
