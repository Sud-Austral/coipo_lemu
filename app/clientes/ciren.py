"""Cliente HTTP para CIREN (ArcGIS REST, Catastro de Uso de la Tierra CONAF).

Servicio publico sin clave: https://esri.ciren.cl/server/rest/services/
USOS_DE_LA_TIERRA__CONAF/MapServer

No hay autenticacion ni secreto que configurar.
"""
from __future__ import annotations

import httpx

CIREN_BASE_URL = "https://esri.ciren.cl/server/rest/services/USOS_DE_LA_TIERRA__CONAF/MapServer"
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


def _sin_tildes(texto: str) -> str:
    reemplazos = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n"}
    for origen, destino in reemplazos.items():
        texto = texto.replace(origen, destino)
    return texto


def es_uso_forestal(uso: str | None, subuso: str | None) -> bool:
    """True si el uso/subuso corresponde a bosque o plantacion forestal."""
    texto = _sin_tildes(f"{uso or ''} {subuso or ''}".strip().lower())
    return any(clave in texto for clave in USOS_FORESTALES)
