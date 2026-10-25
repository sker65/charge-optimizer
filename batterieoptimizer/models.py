"""Datenmodelle: Snapshot aus dem evcc-State und die veröffentlichte Entscheidung.

Die Feldnamen des evcc-UI-States haben keine Kompatibilitätszusage. Alles, was die
Planung braucht, wird hier geprüft; fehlt etwas, gibt es einen SnapshotError und der
Dienst geht in den sicheren Zustand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class SnapshotError(Exception):
    """evcc-State unvollständig oder unplausibel."""


class PriceSlot(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime  # UTC
    end: datetime
    price: float  # €/kWh


class SolarPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime  # UTC
    power_w: float


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    grid: tuple[PriceSlot, ...]
    feedin: tuple[PriceSlot, ...]
    solar: tuple[SolarPoint, ...]
    soc: float
    capacity_kwh: float | None
    grid_charge_limit: float | None
    battery_mode: str | None
    tariff_grid: float | None
    tariff_feedin: float | None
    pv_power_w: float | None


def _utc(ts: Any) -> datetime:
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        raise SnapshotError(f"Zeitstempel ungültig: {ts!r}")
    return datetime.fromtimestamp(ts, tz=UTC)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _price_slots(raw: Any, name: str) -> tuple[PriceSlot, ...]:
    if not isinstance(raw, list):
        raise SnapshotError(f"forecast.{name} fehlt")
    slots = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            raise SnapshotError(f"forecast.{name}: Eintrag ungültig: {item!r}")
        start, end, price = _utc(item[0]), _utc(item[1]), _num(item[2])
        if price is None or not -1.0 <= price <= 2.0 or end <= start:
            raise SnapshotError(f"forecast.{name}: Eintrag unplausibel: {item!r}")
        slots.append(PriceSlot(start=start, end=end, price=price))
    slots.sort(key=lambda s: s.start)
    return tuple(slots)


def parse_state(state: dict[str, Any]) -> Snapshot:
    """Wandelt den Inhalt von /api/state in einen geprüften Snapshot um."""
    forecast = state.get("forecast")
    if not isinstance(forecast, dict):
        raise SnapshotError("forecast fehlt")

    grid = _price_slots(forecast.get("grid"), "grid")
    if not grid:
        raise SnapshotError("keine Preisprognose")
    try:
        feedin = _price_slots(forecast.get("feedin"), "feedin")
    except SnapshotError:
        feedin = ()  # Einspeisevergütung ist verzichtbar, dann gilt tariffFeedIn bzw. Fallback

    solar_raw = (forecast.get("solar") or {}).get("timeseries")
    if not isinstance(solar_raw, list) or not solar_raw:
        raise SnapshotError("forecast.solar.timeseries fehlt")
    solar = []
    for item in solar_raw:
        if not isinstance(item, list) or len(item) != 2 or _num(item[1]) is None:
            raise SnapshotError(f"forecast.solar.timeseries: Eintrag ungültig: {item!r}")
        solar.append(SolarPoint(ts=_utc(item[0]), power_w=max(0.0, float(item[1]))))
    solar.sort(key=lambda p: p.ts)

    battery = state.get("battery")
    soc = _num(battery.get("soc")) if isinstance(battery, dict) else None
    if soc is None or not 0 <= soc <= 100:
        raise SnapshotError(f"battery.soc unbekannt oder unplausibel: {soc!r}")
    capacity = _num(battery.get("capacity"))
    if capacity is not None and capacity <= 0:
        capacity = None

    limit_raw = state.get("batteryGridChargeLimit")
    limit = _num(limit_raw)
    if limit_raw is not None and limit is None:
        raise SnapshotError(f"batteryGridChargeLimit unplausibel: {limit_raw!r}")

    mode = state.get("batteryMode")
    return Snapshot(
        grid=grid,
        feedin=feedin,
        solar=tuple(solar),
        soc=soc,
        capacity_kwh=capacity,
        grid_charge_limit=limit,
        battery_mode=mode if isinstance(mode, str) else None,
        tariff_grid=_num(state.get("tariffGrid")),
        tariff_feedin=_num(state.get("tariffFeedIn")),
        pv_power_w=_num(state.get("pvPower")),
    )


DecisionKind = Literal["charge", "none", "error"]
Applied = Literal["dry_run", "written", "unchanged", "foreign_limit", "failed", "skipped"]


class PlannedBlock(BaseModel):
    start: str  # lokale Zeit, ISO
    end: str
    energy_kwh: float
    avg_price: float


class Decision(BaseModel):
    """Wird als retained JSON per MQTT veröffentlicht."""

    ts: str
    decision: DecisionKind
    limit_eur: float | None = None
    applied: Applied = "skipped"
    reason: str
    energy_needed_kwh: float | None = None  # geplante Netzladung im Horizont
    slots: list[str] = []  # Startzeiten der geplanten Lade-Viertelstunden
    blocks: list[PlannedBlock] = []
    avg_charge_price: float | None = None
    avoided_price: float | None = None
    spread_after_losses: float | None = None  # Ersparnis je geladener kWh
    expected_savings_eur: float | None = None
    pv_forecast_kwh: float | None = None
    consumption_forecast_kwh: float | None = None
    soc: float | None = None
    horizon_end: str | None = None
    current_limit_eur: float | None = None
    hysteresis_hold: bool = False
