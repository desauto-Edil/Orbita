"""Operaciones de dominio de Tickets — incrementos 2.1 (CU-015; modelo base
y borradores), 2.2 (CU-014; radicación y respuestas), 2.3 (CU-017;
atención y asignación), 2.4 (CU-018; comunicación, adjuntos operativos y
solicitud de información), 2.5 (CU-019; resolución, cierre, cancelación y
reapertura) y 2.C (cierre técnico Sprint 2). RQF-049 a 054,
RQF-056/057/058/059/060/061/118/119; RN-014/015/016/018.

2.1 cubre el ciclo de vida de un BORRADOR: crear, guardar respuestas
(incluidos archivos de campos tipo ARCHIVO) y eliminar. 2.2 agrega
`radicar_ticket`: valida el estado ya persistido (`validaciones.py`) y
transiciona BORRADOR -> RADICADO generando el `radicado` único. 2.3 agrega
`tomar_ticket`/`asignar_ticket`/`reasignar_ticket` (CU-017) — cada una
bloquea la fila del ticket con `select_for_update()` antes de revalidar,
para que dos operaciones concurrentes sobre el mismo ticket nunca se
pisen en silencio (la segunda, al re-leer el estado ya bloqueado, falla
con un `ValidationError` explícito en vez de sobrescribir). 2.4 agrega
`comentar_ticket`/`adjuntar_archivo_ticket`/`solicitar_informacion`/
`responder_solicitud` — ninguna transiciona `Ticket.estado` ni toca
`estados.py` (ver `apps.tickets.models.SolicitudInformacion`). 2.5 agrega
`resolver_ticket`/`cerrar_ticket`/`cancelar_ticket`/`reabrir_ticket` —
mismo patrón de lock que 2.3, y cada una alimenta tanto `HistorialTicket`
(trazabilidad operacional) como `RegistroAuditoria` (auditoría
transversal, vía `_auditar_cambio_estado`) — corrección explícita del
usuario: no son sustitutos (ver docstring de `apps.tickets.models.
HistorialTicket`). 2.C corrige dos hallazgos de cierre técnico:
`resolver_ticket` ya no bloquea una segunda resolución tras `REABRIR`
(`ResolucionTicket.ticket` pasó de `OneToOneField` a `ForeignKey`, ver su
docstring), y `tomar_ticket`/`asignar_ticket`/`reasignar_ticket` (2.3)
ahora también alimentan `RegistroAuditoria` vía
`_auditar_cambio_responsable` (RQF-119, deuda cerrada)."""

import uuid
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import historial
from apps.tickets.autorizacion import (
    es_propietario_borrador,
    puede_asignar,
    puede_cancelar_ticket,
    puede_cerrar_ticket,
    puede_comentar_ticket,
    puede_reabrir_ticket,
    puede_reasignar,
    puede_resolver_ticket,
    puede_responder_solicitud,
    puede_solicitar_informacion,
    puede_tomar,
)
from apps.tickets.estados import exigir_transicion
from apps.tickets.models import (
    COLUMNA_POR_TIPO,
    COLUMNAS_REFERENCIA,
    Adjunto,
    ArchivoRespuestaCampo,
    ComentarioTicket,
    HistorialTicket,
    ResolucionTicket,
    RespuestaCampo,
    RespuestaFormulario,
    RespuestaSolicitudInformacion,
    SolicitudInformacion,
    Ticket,
    TicketContextoAtencion,
    TicketServicio,
)
from apps.tickets.validaciones import calcular_estados_efectivos, validar_para_radicar


