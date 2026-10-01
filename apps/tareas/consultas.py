"""Consultas de dominio de Tareas — API de lectura para la futura bandeja
unificada (3.UI). Backend únicamente: ningún QuerySet de aquí se expone
todavía en ninguna vista/formulario.

5 funciones, cada una respaldada por un RQF/CU concreto — sin filtros
hipotéticos adicionales (instrucción explícita: "no preparar diez filtros
para una UI que todavía no existe")."""

from django.db.models import Q
from django.utils import timezone

from apps.core.autorizacion import usuario_tiene_permiso
from apps.tareas.autorizacion import PERMISO_CONSULTAR, PERMISO_GESTIONAR
from apps.tareas.models import Tarea


def tareas_visibles_para(usuario):
    """RQF-072 — población amplia de solo-lectura: creadora, responsable
    directo (usuario o su equipo), o alcance de `tareas.consultar`/
    `tareas.gestionar` (GLOBAL únicamente en 3.3, ver
    `apps.tareas.autorizacion`)."""
    if not getattr(usuario, "is_authenticated", False):
        return Tarea.objects.none()
    if usuario_tiene_permiso(usuario, PERMISO_CONSULTAR) or usuario_tiene_permiso(usuario, PERMISO_GESTIONAR):
        return Tarea.objects.all()
    return Tarea.objects.filter(
        Q(creada_por=usuario)
        | Q(usuario_responsable=usuario)
        | Q(equipo_responsable__miembros__usuario=usuario, equipo_responsable__miembros__activo=True)
    ).distinct()


def tareas_asignadas_a(usuario):
    """RQF-071 — bandeja: estrictamente responsable actual (directo, no
    por alcance de gestión)."""
    if not getattr(usuario, "is_authenticated", False):
        return Tarea.objects.none()
    return Tarea.objects.filter(
        Q(usuario_responsable=usuario)
        | Q(equipo_responsable__miembros__usuario=usuario, equipo_responsable__miembros__activo=True)
    ).distinct()


def tareas_disponibles_para_tomar(usuario):
    """CU-021/RQF-073 — PENDIENTE, sin responsable directo aún, dentro del
    alcance real de `usuario` (mismo criterio que `puede_tomar_tarea`)."""
    if not getattr(usuario, "is_authenticated", False):
        return Tarea.objects.none()
    base = Tarea.objects.filter(estado=Tarea.Estado.PENDIENTE, usuario_responsable__isnull=True)
    if usuario_tiene_permiso(usuario, PERMISO_GESTIONAR):
        return base
    return base.filter(
        equipo_responsable__miembros__usuario=usuario, equipo_responsable__miembros__activo=True
    ).distinct()


def tareas_del_equipo(equipo):
    """RQF-077 literal: "consultar tareas del equipo"."""
    return Tarea.objects.filter(equipo_responsable=equipo)


def tareas_vencidas(queryset=None):
    """RQF-077 — "identificar tareas vencidas": `fecha_limite` pasada y
    todavía no `COMPLETADA` (condición derivada, nunca un estado
    persistido). `queryset` permite acotar antes, p.ej.
    `tareas_vencidas(tareas_asignadas_a(usuario))`."""
    base = Tarea.objects.all() if queryset is None else queryset
    return base.filter(fecha_limite__lt=timezone.now()).exclude(estado=Tarea.Estado.COMPLETADA)
