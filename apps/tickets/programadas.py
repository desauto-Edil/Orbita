"""Generación automática de ejecuciones de Procesos programados — Sprint 4.G1.

Un Proceso programado (`catalogo.ProgramacionProceso`) produce, cada periodo, UN Ticket normal
(`tipo=PROCESO`, `origen=PROGRAMACION`, sin solicitante) que entra al circuito operativo de siempre
(Cola → Tomar → Mi trabajo → flujo por fases). No hay un modelo de ticket paralelo ni una UI nueva.

Piezas:

- `periodos.py` calcula fechas (módulo puro).
- `generar_ejecucion_programada` valida, garantiza la idempotencia y crea el Ticket. NO reutiliza
  `radicar_ticket` (que exige un solicitante propietario): comparte con él el núcleo interno
  `operaciones._radicar_nucleo`, de modo que la radicación de un ticket manual no cambia.
- `reconciliar_ejecuciones_programadas` es lo que ejecuta Celery Beat (una tarea para todos los
  Procesos, ver `tasks.generar_ejecuciones_programadas`).

Idempotencia («Proceso X, periodo Y» = un solo Ticket): (1) `select_for_update` sobre la fila de la
programación serializa a los workers; (2) la restricción única de `EjecucionProgramada`
(`servicio`, `frecuencia`, `periodo_inicio`) lo garantiza en base de datos aunque (1) fallara;
(3) la ejecución y su Ticket se crean en una sola transacción: si algo falla no queda nada, ni
siquiera el consecutivo TCK.

Reconciliación en vez de «hoy es el día N»: se calcula qué ejecución YA DEBERÍA existir
(`periodos.creacion_vencida_mas_reciente`) y se crea si falta. Así un Beat apagado el día 25 se
recupera el 26. Solo se genera la ejecución vencida MÁS RECIENTE; los periodos anteriores que
faltaron no se rellenan en silencio (`periodos_omitidos` permite detectarlos). Tampoco se generan
periodos anteriores a `activada_desde`.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalogo.models import ProgramacionProceso, ServicioResponsable
from apps.catalogo.programacion import errores_de_generacion, foto_de_programacion
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets import periodos
from apps.tickets.models import EjecucionProgramada, Ticket
from apps.tickets.operaciones import _auditar_cambio_responsable, _crear_borrador_de_servicio, _radicar_nucleo

logger = logging.getLogger(__name__)

MAX_LARGO_ERROR = 2000


def _existe(programacion, periodo):
    return EjecucionProgramada.objects.filter(
        servicio_id=programacion.servicio_id, frecuencia=programacion.frecuencia, periodo_inicio=periodo.inicio
    ).exists()


def periodo_pendiente(programacion, hoy):
    """El `Periodo` que YA debería existir para `programacion` y todavía no existe, o `None`.
    Solo lectura. Es la ejecución vencida más reciente cuya fecha de creación no es anterior a
    `activada_desde` (nunca retroactivo)."""
    if not programacion.activa or programacion.activada_desde is None:
        return None
    creacion = periodos.creacion_vencida_mas_reciente(hoy, programacion.dia_creacion)
    if creacion < programacion.activada_desde:
        return None
    periodo = periodos.periodo_de_creacion(creacion, programacion.periodo)
    return None if _existe(programacion, periodo) else periodo


def proxima_ejecucion(programacion, hoy):
    """Qué sigue para `programacion`: `(fecha_de_creacion, Periodo, vencida)`. `vencida=True` = la
    ejecución YA debería existir y falta (se genera en la próxima revisión horaria o con «Generar
    ahora»); `False` = es la próxima fecha de creación, todavía en el futuro. Solo lectura."""
    pendiente = periodo_pendiente(programacion, hoy)
    if pendiente is not None:
        return periodos.creacion_vencida_mas_reciente(hoy, programacion.dia_creacion), pendiente, True
    creacion = periodos.creacion_siguiente(hoy, programacion.dia_creacion)
    return creacion, periodos.periodo_de_creacion(creacion, programacion.periodo), False


def periodos_omitidos(programacion, hoy):
    """Periodos cuya fecha de creación ya pasó (desde `activada_desde`) y NO tienen ejecución,
    sin contar el vencido más reciente. Para detectarlos y reportarlos; la generación automática
    nunca los rellena."""
    if not programacion.activa or programacion.activada_desde is None:
        return []
    ultima = periodos.creacion_vencida_mas_reciente(hoy, programacion.dia_creacion)
    fechas = [
        f for f in periodos.creaciones_entre(programacion.activada_desde, ultima, programacion.dia_creacion)
        if f != ultima
    ]
    omitidos = []
    for fecha in fechas:
        periodo = periodos.periodo_de_creacion(fecha, programacion.periodo)
        if not _existe(programacion, periodo):
            omitidos.append(periodo)
    return omitidos


def _responsable_inicial(ticket, responsable):
    if responsable.tipo_responsable == ServicioResponsable.TipoResponsable.USUARIO:
        ticket.usuario_responsable = responsable.usuario
    else:
        ticket.equipo_responsable = responsable.equipo


def _crear_ejecucion(programacion, periodo):
    """Crea el Ticket programado (BORRADOR → RADICADO por el núcleo común) y su identidad. Debe
    llamarse con la programación bloqueada y dentro de una transacción."""
    servicio = programacion.servicio
    responsable = programacion.responsable_inicial
    ticket = _crear_borrador_de_servicio(
        None, servicio, origen=Ticket.Origen.PROGRAMACION, etiqueta=periodo.etiqueta
    )
    # El responsable inicial se deja ANTES de arrancar el flujo: un bloque dirigido al responsable
    # del ticket lo encuentra desde el primer momento. No inicia la atención: el ticket queda
    # RADICADO (equipo → Cola, y se toma; persona → la Cola de esa persona, y inicia la atención).
    _responsable_inicial(ticket, responsable)
    ticket.save()
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    _radicar_nucleo(
        ticket, servicio, None,
        datos_historial={
            "origen": Ticket.Origen.PROGRAMACION,
            "etiqueta": periodo.etiqueta,
            "periodo_inicio": periodo.inicio.isoformat(),
            "periodo_fin": periodo.fin.isoformat(),
            "responsable_usuario_id": ticket.usuario_responsable_id,
            "responsable_equipo_id": ticket.equipo_responsable_id,
        },
    )
    ejecucion = EjecucionProgramada.objects.create(
        servicio=servicio, frecuencia=programacion.frecuencia, periodo_inicio=periodo.inicio,
        periodo_fin=periodo.fin, etiqueta=periodo.etiqueta, ticket=ticket,
        generada_en=timezone.now(), programacion_foto=foto_de_programacion(programacion),
    )
    # Auditoría con origen SISTEMA (no hay actor): quién quedó como responsable y de qué periodo es.
    _auditar_cambio_responsable(
        ticket, None, usuario_anterior_id=None, usuario_nuevo_id=ticket.usuario_responsable_id,
        equipo_anterior_id=None, equipo_nuevo_id=ticket.equipo_responsable_id,
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=ejecucion,
        origen=RegistroAuditoria.Origen.SISTEMA, usuario=None,
        datos_nuevos={
            "ticket_id": ticket.pk, "servicio_id": servicio.pk, "frecuencia": ejecucion.frecuencia,
            "periodo_inicio": periodo.inicio.isoformat(), "periodo_fin": periodo.fin.isoformat(),
            "etiqueta": periodo.etiqueta, "programacion": ejecucion.programacion_foto,
        },
    )
    return ejecucion


@transaction.atomic
def generar_ejecucion_programada(programacion, *, hoy=None):
    """Genera la ejecución que `programacion` debe tener a la fecha local `hoy` (por defecto,
    `timezone.localdate()`), o no hace nada.

    Devuelve la `EjecucionProgramada` creada, o `None` si no había nada por generar (pausada, aún
    no vence, anterior a `activada_desde` o ya existía: llamarla dos veces no duplica). Si algo
    impide generar (responsable inactivo, flujo incompatible…) levanta `ValidationError` y NO crea
    nada; `reconciliar_ejecuciones_programadas` lo registra en la programación."""
    hoy = hoy or timezone.localdate()
    programacion = (
        # `of=("self",)`: solo se bloquea la fila de la programación (Postgres no admite FOR UPDATE
        # sobre el lado nulable de un LEFT JOIN, y el responsable inicial es opcional en el modelo).
        ProgramacionProceso.objects.select_for_update(of=("self",))
        .select_related("servicio", "responsable_inicial__usuario", "responsable_inicial__equipo")
        .get(pk=programacion.pk)
    )
    periodo = periodo_pendiente(programacion, hoy)
    if periodo is None:
        return None
    errores = errores_de_generacion(programacion)
    if errores:
        raise ValidationError(errores)
    try:
        with transaction.atomic():
            ejecucion = _crear_ejecucion(programacion, periodo)
    except IntegrityError:
        # Red de seguridad de base de datos: otra transacción generó este periodo. Todo lo creado
        # aquí (incluido el consecutivo) ya se deshizo con el savepoint.
        if _existe(programacion, periodo):
            return None
        raise
    ProgramacionProceso.objects.filter(pk=programacion.pk).update(
        ultimo_intento_en=timezone.now(), ultimo_error=""
    )
    return ejecucion


def registrar_resultado_de_intento(programacion_id, error=""):
    """Deja en la programación cuándo se intentó generar y el error (vacío = funcionó). `update()`
    deliberado: no pasa por `clean()` ni depende de que la fila siga siendo válida."""
    ProgramacionProceso.objects.filter(pk=programacion_id).update(
        ultimo_intento_en=timezone.now(), ultimo_error=(error or "")[:MAX_LARGO_ERROR]
    )


def reconciliar_ejecuciones_programadas(hoy=None):
    """Revisa TODAS las programaciones activas y genera la ejecución que falte en cada una. Un error
    en una programación se registra en ella y NO impide procesar las demás. Devuelve un resumen
    `{"generadas": n, "con_error": n, "omitidos": n}` (`omitidos` = periodos anteriores que no
    se generaron y se reportan, nunca se rellenan)."""
    hoy = hoy or timezone.localdate()
    resumen = {"generadas": 0, "con_error": 0, "omitidos": 0}
    for programacion in ProgramacionProceso.objects.filter(activa=True).order_by("pk"):
        try:
            ejecucion = generar_ejecucion_programada(programacion, hoy=hoy)
        except ValidationError as exc:
            resumen["con_error"] += 1
            registrar_resultado_de_intento(programacion.pk, "; ".join(exc.messages))
            logger.warning("Programación %s no generó su ejecución: %s", programacion.pk, "; ".join(exc.messages))
            continue
        except Exception as exc:  # noqa: BLE001 — código/infraestructura inesperados; no tumba el lote.
            resumen["con_error"] += 1
            registrar_resultado_de_intento(programacion.pk, f"Error inesperado: {exc}")
            logger.exception("Error generando la ejecución de la programación %s.", programacion.pk)
            continue
        if ejecucion is not None:
            resumen["generadas"] += 1
            omitidos = periodos_omitidos(programacion, hoy)
            if omitidos:
                resumen["omitidos"] += len(omitidos)
                logger.warning(
                    "Programación %s: %s periodo(s) anterior(es) sin ejecución (no se rellenan): %s",
                    programacion.pk, len(omitidos), ", ".join(p.etiqueta for p in omitidos),
                )
    return resumen