@transaction.atomic
def crear_borrador(usuario, servicio):
    """CU-014 (parcial)/RQF-049 — inicia un Ticket de Servicio en BORRADOR.

    La visibilidad del Servicio (`servicios_visibles_para`, 1.1) se valida
    ÚNICAMENTE aquí. Editar/consultar/eliminar el borrador ya creado no
    vuelve a consultarla — ver `apps.tickets.autorizacion`.
    """
    if not servicios_visibles_para(usuario).filter(pk=servicio.pk).exists():
        raise PermissionDenied("El servicio no está activo o no es visible para este usuario.")

    formulario = servicio.formulario
    version = formulario.version_activa if formulario else None
    if version is None:
        raise ValidationError("El servicio no tiene un formulario activo utilizable todavía.")

    ticket = Ticket.objects.create(solicitante=usuario, tipo=Ticket.Tipo.SERVICIO)
    TicketServicio.objects.create(ticket=ticket, servicio=servicio, formulario_version=version)
    RespuestaFormulario.objects.create(ticket=ticket, formulario_version=version)
    return ticket


def _es_valor_vacio(valor_crudo):
    """Señal de "borrar esta respuesta" — un valor ausente/vacío nunca se
    valida como si fuera una respuesta real (RQF-053/obligatoriedad es
    2.2); simplemente se descarta lo que hubiera antes."""
    if valor_crudo is None:
        return True
    if isinstance(valor_crudo, str) and valor_crudo.strip() == "":
        return True
    if isinstance(valor_crudo, (list, tuple)) and len(valor_crudo) == 0:
        return True
    return False


def _upsert_respuesta_escalar(respuesta_formulario, campo, valor_normalizado, existente):
    columna = COLUMNA_POR_TIPO[campo.tipo]
    instancia = existente or RespuestaCampo(respuesta_formulario=respuesta_formulario, campo=campo)
    if columna in COLUMNAS_REFERENCIA:
        setattr(instancia, f"{columna}_id", valor_normalizado)
    elif columna == "valor_numero":
        setattr(instancia, columna, Decimal(str(valor_normalizado)))
    else:
        setattr(instancia, columna, valor_normalizado)
    instancia.save()
    return instancia


def _validar_archivo_tecnico(archivo_subido):
    """Control técnico mínimo compartido por `ArchivoRespuestaCampo` (vía
    `_guardar_archivo`) y `Adjunto` (2.4, vía `_crear_adjunto`) — **no** es
    un límite de negocio. RQF-007 exige validar "según restricciones...
    configuradas", pero no existe ninguna configuración de tamaño/
    extensión para adjuntos operativos (a diferencia de
    `Campo.configuracion`, que sí aplica a `ArchivoRespuestaCampo` vía
    `EstrategiaArchivo`, sin cambios, y sigue siendo la única fuente de
    límites de negocio de este archivo). Revisado explícitamente contra el
    Excel (Requerimientos No funcionales, Reglas): no hay tamaño máximo,
    extensiones permitidas ni cantidad máxima documentados en ningún lado
    para adjuntos operativos — no se inventan aquí (propuesta 2.4,
    punto 12). Se rechaza únicamente lo que nunca puede ser un archivo
    real: sin nombre, o de tamaño cero."""
    if not archivo_subido or not archivo_subido.name:
        raise ValidationError("Debe seleccionar un archivo.")
    if archivo_subido.size == 0:
        raise ValidationError("El archivo está vacío.")


def _guardar_archivo(respuesta_formulario, campo, archivo_subido, actor, existente):
    """Reutiliza `EstrategiaArchivo.validar_valor()` (`campos.py`, sin
    modificar) contra las restricciones ya configuradas en
    `campo.configuracion` (`extensiones_permitidas`/`tamano_maximo_mb`) —
    ningún límite nuevo se inventa aquí (RQF-007). `_validar_archivo_tecnico`
    agrega, antes, el único control técnico compartido con `Adjunto`."""
    _validar_archivo_tecnico(archivo_subido)
    estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
    metadata = {"nombre": archivo_subido.name, "tamano_mb": archivo_subido.size / (1024 * 1024)}
    estrategia.validar_valor(campo, metadata)

    instancia = existente or RespuestaCampo(respuesta_formulario=respuesta_formulario, campo=campo)
    if existente is None:
        instancia.save()

    archivo_anterior = getattr(instancia, "archivo", None)
    if archivo_anterior is not None:
        archivo_anterior.delete()  # dispara el pre_delete que libera el archivo físico

    ArchivoRespuestaCampo.objects.create(
        respuesta_campo=instancia,
        archivo=archivo_subido,
        nombre_original=archivo_subido.name,
        tipo_mime=getattr(archivo_subido, "content_type", "") or "",
        tamano_bytes=archivo_subido.size,
        subido_por=actor,
    )
    return instancia


