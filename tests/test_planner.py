"""DP-Kern: Energiebilanz je Slot und Optimalität gegen Ausprobieren aller Pläne."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from batterieoptimizer.planner import Battery, Slot, rollout, solve, step

B = Battery(
    capacity_kwh=12.5,
    min_kwh=2.5,
    target_kwh=10.0,
    charge_power_kw=3.0,
    max_power_kw=6.0,
    roundtrip_efficiency=0.85,
    wear_eur_kwh=0.02,
)
T0 = datetime(2026, 11, 10, tzinfo=UTC)


def slot(i: int, price: float, pv: float = 0.0, load: float = 0.15) -> Slot:
    start = T0 + timedelta(minutes=15 * i)
    return Slot(start, start + timedelta(minutes=15), price, 0.077, pv, load)


@pytest.mark.parametrize("charge", [False, True])
@pytest.mark.parametrize("pv", [0.0, 0.1, 0.5, 2.0])
@pytest.mark.parametrize("soc", [2.5, 6.0, 10.0, 12.5])
def test_step_energy_balance(charge, pv, soc):
    s = slot(0, 0.3, pv=pv)
    r = step(B, s, soc, charge)
    battery_in = max(0.0, r.soc_kwh - soc) / B.eta
    battery_out = max(0.0, soc - r.soc_kwh) * B.eta
    # PV + Netz + Batterieabgabe = Last + Einspeisung + Batterieaufnahme
    assert pv + r.grid_import + battery_out == pytest.approx(
        s.load_kwh + r.grid_export + battery_in
    )
    assert B.min_kwh - 1e-9 <= r.soc_kwh <= B.capacity_kwh + 1e-9
    if charge and soc >= B.target_kwh:
        assert r.soc_kwh >= B.target_kwh - 1e-9  # Lademodus entlädt nicht unter Ziel-SoC


def test_charge_mode_stops_at_target():
    r = step(B, slot(0, 0.2), 9.9, True)
    assert r.soc_kwh == pytest.approx(B.target_kwh)


def test_dp_matches_brute_force():
    prices = [0.20, 0.20, 0.45, 0.45, 0.25, 0.45, 0.45, 0.45]
    slots = [slot(i, p, load=0.8) for i, p in enumerate(prices)]
    soc0 = 3.0
    kw = {"terminal_price": 0.0, "min_spread": 0.0}
    table = solve(slots, B, soc0, step_kwh=0.01, **kw)
    plan = rollout(slots, B, soc0, table, **kw)

    best = float("inf")
    for actions in itertools.product([False, True], repeat=len(slots)):
        soc, cost = soc0, 0.0
        for s, a in zip(slots, actions, strict=True):
            r = step(B, s, soc, a)
            cost += r.cost
            soc = r.soc_kwh
        best = min(best, cost)
    assert plan.cost == pytest.approx(best, abs=0.01)
    assert plan.steps[0].charge and plan.steps[1].charge


def test_baseline_never_charges():
    slots = [slot(i, 0.1) for i in range(8)]
    plan = rollout(slots, B, 3.0, None, terminal_price=0.3, min_spread=0.0)
    assert not any(s.charge for s in plan.steps)
    assert plan.grid_charge_kwh == 0
