"""Autorización de Tickets — incrementos 2.1, 2.2, 2.3, 2.4 y 2.5.

Solo relación solicitante↔Ticket (corrección del usuario sobre la
propuesta original): un ticket (borrador o ya radicado) se administra/
consulta exclusivamente por quién lo creó, nunca por la visibilidad
vigente del Servicio de origen — esa solo se valida una vez, al crear
(`apps.tickets.operaciones.crear_borrador`). Un cambio posterior en la
visibilidad del catálogo no debe hacer que el solicitante pierda acceso a
su propio ticket (RQF-049/2.1) — mismo criterio se extiende a radicar
(2.2, ver `operaciones.radicar_ticket`).

2.2 reutiliza `es_propietario_borrador` también para el detalle de solo
lectura de un ticket YA radicado (`views.detalle_view`) — el nombre queda
como está (el resto del código y las pruebas ya lo usan así), pero su
alcance real es "es el solicitante de este ticket", no solo "de este
borrador".

**2.3 — autorización de atención (CU-017, RQF-056/061; RN-006/009,
RQF-028).** `PERMISO_ATENDER = "tickets.atender"` es el permiso funcional
base: sin él, en ningún alcance, ninguna función de este módulo autoriza
nada (RN-006 exige usuario + permiso + alcance + relación con el objeto,
los cuatro factores). Sobre esa base:

- `usuario_es_responsable_configurado` responde "¿está `usuario`
  configurado (`ServicioResponsable`, RQF-033) para atender este
  Servicio?" — directo, o como miembro activo de un Equipo responsable.
  Pertenecer al Equipo por sí solo (sin que el Equipo esté configurado
  como `ServicioResponsable`) NUNCA basta (RQF-028/RN-009).
- `es_responsable_actual` responde "¿es `usuario` ya el responsable
  vigente de ESTE ticket concreto?" (él mismo, o miembro de su
  `equipo_responsable` actual) — la relación directa más fuerte, análoga a
  `es_propietario_borrador` para el solicitante.
- `_alcance_cubre_ticket` responde "¿el alcance vigente de
  `tickets.atender` (GLOBAL, o AREA/UNIDAD que coincide con algún
  `TicketContextoAtencion` congelado del ticket) cubre este ticket?" —
  RQF-061. Coincidir con un alcance AREA/UNIDAD nunca basta por sí solo
  para TOMAR (RQF-028/RN-009): `puede_tomar` exige además relación
  operacional real. Para ASIGNAR/REASIGNAR a un tercero sí basta —
  función supervisora explícita del alcance de un Gestor de Área/Unidad
  (RQF-056, actor "Ejecutor / Gestor"), sin exigir que el Gestor esté él
  mismo configurado como responsable del servicio.

**2.4 — comunicación/solicitud de información (CU-018, RQF-057/058).**
Corrección explícita del usuario sobre la primera propuesta: **consultar
y participar no son lo mismo**. `puede_consultar_ticket` (extraída de la
expresión que `views.detalle_view` ya evaluaba inline desde 2.2/2.3, sin
cambiar su alcance) sigue siendo la población amplia de solo-lectura
(solicitante + cualquiera con autorización de atención por alcance). Las
funciones de participación son deliberadamente más estrechas y NO se
definen en términos de `puede_consultar_ticket`:

- `puede_comentar_ticket`: solicitante O responsable actual — un Gestor
  que consulta por alcance de `tickets.atender` (sin ser el responsable
  de ESTE ticket) no obtiene por eso capacidad de escribir. Además exige
  que el ticket esté "abierto a interacción" (CU-018) — RADICADO o
  EN_ATENCION, nunca BORRADOR/RESUELTO/CERRADO/CANCELADO.
- `puede_solicitar_informacion`: exclusivamente el responsable actual
  (RQF-058, actor "Ejecutor") — acción de atención concreta, no
  supervisora del alcance (a diferencia de ASIGNAR/REASIGNAR). Tener
  `tickets.atender` por alcance, sin ser el responsable de este ticket,
  NO basta (decisión explícita del usuario).
- `puede_responder_solicitud`: exclusivamente `usuario.id ==
  solicitud.destinatario_id` — en 2.4 el destinatario es siempre el
  solicitante del ticket (fijado por
  `apps.tickets.operaciones.solicitar_informacion`, nunca recibido del
  cliente), pero la función se apoya en el campo, no en
  `es_propietario_borrador`, para quedar correcta si esa fijación
  cambiara en el futuro.
"""

