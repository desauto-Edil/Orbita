"""Operaciones de dominio de las gacetas — Fase 3 del modelo configurable de procesos.

Reutiliza, sin duplicar: los ítems y sus versiones (`apps.tickets.parrilla`: `VersionElemento` con huella), el
alcance (`DecisionAlcanceElemento`, origen GACETA), las aprobaciones (`apps.aprobaciones`: un esquema por ronda,
modalidad TODOS / CUALQUIERA, decisión EXCLUIDA cuando la gaceta la permite), las tareas (`apps.tareas`: la
corrección es una Tarea del responsable), los cálculos de fechas (`apps.tickets.tiempos`) y la auditoría. La
lógica PROPIA de la gaceta es la de observaciones, correcciones, rondas, periodo y resolución.

Ciclo de un ítem en una gaceta (ESTADO DE GACETA, independiente de su recorrido operativo):

    presentar → EN_REVISION (ronda 1: versión + esquema)
        observación de un revisor/aprobador, o DEVOLVER → EN_CORRECCION (tarea al responsable, plazo)
            reenviar (nueva versión) → EN_REVISION (ronda n+1, nuevo esquema; las observaciones quedan atendidas)
        APROBAR → APROBADA · RECHAZAR → RECHAZADA · EXCLUIR → EXCLUIDA (las dos últimas sacan al ítem del alcance)
    al cerrar el periodo (vencido) con aceptación automática: EN_REVISION sin observaciones pendientes → APROBADA
    al resolver la gaceta: lo pendiente se resuelve según la regla (IMPEDIR / EXCLUIR / RECHAZAR); nunca se
    aprueba automáticamente un ítem en corrección.

Orden de locks (el del proyecto): instancia principal del flujo → Ticket → (colección) → gaceta → participación →
aprobaciones → tareas. Todas las operaciones públicas empiezan por `_bloquear`. Idempotencia: presentar dos
veces no duplica (única por gaceta+ítem); cerrar, resolver, cancelar y los avisos revalidan el estado bajo lock.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.auditoria import registrar_evento
from apps.core.models import Equipo, RegistroAuditoria, Usuario
from apps.gacetas import autorizacion
from apps.gacetas.models import GacetaTicket, ObservacionGaceta, ParticipacionItemGaceta, RondaRevisionGaceta
from apps.tickets.models import ElementoParrilla, Ticket
from apps.workflows.models import InstanciaWorkflow

Estado = ParticipacionItemGaceta.Estado
ESTADOS_TICKET_OPERATIVOS = (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)
SALIDA_POR_ESTADO = {Estado.RECHAZADA: "RECHAZADO", Estado.EXCLUIDA: "EXCLUIDO"}


def _auditar(instancia, actor, accion, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=instancia,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _bloquear(gaceta_id):
    """Locks en el orden del proyecto. Devuelve `(ticket, gaceta)` bloqueados."""
    ticket_id, instancia_id = GacetaTicket.objects.values_list("ticket_id", "ticket__instancia_workflow_id").get(pk=gaceta_id)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    gaceta = GacetaTicket.objects.select_for_update().get(pk=gaceta_id)
    return ticket, gaceta


def _bloquear_participacion(participacion_id):
    gaceta_id = ParticipacionItemGaceta.objects.values_list("gaceta_id", flat=True).get(pk=participacion_id)
    ticket, gaceta = _bloquear(gaceta_id)
    participacion = ParticipacionItemGaceta.objects.select_for_update().select_related("elemento__parrilla").get(
        pk=participacion_id
    )
    participacion.gaceta = gaceta
    return ticket, gaceta, participacion


def _plazo(desde, plazo):
    if not plazo:
        return None
    from apps.tickets import tiempos

    return tiempos.calcular_fecha_objetivo(desde, plazo["cantidad"], plazo["unidad"], plazo.get("habiles", False))


# --- Nacimiento y apertura ---------------------------------------------------------------------------------


def materializar(ticket, servicio):
    """Al crear el ticket (BORRADOR) congela una `GacetaTicket` por cada gaceta ACTIVA del Proceso, con su
    configuración y versión. Cambiar el Studio después no altera este ticket. Sin gacetas no crea nada."""
    from apps.catalogo.gacetas import definiciones_activas, normalizar

    creadas = []
    if servicio.tipo != "PROCESO":
        return creadas
    for definicion in definiciones_activas(servicio):
        configuracion, _ = normalizar(definicion.configuracion)
        creadas.append(GacetaTicket.objects.create(
            ticket=ticket, definicion=definicion, clave=definicion.clave, nombre=definicion.nombre,
            descripcion=definicion.descripcion, version_definicion=definicion.version, configuracion=configuracion,
        ))
    return creadas


def _abrir_bloqueada(gaceta, actor=None):
    if gaceta.estado != GacetaTicket.Estado.PENDIENTE:
        return gaceta
    ahora = timezone.now()
    gaceta.estado = GacetaTicket.Estado.ABIERTA
    gaceta.abierta_en = ahora
    gaceta.vence_revision_en = _plazo(ahora, gaceta.configuracion.get("plazo_revision"))
    gaceta.save(update_fields=["estado", "abierta_en", "vence_revision_en", "actualizado_en"])
    _auditar(gaceta, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": "PENDIENTE"},
             {"estado": gaceta.estado, "vence_revision_en": gaceta.vence_revision_en})
    return gaceta


@transaction.atomic
def abrir(gaceta, actor=None):
    _ticket, gaceta = _bloquear(gaceta.pk)
    return _abrir_bloqueada(gaceta, actor)


def abrir_al_iniciar(ticket):
    """Al radicar: abre las gacetas configuradas para abrirse al iniciar (las demás las abre un bloque GACETA).
    Se llama dentro de la transacción de la radicación (ticket ya bloqueado)."""
    for gaceta in GacetaTicket.objects.select_for_update().filter(ticket=ticket, estado=GacetaTicket.Estado.PENDIENTE):
        if gaceta.configuracion.get("apertura", "AL_INICIAR") == "AL_INICIAR":
            _abrir_bloqueada(gaceta)


# --- Ingreso progresivo ------------------------------------------------------------------------------------


def _motivo_para_no_ingresar(gaceta, elemento):
    """Por qué `elemento` no puede ingresar a `gaceta` (texto) o `None`."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.workflows.recorridos import _cumple_ingreso

    cfg = gaceta.configuracion or {}
    if gaceta.estado != GacetaTicket.Estado.ABIERTA:
        return "La gaceta no está abierta para recibir ítems."
    if elemento.parrilla.ticket_id != gaceta.ticket_id:
        return "El ítem no es de este ticket."
    if elemento.parrilla.clave not in (cfg.get("colecciones") or []):
        return "La colección del ítem no participa en esta gaceta."
    if elemento.retirado_en is not None or parrilla_ops.decision_de_salida(elemento) is not None:
        return "El ítem ya no está en el alcance."
    if not _cumple_ingreso(elemento, cfg.get("condicion_ingreso")):
        return "El ítem no cumple la condición de ingreso de la gaceta."
    if cfg.get("exige_item_completo", True) and not parrilla_ops.completitud_de(elemento).completo:
        return "El ítem todavía está incompleto."
    return None


