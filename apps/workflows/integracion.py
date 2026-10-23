"""Integración Tarea/Aprobación ↔ Workflow — 3.3/3.4, corrección
arquitectónica aprobada.

Ni `apps.tareas` ni `apps.aprobaciones` conocen `apps.workflows` (ni
siquiera de forma diferida) — la orquestación de "completar/resolver un
trabajo externo que además debe reanudar su Workflow" vive exclusivamente
aquí, del lado integrador:

    apps.tareas / apps.aprobaciones
        completan/resuelven su propio trabajo, no saben que existe un
        Workflow
    apps.workflows (este módulo)
        sabe que estaban vinculados, y reanuda el motor

Cada función pública corre en una única transacción — "Tarea COMPLETADA/
Aprobación decidida pero Workflow no avanzó" queda estructuralmente
imposible (todo o nada). Sin `signals`, sin Domain Events/Observer todavía
(ese patrón está atado a Sprint 7 en `CLAUDE.md`, no se adelanta aquí).

ORDEN DE LOCKS (4.G3A). La `InstanciaWorkflow` es la COMPUERTA de su ejecución: todo lo que se crea,
reasigna, completa o cancela dentro de ella lo hace con su fila bloqueada, y toda operación que toma más
de un lock de una ejecución empieza por ella. Orden único:

    InstanciaWorkflow → Ticket → ParrillaTicket/ElementoParrilla → Tarea(s) y Aprobación(es) → InstanciaEtapa

Se omiten los niveles que una operación no necesita, pero nunca se invierte el orden entre los que usa.
Una operación que solo toca UNA Tarea (iniciar, tomar, delegar, completar sin flujo) no toma otro lock tras
ella, así que no puede cruzarse. `completar_tarea_workflow` y `resolver_aprobacion_workflow` bloquean la
instancia ANTES de la Tarea/Aprobación (resuelta por lectura sin lock: el vínculo no cambia); la
cancelación bloquea la instancia, el Ticket y solo entonces el trabajo de la ejecución, y calcula qué
trabajo hay YA con la compuerta cerrada (nada nuevo puede aparecer entre medias)."""

from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion
from apps.aprobaciones.operaciones import anular_esquema_aprobacion, resolver_aprobacion
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tareas.models import Tarea
from apps.tareas.operaciones import asignar_por_flujo, cancelar_tarea, completar_tarea
from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa, InstanciaWorkflow, TareaWorkflow
from apps.workflows.motor import continuar_espera_externa


def bloquear_instancia(instancia_id):
    """Primer lock de toda operación que toma más de un lock de una ejecución (ver el docstring del módulo).
    Devuelve la `InstanciaWorkflow` bloqueada. Debe llamarse dentro de una transacción. Fase 2: si es una
    subinstancia, primero se bloquea su instancia principal (orden «principal → hija»)."""
    from apps.workflows.motor import bloquear_con_padre

    return bloquear_con_padre(instancia_id)


def ids_de_ejecucion(instancia_id):
    """La instancia y sus subinstancias (Fase 2), en orden de pk: todo lo que «la ejecución del Ticket» abarca."""
    return [instancia_id, *InstanciaWorkflow.objects.filter(instancia_padre_id=instancia_id).order_by("pk").values_list("pk", flat=True)]


def _instancia_id_de_tarea(tarea):
    """Id de la `InstanciaWorkflow` de una Tarea del flujo o generada por un elemento, o `None` si no
    pertenece a una ejecución. Lectura SIN lock: el vínculo es inmutable."""
    instancia_id = (
        TareaWorkflow.objects.filter(tarea_id=tarea.pk)
        .values_list("instancia_etapa__instancia_workflow_id", flat=True).first()
    )
    if instancia_id is not None:
        return instancia_id
    from apps.tickets.models import TareaElemento

    return (
        TareaElemento.objects.filter(tarea_id=tarea.pk)
        .values_list("elemento__parrilla__ticket__instancia_workflow_id", flat=True).first()
    )


