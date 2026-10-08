"""Cliente HTTP para per_api_gee (NDVI/NDBI sobre Sentinel-2)."""
from __future__ import annotations

import os

import httpx

GEE_BASE_URL = os.getenv("GEE_BASE_URL", "http://forge-lab-api-gee-api:8080")
GEE_API_KEY = os.getenv("GEE_API_KEY", "")
TIMEOUT_S = 60.0


class GeeError(RuntimeError):
    """Error al consultar la API de GEE (upstream)."""


async def query_point_index(
    lat: float,
    lon: float,
    start: str,
    end: str,
    cloud: int = 40,
) -> dict:
    """Consulta un índice (NDVI por defecto) para un punto y rango de fechas.

    Usa /v1/query/point, que ya existe en per_api_gee.
    """
    url = f"{GEE_BASE_URL}/v1/query/point"
    params = {"lat": lat, "lon": lon, "start": start, "end": end, "cloud": cloud}
    headers = {"X-API-Key": GEE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise GeeError(f"gee_unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise GeeError("gee_unauthorized")
    if resp.status_code >= 400:
        raise GeeError(f"gee_error_{resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def analysis_heatmap(
    geometry: dict,
    variable: str,
    start: str,
    end: str,
    cloud: int = 40,
    dimensions: int = 1200,
) -> dict:
    """Genera un heatmap (ndvi/ndmi/nbr) sobre una geometría.

    Usa /v1/analysis/heatmap, que devuelve una imagen/thumbnail renderizada
    por GEE para la geometria y rango de fechas dados.
    """
    url = f"{GEE_BASE_URL}/v1/analysis/heatmap"
    body = {
        "geometry": geometry,
        "variable": variable,
        "start": start,
        "end": end,
        "cloud": cloud,
        "dimensions": dimensions,
    }
    headers = {"X-API-Key": GEE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GeeError(f"gee_unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise GeeError("gee_unauthorized")
    if resp.status_code >= 400:
        raise GeeError(f"gee_error_{resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def ping() -> bool:
    """True si per_api_gee responde /health."""
    url = f"{GEE_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
