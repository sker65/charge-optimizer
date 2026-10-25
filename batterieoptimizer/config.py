"""Konfiguration aus Umgebungsvariablen und optionaler YAML-Datei (Umgebung hat Vorrang)."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    evcc_url: str = "http://localhost:7070"
    evcc_password: str | None = None  # nur nötig, wenn evcc für Schreibzugriffe Login verlangt
    evcc_timeout_s: float = 10.0

    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_prefix: str = "batterieoptimizer"
    mqtt_discovery_prefix: str = "homeassistant"

    timezone: str = "Europe/Berlin"
    # Lauf alle 15 min, so viele Sekunden nach dem Viertelstundenwechsel
    run_offset_s: int = Field(default=20, ge=0, lt=900)

    battery_capacity_kwh: float | None = None  # None = aus evcc, Fallback 12,5
    battery_capacity_fallback_kwh: float = 12.5
    min_soc: float = Field(default=20, ge=0, le=100)
    # muss zum Ziel-SoC passen, den HA beim Netzladen setzt
    target_soc: float = Field(default=80, ge=0, le=100)
    # muss zur Ladeleistung passen, die HA beim Netzladen setzt
    charge_power_w: float = Field(default=3000, gt=0)
    battery_max_power_w: float = Field(default=6000, gt=0)  # Lade-/Entladeleistung im Normalbetrieb

    roundtrip_efficiency: float = Field(default=0.85, gt=0, le=1)
    wear_cost_eur_kwh: float = Field(default=0.02, ge=0)
    min_spread_eur_kwh: float = Field(default=0.02, ge=0)
    # Wert einer kWh, die am Horizontende noch in der Batterie ist; None = günstigster Preis
    terminal_price_eur_kwh: float | None = None
    # Mindest-Kostenvorteil, damit ein laufender Lademodus vorzeitig beendet wird (je Lauf)
    hysteresis_eur: float = Field(default=0.005, ge=0)
    # Sicherheitsdeckel: nie eine höhere Grenze setzen
    max_limit_eur_kwh: float = Field(default=0.40, gt=0)
    feedin_fallback_eur_kwh: float = 0.077

    pv_factor: float = Field(default=0.8, ge=0, le=2)
    history_days: int = Field(default=21, ge=3, le=60)
    min_horizon_h: float = Field(default=4, ge=1)
    soc_step_kwh: float = Field(default=0.05, gt=0)

    respect_foreign_limit: bool = True
    dry_run: bool = True
    state_dir: str = "/data"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_file = os.environ.get("CONFIG_FILE", "config.yaml")
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file),
        )
