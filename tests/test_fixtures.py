"""Aufgezeichnete Snapshots aus dem echten evcc."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from batterieoptimizer.decide import decide
from batterieoptimizer.models import parse_state
from batterieoptimizer.profile import build_profile

from .helpers import cfg

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Berlin")


def load(name: str):
    d = FIXTURES / name
    snap = parse_state(json.loads((d / "state.json").read_text()))
    profile = build_profile(json.loads((d / "history.json").read_text()), TZ)
    now = datetime.fromisoformat(json.loads((d / "meta.json").read_text())["now"])
    return snap, profile, now


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()))
def test_fixture_runs(fixture):
    snap, profile, now = load(fixture)
    out = decide(snap, profile, cfg(), now)
    assert out.decision.decision in ("charge", "none")
    assert out.plan.cost <= out.baseline.cost + 1e-9  # Plan nie schlechter als nichts tun


def test_sunday_overcast():
    """25.10.2026: PV-Prognose 3 kW, real 0,6 kW. Mit pessimistischem Faktor wird jetzt zu
    0,199 € geladen, mit Standardfaktor reicht die Prognose-PV."""
    snap, profile, now = load("2026-10-25-sonntag")
    assert snap.grid_charge_limit == pytest.approx(0.22)
    pessimistic = decide(snap, profile, cfg(pv_factor=0.3), now).decision
    assert pessimistic.decision == "charge"
    assert pessimistic.limit_eur == pytest.approx(0.2)
    default = decide(snap, profile, cfg(), now).decision
    assert default.decision == "none"