@transaction.atomic
def guardar_respuestas_borrador(ticket, actor, respuestas_crudas):
    """CU-015/RQF-050 — guarda (upsert) respuestas de un borrador.

    `respuestas_crudas` es `{campo_id: valor_crudo}` — solo los campos
    presentes en el dict se tocan en esta llamada; los demás conservan su
    valor previo (o su ausencia). Un valor vacío/`None` para un campo
    presente en el dict borra su respuesta existente.

    Reglas MOSTRAR/OCULTAR se reevalúan en cada llamada contra el conjunto
    completo de respuestas (previas + las de esta llamada): cualquier
    campo que resulte oculto pierde su `RespuestaCampo`, exista o no en
    `respuestas_crudas` — un cambio en OTRA respuesta puede ocultar un
    campo que ya tenía valor guardado.
    """
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede modificar este borrador.")
    if ticket.estado != Ticket.Estado.BORRADOR:
        raise ValidationError("Solo se pueden guardar respuestas mientras el ticket es un borrador.")

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    campos = {c.id: c for c in version.campos.prefetch_related("opciones")}
    existentes = {
        rc.campo_id: rc
        for rc in respuesta_formulario.respuestas_campo.select_related("campo").all()
    }

    valores_para_evaluar = {campo_id: rc.valor for campo_id, rc in existentes.items()}
    valores_para_evaluar.update(respuestas_crudas)

    estados_efectivos = calcular_estados_efectivos(version, valores_para_evaluar)

    for campo_id, campo in campos.items():
        existente = existentes.get(campo_id)
        visible = estados_efectivos[campo_id].visible

        if not visible:
            if existente is not None:
                existente.delete()
            continue

        if campo_id not in respuestas_crudas:
            continue  # no se modifica lo que no vino en esta llamada

        valor_crudo = respuestas_crudas[campo_id]

        if _es_valor_vacio(valor_crudo):
            if existente is not None:
                existente.delete()
            continue

        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            _guardar_archivo(respuesta_formulario, campo, valor_crudo, actor, existente)
            continue

        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        estrategia.validar_valor(campo, valor_crudo)
        valor_normalizado = estrategia.normalizar(valor_crudo)
        _upsert_respuesta_escalar(respuesta_formulario, campo, valor_normalizado, existente)

    return respuesta_formulario


def eliminar_borrador(ticket, actor):
    """CU-015/RQF-051. `Ticket.delete()` exige `estado == BORRADOR`
    (`Ticket.exigir_eliminable`) y arrastra en cascada `TicketServicio`,
    `RespuestaFormulario`, `RespuestaCampo` y `ArchivoRespuestaCampo`
    (incluido el archivo físico, vía el `pre_delete` de `apps.py`)."""
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede eliminar este borrador.")
    ticket.delete()


