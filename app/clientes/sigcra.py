"""Cliente HTTP para per_api_sigcra (predios/rodales con geometria)."""
from __future__ import annotations

import os

import httpx

SIGCRA_BASE_URL = os.getenv("SIGCRA_BASE_URL", "http://forge-lab-api-sigcra-api:8080")
SIGCRA_API_KEY = os.getenv("SIGCRA_API_KEY", "")
TIMEOUT_S = 30.0


class SigcraError(RuntimeError):
    """Error al consultar per_api_sigcra."""


async def rodales_por_solicitud(solicitud: str) -> dict:
    """GeoJSON del/los rodal(es) de una solicitud SAFF (codigo_solicitud o nro_solicitud)."""
    url = f"{SIGCRA_BASE_URL}/v1/rodales"
    params = {"codigo_solicitud": solicitud}
    headers = {"X-API-Key": SIGCRA_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise SigcraError(f"sigcra_unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise SigcraError("sigcra_unauthorized")
    if resp.status_code >= 400:
        raise SigcraError(f"sigcra_error_{resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def ping() -> bool:
    url = f"{SIGCRA_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
