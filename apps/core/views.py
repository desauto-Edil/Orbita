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
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import connections
from django.db.models import Q
from django.db.utils import OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.aprobaciones.consultas import aprobaciones_pendientes_para
from apps.aprobaciones.models import Aprobacion
from apps.catalogo import busqueda
from apps.core import disenador, inicio
from apps.tickets import trabajo as trabajo_ops
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
    """Inicio / Mi Portal (V1). Portada cotidiana de LECTURA: una sola página
    que se adapta por capacidades y relaciones reales (nunca por nombre de
    rol) y compone datos de otros dominios vía `apps.core.inicio`. No hay
    cargo/área/unidad aquí: eso es Mi perfil. Los bloques sin datos reales no
    se renderizan y la composición se reorganiza (ver `portal/inicio.html`)."""
    usuario = request.user
    categorias = inicio.categorias_con_servicios(usuario)
    usados = inicio.servicios_usados(usuario)
    contexto = {
        "saludo": inicio.saludo(usuario),
        "categorias": categorias,
        "categorias_tiles": categorias[: inicio.LIMITE_CATEGORIAS_TILES],
        "total_servicios": sum(c["n"] for c in categorias),
        "frecuentes": usados["frecuentes"],
        "recientes": usados["recientes"],
        "tiene_usados": bool(usados["frecuentes"] or usados["recientes"]),
        "trabajo": inicio.resumen_trabajo(usuario),
        "ticket_general": inicio.ticket_general_disponible(usuario),
        "agenda": inicio.agenda(usuario, request.GET.get("mes")),
        "tickets_recientes": inicio.tickets_recientes(usuario),
        "titulo_pagina": "Inicio",
    }
    return render(request, "portal/inicio.html", contexto)


@login_required
@require_GET
def explorar_view(request):
    """Fragmento HTML del explorador de Inicio ("Ver todo"). Mismas reglas de
    visibilidad que el catálogo (`servicios_visibles_para`): no expone nada
    que el usuario no pueda utilizar. Solo GET; el JS lo pide al abrir el
    explorador o al buscar/filtrar, para no cargar el catálogo en Inicio."""
    try:
        categoria_id = int(request.GET.get("categoria", "")) or None
    except ValueError:
        categoria_id = None
    texto = request.GET.get("q", "").strip()[:100]
    resultados, hay_mas = inicio.buscar_servicios(request.user, texto, categoria_id)
    contexto = {"resultados": resultados, "hay_mas": hay_mas, "texto": texto, "filtrado": bool(texto or categoria_id)}
    # Sin `request`: es un fragmento que no usa el shell, así que no se
    # ejecutan los context processors de navegación (varias consultas) en cada
    # pulsación de tecla del buscador.
    return HttpResponse(render_to_string("portal/_explorador_resultados.html", contexto))


@login_required
@require_GET
def necesidad_view(request):
    """4.D — buscador "¿Qué necesitas?". Solo GET y sin efectos: no crea tickets ni
    borradores, no guarda el texto escrito y no audita la búsqueda. La vista recibe
    el texto, lo valida, llama al buscador de dominio (`apps.catalogo.busqueda`: la
    visibilidad, el puntaje y el umbral viven allí) y presenta. El Ticket General
    nunca es un resultado; se ofrece aparte cuando está disponible
    (`inicio.ticket_general_disponible`, la misma regla de 4.C1/4.C2).

    Con `X-Requested-With: fetch` (Inicio con JS) responde solo el fragmento; sin
    él, la página completa, que es el respaldo sin JS."""
    es_fragmento = request.headers.get("X-Requested-With") == "fetch"
    enviada = es_fragmento or "q" in request.GET
    texto = request.GET.get("q", "")[: busqueda.LARGO_MAXIMO_CONSULTA]
    error, resultados, ticket_general = None, [], False
    if enviada:
        error = busqueda.validar_consulta(texto)
        if error is None:
            resultados = busqueda.buscar_servicios_por_necesidad(request.user, texto)
            for resultado in resultados:
                resultado.servicio.tono = inicio.tono(resultado.servicio.categoria_id)
            ticket_general = inicio.ticket_general_disponible(request.user)
    contexto = {
        "q": texto, "enviada": enviada, "error": error, "resultados": resultados, "ticket_general": ticket_general,
    }
    if es_fragmento:
        # Sin `request`: fragmento sin shell, no corren los context processors de navegación.
        return HttpResponse(render_to_string("portal/_necesidad_resultados.html", contexto))
    contexto["titulo_pagina"] = "¿Qué necesitas?"
    return render(request, "portal/necesidad.html", contexto)


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


