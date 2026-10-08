"""Tests del cruce por rol: normalizacion, regla de estado, predios CIREN y ficha."""
from __future__ import annotations

import datetime

import pytest

from app.clientes import ciren, sigcra
from app.logica import ficha_punto, lista_negra, predios_ciren, rol


@pytest.mark.parametrize("texto, esperado", [
    ("209-58", "209-58"),
    ("00209-00058", "209-58"),
    ("209/58", "209-58"),
    (" 209 - 058 ", "209-58"),
    ("Rol 209-58", "209-58"),
    ("14108-209-58", "209-58"),
    ("1234-5", "1234-5"),
    ("SR", None),
    ("RUTA 203", None),
    ("T-203", None),
    ("", None),
    (None, None),
])
def test_normalizar_rol(texto, esperado):
    assert rol.normalizar_rol(texto) == esperado


def test_roles_en_texto_varios_y_codigo_comuna():
    assert rol.roles_en_texto("209-58; 209-59 y 00209-00058") == [("209-58", None), ("209-59", None)]
    assert rol.roles_en_texto("14108-00209-00058") == [("209-58", "14108")]
    assert rol.roles_en_texto("209-58 / 209-59") == [("209-58", None), ("209-59", None)]


PREDIO = {"estado": "ok", "rol": "209-58", "rol_norm": "209-58", "comuna": "PANGUIPULLI", "comudere": "14108"}


def test_planes_del_predio_compara_rol_codigo_y_comuna():
    planes = [
        {"Nro.Solicitud": "A", "Rol Avaluo": "00209-00058", "Comuna_Predio": "Panguipulli"},
        {"Nro.Solicitud": "B", "Rol Avaluo": "14108-209-58", "Comuna_Predio": "PANGUIPULLI"},
        {"Nro.Solicitud": "C", "Rol Avaluo": "14107-209-58", "Comuna_Predio": "PANGUIPULLI"},  # otro codigo
        {"Nro.Solicitud": "D", "Rol Avaluo": "209-58", "Comuna_Predio": "LANCO"},  # otra comuna
        {"Nro.Solicitud": "E", "Rol Avaluo": "209-59", "Comuna_Predio": "PANGUIPULLI"},
        {"Nro.Solicitud": "F", "Rol Avaluo": "209-58; 209-58", "Comuna_Predio": "Pangüipulli"},  # tilde, repetido
    ]
    encontrados = rol.planes_del_predio(rol.indexar_planes(planes), PREDIO)
    assert [p["Nro.Solicitud"] for p in encontrados] == ["A", "B", "F"]
    assert rol.planes_del_predio(rol.indexar_planes(planes), {**PREDIO, "rol_norm": None}) == []
    assert rol.planes_del_predio(rol.indexar_planes(planes), None) == []


APROBADO = {"Nro.Solicitud": "77/14/2025", "aprobado_a_t1": True}
NO_APROBADO = {"Nro.Solicitud": "80/14/2026", "aprobado_a_t1": False}


@pytest.mark.parametrize("args, kwargs, estado, motivo", [
    # 1. Plan aprobado con el mismo rol.
    ((True, PREDIO, [NO_APROBADO, APROBADO], 0, 3.0), {}, "en_regla", "Plan 77/14/2025 aprobado para el rol 209-58"),
    # 2. Planes del rol sin aprobar a t1 (aunque haya cercanos).
    ((True, PREDIO, [NO_APROBADO], 4, 3.0), {}, "revisar", "1 plan(es) SAFF para el rol 209-58"),
    # 3. Sin plan del rol, con planes cercanos.
    ((True, PREDIO, [], 2, 3.0), {}, "revisar", "Sin plan para el rol 209-58; hay 2 plan(es) a menos de 3 km"),
    # 3b. Predio sin rol SII: solo cercania.
    ((True, {**PREDIO, "rol": "SR", "rol_norm": None}, [], 1, 3.0), {}, "revisar", "Predio sin rol SII (SR)"),
    # 4. Ninguno.
    ((True, PREDIO, [], 0, 3.0), {}, "alerta", "Sin plan para el rol 209-58 ni planes"),
    # 5. Sin predio en el centroide / predios caidos / SAFF caido.
    ((True, {"estado": "sin_predio"}, [], 3, 3.0), {}, "sin_datos", "Sin predio CIREN en el centroide"),
    ((True, {"estado": "sin_datos"}, [], 3, 3.0), {}, "sin_datos", "Predios CIREN sin datos"),
    ((True, None, [], 0, 3.0), {}, "sin_datos", "Predios CIREN sin datos"),
    ((False, PREDIO, [APROBADO], 0, 3.0), {}, "sin_datos", "SAFF sin datos"),
    # SIGCRA: dentro de rodal aprobado es en_regla aunque no haya predio.
    ((True, {"estado": "sin_predio"}, [], 1, 3.0),
     {"dentro": {"nro_solicitud": "9", "rodal": "2", "aprobado_a_t1": True}}, "en_regla", "Dentro del rodal 2 del plan 9"),
    # Dentro de rodal no aprobado: no cambia la regla por rol/cercania.
    ((True, PREDIO, [], 1, 3.0),
     {"dentro": {"nro_solicitud": "9", "rodal": "2", "aprobado_a_t1": False}}, "revisar", "dentro del rodal 2 del plan 9"),
])
def test_regla_de_estado(args, kwargs, estado, motivo):
    r = lista_negra.evaluar(*args, **kwargs)
    assert r.estado == estado
    assert motivo in r.motivo


