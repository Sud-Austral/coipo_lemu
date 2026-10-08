"""Rol de avaluo SII: normalizacion y cruce de planes SAFF con predios CIREN.

CIREN (Propiedades Rurales) trae el rol como «manzana-predio» ('209-58') y
el codigo de comuna SUBDERE aparte (comudere). En SAFF «Rol Avaluo» es texto
libre (COALESCE de VISTA_PREDIO.PREDIO_ROL, VISTA_TRAMITACION.ROL_AVALUO y
ROL.ROL_NUMERO en per_api_saff), asi que se aceptan variantes:
'209-58', '00209-00058', '209/58', ' 209 - 058 ', '14108-209-58' (con codigo
de comuna) y varios roles en un mismo campo ('209-58; 209-59').
Rotulos sin numero ('SR', 'RIO FUY', 'RUTA 203') no son roles.
"""
from __future__ import annotations

import re
import unicodedata

# [codigo comuna de 5 digitos-]manzana-predio, separados por guion o barra.
_RE_ROL = re.compile(r"(?<!\d)(?:(\d{5})\s*[-/]\s*)?(\d{1,5})\s*[-/]\s*(\d{1,5})(?!\d)")


def normalizar_comuna(texto: str | None) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.upper().split())


def _codigo(valor) -> str | None:
    s = str(valor or "").strip().lstrip("0")
    return s or None


def roles_en_texto(texto) -> list[tuple[str, str | None]]:
    """[(rol normalizado 'manzana-predio', codigo de comuna o None)], sin repetidos."""
    if texto is None:
        return []
    salida: list[tuple[str, str | None]] = []
    for m in _RE_ROL.finditer(str(texto)):
        comuna, manzana, predio = m.groups()
        par = (f"{int(manzana)}-{int(predio)}", _codigo(comuna))
        if par not in salida:
            salida.append(par)
    return salida


def normalizar_rol(texto) -> str | None:
    """Primer rol del texto como 'manzana-predio' sin ceros a la izquierda, o None."""
    roles = roles_en_texto(texto)
    return roles[0][0] if roles else None


def indexar_planes(planes: list[dict]) -> dict[str, list[tuple[dict, str | None]]]:
    """rol normalizado -> [(plan, codigo de comuna declarado en el rol o None)]."""
    indice: dict[str, list[tuple[dict, str | None]]] = {}
    for plan in planes:
        for r, codigo in roles_en_texto(plan.get("Rol Avaluo")):
            indice.setdefault(r, []).append((plan, codigo))
    return indice


def plan_es_del_predio(plan: dict, codigo_rol: str | None, predio: dict) -> bool:
    """Misma comuna: por codigo si el rol SAFF lo trae y por nombre (sin tildes)."""
    if codigo_rol and _codigo(predio.get("comudere")) and codigo_rol != _codigo(predio.get("comudere")):
        return False
    comuna_predio = normalizar_comuna(predio.get("comuna"))
    comuna_plan = normalizar_comuna(plan.get("Comuna_Predio"))
    if comuna_predio and comuna_plan:
        return comuna_predio == comuna_plan or comuna_predio in comuna_plan or comuna_plan in comuna_predio
    return True


def planes_del_predio(indice: dict, predio: dict | None) -> list[dict]:
    """Planes SAFF con el mismo rol (y comuna) que el predio CIREN."""
    if not predio or not predio.get("rol_norm"):
        return []
    salida, vistos = [], set()
    for plan, codigo in indice.get(predio["rol_norm"], []):
        if id(plan) in vistos or not plan_es_del_predio(plan, codigo, predio):
            continue
        vistos.add(id(plan))
        salida.append(plan)
    return salida
