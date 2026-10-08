"""Cliente HTTP para per_api_saff (planes de manejo vigentes por predio)."""
from __future__ import annotations

import os
from datetime import date

import httpx

SAFF_BASE_URL = os.getenv("SAFF_BASE_URL", "http://forge-lab-api-saff-api:8080")
SAFF_API_KEY = os.getenv("SAFF_API_KEY", "")
TIMEOUT_S = 60.0


class SaffError(RuntimeError):
    """Error al consultar per_api_saff."""


async def historico_pm(fecha_desde: date, fecha_hasta: date, limit: int = 2000) -> dict:
    """Trae solicitudes de plan de manejo resueltas en el rango de fechas.

    Usa /v1/historico/pm (contrato real de per_api_saff, campo de fecha =
    SOLI_FECHA_RESOLUCION). No filtra por predio en el servidor: el llamador
    debe cruzar Cod. Predio / Nro.Solicitud contra el resultado.
    """
    url = f"{SAFF_BASE_URL}/v1/historico/pm"
    params = {
        "fecha_desde": fecha_desde.isoformat(),
        "fecha_hasta": fecha_hasta.isoformat(),
        "limit": limit,
    }
    headers = {"X-API-Key": SAFF_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise SaffError(f"saff_unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise SaffError("saff_unauthorized")
    if resp.status_code >= 400:
        raise SaffError(f"saff_error_{resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def ping() -> bool:
    url = f"{SAFF_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