@transaction.atomic
def completar_tarea_workflow(tarea, actor):
    """Única forma correcta de completar la Tarea principal que una Etapa
    TAREA generó y que debe reanudar su Workflow (W.6/W.7, correcciones
    aprobadas).

    Completar una SUBTAREA nunca pasa por aquí ni reanuda nada — una
    subtarea nunca tiene fila `TareaWorkflow` (solo la Tarea que
    `EstrategiaTarea` creó directamente la tiene), así que esta función
    falla explícitamente si se le pasa una: no es una omisión, es la
    consecuencia directa y buscada del esquema (1 ejecución TAREA → 1
    Tarea, W.6: "solo completar la Tarea principal vinculada al Workflow
    puede producir continuación")."""
    # 4.G3A: la instancia ANTES que la Tarea (que bloquea `completar_tarea`): cancelar el Ticket, tomarlo o
    # materializar tareas bloquean la instancia primero, y así no se cruzan con esta finalización.
    instancia_id = _instancia_id_de_tarea(tarea)
    if instancia_id is not None:
        bloquear_instancia(instancia_id)
    vinculo = TareaWorkflow.objects.filter(tarea=tarea).select_related("instancia_etapa").first()
    if vinculo is None and hasattr(tarea, "vinculo_elemento"):
        # 4.G2D2: una Tarea generada por un elemento no tiene su propio bloque; si es la última del conjunto,
        # libera el bloque TAREAS_ELEMENTOS.
        tarea_completada = completar_tarea(tarea, actor)
        continuar_por_tareas_elementos(tarea_completada)
        return tarea_completada
    if vinculo is None:
        raise ValueError(
            "Esta tarea no está vinculada a ningún Workflow — use "
            "apps.tareas.operaciones.completar_tarea directamente."
        )

    _exigir_datos_inicio_si_corresponde(vinculo.instancia_etapa)
    tarea_completada = completar_tarea(tarea, actor)
    continuar_espera_externa(vinculo.instancia_etapa, motivo_espera=InstanciaEtapa.MotivoEspera.TAREA)
    return tarea_completada


def _exigir_datos_inicio_si_corresponde(ejecucion):
    """Fase 2 — una ACTIVIDAD configurada con «exige datos de inicio completos» no se completa mientras el
    diligenciamiento de los datos de inicio del ticket siga pendiente. Opcional por actividad: el formulario no
    se asocia obligatoriamente a ninguna fase ni actividad."""
    bloque = ejecucion.bloque_operativo
    if bloque is None or not (bloque.configuracion or {}).get("exige_datos_inicio"):
        return
    from apps.tickets.datos_inicio import datos_completos
    from apps.tickets.models import Ticket

    instancia = ejecucion.instancia_workflow
    ticket = Ticket.objects.filter(instancia_workflow_id=instancia.raiz_id).first()
    if ticket is not None and not datos_completos(ticket):
        raise ValidationError(
            "Completa primero el diligenciamiento de los datos de inicio del ticket: esta actividad lo exige."
        )


@transaction.atomic
def continuar_por_tareas_elementos(tarea):
    """4.G2D2 — libera un bloque TAREAS_ELEMENTOS cuando se completó la ÚLTIMA Tarea generada por los
    elementos del Ticket. Lo invoca `completar_tarea_workflow` en la misma transacción que completa la Tarea.

    Devuelve `True` si continuó el Workflow y `False` si no había nada que continuar. Es IDEMPOTENTE: bajo
    el lock de la instancia (orden de locks Tarea → instancia → Ticket, el de siempre) vuelve a evaluar el
    conjunto y solo continúa si la ejecución vigente sigue EN_ESPERA por ELEMENTOS y ya no queda ninguna
    Tarea abierta; un segundo aviso (reintento, otra tarea completada a la vez) ya no encuentra esa espera.
    Una instancia CANCELADA (o ya terminada) no es EN_ESPERA, así que una finalización tardía no la
    reactiva. Publica `tareas_elementos.<clave>.completadas|total` y sigue por la ruta del bloque."""
    from apps.tickets import tareas_elemento

    ticket = tareas_elemento.ticket_de(tarea)
    if ticket is None or ticket.instancia_workflow_id is None:
        return False
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=ticket.instancia_workflow_id)
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return False
    ejecucion = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if (
        ejecucion is None
        or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA
        or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.ELEMENTOS
        or ejecucion.bloque_operativo is None
    ):
        return False
    if tareas_elemento.correccion_pendiente(ejecucion):
        # 4.G2E: el bloque espera que se corrija información y se reintente la generación; completar una
        # tarea ya existente no lo libera (aún faltan las de los elementos que no se pudieron generar).
        return False
    progreso = tareas_elemento.progreso_de(ticket)
    if progreso.abiertas:
        return False
    datos = {"total": progreso.total, "completadas": progreso.completadas}
    continuar_espera_externa(
        ejecucion,
        motivo_espera=InstanciaEtapa.MotivoEspera.ELEMENTOS,
        resultado_bloque=("tareas_elementos", datos),
        resultado_ejecucion=datos,
    )
    return True


