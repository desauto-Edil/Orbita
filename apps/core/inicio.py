"""Datos de la pantalla Inicio / Mi Portal (Fase visual V1) — sin CU propio.

Inicio es una portada de LECTURA que compone información que ya existe en
otros dominios; no crea modelos, no tiene reglas de negocio propias y no
concede ninguna capacidad: cada enlace apunta a una pantalla que revalida su
propia autorización. Todo bloque sale de datos reales; cuando no hay datos
suficientes la función devuelve vacío (la plantilla reorganiza la
composición en vez de pintar cajas vacías).

Qué alimenta cada bloque:

  Explorar              `apps.catalogo.visibilidad.servicios_visibles_para`
                        (única autoridad de qué ve el usuario) agrupado por
                        categoría.
  Frecuentes/recientes  Tickets propios agrupados por servicio, siempre
                        filtrados otra vez por visibilidad (un servicio
                        desactivado o ya no visible no aparece).
  Tu día                Fechas reales: `Tarea.fecha_limite` de sus tareas
                        pendientes y `EntregaTicket.vence_en` de las
                        entregas que el usuario (como solicitante) debe
                        responder. Las aprobaciones no tienen fecha y no se
                        inventan.
  Tu trabajo            Tareas, aprobaciones y tickets a cargo. La Cola se
                        enlaza pero NO se cuenta: saber qué tickets ve un
                        usuario exige `puede_ver_en_cola` por cada ticket
                        (varias consultas cada uno) y haría de Inicio una
                        pantalla lenta; el conteo real queda para el rediseño
                        de Trabajo.
  Tickets recientes     Tickets propios más recientemente actualizados.

Cada función hace consultas acotadas (límites explícitos, sin cargar el
catálogo completo ni consultar por tarjeta).
"""

import calendar
from collections import Counter
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as formatear_fecha
from django.utils.formats import date_format

from apps.aprobaciones.consultas import aprobaciones_pendientes_para
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.core.autorizacion import alcances_autorizados
from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
from apps.tareas.models import Tarea
from apps.tickets.models import EntregaTicket, Ticket

LIMITE_CATEGORIAS_TILES = 6
LIMITE_TARJETAS_USADAS = 3
LIMITE_TICKETS_RECIENTES = 4
LIMITE_RESULTADOS_EXPLORADOR = 48
DIAS_PROXIMOS = 14
LIMITE_EVENTOS = 200

def tono(pk):
    """1–4: variante de color de categoría (tokens `--tag-*` de V0), estable
    por categoría. Es identidad visual, no un dato."""
    return (pk or 0) % 4 + 1


DIAS_SEMANA = (
    ("L", "Lunes"), ("M", "Martes"), ("X", "Miércoles"), ("J", "Jueves"),
    ("V", "Viernes"), ("S", "Sábado"), ("D", "Domingo"),
)


# --- Hero -------------------------------------------------------------------


def saludo(usuario, ahora=None):
    """Saludo humano según la hora local. Solo el nombre de pila: cargo, área
    y demás datos organizacionales pertenecen a Mi perfil."""
    ahora = timezone.localtime(ahora or timezone.now())
    if ahora.hour < 12:
        momento = "Buenos días"
    elif ahora.hour < 19:
        momento = "Buenas tardes"
    else:
        momento = "Buenas noches"
    nombre = (usuario.first_name or "").split(" ")[0] or usuario.get_username()
    dia_semana = date_format(ahora.date(), "l")
    mes_anio = date_format(ahora.date(), "F Y")
    return {
        "momento": momento, "nombre": nombre,
        "dia": ahora.day, "dia_semana": dia_semana[:1].upper() + dia_semana[1:], "mes_anio": mes_anio,
    }


# --- Explorar ---------------------------------------------------------------


def categorias_con_servicios(usuario):
    """[{id, nombre, n}] de las categorías activas que tienen al menos un
    servicio/proceso visible para `usuario`, de mayor a menor cantidad. Una
    sola consulta agregada; nunca carga los servicios."""
    filas = (
        servicios_visibles_para(usuario)
        .filter(categoria__activo=True)
        .values("categoria_id", "categoria__nombre")
        .annotate(n=Count("pk"))
        .order_by("-n", "categoria__nombre")
    )
    return [
        {"id": f["categoria_id"], "nombre": f["categoria__nombre"], "n": f["n"], "tono": tono(f["categoria_id"])}
        for f in filas
    ]


def buscar_servicios(usuario, texto="", categoria_id=None, limite=LIMITE_RESULTADOS_EXPLORADOR):
    """Resultados del explorador: SIEMPRE sobre `servicios_visibles_para`.
    Busca por nombre, descripción o categoría ("necesidad" del usuario), no
    solo por título. Devuelve (lista acotada, hay_mas)."""
    consulta = servicios_visibles_para(usuario).filter(categoria__activo=True)
    if categoria_id:
        consulta = consulta.filter(categoria_id=categoria_id)
    texto = (texto or "").strip()
    if texto:
        consulta = consulta.filter(
            Q(nombre__icontains=texto) | Q(descripcion__icontains=texto) | Q(categoria__nombre__icontains=texto)
        )
    resultados = list(consulta.select_related("categoria").order_by("nombre")[: limite + 1])
    for servicio in resultados:
        servicio.tono = tono(servicio.categoria_id)
    return resultados[:limite], len(resultados) > limite


