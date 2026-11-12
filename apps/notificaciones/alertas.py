"""Alertas, recordatorios y escalamientos — CU-034 parcial (RQF-101, RQF-103), CU-033 parcial (umbrales).

Un evaluador PERIÓDICO (Beat, cada hora: `notificaciones.evaluar_alertas`) mira situaciones de tiempo que ya
existen en el dominio y avisa una sola vez por umbral:

| Situación                                         | Se basa en                                   | Avisa a                 |
|---------------------------------------------------|----------------------------------------------|-------------------------|
| Ticket próximo a vencer / vencido                 | `fecha_objetivo_vigente` (umbral en %)       | responsable             |
| Ticket sin tomar                                  | `radicado_en` + horas                        | equipo (pueden tomarlo) |
| Actividad próxima a vencer / vencida / pendiente  | `Tarea.fecha_limite` o fecha de asignación   | responsable de la tarea |
| Aprobación pendiente                              | llegada del turno                            | aprobador               |
| Información sin respuesta                         | `SolicitudInformacion.solicitada_en`         | solicitante             |
| Entrega por aceptar automáticamente               | `EntregaTicket.vence_en` (24 h antes)        | solicitante             |
| Gaceta por cerrar / corrección por vencer         | plazos de la gaceta (24 h antes)             | revisores / corrector   |
| Proceso detenido                                  | ejecución esperando corrección o con error   | responsable             |
| Falló la generación programada                    | `ProgramacionProceso.ultimo_error`           | responsable inicial     |
| ESCALAMIENTO (lo vencido sigue sin atenderse)     | vencido + horas                              | quien supervisa         |

Reglas:
- La SITUACIÓN (`Alerta.base`) se emite una vez por ocurrencia (`Alerta.clave` única); repetir un recordatorio es
  otra ocurrencia, hasta el máximo configurado. Dos evaluaciones simultáneas no duplican (constraint única).
- Cuando la situación desaparece (se atendió, se cerró, cambió la fecha), la alerta se RESUELVE sola.
- No cambia nada del dominio: no reasigna, no pausa ni mueve fechas (las pausas de SLA quedan fuera, RQF-102).
- Los tiempos usan días calendario y la hora local (`TIME_ZONE`); el calendario laboral configurable está fuera."""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.notificaciones import configuracion as cfg, destinatarios as dest
from apps.notificaciones.despacho import Aviso, entregar
from apps.notificaciones.models import Alerta

logger = logging.getLogger(__name__)

VENTANA_PREVIA = timedelta(hours=24)
ESTADOS_ABIERTOS = ("RADICADO", "EN_ATENCION")


class Evaluacion:
    """Una pasada del evaluador: emite lo que falta y recuerda qué situaciones siguen vivas para resolver el resto."""

    def __init__(self, ahora=None):
        self.ahora = ahora or timezone.now()
        self.vivas = set()
        self.emitidas = 0

    def situacion(self, base, *, aviso, ocurrencia=1, ticket=None, objeto=("ticket", 0), usuarios=(), contexto=None,
                  escalamiento=False):
        """Registra que la situación `base` existe y, si su ocurrencia aún no se avisó, la avisa."""
        self.vivas.add(base)
        clave = f"{base}#{ocurrencia}"
        if Alerta.objects.filter(clave=clave).exists():
            return None
        usuarios = [u for u in usuarios if u is not None]
        try:
            with transaction.atomic():
                alerta = Alerta.objects.create(
                    tipo=aviso, base=base[:200], clave=clave[:210], ticket=ticket, objeto_tipo=objeto[0],
                    objeto_id=objeto[1], ocurrencia=ocurrencia, escalamiento=escalamiento,
                    destinatarios=len(usuarios),
                )
        except IntegrityError:
            return None  # Otra evaluación la emitió primero.
        entregar([
            Aviso(usuario=u, clave=aviso, ticket=ticket, contexto=dict(contexto or {}), agrupacion=f"ALERTA:{clave}")
            for u in usuarios
        ])
        self.emitidas += 1
        return alerta

    def resolver_las_demas(self):
        """Las alertas ACTIVAS cuya situación ya no existe quedan RESUELTAS (historia, RQF-103)."""
        resueltas = 0
        for alerta in Alerta.objects.filter(estado=Alerta.Estado.ACTIVA).only("pk", "base"):
            if alerta.base not in self.vivas:
                Alerta.objects.filter(pk=alerta.pk).update(estado=Alerta.Estado.RESUELTA, resuelta_en=self.ahora)
                resueltas += 1
        return resueltas


