"""Lienzo visual del flujo de un Servicio/Proceso — Fase 4.B.

Compone lo que dibuja el constructor visual del Studio (fases como columnas, actividades como tarjetas,
conexiones) a partir de la interfaz de dominio `flujo_api` y guarda las POSICIONES VISUALES. No contiene reglas de
ejecución ni escribe la configuración: crear, editar, reordenar o cambiar de fase una actividad siguen siendo las
operaciones de `configuracion_ejecucion` (con su validación, permisos, versión en borrador y auditoría).

POSICIÓN VISUAL ≠ CONEXIÓN FUNCIONAL:
- La posición (`PosicionVisualBloque`, por servicio + clave estable del bloque) solo decide dónde se pinta la
  tarjeta dentro de su columna. El motor nunca la lee.
- Lo que se ejecuta se dibuja tal cual lo resuelve el motor: secuencia por `orden` dentro de la fase y paso a la
  fase siguiente por la PRIMERA conexión de fases por prioridad; rutas explícitas de aprobaciones (por resultado)
  y decisiones (condición / en otro caso); «finalizar». Las conexiones de fase adicionales de la plantilla se
  muestran como «no se ejecutan» (el motor no las sigue) y nunca como caminos reales.
- Dentro de un recorrido por ítem o de un grupo en paralelo, los pasos se muestran dentro de la tarjeta del
  contenedor (en su orden, por rama); el lienzo no dibuja conexiones que crucen ámbitos porque el motor no las
  admite.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalogo import flujo_api
from apps.catalogo.models import BloqueOperativo, PosicionVisualBloque

ALTO_TARJETA = 132  # posición vertical por defecto: una tarjeta tras otra, en orden de ejecución
LIMITE_Y = 20000

ICONOS = {
    "ACTIVIDAD": "check-circle", "GRUPO_ACTIVIDADES": "layers", "ENTREGABLE": "file", "APROBACION": "shield",
    "DECISION": "git-branch", "RECORRIDO_ITEMS": "repeat", "TAREAS_ELEMENTOS": "list", "GACETA": "users",
    "ESPERA": "clock",
}
TIPO_FUNCIONAL = {
    "ACTIVIDAD": "Tarea", "GRUPO_ACTIVIDADES": "Tareas en paralelo", "ENTREGABLE": "Resultado",
    "APROBACION": "Aprobación", "DECISION": "Condición", "RECORRIDO_ITEMS": "Recorrido por ítem",
    "TAREAS_ELEMENTOS": "Una tarea por elemento", "GACETA": "Revisión colaborativa", "ESPERA": "Espera (heredada)",
}
ESTILO_RESULTADO = {"APROBADA": "aprobada", "DEVUELTA": "devuelta", "RECHAZADA": "rechazada", "EXCLUIDA": "rechazada"}


def clave_visual(bloque):
    """Identidad visual del bloque: su clave estable (o, si una fila histórica no la tiene, su pk)."""
    return bloque.clave or f"pk-{bloque.pk}"


@dataclass
class Tarjeta:
    bloque: object
    clave: str
    icono: str
    tipo: str
    resumen: str
    paso: int
    y: int
    errores: list = field(default_factory=list)
    ramas: list = field(default_factory=list)  # contenedores: [(etiqueta, [Tarjeta])]

    @property
    def dom_id(self):
        return f"nodo-{self.bloque.pk}"

    @property
    def estado(self):
        return "attention" if self.errores else "complete"


def _errores_por_bloque(configuracion, errores):
    """Asocia cada mensaje de validación a los bloques que nombra (los validadores citan el nombre del bloque)."""
    por_bloque = {}
    nombres = sorted(configuracion.bloques.values_list("pk", "nombre"), key=lambda par: -len(par[1] or ""))
    for mensaje in errores:
        for pk, nombre in nombres:
            if nombre and nombre in mensaje:
                por_bloque.setdefault(pk, []).append(mensaje)
                break
    return por_bloque


def _tarjeta(bloque, *, paso, y, errores, resumen_de):
    tarjeta = Tarjeta(
        bloque=bloque, clave=clave_visual(bloque), icono=ICONOS.get(bloque.tipo, "circle"),
        tipo=TIPO_FUNCIONAL.get(bloque.tipo, bloque.tipo), resumen=resumen_de(bloque), paso=paso, y=y,
        errores=errores.get(bloque.pk, []),
    )
    if bloque.tipo in BloqueOperativo.TIPOS_CONTENEDOR:
        for rama, etiqueta in (("ITEM", "Para cada ítem"), ("GENERAL", "En paralelo")):
            pasos = list(bloque.contenidos.filter(rama=rama).order_by("orden", "pk"))
            if pasos:
                tarjeta.ramas.append((etiqueta, [
                    _tarjeta(p, paso=i, y=0, errores=errores, resumen_de=resumen_de) for i, p in enumerate(pasos, 1)
                ]))
    return tarjeta


def datos(servicio, configuracion, *, errores=(), resumen_de=lambda b: ""):
    """Todo lo que pinta el lienzo para `configuracion` (la que se muestra: borrador o activa)."""
    posiciones = {p.clave: p for p in PosicionVisualBloque.objects.filter(servicio=servicio)}
    por_bloque = _errores_por_bloque(configuracion, errores)
    columnas = []
    for indice, fila in enumerate(flujo_api.fases(configuracion)):
        fase = fila["fase"]
        tarjetas = []
        for paso, bloque in enumerate(fila["bloques"], start=1):
            guardada = posiciones.get(clave_visual(bloque))
            y = guardada.y if guardada is not None else (paso - 1) * ALTO_TARJETA
            tarjetas.append(_tarjeta(bloque, paso=paso, y=y, errores=por_bloque, resumen_de=resumen_de))
        salidas = list(fase.transiciones_salientes.select_related("fase_destino").order_by("prioridad", "pk"))
        columnas.append({
            "fase": fase, "indice": indice, "dom_id": f"fase-{fase.pk}", "tarjetas": tarjetas,
            "alto": max([t.y for t in tarjetas] + [0]) + ALTO_TARJETA + 24,
            "con_errores": any(t.errores for t in tarjetas),
            "salidas": [{"destino": t.fase_destino, "ejecuta": i == 0, "transicion": t} for i, t in enumerate(salidas)],
        })
    return {"columnas": columnas, "conexiones": conexiones(configuracion), "fases_extra": _fases_extra(columnas)}


def conexiones(configuracion):
    """Conexiones que se dibujan (solo entre bloques del flujo principal; ver docstring del módulo)."""
    resultado = []
    for indice, conexion in enumerate(flujo_api.conexiones(configuracion), start=1):
        origen = conexion["origen"]
        if origen.contenedor_id is not None:
            continue
        destino = conexion["destino"]
        transicion = conexion.get("transicion")
        if conexion["tipo"] == "secuencia":
            estilo = "secuencia"
        elif transicion is not None and transicion.resultado_aprobacion:
            estilo = ESTILO_RESULTADO.get(transicion.resultado_aprobacion, "condicion")
        elif transicion is not None and transicion.es_fallback:
            estilo = "otro_caso"
        else:
            estilo = "condicion"
        resultado.append({
            "id": f"conexion-{indice}", "de": f"nodo-{origen.pk}",
            "a": "nodo-fin" if conexion["finaliza"] or destino is None else f"nodo-{destino.pk}",
            "estilo": estilo, "etiqueta": "" if estilo == "secuencia" else conexion["cuando"],
            "origen": origen, "destino": destino, "transicion": transicion, "tipo": conexion["tipo"],
            "cuando": conexion["cuando"],
        })
    return resultado


def _fases_extra(columnas):
    """Conexiones de fase que la plantilla tiene pero el motor NO sigue (solo la primera por prioridad)."""
    extra = []
    for columna in columnas:
        for salida in columna["salidas"]:
            if not salida["ejecuta"]:
                extra.append({"de": columna["dom_id"], "a": f"fase-{salida['destino'].pk}", "nombre": salida["destino"].nombre})
    return extra


def json_para_dibujar(datos_lienzo):
    """Lo mínimo que necesita `static/js/lienzo.js` (ids del DOM y estilos), sin objetos del ORM."""
    return {
        "conexiones": [
            {"id": c["id"], "de": c["de"], "a": c["a"], "estilo": c["estilo"], "etiqueta": c["etiqueta"]}
            for c in datos_lienzo["conexiones"]
        ],
        "fasesExtra": datos_lienzo["fases_extra"],
    }


@transaction.atomic
def guardar_posiciones(servicio, posiciones):
    """Guarda posiciones VISUALES (`[{"clave", "x", "y"}]`). Solo claves de bloques de este Servicio; valores
    enteros acotados. No toca la configuración ni el orden de ejecución, por eso no exige un borrador: mover una
    tarjeta en una configuración publicada no cambia nada de lo que se ejecuta. Devuelve cuántas guardó."""
    validas = {
        clave_visual(b) for b in BloqueOperativo.objects.filter(version__servicio=servicio).only("pk", "clave")
    }
    guardadas = 0
    for dato in posiciones or []:
        clave = str((dato or {}).get("clave") or "")
        if clave not in validas:
            raise ValidationError(f"«{clave}» no es una actividad de este servicio.")
        try:
            x, y = int(dato.get("x") or 0), int(dato.get("y") or 0)
        except (TypeError, ValueError):
            raise ValidationError("Posición no válida.")
        if not (0 <= y <= LIMITE_Y and 0 <= x <= LIMITE_Y):
            raise ValidationError("Posición fuera del lienzo.")
        PosicionVisualBloque.objects.update_or_create(servicio=servicio, clave=clave, defaults={"x": x, "y": y})
        guardadas += 1
    return guardadas