from django.db.models import Q

from apps.aprobaciones.autorizacion import es_aprobador_directo, puede_aprobar
from apps.catalogo.models import Servicio, ServicioResponsable
from apps.core.autorizacion import alcances_autorizados
from apps.core.models import MiembroEquipo
from apps.tickets.models import ProrrogaTicket, Ticket, TicketContextoAtencion

PERMISO_ATENDER = "tickets.atender"


def es_propietario_borrador(usuario, ticket):
    return bool(getattr(usuario, "is_authenticated", False)) and ticket.solicitante_id == usuario.id


def usuario_es_responsable_configurado(usuario, servicio):
    if not getattr(usuario, "is_authenticated", False):
        return False
    return (
        ServicioResponsable.objects.filter(servicio=servicio, activo=True)
        .filter(
            Q(tipo_responsable=ServicioResponsable.TipoResponsable.USUARIO, usuario=usuario)
            | Q(
                tipo_responsable=ServicioResponsable.TipoResponsable.EQUIPO,
                equipo__miembros__usuario=usuario,
                equipo__miembros__activo=True,
            )
        )
        .exists()
    )


def es_responsable_actual(usuario, ticket):
    if not getattr(usuario, "is_authenticated", False):
        return False
    if ticket.usuario_responsable_id == usuario.id:
        return True
    if ticket.equipo_responsable_id is None:
        return False
    return MiembroEquipo.objects.filter(
        equipo_id=ticket.equipo_responsable_id, usuario=usuario, activo=True
    ).exists()


def _alcance_cubre_ticket(usuario, ticket):
    alcances = alcances_autorizados(usuario, PERMISO_ATENDER)
    if alcances["global"]:
        return True
    for contexto in ticket.contextos_atencion.all():
        if (
            contexto.tipo_alcance == TicketContextoAtencion.TipoAlcance.AREA
            and contexto.area_id in alcances["areas"]
        ):
            return True
        if (
            contexto.tipo_alcance == TicketContextoAtencion.TipoAlcance.UNIDAD
            and contexto.unidad_negocio_id in alcances["unidades_negocio"]
        ):
            return True
    return False


def puede_tomar(usuario, ticket):
    """TOMAR = autoasignación (no existe una operación "Asignarme" aparte).
    Exige relación operacional real, no solo alcance de área/unidad
    (RQF-028/RN-009): GLOBAL, o estar configurado (`ServicioResponsable`)
    para el servicio del ticket."""
    if ticket.estado != Ticket.Estado.RADICADO or ticket.usuario_responsable_id is not None:
        return False
    alcances = alcances_autorizados(usuario, PERMISO_ATENDER)
    if not (alcances["global"] or alcances["areas"] or alcances["unidades_negocio"]):
        return False
    if alcances["global"]:
        return True
    servicio = ticket.detalle_servicio.servicio
    if usuario_es_responsable_configurado(usuario, servicio):
        return True
    # 4.C2 — un Ticket General se dirige a un destino y su responsable no sale de
    # `ServicioResponsable` (el único Servicio interno no puede enumerar a todos
    # los destinos): quien pertenece al equipo al que se dirigió SÍ puede tomarlo.
    # Solo para el Ticket General; para el resto de servicios la regla no cambia.
    return servicio.es_ticket_general and es_responsable_actual(usuario, ticket)


def puede_iniciar_atencion(usuario, ticket):
    """4.C2 — INICIAR ATENCIÓN de un ticket RADICADO que ya fue direccionado a una
    PERSONA concreta (`usuario_responsable` desde la radicación): solo esa persona
    o quien supervisa por alcance (`tickets.atender`, como ASIGNAR) puede hacerlo.
    Si el responsable es un equipo (sin persona), se TOMA (`puede_tomar`)."""
    if not getattr(usuario, "is_authenticated", False) or not getattr(usuario, "is_active", False):
        return False
    if ticket.estado != Ticket.Estado.RADICADO or ticket.usuario_responsable_id is None:
        return False
    return ticket.usuario_responsable_id == usuario.pk or _alcance_cubre_ticket(usuario, ticket)


