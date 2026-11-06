"""Studio › Notificaciones (CU-036): tres apartados de UNA sección — Avisos base · Por etapa · Recordatorios.
Mismo permiso que el resto del Studio (`catalogo.administrar`); cada formulario envía a su operación de
`apps.notificaciones.configuracion` (que valida y audita) y vuelve al mismo apartado."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.catalogo.models import Servicio
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import Equipo, Usuario
from apps.notificaciones import configuracion as cfg
from apps.notificaciones.models import CAMPOS_PARAMETROS, ConfiguracionNotificaciones, ConfiguracionNotificacionesServicio, ReglaNotificacion

VISTAS = ("base", "etapa", "recordatorios")


def contexto_studio(servicio, vista=""):
    programado = cfg._es_programado(servicio)
    reglas = cfg.reglas_de(servicio)
    huerfanas = {r.pk for r in cfg.reglas_huerfanas(servicio, reglas)}
    configuracion = cfg.de_servicio(servicio)
    globales = ConfiguracionNotificaciones.actual()
    destinatarios = [
        (valor, etiqueta) for valor, etiqueta in ReglaNotificacion.Destinatario.choices
        if not (programado and valor == ReglaNotificacion.Destinatario.SOLICITANTE)
    ]
    parametros = [
        {
            "campo": campo,
            "etiqueta": ConfiguracionNotificaciones._meta.get_field(campo).verbose_name,
            "global": getattr(globales, campo),
            "propio": getattr(configuracion, campo) if configuracion is not None else None,
            "booleano": campo == "escalar",
        }
        for campo in CAMPOS_PARAMETROS
    ]
    objetivos = cfg.objetivos(servicio)
    propios = (configuracion.recordatorios_actividad if configuracion else None) or {}
    return {
        "notif_vista": vista if vista in VISTAS else "base",
        "notif_avisos_base": cfg.avisos_base(servicio),
        "notif_reglas": reglas,
        "notif_huerfanas": huerfanas,
        "notif_objetivos": objetivos,
        "notif_campos_persona": cfg.campos_de_persona(servicio),
        "notif_momentos": ReglaNotificacion.Momento.choices,
        "notif_destinatarios": destinatarios,
        "notif_usuarios": Usuario.objects.filter(is_active=True).order_by("first_name", "last_name", "username"),
        "notif_equipos": Equipo.objects.filter(activo=True).order_by("nombre"),
        "notif_parametros": parametros,
        "notif_config": configuracion,
        "notif_escalar_a": ConfiguracionNotificacionesServicio.EscalarA.choices,
        "notif_actividades_propias": [
            {"clave": clave, "nombre": nombre, "fase": fase, **propios.get(clave, {})}
            for clave, nombre, fase, tipo in objetivos["actividades"] if tipo == "ACTIVIDAD"
        ],
        "notif_programado": programado,
    }


def _servicio(request, pk):
    if not usuario_tiene_permiso(request.user, "catalogo.administrar"):
        raise PermissionDenied
    return get_object_or_404(Servicio, pk=pk)


def _volver(pk, vista):
    return f"{reverse('core:disenador_servicio', args=[pk])}?tab=notificaciones&vista={vista}"


def _error(request, exc):
    messages.error(request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc))


@login_required
@require_POST
def avisos_base_view(request, pk):
    servicio = _servicio(request, pk)
    claves = [a["clave"] for a in cfg.avisos_base(servicio)]
    cfg.guardar_avisos_base(servicio, request.user, {c: request.POST.get(f"correo_{c}") == "on" for c in claves})
    messages.success(request, "Avisos guardados. Aplican a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "base"))


@login_required
@require_POST
def regla_crear_view(request, pk):
    servicio = _servicio(request, pk)
    datos = request.POST
    usuario = Usuario.objects.filter(pk=datos.get("usuario") or None, is_active=True).first() if datos.get("usuario") else None
    equipo = Equipo.objects.filter(pk=datos.get("equipo") or None, activo=True).first() if datos.get("equipo") else None
    momento = datos.get("momento", "")
    grupo = "fase" if momento in cfg.MOMENTOS_DE_FASE else "gaceta" if momento in cfg.MOMENTOS_DE_GACETA else "actividad"
    destinatario = datos.get("destinatario", "")
    campo = datos.get("campo_item" if destinatario == ReglaNotificacion.Destinatario.COLUMNA_ITEM else "campo_formulario", "")
    try:
        cfg.agregar_regla(
            servicio, request.user, momento=momento, clave_objetivo=datos.get(f"objetivo_{grupo}", ""),
            destinatario=destinatario, usuario=usuario, equipo=equipo,
            campo_clave=campo, correo=datos.get("correo") == "on", mensaje=datos.get("mensaje", ""),
        )
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Aviso agregado. Aplica a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "etapa"))


@login_required
@require_POST
def regla_retirar_view(request, pk, regla_id):
    servicio = _servicio(request, pk)
    regla = get_object_or_404(ReglaNotificacion, pk=regla_id, servicio=servicio, activa=True)
    cfg.retirar_regla(regla, request.user)
    messages.success(request, "Aviso retirado. Los tickets ya creados lo conservan.")
    return redirect(_volver(pk, "etapa"))


def _entero(valor):
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        return int(valor)
    except ValueError:
        raise ValidationError("Los valores deben ser números enteros.") from None


@login_required
@require_POST
def recordatorios_view(request, pk):
    servicio = _servicio(request, pk)
    datos = request.POST
    try:
        parametros = {}
        for campo in CAMPOS_PARAMETROS:
            if campo == "escalar":
                parametros[campo] = {"si": True, "no": False}.get(datos.get("escalar"))
            else:
                parametros[campo] = _entero(datos.get(campo))
        escalar_a = datos.get("escalar_a") or ConfiguracionNotificacionesServicio.EscalarA.SUPERVISION
        cfg.guardar_recordatorios(
            servicio, request.user, parametros=parametros, escalar_a=escalar_a,
            escalar_usuario=Usuario.objects.filter(pk=_entero(datos.get("escalar_usuario")), is_active=True).first(),
            escalar_equipo=Equipo.objects.filter(pk=_entero(datos.get("escalar_equipo")), activo=True).first(),
        )
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Recordatorios guardados. Aplican a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "recordatorios"))


@login_required
@require_POST
def recordatorio_actividad_view(request, pk):
    servicio = _servicio(request, pk)
    datos = request.POST
    try:
        cfg.guardar_recordatorio_actividad(
            servicio, request.user, datos.get("clave_bloque", ""), dias_antes=_entero(datos.get("dias_antes")),
            repetir_cada_dias=_entero(datos.get("repetir_cada_dias")), maximo=_entero(datos.get("maximo")),
        )
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Recordatorio de la actividad guardado.")
    return redirect(_volver(pk, "recordatorios"))
