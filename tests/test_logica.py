"""Tests de la logica de orquestacion con clientes reales reemplazados por mocks."""
from __future__ import annotations

import pytest

import asyncio

from app.logica import cambio_vegetacion, lista_negra


def test_regla_de_cambio_usa_umbral():
    assert cambio_vegetacion.delta(0.75, 0.40) == pytest.approx(-0.35)
    assert cambio_vegetacion.hay_cambio(-0.35) is True
    assert cambio_vegetacion.hay_cambio(-0.15) is True
    assert cambio_vegetacion.hay_cambio(-0.149) is False
    assert cambio_vegetacion.hay_cambio(None) is False
    assert cambio_vegetacion.delta(None, 0.4) is None


def test_es_uso_forestal():
    from app.clientes import ciren

    assert ciren.es_uso_forestal("Plantación", "Plantación Adulta") is True
    assert ciren.es_uso_forestal("Agrícola", "Cultivo anual") is False
    assert ciren.es_uso_forestal(None, None) is False


PREDIO_OK = {"estado": "ok", "rol": "209-58", "rol_norm": "209-58", "comuna": "PANGUIPULLI", "comudere": "14108"}


def _ficha(**extra):
    base = {"radio_km": 3.0, "saff": {"estado": "ok"}, "sigcra": {"estado": "ok"}, "predio": PREDIO_OK,
            "planes_rol": [], "planes": [], "dentro_de_rodal": None}
    return {**base, **extra}


def test_estado_sin_planes_es_alerta():
    assert lista_negra.evaluar_ficha(_ficha()).estado == lista_negra.ESTADO_ALERTA


def test_estado_saff_caido_es_sin_datos():
    ficha = _ficha(saff={"estado": "sin_datos", "error": "saff_unreachable"})
    assert lista_negra.evaluar_ficha(ficha).estado == lista_negra.ESTADO_SIN_DATOS


def test_estado_dentro_de_rodal_aprobado_es_en_regla():
    ficha = _ficha(
        planes=[{"Nro.Solicitud": "494/32-74/18"}],
        dentro_de_rodal={"nro_solicitud": "494/32-74/18", "rodal": "4", "aprobado": True, "aprobado_a_t1": True},
    )
    resultado = lista_negra.evaluar_ficha(ficha)
    assert resultado.estado == lista_negra.ESTADO_EN_REGLA
    assert resultado.plan_manejo == {"nro_solicitud": "494/32-74/18", "rodal": "4"}


def test_estado_sigcra_caido_con_planes_cercanos_queda_revisar():
    ficha = _ficha(planes=[{"Nro.Solicitud": "1"}], sigcra={"estado": "sin_datos"})
    resultado = lista_negra.evaluar_ficha(ficha)
    assert resultado.estado == lista_negra.ESTADO_REVISAR
    assert "SIGCRA sin datos" in resultado.motivo


def test_estado_plan_cercano_fuera_de_rodal_es_revisar():
    ficha = _ficha(planes=[{"Nro.Solicitud": "1"}])
    assert lista_negra.evaluar_ficha(ficha).estado == lista_negra.ESTADO_REVISAR


def test_plan_aprobado_respeta_fecha_t1():
    import datetime

    from app.logica.ficha_punto import plan_aprobado

    plan = {"Estdo Solicitud": "APROBADA", "Fec.Resolucion": "2026-05-10T00:00:00"}
    assert plan_aprobado(plan, datetime.date(2026, 9, 1)) is True
    assert plan_aprobado(plan, datetime.date(2026, 1, 1)) is False
    sin_estado = {"Estdo Solicitud": "EN TRAMITE", "Nro.Resolucion": "12", "Super.Aprobada": 5.2, "Fec.Resolucion": "2020-01-01"}
    assert plan_aprobado(sin_estado, datetime.date(2026, 1, 1)) is True
    assert plan_aprobado({"Estdo Solicitud": "RECHAZADA", "Super.Aprobada": 0}, None) is False


def test_estado_listado_sin_rol_por_cercania():
    assert lista_negra.evaluar(False, PREDIO_OK, [], 0, 3.0).estado == lista_negra.ESTADO_SIN_DATOS
    assert lista_negra.evaluar(True, PREDIO_OK, [], 0, 3.0).estado == lista_negra.ESTADO_ALERTA
    assert lista_negra.evaluar(True, PREDIO_OK, [], 2, 3.0).estado == lista_negra.ESTADO_REVISAR


CUADRADO = {"type": "Polygon", "coordinates": [[[-72.2, -39.7], [-72.19, -39.7], [-72.19, -39.69], [-72.2, -39.69], [-72.2, -39.7]]]}


