"""Lemu 2.0: deteccion de cambios de cobertura forestal (corta no autorizada).

Flujo: NDVI Sentinel-2 (per_api_gee) t0 vs t1 -> mascara de uso de suelo
(CIREN) -> planes de manejo cercanos (per_api_saff) -> rodales del plan
(per_api_sigcra) -> estado del punto.
"""
from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.clientes import gee, saff, sigcra
from app.logica import capas, ficha_punto
from app.logica.orquestador import REGIONES_PILOTO, ejecutar_deteccion, info_regiones

app = FastAPI(title="Lemu 2.0", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _precargar_saff():
    # La carga SAFF de 10 anios tarda minutos: se hace en segundo plano al
    # arrancar para que deteccion y ficha no queden esperando.
    for cfg in REGIONES_PILOTO.values():
        ficha_punto.precargar_saff(cfg["comuna"])


def _region(region: str) -> dict:
    config = REGIONES_PILOTO.get(region)
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
    region: str = Query(..., description="Region piloto, ver /api/v1/regiones"),
    t0: date = Query(..., description="Fecha inicial de comparacion (YYYY-MM-DD)"),
    t1: date = Query(..., description="Fecha final de comparacion (YYYY-MM-DD)"),
    cloud: int = Query(40, ge=0, le=100, description="Porcentaje maximo de nubes aceptado"),
):
    if t0 >= t1:
        raise HTTPException(status_code=400, detail="t0_debe_ser_anterior_a_t1")
    if (t1 - t0).days > 365:
        raise HTTPException(status_code=400, detail="rango_maximo_1_anio")
    resultado = await ejecutar_deteccion(region, t0.isoformat(), t1.isoformat(), cloud)
    return resultado


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
    ficha = await ficha_punto.ficha_punto(config["comuna"], config["centro"], lat, lon, radio_km, t1, esperar_saff=False)
    return {"region": region, **ficha}


app.mount("/", StaticFiles(directory="web", html=True), name="web")