def _esquema_de(gaceta, actor):
    """Esquema de aprobación de una ronda (uno por ronda, `apps.aprobaciones`)."""
    from apps.aprobaciones.models import Aprobacion
    from apps.aprobaciones.operaciones import crear_esquema_aprobacion

    cfg = gaceta.configuracion or {}
    participantes = []
    for dato in cfg.get("aprobadores") or []:
        if dato["tipo"] == "USUARIO":
            usuario = Usuario.objects.filter(pk=dato["usuario_id"], is_active=True).first()
            if usuario is not None:
                participantes.append((Aprobacion.TipoAprobador.USUARIO, usuario))
        else:
            equipo = Equipo.objects.filter(pk=dato["equipo_id"], activo=True).first()
            if equipo is not None:
                participantes.append((Aprobacion.TipoAprobador.EQUIPO, equipo))
    if not participantes:
        raise ValidationError(f"La gaceta «{gaceta.nombre}» no tiene aprobadores activos.")
    varios = len(participantes) > 1
    return crear_esquema_aprobacion(
        modo="PARALELA" if varios else "SECUENCIAL", politica=cfg.get("politica", "TODOS") if varios else None,
        participantes=participantes, actor=actor, permite_exclusion=bool(cfg.get("permite_exclusion")),
        justificacion_al_aprobar=bool(cfg.get("justificacion_al_aprobar")),
    )


def _abrir_ronda(participacion, actor, motivo):
    from apps.tickets.parrilla import _registrar_version

    version = _registrar_version(participacion.elemento, actor, motivo)
    return RondaRevisionGaceta.objects.create(
        participacion=participacion, numero=participacion.ronda, version=version,
        esquema=_esquema_de(participacion.gaceta, actor),
    )


def _presentar_bloqueado(gaceta, elemento, actor, *, origen, silencioso):
    existente = ParticipacionItemGaceta.objects.filter(gaceta=gaceta, elemento=elemento).first()
    if existente is not None:
        return existente  # idempotente: un reintento no duplica la participación
    motivo = _motivo_para_no_ingresar(gaceta, elemento)
    if motivo:
        if silencioso:
            return None
        raise ValidationError(motivo)
    participacion = ParticipacionItemGaceta.objects.create(
        gaceta=gaceta, elemento=elemento, origen=origen, presentada_por=actor,
    )
    participacion.gaceta = gaceta
    ronda = _abrir_ronda(participacion, actor, f"Presentado a «{gaceta.nombre}»")
    _auditar(participacion, actor, RegistroAuditoria.Accion.CREAR, None, {
        "gaceta": gaceta.clave, "elemento_id": elemento.pk, "numero": elemento.numero, "origen": origen,
        "version": ronda.version.numero if ronda.version else None,
    })
    return participacion