@dataclass(frozen=True)
class ResultadoReintento:
    """Qué pasó con un reintento explícito de la generación de tareas por elemento (4.G2E).

    - `aplicado=False` y sin `mensajes`: no había nada que reintentar (ya se corrigió, o el flujo no espera
      una corrección): idempotente, sin efectos.
    - `aplicado=True` y `mensajes`: la información sigue incompleta; el bloque sigue esperando y `mensajes`
      dice qué corregir.
    - `aplicado=True`, sin `mensajes`: se generaron las tareas; `continuo` indica si además el flujo
      avanzó (cuando no quedó ninguna tarea abierta)."""

    aplicado: bool = False
    mensajes: tuple = ()
    continuo: bool = False
    generadas: int = 0


@transaction.atomic
def reintentar_tareas_elementos(ticket, actor):
    """4.G2E — reintento EXPLÍCITO de la generación de tareas de un bloque TAREAS_ELEMENTOS que quedó
    esperando una corrección de información. Nada lo dispara solo ni lo repite indefinidamente.

    Autoriza quien edita los elementos (`puede_editar_parrilla`: responsable individual en atención).
    Orden de locks: instancia → Ticket → parrilla (el del motor). Revalida bajo lock:

    - una instancia CANCELADA (o el Ticket cancelado) se rechaza: nunca se reactiva;
    - si la ejecución vigente ya no espera una corrección (otro reintento la resolvió, o el flujo avanzó)
      no hace nada (idempotente);
    - si la información sigue incompleta, conserva el Ticket, la instancia y la espera, y devuelve qué
      corregir; no genera ninguna tarea (la materialización es atómica);
    - si ya es válida genera las tareas que falten (asociadas a esta ejecución, sin duplicar) y, si no
      queda ninguna abierta, continúa por el motor.
    """
    from apps.tickets import tareas_elemento
    from apps.tickets.autorizacion import puede_editar_parrilla
    from apps.tickets.models import Ticket
    from apps.workflows.contexto import registrar_resultado_etapa

    if ticket.instancia_workflow_id is None:
        raise ValidationError("Este ticket no tiene un flujo de trabajo en curso.")
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=ticket.instancia_workflow_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if instancia.estado == InstanciaWorkflow.Estado.CANCELADA or ticket.estado == Ticket.Estado.CANCELADO:
        raise ValidationError("Este ticket fue cancelado: su flujo no se reactiva.")
    if not puede_editar_parrilla(actor, ticket):
        raise PermissionDenied("Solo quien atiende el ticket puede reintentar la generación de tareas.")
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return ResultadoReintento()
    # La fila de la instancia ya está bloqueada: nadie más avanza esta ejecución mientras tanto.
    ejecucion = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if (
        ejecucion is None
        or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA
        or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.ELEMENTOS
        or ejecucion.bloque_operativo is None
        or ejecucion.bloque_operativo.tipo != "TAREAS_ELEMENTOS"
    ):
        return ResultadoReintento()
    anterior = tareas_elemento.correccion_pendiente(ejecucion)
    if not anterior:
        return ResultadoReintento()

    antes = tareas_elemento.progreso_de(ticket).total
    try:
        progreso = tareas_elemento.entrar_al_bloque(ticket, ejecucion)
    except ValidationError as exc:
        resultado = {
            **tareas_elemento.datos_de_correccion(
                ticket, exc.messages, reintentos=int(anterior.get("reintentos", 0)) + 1
            ),
        }
        ejecucion.resultado = resultado
        ejecucion.save(update_fields=["resultado", "actualizado_en"])
        registrar_resultado_etapa(instancia.contexto, ejecucion.bloque_operativo.pk, resultado)
        instancia.save(update_fields=["contexto", "actualizado_en"])
        return ResultadoReintento(aplicado=True, mensajes=tuple(exc.messages))

    datos = {"total": progreso.total, "completadas": progreso.completadas}
    ejecucion.resultado = datos
    ejecucion.save(update_fields=["resultado", "actualizado_en"])
    registrar_resultado_etapa(instancia.contexto, ejecucion.bloque_operativo.pk, datos)
    instancia.save(update_fields=["contexto", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=ejecucion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"correccion": anterior}, datos_nuevos={**datos, "tareas_generadas": progreso.total - antes},
    )
    continuo = False
    if not progreso.abiertas:
        continuar_espera_externa(
            ejecucion,
            motivo_espera=InstanciaEtapa.MotivoEspera.ELEMENTOS,
            resultado_bloque=("tareas_elementos", datos),
            resultado_ejecucion=datos,
        )
        continuo = True
    return ResultadoReintento(aplicado=True, continuo=continuo, generadas=progreso.total - antes)


