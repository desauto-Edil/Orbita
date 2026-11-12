"""Histórico de tickets — requerimiento nuevo del usuario (2026-11-11; CU por asignar, extiende CU-014/CU-015 y la
bandeja de Mi trabajo): una pestaña «Histórico» en Mi trabajo y en Mis tickets con los tickets que YA NO están en
curso (resueltos, cerrados o cancelados), con filtros de día, mes, persona, tipo, servicio/proceso y estado.

- Mis tickets › Histórico = lo que la persona SOLICITÓ y ya terminó.
- Mi trabajo › Histórico = los tickets que la persona ATENDIÓ y ya terminaron: fue su responsable, actuó en su
  historia (tomarlo, resolverlo…), completó una de sus actividades o decidió una de sus aprobaciones.

Solo LECTURA y solo presentación: no concede ninguna capacidad. Cada fila abre el espacio del ticket, que revalida
su propia autorización y, cerrado o cancelado, lo muestra en solo consulta (`espacio.solo_consulta`). La fecha de
referencia de los filtros es la de FINALIZACIÓN (la última vez que se resolvió, cerró o canceló, leída del
`HistorialTicket`; sin ese evento, la última actualización)."""

from datetime import date

from django.db.models import DateTimeField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from apps.tickets.models import HistorialTicket, Ticket

ESTADOS = (Ticket.Estado.RESUELTO, Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO)
EVENTOS_DE_FIN = ("RESUELTO", "CERRADO", "CANCELADO")
POR_PAGINA = 30


def _base(qs):
    fin = (
        HistorialTicket.objects.filter(ticket=OuterRef("pk"), tipo_evento__in=EVENTOS_DE_FIN)
        .order_by("-creado_en", "-pk").values("creado_en")[:1]
    )
    return (
        qs.filter(estado__in=ESTADOS)
        .select_related("detalle_servicio__servicio", "solicitante", "usuario_responsable", "equipo_responsable")
        .annotate(finalizado_en=Coalesce(Subquery(fin, output_field=DateTimeField()), "actualizado_en"))
        .order_by("-finalizado_en", "-pk")
    )


def solicitados_por(usuario):
    """Lo que `usuario` solicitó y ya no está en curso."""
    return _base(Ticket.objects.filter(solicitante=usuario))


def _ids_por_actividades(usuario):
    from apps.aprobaciones.models import Aprobacion
    from apps.tareas.models import Tarea
    from apps.tickets import vinculos

    tareas = Tarea.objects.filter(Q(completada_por=usuario) | Q(usuario_responsable=usuario)).values_list("pk", flat=True)
    ids = {o["ticket_id"] for o in vinculos.origen_de_tareas(list(tareas)).values()}
    aprobaciones = list(Aprobacion.objects.filter(decidido_por=usuario).only("pk", "esquema_id"))
    ids |= {o["ticket_id"] for o in vinculos.origen_de_aprobaciones(aprobaciones).values()}
    return ids


def atendidos_por(usuario):
    """Los tickets que `usuario` atendió (responsable, actor de su historia, actividades o aprobaciones) y que ya
    no están en curso. No incluye lo que solo solicitó (eso está en Mis tickets)."""
    actuo = HistorialTicket.objects.filter(actor=usuario).exclude(tipo_evento="RADICADO").values("ticket_id")
    return _base(
        Ticket.objects.filter(
            Q(usuario_responsable=usuario) | Q(pk__in=actuo) | Q(pk__in=_ids_por_actividades(usuario))
        ).distinct()
    )


def _fecha(texto):
    try:
        return date.fromisoformat(texto)
    except (TypeError, ValueError):
        return None


def _mes(texto):
    try:
        anio, mes = (int(p) for p in (texto or "").split("-"))
        return (anio, mes) if 1 <= mes <= 12 else None
    except ValueError:
        return None


def _entero(texto):
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


def _nombre(usuario):
    return (usuario.nombre_visible) if usuario is not None else ""


