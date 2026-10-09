"""Capas de mapa: teselas NDVI Sentinel-2 (API GEE) y consulta CIREN por punto."""
from __future__ import annotations

import asyncio
import re
import time

from app.clientes import ciren, gee
from app.logica.orquestador import rango

SENTINEL_CACHE_TTL_S = 30 * 60
MARGEN_BBOX_GRADOS = 0.05
SIN_IMAGEN = "Sin imagen Sentinel-2 en esa ventana"

_cache_tiles: dict[tuple, tuple[float, dict]] = {}


async def _tiles_fecha(fecha: str, bbox: tuple[float, float, float, float]) -> dict:
    inicio, fin = rango(fecha)
    ventana = {"inicio": inicio, "fin": fin}
    clave = (inicio, fin, bbox)
    guardado = _cache_tiles.get(clave)
    if guardado and time.time() - guardado[0] < SENTINEL_CACHE_TTL_S:
        return guardado[1]
    oeste, sur, este, norte = bbox
    try:
        data = await gee.tiles_ndvi(
            inicio, fin,
            oeste - MARGEN_BBOX_GRADOS, sur - MARGEN_BBOX_GRADOS,
            este + MARGEN_BBOX_GRADOS, norte + MARGEN_BBOX_GRADOS,
        )
    except gee.GeeError as exc:
        return {"estado": "sin_datos", "mensaje": SIN_IMAGEN, "error": str(exc).split(":")[0], "ventana": ventana}
    imagenes = data.get("images") or 0
    if not imagenes or not data.get("tile_url"):
        return {"estado": "sin_datos", "mensaje": SIN_IMAGEN, "images": imagenes, "ventana": ventana}
    resultado = {"estado": "ok", "tile_url": data["tile_url"], "images": imagenes, "ventana": ventana}
    _cache_tiles[clave] = (time.time(), resultado)
    return resultado


async def tiles_sentinel(bbox: tuple[float, float, float, float], t0: str, t1: str) -> dict:
    r0, r1 = await asyncio.gather(_tiles_fecha(t0, bbox), _tiles_fecha(t1, bbox))
    return {"indice": "ndvi", "fuente": "NDVI Sentinel-2, 10 m", "t0": r0, "t1": r1}


def _anio(nombre: str | None) -> int | None:
    m = re.search(r"(19|20)\d{2}", nombre or "")
    return int(m.group(0)) if m else None


async def ciren_punto(lat: float, lon: float, layer_id: int | None = None, anio: int | None = None) -> dict:
    """Uso/subuso CIREN del poligono que contiene el punto."""
    capa_nombre = None
    try:
        if layer_id is None:
            capa = await ciren.identify_layer(lon, lat)
            if not capa or capa.get("layer_id") is None:
                return {"estado": "sin_poligono", "mensaje": "Punto fuera de la cobertura CIREN"}
            layer_id = capa["layer_id"]
            capa_nombre = capa.get("layer_name")
            anio = _anio(capa_nombre)
        attrs = await ciren.query_punto(layer_id, lon, lat)
    except (ciren.CirenError, ValueError) as exc:
        return {"estado": "sin_datos", "mensaje": "Sin datos por el momento", "error": str(exc).split(":")[0]}
    base = {"capa": {"id": layer_id, "nombre": capa_nombre, "anio": anio}}
    if not attrs:
        return {"estado": "sin_poligono", "mensaje": "Sin polígono CIREN en este punto", **base}
    return {
        "estado": "ok",
        "uso": attrs.get("uso"),
        "subuso": attrs.get("subuso"),
        "uso_tierra": attrs.get("uso_tierra"),
        "superf_ha": attrs.get("superf_ha"),
        "nom_com": attrs.get("nom_com"),
        **base,
    }
