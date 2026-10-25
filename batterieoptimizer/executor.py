"""Entscheidung in evcc umsetzen: Fremdgrenzen respektieren, nur bei Änderung schreiben."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Literal

from .evcc import EvccClient, EvccError
from .models import Decision
from .state import ServiceState

log = logging.getLogger(__name__)

Action = Literal["set", "remove", "unchanged", "foreign"]


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, abs_tol=5e-5)


def choose_action(
    desired: float | None,
    current: float | None,
    last_written: float | None,
    *,
    respect_foreign: bool,
) -> Action:
    """Was mit der Grenze in evcc zu tun ist.

    Eine Grenze gehört dem Dienst, wenn sie dem zuletzt geschriebenen Wert entspricht.
    Jede andere gesetzte Grenze ist fremd (von Hand gesetzt, oder Zustand verloren).
    """
    own = current is not None and _same(current, last_written)
    if current is not None and not own and respect_foreign:
        return "foreign"
    if _same(desired, current):
        return "unchanged"
    return "remove" if desired is None else "set"


async def apply(
    decision: Decision,
    current: float | None,
    state: ServiceState,
    client: EvccClient,
    *,
    dry_run: bool,
    respect_foreign: bool,
    now: datetime,
) -> tuple[Decision, ServiceState]:
    """Setzt bzw. entfernt die Grenze. Bei error ist die gewünschte Grenze immer None."""
    desired = decision.limit_eur if decision.decision == "charge" else None
    action = choose_action(desired, current, state.last_written, respect_foreign=respect_foreign)
    new_state = ServiceState(
        last_written=state.last_written,
        written_at=state.written_at,
        last_charge=None if decision.decision == "error" else decision.decision == "charge",
    )

    if action == "foreign":
        applied = "foreign_limit"
    elif action == "unchanged":
        applied = "unchanged"
    elif dry_run:
        applied = "dry_run"
        log.info("DRY_RUN: würde Grenze %s", f"auf {desired} setzen" if desired else "entfernen")
    else:
        try:
            if action == "set":
                assert desired is not None
                await client.set_limit(desired)
            else:
                await client.remove_limit()
        except EvccError as e:
            log.error("Schreiben in evcc fehlgeschlagen: %s", e)
            return decision.model_copy(
                update={"applied": "failed", "reason": f"{decision.reason} (evcc-Fehler: {e})"}
            ), new_state
        applied = "written"
        new_state.last_written = desired
        new_state.written_at = now.isoformat()

    return decision.model_copy(update={"applied": applied}), new_state
