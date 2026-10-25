"""CLI.

python -m batterieoptimizer                       Dienst starten (alle 15 min)
python -m batterieoptimizer --once                ein Live-Lauf (DRY_RUN beachten!)
python -m batterieoptimizer --snapshot state.json Entscheidung aus aufgezeichnetem Snapshot
python -m batterieoptimizer --record DIR          Live-Snapshot als Test-Fixture speichern
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .decide import Outcome, decide
from .evcc import EvccClient
from .models import parse_state
from .mqtt import MqttPublisher
from .profile import build_profile
from .service import Service

# Nur diese Felder landen in Fixtures; der volle State enthält Tokens und Zertifikate
FIXTURE_KEYS = [
    "forecast",
    "battery",
    "batteryGridChargeLimit",
    "batteryMode",
    "tariffGrid",
    "tariffFeedIn",
    "pvPower",
    "homePower",
    "timezone",
    "version",
]


def _print_plan(outcome: Outcome, tz: ZoneInfo) -> None:
    if outcome.plan is None:
        return
    print("\nZeit         Preis   PV kWh  Last kWh  Modus    SoC kWh  Netz kWh", file=sys.stderr)
    for s in outcome.plan.steps:
        t = s.slot.start.astimezone(tz).strftime("%a %H:%M")
        print(
            f"{t:<12} {s.slot.price:6.3f}  {s.slot.pv_kwh:6.2f}  {s.slot.load_kwh:8.2f}  "
            f"{'charge' if s.charge else 'normal':<7}  {s.result.soc_kwh:7.2f}  "
            f"{s.result.grid_import:8.2f}",
            file=sys.stderr,
        )


def cmd_snapshot(args: argparse.Namespace, cfg: Settings) -> int:
    path = Path(args.snapshot)
    tz = ZoneInfo(cfg.timezone)
    history_path = Path(args.history) if args.history else path.parent / "history.json"
    meta_path = path.parent / "meta.json"
    if args.now:
        now = datetime.fromisoformat(args.now)
    elif meta_path.exists():
        now = datetime.fromisoformat(json.loads(meta_path.read_text())["now"])
    else:
        now = datetime.now(UTC)
    snap = parse_state(json.loads(path.read_text()))
    profile = build_profile(json.loads(history_path.read_text()), tz)
    outcome = decide(snap, profile, cfg, now)
    print(outcome.decision.model_dump_json(indent=2))
    if args.plan:
        _print_plan(outcome, tz)
    return 0


def anonymize_history(history: list[dict], timezone: str) -> list[dict]:
    """Ersetzt jeden Verbrauchswert durch den Profil-Mittelwert seiner Viertelstunde.

    Fixtures landen im öffentlichen Repo; einzelne Tage sollen keine Anwesenheit verraten.
    Das daraus gebaute Lastprofil bleibt identisch.
    """
    history = [s for s in history if isinstance(s, dict) and s.get("group") == "home"]
    profile = build_profile(history, ZoneInfo(timezone))
    for item in history[0]["data"]:
        item["energy"] = round(profile.kwh_per_quarter(datetime.fromisoformat(item["start"])), 4)
        item["returnEnergy"] = 0
    return history


async def cmd_record(args: argparse.Namespace, cfg: Settings) -> int:
    out = Path(args.record)
    out.mkdir(parents=True, exist_ok=True)
    client = EvccClient(cfg.evcc_url, timeout=cfg.evcc_timeout_s)
    try:
        now = datetime.now(UTC)
        state = await client.state()
        history = await client.home_history(now - timedelta(days=cfg.history_days), now)
    finally:
        await client.close()
    state = {k: state[k] for k in FIXTURE_KEYS if k in state}
    (out / "state.json").write_text(json.dumps(state, indent=1))
    (out / "history.json").write_text(json.dumps(anonymize_history(history, cfg.timezone)))
    local = now.astimezone(ZoneInfo(cfg.timezone)).isoformat(timespec="seconds")
    (out / "meta.json").write_text(json.dumps({"now": local}, indent=1))
    print(f"Fixture gespeichert in {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="batterieoptimizer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--snapshot", help="State-JSON (offline, schreibt nie)")
    parser.add_argument("--history", help="Historie-JSON (Standard: history.json daneben)")
    parser.add_argument("--now", help="Zeitpunkt der Entscheidung, ISO (Standard: meta.json)")
    parser.add_argument("--plan", action="store_true", help="kompletten Ladeplan ausgeben")
    parser.add_argument("--once", action="store_true", help="ein Live-Lauf")
    parser.add_argument("--record", metavar="DIR", help="Live-Snapshot als Fixture speichern")
    parser.add_argument("--dry-run", action="store_true", help="nie in evcc schreiben")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    overrides = {"dry_run": True} if args.dry_run else {}
    cfg = Settings(**overrides)

    if args.snapshot:
        return cmd_snapshot(args, cfg)
    if args.record:
        return asyncio.run(cmd_record(args, cfg))
    if args.once:
        decision = asyncio.run(_once(cfg))
        print(decision.model_dump_json(indent=2))
        return 0 if decision.decision != "error" else 1
    asyncio.run(Service(cfg, publisher=MqttPublisher(cfg)).run_forever())
    return 0


async def _once(cfg: Settings):
    service = Service(cfg)
    try:
        return await service.run_once()
    finally:
        await service.client.close()


if __name__ == "__main__":
    sys.exit(main())
