"""Cliente HTTP para CIREN (ArcGIS REST).

Servicios publicos sin clave en https://esri.ciren.cl/server/rest/services/:
- USOS_DE_LA_TIERRA__CONAF/MapServer: Catastro de Uso de la Tierra CONAF.
- IDEMINAGRI/PROPIEDADES_RURALES/MapServer: deslindes prediales SII
  preparados por CIREN (IDE Minagri), una capa por region. Campos: rol,
  desccomu, comudere (codigo comuna SUBDERE), provdere, regidere.

No hay autenticacion ni secreto que configurar.
"""
from __future__ import annotations

import asyncio

import httpx

CIREN_BASE_URL = "https://esri.ciren.cl/server/rest/services/USOS_DE_LA_TIERRA__CONAF/MapServer"
PREDIOS_BASE_URL = "https://esri.ciren.cl/server/rest/services/IDEMINAGRI/PROPIEDADES_RURALES/MapServer"
CAMPOS_PREDIO = "objectid,rol,desccomu,comudere"
TIMEOUT_S = 30.0

# Usos considerados "bosque o plantacion forestal" para la mascara: todo lo
# demas (agricola, urbano, agua, etc.) se descarta de las alertas.
USOS_FORESTALES = {
    "bosque nativo",
    "bosque mixto",
    "plantacion",
    "plantacion adulta",
    "plantacion joven",
    "matorral arborescente denso",
    "renoval",
}


class CirenError(RuntimeError):
    """Error al consultar el servicio ArcGIS de CIREN."""


def _bbox_params(lon: float, lat: float, radio_m: float) -> str:
    # Aproximacion simple grados<->metros valida para Chile continental.
    delta_lat = radio_m / 111_000.0
    delta_lon = radio_m / (111_000.0 * 0.85)
    return f"{lon - delta_lon},{lat - delta_lat},{lon + delta_lon},{lat + delta_lat}"


async def identify_layer(lon: float, lat: float) -> dict | None:
    """Determina que capa regional de CIREN cubre el punto dado."""
    bbox = _bbox_params(lon, lat, 3000)
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": "4326",
        "layers": "all",
        "tolerance": "3",
        "mapExtent": bbox,
        "imageDisplay": "400,400,96",
        "returnGeometry": "false",
        "f": "json",
    }
    url = f"{CIREN_BASE_URL}/identify"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CirenError(f"ciren_unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise CirenError(f"ciren_error_{resp.status_code}")
    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    return {"layer_id": results[0].get("layerId"), "layer_name": results[0].get("layerName")}


async def query_uso_suelo(layer_id: int, lon: float, lat: float, radio_m: float = 500) -> list[dict]:
    """Trae los poligonos de uso de suelo que caen dentro del radio del punto."""
    west, south, east, north = _bbox_params(lon, lat, radio_m).split(",")
    params = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "uso,subuso,superf_ha,nom_com,uso_tierra",
        "maxAllowableOffset": "0.0003",
        "geometryPrecision": "5",
        "f": "geojson",
    }
    url = f"{CIREN_BASE_URL}/{layer_id}/query"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CirenError(f"ciren_unreachable: {exc}") from exc
    if resp.status_code >= 400:
        raise CirenError(f"ciren_error_{resp.status_code}")
    data = resp.json()
    return data.get("features") or []


