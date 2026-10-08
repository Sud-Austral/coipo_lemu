"""Cliente HTTP para per_api_gee (NDVI/NDBI sobre Sentinel-2)."""
from __future__ import annotations

import os

import httpx

GEE_BASE_URL = os.getenv("GEE_BASE_URL", "http://forge-lab-api-gee-api:8080")
GEE_API_KEY = os.getenv("GEE_API_KEY", "")
TIMEOUT_S = 60.0


class GeeError(RuntimeError):
    """Error al consultar la API de GEE (upstream)."""


ZONAL_TIMEOUT_S = 180.0


async def zonal_ndvi_change(
    features: dict,
    t0: tuple[str, str],
    t1: tuple[str, str],
    cloud: int = 40,
    scale: int = 20,
) -> dict:
    """NDVI medio por poligono en t0 y t1 (POST /v1/zonal/ndvi-change).

    `features` es una FeatureCollection 4326 (max 2500, cada una con
    properties.id). Devuelve {images:{t0,t1}, scale, items:[{id, ndvi_t0,
    ndvi_t1, delta}]}. Sin imagenes en algun periodo -> GeeError
    gee_sin_imagenes.
    """
    url = f"{GEE_BASE_URL}/v1/zonal/ndvi-change"
    body = {
        "features": features,
        "t0_start": t0[0], "t0_end": t0[1],
        "t1_start": t1[0], "t1_end": t1[1],
        "cloud": cloud, "scale": scale,
    }
    headers = {"X-API-Key": GEE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=ZONAL_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GeeError(f"gee_unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        raise GeeError("gee_unauthorized")
    if resp.status_code == 422 and "NO_SENTINEL2_IMAGES" in resp.text:
        raise GeeError("gee_sin_imagenes")
    if resp.status_code >= 400:
        raise GeeError(f"gee_error_{resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GeeError("gee_respuesta_invalida") from exc


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


async def tiles_ndvi(start: str, end: str, west: float, south: float, east: float, north: float) -> dict:
    """Plantilla de teselas NDVI (mediana Sentinel-2 de la ventana) para MapLibre.

    Usa GET /v1/tiles/ndvi de per_api_gee; devuelve {tile_url, images, ...}.
    """
    url = f"{GEE_BASE_URL}/v1/tiles/ndvi"
    params = {"start": start, "end": end, "west": west, "south": south, "east": east, "north": north}
    headers = {"X-API-Key": GEE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise GeeError(f"gee_unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        raise GeeError("gee_unauthorized")
    if resp.status_code >= 400:
        raise GeeError(f"gee_error_{resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GeeError("gee_respuesta_invalida") from exc


async def tiles_rgb(start: str, end: str, west: float, south: float, east: float, north: float, cloud: int = 40) -> dict:
    """Teselas Sentinel-2 color real (mediana B4/B3/B2) del bbox (POST /v1/tiles/rgb).

    Devuelve {tile_url, images, fecha_min, fecha_max, ventana}. Sin escenas
    en la ventana -> GeeError gee_sin_imagenes.
    """
    url = f"{GEE_BASE_URL}/v1/tiles/rgb"
    body = {"start": start, "end": end, "west": west, "south": south, "east": east, "north": north, "cloud": cloud}
    headers = {"X-API-Key": GEE_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GeeError(f"gee_unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        raise GeeError("gee_unauthorized")
    if resp.status_code == 422 and "NO_SENTINEL2_IMAGES" in resp.text:
        raise GeeError("gee_sin_imagenes")
    if resp.status_code >= 400:
        raise GeeError(f"gee_error_{resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GeeError("gee_respuesta_invalida") from exc


async def ping() -> bool:
    """True si per_api_gee responde /health."""
    url = f"{GEE_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