@transaction.atomic
def entregar_tareas_pendientes_al_responsable(ticket, actor):
    """4.F3 — cuando el Ticket recibe un responsable individual (tomar / asignar), las Tareas que
    el flujo ya creó PARA «EL RESPONSABLE DEL TICKET» y que quedaron sin dueño (el Ticket aún no
    tenía responsable cuando el motor llegó a ese bloque) pasan a esa persona. Solo toca esas:
    una Tarea con responsable o equipo propio, o de otro tipo de actor, no se modifica.
    Devuelve las Tareas entregadas."""
    if ticket.instancia_workflow_id is None or ticket.usuario_responsable_id is None:
        return []
    entregadas = []
    vinculos = (
        TareaWorkflow.objects.filter(
            # Fase 2: también las de los recorridos por ítem y las actividades en paralelo del ticket.
            instancia_etapa__instancia_workflow_id__in=ids_de_ejecucion(ticket.instancia_workflow_id),
            instancia_etapa__estado=InstanciaEtapa.Estado.EN_ESPERA,
            instancia_etapa__instancia_workflow__estado=InstanciaWorkflow.Estado.EN_ESPERA,
            tarea__usuario_responsable__isnull=True,
            tarea__equipo_responsable__isnull=True,
        )
        .exclude(tarea__estado__in=Tarea.ESTADOS_CERRADOS)
        .select_related("tarea", "instancia_etapa__bloque_operativo", "instancia_etapa__etapa")
    )
    for vinculo in vinculos:
        ejecucion = vinculo.instancia_etapa
        if ejecucion.bloque_operativo_id is not None:
            tipo_actor = (ejecucion.bloque_operativo.configuracion or {}).get("tipo_actor")
        else:
            configuracion = getattr(ejecucion.etapa, "configuracion_tarea", None)
            tipo_actor = getattr(configuracion, "tipo_responsable", None)
        if tipo_actor == "RESPONSABLE_TICKET":
            entregadas.append(asignar_por_flujo(vinculo.tarea, ticket.usuario_responsable, actor))
    return entregadas