def puede_asignar(usuario, ticket):
    """ASIGNAR = asignar a un tercero un ticket RADICADO sin responsable
    aún. No exige GLOBAL ni estar configurado como `ServicioResponsable`
    del servicio — basta el alcance vigente de `tickets.atender` (función
    supervisora del Gestor de Área/Unidad, RQF-056/061). Ser miembro de un
    Equipo por sí solo, sin ese alcance, no concede esta capacidad."""
    if ticket.estado != Ticket.Estado.RADICADO or ticket.usuario_responsable_id is not None:
        return False
    return _alcance_cubre_ticket(usuario, ticket)


def puede_reasignar(usuario, ticket):
    """REASIGNAR = cambiar responsable/equipo de un ticket ya EN_ATENCION.
    Mismo criterio de alcance que `puede_asignar`, más una excepción
    explícita: el responsable actual siempre puede reasignar/entregar su
    propio ticket, tenga o no alcance de área/unidad vigente sobre él
    (relación directa con el objeto, RN-006)."""
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        return False
    return _alcance_cubre_ticket(usuario, ticket) or es_responsable_actual(usuario, ticket)


def puede_ver_en_cola(usuario, ticket):
    """Visibilidad en la Cola de atención (RQF-061) y en el detalle para un
    no-solicitante — deliberadamente NO incluye ser el solicitante (esa
    relación es `es_propietario_borrador`, "Mis tickets" no se mezcla con
    la cola)."""
    if not getattr(usuario, "is_authenticated", False):
        return False
    return (
        es_responsable_actual(usuario, ticket)
        or puede_tomar(usuario, ticket)
        or puede_asignar(usuario, ticket)
        or puede_reasignar(usuario, ticket)
        or puede_iniciar_atencion(usuario, ticket)
    )


def puede_consultar_ticket(usuario, ticket):
    """CU-016/RQF-055 — población autorizada para CONSULTAR (ver detalle,
    descargar adjuntos/archivos de respuesta). Es exactamente la unión que
    `views.detalle_view` ya evaluaba inline desde 2.2/2.3 — extraída aquí
    (2.4) para que descarga (`ArchivoRespuestaCampo`/`Adjunto`) y las
    vistas de comunicación reutilicen la misma función en vez de repetir
    la expresión en cuatro lugares distintos."""
    return es_propietario_borrador(usuario, ticket) or puede_ver_en_cola(usuario, ticket)


_ESTADOS_ABIERTOS_A_INTERACCION = (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)


def puede_comentar_ticket(usuario, ticket):
    """CU-018/RQF-057 — participar (escribir) es más estrecho que consultar
    (corrección explícita del usuario sobre la propuesta inicial): NO se
    define como `puede_consultar_ticket`. Solo el solicitante o el
    responsable actual, y solo mientras el ticket está "abierto a
    interacción" (CU-018) — RADICADO o EN_ATENCION."""
    if ticket.estado not in _ESTADOS_ABIERTOS_A_INTERACCION:
        return False
    return es_propietario_borrador(usuario, ticket) or es_responsable_actual(usuario, ticket)


def puede_solicitar_informacion(usuario, ticket):
    """CU-018/RQF-058 (actor "Ejecutor") — exclusivamente el responsable
    actual del ticket. Solicitar información es una acción de atención
    concreta, no una función supervisora del alcance (a diferencia de
    ASIGNAR/REASIGNAR): tener `tickets.atender` por alcance, sin ser el
    responsable de ESTE ticket, no basta (decisión explícita del usuario)."""
    if ticket.estado not in _ESTADOS_ABIERTOS_A_INTERACCION:
        return False
    return es_responsable_actual(usuario, ticket)


def puede_responder_solicitud(usuario, solicitud):
    """CU-018/RQF-058 (actor "Usuario") — exclusivamente el destinatario de
    ESTA solicitud concreta (en 2.4 siempre el solicitante del ticket,
    fijado por `apps.tickets.operaciones.solicitar_informacion` — ver R.1
    de la propuesta aprobada)."""
    return (
        bool(getattr(usuario, "is_authenticated", False))
        and solicitud.destinatario_id == usuario.id
    )


# --- 2.5 — Resolución, cierre, cancelación, reapertura (CU-019/RQF-059) ---
#
# V1 aprobado: consultar ≠ gestionar asignación ≠ participar ≠ finalizar.
# Tener `tickets.atender` por alcance (lo que basta para ASIGNAR/REASIGNAR,
# funciones supervisoras) NO concede por sí solo ninguna de estas 4
# operaciones — todas exigen una relación directa con el ticket (ser el
# solicitante o el responsable actual), nunca solo alcance.