def _ocurrencia(inicio, ahora, cada_dias, maximo):
    """Número de recordatorio que ya toca (1…máximo) desde `inicio`, o 0 si todavía no."""
    if inicio is None or ahora < inicio:
        return 0
    transcurrido = ahora - inicio
    return min(maximo, int(transcurrido / timedelta(days=max(1, cada_dias))) + 1)


def _iso(valor):
    return valor.isoformat() if valor else ""


# --- Tickets -----------------------------------------------------------------------------------------------


def estado_de_plazo(ticket, ahora=None):
    """«vencido» · «por_vencer» · "" para un ticket abierto, con el umbral efectivo (el del ticket o el general).
    Es la regla que usa el evaluador y la que marcan las listas."""
    if ticket is None or ticket.estado not in ESTADOS_ABIERTOS:
        return ""
    vigente, inicio = ticket.fecha_objetivo_vigente, ticket.radicado_en
    if not vigente or not inicio or vigente <= inicio:
        return ""
    ahora = ahora or timezone.now()
    if ahora >= vigente:
        return "vencido"
    consumido = (ahora - inicio) / (vigente - inicio)
    return "por_vencer" if consumido * 100 >= cfg.parametros_de(ticket)["por_vencer_porcentaje"] else ""



def _tickets_abiertos():
    from apps.tickets.models import Ticket

    return Ticket.objects.filter(estado__in=ESTADOS_ABIERTOS).select_related(
        "detalle_servicio__servicio", "solicitante", "usuario_responsable", "equipo_responsable")


def _escalar(ev, ticket, base, contexto):
    parametros = cfg.parametros_de(ticket)
    if not parametros["escalar"]:
        return
    destino = cfg.escalamiento_de(ticket)
    if destino.get("tipo") == "USUARIO":
        usuarios = dest.personas([destino.get("usuario_id")])
    elif destino.get("tipo") == "EQUIPO":
        usuarios = dest.miembros_de(destino.get("equipo_id"))
    else:
        usuarios = dest.supervisores(ticket)
    if not usuarios:
        logger.info("Escalamiento sin destinatarios para el ticket %s (%s).", ticket.pk, base)
    ev.situacion(f"ESCALAMIENTO:{base}", aviso="ESCALAMIENTO", ticket=ticket, objeto=("ticket", ticket.pk),
                 usuarios=usuarios, contexto=contexto, escalamiento=True)