def _zona(zid, ha=5.0, lon=-72.195):
    return {"id": zid, "uso": "Bosques", "subuso": "Bosque Nativo", "superf_ha": ha,
            "centroide": {"lat": -39.695, "lon": lon}, "geometry": CUADRADO}


async def _esperar(orq, *args):
    for _ in range(200):
        r = await orq.deteccion(*args)
        if r["estado"] != "calculando":
            return r
        await asyncio.sleep(0.01)
    raise AssertionError("la deteccion no termino")


@pytest.fixture
def orq(monkeypatch):
    from app.logica import ficha_punto, orquestador, predios_ciren, zonas_ciren

    async def fake_zonas(region, config):
        return {"capa": 12, "anio": 2024, "zonas": [_zona(1), _zona(2), _zona(3), _zona(4, ha=0.4)]}

    monkeypatch.setattr(zonas_ciren, "cargar_zonas", fake_zonas)
    monkeypatch.setattr(ficha_punto, "precargar_saff", lambda comuna: None)
    monkeypatch.setattr(ficha_punto, "indice_rol_en_cache", lambda comuna: {})
    monkeypatch.setattr(predios_ciren, "precargar_predios", lambda region, config: None)
    # Un predio con rol que cubre todos los centroides de prueba.
    predio = {"id": 1, "rol": "209-58", "rol_norm": "209-58", "comuna": "PANGUIPULLI", "comudere": "14108",
              "bbox": [-72.2, -39.7, -72.19, -39.69], "geometry": CUADRADO}
    carga_predios = {"region": "panguipulli", "capa": 11, "anio": 2018, "cargado_en": 1.0, "predios": [predio]}
    monkeypatch.setattr(predios_ciren, "predios_en_cache", lambda region: carga_predios)
    monkeypatch.setattr(orquestador, "TAMANO_LOTE", 2)
    orquestador._trabajos.clear()
    yield orquestador
    orquestador._trabajos.clear()


@pytest.mark.asyncio
async def test_deteccion_poligonos_en_lotes_y_estado_saff(orq, monkeypatch):
    from app.clientes import gee
    from app.logica import ficha_punto

    lotes = []

    async def fake_zonal(fc, t0, t1, cloud=40, scale=20):
        ids = [f["properties"]["id"] for f in fc["features"]]
        lotes.append(ids)
        valores = {1: (0.8, 0.3), 2: (0.8, 0.75), 3: (0.7, 0.5)}
        return {"images": {"t0": 2, "t1": 3}, "scale": 20,
                "items": [{"id": i, "ndvi_t0": valores[i][0], "ndvi_t1": valores[i][1]} for i in ids]}

    monkeypatch.setattr(gee, "zonal_ndvi_change", fake_zonal)
    # Plan SAFF a ~1 km del centroide: el poligono queda «revisar», no «alerta».
    monkeypatch.setattr(ficha_punto, "planes_ubicados_en_cache", lambda comuna, centro: [(-39.695, -72.183)])
    r = await _esperar(orq, "panguipulli", "2026-07-01", "2026-09-01", 40, 1.0)
    assert sorted(sum(lotes, [])) == [1, 2, 3]  # el de 0,4 ha no se evalua
    assert all(len(l) <= 2 for l in lotes)
    assert r["estado"] == "listo"
    assert r["resumen"]["poligonos_evaluados"] == 3
    assert r["resumen"]["con_cambio"] == 2
    assert [p["id"] for p in r["poligonos"]] == [1, 3]  # mas negativo primero
    assert {p["estado"] for p in r["poligonos"]} == {"revisar"}
    assert r["poligonos"][0]["planes_cercanos"] == 1
    assert r["poligonos"][0]["predio"]["rol"] == "209-58"
    assert "geometry" not in r["poligonos"][0]["predio"]
    assert r["imagenes"] == {"t0": 2, "t1": 3}

    # Plan aprobado (antes de t1) con el mismo rol -> en_regla ya en el listado.
    plan = {"Nro.Solicitud": "77/14/2025", "Rol Avaluo": "00209-00058", "Comuna_Predio": "Panguipulli",
            "Estdo Solicitud": "APROBADA", "Fec.Resolucion": "2025-03-01T00:00:00"}
    from app.logica import rol

    monkeypatch.setattr(ficha_punto, "indice_rol_en_cache", lambda comuna: rol.indexar_planes([plan]))
    r3 = await orq.deteccion("panguipulli", "2026-07-01", "2026-09-01", 40, 1.0)
    assert r3["resumen"]["en_regla"] == 2
    assert r3["poligonos"][0]["motivo"] == "Plan 77/14/2025 aprobado para el rol 209-58"
    assert r3["poligonos"][0]["planes_rol"] == 1
    monkeypatch.setattr(ficha_punto, "indice_rol_en_cache", lambda comuna: {})

    # Sin planes cerca -> alerta; el estado se recalcula sin volver a GEE.
    monkeypatch.setattr(ficha_punto, "planes_ubicados_en_cache", lambda comuna, centro: [])
    r2 = await orq.deteccion("panguipulli", "2026-07-01", "2026-09-01", 40, 1.0)
    assert r2["resumen"]["alertas"] == 2
    assert len(lotes) == 2


