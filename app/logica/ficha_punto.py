"""Ficha de un punto: planes de manejo SAFF cercanos y sus rodales SIGCRA.

SAFF no tiene geometria del predio, solo un punto de referencia en UTM
(Norte01/Este01). La cercania se mide desde ese punto; el cruce fuerte es si
el punto de deteccion cae dentro de un rodal SIGCRA de alguno de los planes.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from datetime import date, timedelta

import httpx

from app.clientes import saff, sigcra
from app.logica import geo

NOTA_CERCANIA = "Cercanía por punto de referencia del predio declarado en SAFF (no por el polígono)."
SAFF_CACHE_TTL_S = 6 * 3600
SAFF_ANIOS = 10
SAFF_TRAMO_DIAS = 365  # tramos cortos para no pasar el timeout de Oracle ni el limit=5000
SAFF_LIMIT = 5000
SAFF_MAX_PAGINAS_TRAMO = 4
SAFF_CONCURRENCIA = 3
MAX_PLANES = 15
MAX_PLANES_SIGCRA = 5

_cache_saff: dict[str, tuple[float, dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


def normalizar(texto: str | None) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.upper().split())


def _numero(valor) -> float | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(str(valor).strip().replace(",", "."))
    except ValueError:
        return None


def _datum_confiable(datum: str | None) -> bool:
    d = normalizar(datum)
    return "WGS" in d or "SIRGAS" in d


def _huso_declarado(datum: str | None) -> int | None:
    m = re.search(r"(?<!\d)(18|19)(?!\d)", normalizar(datum))
    return int(m.group(1)) if m else None


def ubicar_plan(item: dict, centro: tuple[float, float]) -> dict | None:
    """Punto de referencia del predio en WGS84, o None si no es convertible.

    Si DATUM01 no trae el huso se prueban 18 y 19 y se elige el resultado que
    cae en Chile continental; si ambos caen (la caja es amplia), el mas
    cercano al centro de la region.
    """
    este = _numero(item.get("Este01"))
    norte = _numero(item.get("Norte01"))
    if este is None or norte is None or not (100_000 <= este <= 900_000) or not (1_000_000 <= norte <= 10_000_000):
        return None
    datum = item.get("DATUM01")
    huso = _huso_declarado(datum)
    candidatos = [huso] if huso else [18, 19]
    opciones = []
    for z in candidatos:
        lat, lon = geo.utm_a_wgs84(este, norte, z)
        if geo.en_chile(lat, lon):
            opciones.append((geo.haversine_km(lat, lon, centro[0], centro[1]), z, lat, lon))
    if not opciones:
        return None
    _, z, lat, lon = min(opciones)
    return {
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "huso": z,
        "huso_inferido": huso is None,
        "aproximada": not _datum_confiable(datum),
    }


def _tramos(hasta: date, anios: int, dias: int) -> list[tuple[date, date]]:
    desde_total = hasta - timedelta(days=365 * anios)
    tramos = []
    fin = hasta
    while fin > desde_total:
        ini = max(desde_total, fin - timedelta(days=dias - 1))
        tramos.append((ini, fin))
        fin = ini - timedelta(days=1)
    return tramos


async def _cargar_tramo(client: httpx.AsyncClient, sem: asyncio.Semaphore, ini: date, fin: date) -> tuple[list[dict], bool]:
    items: list[dict] = []
    async with sem:
        for pagina in range(SAFF_MAX_PAGINAS_TRAMO):
            data = await saff.historico_pm(ini, fin, limit=SAFF_LIMIT, offset=pagina * SAFF_LIMIT, client=client)
            lote = data["items"]
            items.extend(lote)
            if len(lote) < SAFF_LIMIT:
                return items, False
    return items, True


async def cargar_planes_comuna(comuna: str, hoy: date | None = None) -> dict:
    """Planes SAFF de los ultimos 10 anios en la comuna (cache 6 h).

    Lanza saff.SaffError si cualquier tramo falla: no se devuelven cargas
    parciales como si fueran completas.
    """
    clave = normalizar(comuna)
    ahora = time.time()
    guardado = _cache_saff.get(clave)
    if guardado and ahora - guardado[0] < SAFF_CACHE_TTL_S:
        return guardado[1]
    lock = _locks.setdefault(clave, asyncio.Lock())
    async with lock:
        guardado = _cache_saff.get(clave)
        if guardado and time.time() - guardado[0] < SAFF_CACHE_TTL_S:
            return guardado[1]
        hasta = hoy or date.today()
        tramos = _tramos(hasta, SAFF_ANIOS, SAFF_TRAMO_DIAS)
        sem = asyncio.Semaphore(SAFF_CONCURRENCIA)
        async with httpx.AsyncClient(timeout=saff.TIMEOUT_S) as client:
            resultados = await asyncio.gather(*(_cargar_tramo(client, sem, i, f) for i, f in tramos))
        planes = []
        truncado = False
        for items, trunc in resultados:
            truncado = truncado or trunc
            planes.extend(i for i in items if clave in normalizar(i.get("Comuna_Predio")))
        carga = {
            "comuna": clave,
            "desde": tramos[-1][0].isoformat(),
            "hasta": hasta.isoformat(),
            "planes": planes,
            "truncado": truncado,
            "cargado_en": time.time(),
        }
        _cache_saff[clave] = (time.time(), carga)
        return carga


def saff_en_cache(comuna: str) -> bool:
    guardado = _cache_saff.get(normalizar(comuna))
    return bool(guardado and time.time() - guardado[0] < SAFF_CACHE_TTL_S)


_ubicados: dict[str, tuple[float, list[tuple[float, float]]]] = {}


def planes_ubicados_en_cache(comuna: str, centro: tuple[float, float]) -> list[tuple[float, float]] | None:
    """(lat, lon) de los planes SAFF de la comuna ya cargados, o None si no hay carga.

    No consulta SAFF: sirve para el estado del listado de deteccion, que debe
    ser barato. La conversion UTM se hace una vez por carga.
    """
    clave = normalizar(comuna)
    guardado = _cache_saff.get(clave)
    if not guardado or time.time() - guardado[0] >= SAFF_CACHE_TTL_S:
        return None
    carga = guardado[1]
    previo = _ubicados.get(clave)
    if previo and previo[0] == carga["cargado_en"]:
        return previo[1]
    puntos = []
    for plan in carga["planes"]:
        ubic = ubicar_plan(plan, centro)
        if ubic:
            puntos.append((ubic["lat"], ubic["lon"]))
    _ubicados[clave] = (carga["cargado_en"], puntos)
    return puntos


def contar_cercanos(puntos: list[tuple[float, float]], lat: float, lon: float, radio_km: float) -> int:
    dlat = radio_km / 110.0
    dlon = radio_km / (111.32 * max(0.1, abs(math.cos(math.radians(lat)))))
    return sum(
        1 for plat, plon in puntos
        if abs(plat - lat) <= dlat and abs(plon - lon) <= dlon and geo.haversine_km(lat, lon, plat, plon) <= radio_km
    )


_tareas: dict[str, asyncio.Task] = {}


def precargar_saff(comuna: str) -> None:
    """Lanza la carga SAFF de la comuna en segundo plano (una a la vez)."""
    clave = normalizar(comuna)
    tarea = _tareas.get(clave)
    if tarea and not tarea.done():
        return

    async def _correr():
        try:
            await cargar_planes_comuna(comuna)
        except Exception:  # noqa: BLE001 - se reintenta en la proxima consulta
            pass

    _tareas[clave] = asyncio.create_task(_correr())


def plan_aprobado(plan: dict, t1: date | None) -> bool:
    """Resolucion aprobada con Fec.Resolucion <= t1 (si se entrega t1)."""
    estado = normalizar(plan.get("Estdo Solicitud"))
    sup = _numero(plan.get("Super.Aprobada")) or 0
    aprobado = "APROB" in estado or (bool(plan.get("Nro.Resolucion")) and sup > 0)
    if not aprobado:
        return False
    if t1 is None:
        return True
    fec = str(plan.get("Fec.Resolucion") or "")[:10]
    try:
        return date.fromisoformat(fec) <= t1
    except ValueError:
        return False


def _etiqueta_rodal(props: dict, indice: int) -> str:
    valor = props.get("numero_rodalet")
    return str(valor) if valor not in (None, "") else str(indice + 1)


async def _rodales(nro: str) -> dict:
    try:
        fc = await sigcra.rodales_por_solicitud(nro, limit=200)
        return {"estado": "ok", "fc": fc}
    except sigcra.SigcraError as exc:
        return {"estado": "sin_datos", "error": str(exc)}


def _conclusion(radio_km: float, planes: list[dict], dentro: dict | None, saff_ok: bool) -> str:
    if not saff_ok:
        return "Sin datos por el momento: SAFF no respondió."
    radio = f"{radio_km:g}".replace(".", ",")
    if not planes:
        return f"No hay planes de manejo registrados en SAFF a menos de {radio} km."
    n = len(planes)
    base = f"Hay {n} plan{'es' if n != 1 else ''} de manejo a menos de {radio} km"
    if dentro:
        return f"{base}; el punto cae dentro del rodal {dentro['rodal']} del plan {dentro['nro_solicitud']}."
    return f"{base}; el punto no cae dentro de los rodales SIGCRA consultados."


async def ficha_punto(
    comuna: str,
    centro: tuple[float, float],
    lat: float,
    lon: float,
    radio_km: float = 3.0,
    t1: date | None = None,
    esperar_saff: bool = True,
) -> dict:
    """Planes SAFF cercanos, rodales SIGCRA y cruce punto-en-rodal.

    Con esperar_saff=False no bloquea si la carga SAFF de la comuna aun no
    esta en cache: la lanza en segundo plano y responde sin_datos (cargando).
    """
    respuesta: dict = {
        "lat": lat,
        "lon": lon,
        "radio_km": radio_km,
        "nota_cercania": NOTA_CERCANIA,
        "saff": {"estado": "ok"},
        "sigcra": {"estado": "no_consultado"},
        "planes": [],
        "rodales": {"type": "FeatureCollection", "features": []},
        "dentro_de_rodal": None,
    }
    if not esperar_saff and not saff_en_cache(comuna):
        precargar_saff(comuna)
        respuesta["saff"] = {"estado": "sin_datos", "error": "saff_cargando"}
        respuesta["conclusion"] = "Sin datos por el momento: SAFF se está cargando, vuelve a intentar en unos minutos."
        return respuesta
    try:
        carga = await cargar_planes_comuna(comuna)
    except saff.SaffError as exc:
        respuesta["saff"] = {"estado": "sin_datos", "error": str(exc)}
        respuesta["conclusion"] = _conclusion(radio_km, [], None, False)
        return respuesta
    respuesta["saff"] = {
        "estado": "ok",
        "periodo": {"desde": carga["desde"], "hasta": carga["hasta"]},
        "planes_comuna": len(carga["planes"]),
        "truncado": carga["truncado"],
    }

    cercanos = []
    for plan in carga["planes"]:
        ubic = ubicar_plan(plan, centro)
        if not ubic:
            continue
        dist = geo.haversine_km(lat, lon, ubic["lat"], ubic["lon"])
        if dist <= radio_km:
            cercanos.append({**plan, "ubicacion": ubic, "dist_km": round(dist, 3)})
    cercanos.sort(key=lambda p: p["dist_km"])
    cercanos = cercanos[:MAX_PLANES]

    consultar = [p for p in cercanos if p.get("Nro.Solicitud")][:MAX_PLANES_SIGCRA]
    rodales = await asyncio.gather(*(_rodales(str(p["Nro.Solicitud"])) for p in consultar))
    por_plan = {str(p["Nro.Solicitud"]): r for p, r in zip(consultar, rodales)}

    features = []
    dentro = None
    fallas = []
    for plan in cercanos:
        nro = str(plan.get("Nro.Solicitud") or "")
        r = por_plan.get(nro)
        plan["aprobado"] = plan_aprobado(plan, None)
        plan["aprobado_a_t1"] = plan_aprobado(plan, t1) if t1 else None
        if r is None:
            plan["sigcra"] = {"estado": "no_consultado"}
            continue
        if r["estado"] != "ok":
            plan["sigcra"] = {"estado": "sin_datos"}
            fallas.append(nro)
            continue
        plan["sigcra"] = {"estado": "ok", "rodales": len(r["fc"]["features"])}
        plan["punto_en_rodal"] = None
        for i, f in enumerate(r["fc"]["features"]):
            etiqueta = _etiqueta_rodal(f["properties"], i)
            contiene = geo.punto_en_geometria(lon, lat, f["geometry"])
            f["properties"] = {**f["properties"], "plan": nro, "rodal": etiqueta, "contiene_punto": contiene}
            features.append(f)
            if contiene and plan["punto_en_rodal"] is None:
                plan["punto_en_rodal"] = etiqueta
                if dentro is None or (not dentro["aprobado_a_t1"] and plan["aprobado_a_t1"]):
                    dentro = {
                        "nro_solicitud": nro,
                        "rodal": etiqueta,
                        "aprobado": plan["aprobado"],
                        "aprobado_a_t1": plan["aprobado_a_t1"],
                    }

    if consultar:
        if len(fallas) == len(consultar):
            respuesta["sigcra"] = {"estado": "sin_datos", "error": "sigcra_no_respondio"}
        elif fallas:
            respuesta["sigcra"] = {"estado": "parcial", "planes_sin_respuesta": fallas}
        else:
            respuesta["sigcra"] = {"estado": "ok"}
    respuesta["planes"] = cercanos
    respuesta["rodales"] = {"type": "FeatureCollection", "features": features}
    respuesta["dentro_de_rodal"] = dentro
    respuesta["conclusion"] = _conclusion(radio_km, cercanos, dentro, True)
    if consultar and respuesta["sigcra"]["estado"] == "sin_datos":
        respuesta["conclusion"] += " Sin datos por el momento de SIGCRA (rodales)."
    return respuesta