@transaction.atomic
def presentar(gaceta, elemento, actor):
    """Presentación MANUAL de un ítem por el coordinador. Idempotente."""
    _ticket, gaceta = _bloquear(gaceta.pk)
    if not autorizacion.puede_coordinar(actor, gaceta):
        raise PermissionDenied("Solo quien coordina la gaceta presenta ítems.")
    elemento = ElementoParrilla.objects.select_related("parrilla").get(pk=elemento.pk)
    return _presentar_bloqueado(gaceta, elemento, actor, origen=ParticipacionItemGaceta.Origen.MANUAL, silencioso=False)


def ingresar_automaticamente(elemento, actor=None, *, ingreso):
    """Ingreso PROGRESIVO: el ítem entra a cada gaceta ABIERTA de su ticket configurada con ese `ingreso`
    (AL_CUMPLIR_CONDICION o AL_TERMINAR_RECORRIDO) si cumple sus requisitos; si no, se ignora en silencio (volverá
    a evaluarse en el próximo cambio). Se llama desde las operaciones de la colección y desde el motor, con el
    ticket ya bloqueado. Idempotente. Devuelve las participaciones creadas."""
    elemento = ElementoParrilla.objects.select_related("parrilla").get(pk=elemento.pk)
    creadas = []
    for gaceta in GacetaTicket.objects.select_for_update().filter(
        ticket_id=elemento.parrilla.ticket_id, estado=GacetaTicket.Estado.ABIERTA
    ).order_by("pk"):
        cfg = gaceta.configuracion or {}
        if cfg.get("ingreso") != ingreso or elemento.parrilla.clave not in (cfg.get("colecciones") or []):
            continue
        if ParticipacionItemGaceta.objects.filter(gaceta=gaceta, elemento=elemento).exists():
            continue
        participacion = _presentar_bloqueado(
            gaceta, elemento, actor, origen=ParticipacionItemGaceta.Origen.AUTOMATICO, silencioso=True
        )
        if participacion is not None:
            creadas.append(participacion)
    return creadas


def presentar_por_flujo(gaceta):
    """El bloque GACETA presenta todos los ítems en el alcance que cumplan los requisitos (dentro del motor, con
    la instancia bloqueada). Idempotente."""
    from apps.tickets import parrilla as parrilla_ops

    gaceta = GacetaTicket.objects.select_for_update().get(pk=gaceta.pk)
    for parrilla in parrilla_ops.parrillas_de(gaceta.ticket):
        if parrilla.clave not in (gaceta.configuracion.get("colecciones") or []):
            continue
        for elemento in parrilla_ops.elementos_en_alcance(parrilla):
            _presentar_bloqueado(gaceta, elemento, None, origen=ParticipacionItemGaceta.Origen.FLUJO, silencioso=True)


# --- Observaciones y correcciones --------------------------------------------------------------------------


def _ronda_actual(participacion):
    return RondaRevisionGaceta.objects.select_for_update().get(participacion=participacion, numero=participacion.ronda)


def _cerrar_esquema(ronda, actor):
    from apps.aprobaciones.models import EsquemaAprobacion
    from apps.aprobaciones.operaciones import anular_esquema_aprobacion

    if ronda.esquema_id is None:
        return
    esquema = EsquemaAprobacion.objects.get(pk=ronda.esquema_id)
    if esquema.resultado is None:
        anular_esquema_aprobacion(esquema, actor)


def _responsable_de_correccion(participacion):
    cfg = participacion.gaceta.configuracion or {}
    columna = cfg.get("responsable_correccion_columna") or ""
    if columna:
        valor = participacion.elemento.valores.select_related("campo", "valor_usuario", "valor_equipo").filter(
            campo__clave=columna
        ).first()
        if valor is not None and valor.valor_usuario_id and valor.valor_usuario.is_active:
            return valor.valor_usuario, None
        if valor is not None and valor.valor_equipo_id and valor.valor_equipo.activo:
            return None, valor.valor_equipo
    ticket = participacion.gaceta.ticket
    return ticket.usuario_responsable, (ticket.equipo_responsable if ticket.usuario_responsable_id is None else None)


