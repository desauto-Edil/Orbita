"""Vistas de Órbita.

Sistema (incremento 0.1): healthcheck y manejadores de error — habilitadores
técnicos sin CU funcional propio.

Identidad (incremento 0.2): CU-001 Autenticarse, CU-002 Cerrar sesión,
CU-003 Consultar perfil.

Application Shell (incremento 0.5): `inicio_view` es la pantalla de
aterrizaje autenticada — sin CU propio, usa solo información organizacional
ya disponible (CU-003). Ningún dominio inexistente (Tickets, Tareas,
Procesos) se simula aquí.
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
