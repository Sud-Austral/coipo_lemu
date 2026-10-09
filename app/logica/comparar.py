"""Comparador antes/despues: Sentinel-2 del poligono en t0 y t1 (API GEE).

Por cada fecha se pide /v1/tiles/rgb con la geometria del poligono CIREN,
`escena='mas_limpia'` y `mascara='cs_plus'`: una sola fecha de pasada (mosaico
de los granulos del dia, nunca mediana), la de menos nubes (Cloud Score+)
SOBRE EL POLIGONO, sin recortar la tesela (se ve el entorno) y con los
pixeles nublados residuales transparentes. Empate de nubosidad: la escena mas
cercana a la fecha (t0 o t1). `combinacion`: 'color_real' (B4/B3/B2) o
'infrarrojo' (B8/B4/B3).

La ventana crece +-15, +-30, +-60 dias y se queda con la primera cuya mejor
escena tenga <= 10 % de nubes sobre el poligono; si ninguna, la mejor
encontrada (la de +-60, que contiene a las otras). Se informan la fecha real
de la escena, su % de nubes y las candidatas de esa ventana (fecha, % nubes)
para elegir otra a mano (`escena_t0`/`escena_t1`: se pide solo ese dia).

Presentacion: remuestreo bicubico (suaviza el borde del pixel de 10 m al
acercar; no inventa detalle) y contraste local (percentiles 2-98 por banda del
poligono + 1,5 km). Doble pasada: cada lado trae su rango; luego se pide de
nuevo la escena de cada lado con el rango COMUN (min de los min, max de los
max), para que un lado no se vea mas claro por el ajuste y no por el cambio.
Si la segunda pasada falla, cada lado queda con su propio rango
(`estiramiento_comun: false`).

Cache 30 min por (region, poligono, combinacion, fecha, cloud), por (...,
escena elegida, cloud) y por (..., escena, rango comun); un fallo de GEE no se
cachea ni agranda la ventana.
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
COMBINACIONES = {
    "color_real": "Sentinel-2 color real",
    "infrarrojo": "Sentinel-2 falso color infrarrojo (B8/B4/B3)",
}
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
        "estiramiento": data.get("estiramiento"),
    }


def _error(exc: gee.GeeError) -> str:
    return str(exc).split(":")[0]


async def lado(fecha: str, geometria: dict, cloud: int, combinacion: str = "color_real") -> dict:
    """Escena mas limpia sobre el poligono alrededor de `fecha` (+-15, +-30, +-60 dias)."""
    base = date.fromisoformat(fecha)
    mejor = None
    for dias in VENTANAS_DIAS:
        inicio, fin = (base - timedelta(days=dias)).isoformat(), (base + timedelta(days=dias)).isoformat()
        try:
            data = await gee.tiles_rgb(inicio, fin, geometria, cloud=cloud, fecha_objetivo=fecha, combinacion=combinacion)
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


def _dia(escena: str) -> tuple[str, str]:
    """Ventana [escena, escena + 1 dia): solo la pasada de ese dia."""
    return escena, (date.fromisoformat(escena) + timedelta(days=1)).isoformat()


async def lado_escena(fecha: str, escena: str, geometria: dict, cloud: int, combinacion: str = "color_real") -> dict:
    """La escena de un dia elegido a mano."""
    inicio, fin = _dia(escena)
    try:
        data = await gee.tiles_rgb(inicio, fin, geometria, cloud=cloud, fecha_objetivo=escena, combinacion=combinacion)
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


async def _lado_completo(
    region: str, zona_id: int, fecha: str, escena: str | None, geometria: dict, cloud: int, combinacion: str,
) -> dict:
    auto = await _cacheado((region, zona_id, combinacion, "auto", fecha, cloud),
                           lambda: lado(fecha, geometria, cloud, combinacion))
    if not escena or escena == auto.get("fecha_escena"):
        return auto
    manual = await _cacheado((region, zona_id, combinacion, "escena", escena, cloud),
                             lambda: lado_escena(fecha, escena, geometria, cloud, combinacion))
    # El selector sigue mostrando las candidatas de la busqueda automatica.
    return {**manual, "fecha": fecha, "candidatas": auto.get("candidatas") or manual.get("candidatas") or [],
            "automatica": auto.get("fecha_escena")}


def _rango_local(r: dict) -> tuple[list, list] | None:
    est = r.get("estiramiento") or {}
    if r.get("estado") != "ok" or not r.get("fecha_escena") or est.get("modo") != "local":
        return None
    mins, maxs = est.get("min"), est.get("max")
    if not isinstance(mins, list) or not isinstance(maxs, list) or not mins or len(mins) != len(maxs):
        return None
    return mins, maxs


def rango_comun(r0: dict, r1: dict) -> dict | None:
    """{min, max} por banda que cubre el contraste local de ambos lados; None si falta uno."""
    a, b = _rango_local(r0), _rango_local(r1)
    if not a or not b or len(a[0]) != len(b[0]):
        return None
    return {"min": [min(x, y) for x, y in zip(a[0], b[0])], "max": [max(x, y) for x, y in zip(a[1], b[1])]}


async def _tesela_comun(r: dict, geometria: dict, cloud: int, combinacion: str, rango: dict) -> dict:
    """La misma escena del lado, pedida de nuevo con el rango comun."""
    inicio, fin = _dia(r["fecha_escena"])
    try:
        data = await gee.tiles_rgb(inicio, fin, geometria, cloud=cloud, fecha_objetivo=r["fecha_escena"],
                                   combinacion=combinacion, estiramiento_min=rango["min"], estiramiento_max=rango["max"])
    except gee.GeeError as exc:
        return {"estado": "sin_datos", "error": _error(exc)}
    if not data.get("tile_url") or data.get("fecha_escena") not in (None, r["fecha_escena"]):
        return {"estado": "sin_datos", "error": "escena_distinta"}
    return {"estado": "ok", "tile_url": data["tile_url"], "estiramiento": data.get("estiramiento")}


async def _con_rango(
    region: str, zona_id: int, r: dict, geometria: dict, cloud: int, combinacion: str, rango: dict | None,
) -> dict:
    """El lado con la tesela del rango comun; si no se pudo, con su propio rango."""
    if r.get("estado") != "ok":
        return r
    if rango is None:
        return {**r, "estiramiento_comun": False}
    clave = (region, zona_id, combinacion, "comun", r["fecha_escena"], cloud, tuple(rango["min"]), tuple(rango["max"]))
    comun = await _cacheado(clave, lambda: _tesela_comun(r, geometria, cloud, combinacion, rango))
    if comun["estado"] != "ok":
        return {**r, "estiramiento_comun": False}
    return {**r, "tile_url": comun["tile_url"], "estiramiento": comun.get("estiramiento") or {"modo": "forzado", **rango},
            "estiramiento_comun": True}


async def sentinel(
    region: str, config: dict, zona_id: int, t0: str, t1: str, cloud: int,
    escena_t0: str | None = None, escena_t1: str | None = None, combinacion: str = "color_real",
) -> dict:
    """{estado, id, bbox, combinacion, estiramiento_comun, t0:{...}, t1:{...}}; cada lado 'ok' o 'sin_datos'."""
    if combinacion not in COMBINACIONES:
        combinacion = "color_real"
    base = {"region": region, "id": zona_id, "t0_pedido": t0, "t1_pedido": t1, "combinacion": combinacion,
            "fuente": f"{COMBINACIONES[combinacion]}, 10 m, escena más limpia sobre el polígono (Cloud Score+)",
            "remuestreo": "bicubico", "nubes_max_pct": NUBES_MAX_PCT}
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
    geometria = zona["geometry"]
    r0, r1 = await asyncio.gather(
        _lado_completo(region, zona_id, t0, escena_t0, geometria, cloud, combinacion),
        _lado_completo(region, zona_id, t1, escena_t1, geometria, cloud, combinacion),
    )
    rango = rango_comun(r0, r1)
    c0, c1 = await asyncio.gather(
        _con_rango(region, zona_id, r0, geometria, cloud, combinacion, rango),
        _con_rango(region, zona_id, r1, geometria, cloud, combinacion, rango),
    )
    comun = rango if rango and c0.get("estiramiento_comun") and c1.get("estiramiento_comun") else None
    if comun or rango is None:
        r0, r1 = c0, c1
    else:  # la segunda pasada fallo en un lado: ambos con su propio rango, nunca mezclados
        r0 = await _con_rango(region, zona_id, r0, geometria, cloud, combinacion, None)
        r1 = await _con_rango(region, zona_id, r1, geometria, cloud, combinacion, None)
    algun_ok = r0["estado"] == "ok" or r1["estado"] == "ok"
    return {**base, "estado": "ok" if algun_ok else "sin_datos", "bbox": list(caja), "estiramiento_comun": comun,
            "t0": r0, "t1": r1}
