"""Cliente HTTP para API GEE (NDVI/NDBI sobre Sentinel-2)."""
from __future__ import annotations

import os

import httpx

GEE_BASE_URL = os.getenv("GEE_BASE_URL", "")
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
    mascara: str = "cs_plus",
) -> dict:
    """NDVI medio por poligono en t0 y t1 (POST /v1/zonal/ndvi-change).

    `features` es una FeatureCollection 4326 (max 2500, cada una con
    properties.id). Con mascara 'cs_plus' (Cloud Score+ por pixel) devuelve
    {images:{t0,t1}, scale, items:[{id, ndvi_t0, ndvi_t1, delta,
    cobertura_t0, cobertura_t1, motivo?}]}; motivo 'nubes' = menos de la
    mitad del poligono sin nubes en t0 o t1 (ese NDVI viene null). Sin
    imagenes en algun periodo -> GeeError gee_sin_imagenes.
    """
    url = f"{GEE_BASE_URL}/v1/zonal/ndvi-change"
    body = {
        "features": features,
        "t0_start": t0[0], "t0_end": t0[1],
        "t1_start": t1[0], "t1_end": t1[1],
        "cloud": cloud, "scale": scale, "mascara": mascara,
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

    Usa GET /v1/tiles/ndvi de API GEE; devuelve {tile_url, images, ...}.
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


async def tiles_rgb(
    start: str, end: str, geometry: dict, cloud: int = 40, fecha_objetivo: str | None = None,
    combinacion: str = "color_real", estiramiento_min: list | None = None, estiramiento_max: list | None = None,
) -> dict:
    """Teselas Sentinel-2 de la escena mas limpia sobre `geometry` (POST /v1/tiles/rgb).

    escena 'mas_limpia' + mascara 'cs_plus' + recortar false: la fecha de
    pasada con menos nubes (Cloud Score+) sobre el poligono, sin recortar la
    tesela y con los pixeles nublados transparentes. Siempre con remuestreo
    bicubico y contraste local (percentiles 2-98 del poligono + 1,5 km), o el
    rango `estiramiento_min/max` si viene. `combinacion`: 'color_real' o
    'infrarrojo' (B8/B4/B3). Devuelve {tile_url, images, fecha_escena,
    nubes_pct, candidatas:[{fecha, nubes_pct}], fecha_min, fecha_max, ventana,
    estiramiento:{modo, min, max}}. Sin escenas en la ventana -> GeeError
    gee_sin_imagenes.
    """
    url = f"{GEE_BASE_URL}/v1/tiles/rgb"
    body = {
        "geometry": geometry, "start": start, "end": end, "cloud": cloud,
        "escena": "mas_limpia", "mascara": "cs_plus", "recortar": False, "fecha_objetivo": fecha_objetivo,
        "combinacion": combinacion, "remuestreo": "bicubico", "estiramiento": "local",
    }
    if estiramiento_min is not None and estiramiento_max is not None:
        body.update(estiramiento_min=list(estiramiento_min), estiramiento_max=list(estiramiento_max))
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
    """True si API GEE responde /health."""
    url = f"{GEE_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