def evaluar_tickets(ev):
    for ticket in _tickets_abiertos():
        parametros = cfg.parametros_de(ticket)
        vigente, inicio = ticket.fecha_objetivo_vigente, ticket.radicado_en
        responsables = dest.responsable(ticket)
        if vigente and inicio and vigente > inicio:
            consumido = (ev.ahora - inicio) / (vigente - inicio)
            marca = _iso(vigente)
            if ev.ahora >= vigente:
                base = f"ALERTA_TICKET_VENCIDO:ticket:{ticket.pk}:{marca}"
                contexto = {
                    "titulo": "Ticket vencido",
                    "resumen": f"«{_nombre(ticket)}» superó su fecha objetivo y sigue abierto.",
                }
                ev.situacion(base, aviso="ALERTA_TICKET_VENCIDO", ticket=ticket, objeto=("ticket", ticket.pk),
                             usuarios=responsables, contexto=contexto)
                if ev.ahora >= vigente + timedelta(hours=parametros["escalar_horas"]):
                    _escalar(ev, ticket, base, {
                        "titulo": "Escalamiento: ticket vencido",
                        "resumen": f"«{_nombre(ticket)}» lleva vencido más de {parametros['escalar_horas']} h y sigue abierto.",
                        "hechos_extra": [["Responsable", _quien(ticket)]],
                    })
            elif consumido * 100 >= parametros["por_vencer_porcentaje"]:
                ev.situacion(
                    f"ALERTA_TICKET_POR_VENCER:ticket:{ticket.pk}:{marca}", aviso="ALERTA_TICKET_POR_VENCER",
                    ticket=ticket, objeto=("ticket", ticket.pk), usuarios=responsables,
                    contexto={"titulo": "Ticket próximo a vencer",
                              "resumen": f"«{_nombre(ticket)}» ya consumió el {int(consumido * 100)} % de su tiempo objetivo."},
                )
        if (
            ticket.estado == "RADICADO" and ticket.usuario_responsable_id is None and ticket.equipo_responsable_id
            and inicio and ev.ahora >= inicio + timedelta(hours=parametros["sin_tomar_horas"])
        ):
            ev.situacion(
                f"ALERTA_SIN_TOMAR:ticket:{ticket.pk}", aviso="ALERTA_SIN_TOMAR", ticket=ticket,
                objeto=("ticket", ticket.pk), usuarios=dest.pueden_tomar(ticket),
                contexto={"titulo": "Ticket sin tomar",
                          "resumen": f"«{_nombre(ticket)}» lleva más de {parametros['sin_tomar_horas']} h en la Cola sin que nadie lo tome."},
            )


def _nombre(ticket):
    from apps.notificaciones.mensajes import nombre_ticket

    return nombre_ticket(ticket)


def _quien(ticket):
    if ticket.usuario_responsable_id:
        return ticket.usuario_responsable.nombre_visible
    return ticket.equipo_responsable.nombre if ticket.equipo_responsable_id else "Sin responsable"


# --- Tareas ------------------------------------------------------------------------------------------------


def _bloques_de_tareas(tarea_ids):
    from apps.workflows.models import TareaWorkflow

    return dict(
        TareaWorkflow.objects.filter(tarea_id__in=tarea_ids, instancia_etapa__bloque_operativo__isnull=False)
        .values_list("tarea_id", "instancia_etapa__bloque_operativo__clave")
    )