@transaction.atomic
def radicar_ticket(ticket, actor):
    """CU-014/RQF-053/RQF-054, RN-014/RN-016 — incremento 2.2.

    Responsabilidad exclusiva: VALIDAR el estado YA PERSISTIDO del ticket y,
    si es válido, transicionarlo a RADICADO. No purga ni normaliza
    respuestas — esa responsabilidad sigue siendo de
    `guardar_respuestas_borrador` (2.1). El flujo de UI (`views.radicar_view`)
    ejecuta primero `guardar_respuestas_borrador` con el envío actual del
    formulario y luego esta función, como dos operaciones atómicas
    independientes en secuencia (no una única transacción envolvente) — ver
    esa vista para la justificación completa de esa decisión.

    Precondiciones, en orden: actor es el solicitante; ticket sigue en
    BORRADOR (cubre también la doble radicación); el Servicio sigue activo
    (decisión de negocio del proyecto — RN-011 protege tickets ya
    radicados/históricos, no autoriza radicar contra un servicio que el
    catálogo ya retiró; un cambio posterior únicamente de *visibilidad* NO
    bloquea, mismo criterio que 2.1 aplica al guardar un borrador);
    `validar_para_radicar` (RQF-044/RN-012, solo lectura).

    `radicado` (UUID4, RN-014) y `radicado_en` (`timezone.now()`, RN-016)
    se fijan aquí exclusivamente — nunca a partir de un valor del cliente.

    2.3 agrega dos efectos, ambos parte de la misma transacción: se
    congela una copia (`TicketContextoAtencion`) de TODOS los
    `ServicioContextoAtencion` activos del servicio — un servicio
    transversal conserva los varios contextos que tenía, no se elige uno
    (RQF-062/RN-019, mismo criterio que `FormularioVersion`); y se
    registra el evento `RADICADO` en `HistorialTicket` (decisión explícita
    del usuario: el historial no se rellena retroactivamente para tickets
    radicados antes de que este modelo existiera).
    """
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede radicar este ticket.")
    if ticket.estado != Ticket.Estado.BORRADOR:
        raise ValidationError("Solo un ticket en estado BORRADOR puede radicarse.")

    servicio = ticket.detalle_servicio.servicio
    if not servicio.activo:
        raise ValidationError(
            "El servicio ya no está activo: no es posible radicar este ticket."
        )

    validar_para_radicar(ticket)

    ticket.radicado = uuid.uuid4()
    ticket.radicado_en = timezone.now()
    ticket.estado = Ticket.Estado.RADICADO
    ticket.save()

    for contexto in servicio.contextos_atencion.filter(activo=True):
        TicketContextoAtencion.objects.create(
            ticket=ticket,
            tipo_alcance=contexto.tipo_alcance,
            area=contexto.area,
            unidad_negocio=contexto.unidad_negocio,
        )

    historial.registrar(ticket, HistorialTicket.TipoEvento.RADICADO, actor)

    return ticket


@transaction.atomic
def tomar_ticket(ticket, actor):
    """CU-017/RQF-056 — TOMAR es autoasignación; no existe una operación
    "Asignarme" separada (decisión del usuario). `select_for_update()`
    bloquea la fila antes de revalidar: si dos usuarios intentan tomar el
    mismo ticket a la vez, el segundo, al re-leer bajo el lock, ve
    `usuario_responsable` ya poblado y falla con `ValidationError` en vez
    de sobrescribir al primero."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.usuario_responsable_id is not None:
        raise ValidationError("Este ticket ya tiene un responsable asignado.")
    if not puede_tomar(actor, ticket):
        raise PermissionDenied("No tiene autorización para tomar este ticket.")

    usuario_anterior_id = ticket.usuario_responsable_id
    equipo_anterior_id = ticket.equipo_responsable_id
    exigir_transicion(ticket, "TOMAR")
    ticket.usuario_responsable = actor
    ticket.save()
    historial.registrar(ticket, HistorialTicket.TipoEvento.TOMADO, actor)
    _auditar_cambio_responsable(
        ticket,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=ticket.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=ticket.equipo_responsable_id,
    )
    return ticket


@transaction.atomic
def asignar_ticket(ticket, actor, *, usuario=None, equipo=None):
    """CU-017/RQF-056 — ASIGNAR es asignar a un tercero un ticket RADICADO
    que todavía no tiene `usuario_responsable`. Si se asigna un `usuario`,
    dispara la misma transición que TOMAR (RADICADO→EN_ATENCION), pero
    ejecutada por un tercero en vez de autoasignación; asignar solo
    `equipo` no cambia el estado. Un ticket que ya tiene
    `usuario_responsable` se reasigna con `reasignar_ticket`, no con esta
    función."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.RADICADO:
        raise ValidationError("Solo un ticket RADICADO sin responsable puede asignarse por esta vía.")
    if usuario is None and equipo is None:
        raise ValidationError("Debe indicar un usuario y/o un equipo para asignar.")
    if not puede_asignar(actor, ticket):
        raise PermissionDenied("No tiene autorización para asignar este ticket.")

    usuario_anterior_id = ticket.usuario_responsable_id
    equipo_anterior_id = ticket.equipo_responsable_id
    if equipo is not None:
        ticket.equipo_responsable = equipo
    if usuario is not None:
        ticket.usuario_responsable = usuario
        exigir_transicion(ticket, "ASIGNAR_USUARIO")
    ticket.save()

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.ASIGNADO,
        actor,
        usuario_id=usuario.id if usuario else None,
        equipo_anterior_id=equipo_anterior_id,
        equipo_id=equipo.id if equipo else None,
    )
    _auditar_cambio_responsable(
        ticket,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=ticket.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=ticket.equipo_responsable_id,
    )
    return ticket


