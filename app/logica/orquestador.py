"""Orquesta la deteccion por poligonos: bosque CIREN -> NDVI zonal (GEE) -> predio CIREN -> estado SAFF.

1. Poligonos de bosque CIREN de la comuna (zonas_ciren, cache 7 dias).
2. NDVI medio t0/t1 por poligono en per_api_gee /v1/zonal/ndvi-change, en
   lotes de <= 1000 con concurrencia 3, con mascara de nubes por pixel
   Cloud Score+ (`mascara='cs_plus'`). Si menos de la mitad del poligono
   queda sin nubes en t0 o t1, GEE devuelve `motivo='nubes'`: el poligono no
   se marca con cambio y se cuenta aparte (sin imagen limpia). Tarda: corre
   como tarea en segundo plano y el endpoint responde de inmediato con el
   avance.
3. Poligonos con caida de NDVI (cambio_vegetacion) -> predio CIREN que
   contiene el centroide (predios_ciren, cache 7 dias) -> estado con los
   planes SAFF ya cargados en memoria: por rol y, de respaldo, por cercania
   (lista_negra.evaluar). El cruce con rodales SIGCRA se hace al abrir la
   ficha del poligono.
4. Para dibujar, los poligonos con cambio llevan su geometria exacta CIREN
   (zonas_ciren.geometrias_exactas); si CIREN no la entrega, la simplificada
   con `geometria: 'simplificada'`.

El NDVI se cachea 6 h por (region, t0, t1, cloud, min_ha); predio y estado
SAFF se recalculan en cada respuesta (es barato) para recoger las cargas
apenas terminan.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.clientes import gee
from app.logica import cambio_vegetacion, ficha_punto, lista_negra, predios_ciren, rol, zonas_ciren

CACHE_TTL_S = 6 * 3600
# Tope de CLOUDY_PIXEL_PERCENTAGE por escena: con la mascara por pixel una
# escena nublada en su conjunto aun aporta los pixeles despejados.
CLOUD_ESCENA_DEFECTO = 80
CACHE_FALLA_TTL_S = 120  # si GEE fallo: lo justo para que el polling lea el resultado
TAMANO_LOTE = 1000
CONCURRENCIA_LOTES = 3
RADIO_PLANES_KM = 3.0
SIN_DATOS = "Sin datos por el momento"

# Regiones de deteccion: la comuna define los poligonos CIREN y el filtro SAFF.
# - comuna: filtro de nom_com en CIREN y de Comuna_Predio en SAFF.
# - comudere: codigo SUBDERE de la comuna, filtro de los predios CIREN.
# - ciren_layer: capa del MapServer CIREN USOS_DE_LA_TIERRA__CONAF (12 = Los Rios, CONAF 2024).
# - predios_layer: capa de IDEMINAGRI/PROPIEDADES_RURALES (11 = Los Rios, CIREN 2018).
# - bbox: [oeste, sur, este, norte] de la comuna, para teselas Sentinel-2.
REGIONES = {
    "panguipulli": {
        "nombre": "Panguipulli",
        "comuna": "PANGUIPULLI",
        "ciren_layer": 12,
        "ciren_anio": 2024,
        "comudere": "14108",
        "predios_layer": 11,
        "predios_anio": 2018,
        "centro": (-39.64, -72.2),
        "bbox": (-72.75, -39.95, -71.55, -39.30),
    }
}


def rango(fecha_iso: str, dias_ventana: int = 15) -> tuple[str, str]:
    """Convierte una fecha puntual en una ventana [fecha, fecha+dias] para
    darle a Sentinel-2 margen de encontrar una imagen sin nubes."""
    base = date.fromisoformat(fecha_iso)
    fin = base + timedelta(days=dias_ventana)
    return base.isoformat(), fin.isoformat()


@dataclass
class Trabajo:
    region: str
    t0: str
    t1: str
    cloud: int
    min_ha: float
    estado: str = "calculando"
    fase: str = "Cargando polígonos de bosque CIREN"
    lotes_total: int = 0
    lotes_listos: int = 0
    iniciado_en: float = field(default_factory=time.time)
    terminado_en: float | None = None
    ttl_s: float = CACHE_TTL_S
    ndvi: dict | None = None
    tarea: asyncio.Task | None = None

    def vigente(self) -> bool:
        return self.terminado_en is None or time.time() - self.terminado_en < self.ttl_s


_trabajos: dict[tuple, Trabajo] = {}


def _fc(zonas: list[dict]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {"id": z["id"]}, "geometry": z["geometry"]} for z in zonas],
    }


async def _calcular(t: Trabajo, config: dict) -> None:
    """NDVI zonal de todos los poligonos; deja en t.ndvi lo necesario para responder."""
    try:
        predios_ciren.precargar_predios(t.region, config)
        try:
            carga = await zonas_ciren.cargar_zonas(t.region, config)
        except zonas_ciren.ZonasError as exc:
            t.ndvi = {"fuente_caida": "ciren", "error": str(exc)}
            return
        zonas = zonas_ciren.filtrar(carga["zonas"], t.min_ha)
        # Orden oeste-este: cada lote cubre una franja y GEE filtra menos escenas.
        zonas.sort(key=lambda z: z["centroide"]["lon"])
        lotes = [zonas[i:i + TAMANO_LOTE] for i in range(0, len(zonas), TAMANO_LOTE)]
        t.lotes_total = len(lotes)
        t.fase = "Calculando NDVI Sentinel-2 por polígono"
        sem = asyncio.Semaphore(CONCURRENCIA_LOTES)
        v0, v1 = rango(t.t0), rango(t.t1)

        async def lote(zs: list[dict]) -> dict:
            async with sem:
                try:
                    return await gee.zonal_ndvi_change(_fc(zs), v0, v1, t.cloud)
                except gee.GeeError as exc:
                    return {"error": str(exc).split(":")[0]}
                finally:
                    t.lotes_listos += 1

        respuestas = await asyncio.gather(*(lote(zs) for zs in lotes))
        valores: dict = {}
        cobertura: dict = {}
        nubes: set = set()
        errores = []
        imagenes = {"t0": 0, "t1": 0}
        for resp in respuestas:
            if "error" in resp:
                errores.append(resp["error"])
                continue
            for k in imagenes:
                imagenes[k] = max(imagenes[k], (resp.get("images") or {}).get(k) or 0)
            for it in resp.get("items") or []:
                valores[it.get("id")] = (it.get("ndvi_t0"), it.get("ndvi_t1"))
                cobertura[it.get("id")] = (it.get("cobertura_t0"), it.get("cobertura_t1"))
                if it.get("motivo") == "nubes":
                    nubes.add(it.get("id"))
        # Geometria exacta solo para dibujar los poligonos con cambio (la
        # simplificada de GEE deja bordes vecinos cruzados y esquinas en punta).
        con_cambio = [
            zid for zid, (n0, n1) in valores.items()
            if (d := cambio_vegetacion.delta(n0, n1)) is not None and cambio_vegetacion.hay_cambio(d)
        ]
        exactas = {}
        if con_cambio:
            t.fase = "Descargando contornos exactos CIREN"
            try:
                exactas = await zonas_ciren.geometrias_exactas(t.region, config, con_cambio)
            except Exception:  # noqa: BLE001 - sin contorno exacto se dibuja la simplificada
                exactas = {}
        t.ndvi = {
            "zonas": zonas,
            "valores": valores,
            "cobertura": cobertura,
            "nubes": nubes,
            "exactas": exactas,
            "errores": errores,
            "lotes": len(lotes),
            "imagenes": imagenes,
            "ventanas": {"t0": {"inicio": v0[0], "fin": v0[1]}, "t1": {"inicio": v1[0], "fin": v1[1]}},
            "capa": {"id": carga["capa"], "anio": carga.get("anio")},
            "poligonos_capa": len(carga["zonas"]),
        }
        if errores:
            t.ttl_s = CACHE_FALLA_TTL_S
    except Exception as exc:  # noqa: BLE001 - un trabajo nunca queda colgado en "calculando"
        t.ndvi = {"fuente_caida": "interno", "error": type(exc).__name__}
    finally:
        if t.ndvi is None or "fuente_caida" in t.ndvi:
            t.ttl_s = CACHE_FALLA_TTL_S
        t.estado = "listo"
        t.terminado_en = time.time()


def _base(t: Trabajo, config: dict) -> dict:
    return {
        "region": t.region,
        "region_nombre": config["nombre"],
        "t0": t.t0,
        "t1": t.t1,
        "cloud": t.cloud,
        "min_ha": t.min_ha,
        "indice": "ndvi",
        "mascara_nubes": "cs_plus",
        "umbral_delta": -cambio_vegetacion.UMBRAL_CAIDA_NDVI,
    }


def _respuesta(t: Trabajo, config: dict) -> dict:
    """Arma la respuesta final; el estado SAFF se evalua aqui, con la carga en memoria."""
    base = _base(t, config)
    n = t.ndvi or {}
    if "fuente_caida" in n:
        fuente = "CIREN" if n["fuente_caida"] == "ciren" else "el cálculo"
        return {**base, "estado": "sin_datos", "mensaje": f"{SIN_DATOS}: no respondió {fuente}.", "error": n.get("error"),
                "resumen": None, "poligonos": []}

    zonas, valores, errores = n["zonas"], n["valores"], n["errores"]
    if errores and len(errores) == n["lotes"]:
        sin_img = all(e == "gee_sin_imagenes" for e in errores)
        mensaje = "Sin imagen Sentinel-2 en la ventana de t0 o t1." if sin_img else f"{SIN_DATOS}: per_api_gee no respondió."
        gee_estado = {"estado": "sin_datos", "lotes": n["lotes"], "lotes_fallidos": len(errores), "error": errores[0]}
    else:
        mensaje = None
        gee_estado = {"estado": "parcial" if errores else "ok", "lotes": n["lotes"], "lotes_fallidos": len(errores)}

    puntos = ficha_punto.planes_ubicados_en_cache(config["comuna"], config["centro"])
    indice_rol = ficha_punto.indice_rol_en_cache(config["comuna"])
    if puntos is None:
        ficha_punto.precargar_saff(config["comuna"])
    predios = predios_ciren.predios_en_cache(t.region)
    if predios is None:
        predios_ciren.precargar_predios(t.region, config)
    t1 = date.fromisoformat(t.t1)

    con_cambio = []
    sin_ndvi = 0
    sin_imagen_limpia = 0
    nubes, cobertura = n.get("nubes") or set(), n.get("cobertura") or {}
    for z in zonas:
        if z["id"] in nubes:
            sin_imagen_limpia += 1
            continue
        n0, n1 = valores.get(z["id"], (None, None))
        d = cambio_vegetacion.delta(n0, n1)
        if d is None:
            sin_ndvi += 1
            continue
        if not cambio_vegetacion.hay_cambio(d):
            continue
        c = z["centroide"]
        cercanos = (
            ficha_punto.contar_cercanos(puntos, c["lat"], c["lon"], RADIO_PLANES_KM) if puntos is not None else None
        )
        predio = predios_ciren.info_predio(
            predios_ciren.predio_en_punto(predios, c["lat"], c["lon"]) if predios else None, predios,
        )
        planes_rol = [
            {**p, "aprobado_a_t1": ficha_punto.plan_aprobado(p, t1)}
            for p in rol.planes_del_predio(indice_rol or {}, predio if predio["estado"] == "ok" else None)
        ]
        ev = lista_negra.evaluar(puntos is not None, predio, planes_rol, cercanos or 0, RADIO_PLANES_KM)
        exacta = n.get("exactas", {}).get(z["id"])
        c0, c1 = cobertura.get(z["id"], (None, None))
        con_cambio.append({
            "id": z["id"],
            "uso": z.get("uso"),
            "subuso": z.get("subuso"),
            "superf_ha": z.get("superf_ha"),
            "ndvi_t0": n0,
            "ndvi_t1": n1,
            "delta": round(d, 4),
            "cobertura_t0": c0,
            "cobertura_t1": c1,
            "centroide": c,
            "estado": ev.estado,
            "motivo": ev.motivo,
            "planes_cercanos": cercanos,
            "predio": predio,
            "planes_rol": len(planes_rol) if puntos is not None else None,
            "geometry": exacta or z["geometry"],
            "geometria": "exacta" if exacta else "simplificada",
        })
    con_cambio.sort(key=lambda p: p["delta"])

    def contar(estado: str) -> int:
        return sum(1 for p in con_cambio if p["estado"] == estado)

    ha_evaluadas = sum(z.get("superf_ha") or 0 for z in zonas)
    resumen = {
        "poligonos_evaluados": len(zonas),
        "ha_evaluadas": round(ha_evaluadas, 1),
        "poligonos_capa": n["poligonos_capa"],
        "con_ndvi": len(zonas) - sin_ndvi - sin_imagen_limpia,
        "sin_ndvi": sin_ndvi,
        "sin_imagen_limpia": sin_imagen_limpia,
        "con_cambio": len(con_cambio),
        "ha_con_cambio": round(sum(p.get("superf_ha") or 0 for p in con_cambio), 1),
        "alertas": contar(lista_negra.ESTADO_ALERTA),
        "en_regla": contar(lista_negra.ESTADO_EN_REGLA),
        "revisar": contar(lista_negra.ESTADO_REVISAR),
        "sin_datos": contar(lista_negra.ESTADO_SIN_DATOS),
    }
    return {
        **base,
        "estado": "sin_datos" if gee_estado["estado"] == "sin_datos" else "listo",
        "mensaje": mensaje,
        "capa_ciren": n["capa"],
        "ventanas": n["ventanas"],
        "imagenes": n["imagenes"],
        "gee": gee_estado,
        "saff": {"estado": "ok" if puntos is not None else "sin_datos", "planes_ubicados": len(puntos or [])},
        "predios": (
            {"estado": "ok", "capa": predios["capa"], "anio": predios.get("anio"), "predios": len(predios["predios"])}
            if predios else {"estado": "sin_datos"}
        ),
        "radio_planes_km": RADIO_PLANES_KM,
        "resumen": resumen,
        "poligonos": con_cambio,
    }


def _progreso(t: Trabajo) -> dict:
    avance = f"{t.lotes_listos}/{t.lotes_total} lotes" if t.lotes_total else "preparando"
    return {
        "region": t.region, "t0": t.t0, "t1": t.t1, "estado": "calculando",
        "fase": t.fase, "avance": avance, "lotes_listos": t.lotes_listos, "lotes_total": t.lotes_total,
        "segundos": round(time.time() - t.iniciado_en),
    }


async def deteccion(region: str, t0: str, t1: str, cloud: int = CLOUD_ESCENA_DEFECTO, min_ha: float = zonas_ciren.MIN_HA_DEFECTO) -> dict:
    """Devuelve el resultado si esta listo; si no, lanza (o sigue) el calculo y entrega el avance."""
    config = REGIONES.get(region)
    if not config:
        return {"region": region, "estado": "error", "error": "region_no_configurada", "regiones_disponibles": list(REGIONES)}
    for clave in [k for k, v in _trabajos.items() if not v.vigente()]:
        del _trabajos[clave]
    clave = (region, t0, t1, cloud, min_ha)
    t = _trabajos.get(clave)
    if t is None:
        t = Trabajo(region, t0, t1, cloud, min_ha)
        _trabajos[clave] = t
        t.tarea = asyncio.create_task(_calcular(t, config))
        await asyncio.sleep(0)
    if t.estado == "calculando":
        return _progreso(t)
    return {**_respuesta(t, config), "calculado_en": t.terminado_en}


def info_regiones() -> list[dict]:
    salida = []
    for clave, cfg in REGIONES.items():
        carga = zonas_ciren.zonas_en_cache(clave)
        salida.append({
            "id": clave,
            "nombre": cfg["nombre"],
            "comuna": cfg["comuna"],
            "ciren_layer": cfg["ciren_layer"],
            "ciren_anio": cfg["ciren_anio"],
            "predios_layer": cfg["predios_layer"],
            "predios_anio": cfg["predios_anio"],
            "centro": {"lat": cfg["centro"][0], "lon": cfg["centro"][1]},
            "bbox": list(cfg["bbox"]),
            "poligonos_ciren": len(carga["zonas"]) if carga else None,
            "min_ha": zonas_ciren.MIN_HA_DEFECTO,
        })
    return salida