def _responsable(usuario, equipo):
    if usuario is not None:
        return usuario.get_full_name() or usuario.get_username()
    return str(equipo) if equipo is not None else ""


def _fila_tarea(tarea, ahora):
    """Representación de presentación mínima (3.UI.5, punto 4) — no una
    nueva entidad de dominio: nada de esto se persiste ni se reutiliza
    fuera del render de `mi_trabajo.html`."""
    pendiente = tarea.estado != Tarea.Estado.COMPLETADA
    return {
        "tipo": "tarea",
        "titulo": tarea.titulo,
        "estado": tarea.get_estado_display(),
        "estado_clase": _clase_badge_tarea(tarea.estado),
        "fecha_relevante": tarea.fecha_limite,
        "vencida": pendiente and tarea.fecha_limite is not None and tarea.fecha_limite < ahora,
        # Sin responsable directo: está disponible para que alguien la tome.
        "por_tomar": pendiente and tarea.usuario_responsable_id is None,
        "responsable": _responsable(tarea.usuario_responsable, tarea.equipo_responsable),
        "url": reverse("tareas:detalle", args=[tarea.pk]),
        "vista_url": reverse("tareas:vista_previa", args=[tarea.pk]),
        "creado_en": tarea.creado_en,
    }


def _fila_aprobacion(aprobacion):
    return {
        "tipo": "aprobacion",
        "titulo": f"Aprobación #{aprobacion.pk}",
        "estado": aprobacion.get_estado_display(),
        "estado_clase": _clase_badge_aprobacion(aprobacion.estado),
        "fecha_relevante": None,
        "vencida": False,
        "por_tomar": False,
        "responsable": _responsable(aprobacion.aprobador_usuario, aprobacion.aprobador_equipo),
        "url": reverse("aprobaciones:detalle", args=[aprobacion.pk]),
        "vista_url": reverse("aprobaciones:vista_previa", args=[aprobacion.pk]),
        "creado_en": aprobacion.creado_en,
    }