@transaction.atomic
def reasignar_ticket(ticket, actor, *, usuario=None, equipo=None):
    """CU-017/RQF-056 — REASIGNAR cambia responsable/equipo de un ticket ya
    EN_ATENCION. No pasa por `estados.py`: no existe una transición
    EN_ATENCION→EN_ATENCION (decisión explícita del usuario) — solo cambia
    los campos de responsable."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        raise ValidationError("Solo un ticket EN_ATENCION puede reasignarse.")
    if usuario is None and equipo is None:
        raise ValidationError("Debe indicar un usuario y/o un equipo para reasignar.")
    if not puede_reasignar(actor, ticket):
        raise PermissionDenied("No tiene autorización para reasignar este ticket.")

    usuario_anterior_id = ticket.usuario_responsable_id
    equipo_anterior_id = ticket.equipo_responsable_id
    if equipo is not None:
        ticket.equipo_responsable = equipo
    if usuario is not None:
        ticket.usuario_responsable = usuario
    ticket.save()

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.REASIGNADO,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_id=usuario.id if usuario else None,
        equipo_anterior_id=equipo_anterior_id,
        equipo_id=equipo.id if equipo else None,
    )
    _auditar_cambio_responsable(
        ticket,
        actor,
        usuario_anterior_id=usuario_anterior_id,
        usuario_nuevo_id=ticket.usuario_responsable_id,
        equipo_anterior_id=equipo_anterior_id,
        equipo_nuevo_id=ticket.equipo_responsable_id,
    )
    return ticket


def _crear_adjunto(tipo_relacion, parent_kwargs, archivo_subido, actor):
    """Único punto de creación de `Adjunto` — las 4 operaciones de 2.4 que
    aceptan archivos (comentar/adjuntar directo/solicitar/responder) pasan
    por aquí, así la validación técnica y los metadatos derivados del
    archivo (nunca escritos a mano por el usuario, RQF-007) no se repiten
    4 veces."""
    _validar_archivo_tecnico(archivo_subido)
    return Adjunto.objects.create(
        tipo_relacion=tipo_relacion,
        archivo=archivo_subido,
        nombre_original=archivo_subido.name,
        tipo_mime=getattr(archivo_subido, "content_type", "") or "",
        tamano_bytes=archivo_subido.size,
        subido_por=actor,
        **parent_kwargs,
    )


@transaction.atomic
def comentar_ticket(ticket, actor, contenido, archivos=None):
    """CU-018/RQF-057 — comentario cronológico y append-only (sin
    threading, decisión explícita del usuario). No transiciona
    `Ticket.estado`."""
    if not puede_comentar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para comentar este ticket.")
    if not contenido or not contenido.strip():
        raise ValidationError("El comentario no puede estar vacío.")

    comentario = ComentarioTicket.objects.create(
        ticket=ticket, autor=actor, contenido=contenido.strip()
    )
    for archivo_subido in archivos or []:
        _crear_adjunto(Adjunto.TipoRelacion.COMENTARIO, {"comentario": comentario}, archivo_subido, actor)
    return comentario


@transaction.atomic
def adjuntar_archivo_ticket(ticket, actor, archivo_subido):
    """CU-018/RQF-052 (R.4 aprobado) — adjunto directo al Ticket, sin
    exigir crear un comentario. Misma autorización que comentar: es una
    acción de participación, no de simple consulta."""
    if not puede_comentar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para adjuntar archivos a este ticket.")
    return _crear_adjunto(Adjunto.TipoRelacion.TICKET, {"ticket": ticket}, archivo_subido, actor)


@transaction.atomic
def solicitar_informacion(ticket, actor, mensaje, archivos=None):
    """CU-018/RQF-058 — `destinatario` SIEMPRE `ticket.solicitante` en 2.4
    (R.1 aprobado): nunca aceptado como parámetro externo/POST."""
    if not puede_solicitar_informacion(actor, ticket):
        raise PermissionDenied("No tiene autorización para solicitar información en este ticket.")
    if not mensaje or not mensaje.strip():
        raise ValidationError("El mensaje de la solicitud no puede estar vacío.")

    solicitud = SolicitudInformacion.objects.create(
        ticket=ticket,
        solicitada_por=actor,
        destinatario=ticket.solicitante,
        mensaje=mensaje.strip(),
    )
    for archivo_subido in archivos or []:
        _crear_adjunto(Adjunto.TipoRelacion.SOLICITUD, {"solicitud": solicitud}, archivo_subido, actor)
    historial.registrar(
        ticket, HistorialTicket.TipoEvento.INFORMACION_SOLICITADA, actor, solicitud_id=solicitud.id
    )
    return solicitud


@transaction.atomic
def responder_solicitud(solicitud, actor, contenido, archivos=None):
    """CU-018/RQF-058 — respuesta única y final (`RespuestaSolicitudInformacion`,
    `OneToOne`). `select_for_update()` sobre la `SolicitudInformacion`: si
    dos respuestas llegan a la vez, la segunda, al releer bajo el lock, ve
    `estado` ya `RESPONDIDA` (o la fila de respuesta ya creada) y falla con
    `ValidationError` explícito — mismo patrón que `tomar_ticket`. Orden de
    verificación bajo el lock: estado, existencia de respuesta previa,
    autorización — mismo orden (estado antes que autorización) que el
    resto de operaciones de este módulo."""
    solicitud = SolicitudInformacion.objects.select_for_update().get(pk=solicitud.pk)
    if solicitud.estado != SolicitudInformacion.Estado.PENDIENTE:
        raise ValidationError("Esta solicitud ya fue respondida.")
    if RespuestaSolicitudInformacion.objects.filter(solicitud=solicitud).exists():
        raise ValidationError("Esta solicitud ya fue respondida.")
    if not puede_responder_solicitud(actor, solicitud):
        raise PermissionDenied("No tiene autorización para responder esta solicitud.")
    if not contenido or not contenido.strip():
        raise ValidationError("La respuesta no puede estar vacía.")

    respuesta = RespuestaSolicitudInformacion.objects.create(
        solicitud=solicitud, respondida_por=actor, contenido=contenido.strip()
    )
    for archivo_subido in archivos or []:
        _crear_adjunto(
            Adjunto.TipoRelacion.RESPUESTA_SOLICITUD, {"respuesta_solicitud": respuesta}, archivo_subido, actor
        )
    solicitud.estado = SolicitudInformacion.Estado.RESPONDIDA
    solicitud.save(update_fields=["estado"])
    historial.registrar(
        solicitud.ticket,
        HistorialTicket.TipoEvento.INFORMACION_RESPONDIDA,
        actor,
        solicitud_id=solicitud.id,
    )
    return respuesta


def _auditar_cambio_responsable(
    ticket, actor, *, usuario_anterior_id, usuario_nuevo_id, equipo_anterior_id, equipo_nuevo_id
):
    """RQF-119 ("conservar cambios de responsable y asignación") — cierre
    de deuda 2.C: TOMAR/ASIGNAR/REASIGNAR (2.3) ya alimentaban
    `HistorialTicket`; ahora también `RegistroAuditoria`, mismo mecanismo
    que `_auditar_cambio_estado` (llamada directa a
    `apps.core.auditoria.registrar_evento`, sin señales). Contenido
    acotado a los 4 valores que realmente identifican el cambio de
    asignación — no se audita comentarios/adjuntos/comunicación por este
    cambio."""
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=ticket,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={
            "usuario_responsable_id": usuario_anterior_id,
            "equipo_responsable_id": equipo_anterior_id,
        },
        datos_nuevos={
            "usuario_responsable_id": usuario_nuevo_id,
            "equipo_responsable_id": equipo_nuevo_id,
        },
    )


def _auditar_cambio_estado(ticket, actor, estado_anterior):
    """RQF-118 ("conservar cambios de estado de elementos operativos") —
    traza el cambio de `Ticket.estado` también en `RegistroAuditoria`
    (auditoría transversal), además de `HistorialTicket` (trazabilidad
    operacional visible) — 2.5, corrección explícita del usuario: no son
    sustitutos. Reutiliza `apps.core.auditoria.registrar_evento`
    directamente, sin señales/middleware/thread-local (mismo patrón que
    Django Admin ya usa): el actor real está disponible aquí porque la
    operación de dominio lo recibe explícitamente como parámetro.

    Contenido acotado deliberadamente a `{"estado": ...}` (no
    `serializar(ticket)` completo): lo que cambió en esta transición es el
    estado, no el resto de columnas del Ticket — RQ-NFN-09 exige registrar
    "qué cambió", no serializar todo el objeto de nuevo.

    Las asignaciones de 2.3 (TOMAR/ASIGNAR/REASIGNAR, RQF-119) se
    corrigieron en 2.C — ver `_auditar_cambio_responsable`, usado desde
    `tomar_ticket`/`asignar_ticket`/`reasignar_ticket`."""
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=ticket,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={"estado": estado_anterior},
        datos_nuevos={"estado": ticket.estado},
    )


@transaction.atomic
def resolver_ticket(ticket, actor, descripcion, archivos=None):
    """CU-019/RQF-059 — EN_ATENCION → RESUELTO. Bloqueada mientras exista
    una `SolicitudInformacion(estado=PENDIENTE)` del ticket (2.5, V1
    aprobado): falla con un mensaje controlado, sin responder ni cancelar
    nada automáticamente. `descripcion` es obligatoria; crea una nueva
    `ResolucionTicket` (2.C: `ForeignKey`, no `OneToOneField` — un ticket
    puede acumular varias a lo largo de sucesivos ciclos RESOLVER→REABRIR→
    RESOLVER, ninguna se actualiza ni se elimina) con sus adjuntos de
    evidencia opcionales.

    El único guardián contra una segunda resolución *dentro del mismo
    ciclo* es el chequeo de estado (`!= EN_ATENCION`): tras un `RESOLVER`
    exitoso el ticket queda `RESUELTO`, así que un segundo intento sin
    pasar por `REABRIR` falla aquí, antes de crear ninguna fila — no hace
    falta (ni se usa) ningún chequeo de "¿ya existe una resolución?"."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        raise ValidationError("Solo un ticket EN_ATENCION puede resolverse.")
    if ticket.solicitudes_informacion.filter(estado=SolicitudInformacion.Estado.PENDIENTE).exists():
        raise ValidationError(
            "No es posible resolver mientras existan solicitudes de información pendientes."
        )
    if not puede_resolver_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para resolver este ticket.")
    if not descripcion or not descripcion.strip():
        raise ValidationError("La descripción de resolución no puede estar vacía.")

    estado_anterior = ticket.estado
    exigir_transicion(ticket, "RESOLVER")
    ticket.save()

    resolucion = ResolucionTicket.objects.create(
        ticket=ticket, resuelto_por=actor, descripcion=descripcion.strip()
    )
    for archivo_subido in archivos or []:
        _crear_adjunto(Adjunto.TipoRelacion.RESOLUCION, {"resolucion": resolucion}, archivo_subido, actor)

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.RESUELTO,
        actor,
        estado_anterior=estado_anterior,
        estado_nuevo=ticket.estado,
        resolucion_id=resolucion.id,
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    return resolucion


