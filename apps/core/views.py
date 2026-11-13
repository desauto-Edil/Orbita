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

from urllib.parse import urlencode

import redis as redis_client
from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.db.models import Q
from django.db.utils import OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.aprobaciones.consultas import aprobaciones_pendientes_para
from apps.catalogo import busqueda
from apps.core import accesos, disenador, inicio
from apps.core import perfil as perfil_ops
from apps.core.models import Usuario
from apps.core.redirecciones import destino_interno, volver_a
from apps.tickets import bandeja as bandeja_ops
from apps.tickets import parrilla as parrilla_ops
from apps.tickets import revision_elementos as revision_ops
from apps.tickets import seguimiento
from apps.tickets import trabajo as trabajo_ops
from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
from apps.tareas.models import Tarea


def salud(request):
    """Healthcheck sin autenticación: verifica DB y Redis, no depende de Celery.

    Versión 1: responde solo «ok» o «error» (y 200/503), sin decir QUÉ pieza falló: no revela la arquitectura
    interna. Además Caddy la bloquea desde internet; la usan el healthcheck de Docker y el monitoreo interno."""
    sano = True
    try:
        connections["default"].cursor()
    except OperationalError:
        sano = False
    try:
        redis_client.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2).ping()
    except redis_client.RedisError:
        sano = False
    return JsonResponse({"status": "ok" if sano else "error"}, status=200 if sano else 503)


def error_404(request, exception):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)


@require_GET
def login_view(request):
    """CU-001 — Autenticarse en Órbita (RQF-001, RQF-004, RN-001).

    Versión 1: el único ingreso es Google (`apps.core.google_auth`); ya no hay usuario y contraseña.
    La página solo muestra el botón y lleva el `next` validado (p. ej. el enlace de un correo) al
    formulario que inicia el viaje a Google."""
    destino = destino_interno(request, request.GET.get("next"))
    if request.user.is_authenticated:
        return redirect(destino or settings.LOGIN_REDIRECT_URL)

    from apps.core.google_auth import configurado

    return render(request, "cuentas/login.html", {"next": destino, "google_login_disponible": configurado()})


@require_POST
@login_required
def logout_view(request):
    """CU-002 — Cerrar sesión (RQF-002, RN-001)."""
    auth_logout(request)
    return redirect(settings.LOGIN_URL)


def _temas():
    from apps.core import temas

    return temas


@login_required
def perfil_view(request):
    """CU-003 — Consultar perfil (RQF-003, RQF-016, RQF-017, RN-004). Solo lectura.

    Fase visual: identidad (foto de Google), organización (áreas, unidades y equipos), últimos
    accesos (registrados por CU-001, `apps.core.accesos`), compañeros (`apps.core.perfil`) y la
    preferencia personal de apariencia."""
    usuario = request.user
    areas = usuario.areas.filter(activo=True, area__activo=True).select_related("area")
    unidades_negocio = usuario.unidades_negocio.filter(activo=True).select_related("unidad_negocio")
    area_principal = areas.filter(es_principal=True).first()
    from apps.notificaciones import preferencias as notif_preferencias

    contexto = {
        "usuario": usuario,
        "perfil": getattr(usuario, "perfil_organizacional", None),
        "areas": areas,
        "unidades_negocio": unidades_negocio,
        "equipos": perfil_ops.equipos_de(usuario),
        "area_principal": area_principal,
        "unidad_principal": unidades_negocio.filter(es_principal=True).first(),
        "accesos": accesos.ultimos_accesos(usuario, request),
        "companeros": perfil_ops.companeros_de(usuario, area_principal),
        "apariencias": Usuario.Apariencia.choices,
        # Versión 1: color personal (vacío = el del sistema), la misma lista curada de Configuración › General.
        "temas_personales": [{"clave": clave, "nombre": nombre} for clave, (nombre, _d) in _temas().TEMAS.items()],
        # CU-037 (Sprint 7, adelanto): qué grupos de avisos quiere recibir también por correo.
        "notif_preferencias": notif_preferencias.de(usuario),
        "titulo_pagina": "Mi perfil",
    }
    return render(request, "cuentas/perfil.html", contexto)


@login_required
@require_POST
def presencia_view(request):
    """Señal de presencia que envía cada minuto una pestaña de Órbita visible (`static/js/app.js`): la persona sigue
    en línea aunque no navegue. El middleware ya la registró al atender esta petición; aquí no hay nada más."""
    from django.http import HttpResponse

    return HttpResponse(status=204)


@login_required
@require_POST
def apariencia_view(request):
    """Preferencia personal de modo claro/oscuro (Fase visual, CU-003). Solo la propia persona,
    solo un valor de la lista cerrada; no es configuración de la plataforma ni se audita."""
    valor = request.POST.get("apariencia", "")
    if valor in Usuario.Apariencia.values and valor != request.user.apariencia:
        request.user.apariencia = valor
        request.user.save(update_fields=["apariencia"])
    return volver_a(request, "core:perfil")


