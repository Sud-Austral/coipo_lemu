"""Lemu 2.0: deteccion de cambios de cobertura forestal (corta no autorizada).

Flujo: poligonos de bosque CIREN de la comuna -> NDVI Sentinel-2 medio por
poligono t0 vs t1 (per_api_gee) -> predio CIREN (rol SII) del centroide ->
planes de manejo del mismo rol o cercanos al centroide (per_api_saff) ->
rodales del plan (per_api_sigcra, al abrir la ficha) -> estado del poligono.
"""
from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.clientes import gee, saff, sigcra
from app.logica import capas, ficha_punto, lista_negra, orquestador, predios_ciren, rol, zonas_ciren
from app.logica.orquestador import REGIONES, info_regiones

app = FastAPI(title="Lemu 2.0", version="0.4.0", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _precargar():
    # La carga SAFF de 10 anios tarda minutos y las de poligonos y predios
    # CIREN unos segundos: todas en segundo plano al arrancar para que
    # deteccion y ficha no queden esperando.
    for region, cfg in REGIONES.items():
        ficha_punto.precargar_saff(cfg["comuna"])
        zonas_ciren.precargar_zonas(region, cfg)
        predios_ciren.precargar_predios(region, cfg)


def _region(region: str) -> dict:
    config = REGIONES.get(region)
    if not config:
        raise HTTPException(status_code=404, detail="region_no_configurada")
    return config


@app.get("/health")
def health():
    return {"ok": True, "service": "per_lemu2.0"}


@app.get("/ready")
async def ready():
    gee_ok = await gee.ping()
    sigcra_ok = await sigcra.ping()
    saff_ok = await saff.ping()
    ok = gee_ok and sigcra_ok and saff_ok
    body = {
        "ok": ok,
        "service": "per_lemu2.0",
        "dependencias": {"gee": gee_ok, "sigcra": sigcra_ok, "saff": saff_ok},
    }
    return JSONResponse(status_code=200 if ok else 503, content=body)


@app.get("/api/v1/regiones")
def regiones():
    return {"items": info_regiones()}


@app.get("/api/v1/deteccion")
async def deteccion(
    region: str = Query(..., description="Region, ver /api/v1/regiones"),
    t0: date = Query(..., description="Fecha inicial de comparacion (YYYY-MM-DD)"),
    t1: date = Query(..., description="Fecha final de comparacion (YYYY-MM-DD)"),
    cloud: int = Query(40, ge=0, le=100, description="Porcentaje maximo de nubes aceptado"),
    min_ha: float = Query(zonas_ciren.MIN_HA_DEFECTO, ge=0, le=1000, description="Superficie minima del poligono CIREN"),
):
    if t0 >= t1:
        raise HTTPException(status_code=400, detail="t0_debe_ser_anterior_a_t1")
    if (t1 - t0).days > 365:
        raise HTTPException(status_code=400, detail="rango_maximo_1_anio")
    resultado = await orquestador.deteccion(region, t0.isoformat(), t1.isoformat(), cloud, min_ha)
    if resultado.get("error") == "region_no_configurada":
        raise HTTPException(status_code=404, detail="region_no_configurada")
    # 202 mientras el calculo sigue en segundo plano: el front consulta de nuevo cada 5 s.
    return JSONResponse(status_code=202 if resultado["estado"] == "calculando" else 200, content=resultado)


@app.get("/api/v1/sentinel/tiles")
async def sentinel_tiles(region: str = Query(...), t0: date = Query(...), t1: date = Query(...)):
    config = _region(region)
    return await capas.tiles_sentinel(config["bbox"], t0.isoformat(), t1.isoformat())


@app.get("/api/v1/ciren/punto")
async def ciren_punto(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    region: str | None = Query(None),
):
    if region:
        config = _region(region)
        return await capas.ciren_punto(lat, lon, config["ciren_layer"], config["ciren_anio"])
    return await capas.ciren_punto(lat, lon)


@app.get("/api/v1/punto/ficha")
async def punto_ficha(
    region: str = Query(...),
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radio_km: float = Query(3.0, gt=0, le=20),
    t1: date | None = Query(None, description="Si se entrega, evalua aprobacion a esa fecha"),
):
    config = _region(region)
    predio = await predios_ciren.buscar_predio(region, config, lat, lon)
    ficha = await ficha_punto.ficha_punto(
        config["comuna"], config["centro"], lat, lon, radio_km, t1, esperar_saff=False, predio=predio,
    )
    respuesta = {"region": region, **ficha}
    if t1 is not None:
        # Estado definitivo del cambio en este punto (con rodales SIGCRA).
        respuesta["estado_cambio"] = lista_negra.evaluar_ficha(ficha).to_dict()
    return respuesta


@app.get("/api/v1/predio/punto")
async def predio_punto(
    region: str = Query(...),
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Predio CIREN (rol SII) del punto, con su contorno y los planes SAFF de ese rol."""
    config = _region(region)
    predio = await predios_ciren.buscar_predio(region, config, lat, lon, exacto=True)
    respuesta = {"region": region, **{k: v for k, v in predio.items() if k != "geometry"}}
    respuesta["geometria"] = predio.get("geometry")
    indice = ficha_punto.indice_rol_en_cache(config["comuna"])
    if indice is None:
        ficha_punto.precargar_saff(config["comuna"])
        respuesta["saff"] = {"estado": "sin_datos"}
        respuesta["planes_saff"] = None
        respuesta["planes"] = []
        return respuesta
    planes = rol.planes_del_predio(indice, predio if predio["estado"] == "ok" else None)
    respuesta["saff"] = {"estado": "ok"}
    respuesta["planes_saff"] = len(planes)
    respuesta["planes"] = [
        {
            "nro_solicitud": p.get("Nro.Solicitud"),
            "estado": p.get("Estdo Solicitud"),
            "fec_resolucion": p.get("Fec.Resolucion"),
            "aprobado": ficha_punto.plan_aprobado(p, None),
        }
        for p in planes
    ]
    return respuesta


app.mount("/", StaticFiles(directory="web", html=True), name="web")