def _enviar_a_correccion(participacion, ronda, actor, *, resultado_ronda):
    """La ronda se cierra (su esquema, si sigue abierto, se retira), el ítem pasa a EN_CORRECCION y su
    responsable recibe una Tarea de corrección con el plazo configurado."""
    from apps.tareas.models import Tarea
    from apps.tareas.operaciones import crear_tarea

    _cerrar_esquema(ronda, actor)
    ahora = timezone.now()
    gaceta, elemento = participacion.gaceta, participacion.elemento
    participacion.estado = Estado.EN_CORRECCION
    participacion.vence_correccion_en = _plazo(ahora, gaceta.configuracion.get("plazo_correccion"))
    participacion.correccion_vencida_en = None
    participacion.save(update_fields=["estado", "vence_correccion_en", "correccion_vencida_en", "actualizado_en"])
    usuario, equipo = _responsable_de_correccion(participacion)
    observaciones = "\n".join(
        f"- {o.texto}" for o in participacion.observaciones.filter(atendida_en__isnull=True).order_by("creada_en")
    )
    tarea = crear_tarea(
        titulo=f"Corregir {elemento.parrilla.nombre_elemento or 'ítem'} #{elemento.numero} — {gaceta.nombre}"[:200],
        descripcion=f"Observaciones de la gaceta «{gaceta.nombre}»:\n{observaciones}",
        origen=Tarea.Origen.SISTEMA, usuario_responsable=usuario, equipo_responsable=equipo,
        fecha_limite=participacion.vence_correccion_en,
    )
    ronda.resultado = resultado_ronda
    ronda.cerrada_en = ahora
    ronda.tarea_correccion = tarea
    ronda.save(update_fields=["resultado", "cerrada_en", "tarea_correccion"])
    _auditar(participacion, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": "EN_REVISION"},
             {"estado": participacion.estado, "ronda": ronda.numero, "tarea_id": tarea.pk})


@transaction.atomic
def observar(participacion, actor, texto):
    """Un revisor o aprobador registra una observación (solo con el periodo abierto). Si el ítem estaba en
    revisión, pasa a corrección y vuelve a su responsable; si ya estaba en corrección, la observación se suma."""
    texto = (texto or "").strip()
    if not texto:
        raise ValidationError("La observación necesita un texto.")
    _ticket, gaceta, participacion = _bloquear_participacion(participacion.pk)
    if not autorizacion.puede_observar(actor, participacion):
        raise PermissionDenied("No puedes registrar observaciones en este ítem ahora.")
    ronda = _ronda_actual(participacion)
    observacion = ObservacionGaceta.objects.create(participacion=participacion, ronda=ronda, autor=actor, texto=texto)
    _auditar(observacion, actor, RegistroAuditoria.Accion.CREAR, None,
             {"participacion_id": participacion.pk, "ronda": ronda.numero, "texto": texto})
    if participacion.estado == Estado.EN_REVISION:
        _enviar_a_correccion(participacion, ronda, actor, resultado_ronda=RondaRevisionGaceta.Resultado.OBSERVADA)
    return observacion


@transaction.atomic
def responder_observacion(observacion, actor, respuesta):
    respuesta = (respuesta or "").strip()
    if not respuesta:
        raise ValidationError("La respuesta necesita un texto.")
    _ticket, _gaceta, participacion = _bloquear_participacion(observacion.participacion_id)
    if not autorizacion.puede_corregir(actor, participacion):
        raise PermissionDenied("Solo el responsable de la corrección responde las observaciones.")
    observacion = ObservacionGaceta.objects.select_for_update().get(pk=observacion.pk)
    if observacion.atendida_en is not None:
        raise ValidationError("Esta observación ya fue atendida.")
    anterior = observacion.respuesta
    observacion.respuesta, observacion.respondida_por, observacion.respondida_en = respuesta, actor, timezone.now()
    observacion.save(update_fields=["respuesta", "respondida_por", "respondida_en"])
    _auditar(observacion, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"respuesta": anterior}, {"respuesta": respuesta})
    return observacion


def _cerrar_tarea_de_correccion(ronda, actor, motivo):
    from apps.tareas.autorizacion import puede_completar_tarea
    from apps.tareas.models import Tarea
    from apps.tareas.operaciones import cancelar_tarea, completar_tarea

    tarea = ronda.tarea_correccion
    if tarea is None or tarea.estado in Tarea.ESTADOS_CERRADOS:
        return
    if actor is not None and puede_completar_tarea(actor, tarea):
        completar_tarea(tarea, actor)
    else:
        cancelar_tarea(tarea, actor, motivo=motivo)