@login_required
@require_POST
def tema_view(request):
    """Color PERSONAL de acento (Versión 1, etapa 7): uno de la lista curada (`apps.core.temas`) o vacío = el del
    sistema. Solo la propia persona; no cambia nada para nadie más ni se audita (como la apariencia)."""
    from apps.core import temas

    valor = request.POST.get("tema", "")
    if (valor == "" or valor in temas.TEMAS) and valor != request.user.tema:
        request.user.tema = valor
        request.user.save(update_fields=["tema"])
    return volver_a(request, "core:perfil")


@login_required
@require_http_methods(["GET", "POST"])
def bienvenida_view(request):
    """Bienvenida de cinco pasos (Versión 1, etapa 7; `apps.core.bienvenida`). GET muestra un paso (`?paso=`);
    POST guarda la apariencia o el color del paso (solo para esta persona) y avanza, o termina / omite."""
    from apps.core import bienvenida, temas

    usuario = request.user
    destino = destino_interno(request, request.POST.get("next") or request.GET.get("next")) or ""
    if request.method == "POST":
        accion = request.POST.get("accion", "")
        paso = bienvenida.paso_valido(request.POST.get("paso"))
        if accion in ("omitir", "terminar"):
            bienvenida.completar(usuario)
            return redirect(destino or settings.LOGIN_REDIRECT_URL)
        if paso == "apariencia" and request.POST.get("apariencia") in Usuario.Apariencia.values:
            usuario.apariencia = request.POST["apariencia"]
            usuario.save(update_fields=["apariencia"])
        if paso == "color":
            valor = request.POST.get("tema", usuario.tema)
            if valor == "" or valor in temas.TEMAS:
                usuario.tema = valor
                usuario.save(update_fields=["tema"])
        anterior, siguiente = bienvenida.vecinos(paso)
        ir = anterior if accion == "atras" else (siguiente if accion == "siguiente" else paso)
        parametros = {"paso": ir or paso, **({"next": destino} if destino else {})}
        return redirect(f"{reverse('core:bienvenida')}?{urlencode(parametros)}")

    paso = bienvenida.paso_valido(request.GET.get("paso"))
    anterior, siguiente = bienvenida.vecinos(paso)
    numero = bienvenida.CLAVES.index(paso) + 1
    contexto = {
        "paso": paso,
        "pasos": [
            {"clave": clave, "nombre": nombre, "numero": i, "actual": clave == paso, "hecho": i < numero}
            for i, (clave, nombre) in enumerate(bienvenida.PASOS, start=1)
        ],
        "numero": numero,
        "total": len(bienvenida.PASOS),
        "anterior": anterior,
        "siguiente": siguiente,
        "next": destino,
        "titulo_pagina": "Te damos la bienvenida",
    }
    if paso == "apariencia":
        contexto["apariencias"] = Usuario.Apariencia.choices
    elif paso == "color":
        contexto["temas"] = [
            {"clave": clave, "nombre": nombre, "descripcion": descripcion}
            for clave, (nombre, descripcion) in temas.TEMAS.items()
        ]
    elif paso in ("organizacion", "companeros"):
        contexto.update(bienvenida.organizacion_de(usuario))
        if paso == "companeros":
            contexto["companeros"] = perfil_ops.companeros_de(usuario, contexto["area_principal"])
    return render(request, "cuentas/bienvenida.html", contexto)


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
        "ejemplos": inicio.ejemplos_busqueda(usados),
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




