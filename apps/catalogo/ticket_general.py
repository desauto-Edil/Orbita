"""Ticket General — configuración y acceso (Sprint 4.C1).

Órbita debe poder crear un Ticket aunque la necesidad no corresponda a un
Servicio/Proceso del catálogo. No hay un Ticket sin Servicio ni un modelo
paralelo: existe UN Servicio interno (`Servicio.es_ticket_general`) que reutiliza
toda la arquitectura (Ticket → TicketServicio → Formulario versionado → ciclo
normal → historial/auditoría/permisos) pero que NUNCA se comporta como un
Servicio catalogado (ver `apps.catalogo.visibilidad`).

Qué vive dónde (nada se duplica):

- `ConfiguracionTicketGeneral` — solo si está HABILITADO.
- El Servicio interno — formulario, tiempo objetivo, prórroga, política de
  entrega, visibilidad y publicación, todo editable en Studio como cualquier otro.
- Cuál es el Servicio interno: su marca, única por constraint de base.

El Servicio interno está ACTIVO para poder usarse y, aun así, queda fuera del
catálogo: "activo" (se puede usar) y "visible en catálogo" (aparece como opción
catalogada) son cosas distintas.

Un Servicio con tickets no puede dejar de ser el interno ni designarse como tal
(`designar_servicio_ticket_general`): así el origen de un ticket se deduce siempre
de `TicketServicio.servicio` y no hace falta un snapshot en el Ticket.

Destinos y enrutamiento (4.C2) viven en `apps.catalogo.destinos_ticket_general`;
el buscador "¿Qué necesitas?" (4.D, `apps.catalogo.busqueda`) nunca incluye
al Servicio interno entre sus resultados y solo enlaza a esta entrada como alternativa.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo import destinos_ticket_general as destinos
from apps.catalogo.models import Categoria, ConfiguracionTicketGeneral, Servicio
from apps.catalogo.operaciones import _exigir_administracion, crear_servicio, validar_publicacion
from apps.catalogo.visibilidad import servicios_accesibles_para
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria

NOMBRE_POR_DEFECTO = "Ticket general"


def servicio_ticket_general():
    """El Servicio interno (o `None` si todavía no se designó)."""
    return Servicio.objects.filter(es_ticket_general=True).first()


def _tiene_tickets(servicio):
    from apps.tickets.models import TicketServicio

    return TicketServicio.objects.filter(servicio=servicio).exists()


def servicio_para_crear_ticket(usuario):
    """El Servicio interno si `usuario` puede crear un Ticket General ahora; si
    no, la excepción que explica por qué. Única puerta de entrada al Servicio
    interno: comprueba habilitación, existencia y visibilidad (la misma regla de
    concesiones que cualquier Servicio, sin debilitar `servicios_visibles_para`)."""
    if not ConfiguracionTicketGeneral.actual().habilitado:
        raise ValidationError("El ticket general no está habilitado.")
    servicio = servicio_ticket_general()
    if servicio is None:
        raise ValidationError("El ticket general no tiene un servicio interno configurado.")
    if not servicios_accesibles_para(usuario).filter(pk=servicio.pk).exists():
        raise PermissionDenied("El ticket general no está disponible para este usuario.")
    return servicio


def servicio_disponible_para(usuario):
    """Como `servicio_para_crear_ticket`, pero `None` en vez de excepción y
    exigiendo además un formulario activo utilizable. Para decidir si se ofrece
    la entrada "Crear ticket general" (la operación de dominio revalida)."""
    try:
        servicio = servicio_para_crear_ticket(usuario)
    except (ValidationError, PermissionDenied):
        return None
    formulario = servicio.formulario
    if formulario is None or formulario.version_activa is None:
        return None
    # 4.C2: sin un destino utilizable (ni uno de reserva) ningún ticket podría
    # radicarse; no se ofrece una entrada que no puede completarse.
    if not destinos.hay_destinos_utilizables():
        return None
    return servicio


def _auditar_configuracion(configuracion, actor, anterior, nuevo, accion):
    registrar_evento(
        accion=accion, instancia=configuracion, origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )


@transaction.atomic
def configurar_habilitacion(actor, *, habilitado):
    """Habilita o deshabilita el Ticket General. Para habilitarlo hace falta un
    Servicio interno designado, publicado (activo) y que pase la validación de
    publicación (formulario con versión activa, etc.); deshabilitar siempre es
    posible. Desde 4.C2 también exige al menos un destino utilizable (o uno de
    reserva): sin él nadie podría radicar. Solo habilita/deshabilita: no copia
    nada al Servicio ni a la configuración global."""
    _exigir_administracion(actor)
    configuracion = ConfiguracionTicketGeneral.bloquear()
    habilitado = bool(habilitado)
    if habilitado:
        servicio = servicio_ticket_general()
        if servicio is None:
            raise ValidationError("Antes de habilitarlo, crea o elige el servicio interno del ticket general.")
        if not servicio.activo:
            raise ValidationError("Antes de habilitarlo, publica el servicio interno del ticket general.")
        validar_publicacion(servicio)
        if not destinos.hay_destinos_utilizables():
            raise ValidationError(
                "Antes de habilitarlo, configura al menos un destino activo con un responsable válido."
            )
    if configuracion.habilitado == habilitado:
        return configuracion
    anterior = {"habilitado": configuracion.habilitado}
    configuracion.habilitado = habilitado
    configuracion.save(update_fields=["habilitado", "actualizado_en"])
    _auditar_configuracion(
        configuracion, actor, anterior, {"habilitado": habilitado}, RegistroAuditoria.Accion.ACTUALIZAR
    )
    return configuracion


@transaction.atomic
def designar_servicio_ticket_general(servicio, actor):
    """Marca `servicio` como el Servicio interno del Ticket General, retirando la
    marca al anterior (siempre hay a lo sumo uno). Reglas:

    - debe ser un Servicio (no un Proceso) y no tener tickets propios: convertir
      en "general" tickets ya catalogados cambiaría su origen retroactivamente;
    - el anterior tampoco puede tener tickets: su marca no se retira;
    - al cambiar de Servicio el Ticket General queda DESHABILITADO hasta validar
      el nuevo (no se habilita algo sin revisar).

    Bloquea la fila de configuración: dos designaciones simultáneas se ejecutan
    en serie y el constraint único de base respalda el invariante."""
    _exigir_administracion(actor)
    configuracion = ConfiguracionTicketGeneral.bloquear()
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.es_ticket_general:
        return servicio
    if servicio.tipo != Servicio.Tipo.SERVICIO:
        raise ValidationError("El servicio interno del ticket general debe ser un Servicio, no un Proceso.")
    if _tiene_tickets(servicio):
        raise ValidationError(
            "Este servicio ya tiene tickets del catálogo: no puede convertirse en el ticket general."
        )
    anterior = Servicio.objects.select_for_update().filter(es_ticket_general=True).first()
    if anterior is not None:
        if _tiene_tickets(anterior):
            raise ValidationError(
                "El servicio interno actual ya tiene tickets: edítalo en lugar de cambiarlo por otro."
            )
        anterior.es_ticket_general = False
        anterior.save(update_fields=["es_ticket_general", "actualizado_en"])
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=anterior,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores={"es_ticket_general": True}, datos_nuevos={"es_ticket_general": False},
        )
    servicio.es_ticket_general = True
    servicio.save(update_fields=["es_ticket_general", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"es_ticket_general": False}, datos_nuevos={"es_ticket_general": True},
    )
    if configuracion.habilitado:
        configuracion.habilitado = False
        configuracion.save(update_fields=["habilitado", "actualizado_en"])
        _auditar_configuracion(
            configuracion, actor, {"habilitado": True}, {"habilitado": False}, RegistroAuditoria.Accion.ACTUALIZAR
        )
    return servicio


@transaction.atomic
def crear_servicio_ticket_general(actor, *, categoria, nombre=NOMBRE_POR_DEFECTO):
    """Crea el Servicio interno desde cero (alta explícita, nunca implícita) y lo
    designa. Nace como Servicio en borrador y PÚBLICO INTERNO (cualquier persona
    de la organización podrá usar el Ticket General cuando se habilite; se puede
    restringir en Studio como cualquier Servicio). El formulario genérico NO se
    crea aquí: se construye y versiona en Studio con el Form Builder de siempre
    (asunto, descripción, adjuntos son campos configurables, no código)."""
    _exigir_administracion(actor)
    ConfiguracionTicketGeneral.bloquear()
    if servicio_ticket_general() is not None:
        raise ValidationError("Ya existe un servicio interno para el ticket general.")
    if not isinstance(categoria, Categoria) or not categoria.activo:
        raise ValidationError("Selecciona una categoría activa para el servicio interno.")
    servicio = crear_servicio(
        actor, nombre=(nombre or "").strip() or NOMBRE_POR_DEFECTO, categoria=categoria,
        tipo=Servicio.Tipo.SERVICIO,
        descripcion="Para solicitar algo que no corresponde a un servicio o proceso del catálogo.",
    )
    servicio.alcance_visibilidad = Servicio.AlcanceVisibilidad.PUBLICO_INTERNO
    servicio.save(update_fields=["alcance_visibilidad", "actualizado_en"])
    return designar_servicio_ticket_general(servicio, actor)


def servicios_candidatos():
    """Servicios que podrían designarse como interno: Servicios (no Procesos) sin
    tickets y todavía no marcados."""
    from apps.tickets.models import TicketServicio

    return (
        Servicio.objects.filter(tipo=Servicio.Tipo.SERVICIO, es_ticket_general=False)
        .exclude(pk__in=TicketServicio.objects.values("servicio_id"))
        .order_by("nombre")
    )


def estado_para_administracion():
    """Lo que necesita la pantalla de administración: configuración, Servicio
    interno y por qué (si no) puede habilitarse."""
    configuracion = ConfiguracionTicketGeneral.actual()
    servicio = servicio_ticket_general()
    problemas = []
    if servicio is None:
        problemas.append("Crea o elige el servicio interno.")
    else:
        if not servicio.activo:
            problemas.append("Publica el servicio interno.")
        try:
            validar_publicacion(servicio)
        except ValidationError as exc:
            problemas.extend(exc.messages)
    if not destinos.hay_destinos_utilizables():
        problemas.append("Configura al menos un destino activo con un responsable válido.")
    return {
        "configuracion": configuracion,
        "habilitado": configuracion.habilitado,
        "servicio": servicio,
        "problemas": problemas,
        "puede_habilitar": not problemas,
        "candidatos": servicios_candidatos() if servicio is None or not _tiene_tickets(servicio) else [],
        "categorias": Categoria.objects.filter(activo=True).order_by("nombre"),
        # 4.C2 — destinos: una sola lista para la tarjeta de administración.
        "destinos": destinos.listar_para_administracion(),
        "destino_candidatos": destinos.candidatos_para_administracion(),
        "destino_predeterminado_id": configuracion.destino_predeterminado_id,
    }