def puede_resolver_ticket(usuario, ticket):
    """CU-019/RQF-059 — exclusivamente el responsable actual, solo desde
    EN_ATENCION. Resolver es ejecutar el trabajo, no supervisarlo (mismo
    criterio que `puede_solicitar_informacion`, 2.4) — alcance de
    `tickets.atender`, sin ser el responsable de ESTE ticket, no basta.

    4.5: un ticket con política de entrega congelada NO se resuelve por esta
    vía — se entrega formalmente (`puede_entregar_ticket`)."""
    if ticket.estado != Ticket.Estado.EN_ATENCION or ticket.entrega_politica:
        return False
    return es_responsable_actual(usuario, ticket)


def puede_cerrar_ticket(usuario, ticket):
    """CU-019/RQF-059 — solicitante O responsable actual, solo desde
    RESUELTO. Sin exigir motivo (V1 aprobado) — la propia acción de
    cerrar es la confirmación."""
    if ticket.estado != Ticket.Estado.RESUELTO or ticket.entrega_politica:
        return False
    return es_propietario_borrador(usuario, ticket) or es_responsable_actual(usuario, ticket)


def puede_cancelar_ticket(usuario, ticket):
    """CU-019/RQF-059 — solicitante O responsable actual, solo desde
    RADICADO o EN_ATENCION (nunca desde RESUELTO: ver `apps.tickets.estados`,
    que no declara esa transición)."""
    if ticket.estado not in (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION):
        return False
    return es_propietario_borrador(usuario, ticket) or es_responsable_actual(usuario, ticket)


def puede_reabrir_ticket(usuario, ticket):
    """CU-019/RQF-059 — exclusivamente el responsable actual, solo desde
    RESUELTO. Sin el solicitante (decisión explícita del usuario, evita
    reaperturas arbitrarias de quien no ejecuta el trabajo). CERRADO no se
    reabre en V1 — ver `apps.tickets.estados`, sin esa entrada.

    4.5: con entrega formal, el ticket vuelve a atención únicamente por las
    observaciones del solicitante (`puede_responder_entrega`)."""
    if ticket.estado != Ticket.Estado.RESUELTO or ticket.entrega_politica:
        return False
    return es_responsable_actual(usuario, ticket)


def puede_escribir_entregables_finales(usuario, ticket):
    """4.2: responsabilidad individual, distinta del alcance operativo del equipo.

    Misma relación asignada por TOMAR/ASIGNAR/REASIGNAR; no crea rol,
    permiso ni responsable alternativo. Las operaciones revalidan bajo lock.
    """
    return (
        bool(getattr(usuario, "is_authenticated", False))
        and bool(getattr(usuario, "is_active", False))
        and ticket.estado == Ticket.Estado.EN_ATENCION
        and ticket.usuario_responsable_id is not None
        and ticket.usuario_responsable_id == usuario.pk
    )


# --- 4.5 — Entrega formal al solicitante ---------------------------------
#
# Entregar reutiliza la regla de responsable INDIVIDUAL de los entregables
# finales (`puede_escribir_entregables_finales`); responder es exclusivo del
# solicitante real. El cierre automático lo ejecuta el Sistema, sin actor.


def puede_entregar_ticket(usuario, ticket):
    """Responsable individual actual, ticket EN_ATENCION y con política de
    entrega congelada. Sin política (tickets anteriores a 4.5) no hay entrega
    formal: siguen el flujo resolver/cerrar."""
    return bool(ticket.entrega_politica) and puede_escribir_entregables_finales(usuario, ticket)


def puede_responder_entrega(usuario, entrega):
    """Exclusivamente el solicitante del ticket, sobre una entrega que sigue
    PENDIENTE y con el ticket RESUELTO. No es una Aprobación de Workflow."""
    ticket = entrega.ticket
    return (
        bool(getattr(usuario, "is_authenticated", False))
        and bool(getattr(usuario, "is_active", False))
        and entrega.estado == "PENDIENTE"
        and ticket.estado == Ticket.Estado.RESUELTO
        and ticket.solicitante_id == usuario.pk
    )