@transaction.atomic
def resolver_aprobacion_workflow(aprobacion, actor, *, decision, observacion=""):
    """Única forma correcta de decidir una `Aprobacion` cuyo
    `EsquemaAprobacion` está vinculado a un Workflow y que, al cerrar,
    debe reanudar su ejecución (3.4, diseño técnico aprobado).

    A diferencia de `completar_tarea_workflow` (TAREA: una sola Tarea → un
    solo cierre posible), aquí el cierre del `EsquemaAprobacion` puede NO
    ocurrir todavía con esta decisión (SECUENCIAL que solo avanza al
    siguiente participante, o PARALELA que sigue esperando otras
    decisiones) — en ese caso no hay nada que continuar aún, y esta
    función retorna sin tocar el Workflow. Cuando sí cierra, resuelve la
    `TransicionEtapa` que corresponde al `resultado` final
    (APROBADA/RECHAZADA/DEVUELTA) contra la propia etapa APROBACION —
    búsqueda exacta por `resultado_aprobacion`, sin usar
    `contexto.evaluar_operador`/`resolver_variable` (esos son de
    CONDICION; una decisión de aprobación no es una expresión a evaluar,
    es una de 3 etiquetas cerradas — RN-021, la resolución de la
    transición es responsabilidad de este módulo integrador, nunca del
    motor)."""
    # 4.G3A: la instancia ANTES que la Aprobación (la cancelación la toma primero): ver el docstring del módulo.
    instancia_id = (
        EsquemaAprobacionWorkflow.objects.filter(esquema_id=aprobacion.esquema_id)
        .values_list("instancia_etapa__instancia_workflow_id", flat=True).first()
    )
    if instancia_id is not None:
        bloquear_instancia(instancia_id)
    aprobacion, esquema = resolver_aprobacion(aprobacion, actor, decision=decision, observacion=observacion)
    if esquema.resultado is None:
        return aprobacion, esquema

    vinculo = EsquemaAprobacionWorkflow.objects.filter(esquema=esquema).select_related("instancia_etapa").first()
    if vinculo is None:
        raise ValueError(
            "Este esquema de aprobación no está vinculado a ningún Workflow — use "
            "apps.aprobaciones.operaciones.resolver_aprobacion directamente."
        )

    ejecucion = vinculo.instancia_etapa
    definicion = ejecucion.etapa or ejecucion.bloque_operativo
    bloque = ejecucion.bloque_operativo
    if bloque is not None and bloque.contenedor_id is not None:
        # Fase 2 — una aprobación DENTRO de un contenedor (recorrido por ítem o rama general).
        _resolver_aprobacion_contenida(ejecucion, esquema)
        return aprobacion, esquema
    transicion = definicion.transiciones_salientes.get(resultado_aprobacion=esquema.resultado)
    # 4.E2: si la aprobación REVISA un entregable, esta pasada deja constancia de qué versión
    # vio al decidir (en el historial de ESTA ejecución). Es lo que permite exigir una nueva
    # versión cuando el resultado fue devolver/rechazar. No altera la ruta.
    from apps.tickets.entregables import foto_de_revision

    def _evidencia_de_revision():
        # Se evalúa bajo el lock de la instancia (ver `continuar_espera_externa`): los entregables
        # toman ese mismo lock para escribir, así que la versión que se registra es la que se vio.
        foto = foto_de_revision(ejecucion)
        return {"revision": foto} if foto is not None else None

    # 4.B0: además de elegir la ruta por `resultado_aprobacion` (sin cambios), el
    # resultado queda publicado por la clave estable del bloque para que una
    # DECISION posterior lo consulte (`aprobaciones.<clave>.resultado`).
    continuar_espera_externa(
        ejecucion,
        motivo_espera=InstanciaEtapa.MotivoEspera.APROBACION,
        transicion_seleccionada=transicion,
        resultado_bloque=("aprobaciones", {"resultado": esquema.resultado}),
        resultado_ejecucion=_evidencia_de_revision,
    )
    return aprobacion, esquema


SALIDA_POR_RESULTADO = {"RECHAZADA": "RECHAZADO", "EXCLUIDA": "EXCLUIDO"}


def _decision_que_cerro(esquema):
    """La participación que decidió el resultado del esquema (la primera con ese estado): su autor y su
    observación justifican el cambio de alcance."""
    return esquema.participaciones.filter(estado=esquema.resultado).order_by("decidida_en", "pk").first()