@pytest.mark.asyncio
async def test_deteccion_gee_caido_es_sin_datos_y_saff_cargando(orq, monkeypatch):
    from app.clientes import gee
    from app.logica import ficha_punto

    async def fake_zonal(fc, t0, t1, cloud=40, scale=20):
        raise gee.GeeError("gee_unreachable: ConnectError")

    monkeypatch.setattr(gee, "zonal_ndvi_change", fake_zonal)
    monkeypatch.setattr(ficha_punto, "planes_ubicados_en_cache", lambda comuna, centro: None)
    r = await _esperar(orq, "panguipulli", "2026-07-01", "2026-09-01", 40, 1.0)
    assert r["estado"] == "sin_datos"
    assert r["gee"]["estado"] == "sin_datos"
    assert r["resumen"]["sin_ndvi"] == 3 and r["resumen"]["con_cambio"] == 0
    assert r["mensaje"].startswith("Sin datos por el momento")
    trabajo = next(iter(orq._trabajos.values()))
    assert trabajo.ttl_s == orq.CACHE_FALLA_TTL_S


@pytest.mark.asyncio
async def test_deteccion_ciren_caido_es_sin_datos(orq, monkeypatch):
    from app.logica import zonas_ciren

    async def falla(region, config):
        raise zonas_ciren.ZonasError("ciren_unreachable")

    monkeypatch.setattr(zonas_ciren, "cargar_zonas", falla)
    r = await _esperar(orq, "panguipulli", "2026-07-01", "2026-09-01", 40, 1.0)
    assert r["estado"] == "sin_datos" and "CIREN" in r["mensaje"]


def test_centroide_y_punto_interior():
    from app.logica import geo

    lon, lat = geo.centroide(CUADRADO)
    assert lon == pytest.approx(-72.195) and lat == pytest.approx(-39.695)
    # Forma de C: el centroide cae en el hueco y se usa un punto interior.
    c = {"type": "Polygon", "coordinates": [[[0, 0], [3, 0], [3, 1], [1, 1], [1, 2], [3, 2], [3, 3], [0, 3], [0, 0]]]}
    x, y = geo.centroide(c)
    assert geo.punto_en_geometria(x, y, c)
    assert geo.bbox(c) == (0, 0, 3, 3)


@pytest.mark.asyncio
async def test_zonas_ciren_cache_disco_y_filtro(monkeypatch, tmp_path):
    from app.clientes import ciren
    from app.logica import zonas_ciren

    llamadas = []

    async def fake_query(layer_id, where, out_fields, **kw):
        llamadas.append(where)
        return [
            {"type": "Feature", "properties": {"objectid": 7, "uso": "Bosques", "subuso": "Plantación", "superf_ha": 3.2}, "geometry": CUADRADO},
            {"type": "Feature", "properties": {"objectid": 8, "uso": "Bosques", "subuso": "Bosque Nativo", "superf_ha": 0.5}, "geometry": CUADRADO},
            # Anillo degenerado tras simplificar: se descarta.
            {"type": "Feature", "properties": {"objectid": 9, "uso": "Bosques", "superf_ha": 2}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 0]]]}},
        ]

    monkeypatch.setattr(ciren, "query_poligonos", fake_query)
    monkeypatch.setattr(zonas_ciren, "CACHE_DIR", tmp_path)
    zonas_ciren._cache.clear()
    cfg = {"comuna": "PANGUIPULLI", "ciren_layer": 12, "ciren_anio": 2024}
    carga = await zonas_ciren.cargar_zonas("panguipulli", cfg)
    assert "PANGUIPULLI" in llamadas[0] and "uso='Bosques'" in llamadas[0]
    assert [z["id"] for z in carga["zonas"]] == [7, 8]
    assert [z["id"] for z in zonas_ciren.filtrar(carga["zonas"], 1.0)] == [7]
    assert (tmp_path / "zonas_panguipulli_capa12.json").exists()
    zonas_ciren._cache.clear()
    await zonas_ciren.cargar_zonas("panguipulli", cfg)  # desde disco
    assert len(llamadas) == 1
    zonas_ciren._cache.clear()
