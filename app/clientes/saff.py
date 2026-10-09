"""Cliente HTTP para API SAFF (historico de solicitudes de plan de manejo).

Los items de /v1/historico/pm traen datos personales del propietario. Se
eliminan aqui, apenas llegan, con una lista blanca de campos: nada con
nombre/RUT/correo/sexo/etnia de personas sale de este modulo ni queda en cache.
"""
from __future__ import annotations

import os
import re
from datetime import date

import httpx

SAFF_BASE_URL = os.getenv("SAFF_BASE_URL", "")
SAFF_API_KEY = os.getenv("SAFF_API_KEY", "")
TIMEOUT_S = 90.0
MAX_RANGO_DIAS = 3660

# Unicos campos de SAFF que se exponen (ver FIELDS en API SAFF
# app/historico_pm_routes.py). "Nombre Predio" es el nombre del predio, no de
# una persona.
CAMPOS_PUBLICOS = (
    "Nro.Solicitud",
    "Tipo Solicitud Compl",
    "Estdo Solicitud",
    "Fec.Ingreso",
    "Nro.Resolucion",
    "Fec.Resolucion",
    "Super.Solicitada",
    "Super.Aprobada",
    "Cod. Predio",
    "Nombre Predio",
    "Rol Avaluo",
    "Sup.Predial",
    "Comuna_Predio",
    "Oficina",
    "Pto.Referencia01",
    "Norte01",
    "Este01",
    "DATUM01",
)

# Defensa adicional por si un campo de la lista blanca cambiara de nombre.
_PATRON_PII = re.compile(r"rut|correo|mail|propietario|sexo|etnia|cant_prop|tipo_prop", re.IGNORECASE)


class SaffError(RuntimeError):
    """Error al consultar API SAFF."""


def sin_datos_personales(item: dict) -> dict:
    """Deja solo los campos publicos de un item de /v1/historico/pm."""
    return {k: item.get(k) for k in CAMPOS_PUBLICOS if k in item and not _PATRON_PII.search(k)}


async def historico_pm(
    fecha_desde: date,
    fecha_hasta: date,
    limit: int = 5000,
    offset: int = 0,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Una pagina de /v1/historico/pm, ya sin datos personales.

    El rango se filtra por Fec.Resolucion (SOLI_FECHA_RESOLUCION) y no puede
    superar 3660 dias.
    """
    url = f"{SAFF_BASE_URL}/v1/historico/pm"
    params = {
        "fecha_desde": fecha_desde.isoformat(),
        "fecha_hasta": fecha_hasta.isoformat(),
        "limit": limit,
        "offset": offset,
    }
    headers = {"X-API-Key": SAFF_API_KEY}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as propio:
                resp = await propio.get(url, params=params, headers=headers)
        else:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise SaffError(f"saff_unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        raise SaffError("saff_unauthorized")
    if resp.status_code >= 400:
        raise SaffError(f"saff_error_{resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SaffError("saff_respuesta_invalida") from exc
    return {
        "total": data.get("total"),
        "count": data.get("count"),
        "items": [sin_datos_personales(i) for i in data.get("items") or []],
    }


async def ping() -> bool:
    url = f"{SAFF_BASE_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
