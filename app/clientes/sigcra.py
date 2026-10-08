"""Cliente HTTP para per_api_sigcra (rodales con geometria por solicitud SAFF).

Ruta real en per_api_sigcra: GET /v1/bosque-co2/rodales?solicitud=<Nro.Solicitud>
(app/bosque_routes.py). Devuelve un FeatureCollection en EPSG:4326 buscando la
solicitud en todas las vistas con geometria; solo expone columnas de una lista
fija (sin propietarios ni RUT).
"""
from __future__ import annotations

import os

import httpx

SIGCRA_BASE_URL = os.getenv("SIGCRA_BASE_URL", "http://forge-lab-api-sigcra-api:8080")
SIGCRA_API_KEY = os.getenv("SIGCRA_API_KEY", "")
TIMEOUT_S = 30.0
RODALES_PATH = "/v1/bosque-co2/rodales"

# Propiedades del rodal que se reenvian al front.
PROPIEDADES_RODAL = (
    "nro_solicitud",
    "numero_rodalet",
    "super_rodal",
    "sup_aprobada",
    "superficie_aprobada",
    "tipo_solicitud_compl",
    "fec_resolucion",
    "vista",
)


class SigcraError(RuntimeError):
    """Error al consultar per_api_sigcra."""


async def rodales_por_solicitud(nro_solicitud: str, limit: int = 200) -> dict:
    """FeatureCollection (EPSG:4326) de los rodales de una solicitud SAFF (p. ej. 494/32-74/18)."""
    url = f"{SIGCRA_BASE_URL}{RODALES_PATH}"
    params = {"solicitud": nro_solicitud, "limit": limit}
    headers = {"X-API-Key": SIGCRA_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise SigcraError(f"sigcra_unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        raise SigcraError("sigcra_unauthorized")
    if resp.status_code >= 400:
        raise SigcraError(f"sigcra_error_{resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SigcraError("sigcra_respuesta_invalida") from exc
    features = []
    for f in data.get("features") or []:
        if not f.get("geometry"):
            continue
        props = f.get("properties") or {}
        features.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": {k: props.get(k) for k in PROPIEDADES_RODAL if k in props},
        })
    return {"type": "FeatureCollection", "features": features}


async def ping() -> bool:
    url = f"{SIGCRA_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
