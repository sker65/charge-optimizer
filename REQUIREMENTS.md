# Batterie-Optimizer: Netzladen der Hausbatterie bei dynamischem Tarif

Anforderungen (Stand vor der Umsetzung). Abweichungen in der Umsetzung sind im README beschrieben.

## 1. Ziel

Ein kleiner, eigenständiger Dienst entscheidet einmal bzw. mehrmals täglich, **ob und bis zu welchem Preis** die Hausbatterie aus dem Netz geladen werden soll. Er setzt dafür in evcc die **Preisgrenze für das Netzladen der Batterie** (`batteryGridChargeLimit`) oder entfernt sie. Wann genau geladen wird, entscheidet evcc anhand dieser Grenze. Die Ausführung am Wechselrichter übernimmt die bestehende Home-Assistant-Logik.

Der Dienst soll sich nur dann lohnen lassen, wenn

- die PV-Prognose den Verbrauch bis zum nächsten sonnigen Zeitraum nicht deckt **und**
- der Preisabstand zwischen den billigen Ladeslots und den teuren Stunden, in denen die Batterie sonst Netzbezug vermeidet, Wirkungsgradverluste und Verschleiß übersteigt.

Der Dienst ist bewusst austauschbar: Sobald der evcc-Optimizer die Hausbatterie selbst aktiv steuert, kann er entfallen.

## 2. Systemkontext (Ist-Zustand)

| Komponente | Details |
|---|---|
| Wechselrichter | Hybrid-Wechselrichter (3-phasig, Niedervolt-Batterie), per Modbus und MQTT an HA |
| Hausbatterie | 12,5 kWh, Entladegrenze 20 % SoC, Spannung ≈ 50–54 V |
| PV | Ost/West, ca. 12 kWp |
| Wallbox / EV | Wallbox (11 kW) und E-Auto, von evcc gesteuert |
| Tarif | Dynamischer Stromtarif, 15-min-Preise, Abrechnung mit 15-min-Werten. evcc liefert **Endpreise inkl. Netzentgelt und Steuern** in €/kWh (z. B. 0,199 € bei 0 ct Börsenpreis). Einspeisung 0,077 €/kWh |
| evcc | v0.316.0, läuft per Docker auf demselben Host wie Home Assistant (`<evcc-host>:7070`). Optimizer 🧪 verfügbar (derzeit nur informativ) |
| Home Assistant | Docker-Container auf demselben Host, MQTT (Mosquitto) als Backbone |
| Hausverbrauch | ca. 11–16 kWh/Tag im Herbst/Winter (ohne Auto) |

### 2.1 Batteriesteuerung (fertig, nicht Teil dieses Projekts)

evcc schaltet die Hausbatterie über HA-Skripte in Modi. HA setzt den Wechselrichter entsprechend und prüft das Ergebnis.

| evcc-Modus | HA-Skript | Wirkung am Wechselrichter |
|---|---|---|
| normal | `script.battery_normal` | Normalbetrieb |
| hold | `script.battery_hold` | Batterie wird nicht entladen |
| holdcharge | `script.battery_hold_charge` | Batterie wird nicht geladen |
| **charge** | `script.battery_grid_charge` | **Netzladen**: Grid Charge + alle TOU-Slots laden bis `input_number.netzladen_ziel_soc` (Standard 80 %) mit `input_number.netzladen_leistung` (Standard 3000 W) |

evcc schaltet von selbst in `charge`, solange der aktuelle Preis ≤ `batteryGridChargeLimit` ist, und verlässt den Modus, wenn der Preis darüber steigt. Der Wechselrichter hört beim Ziel-SoC von selbst auf. **Genau diese Grenze ist die einzige Stellgröße des Optimizers.**

## 3. Schnittstellen

### 3.1 Eingaben (evcc REST-API)

Alle Werte über `GET http://<evcc-host>:7070/api/state` (optional mit `?jq=...`):

| Feld | Inhalt | Beispiel |
|---|---|---|
| `forecast.grid` | Preisprognose, Liste `[start_unix, end_unix, preis_eur_kwh]`, 15-min-Slots, ca. 22–36 h voraus | `[[1792920600, 1792921500, 0.1994], …]` |
| `forecast.feedin` | Einspeisevergütung, gleiches Format | |
| `forecast.solar.timeseries` | PV-Prognose, Liste `[ts_unix, leistung_w]`, 15-min-Raster | `[[1792929600, 2620], …]` |
| `forecast.solar.today/tomorrow/dayAfterTomorrow` | Tagessummen (`energy` in Wh, `complete`) | |
| `forecast.solar.scale` | Korrekturfaktor der Prognose | |
| `battery.soc`, `battery.capacity` | aktueller SoC (%), Kapazität (kWh) | `49`, `12.5` |
| `batteryGridChargeLimit` | aktuell gesetzte Grenze oder `null` | `0.22` |
| `batteryMode` | aktueller Modus | `charge` |
| `tariffGrid` | aktueller Preis (€/kWh) | `0.199` |