def evaluar_tareas(ev):
    from apps.tareas.models import Tarea
    from apps.tickets import vinculos
    from apps.tickets.models import Ticket

    tareas = list(Tarea.objects.exclude(estado__in=Tarea.ESTADOS_CERRADOS).select_related("usuario_responsable", "equipo_responsable"))
    if not tareas:
        return
    origen = vinculos.origen_de_tareas([t.pk for t in tareas])
    tickets = Ticket.objects.select_related("detalle_servicio__servicio", "solicitante").in_bulk(
        {o["ticket_id"] for o in origen.values() if o.get("ticket_id")})
    bloques = _bloques_de_tareas([t.pk for t in tareas])
    for tarea in tareas:
        ticket = tickets.get((origen.get(tarea.pk) or {}).get("ticket_id"))
        if ticket is not None and ticket.estado not in ESTADOS_ABIERTOS:
            continue
        parametros = dict(cfg.parametros_de(ticket))
        propio = cfg.recordatorio_de_actividad(ticket, bloques.get(tarea.pk))
        dias_antes = propio.get("dias_antes", parametros["tarea_dias_antes"])
        cada = propio.get("repetir_cada_dias", parametros["repetir_cada_dias"])
        maximo = propio.get("maximo", parametros["maximo_recordatorios"])
        usuarios = dest.de_tarea(tarea)
        contexto = {"tarea": tarea.titulo, "tarea_id": tarea.pk, "fecha_limite": tarea.fecha_limite}
        objeto = ("tarea", tarea.pk)
        if tarea.fecha_limite:
            marca = _iso(tarea.fecha_limite)
            if ev.ahora >= tarea.fecha_limite:
                base = f"ALERTA_TAREA_VENCIDA:tarea:{tarea.pk}:{marca}"
                n = _ocurrencia(tarea.fecha_limite, ev.ahora, cada, maximo)
                ev.situacion(base, aviso="ALERTA_TAREA_VENCIDA" if n <= 1 else "RECORDATORIO_TAREA", ocurrencia=max(1, n),
                             ticket=ticket, objeto=objeto, usuarios=usuarios, contexto={
                                 **contexto, "titulo": "Actividad vencida" if n <= 1 else "Recordatorio: actividad vencida",
                                 "resumen": f"La actividad «{tarea.titulo}» superó su fecha límite y sigue pendiente."})
                if ticket is not None and ev.ahora >= tarea.fecha_limite + timedelta(hours=parametros["escalar_horas"]):
                    _escalar(ev, ticket, base, {
                        **contexto, "titulo": "Escalamiento: actividad vencida",
                        "resumen": f"La actividad «{tarea.titulo}» lleva vencida más de {parametros['escalar_horas']} h.",
                    })
            elif dias_antes and ev.ahora >= tarea.fecha_limite - timedelta(days=dias_antes):
                ev.situacion(f"ALERTA_TAREA_POR_VENCER:tarea:{tarea.pk}:{marca}", aviso="ALERTA_TAREA_POR_VENCER",
                             ticket=ticket, objeto=objeto, usuarios=usuarios, contexto={
                                 **contexto, "titulo": "Actividad próxima a vencer",
                                 "resumen": f"La actividad «{tarea.titulo}» vence pronto."})
        elif parametros["tarea_dias_sin_fecha"] and tarea.estado == Tarea.Estado.PENDIENTE:
            inicio = tarea.creado_en + timedelta(days=parametros["tarea_dias_sin_fecha"])
            n = _ocurrencia(inicio, ev.ahora, cada, maximo)
            if n:
                ev.situacion(f"RECORDATORIO_TAREA:tarea:{tarea.pk}", aviso="RECORDATORIO_TAREA", ocurrencia=n,
                             ticket=ticket, objeto=objeto, usuarios=usuarios, contexto={
                                 **contexto, "titulo": "Recordatorio de una actividad",
                                 "resumen": f"La actividad «{tarea.titulo}» sigue sin empezar."})


# --- Aprobaciones, información, entregas --------------------------------------------------------------------


def _inicio_del_turno(aprobacion):
    """Desde cuándo le toca: la creación del esquema o, en una secuencia, la decisión anterior."""
    anterior = (
        aprobacion.esquema.participaciones.filter(orden__lt=aprobacion.orden, decidida_en__isnull=False)
        .order_by("-decidida_en").values_list("decidida_en", flat=True).first()
    )
    return max(filter(None, [aprobacion.esquema.creado_en, anterior]))


def evaluar_aprobaciones(ev):
    from apps.aprobaciones.models import EsquemaAprobacion
    from apps.tickets import vinculos
    from apps.tickets.models import Ticket

    for esquema in EsquemaAprobacion.objects.filter(resultado__isnull=True):
        for aprobacion in dest.aprobaciones_en_turno(esquema):
            origen = vinculos.origen_de_aprobaciones([aprobacion]).get(aprobacion.pk) or {}
            ticket = Ticket.objects.select_related("detalle_servicio__servicio").filter(pk=origen.get("ticket_id")).first()
            if ticket is not None and ticket.estado not in ESTADOS_ABIERTOS:
                continue
            parametros = cfg.parametros_de(ticket)
            if not parametros["aprobacion_dias"]:
                continue
            inicio = _inicio_del_turno(aprobacion) + timedelta(days=parametros["aprobacion_dias"])
            n = _ocurrencia(inicio, ev.ahora, parametros["repetir_cada_dias"], parametros["maximo_recordatorios"])
            if n:
                ev.situacion(f"RECORDATORIO_APROBACION:aprobacion:{aprobacion.pk}", aviso="RECORDATORIO_APROBACION",
                             ocurrencia=n, ticket=ticket, objeto=("aprobacion", aprobacion.pk),
                             usuarios=dest.de_aprobacion(aprobacion), contexto={
                                 "aprobacion_id": aprobacion.pk, "titulo": "Tienes una aprobación pendiente",
                                 "resumen": "Una aprobación sigue esperando tu decisión."})