def ticket_general_disponible(usuario):
    """4.C1 — ¿se ofrece la entrada "Crear ticket general"? Solo si está
    habilitado, el Servicio interno es accesible para el usuario y tiene un
    formulario activo. Es solo un CTA: la vista y la operación de dominio vuelven
    a validarlo. El Servicio interno nunca aparece como Servicio ordinario."""
    from apps.catalogo.ticket_general import servicio_disponible_para

    return servicio_disponible_para(usuario) is not None


# --- Frecuentes y recientes ---------------------------------------------------


def servicios_usados(usuario):
    """{"frecuentes": [...], "recientes": [...]} derivados de los tickets del
    propio usuario. Frecuente = usado 2 o más veces. Se vuelve a filtrar por
    `servicios_visibles_para`: nunca se muestra algo que ya no puede usar.
    Sin recomendaciones ni favoritos."""
    filas = list(
        Ticket.objects.filter(solicitante=usuario)
        .values("detalle_servicio__servicio_id")
        .annotate(n=Count("pk"), ultimo=Max("creado_en"))
        .order_by("-ultimo")[:30]
    )
    if not filas:
        return {"frecuentes": [], "recientes": []}
    visibles = {
        s.pk: s
        for s in servicios_visibles_para(usuario)
        .filter(pk__in=[f["detalle_servicio__servicio_id"] for f in filas], categoria__activo=True)
        .select_related("categoria")
    }
    for servicio in visibles.values():
        servicio.tono = tono(servicio.categoria_id)
    usados = [
        (visibles[f["detalle_servicio__servicio_id"]], f["n"], f["ultimo"])
        for f in filas
        if f["detalle_servicio__servicio_id"] in visibles
    ]
    frecuentes = sorted((u for u in usados if u[1] >= 2), key=lambda u: (-u[1], -u[2].timestamp()))
    frecuentes = [u[0] for u in frecuentes[:LIMITE_TARJETAS_USADAS]]
    recientes = [u[0] for u in usados if u[0] not in frecuentes][:LIMITE_TARJETAS_USADAS]
    return {"frecuentes": frecuentes, "recientes": recientes}


# --- Tickets recientes ----------------------------------------------------------


def tickets_recientes(usuario):
    """Pocos tickets propios, los de actividad más reciente (todos los
    estados salvo CANCELADO). No replica Mis tickets: es continuidad."""
    tickets = list(
        Ticket.objects.filter(solicitante=usuario)
        .exclude(estado=Ticket.Estado.CANCELADO)
        .select_related("detalle_servicio__servicio")
        .order_by("-actualizado_en")[:LIMITE_TICKETS_RECIENTES]
    )
    for ticket in tickets:
        ticket.url_inicio = reverse(
            "tickets:borrador" if ticket.estado == Ticket.Estado.BORRADOR else "tickets:detalle", args=[ticket.pk]
        )
    return tickets


# --- Tu trabajo ----------------------------------------------------------------


def resumen_trabajo(usuario):
    """Pendientes reales del usuario, o None si no tiene capacidad de Trabajo
    (ni Cola ni trabajo personal). Mismo criterio de visibilidad de Trabajo
    que `apps.core.navegacion`. `hay_pendientes` distingue "tiene cosas por
    hacer" (Inicio muestra el resumen) de "capacidad sin pendientes a su
    nombre" (solo una señal discreta de "Todo al día")."""
    alcances = alcances_autorizados(usuario, "tickets.atender")
    acceso_cola = bool(alcances["global"] or alcances["areas"] or alcances["unidades_negocio"])
    abiertas = ~Q(estado=Tarea.Estado.COMPLETADA)
    tareas = tareas_asignadas_a(usuario).filter(abiertas).count()
    tareas_por_tomar = tareas_disponibles_para_tomar(usuario).filter(abiertas).count()
    aprobaciones = aprobaciones_pendientes_para(usuario).count()
    tickets_a_cargo = Ticket.objects.filter(usuario_responsable=usuario, estado=Ticket.Estado.EN_ATENCION).count()
    hay_pendientes = bool(tareas or tareas_por_tomar or aprobaciones or tickets_a_cargo)
    if not (acceso_cola or hay_pendientes):
        return None
    return {
        # Pendientes A SU NOMBRE (la Cola no se cuenta, ver docstring del
        # módulo): sin ellos Inicio solo muestra una señal discreta.
        "hay_pendientes": hay_pendientes,
        "acceso_cola": acceso_cola,
        "tareas": tareas,
        "tareas_por_tomar": tareas_por_tomar,
        "aprobaciones": aprobaciones,
        "tickets_a_cargo": tickets_a_cargo,
        # Mismo destino que el dock: Cola si tiene acceso; si no, Mi trabajo.
        "url": reverse("tickets:cola" if acceso_cola else "core:mi_trabajo"),
    }


