"""Tests de UTM->WGS84, point-in-polygon, filtro de datos personales y ficha del punto."""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.clientes import saff, sigcra
from app.logica import ficha_punto, geo

CENTRO_PANGUIPULLI = (-39.64, -72.2)


def test_utm_a_wgs84_punto_conocido_huso_18():
    # Referencia: pyproj EPSG:4326 -> EPSG:32718 para (-39.715, -72.145)
    # = E 744730.860, N 5599976.411.
    lat, lon = geo.utm_a_wgs84(744730.860, 5599976.411, 18)
    assert lat == pytest.approx(-39.715, abs=1e-6)
    assert lon == pytest.approx(-72.145, abs=1e-6)


def test_utm_a_wgs84_punto_conocido_huso_19():
    # Referencia: pyproj EPSG:4326 -> EPSG:32719 para Plaza de Armas de Santiago
    # (-33.4378, -70.6504) = E 346584.032, N 6298959.054.
    lat, lon = geo.utm_a_wgs84(346584.032, 6298959.054, 19)
    assert lat == pytest.approx(-33.4378, abs=1e-6)
    assert lon == pytest.approx(-70.6504, abs=1e-6)


def test_utm_meridiano_central_y_ecuador():
    lat, lon = geo.utm_a_wgs84(500000.0, 10000000.0, 18)
    assert lat == pytest.approx(0.0, abs=1e-9)
    assert lon == pytest.approx(-75.0, abs=1e-9)


def test_ubicar_plan_infiere_huso_18_en_los_rios():
    # Sin huso en DATUM01, el huso 19 cae en lon -66.1 (dentro de la caja de
    # Chile) pero lejos de Panguipulli: debe ganar el 18.
    plan = {"Este01": "744730,86", "Norte01": 5599976.411, "DATUM01": "WGS84"}
    ubic = ficha_punto.ubicar_plan(plan, CENTRO_PANGUIPULLI)
    assert ubic["huso"] == 18
    assert ubic["huso_inferido"] is True
    assert ubic["aproximada"] is False
    assert ubic["lat"] == pytest.approx(-39.715, abs=1e-5)
    assert ubic["lon"] == pytest.approx(-72.145, abs=1e-5)


def test_ubicar_plan_datum_no_wgs_es_aproximada_y_huso_declarado():
    plan = {"Este01": 230405.906, "Norte01": 5599143.809, "DATUM01": "PSAD56 H19"}
    ubic = ficha_punto.ubicar_plan(plan, CENTRO_PANGUIPULLI)
    assert ubic["huso"] == 19
    assert ubic["huso_inferido"] is False
    assert ubic["aproximada"] is True


def test_ubicar_plan_sin_coordenadas():
    assert ficha_punto.ubicar_plan({"Este01": None, "Norte01": None}, CENTRO_PANGUIPULLI) is None
    assert ficha_punto.ubicar_plan({"Este01": 12, "Norte01": 34}, CENTRO_PANGUIPULLI) is None


CUADRADO = {
    "type": "Polygon",
    "coordinates": [
        [[-72.2, -39.8], [-72.1, -39.8], [-72.1, -39.7], [-72.2, -39.7], [-72.2, -39.8]],
        [[-72.16, -39.76], [-72.14, -39.76], [-72.14, -39.74], [-72.16, -39.74], [-72.16, -39.76]],
    ],
}


def test_punto_en_poligono_con_hoyo():
    assert geo.punto_en_geometria(-72.18, -39.72, CUADRADO) is True
    assert geo.punto_en_geometria(-72.15, -39.75, CUADRADO) is False  # en el hoyo
    assert geo.punto_en_geometria(-72.05, -39.75, CUADRADO) is False  # fuera


def test_punto_en_multipoligono():
    multi = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]], CUADRADO["coordinates"]]}
    assert geo.punto_en_geometria(0.5, 0.5, multi) is True
    assert geo.punto_en_geometria(-72.18, -39.72, multi) is True
    assert geo.punto_en_geometria(5, 5, multi) is False
    assert geo.punto_en_geometria(0.5, 0.5, None) is False


