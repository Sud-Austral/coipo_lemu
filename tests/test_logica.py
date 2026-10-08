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


@pytest.mark.asyncio
async def test_evaluar_predio_sin_codigo_predio():
    resultado = await lista_negra.evaluar_predio(None, __import__("datetime").date(2026, 1, 1), __import__("datetime").date(2026, 12, 31))
    assert resultado.estado == lista_negra.ESTADO_SIN_DATOS


@pytest.mark.asyncio
async def test_evaluar_predio_encuentra_plan_vigente(monkeypatch):
    async def fake_historico(fecha_desde, fecha_hasta, limit=2000):
        return {"items": [{"Cod. Predio": "12345", "Nro.Solicitud": "478/32-74/21"}]}

    from app.clientes import saff

    monkeypatch.setattr(saff, "historico_pm", fake_historico)

    import datetime

    resultado = await lista_negra.evaluar_predio("12345", datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
    assert resultado.estado == lista_negra.ESTADO_EN_REGLA
    assert resultado.plan_manejo is not None


@pytest.mark.asyncio
async def test_evaluar_predio_sin_plan_es_alerta(monkeypatch):
    async def fake_historico(fecha_desde, fecha_hasta, limit=2000):
        return {"items": []}

    from app.clientes import saff

    monkeypatch.setattr(saff, "historico_pm", fake_historico)

    import datetime

    resultado = await lista_negra.evaluar_predio("99999", datetime.date(2026, 1, 1), datetime.date(2026, 12, 31))
    assert resultado.estado == lista_negra.ESTADO_ALERTA