def test_en_regla_por_rol_informa_plan():
    r = lista_negra.evaluar(True, PREDIO, [APROBADO], 0, 3.0)
    assert r.plan_manejo == {"nro_solicitud": "77/14/2025", "rol": "209-58"}


def _cuadro(o, s, e, n):
    return {"type": "Polygon", "coordinates": [[[o, s], [e, s], [e, n], [o, n], [o, s]]]}


def _feature(oid, rol_txt, geom):
    return {"type": "Feature", "properties": {"objectid": oid, "rol": rol_txt, "desccomu": "PANGUIPULLI", "comudere": "14108"},
            "geometry": geom}


FEATURES_PREDIOS = [
    _feature(1, "209-58", _cuadro(-72.15, -39.72, -72.14, -39.71)),
    _feature(2, "209-59", _cuadro(-72.14, -39.72, -72.10, -39.71)),
    _feature(3, "SR", _cuadro(-72.30, -39.80, -72.20, -39.75)),
    # Predio grande que se traslapa con el 1 por la simplificacion: gana el mas chico.
    _feature(4, "300-1", _cuadro(-72.16, -39.73, -72.13, -39.70)),
    _feature(5, "1-1", {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 0]]]}),  # degenerado
]


@pytest.mark.asyncio
async def test_predios_carga_cache_disco_y_pip(monkeypatch, tmp_path):
    llamadas = []

    async def fake_query(layer_id, where, out_fields, **kw):
        llamadas.append((layer_id, where, kw.get("base_url")))
        return FEATURES_PREDIOS

    monkeypatch.setattr(ciren, "query_poligonos", fake_query)
    monkeypatch.setattr(predios_ciren, "CACHE_DIR", tmp_path)
    predios_ciren._cache.clear()
    cfg = {"comuna": "PANGUIPULLI", "comudere": "14108", "predios_layer": 11, "predios_anio": 2018}
    carga = await predios_ciren.cargar_predios("panguipulli", cfg)
    assert llamadas == [(11, "comudere='14108'", ciren.PREDIOS_BASE_URL)]
    assert [p["id"] for p in carga["predios"]] == [1, 2, 3, 4]
    assert predios_ciren.resumen(carga)["predios"] == 4
    assert predios_ciren.resumen(carga)["con_rol"] == 3

    assert predios_ciren.predio_en_punto(carga, -39.715, -72.145)["rol_norm"] == "209-58"
    assert predios_ciren.predio_en_punto(carga, -39.715, -72.12)["rol"] == "209-59"
    assert predios_ciren.predio_en_punto(carga, -39.705, -72.155)["rol"] == "300-1"
    assert predios_ciren.predio_en_punto(carga, -39.0, -72.0) is None
    sin_rol = predios_ciren.predio_en_punto(carga, -39.78, -72.25)
    assert sin_rol["rol"] == "SR" and sin_rol["rol_norm"] is None

    info = predios_ciren.info_predio(predios_ciren.predio_en_punto(carga, -39.715, -72.145), carga)
    assert info == {"estado": "ok", "id": 1, "rol": "209-58", "rol_norm": "209-58", "comuna": "PANGUIPULLI",
                    "comudere": "14108", "capa": {"id": 11, "anio": 2018}}
    assert predios_ciren.info_predio(None, carga)["estado"] == "sin_predio"
    assert predios_ciren.info_predio(None, None) == {"estado": "sin_datos"}

    assert (tmp_path / "predios_panguipulli_capa11.json").exists()
    predios_ciren._cache.clear()
    await predios_ciren.cargar_predios("panguipulli", cfg)  # desde disco
    assert len(llamadas) == 1
    predios_ciren._cache.clear()


