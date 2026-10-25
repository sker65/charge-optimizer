"""Dienst: alle 15 min (kurz nach dem Viertelstundenwechsel) planen, umsetzen, veröffentlichen."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .decide import decide, error_decision
from .evcc import EvccClient, EvccError
from .executor import apply
from .models import Decision, Snapshot, SnapshotError, parse_state
from .mqtt import MqttPublisher
from .planner import pv_power_at
from .profile import LoadProfile, ProfileError, build_profile
from .state import StateStore

log = logging.getLogger(__name__)

PROFILE_MAX_AGE = timedelta(hours=1)


def next_run(now: datetime, offset_s: int) -> datetime:
    """Nächster Viertelstundenwechsel plus Versatz."""
    base = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    candidate = base + timedelta(seconds=offset_s)
    while candidate <= now:
        candidate += timedelta(minutes=15)
    return candidate


class Service:
    def __init__(self, cfg: Settings, *, publisher: MqttPublisher | None = None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.client = EvccClient(
            cfg.evcc_url, password=cfg.evcc_password, timeout=cfg.evcc_timeout_s
        )
        self.store = StateStore(cfg.state_dir)
        self.publisher = publisher
        self._profile: LoadProfile | None = None
        self._profile_at: datetime | None = None

    async def _load_profile(self, now: datetime) -> LoadProfile:
        if self._profile and self._profile_at and now - self._profile_at < PROFILE_MAX_AGE:
            return self._profile
        history = await self.client.home_history(now - timedelta(days=self.cfg.history_days), now)
        self._profile = build_profile(history, self.tz)
        self._profile_at = now
        return self._profile

    async def run_once(self, now: datetime | None = None) -> Decision:
        now = now or datetime.now(UTC)
        state = self.store.load()
        snap: Snapshot | None = None
        current: float | None = None
        try:
            raw = await self.client.state()
            limit_raw = raw.get("batteryGridChargeLimit")
            current = float(limit_raw) if isinstance(limit_raw, int | float) else None
            snap = parse_state(raw)
            profile = await self._load_profile(now)
            decision = decide(
                snap, profile, self.cfg, now, previous_charge=state.last_charge
            ).decision
        except EvccError as e:
            decision = error_decision(now, self.tz, f"evcc nicht erreichbar: {e}")
            decision = decision.model_copy(update={"applied": "failed"})
            self._finish(decision, snap)
            return decision
        except (SnapshotError, ProfileError) as e:
            decision = error_decision(now, self.tz, f"Daten unvollständig: {e}", snap)

        decision, new_state = await apply(
            decision,
            current,
            state,
            self.client,
            dry_run=self.cfg.dry_run,
            respect_foreign=self.cfg.respect_foreign_limit,
            now=now,
        )
        self.store.save(new_state)
        self._finish(decision, snap)
        return decision

    def _finish(self, decision: Decision, snap: Snapshot | None) -> None:
        log.info(
            "%s limit=%s applied=%s | %s",
            decision.decision,
            decision.limit_eur,
            decision.applied,
            decision.reason,
        )
        if self.publisher:
            self.publisher.publish(decision)
        if snap is not None:
            self._log_pv(decision, snap)

    def _log_pv(self, decision: Decision, snap: Snapshot) -> None:
        """Prognose und Ist-PV protokollieren, um PV_FACTOR später zu kalibrieren."""
        now = datetime.fromisoformat(decision.ts)
        entry = {
            "ts": decision.ts,
            "forecast_w": round(pv_power_at(snap.solar, now), 1),
            "actual_w": snap.pv_power_w,
            "pv_factor": self.cfg.pv_factor,
        }
        path = Path(self.cfg.state_dir) / "pv_log.jsonl"
        try:
            with path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            log.warning("PV-Protokoll nicht schreibbar: %s", e)

    async def run_forever(self) -> None:
        log.info(
            "Batterie-Optimizer gestartet (DRY_RUN=%s, evcc=%s)",
            self.cfg.dry_run,
            self.cfg.evcc_url,
        )
        tasks = []
        if self.publisher:
            tasks.append(asyncio.create_task(self.publisher.run()))
        try:
            while True:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("Unerwarteter Fehler im Lauf")
                wake = next_run(datetime.now(UTC), self.cfg.run_offset_s)
                timeout = (wake - datetime.now(UTC)).total_seconds()
                if self.publisher:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.publisher.run_requested.wait(), timeout)
                    self.publisher.run_requested.clear()
                else:
                    await asyncio.sleep(timeout)
        finally:
            for t in tasks:
                t.cancel()
            await self.client.close()
