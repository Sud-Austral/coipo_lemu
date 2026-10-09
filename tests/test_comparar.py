"""Comparador antes/despues: ventana creciente de Sentinel-2 color real y cache (API GEE mockeado)."""
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

    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, **kw):
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
    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, **kw):
        return _escena("2026-07-05", {15: 60.0, 30: 35.0, 60: 22.0}[(date.fromisoformat(end) - date.fromisoformat(start)).days // 2])

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["t0"]["nubes_pct"] == 22.0 and r["t0"]["ventana"]["dias"] == 60 and r["t0"]["limpia"] is False


@pytest.mark.asyncio
async def test_escena_elegida_a_mano(zonas, monkeypatch):
    pedidos = []

    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, **kw):
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
    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, **kw):
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
    assert c.get("/api/v1/comparar/sentinel?region=panguipulli&id=7&t0=2026-07-01&t1=2026-09-01&combinacion=swir").status_code == 422


def test_endpoint_pasa_combinacion(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    recibido = {}

    async def fake(*args, **kwargs):
        recibido["args"] = args
        return {"estado": "ok"}

    monkeypatch.setattr(comparar, "sentinel", fake)
    c = TestClient(main.app)
    c.get("/api/v1/comparar/sentinel?region=panguipulli&id=7&t0=2026-07-01&t1=2026-09-01&combinacion=infrarrojo")
    assert recibido["args"][-1] == "infrarrojo"
    c.get("/api/v1/comparar/sentinel?region=panguipulli&id=7&t0=2026-07-01&t1=2026-09-01")
    assert recibido["args"][-1] == "color_real"


# --- contraste local con rango comun t0/t1 (doble pasada) ---

def _local(fecha, mins, maxs):
    return {**_escena(fecha, 2.0), "estiramiento": {"modo": "local", "min": mins, "max": maxs, "gamma": 1.1}}


def _fake_doble(pedidos, falla_comun=None):
    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, combinacion="color_real",
                       estiramiento_min=None, estiramiento_max=None):
        pedidos.append({"start": start, "end": end, "combinacion": combinacion, "min": estiramiento_min, "max": estiramiento_max})
        if estiramiento_min is not None:  # segunda pasada: la misma escena con el rango comun
            if start == falla_comun:
                raise gee.GeeError("gee_error_502")
            return {**_escena(start, 2.0), "tile_url": f"https://ee/comun/{combinacion}/{start}",
                    "estiramiento": {"modo": "forzado", "min": estiramiento_min, "max": estiramiento_max, "gamma": 1.1}}
        if fecha_objetivo == "2026-07-01":
            return _local("2026-07-05", [300, 400, 200], [1800, 1500, 1200])
        return _local("2026-08-28", [150, 450, 250], [2600, 1400, 1300])

    return fake_rgb


@pytest.mark.asyncio
async def test_rango_comun_en_ambos_lados(zonas, monkeypatch):
    pedidos = []
    monkeypatch.setattr(gee, "tiles_rgb", _fake_doble(pedidos))
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80, combinacion="infrarrojo")
    comun = {"min": [150, 400, 200], "max": [2600, 1500, 1300]}
    assert r["estiramiento_comun"] == comun and r["combinacion"] == "infrarrojo" and r["remuestreo"] == "bicubico"
    assert "infrarrojo" in r["fuente"]
    for ref, escena in (("t0", "2026-07-05"), ("t1", "2026-08-28")):
        assert r[ref]["estiramiento_comun"] is True
        assert r[ref]["tile_url"] == f"https://ee/comun/infrarrojo/{escena}"
        assert r[ref]["estiramiento"]["min"] == comun["min"]
        assert r[ref]["fecha_escena"] == escena and r[ref]["candidatas"]  # el resto del lado no cambia
    segunda = [p for p in pedidos if p["min"] is not None]
    assert {(p["start"], p["end"]) for p in segunda} == {("2026-07-05", "2026-07-06"), ("2026-08-28", "2026-08-29")}
    assert all(p["combinacion"] == "infrarrojo" for p in pedidos)
    # cache: misma consulta sin GEE; otra combinacion se calcula aparte
    n = len(pedidos)
    await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80, combinacion="infrarrojo")
    assert len(pedidos) == n
    r2 = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert len(pedidos) == n + 4 and r2["t0"]["tile_url"] == "https://ee/comun/color_real/2026-07-05"