async def query_punto(layer_id: int, lon: float, lat: float) -> dict | None:
    """Atributos del poligono de uso de suelo que contiene el punto (o None)."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "uso,subuso,uso_tierra,superf_ha,nom_com",
        "returnGeometry": "false",
        "f": "json",
    }
    url = f"{CIREN_BASE_URL}/{layer_id}/query"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CirenError(f"ciren_unreachable: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise CirenError(f"ciren_error_{resp.status_code}")
    data = resp.json()
    if data.get("error"):
        raise CirenError("ciren_query_error")
    features = data.get("features") or []
    if not features:
        return None
    return features[0].get("attributes") or {}


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise CirenError(f"ciren_unreachable: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise CirenError(f"ciren_error_{resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise CirenError("ciren_respuesta_invalida") from exc
    if data.get("error"):
        raise CirenError("ciren_query_error")
    return data


async def query_poligonos(
    layer_id: int,
    where: str,
    out_fields: str,
    max_allowable_offset: float = 0.0002,
    geometry_precision: int = 5,
    concurrencia: int = 3,
    timeout_s: float = 120.0,
    base_url: str = CIREN_BASE_URL,
    max_por_pagina: int | None = None,
) -> list[dict]:
    """Todos los poligonos (GeoJSON 4326) de la capa que cumplen `where`.

    Pagina con resultOffset/resultRecordCount usando el maxRecordCount que
    declara el servicio (acotado por max_por_pagina), ordenado por objectid
    para que las paginas no se traslapen. Lanza CirenError si cualquier
    pagina falla (nada de cargas parciales).
    """
    url = f"{base_url}/{layer_id}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        info = await _get_json(client, url, {"f": "json"})
        por_pagina = int(info.get("maxRecordCount") or 1000)
        if max_por_pagina:
            por_pagina = min(por_pagina, max_por_pagina)
        conteo = await _get_json(client, f"{url}/query", {"where": where, "returnCountOnly": "true", "f": "json"})
        total = int(conteo.get("count") or 0)
        sem = asyncio.Semaphore(concurrencia)

        async def pagina(offset: int) -> list[dict]:
            params = {
                "where": where,
                "outFields": out_fields,
                "orderByFields": "objectid",
                "resultOffset": offset,
                "resultRecordCount": por_pagina,
                "outSR": "4326",
                "maxAllowableOffset": max_allowable_offset,
                "geometryPrecision": geometry_precision,
                "f": "geojson",
            }
            async with sem:
                data = await _get_json(client, f"{url}/query", params)
            return data.get("features") or []

        paginas = await asyncio.gather(*(pagina(o) for o in range(0, total, por_pagina)))
    features = [f for p in paginas for f in p]
    if len(features) < total:
        raise CirenError("ciren_paginacion_incompleta")
    return features


async def query_por_ids(
    layer_id: int,
    ids: list[int],
    geometry_precision: int = 6,
    concurrencia: int = 3,
    timeout_s: float = 60.0,
    base_url: str = CIREN_BASE_URL,
    por_lote: int = 200,
) -> dict[int, dict]:
    """Geometria sin generalizar (GeoJSON 4326) de los objectid pedidos.

    Lotes de `objectid IN (...)` de <= por_lote, sin maxAllowableOffset. Un
    lote que falla se omite (sus ids no vienen en la respuesta); solo lanza
    CirenError si fallan todos.
    """
    lotes = [ids[i:i + por_lote] for i in range(0, len(ids), por_lote)]
    if not lotes:
        return {}
    url = f"{base_url}/{layer_id}/query"
    sem = asyncio.Semaphore(concurrencia)
    async with httpx.AsyncClient(timeout=timeout_s) as client:

        async def lote(grupo: list[int]) -> list[dict] | None:
            params = {
                "where": f"objectid IN ({','.join(str(int(i)) for i in grupo)})",
                "outFields": "objectid",
                "outSR": "4326",
                "geometryPrecision": geometry_precision,
                "f": "geojson",
            }
            async with sem:
                try:
                    data = await _get_json(client, url, params)
                except CirenError:
                    return None
            return data.get("features") or []

        respuestas = await asyncio.gather(*(lote(g) for g in lotes))
    if all(r is None for r in respuestas):
        raise CirenError("ciren_geometria_exacta")
    return {
        f["properties"]["objectid"]: f["geometry"]
        for r in respuestas if r for f in r
        if (f.get("properties") or {}).get("objectid") is not None and f.get("geometry")
    }


async def query_predio_punto(layer_id: int, lon: float, lat: float, geometria: bool = True) -> dict | None:
    """Predio CIREN (GeoJSON 4326) que contiene el punto, o None."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": CAMPOS_PREDIO,
        "returnGeometry": "true" if geometria else "false",
        "outSR": "4326",
        "geometryPrecision": "6",
        "f": "geojson",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        data = await _get_json(client, f"{PREDIOS_BASE_URL}/{layer_id}/query", params)
    features = data.get("features") or []
    return features[0] if features else None


def _sin_tildes(texto: str) -> str:
    reemplazos = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n"}
    for origen, destino in reemplazos.items():
        texto = texto.replace(origen, destino)
    return texto


def es_uso_forestal(uso: str | None, subuso: str | None) -> bool:
    """True si el uso/subuso corresponde a bosque o plantacion forestal."""
    texto = _sin_tildes(f"{uso or ''} {subuso or ''}".strip().lower())
    return any(clave in texto for clave in USOS_FORESTALES)