Verbrauchshistorie: `GET /api/history/energy?aggregate=15m&group=home&from=…&to=…` liefert `[{title, group, data:[{start, end, energy (kWh), returnEnergy}]}]`. Damit lässt sich ein Lastprofil nach Wochentag und Uhrzeit bilden. Der Ladepunkt (Auto) steht separat unter `group=loadpoint` und gehört **nicht** zum Hausverbrauch.

> Die Feldnamen stammen aus dem UI-State von evcc und sind laut evcc ohne Kompatibilitätszusage. Der Code muss fehlende Felder sauber abfangen und dann **keine** Grenze setzen (sicherer Zustand).

### 3.2 Ausgaben (evcc REST-API)

- Grenze setzen: `POST /api/batterygridchargelimit/{cost}` (cost in €/kWh, z. B. `0.215`)
- Grenze entfernen: `DELETE /api/batterygridchargelimit`

Pfade und Authentifizierung vor der Umsetzung gegen die OpenAPI von evcc 0.316 prüfen.

### 3.3 Transparenz (MQTT nach HA)

Jede Entscheidung wird als retained JSON auf MQTT veröffentlicht, z. B. `batterieoptimizer/state`, und per MQTT-Discovery als Sensor in HA angelegt:

```json
{
  "ts": "2026-10-25T14:00:00+01:00",
  "decision": "charge",            // charge | none | error
  "limit_eur": 0.215,               // null bei none
  "energy_needed_kwh": 5.8,
  "slots": ["2026-10-26T02:00", "..."],
  "avg_charge_price": 0.204,
  "avoided_price": 0.312,
  "spread_after_losses": 0.061,
  "pv_forecast_kwh": 3.4,
  "consumption_forecast_kwh": 13.1,
  "reason": "PV deckt Bedarf nicht, Spread 6,1 ct nach Verlusten"
}
```

Ziel: Im HA-Dashboard ist jederzeit nachvollziehbar, was der Optimizer warum entschieden hat.

## 4. Entscheidungslogik (Heuristik, Version 1)

Läuft zu festen Zeiten (z. B. 14:00 nach Veröffentlichung der Day-Ahead-Preise, 18:00, 23:00) und bei Bedarf manuell.

1. **Horizont** bestimmen: von jetzt bis zum Ende der vorhandenen Preisprognose.
2. **Verbrauchsprognose** pro 15-min-Slot aus dem Lastprofil der letzten 14–28 Tage, getrennt nach Werktag und Wochenende.
3. **PV-Prognose** pro Slot aus `forecast.solar.timeseries`.
4. **Energiebilanz simulieren:** SoC ab jetzt Slot für Slot fortschreiben (PV-Überschuss lädt, Defizit entlädt bis 20 %). Daraus ergeben sich die Slots mit Netzbezug ohne Netzladen und deren Preise.
5. **Bedarf** = Energie aus dem Netz in den teuren Slots, die die Batterie hätte decken können. Begrenzt auf `(Ziel-SoC − Prognose-SoC am Ladezeitpunkt) × Kapazität`.
6. **Ladeslots wählen:** die billigsten Slots *vor* den teuren Slots, Anzahl = Bedarf ÷ (Ladeleistung × 0,25 h).
7. **Wirtschaftlichkeit:** laden nur, wenn
   `vermiedener_Preis × η − Ladepreis − Verschleiß ≥ Mindestspread`
   mit η ≈ 0,85 (Roundtrip), Verschleiß ≈ 0,02 €/kWh, Mindestspread ≈ 0,02 €/kWh (alles konfigurierbar).
   PV-Überschuss, der danach ohnehin in die Batterie gegangen wäre, wird mit der Einspeisevergütung bewertet (Netzladen verdrängt sonst günstigen Eigenstrom).
8. **Ergebnis:** Grenze = Preis des teuersten gewählten Ladeslots, aufgerundet auf 0,1 ct, damit evcc genau diese Slots nutzt. Lohnt es sich nicht, wird die Grenze entfernt.
9. **Idempotent:** Nur schreiben, wenn sich die Grenze ändert.

Version 2 (optional, später): Optimierung als LP/MILP statt Heuristik, z. B. mit PuLP/HiGHS, oder Anbindung an den evcc-Optimizer (`github.com/evcc-io/optimizer`, als Docker-Image lokal betreibbar) als Rechenkern.

## 5. Randbedingungen

