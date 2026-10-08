"""Zonas de deteccion: poligonos de bosque CIREN de la comuna de la region.

Se consultan todos los poligonos con uso «Bosques» de la comuna en la capa
regional CIREN (CONAF), simplificados (maxAllowableOffset ~20 m) y sin
atributos extra. Cache en memoria y en disco (datos_cache/) por 7 dias; se
precargan al arrancar en segundo plano. Para dibujar los poligonos con cambio
se pide aparte su geometria exacta (geometrias_exactas).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from app.clientes import ciren
from app.logica import geo

CACHE_TTL_S = 7 * 24 * 3600
CACHE_DIR = Path(os.getenv("LEMU_CACHE_DIR", "datos_cache"))
USO_BOSQUE = "Bosques"
MIN_HA_DEFECTO = 1.0  # bajo 1 ha el promedio a 20 m tiene muy pocos pixeles
OUT_FIELDS = "objectid,uso,subuso,superf_ha,nom_com"

_cache: dict[str, tuple[float, dict]] = {}
_exactas: dict[str, tuple[float, dict[int, dict]]] = {}  # region -> (cargado_en, objectid -> geometria)
_locks: dict[str, asyncio.Lock] = {}
_tareas: dict[str, asyncio.Task] = {}


class ZonasError(RuntimeError):
    """No se pudieron cargar los poligonos CIREN de la region."""


def _where(comuna: str) -> str:
    return f"upper(nom_com) like '%{comuna.upper()}%' AND uso='{USO_BOSQUE}'"


def limpiar_geometria(geometria: dict | None) -> dict | None:
    """Quita anillos degenerados que deja la simplificacion (< 4 vertices)."""
    if not geometria or geometria.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    polis = [geometria["coordinates"]] if geometria["type"] == "Polygon" else geometria["coordinates"]
    limpios = []
    for poli in polis:
        anillos = [a for a in poli if len(a) >= 4]
        if anillos and len(poli[0]) >= 4:
            limpios.append(anillos)
    if not limpios:
        return None
    if len(limpios) == 1:
        return {"type": "Polygon", "coordinates": limpios[0]}
    return {"type": "MultiPolygon", "coordinates": limpios}


def _zona(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    geometria = limpiar_geometria(feature.get("geometry"))
    zid = props.get("objectid")
    if geometria is None or zid is None:
        return None
    lon, lat = geo.centroide(geometria)
    return {
        "id": zid,
        "uso": props.get("uso"),
        "subuso": props.get("subuso"),
        "superf_ha": props.get("superf_ha"),
        "centroide": {"lat": round(lat, 6), "lon": round(lon, 6)},
        "geometry": geometria,
    }


def _archivo(region: str, layer_id: int) -> Path:
    return CACHE_DIR / f"zonas_{region}_capa{layer_id}.json"


def _leer_disco(region: str, layer_id: int) -> dict | None:
    ruta = _archivo(region, layer_id)
    try:
        carga = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - (carga.get("cargado_en") or 0) >= CACHE_TTL_S:
        return None
    return carga


def _escribir_disco(region: str, layer_id: int, carga: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _archivo(region, layer_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(carga, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_archivo(region, layer_id))
    except OSError:
        pass  # sin disco escribible queda solo la cache en memoria


async def cargar_zonas(region: str, config: dict) -> dict:
    """Poligonos de bosque CIREN de la comuna (memoria -> disco -> CIREN)."""
    guardado = _cache.get(region)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    lock = _locks.setdefault(region, asyncio.Lock())
    async with lock:
        guardado = _cache.get(region)
        if guardado and time.time() - guardado[0] < CACHE_TTL_S:
            return guardado[1]
        carga = _leer_disco(region, config["ciren_layer"])
        if carga is None:
            try:
                features = await ciren.query_poligonos(config["ciren_layer"], _where(config["comuna"]), OUT_FIELDS)
            except ciren.CirenError as exc:
                raise ZonasError(str(exc).split(":")[0]) from exc
            zonas = [z for z in (_zona(f) for f in features) if z]
            carga = {
                "region": region,
                "capa": config["ciren_layer"],
                "anio": config.get("ciren_anio"),
                "comuna": config["comuna"],
                "cargado_en": time.time(),
                "zonas": zonas,
            }
            _escribir_disco(region, config["ciren_layer"], carga)
        _cache[region] = (carga["cargado_en"], carga)
        return carga


def zonas_en_cache(region: str) -> dict | None:
    guardado = _cache.get(region)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    return None


def precargar_zonas(region: str, config: dict) -> None:
    """Lanza la carga de zonas en segundo plano (una a la vez por region)."""
    tarea = _tareas.get(region)
    if tarea and not tarea.done():
        return

    async def _correr():
        try:
            await cargar_zonas(region, config)
        except Exception:  # noqa: BLE001 - se reintenta en la proxima deteccion
            pass

    _tareas[region] = asyncio.create_task(_correr())


def _archivo_exactas(region: str, layer_id: int) -> Path:
    return CACHE_DIR / f"zonas_{region}_capa{layer_id}_exactas.json"


async def geometrias_exactas(region: str, config: dict, ids: list[int]) -> dict[int, dict]:
    """Geometria sin generalizar de los poligonos pedidos, solo para dibujar.

    La simplificada (cargar_zonas) es la que va a GEE; ArcGIS la simplifica
    poligono por poligono, asi que los bordes vecinos se cruzan o dejan
    huecos. Para los poligonos con cambio se pide la exacta por objectid y se
    cachea (memoria + disco, 7 dias, por objectid). Los ids que CIREN no
    entregue quedan fuera del resultado (el llamador usa la simplificada).
    """
    layer_id = config["ciren_layer"]
    guardado = _exactas.get(region)
    if not guardado or time.time() - guardado[0] >= CACHE_TTL_S:
        ruta = _archivo_exactas(region, layer_id)
        try:
            carga = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            carga = {}
        if time.time() - (carga.get("cargado_en") or 0) >= CACHE_TTL_S:
            carga = {"cargado_en": time.time(), "geometrias": {}}
        guardado = (carga["cargado_en"], {int(k): v for k, v in carga["geometrias"].items()})
        _exactas[region] = guardado
    cargado_en, geometrias = guardado
    faltan = [i for i in dict.fromkeys(ids) if i not in geometrias]
    if faltan:
        lock = _locks.setdefault(f"{region}|exactas", asyncio.Lock())
        async with lock:
            faltan = [i for i in faltan if i not in geometrias]
            nuevas = {}
            if faltan:
                try:
                    nuevas = await ciren.query_por_ids(layer_id, faltan)
                except ciren.CirenError:
                    nuevas = {}
            for zid, g in nuevas.items():
                limpia = limpiar_geometria(g)
                if limpia:
                    geometrias[zid] = limpia
            if nuevas:
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    ruta = _archivo_exactas(region, layer_id)
                    tmp = ruta.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"cargado_en": cargado_en, "geometrias": geometrias}, separators=(",", ":")),
                                   encoding="utf-8")
                    tmp.replace(ruta)
                except OSError:
                    pass
    return {i: geometrias[i] for i in ids if i in geometrias}


def filtrar(zonas: list[dict], min_ha: float) -> list[dict]:
    return [z for z in zonas if (z.get("superf_ha") or 0) >= min_ha]


def resumen(carga: dict, min_ha: float = MIN_HA_DEFECTO) -> dict:
    """Conteos de la carga (para el arranque y la verificacion del cache)."""
    zonas = carga["zonas"]
    usadas = filtrar(zonas, min_ha)
    por_subuso: dict[str, int] = {}
    for z in zonas:
        por_subuso[z.get("subuso") or "—"] = por_subuso.get(z.get("subuso") or "—", 0) + 1
    ruta = _archivo(carga["region"], carga["capa"])
    return {
        "poligonos": len(zonas),
        "ha": round(sum(z.get("superf_ha") or 0 for z in zonas), 1),
        "por_subuso": por_subuso,
        "min_ha": min_ha,
        "poligonos_min_ha": len(usadas),
        "ha_min_ha": round(sum(z.get("superf_ha") or 0 for z in usadas), 1),
        "cache_mb": round(ruta.stat().st_size / 1e6, 2) if ruta.exists() else None,
    }
