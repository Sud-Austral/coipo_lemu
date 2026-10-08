"""Comparador antes/despues: ventana creciente de Sentinel-2 color real y cache (per_api_gee mockeado)."""
from __future__ import annotations

import pytest

from app.clientes import gee
from app.logica import comparar, zonas_ciren

CUADRADO = {"type": "Polygon", "coordinates": [[[-72.2, -39.7], [-72.19, -39.7], [-72.19, -39.69], [-72.2, -39.69], [-72.2, -39.7]]]}
CONFIG = {"comuna": "PANGUIPULLI", "ciren_layer": 12}


@pytest.fixture
def zonas(monkeypatch):
    async def fake_zonas(region, config):
        return {"zonas": [{"id": 7, "geometry": CUADRADO}]}

    monkeypatch.setattr(zonas_ciren, "cargar_zonas", fake_zonas)
    comparar._cache.clear()
    yield
    comparar._cache.clear()


def test_bbox_con_margen_de_1_km():
    oeste, sur, este, norte = comparar.bbox_con_margen(CUADRADO)
    assert norte - (-39.69) == pytest.approx(1 / 110.574, abs=1e-5)
    # 1 km en longitud a -39.7 son ~0,01168 grados
    assert -72.19 - (-72.2) + 2 * 0.01168 == pytest.approx(este - oeste, abs=1e-4)


@pytest.mark.asyncio
async def test_ventana_se_agranda_hasta_encontrar_imagen(zonas, monkeypatch):
    pedidos = []

    async def fake_rgb(start, end, west, south, east, north, cloud=40):
        pedidos.append((start, end))
        # t0 (2026-07-01) solo tiene escenas a +-60; t1 (2026-09-01) a +-15
        if start == "2026-08-17" or start == "2026-05-02":
            return {"images": 2, "tile_url": f"https://ee/{start}/{{z}}/{{x}}/{{y}}", "fecha_min": "2026-05-10", "fecha_max": "2026-06-20"}
        raise gee.GeeError("gee_sin_imagenes")

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 40)
    assert r["estado"] == "ok"
    assert r["t0"]["estado"] == "ok" and r["t0"]["ventana"] == {"inicio": "2026-05-02", "fin": "2026-08-30", "dias": 60}
    assert r["t0"]["fecha_min"] == "2026-05-10" and r["t0"]["fecha_max"] == "2026-06-20"
    assert r["t1"]["ventana"]["dias"] == 15
    assert ("2026-06-16", "2026-07-16") in pedidos and ("2026-06-01", "2026-07-31") in pedidos
    # cache: la segunda vez no se consulta GEE
    n = len(pedidos)
    await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 40)
    assert len(pedidos) == n


@pytest.mark.asyncio
async def test_gee_caido_es_sin_datos_por_lado_sin_cache(zonas, monkeypatch):
    llamadas = []

    async def falla(*args, **kwargs):
        llamadas.append(args)
        raise gee.GeeError("gee_unreachable: ConnectError")

    monkeypatch.setattr(gee, "tiles_rgb", falla)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 40)
    assert r["estado"] == "sin_datos"
    assert r["t0"] == {"estado": "sin_datos", "mensaje": comparar.SIN_DATOS, "error": "gee_unreachable", "fecha": "2026-07-01"}
    assert len(llamadas) == 2  # un fallo de GEE no agranda la ventana
    assert not comparar._cache


@pytest.mark.asyncio
async def test_sin_imagen_en_ninguna_ventana(zonas, monkeypatch):
    async def vacio(*args, **kwargs):
        raise gee.GeeError("gee_sin_imagenes")

    monkeypatch.setattr(gee, "tiles_rgb", vacio)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 40)
    assert r["t1"]["estado"] == "sin_datos" and r["t1"]["mensaje"] == comparar.SIN_IMAGEN


@pytest.mark.asyncio
async def test_poligono_inexistente(zonas):
    r = await comparar.sentinel("panguipulli", CONFIG, 99, "2026-07-01", "2026-09-01", 40)
    assert r["estado"] == "sin_poligono"


def test_endpoint_nunca_500(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    async def explota(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(comparar, "sentinel", explota)
    c = TestClient(main.app)
    r = c.get("/api/v1/comparar/sentinel?region=panguipulli&id=7&t0=2026-07-01&t1=2026-09-01")
    assert r.status_code == 200 and r.json()["estado"] == "sin_datos"
    assert c.get("/api/v1/comparar/sentinel?region=panguipulli&id=7&t0=2026-09-01&t1=2026-07-01").status_code == 400
    assert c.get("/api/v1/comparar/sentinel?region=nada&id=7&t0=2026-07-01&t1=2026-09-01").status_code == 404
