"""CU-037 — preferencias de notificación (RQF-107): por grupo, si la persona quiere el correo. Lo que exige acción
siempre sale por correo («dentro de los límites definidos por la organización»)."""

from django.db import transaction

from apps.notificaciones.models import Grupo, PreferenciaNotificacion


def acepta_correo(usuario, definicion):
    if definicion.requiere_accion:
        return True
    preferencia = PreferenciaNotificacion.objects.filter(usuario=usuario, grupo=definicion.grupo).values_list(
        "correo", flat=True).first()
    return True if preferencia is None else bool(preferencia)


def de(usuario):
    """`[{"grupo", "etiqueta", "correo"}]` en el orden de los grupos (sin fila = correo activo)."""
    guardadas = dict(PreferenciaNotificacion.objects.filter(usuario=usuario).values_list("grupo", "correo"))
    return [
        {"grupo": valor, "etiqueta": etiqueta, "correo": guardadas.get(valor, True), "descripcion": DESCRIPCIONES[valor]}
        for valor, etiqueta in Grupo.choices
    ]


DESCRIPCIONES = {
    Grupo.TICKETS: "Lo que pasa con los tickets que solicitaste.",
    Grupo.ATENCION: "Tickets que llegan a tu Cola o quedan a tu cargo.",
    Grupo.TRABAJO: "Actividades asignadas y avisos de las etapas del proceso.",
    Grupo.GACETAS: "Revisiones y correcciones de gacetas.",
    Grupo.ALERTAS: "Vencimientos, recordatorios y escalamientos.",
}


@transaction.atomic
def guardar(usuario, correo_por_grupo):
    """`correo_por_grupo`: `{grupo: bool}`; los grupos desconocidos se ignoran."""
    for grupo, correo in correo_por_grupo.items():
        if grupo not in Grupo.values:
            continue
        PreferenciaNotificacion.objects.update_or_create(usuario=usuario, grupo=grupo, defaults={"correo": bool(correo)})
