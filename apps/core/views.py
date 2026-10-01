"""Vistas de Órbita.

Sistema (incremento 0.1): healthcheck y manejadores de error — habilitadores
técnicos sin CU funcional propio.

Identidad (incremento 0.2): CU-001 Autenticarse, CU-002 Cerrar sesión,
CU-003 Consultar perfil.

Application Shell (incremento 0.5): `inicio_view` es la pantalla de
aterrizaje autenticada — sin CU propio, usa solo información organizacional
ya disponible (CU-003). Ningún dominio inexistente (Tickets, Tareas,
Procesos) se simula aquí.

Mi trabajo (3.UI.5): `mi_trabajo_view` es una bandeja personal de lectura
que compone, únicamente para presentación, resultados ya expuestos por
`apps.tareas.consultas`/`apps.aprobaciones.consultas` — sin modelo propio,
sin duplicar sus reglas de negocio ni su autorización de objeto (que sigue
viviendo en el detalle de cada Tarea/Aprobación). No hay una fecha
"relevante" comparable entre ambos dominios: `Tarea.fecha_limite` es un
plazo opcional (cuándo debe actuarse), y `Aprobacion` no tiene ningún campo
equivalente (`decidida_en` solo existe una vez resuelta, y `creado_en` es
una marca de creación, no de vencimiento) — inventar esa equivalencia
habría sido fabricar una prioridad que ningún RQF documenta. Por eso el
orden de "Todo" usa `creado_en` (el único campo que ambos dominios
realmente comparten, vía `RegistroBase`) como criterio simple y
determinista de reciente-primero, no como señal de urgencia.
"""

import redis as redis_client
from django.conf import settings
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.aprobaciones.consultas import aprobaciones_pendientes_para
from apps.aprobaciones.models import Aprobacion
from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
from apps.tareas.models import Tarea


def salud(request):
    """Healthcheck sin autenticación: verifica DB y Redis, no depende de Celery."""
    estado = {"status": "ok", "database": "ok", "redis": "ok"}
    http_status = 200

    try:
        connections["default"].cursor()
    except OperationalError:
        estado["database"] = "error"
        estado["status"] = "error"
        http_status = 503

    try:
        cliente = redis_client.from_url(settings.REDIS_URL)
        cliente.ping()
    except redis_client.RedisError:
        estado["redis"] = "error"
        estado["status"] = "error"
        http_status = 503

    return JsonResponse(estado, status=http_status)


def error_404(request, exception):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)


def login_view(request):
    """CU-001 — Autenticarse en Órbita (RQF-001, RQF-004, RN-001)."""
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.get_user())
        return redirect(settings.LOGIN_REDIRECT_URL)

    return render(request, "cuentas/login.html", {"form": form})


@login_required
def logout_view(request):
    """CU-002 — Cerrar sesión (RQF-002, RN-001)."""
    auth_logout(request)
    return redirect(settings.LOGIN_URL)


@login_required
def perfil_view(request):
    """CU-003 — Consultar perfil (RQF-003, RQF-016, RQF-017, RN-004). Solo lectura."""
    usuario = request.user
    areas = usuario.areas.filter(activo=True).select_related("area")
    unidades_negocio = usuario.unidades_negocio.filter(activo=True).select_related("unidad_negocio")

    contexto = {
        "usuario": usuario,
        "perfil": getattr(usuario, "perfil_organizacional", None),
        "areas": areas,
        "unidades_negocio": unidades_negocio,
        "area_principal": areas.filter(es_principal=True).first(),
        "unidad_principal": unidades_negocio.filter(es_principal=True).first(),
        "titulo_pagina": "Mi perfil",
    }
    return render(request, "cuentas/perfil.html", contexto)


@login_required
def inicio_view(request):
    """Application Shell autenticado (incremento 0.5). Sin CU propio: usa
    exclusivamente información organizacional real ya disponible desde 0.2
    (área/unidad principal) — ningún dato de Tickets/Tareas/Procesos, que
    todavía no existen.
    """
    usuario = request.user
    area_principal = (
        usuario.areas.filter(activo=True, es_principal=True).select_related("area").first()
    )
    unidad_principal = (
        usuario.unidades_negocio.filter(activo=True, es_principal=True)
        .select_related("unidad_negocio")
        .first()
    )

    contexto = {
        "usuario": usuario,
        "area_principal": area_principal,
        "unidad_principal": unidad_principal,
        "titulo_pagina": "Inicio",
    }
    return render(request, "portal/inicio.html", contexto)


