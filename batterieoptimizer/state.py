"""Eigener Zustand des Dienstes (JSON im Volume), damit er weiß, welche Grenze von ihm stammt."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ServiceState:
    last_written: float | None = None  # zuletzt vom Dienst in evcc gesetzte Grenze
    written_at: str | None = None
    last_charge: bool | None = None  # Entscheidung des letzten Laufs (Hysterese)


class StateStore:
    def __init__(self, directory: str):
        self.path = Path(directory) / "state.json"

    def load(self) -> ServiceState:
        try:
            data = json.loads(self.path.read_text())
            return ServiceState(**data)
        except FileNotFoundError:
            return ServiceState()
        except (ValueError, TypeError) as e:
            log.warning("Zustandsdatei %s unlesbar (%s), starte ohne Zustand", self.path, e)
            return ServiceState()

    def save(self, state: ServiceState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state)))
        os.replace(tmp, self.path)