# --- Tu día / agenda -----------------------------------------------------------


def _mes_solicitado(valor, hoy):
    """`?mes=AAAA-MM` → (año, mes); cualquier valor inválido o fuera de rango
    cae en el mes actual."""
    try:
        anio, mes = (int(p) for p in str(valor).split("-"))
        if 2000 <= anio <= 2100 and 1 <= mes <= 12:
            return anio, mes
    except (TypeError, ValueError):
        pass
    return hoy.year, hoy.month


def _desplazar_mes(anio, mes, delta):
    indice = anio * 12 + (mes - 1) + delta
    return indice // 12, indice % 12 + 1


def _eventos(usuario, hasta):
    """Eventos reales con fecha hasta `hasta` (incluye lo vencido): tareas
    pendientes con fecha límite y entregas por responder. Dos consultas
    acotadas."""
    eventos = []
    tareas = (
        tareas_asignadas_a(usuario)
        .exclude(estado=Tarea.Estado.COMPLETADA)
        .filter(fecha_limite__isnull=False, fecha_limite__lt=hasta)
        .order_by("fecha_limite")[:LIMITE_EVENTOS]
    )
    for tarea in tareas:
        eventos.append(
            {
                "tipo": "tarea", "titulo": tarea.titulo, "fecha": tarea.fecha_limite,
                "url": reverse("tareas:detalle", args=[tarea.pk]), "etiqueta": "Vence la tarea",
            }
        )
    entregas = (
        EntregaTicket.objects.filter(
            ticket__solicitante=usuario, estado=EntregaTicket.Estado.PENDIENTE,
            vence_en__isnull=False, vence_en__lt=hasta,
        )
        .select_related("ticket__detalle_servicio__servicio")
        .order_by("vence_en")[:LIMITE_EVENTOS]
    )
    for entrega in entregas:
        eventos.append(
            {
                "tipo": "entrega", "titulo": entrega.ticket.detalle_servicio.servicio.nombre,
                "fecha": entrega.vence_en, "url": reverse("tickets:detalle", args=[entrega.ticket_id]),
                "etiqueta": "Plazo para responder la entrega",
            }
        )
    return sorted(eventos, key=lambda e: e["fecha"])


def agenda(usuario, mes_param=None, ahora=None):
    """Mes con marcadores + lista de lo próximo. El calendario es un elemento
    permanente de "Tu día": existe aunque el usuario no tenga nada con fecha
    (`hay_eventos` False); los marcadores y la lista salen solo de datos
    reales."""
    ahora = timezone.localtime(ahora or timezone.now())
    hoy = ahora.date()
    anio, mes = _mes_solicitado(mes_param, hoy)
    primer_dia = date(anio, mes, 1)
    siguiente = date(*_desplazar_mes(anio, mes, 1), 1)
    zona = ahora.tzinfo
    fin_mes = datetime.combine(siguiente, time.min, tzinfo=zona)
    fin_proximos = datetime.combine(hoy + timedelta(days=DIAS_PROXIMOS + 1), time.min, tzinfo=zona)

    eventos = _eventos(usuario, max(fin_mes, fin_proximos))
    for evento in eventos:
        evento["local"] = timezone.localtime(evento["fecha"])
        evento["vencido"] = evento["fecha"] < ahora and evento["tipo"] == "tarea"
        evento["hoy"] = evento["local"].date() == hoy

    proximos = [e for e in eventos if hoy <= e["local"].date() < hoy + timedelta(days=DIAS_PROXIMOS + 1)]
    vencidos = [e for e in eventos if e["vencido"]]

    por_dia = Counter(e["local"].date() for e in eventos if primer_dia <= e["local"].date() < siguiente)
    semanas = []
    for semana in calendar.Calendar(firstweekday=0).monthdatescalendar(anio, mes):
        semanas.append(
            [
                {
                    "dia": d.day, "en_mes": d.month == mes, "hoy": d == hoy, "n": por_dia.get(d, 0),
                    "etiqueta": formatear_fecha(d, "j \\d\\e F"),
                }
                for d in semana
            ]
        )
    mes_anterior = "%04d-%02d" % _desplazar_mes(anio, mes, -1)
    mes_siguiente = "%04d-%02d" % _desplazar_mes(anio, mes, 1)
    titulo_mes = date_format(primer_dia, "F Y")
    return {
        "titulo_mes": titulo_mes[:1].upper() + titulo_mes[1:],
        "dias_semana": DIAS_SEMANA,
        "semanas": semanas,
        "mes_anterior": mes_anterior,
        "mes_siguiente": mes_siguiente,
        "es_mes_actual": (anio, mes) == (hoy.year, hoy.month),
        "proximos": proximos[:6],
        "total_proximos": len(proximos),
        "vencidas": len(vencidos),
        "hay_eventos": bool(eventos),
    }