def evaluar_informacion_y_entregas(ev):
    from apps.tickets.models import EntregaTicket, SolicitudInformacion

    for solicitud in SolicitudInformacion.objects.filter(
        estado=SolicitudInformacion.Estado.PENDIENTE, ticket__estado__in=ESTADOS_ABIERTOS
    ).select_related("ticket__detalle_servicio__servicio"):
        parametros = cfg.parametros_de(solicitud.ticket)
        if not parametros["informacion_dias"]:
            continue
        inicio = solicitud.solicitada_en + timedelta(days=parametros["informacion_dias"])
        n = _ocurrencia(inicio, ev.ahora, parametros["repetir_cada_dias"], parametros["maximo_recordatorios"])
        if n:
            ev.situacion(f"RECORDATORIO_INFORMACION:solicitud:{solicitud.pk}", aviso="RECORDATORIO_INFORMACION",
                         ocurrencia=n, ticket=solicitud.ticket, objeto=("solicitud", solicitud.pk),
                         usuarios=dest.personas([solicitud.destinatario_id]), contexto={
                             "titulo": "Te siguen esperando",
                             "resumen": f"Todavía falta la información que te pidieron para «{_nombre(solicitud.ticket)}».",
                             "extracto": solicitud.mensaje})
    for entrega in EntregaTicket.objects.filter(
        estado=EntregaTicket.Estado.PENDIENTE, vence_en__isnull=False, vence_en__gt=ev.ahora,
        vence_en__lte=ev.ahora + VENTANA_PREVIA,
    ).select_related("ticket__detalle_servicio__servicio", "ticket__solicitante"):
        ev.situacion(f"RECORDATORIO_ENTREGA:entrega:{entrega.pk}", aviso="RECORDATORIO_ENTREGA", ticket=entrega.ticket,
                     objeto=("entrega", entrega.pk), usuarios=dest.solicitante(entrega.ticket), contexto={
                         "vence_en": entrega.vence_en, "titulo": "Tu entrega se aceptará sola pronto",
                         "resumen": f"Si no la revisas, la entrega de «{_nombre(entrega.ticket)}» se aceptará automáticamente."})


# --- Gacetas y procesos ------------------------------------------------------------------------------------


def evaluar_gacetas(ev):
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta, RondaRevisionGaceta

    for gaceta in GacetaTicket.objects.filter(
        estado=GacetaTicket.Estado.ABIERTA, vence_revision_en__isnull=False, vence_revision_en__gt=ev.ahora,
        vence_revision_en__lte=ev.ahora + VENTANA_PREVIA,
    ).select_related("ticket__detalle_servicio__servicio"):
        ev.situacion(f"ALERTA_GACETA_POR_CERRAR:gaceta:{gaceta.pk}:{_iso(gaceta.vence_revision_en)}",
                     aviso="ALERTA_GACETA_POR_CERRAR", ticket=gaceta.ticket, objeto=("gaceta", gaceta.pk),
                     usuarios=dest.revisores_gaceta(gaceta), contexto={
                         "gaceta": gaceta.nombre, "vence_en": gaceta.vence_revision_en,
                         "titulo": "La revisión cierra pronto",
                         "resumen": f"El periodo de revisión de la gaceta «{gaceta.nombre}» cierra en menos de 24 horas."})
    for participacion in ParticipacionItemGaceta.objects.filter(
        estado="EN_CORRECCION", vence_correccion_en__isnull=False, vence_correccion_en__gt=ev.ahora,
        vence_correccion_en__lte=ev.ahora + VENTANA_PREVIA,
    ).select_related("gaceta__ticket__detalle_servicio__servicio", "elemento"):
        ronda = RondaRevisionGaceta.objects.filter(participacion=participacion, numero=participacion.ronda).select_related(
            "tarea_correccion").first()
        usuarios = dest.de_tarea(ronda.tarea_correccion) if ronda and ronda.tarea_correccion_id else dest.coordinadores_gaceta(participacion.gaceta)
        ev.situacion(f"ALERTA_CORRECCION_POR_VENCER:participacion:{participacion.pk}:{participacion.ronda}",
                     aviso="ALERTA_CORRECCION_POR_VENCER", ticket=participacion.gaceta.ticket,
                     objeto=("participacion", participacion.pk), usuarios=usuarios, contexto={
                         "gaceta": participacion.gaceta.nombre, "titulo": "Una corrección está por vencer",
                         "resumen": f"La corrección pedida en la gaceta «{participacion.gaceta.nombre}» vence en menos de 24 horas."})


