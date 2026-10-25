"""Entscheidungen in typischen Situationen (Abschnitt 7 der Anforderungen)."""

from datetime import datetime

import pytest

from batterieoptimizer.decide import decide
from batterieoptimizer.models import SnapshotError, parse_state

from .helpers import TZ, cfg, flat_profile, make_state, sunny_pv

TUESDAY_14 = datetime(2026, 11, 10, 14, 0, tzinfo=TZ)
WEDNESDAY_02 = datetime(2026, 11, 11, 2, 0, tzinfo=TZ)


def run(state: dict, now: datetime, load: float = 0.15, **overrides):
    return decide(parse_state(state), flat_profile(load), cfg(**overrides), now)


def test_cloudy_with_spread_plans_night_charging():
    out = run(make_state(TUESDAY_14, 34), TUESDAY_14)
    d = out.decision
    assert d.decision == "none"  # 14:00 kostet 0,33, geladen wird nachts
    assert d.energy_needed_kwh > 3
    assert d.blocks
    for block in d.blocks:
        start = datetime.fromisoformat(block.start)
        assert start.hour >= 22 or start.hour < 6, block
    assert d.expected_savings_eur > 0
    assert d.spread_after_losses >= cfg().min_spread_eur_kwh


def test_cloudy_with_spread_charges_now_at_night():
    out = run(make_state(WEDNESDAY_02, 30, soc=22), WEDNESDAY_02)
    d = out.decision
    assert d.decision == "charge"
    assert d.limit_eur == pytest.approx(0.20)
    assert d.applied == "skipped"  # decide() schreibt nie


def test_sunny_day_no_grid_charging():
    # 9,6 kWh/Tag: die von PV gefüllte Batterie reicht bis zur nächsten Sonne
    out = run(make_state(TUESDAY_14, 34, pv=sunny_pv, soc=60), TUESDAY_14, load=0.1)
    assert out.decision.energy_needed_kwh == 0
    assert out.decision.limit_eur is None


def test_sunny_day_charges_only_what_pv_cannot_deliver_in_time():
    # 14,4 kWh/Tag: die volle Batterie reicht nicht bis zur Morgenspitze. Nachladen vor
    # 6 Uhr lohnt sich, aber nur so viel, dass PV ab 9 Uhr nicht verdrängt wird.
    out = run(make_state(TUESDAY_14, 34, pv=sunny_pv, soc=60), TUESDAY_14)
    d = out.decision
    assert 0 < d.energy_needed_kwh < 4
    morning = next(s for s in out.plan.steps if s.slot.start.astimezone(TZ).hour == 9)
    assert morning.soc_before < 0.25 * 12.5  # Batterie ist leer, wenn die Sonne kommt


def test_flat_prices_no_grid_charging():
    out = run(make_state(TUESDAY_14, 34, price=lambda _: 0.30), TUESDAY_14)
    assert out.decision.decision == "none"
    assert out.decision.energy_needed_kwh == 0
    assert "lohnt sich" in out.decision.reason


def test_small_spread_below_losses_no_charging():
    # 0,30 vs 0,33: nach 85 % Wirkungsgrad und Verschleiß kein Gewinn
    def price(local):
        return 0.33 if 17 <= local.hour < 21 else 0.30

    out = run(make_state(TUESDAY_14, 34, price=price), TUESDAY_14)
    assert out.decision.energy_needed_kwh == 0


def test_full_battery_no_charging():
    out = run(make_state(TUESDAY_14, 34, pv=sunny_pv, soc=100), TUESDAY_14, load=0.1)
    assert out.decision.decision == "none"
    assert out.decision.energy_needed_kwh == 0


def test_missing_fields_raise():
    state = make_state(TUESDAY_14, 34)
    del state["battery"]["soc"]
    with pytest.raises(SnapshotError):
        parse_state(state)
    state = make_state(TUESDAY_14, 34)
    state["forecast"]["grid"] = []
    with pytest.raises(SnapshotError):
        parse_state(state)
    state = make_state(TUESDAY_14, 34)
    del state["forecast"]["solar"]
    with pytest.raises(SnapshotError):
        parse_state(state)


def test_short_horizon_is_error():
    out = run(make_state(TUESDAY_14, 2), TUESDAY_14)
    assert out.decision.decision == "error"
    assert out.decision.limit_eur is None


def test_stale_prices_are_error():
    # Prognose endet vor "jetzt"
    out = run(make_state(TUESDAY_14, 30), datetime(2026, 11, 12, 12, 0, tzinfo=TZ))
    assert out.decision.decision == "error"


def test_hysteresis_only_against_stopping():
    snap = parse_state(make_state(TUESDAY_14, 34, soc=40))
    # 14:00 kostet 0,33: nicht laden, auch wenn vorher geladen wurde
    stop = decide(snap, flat_profile(), cfg(hysteresis_eur=0), TUESDAY_14, previous_charge=True)
    assert stop.decision.decision == "none"
    assert not stop.decision.hysteresis_hold
    held = decide(snap, flat_profile(), cfg(hysteresis_eur=100), TUESDAY_14, previous_charge=True)
    assert held.decision.decision == "charge"
    assert held.decision.hysteresis_hold


def test_charging_starts_at_window_begin():
    # Nachts durchgehend 0,20: bei gleichen Kosten sofort laden, nicht ans Fensterende schieben
    snap = parse_state(make_state(WEDNESDAY_02, 30, soc=22))
    out = decide(snap, flat_profile(), cfg(), WEDNESDAY_02, previous_charge=False)
    assert out.decision.decision == "charge"
    assert out.decision.blocks[0].start.startswith("2026-11-11T02:00")


def test_limit_covers_next_slot_and_rounds_up():
    def price(local):
        if local.hour == 2 and local.minute == 0:
            return 0.19940000000000002
        if local.hour == 2 and local.minute == 15:
            return 0.2011
        return 0.20 if local.hour < 6 else 0.40

    out = run(make_state(WEDNESDAY_02, 30, price=price, soc=22), WEDNESDAY_02)
    assert out.decision.decision == "charge"
    assert out.decision.limit_eur == pytest.approx(0.202)


def test_mixed_quarter_and_hourly_slots():
    state = make_state(TUESDAY_14, 34, hourly_after=10)
    out = run(state, TUESDAY_14)
    assert out.decision.decision != "error"
    assert len(out.plan.steps) == 34 * 4
    assert all(s.slot.hours == pytest.approx(0.25) for s in out.plan.steps)


def test_partial_first_slot():
    now = datetime(2026, 11, 10, 14, 5, tzinfo=TZ)
    out = run(make_state(TUESDAY_14, 34), now)
    first = out.plan.steps[0].slot
    assert first.hours == pytest.approx(10 / 60)
    assert first.start == now
