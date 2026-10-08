"""Comparador antes/despues: Sentinel-2 color real del poligono en t0 y t1 (per_api_gee).

Por cada fecha se pide /v1/tiles/rgb con la geometria del poligono CIREN,
`escena='mas_limpia'` y `mascara='cs_plus'`: una sola fecha de pasada, la de
menos nubes (Cloud Score+) SOBRE EL POLIGONO, sin recortar la tesela (se ve el
entorno) y con los pixeles nublados residuales transparentes. Empate de
nubosidad: la escena mas cercana a la fecha (t0 o t1).

La ventana crece +-15, +-30, +-60 dias y se queda con la primera cuya mejor
escena tenga <= 10 % de nubes sobre el poligono; si ninguna, la mejor
encontrada (la de +-60, que contiene a las otras). Se informan la fecha real
de la escena, su % de nubes y las candidatas de esa ventana (fecha, % nubes)
para elegir otra a mano (`escena_t0`/`escena_t1`: se pide solo ese dia).

Cache 30 min por (region, poligono, fecha, cloud) y por (region, poligono,
escena elegida, cloud); un fallo de GEE no se cachea ni agranda la ventana.
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
NUBES_MAX_PCT = 10.0  # escena "limpia": <= 10 % del poligono con nubes
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


def _resultado(fecha: str, data: dict, inicio: str, fin: str, dias: int | None, eleccion: str) -> dict:
    nubes = data.get("nubes_pct")
    return {
        "estado": "ok",
        "fecha": fecha,
        "eleccion": eleccion,
        "tile_url": data["tile_url"],
        "images": data.get("images"),
        "fecha_escena": data.get("fecha_escena"),
        "fecha_min": data.get("fecha_min"),
        "fecha_max": data.get("fecha_max"),
        "nubes_pct": nubes,
        "limpia": nubes is not None and nubes <= NUBES_MAX_PCT,
        "candidatas": data.get("candidatas") or [],
        "ventana": {"inicio": inicio, "fin": fin, "dias": dias},
    }


def _error(exc: gee.GeeError) -> str:
    return str(exc).split(":")[0]


async def lado(fecha: str, geometria: dict, cloud: int) -> dict:
    """Escena mas limpia sobre el poligono alrededor de `fecha` (+-15, +-30, +-60 dias)."""
    base = date.fromisoformat(fecha)
    mejor = None
    for dias in VENTANAS_DIAS:
        inicio, fin = (base - timedelta(days=dias)).isoformat(), (base + timedelta(days=dias)).isoformat()
        try:
            data = await gee.tiles_rgb(inicio, fin, geometria, cloud=cloud, fecha_objetivo=fecha)
        except gee.GeeError as exc:
            if _error(exc) == "gee_sin_imagenes":
                continue
            if mejor:  # se muestra lo ya encontrado, sin cachear (la busqueda quedo a medias)
                return {**mejor, "incompleto": True}
            return {"estado": "sin_datos", "mensaje": SIN_DATOS, "error": _error(exc), "fecha": fecha}
        if not data.get("tile_url"):
            continue
        # Cada ventana contiene a la anterior: su mejor escena es al menos igual de limpia.
        mejor = _resultado(fecha, data, inicio, fin, dias, "automatica")
        if mejor["limpia"]:
            return mejor
    return mejor or {"estado": "sin_datos", "mensaje": SIN_IMAGEN, "error": "gee_sin_imagenes", "fecha": fecha}


async def lado_escena(fecha: str, escena: str, geometria: dict, cloud: int) -> dict:
    """La escena de un dia elegido a mano (ventana [escena, escena + 1 dia))."""
    inicio, fin = escena, (date.fromisoformat(escena) + timedelta(days=1)).isoformat()
    try:
        data = await gee.tiles_rgb(inicio, fin, geometria, cloud=cloud, fecha_objetivo=escena)
    except gee.GeeError as exc:
        if _error(exc) == "gee_sin_imagenes":
            return {"estado": "sin_datos", "mensaje": "Sin escena Sentinel-2 ese día", "error": "gee_sin_imagenes",
                    "fecha": fecha, "escena_pedida": escena}
        return {"estado": "sin_datos", "mensaje": SIN_DATOS, "error": _error(exc), "fecha": fecha, "escena_pedida": escena}
    if not data.get("tile_url"):
        return {"estado": "sin_datos", "mensaje": "Sin escena Sentinel-2 ese día", "fecha": fecha, "escena_pedida": escena}
    return _resultado(fecha, data, inicio, fin, None, "manual")


def _cacheable(r: dict) -> bool:
    return (r["estado"] == "ok" and not r.get("incompleto")) or r.get("error") == "gee_sin_imagenes"


async def _cacheado(clave: tuple, fn) -> dict:
    guardado = _cache.get(clave)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    resultado = await fn()
    if _cacheable(resultado):
        _cache[clave] = (time.time(), resultado)
    return resultado


async def _lado_completo(region: str, zona_id: int, fecha: str, escena: str | None, geometria: dict, cloud: int) -> dict:
    auto = await _cacheado((region, zona_id, "auto", fecha, cloud), lambda: lado(fecha, geometria, cloud))
    if not escena or escena == auto.get("fecha_escena"):
        return auto
    manual = await _cacheado((region, zona_id, "escena", escena, cloud), lambda: lado_escena(fecha, escena, geometria, cloud))
    # El selector sigue mostrando las candidatas de la busqueda automatica.
    return {**manual, "fecha": fecha, "candidatas": auto.get("candidatas") or manual.get("candidatas") or [],
            "automatica": auto.get("fecha_escena")}


async def sentinel(
    region: str, config: dict, zona_id: int, t0: str, t1: str, cloud: int,
    escena_t0: str | None = None, escena_t1: str | None = None,
) -> dict:
    """{estado, id, bbox, t0:{...}, t1:{...}}; cada lado 'ok' o 'sin_datos'."""
    base = {"region": region, "id": zona_id, "t0_pedido": t0, "t1_pedido": t1,
            "fuente": "Sentinel-2 color real, 10 m, escena más limpia sobre el polígono (Cloud Score+)",
            "nubes_max_pct": NUBES_MAX_PCT}
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
        _lado_completo(region, zona_id, t0, escena_t0, zona["geometry"], cloud),
        _lado_completo(region, zona_id, t1, escena_t1, zona["geometry"], cloud),
    )
    algun_ok = r0["estado"] == "ok" or r1["estado"] == "ok"
    return {**base, "estado": "ok" if algun_ok else "sin_datos", "bbox": list(caja), "t0": r0, "t1": r1}
