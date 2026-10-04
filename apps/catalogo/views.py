"""Vistas de Catálogo (CU-011) y Form Builder (CU-013, previsualización)."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from apps.catalogo.models import Categoria, FormularioVersion
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.core.autorizacion import usuario_tiene_permiso
from apps.tickets.solicitud import construir_items_para_version


@login_required
def catalogo_lista_view(request):
    visibles = servicios_visibles_para(request.user)

    categoria_id = request.GET.get("categoria", "")
    if categoria_id:
        visibles = visibles.filter(categoria_id=categoria_id)

    texto = request.GET.get("q", "").strip()
    if texto:
        visibles = visibles.filter(nombre__icontains=texto)

    categorias = Categoria.objects.filter(
        activo=True, servicios__in=servicios_visibles_para(request.user)
    ).distinct()

    contexto = {
        "servicios": visibles.select_related("categoria").order_by("nombre"),
        "categorias": categorias,
        "categoria_seleccionada": categoria_id,
        "texto_busqueda": texto,
        "titulo_pagina": "Servicios",
    }
    return render(request, "catalogo/lista.html", contexto)


@login_required
def catalogo_detalle_view(request, pk):
    # 404, no un filtrado silencioso: acceso directo por URL respeta la
    # misma visibilidad que la lista (CU-011, postcondición).
    servicio = get_object_or_404(servicios_visibles_para(request.user), pk=pk)
    contexto = {
        "servicio": servicio,
        "titulo_pagina": servicio.nombre,
    }
    return render(request, "catalogo/detalle.html", contexto)


@login_required
def previsualizar_version_view(request, version_id):
    """CU-013/RQF-045 — previsualización real, sin persistencia. Gatea con
    el mismo permiso que administra Form Builder (`formulario.administrar`):
    no hay CU que exponga esta pantalla a un Usuario final todavía (eso
    llegará con la radicación de Tickets en Sprint 2).
    """
    if not usuario_tiene_permiso(request.user, "formulario.administrar"):
        raise PermissionDenied
    version = get_object_or_404(FormularioVersion.objects.select_related("formulario"), pk=version_id)

    contexto = {
        "version": version,
        "items": construir_items_para_version(version),
        "titulo_pagina": f"Previsualización — {version.formulario.nombre}",
    }
    return render(request, "catalogo/formulario_preview.html", contexto)