@transaction.atomic
def reenviar(participacion, actor, comentario=""):
    """El responsable reenvía la corrección: se registra una NUEVA VERSIÓN del ítem (las anteriores se conservan),
    las observaciones quedan atendidas, la tarea de corrección se cierra y se abre una nueva ronda de revisión
    en la MISMA gaceta (no se crea otra)."""
    _ticket, gaceta, participacion = _bloquear_participacion(participacion.pk)
    if gaceta.estado in GacetaTicket.ESTADOS_FINALES:
        raise ValidationError("La gaceta ya está resuelta.")
    if participacion.estado != Estado.EN_CORRECCION:
        raise ValidationError("El ítem no está en corrección.")
    if not autorizacion.puede_corregir(actor, participacion):
        raise PermissionDenied("Solo el responsable de la corrección puede reenviarla.")
    anterior = _ronda_actual(participacion)
    ahora = timezone.now()
    participacion.observaciones.filter(atendida_en__isnull=True).update(atendida_en=ahora)
    _cerrar_tarea_de_correccion(anterior, actor, "Corrección reenviada a revisión.")
    participacion.ronda += 1
    participacion.estado = Estado.EN_REVISION
    participacion.vence_correccion_en = None
    participacion.correccion_vencida_en = None
    participacion.save(update_fields=["ronda", "estado", "vence_correccion_en", "correccion_vencida_en", "actualizado_en"])
    motivo = f"Corrección para «{gaceta.nombre}» (ronda {participacion.ronda})"
    if (comentario or "").strip():
        motivo = f"{motivo}: {comentario.strip()}"
    ronda = _abrir_ronda(participacion, actor, motivo)
    _auditar(participacion, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": "EN_CORRECCION"},
             {"estado": participacion.estado, "ronda": ronda.numero, "version": ronda.version.numero})
    return participacion


@transaction.atomic
def reenviar_por_tarea(tarea, actor):
    """Completar la tarea de corrección desde Tareas es reenviar la corrección (una sola operación)."""
    ronda = RondaRevisionGaceta.objects.get(tarea_correccion=tarea)
    return reenviar(ronda.participacion, actor)


# --- Decisiones ---------------------------------------------------------------------------------------------


def _finalizar(participacion, actor, estado, justificacion=""):
    participacion.estado = estado
    participacion.decidida_en = timezone.now()
    participacion.decidida_por = actor
    participacion.justificacion = justificacion or ""
    participacion.vence_correccion_en = None
    participacion.save(update_fields=[
        "estado", "decidida_en", "decidida_por", "justificacion", "vence_correccion_en", "actualizado_en",
    ])
    if estado in SALIDA_POR_ESTADO:
        # Rechazo y exclusión sacan al ítem del alcance (origen GACETA, sin otra aprobación); su recorrido en curso
        # se detiene y las demás gacetas lo cierran con el mismo resultado. Nunca se borra.
        from apps.tickets.parrilla import registrar_salida_por_flujo

        registrar_salida_por_flujo(
            participacion.elemento, actor, tipo=SALIDA_POR_ESTADO[estado],
            justificacion=justificacion or f"{participacion.get_estado_display()} en la gaceta «{participacion.gaceta.nombre}».",
            origen="GACETA", detener_recorrido=True,
        )
    _auditar(participacion, actor, RegistroAuditoria.Accion.ACTUALIZAR, None,
             {"estado": estado, "justificacion": justificacion, "gaceta": participacion.gaceta.clave})


def _decision_que_cerro(esquema):
    return esquema.participaciones.filter(estado=esquema.resultado).order_by("decidida_en", "pk").first()


@transaction.atomic
def resolver_aprobacion_gaceta(aprobacion, actor, *, decision, observacion=""):
    """Única forma de decidir la aprobación de una ronda de gaceta: bloquea la gaceta y la participación ANTES
    del esquema (orden del proyecto), decide con `apps.aprobaciones` (TODOS/CUALQUIERA, precedencia RECHAZADA >
    EXCLUIDA > DEVUELTA > APROBADA) y, si el esquema cerró, aplica el resultado SOLO a este ítem."""
    from apps.aprobaciones.operaciones import resolver_aprobacion

    ronda = RondaRevisionGaceta.objects.select_related("participacion").get(esquema_id=aprobacion.esquema_id)
    _ticket, gaceta, participacion = _bloquear_participacion(ronda.participacion_id)
    aprobacion, esquema = resolver_aprobacion(aprobacion, actor, decision=decision, observacion=observacion)
    if esquema.resultado is None:
        return aprobacion, esquema
    ronda = RondaRevisionGaceta.objects.select_for_update().get(pk=ronda.pk)
    if ronda.numero != participacion.ronda or participacion.estado != Estado.EN_REVISION:
        return aprobacion, esquema  # ronda vieja: el ítem ya cambió de estado por otra vía
    decisiva = _decision_que_cerro(esquema)
    autor = decisiva.decidido_por if decisiva is not None else actor
    texto = (decisiva.observacion if decisiva is not None else observacion) or ""
    if esquema.resultado == "DEVUELTA":
        ObservacionGaceta.objects.create(
            participacion=participacion, ronda=ronda, autor=autor, texto=texto or "Devuelto para corrección.",
            origen=ObservacionGaceta.Origen.DECISION,
        )
        _enviar_a_correccion(participacion, ronda, autor, resultado_ronda=RondaRevisionGaceta.Resultado.DEVUELTA)
    else:
        ronda.resultado = esquema.resultado
        ronda.cerrada_en = timezone.now()
        ronda.save(update_fields=["resultado", "cerrada_en"])
        _finalizar(participacion, autor, esquema.resultado, texto)
    _resolver_si_corresponde(gaceta)
    return aprobacion, esquema