def _resolver_aprobacion_contenida(ejecucion, esquema):
    """Fase 2 — cierre de una aprobación de un recorrido por ítem o de una rama general.

    - Rama ITEM, RECHAZADA o EXCLUIDA: el ítem sale del alcance con esa decisión (origen APROBACION, la
      observación de quien decidió como justificación) y SU recorrido termina; los demás ítems no cambian.
    - Rama ITEM, APROBADA o DEVUELTA: sigue la ruta configurada para ese resultado dentro del recorrido (o la
      secuencia, si la aprobación no tiene rutas). La devolución solo vuelve atrás en el recorrido de su ítem.
    - Rama GENERAL: la rama es una sola aprobación; termina con su resultado publicado
      (`aprobaciones.<clave>.resultado`), que la principal recibe al terminar el contenedor."""
    from apps.workflows.motor import bloquear_con_padre, finalizar_subinstancia

    bloque = ejecucion.bloque_operativo
    publicado = ("aprobaciones", {"resultado": esquema.resultado})
    if bloque.rama == "ITEM" and esquema.resultado in SALIDA_POR_RESULTADO:
        hija = bloquear_con_padre(ejecucion.instancia_workflow_id)
        ejecucion = InstanciaEtapa.objects.select_for_update().get(pk=ejecucion.pk)
        if hija.estado != InstanciaWorkflow.Estado.EN_ESPERA or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA:
            return
        decision = _decision_que_cerro(esquema)
        resultado = SALIDA_POR_RESULTADO[esquema.resultado]
        if hija.elemento_id is not None:
            from apps.tickets.parrilla import registrar_salida_por_flujo

            registrar_salida_por_flujo(
                hija.elemento, decision.decidido_por if decision else None, tipo=resultado,
                justificacion=(decision.observacion if decision else "") or f"{esquema.get_resultado_display()} en «{bloque.nombre}».",
            )
        from apps.workflows.contexto import publicar_resultado_bloque

        if bloque.clave:
            ambito, datos = publicado
            publicar_resultado_bloque(hija.contexto, ambito, bloque.clave, datos)
        ejecucion.resultado = {**(ejecucion.resultado or {}), "resultado": esquema.resultado}
        ejecucion.save(update_fields=["resultado", "actualizado_en"])
        finalizar_subinstancia(hija, ejecucion, resultado=resultado)
        return
    transicion = None
    if bloque.rama == "ITEM":
        transicion = bloque.transiciones_salientes.filter(resultado_aprobacion=esquema.resultado).first()
        if transicion is None and esquema.resultado != "APROBADA":
            raise ValidationError(
                f"La aprobación «{bloque.nombre}» no tiene ruta para «{esquema.get_resultado_display()}»."
            )
    continuar_espera_externa(
        ejecucion,
        motivo_espera=InstanciaEtapa.MotivoEspera.APROBACION,
        transicion_seleccionada=transicion,
        resultado_bloque=publicado,
        resultado_ejecucion={"resultado": esquema.resultado},
    )


@transaction.atomic
def continuar_por_entregable(entregable):
    """4.B1 — libera un bloque ENTREGABLE que esperaba su entregable. Lo invoca el
    DOMINIO del entregable (`apps.tickets.entregables`) cada vez que se escribe uno, no una
    vista: completar por otra vista, API o automatización reanuda igual.

    Devuelve `True` si continuó el Workflow y `False` si no había nada que continuar. Es
    IDEMPOTENTE: bajo el lock de la instancia solo continúa si la ejecución vigente sigue
    EN_ESPERA por ENTREGABLE, es el bloque de ESTE entregable y este sigue satisfecho; un
    segundo evento (doble clic, reintento, otra escritura) ya no encuentra esa espera.

    Orden de locks: instancia → ticket, el mismo que usa el motor al resolver actores (los
    entregables toman el lock de la instancia ANTES del de su ticket para no cruzarlo).
    Publica `entregables.<clave>.satisfecho` y sigue por `TransicionBloqueOperativo`; no
    entrega formalmente, no resuelve ni cierra el Ticket."""
    from apps.tickets.entregables import entregable_vigente_para_flujo

    instancia_id = entregable.ticket.instancia_workflow_id
    if instancia_id is None:
        return False
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return False
    ejecucion = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if (
        ejecucion is None
        or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA
        or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.ENTREGABLE
        or ejecucion.bloque_operativo is None
        or ejecucion.bloque_operativo.definicion_entregable_id != entregable.definicion_id
    ):
        return False
    if not entregable_vigente_para_flujo(entregable):
        return False
    continuar_espera_externa(
        ejecucion,
        motivo_espera=InstanciaEtapa.MotivoEspera.ENTREGABLE,
        resultado_bloque=("entregables", {"satisfecho": True}),
    )
    return True


# --- 4.G1.5 — Cancelación coherente de la ejecución ------------------------------------------------