@pytest.mark.asyncio
async def test_buscar_predio_ciren_caido_sin_cache_es_sin_datos(monkeypatch):
    async def falla(*a, **k):
        raise ciren.CirenError("ciren_unreachable: ConnectError")

    monkeypatch.setattr(ciren, "query_predio_punto", falla)
    monkeypatch.setattr(predios_ciren, "precargar_predios", lambda region, config: None)
    predios_ciren._cache.clear()
    cfg = {"predios_layer": 11, "predios_anio": 2018}
    assert await predios_ciren.buscar_predio("panguipulli", cfg, -39.715, -72.145) == {"estado": "sin_datos"}


@pytest.mark.asyncio
async def test_buscar_predio_exacto_consulta_ciren(monkeypatch):
    async def punto(layer_id, lon, lat, geometria=True):
        return FEATURES_PREDIOS[0]

    monkeypatch.setattr(ciren, "query_predio_punto", punto)
    monkeypatch.setattr(predios_ciren, "precargar_predios", lambda region, config: None)
    predios_ciren._cache.clear()
    info = await predios_ciren.buscar_predio("panguipulli", {"predios_layer": 11, "predios_anio": 2018}, -39.715, -72.145, exacto=True)
    assert info["rol_norm"] == "209-58" and info["capa"] == {"id": 11, "anio": 2018}
    assert info["geometry"]["type"] == "Polygon"


@pytest.mark.asyncio
async def test_ficha_lista_planes_del_rol_primero(monkeypatch):
    # Plan del rol con punto de referencia lejano (> 3 km) y plan cercano de otro rol.
    del_rol = {"Nro.Solicitud": "77/14/2025", "Rol Avaluo": "00209-00058", "Comuna_Predio": "PANGUIPULLI",
               "Estdo Solicitud": "APROBADA", "Fec.Resolucion": "2025-03-01T00:00:00",
               "Este01": 760000, "Norte01": 5620000, "DATUM01": "WGS84 H18"}
    cercano = {"Nro.Solicitud": "5/14/2020", "Rol Avaluo": "209-59", "Comuna_Predio": "PANGUIPULLI",
               "Estdo Solicitud": "APROBADA", "Fec.Resolucion": "2020-01-01T00:00:00",
               "Este01": 744730.86, "Norte01": 5599976.411, "DATUM01": "WGS84"}
    ficha_punto._cache_saff[ficha_punto.normalizar("PANGUIPULLI")] = (
        __import__("time").time(),
        {"comuna": "PANGUIPULLI", "desde": "2016-01-01", "hasta": "2026-10-08", "planes": [del_rol, cercano],
         "truncado": False, "cargado_en": 123.0},
    )
    consultados = []

    async def fake_rodales(nro, limit=200):
        consultados.append(nro)
        return {"type": "FeatureCollection", "features": []}

    monkeypatch.setattr(sigcra, "rodales_por_solicitud", fake_rodales)
    try:
        ficha = await ficha_punto.ficha_punto(
            "PANGUIPULLI", (-39.64, -72.2), -39.715, -72.145, 3.0, datetime.date(2026, 9, 1), predio=PREDIO,
        )
    finally:
        ficha_punto._cache_saff.clear()
    assert [p["Nro.Solicitud"] for p in ficha["planes_rol"]] == ["77/14/2025"]
    assert ficha["planes_rol"][0]["mismo_rol"] is True
    assert [p["Nro.Solicitud"] for p in ficha["planes"]] == ["5/14/2020"]
    assert consultados == ["77/14/2025", "5/14/2020"]  # SIGCRA: primero el plan del rol
    assert ficha["conclusion"].startswith("Hay 1 plan SAFF para el rol 209-58.")
    estado = lista_negra.evaluar_ficha(ficha)
    assert estado.estado == "en_regla" and "rol 209-58" in estado.motivo
    assert ficha["predio"]["rol"] == "209-58"
