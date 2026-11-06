"""Catálogo CERRADO de avisos (CU-036, RQF-104). Única fuente de: a qué grupo pertenece cada aviso, si exige
acción (su correo no se apaga ni por el Servicio ni por la persona), si sale por correo por defecto y si se puede
configurar en Studio › Notificaciones › Avisos base.

Las claves son estables: el Studio guarda `{clave: bool}` y cada Ticket lo congela."""

from dataclasses import dataclass

from apps.notificaciones.models import Grupo


@dataclass(frozen=True)
class Aviso:
    clave: str
    grupo: str
    etiqueta: str
    requiere_accion: bool = False
    correo: bool = False
    # Configurable en Studio (avisos base). Las alertas se configuran en Recordatorios.
    configurable: bool = True
    # Dirigido a quien solicitó: no aplica a un Proceso programado (no tiene solicitante).
    para_solicitante: bool = False
    es_alerta: bool = False


_AVISOS = (
    # --- Para quien solicita ---
    Aviso("TICKET_RADICADO", Grupo.TICKETS, "Ticket radicado (con su código)", correo=True, para_solicitante=True),
    Aviso("TICKET_EN_ATENCION", Grupo.TICKETS, "Alguien tomó el ticket o empezó la atención", para_solicitante=True),
    Aviso("INFORMACION_SOLICITADA", Grupo.TICKETS, "Le piden información", requiere_accion=True, correo=True, para_solicitante=True),
    Aviso("COMENTARIO", Grupo.TICKETS, "Nuevo comentario en el ticket"),
    Aviso("FECHA_OBJETIVO_CAMBIADA", Grupo.TICKETS, "Cambió la fecha objetivo (prórroga aprobada)"),
    Aviso("ENTREGA_DISPONIBLE", Grupo.TICKETS, "Hay una entrega para revisar y aceptar", requiere_accion=True, correo=True, para_solicitante=True),
    Aviso("TICKET_RESUELTO", Grupo.TICKETS, "Ticket resuelto", correo=True, para_solicitante=True),
    Aviso("TICKET_CERRADO", Grupo.TICKETS, "Ticket cerrado", correo=True, para_solicitante=True),
    Aviso("TICKET_CANCELADO", Grupo.TICKETS, "Ticket cancelado (con el motivo)", correo=True),
    Aviso("TICKET_REABIERTO", Grupo.TICKETS, "Ticket reabierto", correo=True),
    # --- Para quien atiende ---
    Aviso("TICKET_EN_COLA", Grupo.ATENCION, "Nuevo ticket en la Cola del equipo"),
    Aviso("TICKET_ASIGNADO", Grupo.ATENCION, "Ticket asignado o dirigido a una persona", correo=True),
    Aviso("TICKET_REASIGNADO_FUERA", Grupo.ATENCION, "Le quitaron un ticket (reasignación)"),
    Aviso("TICKET_GENERADO", Grupo.ATENCION, "Se generó la ejecución de un proceso programado", correo=True),
    Aviso("INFORMACION_RESPONDIDA", Grupo.ATENCION, "El solicitante respondió la información pedida", correo=True),
    Aviso("ENTREGA_ACEPTADA", Grupo.ATENCION, "El solicitante aceptó la entrega", correo=True),
    Aviso("ENTREGA_OBSERVADA", Grupo.ATENCION, "El solicitante observó la entrega", correo=True),
    Aviso("PRORROGA_RECHAZADA", Grupo.ATENCION, "La prórroga fue rechazada", correo=True),
    # --- Actividades y aprobaciones ---
    Aviso("TAREA_ASIGNADA", Grupo.TRABAJO, "Actividad asignada, delegada o reasignada", correo=True),
    Aviso("APROBACION_TURNO", Grupo.TRABAJO, "Es su turno de aprobar", requiere_accion=True, correo=True),
    Aviso("TRABAJO_DEVUELTO", Grupo.TRABAJO, "Una aprobación devolvió o rechazó el trabajo", correo=True),
    # --- Gacetas ---
    Aviso("GACETA_ABIERTA", Grupo.GACETAS, "Se abrió la gaceta: hay piezas para revisar", correo=True),
    Aviso("GACETA_OBSERVACION", Grupo.GACETAS, "Recibió una observación: debe corregir", requiere_accion=True, correo=True),
    Aviso("GACETA_NUEVA_RONDA", Grupo.GACETAS, "Llegó una versión corregida para revisar"),
    Aviso("GACETA_PERIODO_CERRADO", Grupo.GACETAS, "Se cerró el periodo de revisión"),
    Aviso("GACETA_RESUELTA", Grupo.GACETAS, "La gaceta se resolvió"),
    # --- Avisos por etapa (Studio › Notificaciones › Por etapa): el canal lo decide cada regla. ---
    Aviso("REGLA_ETAPA", Grupo.TRABAJO, "Aviso configurado por etapa", configurable=False),
    # --- Alertas y recordatorios (las genera el evaluador periódico) ---
    Aviso("ALERTA_TICKET_POR_VENCER", Grupo.ALERTAS, "Ticket próximo a vencer", correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_TICKET_VENCIDO", Grupo.ALERTAS, "Ticket vencido", requiere_accion=True, correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_SIN_TOMAR", Grupo.ALERTAS, "Ticket sin tomar en la Cola", correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_TAREA_POR_VENCER", Grupo.ALERTAS, "Actividad próxima a vencer", correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_TAREA_VENCIDA", Grupo.ALERTAS, "Actividad vencida", requiere_accion=True, correo=True, configurable=False, es_alerta=True),
    Aviso("RECORDATORIO_TAREA", Grupo.ALERTAS, "Recordatorio de una actividad pendiente", correo=True, configurable=False, es_alerta=True),
    Aviso("RECORDATORIO_APROBACION", Grupo.ALERTAS, "Recordatorio de una aprobación pendiente", requiere_accion=True, correo=True, configurable=False, es_alerta=True),
    Aviso("RECORDATORIO_INFORMACION", Grupo.ALERTAS, "Recordatorio: información solicitada sin respuesta", requiere_accion=True, correo=True, configurable=False, para_solicitante=True, es_alerta=True),
    Aviso("RECORDATORIO_ENTREGA", Grupo.ALERTAS, "La entrega se aceptará sola pronto", correo=True, configurable=False, para_solicitante=True, es_alerta=True),
    Aviso("ALERTA_GACETA_POR_CERRAR", Grupo.ALERTAS, "El periodo de revisión de una gaceta está por cerrar", correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_CORRECCION_POR_VENCER", Grupo.ALERTAS, "Una corrección de gaceta está por vencer", correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_PROCESO_DETENIDO", Grupo.ALERTAS, "El proceso se detuvo: falta información", requiere_accion=True, correo=True, configurable=False, es_alerta=True),
    Aviso("ALERTA_PROGRAMACION_FALLIDA", Grupo.ALERTAS, "Falló la generación de un proceso programado", correo=True, configurable=False, es_alerta=True),
    Aviso("ESCALAMIENTO", Grupo.ALERTAS, "Escalamiento: algo vencido sigue sin atenderse", requiere_accion=True, correo=True, configurable=False, es_alerta=True),
)

AVISOS = {a.clave: a for a in _AVISOS}


def aviso(clave):
    return AVISOS[clave]


def configurables(*, programado=False):
    """Avisos base que el Studio ofrece, en orden de catálogo (sin los del solicitante en un programado)."""
    return [a for a in _AVISOS if a.configurable and not (programado and a.para_solicitante)]


def correo_por_defecto(clave, ajustes=None):
    """¿Sale por correo este aviso según el catálogo y los ajustes congelados del Ticket (`{clave: bool}`)?
    Lo que exige acción SIEMPRE sale por correo."""
    definicion = AVISOS[clave]
    if definicion.requiere_accion:
        return True
    if ajustes and clave in ajustes and definicion.configurable:
        return bool(ajustes[clave])
    return definicion.correo