- **Sicherer Zustand:** Bei fehlenden Daten, API-Fehlern oder unplausiblen Werten (keine Preise, SoC unbekannt) wird keine Grenze gesetzt bzw. eine vom Dienst gesetzte Grenze entfernt. Fehler werden per MQTT gemeldet.
- **Fremde Grenzen respektieren:** Hat jemand die Grenze von Hand in evcc gesetzt (Abweichung vom zuletzt vom Dienst geschriebenen Wert), wird sie nicht überschrieben, bis sie manuell entfernt ist. Alternativ als Konfigurationsschalter umsetzen.
- **E-Auto:** Gleichzeitiges Laden von Auto und Batterie ist erlaubt, der Netzanschluss hält das aus. Die Ladeenergie des Autos zählt nicht zum Hausverbrauch.
- **Keine Schreibzugriffe auf den Wechselrichter oder auf HA-Entitäten** außer dem eigenen MQTT-Sensor. Die gesamte Gerätesteuerung bleibt in HA.
- **Zeitzone** Europe/Berlin, Zeitumstellung beachten (heute, 25.10., hat 25 Stunden). Intern mit UTC rechnen.
- Läuft als **Docker-Container** auf dem evcc-Host (Docker Compose) neben evcc.

## 6. Konfiguration

Über Umgebungsvariablen oder eine YAML-Datei:

| Parameter | Standard |
|---|---|
| `EVCC_URL` | `http://localhost:7070` |
| `MQTT_HOST` / `MQTT_PORT` / Zugangsdaten | `localhost` / `1883` |
| `SCHEDULE` | `14:00, 18:00, 23:00` |
| `BATTERY_CAPACITY_KWH` | aus evcc, Fallback 12,5 |
| `MIN_SOC` | 20 |
| `TARGET_SOC` | aus `input_number.netzladen_ziel_soc` über MQTT/HA oder fest 80 |
| `CHARGE_POWER_W` | 3000 (passend zu `input_number.netzladen_leistung`) |
| `ROUNDTRIP_EFFICIENCY` | 0,85 |
| `WEAR_COST_EUR_KWH` | 0,02 |
| `MIN_SPREAD_EUR_KWH` | 0,02 |
| `HISTORY_DAYS` | 21 |
| `DRY_RUN` | `true` für den Anfang: nur entscheiden und veröffentlichen, nicht in evcc schreiben |

## 7. Qualität & Tests

- Python 3.12, `httpx`, `paho-mqtt` (oder `aiomqtt`), `pydantic` für Modelle; Typisierung, `ruff`, `pytest`.
- Klare Trennung: **Datenbeschaffung** (evcc-Client) → **reine Entscheidungsfunktion** (ohne I/O) → **Ausführung** (evcc schreiben, MQTT veröffentlichen).
- Die Entscheidungsfunktion wird mit **aufgezeichneten Snapshots** von `/api/state` und der Historie getestet (Fixtures im Repo), u. a. für:
  - sonniger Tag → keine Grenze
  - trüber Tag, großer Abend-Spread → Grenze auf die Nachtslots
  - trüber Tag, flache Preise → keine Grenze
  - Batterie schon voll → keine Grenze
  - Prognose unvollständig oder Felder fehlen → sicherer Zustand
  - Zeitumstellung (23- und 25-Stunden-Tag)
- Ein kleines CLI (`python -m batterieoptimizer --dry-run --snapshot datei.json`), das die Entscheidung samt Begründung ausgibt.

## 8. Abnahmekriterien

1. Im Dry-Run läuft der Dienst eine Woche lang und veröffentlicht nachvollziehbare Entscheidungen in HA. Der Abgleich mit der Anzeige des evcc-Optimizers 🧪 zeigt keine groben Widersprüche.
2. Mit `DRY_RUN=false` setzt und entfernt er die Grenze in evcc; evcc schaltet in den passenden Slots auf `charge` und danach zurück.
3. An sonnigen Tagen wird nicht aus dem Netz geladen.
4. Bei evcc- oder Netzwerkausfall bleibt keine vom Dienst gesetzte Grenze dauerhaft stehen (spätestens beim nächsten Lauf entfernt, bzw. Grenze nur so hoch, dass sie nach dem Ladefenster nicht mehr greift).
5. Container startet nach Reboot automatisch, Logs sind über `docker logs` lesbar.

## 9. Nicht-Ziele

- Netzentladen (Einspeisen aus der Batterie)
- Steuerung des E-Auto-Ladens (macht evcc)
- Eigene Preis- oder PV-Prognosen (kommen aus evcc)
- HA-Integration im Sinne einer Custom Component

## 10. Offene Punkte

- Genaue evcc-Endpunkte und ob für die POST/DELETE-Aufrufe eine Anmeldung nötig ist.
- Ziel-SoC und Ladeleistung: fest im Dienst konfigurieren oder aus den HA-Helfern lesen.
- Plausibilität der evcc-PV-Prognose bei trübem Wetter (heute meldet sie für einen trüben Tag 17,6 kWh); ggf. `forecast.solar.scale` berücksichtigen oder eigene Korrektur über die Historie.
- Umgang mit einer von Hand gesetzten Grenze (siehe Randbedingungen).