def _clase_badge_tarea(estado):
    if estado == Tarea.Estado.COMPLETADA:
        return "success"
    if estado == Tarea.Estado.EN_PROGRESO:
        return "info"
    return "warning"


def _clase_badge_aprobacion(estado):
    if estado == Aprobacion.Estado.APROBADA:
        return "success"
    if estado == Aprobacion.Estado.RECHAZADA:
        return "danger"
    if estado == Aprobacion.Estado.DEVUELTA:
        return "info"
    if estado == Aprobacion.Estado.PENDIENTE:
        return "warning"
    return ""


def _fila_tarea(tarea):
    """Representación de presentación mínima (3.UI.5, punto 4) — no una
    nueva entidad de dominio: nada de esto se persiste ni se reutiliza
    fuera del render de `mi_trabajo.html`."""
    return {
        "tipo": "tarea",
        "titulo": tarea.titulo,
        "estado": tarea.get_estado_display(),
        "estado_clase": _clase_badge_tarea(tarea.estado),
        "fecha_relevante": tarea.fecha_limite,
        "url": reverse("tareas:detalle", args=[tarea.pk]),
        "creado_en": tarea.creado_en,
    }


def _fila_aprobacion(aprobacion):
    return {
        "tipo": "aprobacion",
        "titulo": f"Aprobación #{aprobacion.pk}",
        "estado": aprobacion.get_estado_display(),
        "estado_clase": _clase_badge_aprobacion(aprobacion.estado),
        "fecha_relevante": None,
        "url": reverse("aprobaciones:detalle", args=[aprobacion.pk]),
        "creado_en": aprobacion.creado_en,
    }


@login_required
def mi_trabajo_view(request):
    """3.UI.5 — sin CU propio: agregador operativo de lectura de
    `Tareas` (RQF-071/073) + `Aprobaciones` (RQF-078), ya expuestas por sus
    propias consultas de dominio. Visible para cualquier autenticado —la
    pertenencia real depende de relaciones con objetos (responsable,
    aprobador), no de poseer un rol global— y no concede ninguna capacidad
    nueva: cada enlace apunta al detalle real, que revalida su propia
    autorización de objeto.

    Deduplicación: `tareas_asignadas_a` y `tareas_disponibles_para_tomar`
    pueden solaparse (una tarea de equipo sin `usuario_responsable` directo
    es simultáneamente "asignada" —vía membresía de equipo— y "disponible
    para tomar"). Se combinan con `|` sobre el mismo QuerySet de `Tarea` y
    se aplica `.distinct()` — deduplicación real por identidad de fila en
    SQL, no un ajuste de presentación después de construir los dicts.
    """
    tab = request.GET.get("tab")
    if tab not in ("todo", "tareas", "aprobaciones"):
        tab = "todo"

    usuario = request.user
    tareas_qs = (
        (tareas_asignadas_a(usuario) | tareas_disponibles_para_tomar(usuario))
        .distinct()
        .select_related("usuario_responsable", "equipo_responsable")
        .order_by("-creado_en")
    )
    aprobaciones_qs = (
        aprobaciones_pendientes_para(usuario)
        .select_related("esquema", "aprobador_usuario", "aprobador_equipo")
        .order_by("-creado_en")
    )

    filas_tareas = [_fila_tarea(tarea) for tarea in tareas_qs]
    filas_aprobaciones = [_fila_aprobacion(aprobacion) for aprobacion in aprobaciones_qs]

    if tab == "tareas":
        filas = filas_tareas
    elif tab == "aprobaciones":
        filas = filas_aprobaciones
    else:
        filas = sorted(filas_tareas + filas_aprobaciones, key=lambda fila: fila["creado_en"], reverse=True)

    contexto = {
        "tab": tab,
        "filas": filas,
        # Directo de las listas ya construidas — ninguna consulta adicional
        # solo para contar (punto 11).
        "total_tareas": len(filas_tareas),
        "total_aprobaciones": len(filas_aprobaciones),
        "titulo_pagina": "Mi trabajo",
    }
    return render(request, "core/mi_trabajo.html", contexto)
