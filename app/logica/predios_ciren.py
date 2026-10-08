"""Predios rurales CIREN (deslindes SII, IDE Minagri) de la comuna de la region.

Se consultan todos los predios de la comuna (comudere = codigo SUBDERE) en la
capa regional de PROPIEDADES_RURALES, simplificados (maxAllowableOffset ~20 m)
y con rol/comuna. Cache en memoria y en disco (datos_cache/) por 7 dias, igual
que las zonas de bosque; se precargan al arrancar. El predio de un punto se
busca localmente con una grilla de cajas + point-in-polygon (geo.py).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path

from app.clientes import ciren
from app.logica import geo, rol
from app.logica.zonas_ciren import limpiar_geometria

CACHE_TTL_S = 7 * 24 * 3600
CACHE_DIR = Path(os.getenv("LEMU_CACHE_DIR", "datos_cache"))
MAX_POR_PAGINA = 2000  # el servicio admite 10.000, pero paginas mas chicas van en paralelo
CELDA_GRADOS = 0.02

_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}
_tareas: dict[str, asyncio.Task] = {}
_grillas: dict[str, tuple[float, dict]] = {}


class PrediosError(RuntimeError):
    """No se pudieron cargar los predios CIREN de la region."""


def _where(config: dict) -> str:
    return f"comudere='{config['comudere']}'"


def _predio(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    geometria = limpiar_geometria(feature.get("geometry"))
    pid = props.get("objectid")
    if geometria is None or pid is None:
        return None
    caja = geo.bbox(geometria)
    return {
        "id": pid,
        "rol": (props.get("rol") or "").strip() or None,
        "rol_norm": rol.normalizar_rol(props.get("rol")),
        "comuna": props.get("desccomu"),
        "comudere": props.get("comudere"),
        "bbox": [round(v, 5) for v in caja],
        "geometry": geometria,
    }


def _archivo(region: str, layer_id: int) -> Path:
    return CACHE_DIR / f"predios_{region}_capa{layer_id}.json"


def _leer_disco(region: str, layer_id: int) -> dict | None:
    try:
        carga = json.loads(_archivo(region, layer_id).read_text(encoding="utf-8"))
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


async def cargar_predios(region: str, config: dict) -> dict:
    """Predios CIREN de la comuna (memoria -> disco -> CIREN)."""
    guardado = _cache.get(region)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    lock = _locks.setdefault(region, asyncio.Lock())
    async with lock:
        guardado = _cache.get(region)
        if guardado and time.time() - guardado[0] < CACHE_TTL_S:
            return guardado[1]
        carga = _leer_disco(region, config["predios_layer"])
        if carga is None:
            try:
                features = await ciren.query_poligonos(
                    config["predios_layer"], _where(config), ciren.CAMPOS_PREDIO,
                    base_url=ciren.PREDIOS_BASE_URL, max_por_pagina=MAX_POR_PAGINA,
                )
            except ciren.CirenError as exc:
                raise PrediosError(str(exc).split(":")[0]) from exc
            carga = {
                "region": region,
                "capa": config["predios_layer"],
                "anio": config.get("predios_anio"),
                "comuna": config["comuna"],
                "comudere": config["comudere"],
                "cargado_en": time.time(),
                "predios": [p for p in (_predio(f) for f in features) if p],
            }
            _escribir_disco(region, config["predios_layer"], carga)
        _cache[region] = (carga["cargado_en"], carga)
        return carga


def predios_en_cache(region: str) -> dict | None:
    guardado = _cache.get(region)
    if guardado and time.time() - guardado[0] < CACHE_TTL_S:
        return guardado[1]
    return None


def precargar_predios(region: str, config: dict) -> None:
    """Lanza la carga de predios en segundo plano (una a la vez por region)."""
    tarea = _tareas.get(region)
    if tarea and not tarea.done():
        return

    async def _correr():
        try:
            await cargar_predios(region, config)
        except Exception:  # noqa: BLE001 - se reintenta en la proxima consulta
            pass

    _tareas[region] = asyncio.create_task(_correr())


def _celda(lon: float, lat: float) -> tuple[int, int]:
    return math.floor(lon / CELDA_GRADOS), math.floor(lat / CELDA_GRADOS)


def _grilla(carga: dict) -> dict:
    """Indice celda -> predios cuya caja la toca (se arma una vez por carga)."""
    clave = carga["region"]
    previo = _grillas.get(clave)
    if previo and previo[0] == carga["cargado_en"]:
        return previo[1]
    grilla: dict[tuple[int, int], list[dict]] = {}
    for p in carga["predios"]:
        o, s, e, n = p["bbox"]
        (x0, y0), (x1, y1) = _celda(o, s), _celda(e, n)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                grilla.setdefault((x, y), []).append(p)
    _grillas[clave] = (carga["cargado_en"], grilla)
    return grilla


def predio_en_punto(carga: dict, lat: float, lon: float) -> dict | None:
    """Predio que contiene el punto (el mas chico si la simplificacion traslapa)."""
    candidatos = []
    for p in _grilla(carga).get(_celda(lon, lat), []):
        o, s, e, n = p["bbox"]
        if o <= lon <= e and s <= lat <= n and geo.punto_en_geometria(lon, lat, p["geometry"]):
            candidatos.append(p)
    if not candidatos:
        return None
    return min(candidatos, key=lambda p: (p["bbox"][2] - p["bbox"][0]) * (p["bbox"][3] - p["bbox"][1]))


def info_predio(predio: dict | None, carga: dict | None, geometria: bool = False) -> dict:
    """Predio para la respuesta: estado ok | sin_predio | sin_datos (carga no disponible)."""
    if carga is None:
        return {"estado": "sin_datos"}
    if predio is None:
        return {"estado": "sin_predio", "capa": {"id": carga["capa"], "anio": carga.get("anio")}}
    salida = {
        "estado": "ok",
        "id": predio["id"],
        "rol": predio["rol"],
        "rol_norm": predio["rol_norm"],
        "comuna": predio["comuna"],
        "comudere": predio["comudere"],
        "capa": {"id": carga["capa"], "anio": carga.get("anio")},
    }
    if geometria:
        salida["geometry"] = predio["geometry"]
    return salida


async def buscar_predio(region: str, config: dict, lat: float, lon: float, exacto: bool = False) -> dict:
    """info_predio (con geometria) del punto.

    Usa la carga en cache (geometria simplificada). Con exacto=True, o si la
    carga aun no esta, consulta el punto directo a CIREN (geometria completa);
    si CIREN falla se cae a la cache y, sin cache, responde sin_datos.
    """
    carga = predios_en_cache(region)
    if carga is None:
        precargar_predios(region, config)
    if carga is not None and not exacto:
        return info_predio(predio_en_punto(carga, lat, lon), carga, geometria=True)
    try:
        feature = await ciren.query_predio_punto(config["predios_layer"], lon, lat)
    except ciren.CirenError:
        if carga is not None:
            return info_predio(predio_en_punto(carga, lat, lon), carga, geometria=True)
        return {"estado": "sin_datos"}
    meta = {"capa": config["predios_layer"], "anio": config.get("predios_anio")}
    return info_predio(_predio(feature) if feature else None, meta, geometria=True)


def resumen(carga: dict) -> dict:
    """Conteos de la carga (para la verificacion del cache)."""
    ruta = _archivo(carga["region"], carga["capa"])
    return {
        "predios": len(carga["predios"]),
        "con_rol": sum(1 for p in carga["predios"] if p["rol_norm"]),
        "cache_mb": round(ruta.stat().st_size / 1e6, 2) if ruta.exists() else None,
    }