def bloquear_trabajo_de_ejecucion(instancia_id):
    """Bloquea las Tareas (y subtareas) y las aprobaciones que la ejecución creó, ordenadas por pk. Exige que
    la instancia YA esté bloqueada por esta transacción (`bloquear_instancia`): con la compuerta cerrada el
    conjunto no cambia mientras se bloquea, y quien complete una Tarea o decida una aprobación a la vez
    espera a la instancia (ver el docstring del módulo). Debe llamarse dentro de una transacción."""
    # Fase 2: la ejecución abarca también sus subinstancias (bloqueadas después de la principal, por pk).
    ids = ids_de_ejecucion(instancia_id)
    if len(ids) > 1:
        list(InstanciaWorkflow.objects.select_for_update().filter(pk__in=ids[1:]).order_by("pk"))
    tareas_ids = list(
        TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow_id__in=ids).values_list("tarea_id", flat=True)
    )
    if tareas_ids:
        list(
            Tarea.objects.select_for_update().filter(Q(pk__in=tareas_ids) | Q(tarea_padre_id__in=tareas_ids)).order_by("pk")
        )
    esquemas_ids = list(
        EsquemaAprobacionWorkflow.objects.filter(instancia_etapa__instancia_workflow_id__in=ids).values_list(
            "esquema_id", flat=True
        )
    )
    if esquemas_ids:
        list(EsquemaAprobacion.objects.select_for_update().filter(pk__in=esquemas_ids).order_by("pk"))
        list(Aprobacion.objects.select_for_update().filter(esquema_id__in=esquemas_ids).order_by("pk"))


def bloquear_ejecucion(instancia_id):
    """Toma los locks de una ejecución COMPLETA antes de cancelarla, en el orden único del módulo: la instancia
    (compuerta) y DESPUÉS sus Tareas y aprobaciones. Antes de 4.G3A el trabajo se bloqueaba primero y la
    instancia después, lo que dejaba una ventana: una Tarea creada entre medias (p. ej. por una
    materialización) se bloqueaba con la instancia ya tomada, en sentido contrario al de quien la completa.
    Devuelve la `InstanciaWorkflow` bloqueada. Debe llamarse dentro de una transacción."""
    instancia = bloquear_instancia(instancia_id)
    bloquear_trabajo_de_ejecucion(instancia_id)
    return instancia


@transaction.atomic
def cancelar_ejecucion(instancia, actor, *, motivo=""):
    """Detiene la ejecución de un Ticket cancelado y todo el trabajo que ella creó.

    - La `InstanciaWorkflow` pasa a CANCELADA (terminal): el motor ya no avanza, reanuda ni continúa una
      espera sobre ella, así que ningún aviso tardío (tarea, aprobación, entregable, Celery) la reactiva.
      Una ejecución ya COMPLETADA o CANCELADA no se toca. Las ejecuciones de etapa, sus resultados y las
      decisiones previas se conservan: nada se elimina ni se reinicia.
    - Las Tareas del flujo que siguen abiertas (y sus subtareas) pasan a CANCELADA; las COMPLETADAS se conservan.
    - Los esquemas de aprobación sin resultado retiran sus aprobaciones PENDIENTE (`anular_esquema_aprobacion`,
      el mecanismo existente); las ya decididas se conservan.

    Solo alcanza lo vinculado a ESTA instancia (`TareaWorkflow`, `EsquemaAprobacionWorkflow`): las tareas y
    aprobaciones independientes u de otros Tickets no se tocan. Idempotente. `actor` es quien cancela el Ticket."""
    instancia = bloquear_ejecucion(instancia.pk)
    if instancia.estado in (InstanciaWorkflow.Estado.COMPLETADA, InstanciaWorkflow.Estado.CANCELADA):
        return instancia

    _cancelar_trabajo([instancia.pk], actor, motivo)
    # Fase 2 — cancelación en cascada: las subinstancias abiertas (recorridos por ítem, ramas generales)
    # también se detienen; las terminadas se conservan tal cual.
    for hija in InstanciaWorkflow.objects.filter(instancia_padre=instancia).order_by("pk"):
        if hija.estado in (InstanciaWorkflow.Estado.COMPLETADA, InstanciaWorkflow.Estado.CANCELADA):
            continue
        _cancelar_trabajo([hija.pk], actor, motivo)
        hija.contexto = {**(hija.contexto or {}), "resultado_recorrido": "CANCELADO"}
        hija.estado = InstanciaWorkflow.Estado.CANCELADA
        hija.save(update_fields=["estado", "contexto", "actualizado_en"])

    estado_anterior = instancia.estado
    instancia.estado = InstanciaWorkflow.Estado.CANCELADA
    instancia.save(update_fields=["estado", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=instancia, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"estado": estado_anterior},
        datos_nuevos={"estado": instancia.estado, "motivo": (motivo or "").strip()},
    )
    return instancia


