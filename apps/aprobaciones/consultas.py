"""Consultas de dominio de Aprobaciones — API de lectura para la futura
bandeja unificada "Mi trabajo" (3.UI), backend únicamente, mismo criterio
que `apps/tareas/consultas.py`: sin filtros hipotéticos adicionales."""

from django.db.models import Q

from apps.aprobaciones.autorizacion import PERMISO_CONSULTAR, PERMISO_GESTIONAR
from apps.aprobaciones.models import Aprobacion
from apps.core.autorizacion import usuario_tiene_permiso


def aprobaciones_pendientes_para(usuario):
    """RQF-078 — "presentar al aprobador las decisiones pendientes dentro
    de su alcance": estrictamente aprobador directo (usuario o su equipo
    designado), PENDIENTE."""
    if not getattr(usuario, "is_authenticated", False):
        return Aprobacion.objects.none()
    return Aprobacion.objects.filter(
        Q(aprobador_usuario=usuario)
        | Q(aprobador_equipo__miembros__usuario=usuario, aprobador_equipo__miembros__activo=True),
        estado=Aprobacion.Estado.PENDIENTE,
    ).distinct()


def aprobaciones_visibles_para(usuario):
    """Visibilidad amplia — aprobador directo, o alcance de
    `aprobaciones.consultar`/`aprobaciones.gestionar` (GLOBAL únicamente,
    mismo hallazgo documental que Tareas)."""
    if not getattr(usuario, "is_authenticated", False):
        return Aprobacion.objects.none()
    if usuario_tiene_permiso(usuario, PERMISO_CONSULTAR) or usuario_tiene_permiso(usuario, PERMISO_GESTIONAR):
        return Aprobacion.objects.all()
    return Aprobacion.objects.filter(
        Q(aprobador_usuario=usuario)
        | Q(aprobador_equipo__miembros__usuario=usuario, aprobador_equipo__miembros__activo=True)
    ).distinct()
