"""Optimaler Ladeplan per Dynamischer Programmierung (DP). Reine Funktionen, kein I/O.

Ablauf:
1. ``build_slots`` legt ab jetzt ein 15-min-Raster bis zum Ende der Preis- bzw.
   PV-Prognose an (die erste Viertelstunde ist angebrochen) und füllt es mit Preis,
   Einspeisevergütung, PV- und Verbrauchsprognose.
2. ``solve`` rechnet rückwärts für jede Viertelstunde und jeden SoC die minimalen
   Restkosten bis zum Horizontende aus (Wertetabelle).
3. ``rollout`` läuft vom echten SoC aus vorwärts und wählt in jedem Slot die Aktion
   mit den geringsten Kosten laut Tabelle. Das ergibt den Ladeplan.

Pro Slot gibt es zwei Aktionen, so wie sie evcc und der Wechselrichter ausführen:
- normal: PV deckt erst den Verbrauch, Überschuss lädt die Batterie, ein Defizit
  entlädt sie bis MIN_SOC, der Rest kommt aus dem Netz.
- laden (evcc-Modus charge): die Batterie lädt mit CHARGE_POWER bis TARGET_SOC und wird
  nicht entladen, der Hausverbrauch kommt aus PV und Netz. Über TARGET_SOC wirkt der Modus
  wie normal, entlädt aber nur bis TARGET_SOC (Lade-SoC des Wechselrichters als
  Untergrenze). Am Ziel-SoC hält der Modus also die Batterie.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import PriceSlot, Snapshot, SnapshotError, SolarPoint
from .profile import LoadProfile

QUARTER = timedelta(minutes=15)
# Winziger Aufschlag (€/kWh, steigt über den Horizont): bei gleichen Kosten früher laden
EARLY_BIAS_EUR_KWH = 0.001


@dataclass(frozen=True)
class Slot:
    start: datetime  # UTC
    end: datetime
    price: float
    feedin: float
    pv_kwh: float
    load_kwh: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class Battery:
    capacity_kwh: float
    min_kwh: float
    target_kwh: float
    charge_power_kw: float
    max_power_kw: float
    roundtrip_efficiency: float
    wear_eur_kwh: float

    @property
    def eta(self) -> float:
        """Wirkungsgrad je Richtung (Laden bzw. Entladen)."""
        return math.sqrt(self.roundtrip_efficiency)


@dataclass(frozen=True)
class StepResult:
    soc_kwh: float  # nach dem Slot
    grid_import: float  # kWh
    grid_export: float
    grid_charge: float  # kWh, die aus dem Netz in die Batterie gehen
    discharge: float  # kWh, die die Batterie ans Haus abgibt
    cost: float  # €


def step(b: Battery, slot: Slot, soc: float, charge: bool) -> StepResult:
    h = slot.hours
    net = slot.pv_kwh - slot.load_kwh
    floor = b.min_kwh
    if charge:
        floor = max(floor, b.target_kwh)
        want = max(0.0, min(b.charge_power_kw * h, (b.target_kwh - soc) / b.eta))
        if want > 0 and net < want:
            grid_import = slot.load_kwh + want - slot.pv_kwh
            grid_charge = grid_import - max(0.0, -net)
            return StepResult(
                soc_kwh=soc + want * b.eta,
                grid_import=grid_import,
                grid_export=0.0,
                grid_charge=grid_charge,
                discharge=0.0,
                cost=grid_import * slot.price,
            )
        # PV-Überschuss reicht für die Ladeleistung: verhält sich wie normal

    if net >= 0:
        pv_in = min(net, max(0.0, b.capacity_kwh - soc) / b.eta, b.max_power_kw * h)
        export = net - pv_in
        return StepResult(
            soc_kwh=soc + pv_in * b.eta,
            grid_import=0.0,
            grid_export=export,
            grid_charge=0.0,
            discharge=0.0,
            cost=-export * slot.feedin,
        )
    deficit = -net
    discharge = min(deficit, max(0.0, soc - floor) * b.eta, b.max_power_kw * h)
    grid_import = deficit - discharge
    return StepResult(
        soc_kwh=soc - discharge / b.eta,
        grid_import=grid_import,
        grid_export=0.0,
        grid_charge=0.0,
        discharge=discharge,
        cost=grid_import * slot.price + discharge * b.wear_eur_kwh,
    )


# --- Horizont aufbauen -------------------------------------------------------------


def _floor_quarter(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def _contiguous(slots: Sequence[PriceSlot], now: datetime) -> list[PriceSlot]:
    """Preisslots ab jetzt, bis zur ersten Lücke."""
    result: list[PriceSlot] = []
    for s in slots:
        if s.end <= now:
            continue
        if result and s.start != result[-1].end:
            break
        if not result and s.start > now:
            break
        result.append(s)
    return result


def _price_at(slots: Sequence[PriceSlot], t: datetime) -> float | None:
    for s in slots:
        if s.start <= t < s.end:
            return s.price
    return None


def pv_power_at(points: Sequence[SolarPoint], t: datetime) -> float:
    """PV-Leistung (W) linear interpoliert; nach dem Ende der Prognose 0."""
    if t <= points[0].ts:
        return points[0].power_w
    for a, b in itertools.pairwise(points):
        if a.ts <= t <= b.ts:
            frac = (t - a.ts) / (b.ts - a.ts)
            return a.power_w + frac * (b.power_w - a.power_w)
    return 0.0


def build_slots(
    snap: Snapshot,
    profile: LoadProfile,
    now: datetime,
    *,
    pv_factor: float,
    feedin_fallback: float,
    min_horizon: timedelta,
) -> list[Slot]:
    now = now.astimezone(UTC)  # Rasterarithmetik nur in UTC, sonst verrutscht sie bei DST
    grid = _contiguous(snap.grid, now)
    if not grid:
        raise SnapshotError("keine Preisprognose für den aktuellen Zeitpunkt")
    if snap.solar[0].ts > now + QUARTER:
        raise SnapshotError("PV-Prognose beginnt erst in der Zukunft")
    end = min(grid[-1].end, snap.solar[-1].ts + QUARTER)
    if end - now < min_horizon:
        raise SnapshotError(f"Prognosehorizont zu kurz (bis {end.isoformat()})")

    feedin_default = snap.tariff_feedin if snap.tariff_feedin is not None else feedin_fallback
    slots: list[Slot] = []
    t = _floor_quarter(now)
    while t < end:
        start, stop = max(t, now), min(t + QUARTER, end)
        if stop > start:
            hours = (stop - start).total_seconds() / 3600
            mid = start + (stop - start) / 2
            price = _price_at(grid, start)
            assert price is not None  # durch _contiguous garantiert
            feedin = _price_at(snap.feedin, start)
            slots.append(
                Slot(
                    start=start,
                    end=stop,
                    price=price,
                    feedin=feedin if feedin is not None else feedin_default,
                    pv_kwh=pv_power_at(snap.solar, mid) / 1000 * hours * pv_factor,
                    load_kwh=profile.kwh_per_quarter(t) * hours / 0.25,
                )
            )
        t += QUARTER
    return slots


# --- DP ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueTable:
    lo: float
    step_kwh: float
    values: list[list[float]]  # values[t][i]: minimale Restkosten ab Slot t bei SoC lo+i*step

    def at(self, t: int, soc: float) -> float:
        row = self.values[t]
        x = (soc - self.lo) / self.step_kwh
        if x <= 0:
            return row[0]
        i = int(x)
        if i >= len(row) - 1:
            return row[-1]
        frac = x - i
        return row[i] + frac * (row[i + 1] - row[i])


@dataclass(frozen=True)
class PlanStep:
    slot: Slot
    charge: bool
    soc_before: float
    result: StepResult


@dataclass(frozen=True)
class Plan:
    steps: list[PlanStep]
    cost: float  # echte Kosten inkl. Restwert am Horizontende, ohne Mindestspread-Aufschlag
    q_now: dict[bool, float]  # Kosten (mit Aufschlag) der beiden Aktionen im ersten Slot

    @property
    def grid_charge_kwh(self) -> float:
        return sum(s.result.grid_charge for s in self.steps)


def terminal_value(b: Battery, soc: float, terminal_price: float) -> float:
    """Restkosten am Horizontende: nutzbare Energie mindert künftigen Netzbezug, ihre
    Entnahme kostet aber wie jede Entladung Verschleiß."""
    return -max(0.0, soc - b.min_kwh) * b.eta * (terminal_price - b.wear_eur_kwh)


def _q(
    b: Battery, slot: Slot, soc: float, charge: bool, next_value, penalty: float
) -> tuple[float, StepResult]:
    r = step(b, slot, soc, charge)
    # Aufschlag je kWh Netzladung (Mindestspread): geladen wird nur, wenn es mehr bringt
    return r.cost + penalty * r.grid_charge + next_value(r.soc_kwh), r


def _penalty(min_spread: float, t: int, n: int) -> float:
    return min_spread + EARLY_BIAS_EUR_KWH * t / n


def solve(
    slots: Sequence[Slot],
    b: Battery,
    soc_now: float,
    *,
    terminal_price: float,
    min_spread: float,
    step_kwh: float,
) -> ValueTable:
    lo = min(b.min_kwh, soc_now)
    n = max(2, math.ceil((b.capacity_kwh - lo) / step_kwh) + 1)
    grid = [min(lo + i * step_kwh, b.capacity_kwh) for i in range(n)]
    values: list[list[float]] = [[] for _ in range(len(slots) + 1)]
    values[-1] = [terminal_value(b, e, terminal_price) for e in grid]
    table = ValueTable(lo=lo, step_kwh=step_kwh, values=values)

    for t in range(len(slots) - 1, -1, -1):
        slot = slots[t]
        penalty = _penalty(min_spread, t, len(slots))

        def nxt(e: float, t: int = t) -> float:
            return table.at(t + 1, e)

        row = []
        for e in grid:
            row.append(
                min(
                    _q(b, slot, e, False, nxt, penalty)[0],
                    _q(b, slot, e, True, nxt, penalty)[0],
                )
            )
        values[t] = row
    return table


def rollout(
    slots: Sequence[Slot],
    b: Battery,
    soc_now: float,
    table: ValueTable | None,
    *,
    terminal_price: float,
    min_spread: float,
    force_first: bool | None = None,
) -> Plan:
    """Vorwärtslauf. Ohne Tabelle wird nie geladen (Vergleichsfall)."""
    steps: list[PlanStep] = []
    q_now: dict[bool, float] = {}
    soc = soc_now
    cost = 0.0
    for t, slot in enumerate(slots):
        options = [False] if table is None else [False, True]
        scored = []
        for charge in options:
            if table is None:
                q, r = 0.0, step(b, slot, soc, charge)
            else:
                q, r = _q(
                    b,
                    slot,
                    soc,
                    charge,
                    lambda e, t=t: table.at(t + 1, e),
                    _penalty(min_spread, t, len(slots)),
                )
            scored.append((q, charge, r))
        if t == 0:
            q_now = {charge: q for q, charge, _ in scored}
        # Bei Gleichstand nicht laden
        q, charge, r = min(scored, key=lambda x: x[0] + (1e-9 if x[1] else 0.0))
        if t == 0 and force_first is not None:
            forced = [x for x in scored if x[1] == force_first]
            if forced:
                q, charge, r = forced[0]
        steps.append(PlanStep(slot=slot, charge=charge, soc_before=soc, result=r))
        cost += r.cost
        soc = r.soc_kwh
    cost += terminal_value(b, soc, terminal_price)
    return Plan(steps=steps, cost=cost, q_now=q_now)