def _cancelar_trabajo(instancias_ids, actor, motivo):
    """Cancela las Tareas abiertas y retira las aprobaciones pendientes que crearon estas instancias."""
    for vinculo in (
        TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow_id__in=instancias_ids)
        .select_related("tarea").order_by("pk")
    ):
        cancelar_tarea(vinculo.tarea, actor, motivo=motivo)
    for vinculo in (
        EsquemaAprobacionWorkflow.objects.filter(instancia_etapa__instancia_workflow_id__in=instancias_ids)
        .select_related("esquema").order_by("pk")
    ):
        if vinculo.esquema.resultado is None:
            anular_esquema_aprobacion(vinculo.esquema, actor)


@transaction.atomic
def cancelar_subinstancia(hija, actor, *, resultado="CANCELADO", motivo=""):
    """Fase 2 — detiene UN recorrido (p. ej. su ítem salió del alcance): cancela sus Tareas abiertas y retira
    sus aprobaciones pendientes (lo decidido se conserva), la deja CANCELADA con su `resultado_recorrido` y
    avisa a la instancia principal, que puede continuar si era lo último pendiente. Los demás recorridos no se
    tocan. Idempotente: una subinstancia ya terminada no cambia. Quien llama autoriza."""
    from apps.workflows import recorridos

    hija = bloquear_instancia(hija.pk)
    if hija.instancia_padre_id is None:
        raise ValueError("Solo se cancela así una subinstancia.")
    if hija.estado in (InstanciaWorkflow.Estado.COMPLETADA, InstanciaWorkflow.Estado.CANCELADA):
        return hija
    bloquear_trabajo_de_ejecucion(hija.pk)
    _cancelar_trabajo([hija.pk], actor, motivo)
    estado_anterior = hija.estado
    hija.contexto = {**(hija.contexto or {}), "resultado_recorrido": resultado}
    hija.estado = InstanciaWorkflow.Estado.CANCELADA
    hija.save(update_fields=["estado", "contexto", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=hija, origen=RegistroAuditoria.Origen.USUARIO
        if actor is not None else RegistroAuditoria.Origen.SISTEMA, usuario=actor,
        datos_anteriores={"estado": estado_anterior},
        datos_nuevos={"estado": hija.estado, "resultado": resultado, "motivo": (motivo or "").strip()},
    )
    recorridos.evaluar_espera(hija.ejecucion_padre_id)
    return hija


@transaction.atomic
def reintentar_recorrido(hija, actor):
    """Fase 2 — reintenta la ejecución que dejó en ERROR a un recorrido (p. ej. el ítem no tenía responsable
    en su columna), después de corregir el dato. Autoriza quien edita los elementos del ticket
    (`puede_editar_parrilla`). Si el recorrido termina, la principal se reevalúa."""
    from apps.tickets.autorizacion import puede_editar_parrilla
    from apps.tickets.models import Ticket
    from apps.workflows.motor import reintentar_subinstancia_con_error

    hija = bloquear_instancia(hija.pk)
    ticket = Ticket.objects.select_for_update().get(instancia_workflow_id=hija.raiz_id)
    if not puede_editar_parrilla(actor, ticket):
        raise PermissionDenied("Solo quien atiende el ticket puede reintentar un recorrido.")
    if ticket.estado == Ticket.Estado.CANCELADO:
        raise ValidationError("Este ticket fue cancelado: su flujo no se reactiva.")
    hija = reintentar_subinstancia_con_error(hija)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=hija, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"estado": "ERROR"}, datos_nuevos={"estado": hija.estado, "reintento": True},
    )
    # Si el recorrido terminó, el motor ya avisó a la principal (`motor._avisar_padre`).
    return hija
