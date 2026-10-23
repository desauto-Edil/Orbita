"""Recorridos independientes por ítem y actividades en paralelo — Fase 2 del modelo configurable.

Un bloque CONTENEDOR (`GRUPO_ACTIVIDADES` o `RECORRIDO_ITEMS`) de la instancia principal lanza SUBINSTANCIAS
(`InstanciaWorkflow` hija) que ejecuta el MISMO motor (`apps.workflows.motor`). No hay otro motor ni tickets
adicionales: cada subinstancia tiene su propio punto activo (la invariante del motor no cambia), sus propias
Tareas y aprobaciones (`apps.tareas` / `apps.aprobaciones`, sin duplicar nada) y su propia trazabilidad
(`InstanciaEtapa`, auditoría). La principal espera en el contenedor y este módulo la libera cuando la última
subinstancia termina.

Qué se lanza (`lanzar`), una sola vez por ejecución del contenedor (idempotente por las restricciones únicas de
`InstanciaWorkflow`):

- Rama GENERAL: una subinstancia por cada bloque general del contenedor (actividades generales que corren en
  paralelo entre sí y con los recorridos de los ítems).
- Rama ITEM (solo RECORRIDO_ITEMS): una subinstancia por cada ítem EN EL ALCANCE de la colección configurada
  (vigente y sin decisión de salida) que cumpla la condición de ingreso, si la hay. Recorre la secuencia de
  bloques ITEM del contenedor. Los ítems avanzan en momentos distintos; una devolución vuelve atrás SOLO en el
  recorrido de su ítem.

Configuración del RECORRIDO_ITEMS (`BloqueOperativo.configuracion`):

    coleccion              clave de la colección del Proceso (obligatoria)
    condicion_ingreso      {"variable": "item.<clave>", "operador": ..., "valor": ...} (opcional)
    incorporar_nuevos      bool: un ítem que se agrega mientras el bloque espera entra a su recorrido
    espera_alcance_cerrado bool: el bloque no termina mientras el alcance de la colección siga abierto

Cuándo termina el contenedor (`progreso(...).terminado`): no queda ninguna subinstancia en curso ni con error
y, si se configuró, el alcance de la colección ya se cerró. Al terminar publica `recorridos.<clave>.*` o
`grupos.<clave>.*` (totales por resultado) y las aprobaciones de las ramas generales quedan disponibles como
`aprobaciones.<clave>.resultado` en la principal, para que una DECISION posterior las use.

Resultado de un recorrido de ítem (`contexto["resultado_recorrido"]` de la subinstancia): COMPLETADO (recorrió
su secuencia), EXCLUIDO o RECHAZADO (su ítem salió del alcance por una aprobación o una decisión manual),
RETIRADO o CANCELADO. Un rechazo o una exclusión solo afecta a SU ítem: los demás siguen.

Orden de locks: instancia principal → subinstancias → Ticket → colección → tareas/aprobaciones → etapa
(`motor.bloquear_con_padre`). Todo ocurre dentro de la transacción de quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.workflows.contexto import INEXISTENTE, evaluar_operador
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

TIPO_GRUPO = "GRUPO_ACTIVIDADES"
TIPO_RECORRIDO = "RECORRIDO_ITEMS"
MOTIVOS = (InstanciaEtapa.MotivoEspera.RECORRIDO, InstanciaEtapa.MotivoEspera.GRUPO)
AMBITO_POR_TIPO = {TIPO_GRUPO: "grupos", TIPO_RECORRIDO: "recorridos"}

COMPLETADO = "COMPLETADO"
EXCLUIDO = "EXCLUIDO"
RECHAZADO = "RECHAZADO"
RETIRADO = "RETIRADO"
CANCELADO = "CANCELADO"
ESTADOS_EN_CURSO = (InstanciaWorkflow.Estado.EN_EJECUCION, InstanciaWorkflow.Estado.EN_ESPERA)


# --- Configuración -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfiguracionRecorrido:
    coleccion: str = ""
    condicion_ingreso: dict | None = None
    incorporar_nuevos: bool = False
    espera_alcance_cerrado: bool = False


def configuracion_de(bloque):
    cfg = bloque.configuracion or {}
    condicion = cfg.get("condicion_ingreso")
    if not (isinstance(condicion, dict) and condicion.get("variable") and condicion.get("operador")):
        condicion = None
    return ConfiguracionRecorrido(
        coleccion=str(cfg.get("coleccion") or ""),
        condicion_ingreso=condicion,
        incorporar_nuevos=bool(cfg.get("incorporar_nuevos")),
        espera_alcance_cerrado=bool(cfg.get("espera_alcance_cerrado")),
    )


def bloques_de_rama(contenedor, rama):
    return list(contenedor.contenidos.filter(rama=rama).order_by("orden", "pk"))


# --- Lectura -----------------------------------------------------------------------------------------------


def _ticket_de(instancia):
    from apps.tickets.models import Ticket

    return Ticket.objects.filter(instancia_workflow_id=instancia.raiz_id).first()


def _coleccion(ticket, clave):
    from apps.tickets.parrilla import parrilla_de

    parrilla = parrilla_de(ticket, clave) if clave else None
    if parrilla is None:
        raise ValidationError(
            "Este ticket no tiene la colección que recorre el bloque (se agregó o cambió después de crearlo)."
        )
    return parrilla


def _cumple_ingreso(elemento, condicion):
    if not condicion:
        return True
    variable = str(condicion.get("variable") or "")
    if not variable.startswith("item."):
        return True
    clave = variable.split(".", 1)[1]
    valores = {v.campo.clave: v.valor for v in elemento.valores.select_related("campo")}
    columnas = {c.clave for c in elemento.parrilla.formulario_version.campos.all()}
    actual = valores.get(clave) if clave in columnas else INEXISTENTE
    return evaluar_operador(condicion.get("operador"), actual, str(condicion.get("valor") or ""))


def resultado_de(hija):
    """Resultado de una subinstancia: el que quedó registrado o, si sigue abierta, su estado."""
    registrado = (hija.contexto or {}).get("resultado_recorrido")
    if registrado:
        return registrado
    if hija.estado == InstanciaWorkflow.Estado.COMPLETADA:
        return COMPLETADO
    if hija.estado == InstanciaWorkflow.Estado.CANCELADA:
        return CANCELADO
    return hija.estado


@dataclass(frozen=True)
class Progreso:
    total: int = 0
    en_curso: int = 0
    con_error: int = 0
    completados: int = 0
    excluidos: int = 0
    rechazados: int = 0
    retirados: int = 0
    cancelados: int = 0
    pendiente_alcance: bool = False

    @property
    def terminado(self):
        return self.en_curso == 0 and self.con_error == 0 and not self.pendiente_alcance

    def datos(self):
        return {
            "total": self.total, "en_curso": self.en_curso, "con_error": self.con_error,
            "completados": self.completados, "completadas": self.completados, "excluidos": self.excluidos,
            "rechazados": self.rechazados, "retirados": self.retirados, "cancelados": self.cancelados,
            "pendiente_alcance": self.pendiente_alcance,
        }


def progreso(ejecucion):
    """Estado agregado de las subinstancias que lanzó `ejecucion` (la ejecución del contenedor). Solo lee."""
    conteo = {"en_curso": 0, "con_error": 0, COMPLETADO: 0, EXCLUIDO: 0, RECHAZADO: 0, RETIRADO: 0, CANCELADO: 0}
    hijas = list(InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk))
    for hija in hijas:
        if hija.estado in ESTADOS_EN_CURSO:
            conteo["en_curso"] += 1
        elif hija.estado == InstanciaWorkflow.Estado.ERROR:
            conteo["con_error"] += 1
        else:
            conteo[resultado_de(hija) if resultado_de(hija) in conteo else COMPLETADO] += 1
    pendiente_alcance = False
    bloque = ejecucion.bloque_operativo
    if bloque is not None and bloque.tipo == TIPO_RECORRIDO:
        configuracion = configuracion_de(bloque)
        if configuracion.espera_alcance_cerrado:
            ticket = _ticket_de(ejecucion.instancia_workflow)
            from apps.tickets.parrilla import parrilla_de

            parrilla = parrilla_de(ticket, configuracion.coleccion) if ticket is not None else None
            pendiente_alcance = parrilla is not None and not parrilla.alcance_cerrado
    return Progreso(
        total=len(hijas), en_curso=conteo["en_curso"], con_error=conteo["con_error"],
        completados=conteo[COMPLETADO], excluidos=conteo[EXCLUIDO], rechazados=conteo[RECHAZADO],
        retirados=conteo[RETIRADO], cancelados=conteo[CANCELADO], pendiente_alcance=pendiente_alcance,
    )


def subinstancias_de(ejecucion):
    return list(
        InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk)
        .select_related("elemento__parrilla", "bloque_rama").order_by("pk")
    )


# --- Lanzar ------------------------------------------------------------------------------------------------


def lanzar(ejecucion, *, actor=None):
    """Lanza lo que falte para la ejecución de un contenedor (ver el docstring del módulo). Idempotente: una
    rama o un ítem que ya tiene su subinstancia en ESTA ejecución no se vuelve a lanzar. Se llama dentro del
    motor, con la instancia principal bloqueada."""
    from apps.workflows.motor import iniciar_subinstancia

    bloque = ejecucion.bloque_operativo
    padre = ejecucion.instancia_workflow
    if padre.instancia_padre_id is not None:
        raise ValidationError("Un contenedor no puede ejecutarse dentro de otro recorrido.")
    existentes = InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk)
    ramas_lanzadas = set(existentes.exclude(bloque_rama__isnull=True).values_list("bloque_rama_id", flat=True))
    for general in bloques_de_rama(bloque, "GENERAL"):
        if general.pk not in ramas_lanzadas:
            iniciar_subinstancia(padre, ejecucion, general, bloque_rama=general, actor=actor)
    if bloque.tipo != TIPO_RECORRIDO:
        return
    secuencia = bloques_de_rama(bloque, "ITEM")
    if not secuencia:
        return
    configuracion = configuracion_de(bloque)
    ticket = _ticket_de(padre)
    if ticket is None:
        raise ValidationError("El recorrido por ítem necesita una ejecución vinculada a un Ticket.")
    parrilla = _coleccion(ticket, configuracion.coleccion)
    from apps.tickets.parrilla import elementos_en_alcance

    con_recorrido = set(existentes.exclude(elemento__isnull=True).values_list("elemento_id", flat=True))
    for elemento in elementos_en_alcance(parrilla):
        if elemento.pk in con_recorrido or not _cumple_ingreso(elemento, configuracion.condicion_ingreso):
            continue
        iniciar_subinstancia(padre, ejecucion, secuencia[0], elemento=elemento, actor=actor)


def _ejecucion_vigente_en_espera(instancia_id):
    """La ejecución vigente de la instancia principal si está esperando a sus subinstancias, o `None`."""
    padre = InstanciaWorkflow.objects.filter(pk=instancia_id).first()
    if padre is None or padre.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return None
    ultima = padre.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if ultima is None or ultima.estado != InstanciaEtapa.Estado.EN_ESPERA or ultima.motivo_espera not in MOTIVOS:
        return None
    return ultima


def incorporar_elemento(ticket, elemento, *, actor=None):
    """Un ítem nuevo entra al recorrido que la instancia principal está esperando, si ese recorrido es de su
    colección, admite ítems nuevos y el ítem cumple la condición de ingreso. Lo llaman las operaciones de la
    colección (alta normal o extraordinaria) con la instancia principal ya bloqueada. Devuelve la subinstancia
    o `None`."""
    if ticket.instancia_workflow_id is None:
        return None
    ejecucion = _ejecucion_vigente_en_espera(ticket.instancia_workflow_id)
    if ejecucion is None or ejecucion.bloque_operativo.tipo != TIPO_RECORRIDO:
        return None
    bloque = ejecucion.bloque_operativo
    configuracion = configuracion_de(bloque)
    if not configuracion.incorporar_nuevos or configuracion.coleccion != elemento.parrilla.clave:
        return None
    secuencia = bloques_de_rama(bloque, "ITEM")
    if not secuencia or InstanciaWorkflow.objects.filter(ejecucion_padre=ejecucion, elemento=elemento).exists():
        return None
    if not _cumple_ingreso(elemento, configuracion.condicion_ingreso):
        return None
    from apps.workflows.motor import iniciar_subinstancia

    padre = InstanciaWorkflow.objects.select_for_update().get(pk=ticket.instancia_workflow_id)
    ejecucion = InstanciaEtapa.objects.select_for_update().get(pk=ejecucion.pk)
    return iniciar_subinstancia(padre, ejecucion, secuencia[0], elemento=elemento, actor=actor)


# --- Terminar ----------------------------------------------------------------------------------------------


def _fusionar_resultados_generales(padre, ejecucion):
    """Las aprobaciones de las ramas GENERALES quedan publicadas también en la principal
    (`aprobaciones.<clave>.resultado`), para que un bloque posterior las consulte. Las de los ítems no: cada
    ítem tiene la suya y una DECISION de la principal no puede elegir entre ellas."""
    destino = padre.contexto.setdefault("resultados_bloques", {})
    for hija in InstanciaWorkflow.objects.filter(ejecucion_padre=ejecucion, bloque_rama__isnull=False):
        for ambito, por_clave in ((hija.contexto or {}).get("resultados_bloques") or {}).items():
            destino.setdefault(ambito, {}).update(por_clave or {})


@transaction.atomic
def evaluar_espera(ejecucion_id):
    """Si la ejecución de un contenedor sigue esperando y ya no queda nada en curso, la libera y la instancia
    principal continúa por su ruta (con el resumen publicado). Idempotente: si ya continuó, o si no es la
    ejecución vigente, no hace nada. Devuelve `True` si continuó."""
    from apps.workflows.motor import bloquear_con_padre, continuar_espera_externa

    instancia_id = InstanciaEtapa.objects.filter(pk=ejecucion_id).values_list("instancia_workflow_id", flat=True).first()
    if instancia_id is None:
        return False
    padre = bloquear_con_padre(instancia_id)
    ejecucion = _ejecucion_vigente_en_espera(padre.pk)
    if ejecucion is None or ejecucion.pk != ejecucion_id:
        return False
    avance = progreso(ejecucion)
    if not avance.terminado:
        ejecucion.resultado = {**(ejecucion.resultado or {}), **avance.datos()}
        ejecucion.save(update_fields=["resultado", "actualizado_en"])
        return False
    _fusionar_resultados_generales(padre, ejecucion)
    padre.save(update_fields=["contexto", "actualizado_en"])
    continuar_espera_externa(
        ejecucion,
        motivo_espera=ejecucion.motivo_espera,
        resultado_bloque=(AMBITO_POR_TIPO[ejecucion.bloque_operativo.tipo], avance.datos()),
        resultado_ejecucion=avance.datos(),
    )
    return True


def reevaluar_ticket(ticket):
    """Vuelve a evaluar el contenedor que la instancia principal del Ticket esté esperando (p. ej. tras cerrar
    el alcance de una colección). Idempotente."""
    if ticket.instancia_workflow_id is None:
        return False
    ejecucion = _ejecucion_vigente_en_espera(ticket.instancia_workflow_id)
    if ejecucion is None:
        return False
    return evaluar_espera(ejecucion.pk)


def recorridos_activos_de(elemento):
    """Subinstancias de `elemento` que siguen en curso (o con error) en cualquier ejecución."""
    return list(
        InstanciaWorkflow.objects.filter(
            elemento=elemento,
            estado__in=(*ESTADOS_EN_CURSO, InstanciaWorkflow.Estado.ERROR),
        ).order_by("pk")
    )


def sacar_elemento(elemento, actor, *, resultado, motivo=""):
    """El ítem salió del alcance (excluido o rechazado manualmente) o se retiró: su recorrido en curso se
    detiene (sus Tareas y aprobaciones abiertas se cancelan, nada se borra) con ese resultado, y la principal
    se reevalúa. Los recorridos de los demás ítems no se tocan. Debe llamarse con la instancia principal
    bloqueada (las operaciones de la colección ya la bloquean)."""
    from apps.workflows.integracion import cancelar_subinstancia

    for hija in recorridos_activos_de(elemento):
        cancelar_subinstancia(hija, actor, resultado=resultado, motivo=motivo)
