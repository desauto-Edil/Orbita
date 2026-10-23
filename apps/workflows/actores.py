"""Resolución de actores de definición a responsables reales (4.3).

Se resuelven al entrar en la etapa; las Tareas/Aprobaciones ya creadas
conservan su asignación y sus propias operaciones de reasignación.
No cambia la responsabilidad del Ticket ni la policy de Entregables.
"""

from django.core.exceptions import ValidationError


ACTORES_DINAMICOS = frozenset({"RESPONSABLE_TICKET", "SOLICITANTE"})
# Fase 2 — la persona o el equipo que indica una columna (USUARIO / EQUIPO) del ítem de un recorrido. Solo se
# resuelve dentro de una subinstancia por ítem; la columna se identifica por su clave estable.
ACTOR_COLUMNA = "COLUMNA"
ACTORES = frozenset({"USUARIO", "EQUIPO", ACTOR_COLUMNA}) | ACTORES_DINAMICOS


def validar_actor(tipo, *, usuario=None, equipo=None, permite_vacio=False, columna=""):
    """Contrato común para configuración y resolución, sin inferir referencias."""
    if tipo == "USUARIO" and usuario is not None and equipo is None:
        return
    if tipo == "EQUIPO" and equipo is not None and usuario is None:
        return
    if tipo == ACTOR_COLUMNA:
        if columna and usuario is None and equipo is None:
            return
        raise ValidationError("Indica la columna del ítem de la que sale el responsable.")
    if (tipo in ACTORES_DINAMICOS or (permite_vacio and tipo == "")) and usuario is None and equipo is None:
        return
    raise ValidationError("Actor inválido o referencias incompatibles con su tipo.")


def _ticket_vigente(instancia):
    from apps.tickets.models import Ticket

    # Relación canónica existente: Ticket.instancia_workflow. No nueva FK. Una subinstancia (Fase 2) es del
    # Ticket de su instancia principal.
    ticket = Ticket.objects.select_for_update(of=("self",)).filter(instancia_workflow_id=instancia.raiz_id).first()
    if ticket is not None:
        return ticket

    # iniciar_workflow ejecuta INICIO y las siguientes etapas ANTES de
    # retornar a radicar_ticket, que entonces guarda la relación. Solo en
    # ese arranque se usa el ticket_id que la radicación ya proporcionaba.
    ticket_id = instancia.contexto.get("datos_iniciales", {}).get("ticket_id")
    if not isinstance(ticket_id, int) or isinstance(ticket_id, bool):
        raise ValidationError("Este actor necesita una ejecución vinculada a un Ticket.")
    ticket = Ticket.objects.select_for_update(of=("self",)).filter(
        pk=ticket_id, estado=Ticket.Estado.BORRADOR, instancia_workflow__isnull=True,
        detalle_servicio__servicio__workflow_id=instancia.workflow_version.workflow_id,
    ).first()
    if ticket is None:
        raise ValidationError("El Ticket no corresponde al inicio de esta ejecución.")
    return ticket


def _actor_de_columna(instancia, columna):
    """(usuario, equipo) de la columna `columna` del ítem de la subinstancia `instancia`."""
    elemento_id = getattr(instancia, "elemento_id", None)
    if elemento_id is None:
        raise ValidationError("Un responsable tomado de una columna solo existe en el recorrido de un ítem.")
    from apps.tickets.models import ElementoParrilla, ValorElemento

    elemento = ElementoParrilla.objects.select_related("parrilla").get(pk=elemento_id)
    valor = (
        ValorElemento.objects.select_related("campo", "valor_usuario", "valor_equipo")
        .filter(elemento=elemento, campo__clave=columna).first()
    )
    if valor is not None and valor.campo.tipo == "USUARIO" and valor.valor_usuario is not None:
        if not valor.valor_usuario.is_active:
            raise ValidationError(f"La persona del ítem #{elemento.numero} en «{valor.campo.etiqueta}» no está activa.")
        return valor.valor_usuario, None
    if valor is not None and valor.campo.tipo == "EQUIPO" and valor.valor_equipo is not None:
        if not valor.valor_equipo.activo:
            raise ValidationError(f"El equipo del ítem #{elemento.numero} en «{valor.campo.etiqueta}» no está activo.")
        return None, valor.valor_equipo
    raise ValidationError(
        f"El ítem #{elemento.numero} no tiene responsable en la columna «{columna}»: complétalo y reintenta."
    )


def resolver_actor(tipo, *, instancia, usuario=None, equipo=None, permite_vacio=False, columna=""):
    """Devuelve (usuario, equipo), nunca expande un equipo a todos sus miembros.

    Se llama dentro de la transacción del motor. El lock del Ticket evita
    leer una responsabilidad que está siendo reasignada simultáneamente.
    Un responsable individual ausente es un error explícito, no un equipo
    ni una Tarea sin asignar inventados por el resolutor.
    """
    validar_actor(tipo, usuario=usuario, equipo=equipo, permite_vacio=permite_vacio, columna=columna)
    if tipo == ACTOR_COLUMNA:
        return _actor_de_columna(instancia, columna)
    if tipo in ACTORES_DINAMICOS:
        ticket = _ticket_vigente(instancia)
        usuario = ticket.solicitante if tipo == "SOLICITANTE" else ticket.usuario_responsable
        if usuario is None:
            raise ValidationError("El Ticket todavía no tiene un responsable individual asignado.")
    return usuario, equipo
