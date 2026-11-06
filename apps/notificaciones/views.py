"""Centro de notificaciones (CU-035, RQF-105), preferencias (CU-037, RQF-107) y valores globales de recordatorios
(Configuración › Notificaciones, `sistema.configurar`). Cada persona ve SOLO sus notificaciones; abrir una la marca
como leída y lleva a su objeto, donde se vuelve a autorizar."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.auditoria import auditar_guardado, serializar
from apps.core.redirecciones import volver_a
from apps.notificaciones import preferencias
from apps.notificaciones.models import CAMPOS_PARAMETROS, ConfiguracionNotificaciones, Grupo, Notificacion

POR_PAGINA = 30


@login_required
def centro_view(request):
    consulta = Notificacion.objects.filter(destinatario=request.user).select_related("ticket", "actor")
    ver = request.GET.get("ver") if request.GET.get("ver") in ("sin_leer", "accion") else ""
    grupo = request.GET.get("grupo") if request.GET.get("grupo") in Grupo.values else ""
    if ver == "sin_leer":
        consulta = consulta.filter(leida_en__isnull=True)
    elif ver == "accion":
        consulta = consulta.filter(requiere_accion=True, leida_en__isnull=True)
    if grupo:
        consulta = consulta.filter(grupo=grupo)
    pagina = Paginator(consulta, POR_PAGINA).get_page(request.GET.get("pagina"))
    hoy = timezone.localdate()
    filas = []
    for notificacion in pagina.object_list:
        dia = timezone.localtime(notificacion.creada_en).date()
        etiqueta = "Hoy" if dia == hoy else "Ayer" if (hoy - dia).days == 1 else None
        filas.append({"n": notificacion, "dia": dia, "dia_etiqueta": etiqueta})
    return render(request, "notificaciones/centro.html", {
        "titulo_pagina": "Notificaciones",
        "pagina": pagina,
        "filas": filas,
        "ver": ver,
        "grupo": grupo,
        "grupos": Grupo.choices,
        "sin_leer": Notificacion.objects.filter(destinatario=request.user, leida_en__isnull=True).count(),
    })


@login_required
def abrir_view(request, pk):
    """Marca como leída y lleva al objeto. Una notificación ajena no existe para quien la pide (404)."""
    notificacion = get_object_or_404(Notificacion, pk=pk, destinatario=request.user)
    if notificacion.leida_en is None:
        Notificacion.objects.filter(pk=pk).update(leida_en=timezone.now())
    return redirect(notificacion.enlace or "notificaciones:centro")


@login_required
@require_POST
def marcar_leida_view(request, pk):
    Notificacion.objects.filter(pk=pk, destinatario=request.user, leida_en__isnull=True).update(leida_en=timezone.now())
    return volver_a(request, "notificaciones:centro")


@login_required
@require_POST
def marcar_todas_view(request):
    total = Notificacion.objects.filter(destinatario=request.user, leida_en__isnull=True).update(leida_en=timezone.now())
    if total:
        messages.success(request, "Todas tus notificaciones quedaron como leídas.")
    return volver_a(request, "notificaciones:centro")


@login_required
@require_POST
def preferencias_view(request):
    preferencias.guardar(request.user, {g: request.POST.get(f"correo_{g}") == "on" for g in Grupo.values})
    messages.success(request, "Tus preferencias de correo se guardaron.")
    return volver_a(request, "core:perfil")


# --- Configuración › Notificaciones (valores globales) ------------------------------------------------------


class ConfiguracionNotificacionesForm(forms.ModelForm):
    class Meta:
        model = ConfiguracionNotificaciones
        fields = list(CAMPOS_PARAMETROS)


@login_required
def configuracion_view(request):
    from apps.core.views_configuracion import _entrar

    _caps, contexto = _entrar(request, "sistema", "notificaciones", "Notificaciones — Configuración")
    instancia = ConfiguracionNotificaciones.actual()
    form = ConfiguracionNotificacionesForm(request.POST or None, instance=instancia)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            anterior = serializar(ConfiguracionNotificaciones.objects.get(pk=instancia.pk))
            guardada = form.save()
            auditar_guardado(guardada, request.user, anterior)
        from apps.notificaciones.configuracion import olvidar_globales

        olvidar_globales()
        messages.success(request, "Los valores generales de alertas y recordatorios se actualizaron.")
        return redirect("notificaciones:configuracion")
    from django.conf import settings

    contexto.update({
        "form": form,
        "correo_activo": bool(getattr(settings, "NOTIFICACIONES_CORREO_ACTIVO", False)),
        "correo_redirigido": (getattr(settings, "CORREO_REDIRIGIR_A", "") or "").strip(),
    })
    return render(request, "configuracion/notificaciones.html", contexto)