# --- 4.A2 — Prórrogas de la fecha objetivo -----------------------------------
#
# Separadas, nunca por UI: solicitar ≠ resolver (aprobar/rechazar) ≠ cancelar ≠
# consultar el historial. Las operaciones de `apps.tickets.prorrogas` vuelven a
# validar todo bajo lock; estas funciones solo responden "¿corresponde ofrecerlo?"


def motivo_no_elegible_para_prorroga(ticket):
    """Mensaje de por qué `ticket` NO admite hoy una solicitud de prórroga, o
    `None` si es elegible. Solo mira el Ticket (estado, compromiso temporal,
    política congelada, solicitud pendiente), nunca al usuario."""
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        return "Solo se puede solicitar una prórroga mientras el ticket está en atención."
    if ticket.fecha_objetivo_vigente is None:
        return "Este ticket no tiene un compromiso de tiempo (fecha objetivo) que prorrogar."
    if ticket.prorroga_politica not in (
        Servicio.PoliticaProrroga.SIN_APROBACION, Servicio.PoliticaProrroga.CON_APROBACION,
    ):
        return "Este servicio no permite prórrogas."
    if ticket.prorrogas.filter(estado=ProrrogaTicket.Estado.PENDIENTE).exists():
        return "Ya hay una prórroga pendiente de resolver para este ticket."
    return None


def puede_solicitar_prorroga(usuario, ticket):
    """Quien atiende el ticket (responsable individual o miembro activo del
    equipo responsable) — la misma relación directa que
    `puede_solicitar_informacion` — y solo si el ticket es elegible. Tener
    `tickets.atender` por alcance, sin ser responsable de ESTE ticket, no basta."""
    return es_responsable_actual(usuario, ticket) and motivo_no_elegible_para_prorroga(ticket) is None


def aprobacion_pendiente_de_prorroga(prorroga):
    """La `Aprobacion` PENDIENTE del esquema de una prórroga CON_APROBACION, o
    `None`. Es una única participación por diseño."""
    if prorroga.esquema_aprobacion_id is None:
        return None
    return prorroga.esquema_aprobacion.participaciones.filter(estado="PENDIENTE").first()


def puede_resolver_prorroga(usuario, prorroga):
    """Aprobar o rechazar: exclusivamente el aprobador designado de la
    `Aprobacion` de esa prórroga (reutiliza `apps.aprobaciones`, RN-025 — el
    permiso de supervisar aprobaciones no permite decidirlas)."""
    if prorroga.estado != ProrrogaTicket.Estado.PENDIENTE:
        return False
    aprobacion = aprobacion_pendiente_de_prorroga(prorroga)
    return aprobacion is not None and puede_aprobar(usuario, aprobacion)


def puede_cancelar_prorroga(usuario, prorroga):
    """Solo quien la solicitó y mientras siga PENDIENTE."""
    return (
        prorroga.estado == ProrrogaTicket.Estado.PENDIENTE
        and bool(getattr(usuario, "is_authenticated", False))
        and bool(getattr(usuario, "is_active", False))
        and prorroga.solicitada_por_id == usuario.pk
    )


def puede_consultar_prorrogas(usuario, ticket):
    """El historial de prórrogas es parte del detalle del ticket: lo ve
    exactamente quien puede consultarlo."""
    return puede_consultar_ticket(usuario, ticket)


# --- 4.E2 — Revisión de un entregable por un aprobador ---------------------------------
#
# Una aprobación que REVISA un entregable (`BloqueOperativo.entregable_revisado`) solo sirve si
# el aprobador puede ver lo que revisa. Consultar el ticket completo (cola de atención) es una
# población distinta, así que se concede una lectura MÍNIMA y acotada: el contenido de ESE
# entregable (y la descarga de sus archivos) a quien es aprobador directo de una aprobación del
# flujo del ticket que lo revisa — nunca el resto del ticket.


def puede_revisar_entregable(usuario, entregable):
    if not getattr(usuario, "is_authenticated", False):
        return False
    instancia_id = entregable.ticket.instancia_workflow_id
    if instancia_id is None:
        return False
    from apps.aprobaciones.models import Aprobacion

    aprobaciones = Aprobacion.objects.filter(
        esquema__vinculo_workflow__instancia_etapa__instancia_workflow_id=instancia_id,
        esquema__vinculo_workflow__instancia_etapa__bloque_operativo__entregable_revisado__definicion_entregable_id=(
            entregable.definicion_id
        ),
    )
    return any(es_aprobador_directo(usuario, aprobacion) for aprobacion in aprobaciones)