def evaluar_procesos(ev):
    from apps.catalogo.models import ProgramacionProceso
    from apps.workflows.models import InstanciaEtapa

    detenidas = InstanciaEtapa.objects.filter(
        instancia_workflow__estado__in=("EN_ESPERA", "ERROR"),
    ).filter(estado__in=("EN_ESPERA", "ERROR")).select_related("instancia_workflow", "bloque_operativo")
    for ejecucion in detenidas:
        correccion = (ejecucion.resultado or {}).get("correccion") if ejecucion.estado == "EN_ESPERA" else None
        funcional = ejecucion.estado == "ERROR" and (ejecucion.error or {}).get("tipo") == "FUNCIONAL"
        if not (correccion or funcional):
            continue
        from apps.tickets.models import Ticket

        ticket = Ticket.objects.select_related("detalle_servicio__servicio", "usuario_responsable").filter(
            instancia_workflow_id=ejecucion.instancia_workflow.raiz_id, estado__in=ESTADOS_ABIERTOS).first()
        if ticket is None:
            continue
        motivo = "; ".join((correccion or {}).get("mensajes") or []) or (ejecucion.error or {}).get("mensaje", "")
        paso = ejecucion.bloque_operativo.nombre if ejecucion.bloque_operativo_id else "un paso"
        ev.situacion(f"ALERTA_PROCESO_DETENIDO:ejecucion:{ejecucion.pk}", aviso="ALERTA_PROCESO_DETENIDO", ticket=ticket,
                     objeto=("ejecucion", ejecucion.pk), usuarios=dest.responsable(ticket), contexto={
                         "titulo": "El proceso se detuvo",
                         "resumen": f"«{_nombre(ticket)}» se detuvo en «{paso}»: falta información por corregir.",
                         "extracto": motivo})
    for programacion in ProgramacionProceso.objects.filter(activa=True).exclude(ultimo_error="").select_related(
        "servicio", "responsable_inicial"
    ):
        responsable = programacion.responsable_inicial
        usuarios = []
        if responsable is not None:
            usuarios = dest.unir(dest.personas([responsable.usuario_id]), dest.miembros_de(responsable.equipo_id))
        ev.situacion(f"ALERTA_PROGRAMACION_FALLIDA:programacion:{programacion.pk}:{_iso(programacion.ultimo_intento_en)}",
                     aviso="ALERTA_PROGRAMACION_FALLIDA", objeto=("programacion", programacion.pk), usuarios=usuarios,
                     contexto={"titulo": "No se generó un proceso programado",
                               "resumen": f"La ejecución programada de «{programacion.servicio.nombre}» no se pudo generar.",
                               "extracto": programacion.ultimo_error,
                               "hechos_extra": [["Proceso", programacion.servicio.nombre]]})


EVALUADORES = (evaluar_tickets, evaluar_tareas, evaluar_aprobaciones, evaluar_informacion_y_entregas, evaluar_gacetas,
               evaluar_procesos)


def evaluar(ahora=None):
    """Una pasada completa. Un evaluador que falla no impide los demás ni resuelve sus alertas por error."""
    ev = Evaluacion(ahora)
    fallidos = []
    for evaluador in EVALUADORES:
        try:
            evaluador(ev)
        except Exception:  # noqa: BLE001
            logger.exception("Falló el evaluador de alertas %s", evaluador.__name__)
            fallidos.append(evaluador.__name__)
    resueltas = 0 if fallidos else ev.resolver_las_demas()
    return {"emitidas": ev.emitidas, "resueltas": resueltas, "fallidos": fallidos}
