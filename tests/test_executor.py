"""Umsetzung in evcc: Fremdgrenzen, Idempotenz, Dry-Run, Fehlerfall."""

import asyncio
from datetime import UTC, datetime

import pytest

from batterieoptimizer.evcc import EvccError
from batterieoptimizer.executor import apply, choose_action
from batterieoptimizer.models import Decision
from batterieoptimizer.state import ServiceState, StateStore

NOW = datetime(2026, 10, 25, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("desired", "current", "last", "expected"),
    [
        (0.2, None, None, "set"),  # frei
        (0.2, 0.2, 0.2, "unchanged"),
        (0.21, 0.2, 0.2, "set"),  # eigene Grenze anpassen
        (None, 0.2, 0.2, "remove"),  # eigene Grenze entfernen
        (None, None, None, "unchanged"),
        (0.2, 0.22, None, "foreign"),  # von Hand gesetzt
        (None, 0.22, 0.2, "foreign"),  # von Hand geändert
        (0.2, None, 0.2, "set"),  # von Hand entfernt -> wieder frei
    ],
)
def test_choose_action(desired, current, last, expected):
    assert choose_action(desired, current, last, respect_foreign=True) == expected


def test_foreign_limit_can_be_overridden_by_config():
    assert choose_action(0.2, 0.22, None, respect_foreign=False) == "set"


class FakeClient:
    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail

    async def set_limit(self, cost):
        if self.fail:
            raise EvccError("boom")
        self.calls.append(("set", cost))

    async def remove_limit(self):
        if self.fail:
            raise EvccError("boom")
        self.calls.append(("remove",))


def decision(kind="charge", limit=0.2) -> Decision:
    return Decision(ts="x", decision=kind, limit_eur=limit, reason="test")


def _apply(d, current, state, client, dry_run=False):
    return asyncio.run(
        apply(d, current, state, client, dry_run=dry_run, respect_foreign=True, now=NOW)
    )


def test_writes_and_remembers():
    client = FakeClient()
    d, st = _apply(decision(), None, ServiceState(), client)
    assert client.calls == [("set", 0.2)]
    assert d.applied == "written"
    assert st.last_written == 0.2
    assert st.last_charge is True


def test_dry_run_never_writes():
    client = FakeClient()
    d, st = _apply(decision(), None, ServiceState(), client, dry_run=True)
    assert client.calls == []
    assert d.applied == "dry_run"
    assert st.last_written is None


def test_error_removes_own_limit():
    client = FakeClient()
    _, st = _apply(decision("error", None), 0.2, ServiceState(last_written=0.2), client)
    assert client.calls == [("remove",)]
    assert st.last_written is None
    assert st.last_charge is None


def test_error_leaves_foreign_limit():
    client = FakeClient()
    d, _ = _apply(decision("error", None), 0.22, ServiceState(last_written=0.2), client)
    assert client.calls == []
    assert d.applied == "foreign_limit"


def test_write_failure_keeps_state():
    d, st = _apply(decision(), None, ServiceState(), FakeClient(fail=True))
    assert d.applied == "failed"
    assert st.last_written is None


def test_state_store_roundtrip(tmp_path):
    store = StateStore(str(tmp_path))
    assert store.load() == ServiceState()
    store.save(ServiceState(last_written=0.215, written_at="t", last_charge=True))
    assert store.load().last_written == 0.215
    (tmp_path / "state.json").write_text("{kaputt")
    assert store.load() == ServiceState()
