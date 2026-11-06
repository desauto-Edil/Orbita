"""Ticket de PRUEBA de la demo «Parrilla mensual - Imperandina» (`preparar_demo_orbita --fase ejemplos --casos
imperandina`), sin esperar al día de creación.

1. La ejecución sale de la programación con las operaciones de siempre: si la del periodo ya venció,
   `generar_ejecucion_programada`; si no (p. ej. hoy es 29 y se creó el 28, antes de activar la programación), se
   ADELANTA la ejecución del periodo de la última fecha de creación con el mismo núcleo (`programadas._crear_ejecucion`,
   bajo el lock de la programación y con `errores_de_generacion`). La unicidad de `EjecucionProgramada` impide
   duplicarla y el reconciliador sigue con el periodo siguiente sin tocar este.
2. Laura (la responsable inicial) inicia la atención y registra las ideas del contrato como BORRADORES de la lluvia
   de ideas (`parrilla.crear_elemento`): ninguna se envía, aprueba ni avanza; el flujo se prueba a mano.

Idempotente: con la ejecución ya creada no genera otra, y si el ticket ya tiene ítems no agrega ideas.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.catalogo.demo import contrato
from apps.catalogo.demo.participantes import Participantes
from apps.catalogo.models import ProgramacionProceso


def _ejecucion(programacion, hoy):
    """`(ejecucion, adelantada)` del periodo vigente; `(None, False)` si ya existía."""
    from apps.catalogo.programacion import errores_de_generacion
    from apps.tickets import periodos, programadas

    ejecucion = programadas.generar_ejecucion_programada(programacion, hoy=hoy)
    if ejecucion is not None:
        return ejecucion, False
    programacion = (
        ProgramacionProceso.objects.select_for_update(of=("self",))
        .select_related("servicio", "responsable_inicial__usuario", "responsable_inicial__equipo")
        .get(pk=programacion.pk)
    )
    creacion = periodos.creacion_vencida_mas_reciente(hoy, programacion.dia_creacion)
    periodo = periodos.periodo_de_creacion(creacion, programacion.periodo)
    if programadas._existe(programacion, periodo):
        return None, False
    errores = errores_de_generacion(programacion)
    if errores:
        raise ValidationError(errores)
    return programadas._crear_ejecucion(programacion, periodo), True


def _valores_de(idea, columnas, periodo_inicio, responsable):
    valores = {
        contrato.COPY: idea.copy, contrato.TIPO_PIEZA: idea.tipo, contrato.MEDIO_PUBLICACION: idea.medio,
        contrato.CONTENIDO: idea.contenido,
        contrato.FECHA_PUBLICACION: date(periodo_inicio.year, periodo_inicio.month, idea.dia).isoformat(),
        contrato.RESPONSABLE_PUBLICACION: str(responsable.pk) if responsable is not None else None,
    }
    return {columnas[clave].pk: valor for clave, valor in valores.items() if clave in columnas and valor is not None}


def ticket_de_prueba(recurso, hoy=None):
    """Crea (o encuentra) el ticket de prueba y deja sus ideas como borradores. Devuelve las líneas del informe."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.models import EjecucionProgramada, Ticket
    from apps.tickets.operaciones import iniciar_atencion_ticket

    caso = contrato.CONTRATOS[recurso.caso]
    servicio = recurso.servicio
    programacion = ProgramacionProceso.objects.filter(servicio=servicio).first()
    if not servicio.activo or programacion is None or not programacion.activa:
        return ["Primero publica el caso (--fase publicar --casos imperandina): crea la programación mensual."]
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
        lineas.append(f"{p.planificador.get_username()} inició la atención.")
    parrilla = parrilla_ops.parrilla_de(ticket)
    if parrilla is None:
        raise ValidationError("El ticket no tiene la colección de publicaciones.")
    if parrilla.elementos.exists():
        lineas.append("El ticket ya tiene publicaciones: no se agregaron ideas.")
        return lineas
    columnas = {campo.clave: campo for campo in parrilla_ops.columnas_de(parrilla)}
    for idea in caso.ideas:
        parrilla_ops.crear_elemento(
            ticket, p.planificador, _valores_de(idea, columnas, ejecucion.periodo_inicio, p.responsable_publicacion),
            clave=parrilla.clave,
        )
    lineas.append(
        f"{len(caso.ideas)} ideas registradas como borradores de la lluvia de ideas (ninguna enviada). "
        f"Entra como {p.planificador.get_username()} para revisarlas y enviarlas."
    )
    return lineas
