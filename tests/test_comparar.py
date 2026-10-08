"""Comparador antes/despues: ventana creciente de Sentinel-2 color real y cache (per_api_gee mockeado)."""
from __future__ import annotations

from datetime import date

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


def _escena(fecha, nubes, candidatas=None):
    return {"images": 1, "tile_url": f"https://ee/{fecha}/{{z}}/{{x}}/{{y}}", "fecha_escena": fecha,
            "fecha_min": fecha, "fecha_max": fecha, "nubes_pct": nubes,
            "candidatas": candidatas or [{"fecha": fecha, "nubes_pct": nubes}]}


@pytest.mark.asyncio
async def test_ventana_se_agranda_hasta_escena_limpia(zonas, monkeypatch):
    pedidos = []

    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None):
        pedidos.append((start, end, fecha_objetivo))
        assert geometry == CUADRADO
        # t0 (2026-07-01): +-15 sin escenas, +-30 solo con 40 % de nubes, +-60 con una al 3 %.
        if start == "2026-06-01":
            return _escena("2026-07-20", 40.0)
        if start == "2026-05-02":
            return _escena("2026-07-12", 3.0, [{"fecha": "2026-05-20", "nubes_pct": 12.5}, {"fecha": "2026-07-12", "nubes_pct": 3.0}])
        # t1 (2026-09-01): limpia ya en +-15
        if start == "2026-08-17":
            return _escena("2026-08-28", 0.0)
        raise gee.GeeError("gee_sin_imagenes")

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["estado"] == "ok"
    t0 = r["t0"]
    assert t0["ventana"] == {"inicio": "2026-05-02", "fin": "2026-08-30", "dias": 60}
    assert t0["fecha_escena"] == "2026-07-12" and t0["nubes_pct"] == 3.0 and t0["limpia"] and t0["eleccion"] == "automatica"
    assert t0["candidatas"][0] == {"fecha": "2026-05-20", "nubes_pct": 12.5}
    assert r["t1"]["ventana"]["dias"] == 15 and r["t1"]["fecha_escena"] == "2026-08-28"
    assert ("2026-06-16", "2026-07-16", "2026-07-01") in pedidos and ("2026-08-17", "2026-09-16", "2026-09-01") in pedidos
    assert not any(p[0] == "2026-08-02" for p in pedidos)  # t1 limpio a +-15: no se agranda
    # cache: la segunda vez no se consulta GEE
    n = len(pedidos)
    await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert len(pedidos) == n


@pytest.mark.asyncio
async def test_sin_escena_limpia_queda_la_mejor(zonas, monkeypatch):
    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None):
        return _escena("2026-07-05", {15: 60.0, 30: 35.0, 60: 22.0}[(date.fromisoformat(end) - date.fromisoformat(start)).days // 2])

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["t0"]["nubes_pct"] == 22.0 and r["t0"]["ventana"]["dias"] == 60 and r["t0"]["limpia"] is False


@pytest.mark.asyncio
async def test_escena_elegida_a_mano(zonas, monkeypatch):
    pedidos = []

    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None):
        pedidos.append((start, end))
        if start == "2026-07-20":
            return _escena("2026-07-20", 18.0)
        return _escena("2026-07-05", 1.0, [{"fecha": "2026-07-05", "nubes_pct": 1.0}, {"fecha": "2026-07-20", "nubes_pct": 18.0}])

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80, escena_t0="2026-07-20")
    t0 = r["t0"]
    assert ("2026-07-20", "2026-07-21") in pedidos
    assert t0["eleccion"] == "manual" and t0["fecha_escena"] == "2026-07-20" and t0["nubes_pct"] == 18.0
    assert t0["automatica"] == "2026-07-05" and len(t0["candidatas"]) == 2  # candidatas de la busqueda automatica
    assert r["t1"]["eleccion"] == "automatica"
    # cada combinacion se cachea aparte
    n = len(pedidos)
    await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80, escena_t0="2026-07-20")
    await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert len(pedidos) == n


@pytest.mark.asyncio
async def test_gee_caido_al_agrandar_muestra_lo_encontrado_sin_cache(zonas, monkeypatch):
    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None):
        if (date.fromisoformat(end) - date.fromisoformat(start)).days == 30:
            return _escena("2026-07-03", 50.0)
        raise gee.GeeError("gee_error_502")

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["t0"]["estado"] == "ok" and r["t0"]["incompleto"] and r["t0"]["nubes_pct"] == 50.0
    assert not comparar._cache


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
