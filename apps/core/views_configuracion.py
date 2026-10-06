"""Vistas de Configuración. Qué secciones existen y quién entra a cada una lo
decide `apps/core/configuracion.py` por capacidades; aquí solo se orquesta.

Cubre con interfaz propia lo que hasta ahora solo existía en Django Admin:
CU-004 a CU-008 (usuarios, organización, roles, permisos y asignaciones) y las
categorías de CU-010, además de la identidad del sistema. Toda mutación queda
en `RegistroAuditoria` (CU-040) con el actor real. Nada se elimina: retirar es
desactivar.
"""

import mimetypes

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from apps.core import configuracion
from apps.core.auditoria import auditar_guardado, serializar
from apps.core.forms import (
    AreaForm,
    AsignacionRolForm,
    ConfiguracionSistemaForm,
    MembresiaForm,
    PermisoForm,
    RolForm,
    UnidadNegocioForm,
    UsuarioCrearForm,
    UsuarioDatosForm,
    categoria_form,
)
from apps.core.models import (
    Area,
    AreaUnidadNegocio,
    AsignacionRol,
    ConfiguracionSistema,
    Permiso,
    RolFuncional,
    RolPermiso,
    UnidadNegocio,
    Usuario,
    UsuarioArea,
    UsuarioUnidadNegocio,
)

USUARIOS_POR_PAGINA = 25


# --- Base común -------------------------------------------------------------


def _entrar(request, capacidad, seccion, titulo):
    """Capacidades + contexto base de una pantalla de Configuración. 403 para
    quien no tiene la capacidad, igual que si la navegación no se la ofreciera."""
    caps = configuracion.capacidades(request.user)
    if not caps[capacidad]:
        raise PermissionDenied
    return caps, {
        "caps": caps,
        "config_secciones": configuracion.secciones(caps, seccion),
        "titulo_pagina": titulo,
    }


def _anterior(instancia):
    """Estado guardado de `instancia` antes de este cambio (None si es nueva),
    para la auditoría."""
    if instancia.pk is None:
        return None
    previa = type(instancia).objects.filter(pk=instancia.pk).first()
    return serializar(previa) if previa is not None else None


def _guardar_formulario(form, actor):
    """Guarda un ModelForm ya validado y audita el alta o la edición."""
    anterior = _anterior(form.instance)
    instancia = form.save()
    auditar_guardado(instancia, actor, anterior)
    return instancia


def _guardar(instancia, actor):
    anterior = _anterior(instancia)
    instancia.save()
    auditar_guardado(instancia, actor, anterior)
    return instancia


def _sincronizar_relacion(modelo, fijo, campo, seleccion, actor):
    """Deja activas exactamente las relaciones `modelo` de `fijo` con los
    objetos de `seleccion` (por `campo`): crea o reactiva las que faltan y
    desactiva —sin borrar— las que sobran."""
    elegidos = {obj.pk: obj for obj in seleccion}
    existentes = {getattr(r, f"{campo}_id"): r for r in modelo.objects.filter(**fijo)}
    for pk, relacion in existentes.items():
        debe_estar = pk in elegidos
        if relacion.activo != debe_estar:
            relacion.activo = debe_estar
            _guardar(relacion, actor)
    for pk, obj in elegidos.items():
        if pk not in existentes:
            _guardar(modelo(**fijo, **{campo: obj}), actor)


# --- Portada y General ------------------------------------------------------


@login_required
def configuracion_view(request):
    caps, contexto = _entrar(request, "accede", None, "Configuración")
    contexto["tarjetas"] = configuracion.resumen(caps)
    return render(request, "configuracion/inicio.html", contexto)


