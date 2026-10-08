"""Comparador antes/despues: Sentinel-2 color real del poligono en t0 y t1 (per_api_gee).

Por cada fecha se pide /v1/tiles/rgb con el bbox del poligono + 1 km. La
ventana de busqueda se agranda hasta encontrar escenas: +-15 dias alrededor de
la fecha, luego +-30 y +-60; se usa la primera con imagenes. Se informa el
rango real de fechas de las escenas usadas (fecha_min/fecha_max), no la
ventana. Cache 30 min por (region, poligono, fecha, cloud); un fallo de GEE no
se cachea ni agranda la ventana.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import date, timedelta

from app.clientes import gee
from app.logica import geo, zonas_ciren

CACHE_TTL_S = 30 * 60
VENTANAS_DIAS = (15, 30, 60)
MARGEN_KM = 1.0
SIN_IMAGEN = f"Sin imagen Sentinel-2 a ±{VENTANAS_DIAS[-1]} días de la fecha"
SIN_DATOS = "Sin datos por el momento"

_cache: dict[tuple, tuple[float, dict]] = {}


def bbox_con_margen(geometria: dict, margen_km: float = MARGEN_KM) -> tuple[float, float, float, float] | None:
    caja = geo.bbox(geometria)
    if caja is None:
        return None
    oeste, sur, este, norte = caja
    dlat = margen_km / 110.574
    dlon = margen_km / (111.320 * math.cos(math.radians((sur + norte) / 2)))
    return (round(oeste - dlon, 6), round(sur - dlat, 6), round(este + dlon, 6), round(norte + dlat, 6))


async def lado(fecha: str, caja: tuple[float, float, float, float], cloud: int) -> dict:
    """Primera ventana (+-15, +-30, +-60 dias) con escenas Sentinel-2 alrededor de `fecha`."""
    base = date.fromisoformat(fecha)
    for dias in VENTANAS_DIAS:
        inicio, fin = (base - timedelta(days=dias)).isoformat(), (base + timedelta(days=dias)).isoformat()
        try:
            data = await gee.tiles_rgb(inicio, fin, *caja, cloud=cloud)
        except gee.GeeError as exc:
            error = str(exc).split(":")[0]
            if error == "gee_sin_imagenes":
                continue
            return {"estado": "sin_datos", "mensaje": SIN_DATOS, "error": error, "fecha": fecha}
        if not data.get("images") or not data.get("tile_url"):
            continue
        return {
            "estado": "ok",
            "fecha": fecha,
            "tile_url": data["tile_url"],
            "images": data["images"],
            "fecha_min": data.get("fecha_min"),
            "fecha_max": data.get("fecha_max"),
            "ventana": {"inicio": inicio, "fin": fin, "dias": dias},
        }
    return {"estado": "sin_datos", "mensaje": SIN_IMAGEN, "error": "gee_sin_imagenes", "fecha": fecha}


async def _lado_cacheado(clave: tuple, fecha: str, caja: tuple, cloud: int) -> dict:
    guardado = _cache.get(clave)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    resultado = await lado(fecha, caja, cloud)
    if resultado["estado"] == "ok" or resultado.get("error") == "gee_sin_imagenes":
        _cache[clave] = (time.time(), resultado)
    return resultado


async def sentinel(region: str, config: dict, zona_id: int, t0: str, t1: str, cloud: int) -> dict:
    """{estado, id, bbox, t0:{...}, t1:{...}}; cada lado 'ok' o 'sin_datos'."""
    base = {"region": region, "id": zona_id, "t0_pedido": t0, "t1_pedido": t1, "fuente": "Sentinel-2 color real, 10 m"}
    try:
        carga = await zonas_ciren.cargar_zonas(region, config)
    except zonas_ciren.ZonasError as exc:
        return {**base, "estado": "sin_datos", "mensaje": f"{SIN_DATOS}: no respondió CIREN.", "error": str(exc)}
    zona = next((z for z in carga["zonas"] if z["id"] == zona_id), None)
    if zona is None:
        return {**base, "estado": "sin_poligono", "mensaje": "Polígono CIREN no encontrado en la comuna"}
    caja = bbox_con_margen(zona["geometry"])
    if caja is None:
        return {**base, "estado": "sin_poligono", "mensaje": "Polígono CIREN sin geometría"}
    for clave in [k for k, v in _cache.items() if time.time() - v[0] >= CACHE_TTL_S]:
        del _cache[clave]
    r0, r1 = await asyncio.gather(
        _lado_cacheado((region, zona_id, t0, cloud), t0, caja, cloud),
        _lado_cacheado((region, zona_id, t1, cloud), t1, caja, cloud),
    )
    algun_ok = r0["estado"] == "ok" or r1["estado"] == "ok"
    return {**base, "estado": "ok" if algun_ok else "sin_datos", "bbox": list(caja), "t0": r0, "t1": r1}
