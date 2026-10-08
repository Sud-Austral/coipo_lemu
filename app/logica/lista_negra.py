"""Decide el estado de un cambio forestal cruzando el predio CIREN, SAFF y SIGCRA.

Se aplica al centroide de cada poligono de bosque CIREN con caida de NDVI.
El listado y la ficha usan la misma regla (evaluar); la ficha agrega el cruce
con rodales SIGCRA (`dentro`). En este orden:
- sin_datos: SAFF no disponible.
- en_regla: el punto cae dentro de un rodal SIGCRA de un plan aprobado a t1
  (solo en la ficha).
- sin_datos: predios CIREN no disponibles o sin predio en el centroide.
- en_regla: hay un plan SAFF aprobado (ficha_punto.plan_aprobado, con
  Fec.Resolucion <= t1) con el mismo rol que el predio.
- revisar: hay planes con el mismo rol, pero ninguno aprobado a t1.
- revisar: no hay plan con ese rol (o el predio no tiene rol SII) pero si
  planes con punto de referencia a <= radio_km.
- alerta: ni plan del rol ni planes cercanos.
"""
from __future__ import annotations

ESTADO_ALERTA = "alerta"
ESTADO_EN_REGLA = "en_regla"
ESTADO_REVISAR = "revisar"
ESTADO_SIN_DATOS = "sin_datos"


class ResultadoListaNegra:
    def __init__(self, estado: str, motivo: str, plan_manejo: dict | None = None):
        self.estado = estado
        self.motivo = motivo
        self.plan_manejo = plan_manejo

    def to_dict(self) -> dict:
        return {"estado": self.estado, "motivo": self.motivo, "plan_manejo": self.plan_manejo}


def evaluar(
    saff_ok: bool,
    predio: dict | None,
    planes_rol: list[dict],
    planes_cercanos: int,
    radio_km: float,
    dentro: dict | None = None,
    sigcra_estado: str | None = None,
) -> ResultadoListaNegra:
    """Regla de estado.

    predio: salida de predios_ciren.info_predio. planes_rol: planes SAFF del
    mismo rol con «aprobado_a_t1» ya calculado. dentro: cruce con rodales
    SIGCRA de la ficha (None en el listado).
    """
    radio = f"{radio_km:g}".replace(".", ",")
    if not saff_ok:
        return ResultadoListaNegra(ESTADO_SIN_DATOS, "SAFF sin datos por el momento")
    if dentro and dentro.get("aprobado_a_t1"):
        return ResultadoListaNegra(
            ESTADO_EN_REGLA,
            f"Dentro del rodal {dentro['rodal']} del plan {dentro['nro_solicitud']} (aprobado a t1)",
            plan_manejo={"nro_solicitud": dentro["nro_solicitud"], "rodal": dentro["rodal"]},
        )
    estado_predio = (predio or {}).get("estado")
    if estado_predio != "ok":
        if estado_predio == "sin_predio":
            return ResultadoListaNegra(ESTADO_SIN_DATOS, "Sin predio CIREN en el centroide")
        return ResultadoListaNegra(ESTADO_SIN_DATOS, "Predios CIREN sin datos por el momento")

    r = predio.get("rol_norm")
    if r and planes_rol:
        aprobado = next((p for p in planes_rol if p.get("aprobado_a_t1")), None)
        if aprobado:
            nro = aprobado.get("Nro.Solicitud") or "s/n"
            return ResultadoListaNegra(
                ESTADO_EN_REGLA, f"Plan {nro} aprobado para el rol {r}", plan_manejo={"nro_solicitud": nro, "rol": r},
            )
        return ResultadoListaNegra(ESTADO_REVISAR, f"{len(planes_rol)} plan(es) SAFF para el rol {r}, sin resolución aprobada a t1")

    sin_rol = f"Sin plan para el rol {r}" if r else f"Predio sin rol SII ({predio.get('rol') or 'vacío'})"
    if planes_cercanos:
        motivo = f"{sin_rol}; hay {planes_cercanos} plan(es) a menos de {radio} km"
        if dentro:
            motivo += f"; dentro del rodal {dentro['rodal']} del plan {dentro['nro_solicitud']}, sin aprobación a t1"
        elif sigcra_estado in ("sin_datos", "parcial"):
            motivo += " (SIGCRA sin datos para cruzar rodales)"
        return ResultadoListaNegra(ESTADO_REVISAR, motivo)
    return ResultadoListaNegra(ESTADO_ALERTA, f"{sin_rol} ni planes de manejo SAFF a menos de {radio} km")


def evaluar_ficha(ficha: dict) -> ResultadoListaNegra:
    """Aplica la regla sobre la salida de ficha_punto.ficha_punto (con t1)."""
    return evaluar(
        (ficha.get("saff") or {}).get("estado") == "ok",
        ficha.get("predio"),
        ficha.get("planes_rol") or [],
        len(ficha.get("planes") or []),
        ficha.get("radio_km") or 3.0,
        dentro=ficha.get("dentro_de_rodal"),
        sigcra_estado=(ficha.get("sigcra") or {}).get("estado"),
    )