@transaction.atomic
def cerrar_ticket(ticket, actor):
    """CU-019/RQF-059 — RESUELTO → CERRADO. Sin motivo obligatorio (V1
    aprobado): la propia acción de cerrar es la confirmación."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.RESUELTO:
        raise ValidationError("Solo un ticket RESUELTO puede cerrarse.")
    if not puede_cerrar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para cerrar este ticket.")

    estado_anterior = ticket.estado
    exigir_transicion(ticket, "CERRAR")
    ticket.save()

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.CERRADO,
        actor,
        estado_anterior=estado_anterior,
        estado_nuevo=ticket.estado,
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    return ticket


@transaction.atomic
def cancelar_ticket(ticket, actor, motivo):
    """CU-019/RQF-059 — RADICADO/EN_ATENCION → CANCELADO. Motivo
    obligatorio (V1 aprobado). No elimina físicamente el ticket — distinto
    de `eliminar_borrador`, que solo aplica a BORRADOR."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION):
        raise ValidationError("Solo un ticket RADICADO o EN_ATENCION puede cancelarse.")
    if not puede_cancelar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para cancelar este ticket.")
    if not motivo or not motivo.strip():
        raise ValidationError("El motivo de cancelación no puede estar vacío.")

    estado_anterior = ticket.estado
    exigir_transicion(ticket, "CANCELAR")
    ticket.save()

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.CANCELADO,
        actor,
        estado_anterior=estado_anterior,
        estado_nuevo=ticket.estado,
        motivo=motivo.strip(),
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    return ticket


