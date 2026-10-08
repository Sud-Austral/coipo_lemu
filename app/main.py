"""Lemu 2.0: deteccion de cambios de cobertura forestal (corta no autorizada).

Flujo: NDVI Sentinel-2 (per_api_gee) t0 vs t1 -> mascara de uso de suelo
(CIREN) -> cruce de predio (per_api_sigcra) -> plan de manejo vigente
(per_api_saff) -> lista de alertas reales.
"""
from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.clientes import gee, saff, sigcra
from app.logica.orquestador import REGIONES_PILOTO, ejecutar_deteccion

app = FastAPI(title="Lemu 2.0", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)


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
    return {
        "items": [
            {"id": clave, "nombre": cfg["nombre"], "puntos": len(cfg["puntos"])}
            for clave, cfg in REGIONES_PILOTO.items()
        ]
    }


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


app.mount("/", StaticFiles(directory="web", html=True), name="web")
