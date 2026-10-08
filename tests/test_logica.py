"""Tests de la logica de orquestacion con clientes reales reemplazados por mocks."""
from __future__ import annotations

import pytest

from app.logica import cambio_vegetacion, lista_negra, mascara_bosque


class FakeRespGee:
    def __init__(self, ndvi):
        self._ndvi = ndvi

    def get(self, key, default=None):
        if key == "ndvi":
            return self._ndvi
        return default


@pytest.mark.asyncio
async def test_cambio_vegetacion_detecta_caida(monkeypatch):
    async def fake_query_t0(lat, lon, start, end, cloud):
        return {"ndvi": 0.75}

    async def fake_query_t1(lat, lon, start, end, cloud):
        return {"ndvi": 0.40}

    calls = {"n": 0}

    async def fake_query(lat, lon, start, end, cloud=40):
        calls["n"] += 1
        if calls["n"] == 1:
            return await fake_query_t0(lat, lon, start, end, cloud)
        return await fake_query_t1(lat, lon, start, end, cloud)

    from app.clientes import gee

    monkeypatch.setattr(gee, "query_point_index", fake_query)

    resultado = await cambio_vegetacion.detectar_cambio_punto(
        -39.6, -72.3, "2026-07-01", "2026-07-15", "2026-09-01", "2026-09-15"
    )
    assert resultado.ndvi_t0 == 0.75
    assert resultado.ndvi_t1 == 0.40
    assert resultado.delta == pytest.approx(-0.35)
    assert resultado.cambio_detectado is True


@pytest.mark.asyncio
async def test_cambio_vegetacion_sin_cambio_significativo(monkeypatch):
    async def fake_query(lat, lon, start, end, cloud=40):
        return {"ndvi": 0.70}

    from app.clientes import gee

    monkeypatch.setattr(gee, "query_point_index", fake_query)

    resultado = await cambio_vegetacion.detectar_cambio_punto(
        -39.6, -72.3, "2026-07-01", "2026-07-15", "2026-09-01", "2026-09-15"
    )
    assert resultado.cambio_detectado is False


def test_es_uso_forestal():
    from app.clientes import ciren

    assert ciren.es_uso_forestal("Plantación", "Plantación Adulta") is True
    assert ciren.es_uso_forestal("Agrícola", "Cultivo anual") is False
    assert ciren.es_uso_forestal(None, None) is False


def _ficha(**extra):
    base = {"radio_km": 3.0, "saff": {"estado": "ok"}, "sigcra": {"estado": "ok"}, "planes": [], "dentro_de_rodal": None}
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


def test_estado_sigcra_caido_con_planes_es_sin_datos():
    ficha = _ficha(planes=[{"Nro.Solicitud": "1"}], sigcra={"estado": "sin_datos"})
    assert lista_negra.evaluar_ficha(ficha).estado == lista_negra.ESTADO_SIN_DATOS


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


@pytest.mark.asyncio
async def test_orquestador_sin_ndvi_es_sin_datos_y_no_cachea(monkeypatch):
    from app.clientes import gee
    from app.logica import orquestador

    async def fake_query(lat, lon, start, end, cloud=40):
        raise gee.GeeError("gee_unreachable")

    monkeypatch.setattr(gee, "query_point_index", fake_query)
    orquestador._cache.clear()
    resultado = await orquestador.ejecutar_deteccion("panguipulli", "2026-07-01", "2026-09-01")
    assert {r["lista_negra"]["estado"] for r in resultado["resultados"]} == {"sin_datos"}
    assert orquestador._cache == {}


@pytest.mark.asyncio
async def test_orquestador_cambio_forestal_usa_ficha(monkeypatch):
    from app.clientes import gee
    from app.logica import ficha_punto, orquestador

    valores = iter([0.8, 0.3] * 3)

    async def fake_query(lat, lon, start, end, cloud=40):
        return {"ndvi": next(valores)}

    async def fake_clasificar(lat, lon):
        return mascara_bosque.ClasificacionUso("Bosques", "Bosque Nativo", True)

    async def fake_ficha(comuna, centro, lat, lon, radio_km, t1, esperar_saff=True):
        assert comuna == "PANGUIPULLI" and radio_km == 3.0
        return {"radio_km": radio_km, "saff": {"estado": "ok"}, "sigcra": {"estado": "no_consultado"},
                "planes": [], "dentro_de_rodal": None}

    monkeypatch.setattr(gee, "query_point_index", fake_query)
    monkeypatch.setattr(mascara_bosque, "clasificar_punto", fake_clasificar)
    monkeypatch.setattr(ficha_punto, "ficha_punto", fake_ficha)
    orquestador._cache.clear()
    resultado = await orquestador.ejecutar_deteccion("panguipulli", "2026-07-01", "2026-09-01")
    assert resultado["alertas"] == 3
    orquestador._cache.clear()
