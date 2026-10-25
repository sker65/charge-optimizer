"""Entscheidung: optimaler Ladeplan -> Preisgrenze für die aktuelle Viertelstunde. Kein I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .models import Decision, PlannedBlock, Snapshot, SnapshotError
from .planner import Battery, Plan, PlanStep, Slot, build_slots, rollout, solve
from .profile import LoadProfile

EPS_KWH = 1e-3


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    plan: Plan | None = None
    baseline: Plan | None = None


def _local(t: datetime, tz: ZoneInfo) -> str:
    return t.astimezone(tz).isoformat(timespec="minutes")


def _n(value: float, digits: int) -> str:
    """Zahl mit deutschem Dezimalkomma."""
    return f"{value:.{digits}f}".replace(".", ",")


def battery_from(snap: Snapshot, cfg: Settings) -> Battery:
    capacity = cfg.battery_capacity_kwh or snap.capacity_kwh or cfg.battery_capacity_fallback_kwh
    return Battery(
        capacity_kwh=capacity,
        min_kwh=capacity * cfg.min_soc / 100,
        target_kwh=capacity * cfg.target_soc / 100,
        charge_power_kw=cfg.charge_power_w / 1000,
        max_power_kw=cfg.battery_max_power_w / 1000,
        roundtrip_efficiency=cfg.roundtrip_efficiency,
        wear_eur_kwh=cfg.wear_cost_eur_kwh,
    )


def error_decision(
    now: datetime, tz: ZoneInfo, reason: str, snap: Snapshot | None = None
) -> Decision:
    return Decision(
        ts=_local(now, tz),
        decision="error",
        reason=reason,
        soc=snap.soc if snap else None,
        current_limit_eur=snap.grid_charge_limit if snap else None,
    )


def _blocks(steps: list[PlanStep], tz: ZoneInfo) -> list[PlannedBlock]:
    blocks: list[PlannedBlock] = []
    run: list[PlanStep] = []
    for s in [*steps, None]:
        if s is not None and s.charge:
            if run and run[-1].slot.end != s.slot.start:
                blocks.append(_block(run, tz))
                run = []
            run.append(s)
        elif run:
            blocks.append(_block(run, tz))
            run = []
    return blocks


def _block(run: list[PlanStep], tz: ZoneInfo) -> PlannedBlock:
    energy = sum(s.result.grid_charge for s in run)
    hours = sum(s.slot.hours for s in run)
    return PlannedBlock(
        start=_local(run[0].slot.start, tz),
        end=_local(run[-1].slot.end, tz),
        energy_kwh=round(energy, 2),
        avg_price=round(sum(s.slot.price * s.slot.hours for s in run) / hours, 4),
    )


def _limit_for(plan: Plan, snap: Snapshot) -> float:
    """Grenze für den laufenden Slot; deckt den nächsten mit ab, wenn auch dort geladen wird,
    damit evcc am Viertelstundenwechsel nicht kurz aus dem Lademodus fällt."""
    prices = [plan.steps[0].slot.price]
    if snap.tariff_grid is not None:
        prices.append(snap.tariff_grid)
    if len(plan.steps) > 1 and plan.steps[1].charge:
        prices.append(plan.steps[1].slot.price)
    # auf 0,1 ct aufrunden (round gegen Gleitkomma-Artefakte wie 0.19940000000000002)
    return math.ceil(round(max(prices) * 1000, 6)) / 1000


def decide(
    snap: Snapshot,
    profile: LoadProfile,
    cfg: Settings,
    now: datetime,
    *,
    previous_charge: bool | None = None,
) -> Outcome:
    """Berechnet den optimalen Ladeplan und daraus die Grenze für jetzt.

    previous_charge: ob der letzte Lauf im Lademodus war (für die Hysterese), None = unbekannt.
    """
    tz = ZoneInfo(cfg.timezone)
    try:
        slots = build_slots(
            snap,
            profile,
            now,
            pv_factor=cfg.pv_factor,
            feedin_fallback=cfg.feedin_fallback_eur_kwh,
            min_horizon=timedelta(hours=cfg.min_horizon_h),
        )
    except SnapshotError as e:
        return Outcome(error_decision(now, tz, f"Daten unvollständig: {e}", snap))

    b = battery_from(snap, cfg)
    soc_now = b.capacity_kwh * snap.soc / 100
    # Restenergie am Horizontende ist so viel wert, wie Nachladen zum günstigsten bekannten
    # Preis kosten würde. Konservativ: kein Laden auf Vorrat für Zeiten ohne Preisprognose.
    terminal_price = cfg.terminal_price_eur_kwh
    if terminal_price is None:
        terminal_price = min(s.price for s in slots)
    kw = {"terminal_price": terminal_price, "min_spread": cfg.min_spread_eur_kwh}

    table = solve(slots, b, soc_now, step_kwh=cfg.soc_step_kwh, **kw)
    plan = rollout(slots, b, soc_now, table, **kw)
    baseline = rollout(slots, b, soc_now, None, **kw)

    # Hysterese nur gegen das Abbrechen: ein laufender Lademodus wird erst beendet, wenn
    # das klar besser ist. Starten soll er sofort, sonst würde er ans Fensterende rutschen.
    hold = False
    if (
        previous_charge
        and not plan.steps[0].charge
        and plan.q_now[True] - plan.q_now[False] < cfg.hysteresis_eur
    ):
        plan = rollout(slots, b, soc_now, table, force_first=True, **kw)
        hold = True

    decision = _summarize(snap, cfg, tz, now, slots, plan, baseline, hold)
    return Outcome(decision=decision, plan=plan, baseline=baseline)


def _summarize(
    snap: Snapshot,
    cfg: Settings,
    tz: ZoneInfo,
    now: datetime,
    slots: list[Slot],
    plan: Plan,
    baseline: Plan,
    hold: bool,
) -> Decision:
    charging = [s for s in plan.steps if s.result.grid_charge > EPS_KWH]
    mode_slots = [s for s in plan.steps if s.charge]
    energy = sum(s.result.grid_charge for s in charging)
    savings = baseline.cost - plan.cost
    avg_charge = (
        sum(s.result.grid_charge * s.slot.price for s in charging) / energy if energy else None
    )

    avoided_kwh = avoided_eur = 0.0
    for p, base in zip(plan.steps, baseline.steps, strict=True):
        diff = base.result.grid_import - (p.result.grid_import - p.result.grid_charge)
        if diff > 0:
            avoided_kwh += diff
            avoided_eur += diff * p.slot.price

    charge_now = plan.steps[0].charge
    holding = charge_now and plan.steps[0].result.grid_charge <= EPS_KWH
    limit = _limit_for(plan, snap) if charge_now else None
    blocks = _blocks(plan.steps, tz)
    pv = sum(s.pv_kwh for s in slots)
    load = sum(s.load_kwh for s in slots)

    plan_text = (
        f"Plan: {_n(energy, 1)} kWh aus dem Netz"
        + (f" zu Ø {_n(avg_charge, 3)} €" if avg_charge is not None else "")
        + (f", ersetzt Netzbezug zu Ø {_n(avoided_eur / avoided_kwh, 3)} €" if avoided_kwh else "")
        + f", Ersparnis {_n(savings, 2)} €"
    )
    if charge_now and limit is not None and limit > cfg.max_limit_eur_kwh:
        kind, limit = "none", None
        cap = _n(cfg.max_limit_eur_kwh, 3)
        reason = f"Plan würde jetzt laden, aber Preis über Sicherheitsdeckel {cap} €"
    elif charge_now:
        kind = "charge"
        reason = f"{'Hält die Batterie am Ziel-SoC' if holding else 'Lädt jetzt'}. {plan_text}"
    else:
        kind = "none"
        if blocks:
            reason = (
                f"Jetzt nicht laden, Lademodus ab {blocks[0].start[11:16]} Uhr geplant. {plan_text}"
            )
        elif snap.soc >= cfg.target_soc:
            reason = "Batterie ist bereits auf Ziel-SoC"
        elif pv >= load:
            reason = f"PV ({_n(pv, 1)} kWh) deckt den Verbrauch ({_n(load, 1)} kWh) im Horizont"
        else:
            reason = "Netzladen lohnt sich im Horizont nicht (Preisabstand zu klein)"
    if hold:
        reason += " (Hysterese: bisheriger Zustand beibehalten)"

    return Decision(
        ts=_local(now, tz),
        decision=kind,
        limit_eur=limit,
        reason=reason,
        energy_needed_kwh=round(energy, 2),
        slots=[_local(s.slot.start, tz) for s in mode_slots],
        blocks=blocks,
        avg_charge_price=round(avg_charge, 4) if avg_charge is not None else None,
        avoided_price=round(avoided_eur / avoided_kwh, 4) if avoided_kwh > EPS_KWH else None,
        spread_after_losses=round(savings / energy, 4) if energy > EPS_KWH else None,
        expected_savings_eur=round(savings, 2),
        pv_forecast_kwh=round(pv, 1),
        consumption_forecast_kwh=round(load, 1),
        soc=snap.soc,
        horizon_end=_local(slots[-1].end, tz),
        current_limit_eur=snap.grid_charge_limit,
        hysteresis_hold=hold,
    )