@transaction.atomic
def reabrir_ticket(ticket, actor, motivo):
    """CU-019/RQF-059 — RESUELTO → EN_ATENCION. Exclusivamente el
    responsable actual (V1 aprobado); motivo obligatorio. Conserva
    `usuario_responsable`/`equipo_responsable` sin tocarlos — no crea un
    nuevo ciclo de atención ni un nuevo Ticket. CERRADO no se reabre en V1
    (`estados.py` no tiene esa entrada, así que esta función nunca llega a
    ejecutarse sobre un ticket CERRADO sin fallar antes en el chequeo de
    estado)."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado != Ticket.Estado.RESUELTO:
        raise ValidationError("Solo un ticket RESUELTO puede reabrirse.")
    if not puede_reabrir_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para reabrir este ticket.")
    if not motivo or not motivo.strip():
        raise ValidationError("El motivo de reapertura no puede estar vacío.")

    estado_anterior = ticket.estado
    exigir_transicion(ticket, "REABRIR")
    ticket.save()

    historial.registrar(
        ticket,
        HistorialTicket.TipoEvento.REABIERTO,
        actor,
        estado_anterior=estado_anterior,
        estado_nuevo=ticket.estado,
        motivo=motivo.strip(),
    )
    _auditar_cambio_estado(ticket, actor, estado_anterior)
    return ticket