ITEM_SAFF_CON_PII = {
    "Soli_numero": 123,
    "Region Of": "LOS RIOS",
    "Oficina": "PANGUIPULLI",
    "Nro.Solicitud": "494/32-74/18",
    "Tipo Solicitud Compl": "PLAN DE MANEJO",
    "Estdo Solicitud": "APROBADA",
    "Fec.Ingreso": "2018-03-01T00:00:00",
    "Nro.Resolucion": "77",
    "Fec.Resolucion": "2018-05-01T00:00:00",
    "Super.Solicitada": 12.5,
    "Super.Aprobada": 10.0,
    "Cod. Predio": 999,
    "Nombre Predio": "FUNDO EL ROBLE",
    "Rol Avaluo": "100-20",
    "Sup.Predial": 300.0,
    "Comuna_Predio": "Panguipulli",
    "Nombre Propietario": "JUAN PEREZ",
    "Rut Propietario": "11.111.111-1",
    "cant_prop": 1,
    "Tipo_prop": "NATURAL",
    "Sexo Propietario": "M",
    "Etnia": "NO",
    "Correo": "juan@example.com",
    "Nombre Conservador": "CBR PANGUIPULLI",
    "Pto.Referencia01": "ACCESO",
    "Norte01": 5599976.411,
    "Este01": 744730.86,
    "DATUM01": "WGS84",
}

PII = ("Nombre Propietario", "Rut Propietario", "cant_prop", "Tipo_prop", "Sexo Propietario", "Etnia", "Correo")


def test_filtro_pii_saff():
    limpio = saff.sin_datos_personales(ITEM_SAFF_CON_PII)
    for campo in PII:
        assert campo not in limpio
    assert "Nombre Conservador" not in limpio
    assert limpio["Nombre Predio"] == "FUNDO EL ROBLE"
    assert limpio["Nro.Solicitud"] == "494/32-74/18"
    assert set(limpio) <= set(saff.CAMPOS_PUBLICOS)


class _RespFalsa:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _limpiar_cache():
    ficha_punto._cache_saff.clear()
    yield
    ficha_punto._cache_saff.clear()


@pytest.mark.asyncio
async def test_ficha_punto_cruce_rodal_y_sin_pii(monkeypatch):
    async def fake_get(self, url, params=None, headers=None):
        otro = {**ITEM_SAFF_CON_PII, "Comuna_Predio": "LANCO", "Nro.Solicitud": "1/1/1"}
        items = [ITEM_SAFF_CON_PII, otro] if params["offset"] == 0 and params["fecha_hasta"] == date.today().isoformat() else []
        return _RespFalsa({"total": len(items), "count": len(items), "items": items})

    async def fake_rodales(nro, limit=200):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": CUADRADO, "properties": {"nro_solicitud": nro, "numero_rodalet": 4}},
        ]}

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    monkeypatch.setattr(sigcra, "rodales_por_solicitud", fake_rodales)

    import datetime

    ficha = await ficha_punto.ficha_punto("PANGUIPULLI", CENTRO_PANGUIPULLI, -39.715, -72.15, 3.0, datetime.date(2026, 9, 1))
    assert ficha["saff"]["estado"] == "ok"
    assert len(ficha["planes"]) == 1  # el de LANCO se filtra por comuna
    assert ficha["dentro_de_rodal"]["rodal"] == "4"
    assert ficha["dentro_de_rodal"]["aprobado_a_t1"] is True
    assert "rodal 4 del plan 494/32-74/18" in ficha["conclusion"]
    texto = json.dumps(ficha, ensure_ascii=False)
    for valor in ("JUAN PEREZ", "11.111.111-1", "juan@example.com", "Rut Propietario", "Correo"):
        assert valor not in texto


@pytest.mark.asyncio
async def test_ficha_punto_saff_caido_no_lanza(monkeypatch):
    async def fake_historico(*a, **k):
        raise saff.SaffError("saff_unreachable: ConnectError")

    monkeypatch.setattr(saff, "historico_pm", fake_historico)
    ficha = await ficha_punto.ficha_punto("PANGUIPULLI", CENTRO_PANGUIPULLI, -39.72, -72.18)
    assert ficha["saff"]["estado"] == "sin_datos"
    assert ficha["planes"] == []
    assert ficha["conclusion"].startswith("Sin datos por el momento")
