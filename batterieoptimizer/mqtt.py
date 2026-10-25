"""MQTT: Entscheidung retained veröffentlichen, HA-Discovery, Button für manuellen Lauf.

Die Verbindung läuft im Hintergrund und verbindet sich selbst neu. Ein MQTT-Ausfall hält
die Läufe nicht auf; nach dem Reconnect wird der letzte Stand erneut veröffentlicht.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt

from .config import Settings
from .models import Decision

log = logging.getLogger(__name__)

DEVICE = {
    "identifiers": ["batterieoptimizer"],
    "name": "Batterie-Optimizer",
    "manufacturer": "charge-optimizer",
}

# key, Name, Template, zusätzliche Discovery-Felder
SENSORS: list[tuple[str, str, str, dict]] = [
    ("decision", "Entscheidung", "{{ value_json.decision }}", {"icon": "mdi:battery-charging"}),
    (
        "limit",
        "Preisgrenze",
        "{{ value_json.limit_eur }}",
        {"unit_of_measurement": "€/kWh", "icon": "mdi:cash-lock"},
    ),
    ("applied", "Umsetzung", "{{ value_json.applied }}", {"icon": "mdi:check-network"}),
    ("reason", "Begründung", "{{ value_json.reason[:250] }}", {"icon": "mdi:text"}),
    (
        "savings",
        "Erwartete Ersparnis",
        "{{ value_json.expected_savings_eur }}",
        {"unit_of_measurement": "€", "icon": "mdi:piggy-bank"},
    ),
    (
        "energy",
        "Geplante Netzladung",
        "{{ value_json.energy_needed_kwh }}",
        {"unit_of_measurement": "kWh", "device_class": "energy"},
    ),
    (
        "next_block",
        "Nächster Lademodus",
        "{{ value_json.blocks[0].start if value_json.blocks else 'None' }}",
        {"device_class": "timestamp"},
    ),
    ("last_run", "Letzter Lauf", "{{ value_json.ts }}", {"device_class": "timestamp"}),
]


class MqttPublisher:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.prefix = cfg.mqtt_prefix
        self.state_topic = f"{self.prefix}/state"
        self.availability_topic = f"{self.prefix}/status"
        self.command_topic = f"{self.prefix}/cmd/run"
        self.run_requested = asyncio.Event()
        self._latest: Decision | None = None
        self._dirty = asyncio.Event()

    def publish(self, decision: Decision) -> None:
        self._latest = decision
        self._dirty.set()

    def _discovery(self) -> list[tuple[str, dict]]:
        base = {
            "state_topic": self.state_topic,
            "availability_topic": self.availability_topic,
            "device": DEVICE,
        }
        msgs = []
        for key, name, template, extra in SENSORS:
            cfg = {
                **base,
                "name": name,
                "unique_id": f"batterieoptimizer_{key}",
                "value_template": template,
                **extra,
            }
            if key == "decision":
                cfg["json_attributes_topic"] = self.state_topic
            msgs.append(
                (f"{self.cfg.mqtt_discovery_prefix}/sensor/batterieoptimizer/{key}/config", cfg)
            )
        button = {
            "name": "Jetzt neu rechnen",
            "unique_id": "batterieoptimizer_run",
            "command_topic": self.command_topic,
            "availability_topic": self.availability_topic,
            "device": DEVICE,
            "icon": "mdi:refresh",
        }
        msgs.append(
            (f"{self.cfg.mqtt_discovery_prefix}/button/batterieoptimizer/run/config", button)
        )
        return msgs

    async def run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.mqtt_host,
                    port=self.cfg.mqtt_port,
                    username=self.cfg.mqtt_username,
                    password=self.cfg.mqtt_password,
                    identifier="batterieoptimizer",
                    will=aiomqtt.Will(self.availability_topic, "offline", retain=True),
                ) as client:
                    log.info("MQTT verbunden mit %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
                    for topic, payload in self._discovery():
                        await client.publish(topic, json.dumps(payload), retain=True)
                    await client.publish(self.availability_topic, "online", retain=True)
                    await client.subscribe(self.command_topic)
                    self._dirty.set()
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._listen(client))
                        tg.create_task(self._send(client))
            except* aiomqtt.MqttError as eg:
                log.warning("MQTT-Verbindung verloren: %s, neuer Versuch in 10 s", eg.exceptions[0])
            await asyncio.sleep(10)

    async def _listen(self, client: aiomqtt.Client) -> None:
        async for _ in client.messages:
            log.info("Manueller Lauf über MQTT angefordert")
            self.run_requested.set()

    async def _send(self, client: aiomqtt.Client) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            if self._latest is not None:
                await client.publish(self.state_topic, self._latest.model_dump_json(), retain=True)