@login_required
def mi_trabajo_view(request):
    """Mi trabajo: UNA bandeja agrupada por TICKET (`apps.tickets.bandeja`), con búsqueda y filtro locales. No concede
    ninguna capacidad: cada fila abre el espacio del ticket, que revalida su propia autorización. Las bandejas
    técnicas (`?tab=tareas|aprobaciones|revisiones`) y los pendientes «sin ticket asociado» se retiraron: el trabajo se
    hace en una sola vista, la del ticket (no existe una ruta para crear tareas sueltas).

    Unión por pertenencia de `pk` (subconsultas), no `qs_a | qs_b`: Django no combina un QuerySet con `.distinct()`
    (`tareas_asignadas_a`) con uno sin él (`tareas_disponibles_para_tomar`)."""
    usuario = request.user
    tareas = list(
        Tarea.objects.filter(
            Q(pk__in=tareas_asignadas_a(usuario).values("pk"))
            | Q(pk__in=tareas_disponibles_para_tomar(usuario).values("pk"))
        )
        .exclude(estado=Tarea.Estado.CANCELADA)  # 4.G1.5: lo cancelado no es trabajo
        .select_related("usuario_responsable", "equipo_responsable")
        .order_by("-creado_en")
    )
    aprobaciones = list(
        aprobaciones_pendientes_para(usuario)
        .select_related("esquema", "aprobador_usuario", "aprobador_equipo")
        .order_by("-creado_en")
    )
    a_cargo = list(trabajo_ops.tickets_a_cargo(usuario))
    bandeja = bandeja_ops.construir(
        usuario, tareas=tareas, aprobaciones=aprobaciones,
        revisiones=revision_ops.revisiones_pendientes_para(usuario),
        colecciones=list(parrilla_ops.colecciones_de_participante(usuario)), a_cargo=a_cargo,
    )
    bandeja["total_atencion"] = len(bandeja["atencion"])
    bandeja["total_seguimiento"] = len(bandeja["seguimiento"])
    texto = (request.GET.get("q") or "").strip()[:80]
    ver = request.GET.get("ver")
    if ver == "historico":
        # Histórico: lo que la persona ATENDIÓ y ya terminó (resuelto, cerrado o cancelado), con sus filtros.
        from apps.tickets import historico

        return render(request, "core/mi_trabajo.html", {
            "bandeja": {**bandeja, "ver": "historico", "q": "", "filas": [],
                        "total": bandeja["total_atencion"] + bandeja["total_seguimiento"],
                        "total_historico": historico.contar_atendidos(usuario)},
            "contadores": inicio.contadores_trabajo(usuario),
            "historico": historico.contexto(
                historico.atendidos_por(usuario), request.GET, ver="historico", persona_es="ambos",
                persona_etiqueta="Persona", vacio="Aún no tienes tickets atendidos que hayan terminado.",
            ),
            "titulo_pagina": "Mi trabajo",
        })
    if ver not in ("atencion", "seguimiento"):
        ver = "todos"
    for grupo_clave in ("atencion", "seguimiento"):
        if ver not in ("todos", grupo_clave):
            bandeja[grupo_clave] = []
        else:
            bandeja[grupo_clave] = [g for g in bandeja[grupo_clave] if g.coincide(texto)]
    bandeja["q"] = texto
    bandeja["ver"] = ver
    # Una fila por ticket, primero lo que requiere atención (ya ordenado por vencimiento) y luego lo que está en
    # seguimiento. La clasificación es la de `bandeja.construir`; aquí solo se presenta.
    filas = bandeja["atencion"] + bandeja["seguimiento"]
    fases = seguimiento.fases_actuales([g.ticket for g in filas])
    for grupo in filas:
        grupo.fase = fases.get(grupo.ticket.pk, "")
        grupo.situacion = "atencion" if grupo.requiere_atencion else "seguimiento"
    bandeja["filas"] = filas
    bandeja["total"] = bandeja["total_atencion"] + bandeja["total_seguimiento"]
    from apps.tickets import historico

    bandeja["total_historico"] = historico.contar_atendidos(usuario)
    return render(request, "core/mi_trabajo.html", {
        "bandeja": bandeja,
        # Rediseño 2026-11-13: resumen de lo que está a su nombre (misma regla que Inicio), solo presentación.
        "contadores": inicio.contadores_trabajo(usuario),
        # 4.F3: los tickets bajo su responsabilidad individual (tarjeta con fase, bloque actual y progreso).
        "tarjetas": [trabajo_ops.tarjeta(usuario, ticket) for ticket in a_cargo[:MI_TRABAJO_TICKETS]],
        "titulo_pagina": "Mi trabajo",
    })


MI_TRABAJO_TICKETS = 24


@login_required
def mi_equipo_view(request):
    """«Mi equipo» — vista general para quien tiene personal a cargo (jefatura directa, `apps.core.jefaturas`): una
    fila por colaborador con sus tickets activos a cargo, sus pendientes y vencidos, y un resumen de cada ticket.
    Solo lectura; sin colaboradores, 404 (el menú no lo ofrece)."""
    from django.http import Http404

    from apps.core import jefaturas

    equipo = jefaturas.resumen_de_equipo(request.user)
    if not equipo:
        raise Http404
    areas = sorted({fila["area"] for fila in equipo if fila["area"]})
    return render(request, "core/mi_equipo.html", {"equipo": equipo, "areas": areas, "titulo_pagina": "Mi equipo"})


@login_required
def mi_equipo_persona_view(request, pk):
    """«Mi equipo» — el detalle de UN colaborador directo: su bandeja de trabajo completa, en solo lectura."""
    from django.http import Http404
    from django.shortcuts import get_object_or_404

    from apps.core import jefaturas

    colaborador = get_object_or_404(Usuario, pk=pk)
    detalle = jefaturas.detalle_de_colaborador(request.user, colaborador)
    if detalle is None:
        raise Http404
    return render(request, "core/mi_equipo_persona.html", {
        "detalle": detalle, "titulo_pagina": f"{colaborador.nombre_visible} — Mi equipo",
    })


@login_required
def sistema_visual_view(request):
    """V0 — catálogo visual interno del sistema de diseño (fundamentos,
    componentes y patrones). Herramienta de desarrollo/diseño, no una pantalla
    de producto: sin datos de dominio ni lógica propia.

    Versión 1: lo abre `plataforma.administracion_avanzada` (antes `is_staff`).
    No aparece en la navegación; se abre por su URL (`/sistema-visual/`)."""
    from apps.core import plataforma

    if not plataforma.administra_plataforma(request.user):
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