@transaction.atomic
def excluir_directo(participacion, actor, justificacion):
    """Exclusión por un actor autorizado (el coordinador), sin otra aprobación, con justificación."""
    justificacion = (justificacion or "").strip()
    if not justificacion:
        raise ValidationError("La exclusión necesita una justificación.")
    _ticket, gaceta, participacion = _bloquear_participacion(participacion.pk)
    if not autorizacion.puede_coordinar(actor, gaceta):
        raise PermissionDenied("Solo quien coordina la gaceta puede excluir un ítem.")
    if participacion.es_final:
        raise ValidationError("El ítem ya tiene un resultado en esta gaceta.")
    ronda = _ronda_actual(participacion)
    _cerrar_esquema(ronda, actor)
    _cerrar_tarea_de_correccion(ronda, None, "El ítem se excluyó de la gaceta.")
    ronda.resultado = RondaRevisionGaceta.Resultado.EXCLUIDA
    ronda.cerrada_en = timezone.now()
    ronda.save(update_fields=["resultado", "cerrada_en"])
    _finalizar(participacion, actor, Estado.EXCLUIDA, justificacion)
    _resolver_si_corresponde(gaceta)
    return participacion


def al_salir_del_alcance(elemento, actor, *, tipo, justificacion):
    """El ítem salió del alcance por OTRA vía (decisión manual, otra gaceta, un recorrido, retiro): cada
    participación suya aún pendiente queda cerrada con ese resultado (su esquema se retira y su tarea de
    corrección se cancela). Se llama con el ticket bloqueado. No vuelve a registrar la salida del alcance."""
    estado = Estado.RECHAZADA if tipo == "RECHAZADO" else Estado.EXCLUIDA
    for participacion in ParticipacionItemGaceta.objects.select_for_update().select_related("gaceta").filter(
        elemento=elemento, estado__in=ParticipacionItemGaceta.ESTADOS_PENDIENTES
    ):
        ronda = _ronda_actual(participacion)
        _cerrar_esquema(ronda, actor)
        _cerrar_tarea_de_correccion(ronda, None, "El ítem salió del alcance.")
        ronda.resultado = RondaRevisionGaceta.Resultado.CANCELADA
        ronda.cerrada_en = timezone.now()
        ronda.save(update_fields=["resultado", "cerrada_en"])
        participacion.estado = estado
        participacion.decidida_en = timezone.now()
        participacion.decidida_por = actor
        participacion.justificacion = f"Salió del alcance: {justificacion}"
        participacion.save(update_fields=["estado", "decidida_en", "decidida_por", "justificacion", "actualizado_en"])
        _auditar(participacion, actor, RegistroAuditoria.Accion.ACTUALIZAR, None,
                 {"estado": estado, "motivo": "salida del alcance"})


# --- Periodo, vencimientos y resolución ----------------------------------------------------------------------


def _aceptar_automaticamente(gaceta):
    """Al vencer el periodo con aceptación automática: cada ítem EN_REVISION sin observaciones pendientes se
    acepta. Uno en corrección NUNCA se acepta automáticamente."""
    if not gaceta.configuracion.get("aceptacion_automatica"):
        return 0
    if gaceta.vence_revision_en is None or gaceta.vence_revision_en > timezone.now():
        return 0
    aceptadas = 0
    for participacion in ParticipacionItemGaceta.objects.select_for_update().select_related("elemento").filter(
        gaceta=gaceta, estado=Estado.EN_REVISION
    ):
        if participacion.observaciones.filter(atendida_en__isnull=True).exists():
            continue
        participacion.gaceta = gaceta
        ronda = _ronda_actual(participacion)
        _cerrar_esquema(ronda, None)
        ronda.resultado = RondaRevisionGaceta.Resultado.ACEPTADA_AUTOMATICA
        ronda.cerrada_en = timezone.now()
        ronda.save(update_fields=["resultado", "cerrada_en"])
        participacion.aceptada_automaticamente = True
        participacion.save(update_fields=["aceptada_automaticamente", "actualizado_en"])
        _finalizar(participacion, None, Estado.APROBADA, "Aceptado automáticamente al vencer el periodo de revisión.")
        aceptadas += 1
    return aceptadas


