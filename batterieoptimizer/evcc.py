"""evcc-REST-Client: State und Historie lesen, Preisgrenze setzen und entfernen."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


class EvccError(Exception):
    """evcc nicht erreichbar oder Antwort unbrauchbar."""


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvccClient:
    def __init__(self, base_url: str, *, password: str | None = None, timeout: float = 10.0):
        self._password = password
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: str) -> Any:
        try:
            r = await self._http.get(path, params=params or None)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise EvccError(f"GET {path}: {e}") from e
        # ältere evcc-Versionen verpacken die Antwort in {"result": ...}
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data

    async def state(self) -> dict[str, Any]:
        data = await self._get("/api/state")
        if not isinstance(data, dict):
            raise EvccError("/api/state liefert kein Objekt")
        return data

    async def home_history(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/history/energy",
            aggregate="15m",
            group="home",
            **{"from": _iso(start)},
            to=_iso(end),
        )
        if not isinstance(data, list):
            raise EvccError("/api/history/energy liefert keine Liste")
        return data

    async def set_limit(self, cost: float) -> None:
        await self._write("POST", f"/api/batterygridchargelimit/{cost:.3f}")

    async def remove_limit(self) -> None:
        await self._write("DELETE", "/api/batterygridchargelimit")

    async def _write(self, method: str, path: str) -> None:
        try:
            r = await self._http.request(method, path)
            if r.status_code in (401, 403) and self._password:
                await self._login()
                r = await self._http.request(method, path)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise EvccError(f"{method} {path}: {e}") from e
        log.info("evcc: %s %s -> %s", method, path, r.status_code)

    async def _login(self) -> None:
        r = await self._http.post("/api/auth/login", json={"password": self._password})
        if r.status_code != 200:
            raise EvccError(f"Login bei evcc fehlgeschlagen ({r.status_code})")
