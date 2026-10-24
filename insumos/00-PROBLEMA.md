# Problema — detectar cortas de bosque posiblemente no autorizadas

Marcas: VERIFICADO (visto en código/documentos del repo), INFERIDO, pendiente de confirmar por el equipo. Fuente principal: `AGENTS.md` y el código; no hay documento de requerimiento en el repo.

## Contexto
VERIFICADO (`AGENTS.md`): el sistema busca «corta no autorizada» cruzando NDVI Sentinel-2, uso de suelo CIREN, predios rurales CIREN (rol SII), planes de manejo de SAFF y rodales de SIGCRA. Piloto: comuna de Panguipulli.

## Dolor actual
INFERIDO: revisar a mano miles de polígonos de bosque (VERIFICADO: Panguipulli tiene 5.995 polígonos / 227.005 ha según `AGENTS.md`; 5.130 con ≥ 1 ha) contra imágenes satelitales y planes de manejo no es viable. No encontré en el repo una descripción del proceso previo: pendiente de confirmar por el equipo.

## Usuarios
Pendiente de confirmar por el equipo. INFERIDO: fiscalización forestal de CONAF. El front es de uso interno (AGENTS.md: Bing y Google «solo para comparación en el piloto interno»).

## Alcance (VERIFICADO)
- Detección por polígono entre dos fechas (máx. 1 año de rango, `app/main.py`).
- Clasificación `alerta` / `revisar` / `en_regla` / `sin_datos` según plan aprobado a t1 para el rol del predio, planes cercanos (≤ 3 km) y rodales SIGCRA.
- Comparador visual antes/después y ficha por punto.

## Fuera de alcance
- Una región distinta de Panguipulli (pendiente según AGENTS.md).
- Datos de muestra: la regla del proyecto es no mostrarlos.
- Datos personales de propietarios (se descartan al recibir la respuesta de SAFF).
- Decidir si hubo infracción: la herramienta prioriza revisión, no sanciona (INFERIDO).

## Criterios de éxito
No constan en el repo. Pendiente de confirmar por el equipo (precisión esperada, falsos positivos tolerables, tiempo de respuesta). No comprobé la calidad de las alertas con datos reales: las APIs internas no son accesibles desde este entorno.
