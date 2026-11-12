"""Ticket de PRUEBA de la demo «Parrilla mensual - Solar Andina - Demo» (`preparar_demo_orbita --fase ejemplos
--casos solar`): como el de Imperandina (`demo.imperandina`), la ejecución del periodo sale de la programación
(nace el día 2 del mes) y Laura registra las ideas del contrato (`contrato.IDEAS_SOLAR`) como BORRADORES de la
lluvia de ideas; ninguna se envía. Idempotente: con la ejecución creada no genera otra y con ítems no agrega ideas."""

from datetime import date

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.catalogo.demo import contrato
from apps.catalogo.demo.imperandina import _ejecucion
from apps.catalogo.demo.participantes import Participantes
from apps.catalogo.models import ProgramacionProceso


def _valores_de(idea, columnas, periodo_inicio, responsable):
    valores = {
        contrato.NOMBRE_PIEZA: idea.nombre, contrato.COPY: idea.copy, contrato.CONTENIDO: idea.contenido,
        contrato.TIPO_PIEZA: idea.tipo, contrato.FORMATO_PIEZA: idea.formato,
        contrato.FECHA_PUBLICACION: date(periodo_inicio.year, periodo_inicio.month, idea.dia).isoformat(),
        contrato.MEDIOS_PUBLICACION: list(idea.medios),
        contrato.RESPONSABLE_PUBLICACION: str(responsable.pk) if responsable is not None else None,
    }
    return {columnas[clave].pk: valor for clave, valor in valores.items() if clave in columnas and valor is not None}


def ticket_de_prueba(recurso, hoy=None):
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.models import EjecucionProgramada, Ticket
    from apps.tickets.operaciones import iniciar_atencion_ticket

    servicio = recurso.servicio
    programacion = ProgramacionProceso.objects.filter(servicio=servicio).first()
    if not servicio.activo or programacion is None or not programacion.activa:
        return ["Primero publica el caso (--fase publicar --casos solar): crea la programación mensual."]
    hoy = hoy or timezone.localdate()
    p = Participantes.desde_parametros(recurso.parametros)
    if p.planificador is None or not p.planificador.is_active:
        raise ValidationError("La planificadora elegida al preparar ya no existe o no está activa.")

    ejecucion, adelantada = _ejecucion(programacion, hoy)
    lineas = []
    if ejecucion is None:
        ejecucion = EjecucionProgramada.objects.filter(servicio=servicio).order_by("-periodo_inicio").first()
        lineas.append(f"Ya existía la ejecución {ejecucion.ticket.codigo} «{ejecucion.ticket.etiqueta}».")
    else:
        lineas.append(
            f"Ticket de prueba {ejecucion.ticket.codigo} «{ejecucion.ticket.etiqueta}»"
            + (" (adelantado: no se esperó al día de creación)." if adelantada else ".")
        )
    ticket = Ticket.objects.get(pk=ejecucion.ticket_id)
    if ticket.estado == Ticket.Estado.RADICADO:
        iniciar_atencion_ticket(ticket, p.planificador)
        ticket.refresh_from_db()
        lineas.append(f"{p.planificador.email} inició la atención.")
    parrilla = parrilla_ops.parrilla_de(ticket)
    if parrilla is None:
        raise ValidationError("El ticket no tiene la colección de publicaciones.")
    if parrilla.elementos.exists():
        lineas.append("El ticket ya tiene publicaciones: no se agregaron ideas.")
        return lineas
    columnas = {campo.clave: campo for campo in parrilla_ops.columnas_de(parrilla)}
    for idea in contrato.IDEAS_SOLAR:
        parrilla_ops.crear_elemento(
            ticket, p.planificador, _valores_de(idea, columnas, ejecucion.periodo_inicio, p.responsable_publicacion),
            clave=parrilla.clave,
        )
    lineas.append(
        f"{len(contrato.IDEAS_SOLAR)} ideas registradas como borradores de la lluvia de ideas (ninguna enviada). "
        f"Entra como {p.planificador.email} para revisarlas y enviarlas; Producción vence el día 20."
    )
    return lineas