MI_TRABAJO_POR_PAGINA = 30
MI_TRABAJO_TICKETS = 24


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
    # Unión por pertenencia de `pk` (subconsultas), no `qs_a | qs_b`: Django
    # no combina un QuerySet con `.distinct()` (`tareas_asignadas_a`) con uno
    # sin él (`tareas_disponibles_para_tomar` para quien tiene
    # `tareas.gestionar`) y lanza TypeError. Con `pk__in` la deduplicación
    # sigue siendo por identidad de fila en SQL y no depende de cómo cada
    # consulta de dominio decida usar `distinct`.
    tareas_qs = (
        Tarea.objects.filter(
            Q(pk__in=tareas_asignadas_a(usuario).values("pk"))
            | Q(pk__in=tareas_disponibles_para_tomar(usuario).values("pk"))
        )
        .select_related("usuario_responsable", "equipo_responsable")
        .order_by("-creado_en")
    )
    aprobaciones_qs = (
        aprobaciones_pendientes_para(usuario)
        .select_related("esquema", "aprobador_usuario", "aprobador_equipo")
        .order_by("-creado_en")
    )

    # 4.F3: «Mi trabajo» empieza por los TICKETS que la persona atiende (no los que solicitó): cada
    # tarjeta lleva a la experiencia operativa. Tareas y aprobaciones sueltas siguen debajo.
    tarjetas = [trabajo_ops.tarjeta(usuario, ticket) for ticket in trabajo_ops.tickets_a_cargo(usuario)[:MI_TRABAJO_TICKETS]]

    ahora = timezone.now()
    filas_tareas = [_fila_tarea(tarea, ahora) for tarea in tareas_qs]
    filas_aprobaciones = [_fila_aprobacion(aprobacion) for aprobacion in aprobaciones_qs]

    if tab == "tareas":
        filas = filas_tareas
    elif tab == "aprobaciones":
        filas = filas_aprobaciones
    else:
        filas = sorted(filas_tareas + filas_aprobaciones, key=lambda fila: fila["creado_en"], reverse=True)

    # Con mucho trabajo acumulado la lista se pagina: la pantalla nunca crece
    # sin límite. Los totales de las pestañas siguen siendo los reales.
    pagina = Paginator(filas, MI_TRABAJO_POR_PAGINA).get_page(request.GET.get("pagina"))
    contexto = {
        "tab": tab,
        "filas": pagina.object_list,
        "pagina": pagina,
        "tarjetas": tarjetas,
        "total": len(filas_tareas) + len(filas_aprobaciones),
        # Directo de las listas ya construidas — ninguna consulta adicional
        # solo para contar (punto 11).
        "total_tareas": len(filas_tareas),
        "total_aprobaciones": len(filas_aprobaciones),
        "titulo_pagina": "Mi trabajo",
    }
    return render(request, "core/mi_trabajo.html", contexto)


@login_required
def sistema_visual_view(request):
    """V0 — catálogo visual interno del sistema de diseño (fundamentos,
    componentes y patrones). Herramienta de desarrollo/diseño, no una pantalla
    de producto: sin datos de dominio ni lógica propia.

    Protección mínima con el mecanismo que ya existe: `is_staff`, la misma
    compuerta nativa de Django Admin. No introduce ningún rol ni permiso nuevo
    y no aparece en la navegación; se abre por su URL (`/sistema-visual/`)."""
    if not request.user.is_staff:
        raise PermissionDenied
    return render(request, "core/sistema_visual.html", {"titulo_pagina": "Sistema visual"})


# --- Diseñador (D1) --------------------------------------------------------


def _contexto_disenador(request, activa, titulo):
    """Capacidades + pestañas locales. Quien no entra al Diseñador (ni
    `catalogo.administrar` ni consultar Flujos) recibe 403, igual que si la
    navegación no se lo ofreciera."""
    caps = disenador.capacidades(request.user)
    if not caps["accede"]:
        raise PermissionDenied
    return caps, {
        "caps": caps,
        "disenador_tabs": disenador.pestanas(caps, activa),
        "titulo_pagina": titulo,
    }


@login_required
def disenador_view(request):
    caps, contexto = _contexto_disenador(request, "inicio", "Diseñador")
    if caps["ve_flujos"]:
        contexto["flujos"] = disenador.resumen_de_flujos(caps)
    if caps["ve_servicios"]:
        contexto["servicios"] = disenador.resumen_de_servicios()
    return render(request, "core/disenador.html", contexto)


@login_required
def disenador_flujos_view(request):
    caps, contexto = _contexto_disenador(request, "flujos", "Flujos — Diseñador")
    if not caps["ve_flujos"]:
        raise PermissionDenied
    contexto["flujos"] = disenador.biblioteca_de_flujos(caps)
    return render(request, "core/disenador_flujos.html", contexto)


@login_required
def disenador_servicios_view(request):
    caps, contexto = _contexto_disenador(request, "servicios", "Servicios — Diseñador")
    if not caps["ve_servicios"]:
        raise PermissionDenied
    contexto["servicios"] = disenador.lista_de_servicios()
    contexto["ticket_general"] = disenador.ticket_general()
    return render(request, "core/disenador_servicios.html", contexto)