@transaction.atomic
def cerrar_periodo(gaceta, actor=None):
    """Cierra la ventana de observaciones ordinarias (manual por el coordinador, o el Sistema al vencer). NO
    implica que todo esté aprobado: lo pendiente sigue hasta la resolución. Idempotente."""
    _ticket, gaceta = _bloquear(gaceta.pk)
    if actor is not None and not autorizacion.puede_coordinar(actor, gaceta):
        raise PermissionDenied("Solo quien coordina la gaceta cierra el periodo de revisión.")
    if gaceta.estado != GacetaTicket.Estado.ABIERTA:
        return gaceta
    gaceta.estado = GacetaTicket.Estado.PERIODO_CERRADO
    gaceta.periodo_cerrado_en = timezone.now()
    gaceta.save(update_fields=["estado", "periodo_cerrado_en", "actualizado_en"])
    aceptadas = _aceptar_automaticamente(gaceta)
    _auditar(gaceta, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": "ABIERTA"},
             {"estado": gaceta.estado, "aceptadas_automaticamente": aceptadas})
    _resolver_si_corresponde(gaceta)
    return gaceta


def pendientes_de(gaceta):
    return list(
        ParticipacionItemGaceta.objects.select_related("elemento__parrilla").filter(
            gaceta=gaceta, estado__in=ParticipacionItemGaceta.ESTADOS_PENDIENTES
        )
    )


def resumen(gaceta):
    cuenta = {estado: 0 for estado, _ in Estado.choices}
    for estado in ParticipacionItemGaceta.objects.filter(gaceta=gaceta).values_list("estado", flat=True):
        cuenta[estado] += 1
    return {
        "total": sum(cuenta.values()),
        "aprobados": cuenta[Estado.APROBADA], "rechazados": cuenta[Estado.RECHAZADA],
        "excluidos": cuenta[Estado.EXCLUIDA], "en_revision": cuenta[Estado.EN_REVISION],
        "en_correccion": cuenta[Estado.EN_CORRECCION], "resuelta": gaceta.estado == GacetaTicket.Estado.RESUELTA,
    }


def _resolver_bloqueada(gaceta, actor, justificacion, *, automatica):
    regla = gaceta.configuracion.get("al_resolver_pendientes", "IMPEDIR")
    pendientes = pendientes_de(gaceta)
    if pendientes and regla == "IMPEDIR":
        raise ValidationError(
            "Hay ítems pendientes en la gaceta: "
            + ", ".join(f"#{p.elemento.numero}" for p in pendientes)
            + ". Decide sobre ellos antes de resolverla."
        )
    estado_final = Estado.EXCLUIDA if regla == "EXCLUIR" else Estado.RECHAZADA
    texto = f"Resolución de la gaceta «{gaceta.nombre}»" + (f": {justificacion}" if justificacion else ".")
    for participacion in pendientes:
        participacion = ParticipacionItemGaceta.objects.select_for_update().select_related("elemento__parrilla").get(pk=participacion.pk)
        participacion.gaceta = gaceta
        ronda = _ronda_actual(participacion)
        _cerrar_esquema(ronda, actor)
        _cerrar_tarea_de_correccion(ronda, None, "La gaceta se resolvió.")
        ronda.resultado = RondaRevisionGaceta.Resultado.RESUELTA
        ronda.cerrada_en = timezone.now()
        ronda.save(update_fields=["resultado", "cerrada_en"])
        _finalizar(participacion, actor, estado_final, texto)
    gaceta.estado = GacetaTicket.Estado.RESUELTA
    gaceta.resuelta_en = timezone.now()
    gaceta.resuelta_por = actor
    gaceta.resolucion = {**resumen(gaceta), "resuelta": True, "automatica": automatica, "justificacion": justificacion}
    gaceta.save(update_fields=["estado", "resuelta_en", "resuelta_por", "resolucion", "actualizado_en"])
    _auditar(gaceta, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": "PERIODO_CERRADO"}, gaceta.resolucion)
    from apps.gacetas.integracion import continuar_por_gaceta

    continuar_por_gaceta(gaceta)
    return gaceta


@transaction.atomic
def resolver(gaceta, actor, justificacion=""):
    """RESOLUCIÓN manual por el coordinador: fija el resultado definitivo de lo pendiente según la regla de la
    gaceta (IMPEDIR rechaza resolver con pendientes). Si el periodo seguía abierto, primero se cierra."""
    _ticket, gaceta = _bloquear(gaceta.pk)
    if not autorizacion.puede_coordinar(actor, gaceta):
        raise PermissionDenied("Solo quien coordina la gaceta la resuelve.")
    if gaceta.estado in GacetaTicket.ESTADOS_FINALES:
        raise ValidationError("La gaceta ya está resuelta o cancelada.")
    if gaceta.estado == GacetaTicket.Estado.PENDIENTE:
        raise ValidationError("La gaceta todavía no se abrió.")
    if gaceta.estado == GacetaTicket.Estado.ABIERTA:
        gaceta.estado = GacetaTicket.Estado.PERIODO_CERRADO
        gaceta.periodo_cerrado_en = timezone.now()
        gaceta.save(update_fields=["estado", "periodo_cerrado_en", "actualizado_en"])
    return _resolver_bloqueada(gaceta, actor, (justificacion or "").strip(), automatica=False)


def _resolver_si_corresponde(gaceta):
    """Resolución AUTOMÁTICA (si la gaceta la permite) cuando el periodo ya cerró y: no queda nada pendiente, o
    lo único pendiente son correcciones vencidas y la regla dice qué hacer con ellas. Nunca con ítems esperando
    a sus aprobadores."""
    if gaceta.estado != GacetaTicket.Estado.PERIODO_CERRADO or not gaceta.configuracion.get("resolucion_automatica"):
        return None
    pendientes = pendientes_de(gaceta)
    regla = gaceta.configuracion.get("al_resolver_pendientes", "IMPEDIR")
    if pendientes:
        ahora = timezone.now()
        vencidas = all(
            p.estado == Estado.EN_CORRECCION and p.vence_correccion_en is not None and p.vence_correccion_en <= ahora
            for p in pendientes
        )
        if regla == "IMPEDIR" or not vencidas:
            return None
    return _resolver_bloqueada(
        gaceta, None, "Resolución automática al cerrar el periodo." if not pendientes else
        "Resolución automática: vencieron los plazos de corrección.", automatica=True,
    )


@transaction.atomic
def marcar_correccion_vencida(participacion):
    """El plazo de corrección venció: queda registrado (visible y auditado). No decide nada por sí solo."""
    _ticket, gaceta, participacion = _bloquear_participacion(participacion.pk)
    if (
        participacion.estado != Estado.EN_CORRECCION or participacion.correccion_vencida_en is not None
        or participacion.vence_correccion_en is None or participacion.vence_correccion_en > timezone.now()
    ):
        return participacion
    participacion.correccion_vencida_en = timezone.now()
    participacion.save(update_fields=["correccion_vencida_en", "actualizado_en"])
    _auditar(participacion, None, RegistroAuditoria.Accion.ACTUALIZAR, None, {"correccion_vencida": True})
    _resolver_si_corresponde(gaceta)
    return participacion


@transaction.atomic
def resolver_si_corresponde(gaceta):
    _ticket, gaceta = _bloquear(gaceta.pk)
    return _resolver_si_corresponde(gaceta)


def cancelar_del_ticket(ticket, actor, motivo=""):
    """Al cancelar el ticket (con el ticket bloqueado): sus gacetas quedan CANCELADAS, las aprobaciones pendientes
    se retiran y las tareas de corrección abiertas se cancelan. Lo decidido se conserva."""
    for gaceta in GacetaTicket.objects.select_for_update().filter(ticket=ticket).exclude(estado__in=GacetaTicket.ESTADOS_FINALES):
        for participacion in ParticipacionItemGaceta.objects.select_for_update().filter(
            gaceta=gaceta, estado__in=ParticipacionItemGaceta.ESTADOS_PENDIENTES
        ):
            participacion.gaceta = gaceta
            ronda = _ronda_actual(participacion)
            _cerrar_esquema(ronda, actor)
            _cerrar_tarea_de_correccion(ronda, None, motivo or "El ticket se canceló.")
        anterior = gaceta.estado
        gaceta.estado = GacetaTicket.Estado.CANCELADA
        gaceta.save(update_fields=["estado", "actualizado_en"])
        _auditar(gaceta, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"estado": anterior}, {"estado": gaceta.estado})