@login_required
@require_http_methods(["GET", "POST"])
def general_view(request):
    _caps, contexto = _entrar(request, "sistema", "general", "General — Configuración")
    form = ConfiguracionSistemaForm(
        request.POST or None, request.FILES or None, instance=ConfiguracionSistema.actual()
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            _guardar_formulario(form, request.user)
        messages.success(request, "La identidad del sistema se actualizó.")
        return redirect("core:configuracion_general")
    contexto["form"] = form
    return render(request, "configuracion/general.html", contexto)


@require_GET
def logo_view(request):
    """Logo configurado. Sin autenticación: también se muestra en el inicio de
    sesión. Solo sirve el archivo de `ConfiguracionSistema`, validado al subir."""
    logo = ConfiguracionSistema.actual().logo
    if not logo:
        raise Http404
    try:
        archivo = logo.open("rb")
    except FileNotFoundError as error:
        raise Http404 from error
    tipo = mimetypes.guess_type(logo.name)[0] or "application/octet-stream"
    respuesta = FileResponse(archivo, content_type=tipo)
    respuesta["X-Content-Type-Options"] = "nosniff"
    # La URL lleva la fecha de actualización (`?v=`), así que puede cachearse.
    respuesta["Cache-Control"] = "public, max-age=86400"
    return respuesta


# --- Usuarios ---------------------------------------------------------------

# tipo → (modelo de pertenencia, campo, catálogo, etiqueta)
_MEMBRESIAS = {
    "area": (UsuarioArea, "area", Area, "Área"),
    "unidad": (UsuarioUnidadNegocio, "unidad_negocio", UnidadNegocio, "Unidad de negocio"),
}


@login_required
@require_GET
def usuarios_view(request):
    _caps, contexto = _entrar(request, "ve_usuarios", "usuarios", "Usuarios — Configuración")
    texto = request.GET.get("q", "").strip()[:100]
    estado = request.GET.get("estado")
    if estado not in ("activos", "inactivos", "todos"):
        estado = "activos"

    hoy = timezone.localdate()
    usuarios = Usuario.objects.annotate(
        n_roles=Count(
            "asignaciones_rol",
            filter=Q(asignaciones_rol__activo=True, asignaciones_rol__fecha_inicio__lte=hoy)
            & (Q(asignaciones_rol__fecha_fin__isnull=True) | Q(asignaciones_rol__fecha_fin__gte=hoy)),
        )
    ).prefetch_related(
        Prefetch(
            "areas",
            queryset=UsuarioArea.objects.filter(activo=True, es_principal=True).select_related("area"),
            to_attr="area_principal",
        )
    )
    if estado != "todos":
        usuarios = usuarios.filter(is_active=estado == "activos")
    if texto:
        usuarios = usuarios.filter(
            Q(username__icontains=texto)
            | Q(first_name__icontains=texto)
            | Q(last_name__icontains=texto)
            | Q(email__icontains=texto)
        )
    pagina = Paginator(usuarios.order_by("first_name", "last_name", "username"), USUARIOS_POR_PAGINA).get_page(
        request.GET.get("pagina")
    )
    contexto.update({"pagina": pagina, "texto": texto, "estado": estado})
    return render(request, "configuracion/usuarios.html", contexto)


@login_required
@require_http_methods(["GET", "POST"])
def usuario_crear_view(request):
    _caps, contexto = _entrar(request, "usuarios", "usuarios", "Nuevo usuario — Configuración")
    form = UsuarioCrearForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            usuario = _guardar_formulario(form, request.user)
            perfil, _existia = form.perfil_con_datos(usuario)
            if perfil.cargo or perfil.telefono:
                _guardar(perfil, request.user)
        messages.success(request, f"Se creó la cuenta de {usuario.get_username()}. Ahora puedes indicar sus áreas y roles.")
        return redirect("core:configuracion_usuario", pk=usuario.pk)
    contexto["form"] = form
    return render(request, "configuracion/usuario_crear.html", contexto)


def _disponibles(tipo, usuario):
    modelo, campo, catalogo, _etiqueta = _MEMBRESIAS[tipo]
    ya = modelo.objects.filter(usuario=usuario, activo=True).values(f"{campo}_id")
    return catalogo.objects.filter(activo=True).exclude(pk__in=ya).order_by("nombre")


def _form_membresia(tipo, usuario, datos=None):
    return MembresiaForm(datos, queryset=_disponibles(tipo, usuario), etiqueta=_MEMBRESIAS[tipo][3], prefix=tipo)


def _quitar_principal(modelo, usuario, actor, excepto=None):
    """Como mucho una pertenencia activa puede ser la principal (RN-036/037):
    se libera la actual antes de marcar otra."""
    for actual in modelo.objects.filter(usuario=usuario, activo=True, es_principal=True).exclude(pk=excepto):
        actual.es_principal = False
        _guardar(actual, actor)


def _accion_membresia(request, objetivo, tipo, operacion):
    """Añadir / marcar principal / quitar una pertenencia. Devuelve el
    formulario con errores si hay que volver a mostrarlo."""
    modelo, campo, _catalogo, etiqueta = _MEMBRESIAS[tipo]
    actor = request.user
    if operacion == "agregar":
        form = _form_membresia(tipo, objetivo, request.POST)
        if not form.is_valid():
            return form
        destino, principal = form.cleaned_data["destino"], form.cleaned_data["es_principal"]
        if principal:
            _quitar_principal(modelo, objetivo, actor)
        membresia = modelo.objects.filter(usuario=objetivo, **{campo: destino}).first() or modelo(
            usuario=objetivo, **{campo: destino}
        )
        membresia.activo, membresia.es_principal = True, principal
        _guardar(membresia, actor)
        messages.success(request, f"{etiqueta} añadida: {destino}.")
        return None

    membresia = get_object_or_404(modelo, pk=request.POST.get("membresia"), usuario=objetivo, activo=True)
    if operacion == "principal":
        _quitar_principal(modelo, objetivo, actor, excepto=membresia.pk)
        membresia.es_principal = True
        messages.success(request, f"{getattr(membresia, campo)} es ahora la principal.")
    else:
        membresia.activo = membresia.es_principal = False
        messages.success(request, f"{etiqueta} retirada: {getattr(membresia, campo)}.")
    _guardar(membresia, actor)
    return None


@login_required
@require_http_methods(["GET", "POST"])
def usuario_view(request, pk):
    """Ficha de una persona: cuenta y perfil, contraseña, áreas, unidades
    (`usuarios.administrar`) y roles asignados (`permisos.administrar`)."""
    caps, contexto = _entrar(request, "ve_usuarios", "usuarios", "Usuario — Configuración")
    objetivo = get_object_or_404(Usuario, pk=pk)
    actor = request.user
    # La cuenta de un superusuario solo la modifica otro superusuario: quien
    # administra usuarios no puede apropiarse de una cuenta con más poder.
    edita_cuenta = caps["usuarios"] and (actor.is_superuser or not objetivo.is_superuser)
    edita_roles = caps["permisos"]

    forms_ = {}
    if request.method == "POST":
        accion = request.POST.get("accion", "")
        de_roles = accion.startswith("rol_")
        if not (edita_roles if de_roles else edita_cuenta):
            raise PermissionDenied
        with transaction.atomic():
            if accion == "datos":
                form = UsuarioDatosForm(request.POST, instance=objetivo, editor=actor)
                if form.is_valid():
                    _guardar_formulario(form, actor)
                    perfil, existia = form.perfil_con_datos(objetivo)
                    if existia or perfil.cargo or perfil.telefono:
                        _guardar(perfil, actor)
                    messages.success(request, "Los datos se guardaron.")
                else:
                    forms_["form_datos"] = form
                    objetivo = Usuario.objects.get(pk=pk)
            elif accion == "clave":
                form = SetPasswordForm(objetivo, request.POST)
                if form.is_valid():
                    anterior = serializar(objetivo)
                    form.save()
                    auditar_guardado(objetivo, actor, anterior)
                    if objetivo.pk == actor.pk:
                        update_session_auth_hash(request, objetivo)
                    messages.success(request, "La contraseña se cambió.")
                else:
                    forms_["form_clave"] = form
            elif accion in {f"{t}_{o}" for t in _MEMBRESIAS for o in ("agregar", "principal", "quitar")}:
                tipo, operacion = accion.split("_")
                form = _accion_membresia(request, objetivo, tipo, operacion)
                if form is not None:
                    forms_[f"form_{tipo}"] = form
            elif accion == "rol_asignar":
                form = AsignacionRolForm(request.POST, usuario=objetivo, prefix="rol")
                if form.is_valid():
                    asignacion = _guardar_formulario(form, actor)
                    messages.success(request, f"Rol asignado: {asignacion.rol}.")
                else:
                    forms_["form_rol"] = form
            elif accion == "rol_retirar":
                asignacion = get_object_or_404(
                    AsignacionRol, pk=request.POST.get("asignacion"), usuario=objetivo, activo=True
                )
                hoy = timezone.localdate()
                asignacion.activo = False
                if asignacion.fecha_fin is None or asignacion.fecha_fin > hoy:
                    asignacion.fecha_fin = max(hoy, asignacion.fecha_inicio)
                _guardar(asignacion, actor)
                messages.success(request, f"Rol retirado: {asignacion.rol}.")
            else:
                raise PermissionDenied
        if not forms_:
            return redirect("core:configuracion_usuario", pk=pk)

    hoy = timezone.localdate()
    asignaciones = list(
        objetivo.asignaciones_rol.filter(activo=True).select_related("rol", "area", "unidad_negocio").order_by("rol__nombre")
    )
    for asignacion in asignaciones:
        asignacion.vencida = asignacion.fecha_fin is not None and asignacion.fecha_fin < hoy
        asignacion.futura = asignacion.fecha_inicio > hoy
    contexto.update(
        {
            "objetivo": objetivo,
            "edita_cuenta": edita_cuenta,
            "edita_roles": edita_roles,
            "es_propia": objetivo.pk == actor.pk,
            "areas": objetivo.areas.filter(activo=True).select_related("area").order_by("-es_principal", "area__nombre"),
            "unidades": objetivo.unidades_negocio.filter(activo=True)
            .select_related("unidad_negocio")
            .order_by("-es_principal", "unidad_negocio__nombre"),
            "asignaciones": asignaciones,
        }
    )
    if edita_cuenta:
        contexto.update(
            {
                "form_datos": UsuarioDatosForm(instance=objetivo, editor=actor),
                "form_clave": SetPasswordForm(objetivo),
                "form_area": _form_membresia("area", objetivo),
                "form_unidad": _form_membresia("unidad", objetivo),
            }
        )
    if edita_roles:
        contexto["form_rol"] = AsignacionRolForm(usuario=objetivo, prefix="rol")
    contexto.update(forms_)
    return render(request, "configuracion/usuario.html", contexto)


# --- Roles y permisos -------------------------------------------------------


@login_required
@require_GET
def roles_view(request):
    _caps, contexto = _entrar(request, "permisos", "roles", "Roles y permisos — Configuración")
    hoy = timezone.localdate()
    contexto["roles"] = RolFuncional.objects.annotate(
        n_permisos=Count("rolpermiso", filter=Q(rolpermiso__activo=True, rolpermiso__permiso__activo=True), distinct=True),
        n_personas=Count(
            "asignaciones__usuario",
            filter=Q(asignaciones__activo=True, asignaciones__fecha_inicio__lte=hoy)
            & (Q(asignaciones__fecha_fin__isnull=True) | Q(asignaciones__fecha_fin__gte=hoy)),
            distinct=True,
        ),
    ).order_by("-activo", "nombre")
    contexto["permisos"] = Permiso.objects.annotate(
        n_roles=Count("rolpermiso", filter=Q(rolpermiso__activo=True, rolpermiso__rol__activo=True), distinct=True)
    ).order_by("codigo")
    return render(request, "configuracion/roles.html", contexto)


@login_required
@require_http_methods(["GET", "POST"])
def rol_view(request, pk=None):
    _caps, contexto = _entrar(request, "permisos", "roles", "Rol — Configuración")
    rol = get_object_or_404(RolFuncional, pk=pk) if pk else None
    form = RolForm(request.POST or None, instance=rol)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            guardado = _guardar_formulario(form, request.user)
            _sincronizar_relacion(RolPermiso, {"rol": guardado}, "permiso", form.cleaned_data["permisos"], request.user)
        messages.success(request, f"El rol «{guardado.nombre}» se guardó.")
        return redirect("core:configuracion_roles")
    if rol:
        hoy = timezone.localdate()
        contexto["personas"] = (
            rol.asignaciones.filter(activo=True, fecha_inicio__lte=hoy)
            .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
            .select_related("usuario", "area", "unidad_negocio")
            .order_by("usuario__first_name", "usuario__username")
        )
    contexto.update({"form": form, "rol": rol})
    return render(request, "configuracion/rol.html", contexto)


# --- Catálogos simples: Áreas, Unidades, Categorías y Permisos ----------------


def _categoria():
    from apps.catalogo.models import Categoria

    return Categoria


def _catalogo(clave):
    """Descripción de un catálogo que se administra con la misma lista y el
    mismo formulario. `relacion` = (modelo intermedio, campo propio, campo
    relacionado) cuando el formulario trae el campo `relacionadas`."""
    if clave == "areas":
        return {
            "modelo": Area, "form": AreaForm, "capacidad": "organizacion", "seccion": "areas",
            "plural": "Áreas", "singular": "área", "nueva": "Nueva área",
            "lista": "core:configuracion_areas", "crear": "core:configuracion_area_crear",
            "editar": "core:configuracion_area", "relacion": (AreaUnidadNegocio, "area", "unidad_negocio"),
            "descripcion": "Áreas de la organización. Un área no pertenece a una unidad: se relacionan libremente.",
        }  # fmt: skip
    if clave == "unidades":
        return {
            "modelo": UnidadNegocio, "form": UnidadNegocioForm, "capacidad": "organizacion", "seccion": "unidades",
            "plural": "Unidades de negocio", "singular": "unidad de negocio", "nueva": "Nueva unidad",
            "lista": "core:configuracion_unidades", "crear": "core:configuracion_unidad_crear",
            "editar": "core:configuracion_unidad", "relacion": (AreaUnidadNegocio, "unidad_negocio", "area"),
            "descripcion": "Unidades de negocio. Se relacionan con las áreas sin jerarquía entre ellas.",
        }  # fmt: skip
    if clave == "categorias":
        return {
            "modelo": _categoria(), "form": categoria_form(), "capacidad": "categorias", "seccion": "categorias",
            "plural": "Categorías", "singular": "categoría", "nueva": "Nueva categoría",
            "lista": "core:configuracion_categorias", "crear": "core:configuracion_categoria_crear",
            "editar": "core:configuracion_categoria", "relacion": None,
            "descripcion": "Agrupan los servicios y procesos que las personas pueden solicitar.",
        }  # fmt: skip
    if clave == "permisos":
        return {
            "modelo": Permiso, "form": PermisoForm, "capacidad": "permisos", "seccion": "roles",
            "plural": "Roles y permisos", "singular": "permiso", "nueva": "Nuevo permiso",
            "lista": "core:configuracion_roles", "crear": "core:configuracion_permiso_crear",
            "editar": "core:configuracion_permiso", "relacion": None,
            "descripcion": "",
        }  # fmt: skip
    raise Http404


def _filas_catalogo(clave, modelo):
    """Filas de la lista con el dato que da contexto a cada catálogo."""
    if clave == "areas":
        consulta = modelo.objects.annotate(
            n_a=Count("unidades_relacionadas", filter=Q(unidades_relacionadas__activo=True), distinct=True),
            n_b=Count("usuarios", filter=Q(usuarios__activo=True), distinct=True),
        )
        etiquetas = ("unidades", "personas")
    elif clave == "unidades":
        consulta = modelo.objects.annotate(
            n_a=Count("areas_relacionadas", filter=Q(areas_relacionadas__activo=True), distinct=True),
            n_b=Count("usuarios", filter=Q(usuarios__activo=True), distinct=True),
        )
        etiquetas = ("áreas", "personas")
    else:
        consulta = modelo.objects.annotate(n_a=Count("servicios", distinct=True))
        etiquetas = ("servicios y procesos",)
    return consulta.order_by("-activo", "nombre"), etiquetas


@login_required
@require_GET
def catalogo_view(request, catalogo):
    cat = _catalogo(catalogo)
    _caps, contexto = _entrar(request, cat["capacidad"], cat["seccion"], f"{cat['plural']} — Configuración")
    filas, etiquetas = _filas_catalogo(catalogo, cat["modelo"])
    contexto.update({"cat": cat, "filas": filas, "etiquetas": etiquetas, "con_codigo": catalogo != "categorias"})
    return render(request, "configuracion/catalogo.html", contexto)


@login_required
@require_http_methods(["GET", "POST"])
def catalogo_item_view(request, catalogo, pk=None):
    cat = _catalogo(catalogo)
    _caps, contexto = _entrar(request, cat["capacidad"], cat["seccion"], f"{cat['plural']} — Configuración")
    instancia = get_object_or_404(cat["modelo"], pk=pk) if pk else None
    form = cat["form"](request.POST or None, instance=instancia)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            guardado = _guardar_formulario(form, request.user)
            if cat["relacion"]:
                modelo, propio, relacionado = cat["relacion"]
                _sincronizar_relacion(
                    modelo, {propio: guardado}, relacionado, form.cleaned_data["relacionadas"], request.user
                )
        messages.success(request, f"«{guardado}» se guardó.")
        return redirect(cat["lista"])
    contexto.update({"cat": cat, "form": form, "instancia": instancia})
    return render(request, "configuracion/catalogo_item.html", contexto)