def filtrar(qs, parametros, *, persona_es="ambos"):
    """Aplica los filtros del histórico a `qs` (de `solicitados_por` / `atendidos_por`). Devuelve `(qs, filtros,
    opciones)`: `filtros` = los valores válidos recibidos (para repintar el formulario) y `opciones` = lo elegible en
    los selectores, calculado sobre TODO el histórico de la persona (no sobre lo ya filtrado).

    `persona_es`: "responsable" (Mis tickets: quién lo atendió) o "ambos" (Mi trabajo: quien lo solicitó o lo
    atendió)."""
    todos = list(qs)
    opciones = _opciones(todos, persona_es)
    filtros = {}
    texto = (parametros.get("q") or "").strip()[:80]
    if texto:
        filtros["q"] = texto
    dia = _fecha(parametros.get("dia"))
    if dia:
        filtros["dia"] = dia.isoformat()
    mes = _mes(parametros.get("mes"))
    if mes:
        filtros["mes"] = f"{mes[0]:04d}-{mes[1]:02d}"
    persona = _entero(parametros.get("persona"))
    if persona and persona in {p["pk"] for p in opciones["personas"]}:
        filtros["persona"] = persona
    tipo = parametros.get("tipo")
    if tipo in (Ticket.Tipo.SERVICIO, Ticket.Tipo.PROCESO):
        filtros["tipo"] = tipo
    servicio = _entero(parametros.get("servicio"))
    if servicio and servicio in {s["pk"] for s in opciones["servicios"]}:
        filtros["servicio"] = servicio
    estado = parametros.get("estado")
    if estado in ESTADOS:
        filtros["estado"] = estado

    def coincide(t):
        from django.utils import timezone

        fin = timezone.localtime(t.finalizado_en).date() if t.finalizado_en else None
        if dia and fin != dia:
            return False
        if mes and (fin is None or (fin.year, fin.month) != mes):
            return False
        if "persona" in filtros:
            ids = {t.usuario_responsable_id}
            if persona_es == "ambos":
                ids.add(t.solicitante_id)
            if filtros["persona"] not in ids:
                return False
        if "tipo" in filtros and t.tipo != filtros["tipo"]:
            return False
        if "servicio" in filtros and t.detalle_servicio.servicio_id != filtros["servicio"]:
            return False
        if "estado" in filtros and t.estado != filtros["estado"]:
            return False
        if texto:
            buscado = texto.casefold()
            campos = (t.codigo, t.detalle_servicio.servicio.nombre, t.etiqueta)
            if not any(buscado in (c or "").casefold() for c in campos):
                return False
        return True

    return [t for t in todos if coincide(t)], filtros, {**opciones, "total": len(todos)}


def _opciones(tickets, persona_es):
    personas, servicios = {}, {}
    for t in tickets:
        quienes = [t.usuario_responsable] + ([t.solicitante] if persona_es == "ambos" else [])
        for u in quienes:
            if u is not None:
                personas[u.pk] = _nombre(u)
        servicio = t.detalle_servicio.servicio
        servicios[servicio.pk] = servicio.nombre
    return {
        "personas": [{"pk": pk, "nombre": n} for pk, n in sorted(personas.items(), key=lambda x: x[1].casefold())],
        "servicios": [{"pk": pk, "nombre": n} for pk, n in sorted(servicios.items(), key=lambda x: x[1].casefold())],
        "tipos": [(Ticket.Tipo.SERVICIO, "Servicio"), (Ticket.Tipo.PROCESO, "Proceso")],
        "estados": [(e.value, e.label) for e in ESTADOS],
    }


def contar_atendidos(usuario):
    return atendidos_por(usuario).count()


def contexto(qs, parametros, *, ver, persona_es, persona_etiqueta, vacio):
    """Lo que pinta `tickets/bandeja/_historico.html`: filtros, opciones y la página pedida."""
    from django.core.paginator import Paginator
    from django.utils.http import urlencode

    filas, filtros, opciones = filtrar(qs, parametros, persona_es=persona_es)
    pagina = Paginator(filas, POR_PAGINA).get_page(parametros.get("pagina"))
    return {
        "pagina": pagina,
        "filtros": filtros,
        "opciones": opciones,
        "ver": ver,
        "persona_etiqueta": persona_etiqueta,
        "vacio": vacio,
        "consulta": urlencode({"ver": ver, **filtros}) + "&",
    }
