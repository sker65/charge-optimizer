"""Lastprofil des Hauses aus der evcc-Historie (ohne Ladepunkt).

Pro Tagtyp (Werktag/Wochenende) und Viertelstunde der lokalen Uhrzeit wird der Mittelwert
der verbrauchten Energie gebildet. Der Mittelwert (nicht der Median) ist richtig, weil die
Planung erwartete Kosten minimiert. Durch die lokale Uhrzeit fallen am 25-Stunden-Tag zwei
Viertelstunden auf denselben Eintrag und am 23-Stunden-Tag fehlt eine Stunde einfach.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

QUARTERS_PER_DAY = 96
MIN_SAMPLES = 3 * QUARTERS_PER_DAY


class ProfileError(Exception):
    """Historie fehlt oder ist zu dünn für ein Lastprofil."""


def _key(local: datetime) -> tuple[bool, int]:
    return local.weekday() >= 5, local.hour * 4 + local.minute // 15


@dataclass(frozen=True)
class LoadProfile:
    buckets: dict[tuple[bool, int], float]  # (Wochenende, Viertelstunde) -> kWh je 15 min
    fallback: dict[bool, float]
    tz: ZoneInfo
    samples: int

    def kwh_per_quarter(self, when: datetime) -> float:
        key = _key(when.astimezone(self.tz))
        if key in self.buckets:
            return self.buckets[key]
        return self.fallback.get(key[0], self.fallback[not key[0]])


def build_profile(history: list[dict[str, Any]], tz: ZoneInfo) -> LoadProfile:
    """Erwartet die Antwort von /api/history/energy?aggregate=15m&group=home."""
    series = next((s for s in history if isinstance(s, dict) and s.get("group") == "home"), None)
    if series is None or not isinstance(series.get("data"), list):
        raise ProfileError("Historie ohne Gruppe 'home'")

    values: dict[tuple[bool, int], list[float]] = defaultdict(list)
    samples = 0
    for item in series["data"]:
        try:
            start = datetime.fromisoformat(item["start"])
            end = datetime.fromisoformat(item["end"])
            energy = float(item["energy"])
        except (KeyError, TypeError, ValueError):
            continue
        seconds = (end - start).total_seconds()
        if seconds <= 0 or energy < 0:
            continue
        values[_key(start.astimezone(tz))].append(energy * 900 / seconds)
        samples += 1

    if samples < MIN_SAMPLES:
        raise ProfileError(f"zu wenig Verbrauchsdaten ({samples} Viertelstunden)")

    buckets = {k: sum(v) / len(v) for k, v in values.items()}
    fallback: dict[bool, float] = {}
    for weekend in (False, True):
        day = [v for (w, _), v in buckets.items() if w == weekend]
        if day:
            fallback[weekend] = sum(day) / len(day)
    return LoadProfile(buckets=buckets, fallback=fallback, tz=tz, samples=samples)
