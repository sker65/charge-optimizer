# Batterie-Optimizer

Entscheidet alle 15 Minuten, ob die Hausbatterie gerade aus dem Netz geladen werden soll,
und setzt dafür in evcc die Preisgrenze `batteryGridChargeLimit` oder entfernt sie.
Anforderungen: [REQUIREMENTS.md](REQUIREMENTS.md).

## Funktionsweise

1. **Daten** aus evcc (`/api/state`): Preis- und Einspeiseprognose, PV-Prognose, SoC.
   Lastprofil aus `/api/history/energy` (Hausverbrauch ohne Ladepunkt, Mittelwert je
   Viertelstunde, getrennt nach Werktag/Wochenende, letzte `HISTORY_DAYS` Tage).
2. **Ladeplan** per Dynamischer Programmierung (`planner.py`): Für jede Viertelstunde bis zum
   Ende der Prognose wird entschieden, ob evcc im Modus `charge` sein soll. Simuliert wird,
   was evcc und der Wechselrichter tatsächlich tun:
   - *normal*: PV deckt den Verbrauch, Überschuss lädt, Defizit entlädt bis `MIN_SOC`.
   - *charge*: Netzladen mit `CHARGE_POWER_W` bis `TARGET_SOC`, keine Entladung unter
     `TARGET_SOC`. Am Ziel-SoC **hält** dieser Modus die Batterie. Der Plan nutzt das
     bewusst, um Batterieenergie für teurere Stunden aufzusparen.

   Kosten je Slot: Netzbezug × Preis − Einspeisung × Vergütung + Verschleiß je entladener
   kWh. Wirkungsgrad `ROUNDTRIP_EFFICIENCY` je Richtung als Wurzel. Netzladen kostet einen
   Aufschlag `MIN_SPREAD_EUR_KWH`, es wird also nur geladen, wenn es mindestens so viel
   bringt. Restenergie am Horizontende ist so viel wert wie Nachladen zum günstigsten Preis
   im Horizont (konservativ, kein Laden auf Vorrat für Zeiten ohne Preise).
3. **Stellgröße**: Nur die aktuelle Viertelstunde wird umgesetzt. Grenze = aktueller Preis,
   aufgerundet auf 0,1 ct (bzw. der Preis der nächsten Viertelstunde, wenn auch dort geladen
   wird). Nach 15 min wird mit neuem SoC und neuen Prognosen neu geplant.
4. **Hysterese** nur gegen das Abbrechen eines laufenden Lademodus (`HYSTERESIS_EUR` je
   Lauf). Gestartet wird sofort; bei gleichen Kosten wird früher geladen.
5. **Umsetzung** (`executor.py`):
   - Eine Grenze gehört dem Dienst, wenn sie dem zuletzt von ihm geschriebenen Wert
     entspricht (gespeichert in `/data/state.json`). Jede andere Grenze ist fremd und wird
     nicht angefasst, bis sie in evcc entfernt ist (`RESPECT_FOREIGN_LIMIT`).
   - Geschrieben wird nur bei Änderung. Bei Datenfehlern wird die eigene Grenze entfernt.
   - `DRY_RUN=true`: Es wird nur berechnet und veröffentlicht.

## Home Assistant

Per MQTT-Discovery entsteht das Gerät **Batterie-Optimizer**:
- Sensoren: Entscheidung (alle Details als Attribute), Preisgrenze, Umsetzung, Begründung,
  erwartete Ersparnis, geplante Netzladung, nächster Lademodus, letzter Lauf.
- Button **Jetzt neu rechnen**.

Der volle Stand liegt retained auf `batterieoptimizer/state`. `batterieoptimizer/status`
ist `online` oder `offline` (Last Will).

Felder von `applied`: `dry_run`, `written`, `unchanged`, `foreign_limit` (fremde Grenze,
nichts getan), `failed` (evcc-Fehler).

## Betrieb

```bash
cp .env.example .env        # anpassen
docker compose up -d --build
docker logs -f batterieoptimizer
```

Der Container nutzt `network_mode: host`, damit er evcc und Mosquitto unter `localhost`
erreicht. Zustand und PV-Protokoll liegen im Volume `batterieoptimizer-data`.

**Vor `DRY_RUN=false`:**
1. Die von Hand gesetzte Grenze in evcc entfernen, sonst gilt sie als fremd.
2. Prüfen, ob evcc für `POST/DELETE /api/batterygridchargelimit` ein Login verlangt.
   Falls ja: `EVCC_PASSWORD` setzen (Login über `/api/auth/login`).
3. `PV_FACTOR` anhand von `/data/pv_log.jsonl` kalibrieren (Prognose vs. Ist).

## CLI

```bash
uv run python -m batterieoptimizer --snapshot tests/fixtures/2026-10-25-sonntag/state.json --plan
uv run python -m batterieoptimizer --once --dry-run      # ein Live-Lauf, schreibt nicht
uv run python -m batterieoptimizer --record tests/fixtures/<name>   # Fixture aufzeichnen
```

`--snapshot` liest `history.json` und `meta.json` (Zeitpunkt) aus demselben Ordner.
`--record` speichert nur die benötigten State-Felder (der volle State enthält Tokens) und
ersetzt die Verbrauchshistorie durch Profil-Mittelwerte, damit Fixtures keine Anwesenheit
verraten.

## Entwicklung

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Annahmen und offene Punkte

- **Wechselrichter im Modus `charge` über Ziel-SoC**: angenommen wird, dass der Lade-SoC
  als Entladegrenze wirkt (Entladen bis 80 %, dann halten). Im Dry-Run beobachten.
- **PV-Prognose**: Die evcc-`timeseries` ist nicht mit `forecast.solar.scale` skaliert.
  An trüben Tagen liegt sie weit daneben (25.10.: 3–5 kW prognostiziert, 0,1–0,6 kW real).
  `PV_FACTOR` wirkt pauschal; eine Kalibrierung aus `pv_log.jsonl` steht aus.
- **Lastprofil**: Feiertage werden wie Werktage behandelt.
- **Ladeleistung** fest (`CHARGE_POWER_W`). Variable Leistung wäre im DP eine Liste von
  Aktionen, braucht aber eine HA-Automation, die die Leistung übernimmt.
