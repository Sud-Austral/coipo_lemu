"""Orquesta el flujo completo: NDVI (GEE) -> mascara CIREN -> cruce SIGCRA -> lista negra SAFF.

Cachea resultados en memoria por (region, t0, t1) para no recalcular ni
volver a golpear las fuentes externas en cada carga del front.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from app.logica import cambio_vegetacion, lista_negra, mascara_bosque

CACHE_TTL_S = 15 * 60
_cache: dict[tuple, tuple[float, dict]] = {}

# Puntos de muestreo piloto para la region de Panguipulli (Los Rios). En una
# iteracion posterior esto se reemplaza por una grilla real o por los
# centroides de rodales SIGCRA.
REGIONES_PILOTO = {
    "panguipulli": {
        "nombre": "Panguipulli",
        "puntos": [
            {"lat": -39.636, "lon": -72.333, "codigo_predio": None},
            {"lat": -39.715, "lon": -72.145, "codigo_predio": None},
            {"lat": -39.52, "lon": -72.02, "codigo_predio": None},
        ],
    }
}


def _clave_cache(region: str, t0: str, t1: str) -> tuple:
    return (region, t0, t1)


def _rango(fecha_iso: str, dias_ventana: int = 15) -> tuple[str, str]:
    """Convierte una fecha puntual en una ventana [fecha, fecha+dias] para
    darle a Sentinel-2 margen de encontrar una imagen sin nubes."""
    base = date.fromisoformat(fecha_iso)
    fin = base + timedelta(days=dias_ventana)
    return base.isoformat(), fin.isoformat()


async def ejecutar_deteccion(region: str, t0: str, t1: str, cloud: int = 40) -> dict:
    """Ejecuta (o recupera de cache) la deteccion completa para una region y rango."""
    clave = _clave_cache(region, t0, t1)
    ahora = time.time()
    if clave in _cache:
        guardado_en, resultado = _cache[clave]
        if ahora - guardado_en < CACHE_TTL_S:
            return {**resultado, "desde_cache": True}

    config = REGIONES_PILOTO.get(region)
    if not config:
        return {
            "region": region,
            "t0": t0,
            "t1": t1,
            "error": "region_no_configurada",
            "regiones_disponibles": list(REGIONES_PILOTO),
        }

    t0_inicio, t0_fin = _rango(t0)
    t1_inicio, t1_fin = _rango(t1)

    resultados = []
    for punto in config["puntos"]:
        cambio = await cambio_vegetacion.detectar_cambio_punto(
            punto["lat"], punto["lon"], t0_inicio, t0_fin, t1_inicio, t1_fin, cloud
        )
        item = cambio.to_dict()
        if cambio.cambio_detectado:
            clasificacion = await mascara_bosque.clasificar_punto(punto["lat"], punto["lon"])
            item["uso_suelo"] = clasificacion.to_dict()
            if clasificacion.es_forestal:
                evaluacion = await lista_negra.evaluar_predio(
                    punto.get("codigo_predio"),
                    date.fromisoformat(t0),
                    date.fromisoformat(t1),
                )
                item["lista_negra"] = evaluacion.to_dict()
            else:
                item["lista_negra"] = {"estado": "descartado_no_forestal"}
        else:
            item["uso_suelo"] = None
            item["lista_negra"] = {"estado": "sin_cambio"}
        resultados.append(item)

    resultado = {
        "region": region,
        "region_nombre": config["nombre"],
        "t0": t0,
        "t1": t1,
        "indice": "ndvi",
        "puntos_evaluados": len(resultados),
        "alertas": sum(1 for r in resultados if r.get("lista_negra", {}).get("estado") == "alerta"),
        "resultados": resultados,
        "desde_cache": False,
    }
    _cache[clave] = (ahora, resultado)
    return resultado