# --- Consultas -----------------------------------------------------------------------------------------------


def estado_operativo(elemento):
    """Situación del ítem en su recorrido (independiente de la gaceta)."""
    from apps.workflows.recorridos import resultado_de

    hija = elemento.recorridos.order_by("-pk").first()
    if hija is None:
        return "Sin recorrido"
    from apps.tickets.trabajo import ESTADO_RECORRIDO

    resultado = resultado_de(hija)
    return ESTADO_RECORRIDO.get(resultado, resultado)


def resultado_de_item(elemento):
    """Resultado FINAL del ítem para las reglas de finalización: EXCLUIDO / RECHAZADO (salió del alcance),
    PENDIENTE (alguna gaceta lo tiene en revisión o corrección), APROBADO (aprobado en sus gacetas), COMPLETADO
    (sin gacetas, terminó su recorrido) o SIN_RESULTADO."""
    from apps.tickets.parrilla import decision_de_salida
    from apps.workflows.recorridos import COMPLETADO, resultado_de

    salida = decision_de_salida(elemento)
    if salida is not None:
        return salida.tipo
    estados = list(
        ParticipacionItemGaceta.objects.filter(elemento=elemento).exclude(gaceta__estado=GacetaTicket.Estado.CANCELADA)
        .values_list("estado", flat=True)
    )
    if any(e in ParticipacionItemGaceta.ESTADOS_PENDIENTES for e in estados):
        return "PENDIENTE"
    if estados and all(e == Estado.APROBADA for e in estados):
        return "APROBADO"
    hija = elemento.recorridos.order_by("-pk").first()
    if hija is not None and resultado_de(hija) == COMPLETADO:
        return "COMPLETADO"
    return "SIN_RESULTADO"