@pytest.mark.asyncio
async def test_rango_comun_que_falla_en_un_lado_deja_ambos_con_su_rango(zonas, monkeypatch):
    pedidos = []
    monkeypatch.setattr(gee, "tiles_rgb", _fake_doble(pedidos, falla_comun="2026-08-28"))
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["estiramiento_comun"] is None
    assert r["t0"]["tile_url"] == "https://ee/2026-07-05/{z}/{x}/{y}" and r["t0"]["estiramiento_comun"] is False
    assert r["t1"]["tile_url"] == "https://ee/2026-08-28/{z}/{x}/{y}" and r["t1"]["estiramiento_comun"] is False
    assert r["t0"]["estiramiento"]["modo"] == "local"


@pytest.mark.asyncio
async def test_un_lado_sin_imagen_no_hace_segunda_pasada(zonas, monkeypatch):
    pedidos = []
    base = _fake_doble(pedidos)

    async def fake_rgb(start, end, geometry, cloud=40, fecha_objetivo=None, **kw):
        if fecha_objetivo == "2026-09-01":
            raise gee.GeeError("gee_sin_imagenes")
        return await base(start, end, geometry, cloud, fecha_objetivo, **kw)

    monkeypatch.setattr(gee, "tiles_rgb", fake_rgb)
    r = await comparar.sentinel("panguipulli", CONFIG, 7, "2026-07-01", "2026-09-01", 80)
    assert r["estado"] == "ok" and r["estiramiento_comun"] is None
    assert r["t0"]["estiramiento_comun"] is False and r["t1"]["estado"] == "sin_datos"
    assert not any(p["min"] is not None for p in pedidos)


def test_rango_comun_requiere_contraste_local_en_ambos():
    ok = {"estado": "ok", "fecha_escena": "2026-07-05", "estiramiento": {"modo": "local", "min": [1, 2, 3], "max": [9, 9, 9]}}
    fijo = {**ok, "estiramiento": {"modo": "fijo", "motivo": "sin_rango_local", "min": [0, 0, 0], "max": [3000] * 3}}
    viejo = {k: v for k, v in ok.items() if k != "estiramiento"}  # API GEE sin estiramiento
    assert comparar.rango_comun(ok, ok) == {"min": [1, 2, 3], "max": [9, 9, 9]}
    assert comparar.rango_comun(ok, fijo) is None
    assert comparar.rango_comun(ok, viejo) is None


@pytest.mark.asyncio
async def test_cliente_tiles_rgb_pide_bicubico_y_contraste_local(monkeypatch):
    enviados = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"tile_url": "u"}

    class Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            enviados.append(json)
            return Resp()

    monkeypatch.setattr(gee.httpx, "AsyncClient", Cliente)
    await gee.tiles_rgb("2026-07-01", "2026-07-02", CUADRADO, cloud=80, fecha_objetivo="2026-07-01")
    await gee.tiles_rgb("2026-07-01", "2026-07-02", CUADRADO, combinacion="infrarrojo",
                        estiramiento_min=[1, 2, 3], estiramiento_max=[4, 5, 6])
    assert enviados[0]["remuestreo"] == "bicubico" and enviados[0]["estiramiento"] == "local"
    assert enviados[0]["escena"] == "mas_limpia" and enviados[0]["mascara"] == "cs_plus"
    assert enviados[0]["combinacion"] == "color_real" and "estiramiento_min" not in enviados[0]
    assert enviados[1]["combinacion"] == "infrarrojo"
    assert enviados[1]["estiramiento_min"] == [1, 2, 3] and enviados[1]["estiramiento_max"] == [4, 5, 6]
