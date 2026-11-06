"""Espacio unificado del Ticket (UX-1, incrementos 2–4) — composición de LECTURA.

Una sola pantalla (`tickets:espacio`) para quien atiende, coordina, produce, aprueba, revisa o solicita. No hay
vistas por rol: lo que se muestra sale de la RELACIÓN de la persona con el Ticket, calculada solo con las reglas
`puede_*` que ya existen en cada dominio (`apps.tickets.autorizacion`, `apps.tareas.autorizacion`,
`apps.aprobaciones.autorizacion`, `apps.gacetas.autorizacion`, `edicion_por_tarea`). Este módulo no cambia estado,
no decide avance y no amplía permisos: cada formulario de la pantalla envía a la operación de dominio de siempre,
que vuelve a autorizar y validar bajo lock.

Quién entra (`Relacion.puede_abrir`): quien ya puede consultar el ticket (`puede_consultar_ticket`: solicitante y
quien lo atiende o supervisa) y, además, quien tiene en él una relación directa con un objeto: responsable (o
candidato a tomar) de una de sus tareas, aprobador directo de una de sus aprobaciones, quien puede consultar una de
sus gacetas, participante de una de sus colecciones o revisor de uno de sus conjuntos. Esa segunda población NO gana
el resto del ticket: ve el encabezado mínimo (código, nombre, estado) y solo las piezas que su propia relación
autoriza (su actividad, el ítem que trabaja o revisa, la colección en la que participa). Proceso, Información y el
contexto lateral completo exigen `puede_consultar_ticket`, como siempre.

La asistencia guiada de «Mi actividad» se arma con datos REALES: la descripción configurada de la tarea o del
bloque, la lista de verificación, la evidencia exigida, las subtareas abiertas, las rutas configuradas del bloque
(`TransicionBloqueOperativo`) o el siguiente bloque que el motor tomaría (`motor._siguiente_bloque_lineal`), las
condiciones de cierre (`finalizacion.evaluar`) y los mensajes de dominio. Si un dato no existe, no se inventa."""

from dataclasses import dataclass, field

from django.urls import reverse
from django.utils import timezone

from apps.tareas.models import Tarea
from apps.tickets import seguimiento, vinculos
from apps.tickets import trabajo as trabajo_ops
from apps.tickets.autorizacion import (
    es_propietario_borrador,
    es_responsable_actual,
    puede_cancelar_ticket,
    puede_cerrar_ticket,
    puede_comentar_ticket,
    puede_consultar_coleccion,
    puede_consultar_ticket,
    puede_editar_parrilla,
    puede_entregar_ticket,
    puede_escribir_entregables_finales,
    puede_iniciar_atencion,
    puede_operar_coleccion,
    puede_reabrir_ticket,
    puede_resolver_ticket,
    puede_responder_entrega,
    puede_revisar_conjunto,
    puede_solicitar_informacion,
    puede_tomar,
    puede_ver_en_cola,
)
from apps.tickets.models import EntregaTicket, SolicitudInformacion, Ticket

PESTANAS = (
    ("actividad", "Mi actividad"),
    ("elementos", "Elementos"),
    ("proceso", "Proceso"),
    ("informacion", "Información"),
)


# --- Relación de la persona con el Ticket -----------------------------------------------------------------


@dataclass
class Relacion:
    """Todo lo que `usuario` es respecto de ESTE ticket, resuelto con las reglas de cada dominio."""

    es_solicitante: bool = False
    consulta: bool = False
    atiende: bool = False
    responsable: bool = False
    responsable_individual: bool = False
    tareas: list = field(default_factory=list)
    aprobaciones: list = field(default_factory=list)
    gacetas: list = field(default_factory=list)
    colecciones: list = field(default_factory=list)
    conjuntos: list = field(default_factory=list)
    origen_tareas: dict = field(default_factory=dict)
    origen_aprobaciones: dict = field(default_factory=dict)

    @property
    def puede_abrir(self):
        return bool(
            self.consulta or self.tareas or self.aprobaciones or self.gacetas or self.colecciones or self.conjuntos
        )

    @property
    def roles(self):
        roles = []
        if self.responsable_individual or self.responsable:
            roles.append("Responsable")
        elif self.atiende:
            roles.append("Supervisión")
        if self.es_solicitante:
            roles.append("Solicitante")
        if any(self.origen_tareas.get(t.pk, {}).get("origen") != "GACETA" for t in self.tareas):
            roles.append("Actividades")
        if any(self.origen_tareas.get(t.pk, {}).get("origen") == "GACETA" for t in self.tareas):
            roles.append("Corrección")
        if self.aprobaciones:
            roles.append("Aprobación")
        if any(g["revisor"] for g in self.gacetas):
            roles.append("Revisión de gaceta")
        if any(g["coordina"] for g in self.gacetas):
            roles.append("Coordinación de gaceta")
        if self.conjuntos:
            roles.append("Revisión de elementos")
        if any(c["participante"] for c in self.colecciones):
            roles.append("Participante de colección")
        return roles


def relacion(usuario, ticket):
    from apps.aprobaciones.autorizacion import es_aprobador_directo
    from apps.gacetas import autorizacion as gaceta_aut
    from apps.gacetas.models import GacetaTicket
    from apps.tareas import autorizacion as tarea_aut
    from apps.tickets.autorizacion import es_participante_coleccion
    from apps.tickets.parrilla import parrillas_de

    rel = Relacion(
        es_solicitante=es_propietario_borrador(usuario, ticket),
        consulta=puede_consultar_ticket(usuario, ticket),
        atiende=puede_ver_en_cola(usuario, ticket),
        responsable=es_responsable_actual(usuario, ticket),
        responsable_individual=ticket.usuario_responsable_id is not None and ticket.usuario_responsable_id == usuario.pk,
    )
    # Tareas: solo las de relación directa (responsable, delegada, creadora o candidata a tomarla), no las que se
    # ven por un permiso de supervisión global de tareas.
    tareas = list(
        vinculos.tareas_del_ticket(ticket)
        .select_related("usuario_responsable", "equipo_responsable", "tarea_padre")
        .order_by("creado_en", "pk")
    )
    rel.tareas = [
        t for t in tareas
        if tarea_aut.es_responsable_actual(usuario, t) or tarea_aut.es_creadora(usuario, t)
        or tarea_aut.puede_tomar_tarea(usuario, t)
    ]
    rel.origen_tareas = vinculos.origen_de_tareas([t.pk for t in rel.tareas])
    aprobaciones = list(
        vinculos.aprobaciones_del_ticket(ticket).select_related("esquema", "aprobador_usuario", "aprobador_equipo")
        .order_by("creado_en", "pk")
    )
    rel.aprobaciones = [a for a in aprobaciones if es_aprobador_directo(usuario, a)]
    rel.origen_aprobaciones = vinculos.origen_de_aprobaciones(rel.aprobaciones)
    for gaceta in GacetaTicket.objects.filter(ticket=ticket).order_by("pk"):
        gaceta.ticket = ticket
        if gaceta_aut.puede_consultar_gaceta(usuario, gaceta):
            rel.gacetas.append({
                "gaceta": gaceta,
                "revisor": gaceta_aut.es_revisor(usuario, gaceta),
                "aprobador": gaceta_aut.es_aprobador(usuario, gaceta),
                "coordina": gaceta_aut.puede_coordinar(usuario, gaceta),
            })
    for parrilla in parrillas_de(ticket):
        if puede_consultar_coleccion(usuario, parrilla, ticket):
            rel.colecciones.append({
                "parrilla": parrilla,
                "participante": es_participante_coleccion(usuario, parrilla),
                "opera": puede_operar_coleccion(usuario, parrilla, ticket),
            })
    for entregable in ticket.entregables.filter(tipo="ELEMENTOS"):
        if puede_revisar_conjunto(usuario, entregable):
            rel.conjuntos.append(entregable)
    return rel


# --- «Qué ocurre después» desde la configuración real del flujo -----------------------------------------


def _destino_de(transicion):
    if transicion.finaliza:
        return "el flujo termina"
    if transicion.bloque_destino_id is None:
        return "ruta sin destino configurado"
    return f"sigue «{transicion.bloque_destino.nombre}»"


def despues_de_bloque(bloque, esquema=None):
    """Frases sobre lo que hará el motor al completar `bloque`, tomadas de su configuración: las rutas por
    resultado de una APROBACION y, si no hay ruta, el siguiente bloque que el motor elige en línea
    (`motor._siguiente_bloque_lineal`, la misma función que usa al avanzar)."""
    from apps.workflows.motor import _siguiente_bloque_lineal

    siguiente = _siguiente_bloque_lineal(bloque)
    if siguiente is not None:
        lineal = f"sigue «{siguiente.nombre}»" + (
            f" (fase {siguiente.fase.nombre})" if siguiente.fase_id != bloque.fase_id else ""
        )
    elif bloque.contenedor_id is not None:
        lineal = "termina el recorrido de este ítem"
    else:
        lineal = "el flujo termina"
    if bloque.tipo != "APROBACION":
        return [f"Al completarla, {lineal}."]
    rutas = {
        t.resultado_aprobacion: t
        for t in bloque.transiciones_salientes.select_related("bloque_destino").all()
        if t.resultado_aprobacion
    }
    frases = []
    opciones = [("APROBADA", "Si apruebas"), ("DEVUELTA", "Si devuelves"), ("RECHAZADA", "Si rechazas")]
    if esquema is not None and esquema.permite_exclusion:
        opciones.append(("EXCLUIDA", "Si excluyes"))
    for resultado, etiqueta in opciones:
        ruta = rutas.get(resultado)
        if ruta is not None:
            frases.append(f"{etiqueta}, {_destino_de(ruta)}.")
        elif resultado == "APROBADA":
            frases.append(f"{etiqueta}, {lineal}.")
        elif resultado in ("RECHAZADA", "EXCLUIDA") and bloque.contenedor_id is not None:
            frases.append(f"{etiqueta}, el ítem sale del alcance y termina solo su recorrido.")
    return frases


def _bloque_de_tarea(tarea):
    from apps.workflows.models import TareaWorkflow

    principal = tarea.tarea_padre_id or tarea.pk
    vinculo = TareaWorkflow.objects.filter(tarea_id=principal).select_related(
        "instancia_etapa__bloque_operativo__fase", "instancia_etapa__instancia_workflow"
    ).first()
    return vinculo.instancia_etapa if vinculo is not None else None


def _ejecucion_de_aprobacion(aprobacion):
    from apps.workflows.models import EsquemaAprobacionWorkflow

    vinculo = EsquemaAprobacionWorkflow.objects.filter(esquema_id=aprobacion.esquema_id).select_related(
        "instancia_etapa__bloque_operativo__fase"
    ).first()
    return vinculo.instancia_etapa if vinculo is not None else None


# --- UX-4: lenguaje de la acción, nombre del ítem y su recorrido individual --------------------------------


def etiqueta_de_avance(bloque):
    """Texto del botón que completa una actividad, según lo que el motor hará después (su siguiente paso real):
    a una aprobación → «Enviar a aprobación»; a una gaceta → «Enviar a revisión»; si no, «Registrar <actividad>»."""
    from apps.workflows.motor import _siguiente_bloque_lineal

    siguiente = _siguiente_bloque_lineal(bloque)
    if siguiente is not None and siguiente.tipo == "APROBACION":
        return "Enviar a aprobación"
    if siguiente is not None and siguiente.tipo == "GACETA":
        return "Enviar a revisión"
    return f"Registrar {bloque.nombre[:1].lower()}{bloque.nombre[1:]}"


def nombre_de_item(elemento):
    from apps.tickets.parrilla import nombre_de

    return nombre_de(elemento)


def recorrido_de_item(elemento):
    """El recorrido INDIVIDUAL de un ítem para dibujarlo: pasos visibles (desde el paso por el que entra un ítem
    enviado), situación de cada uno (completado · actual · devuelto · pendiente), en qué paso va y su resultado.
    `None` si el ítem aún no tiene recorrido (es un borrador o el flujo no lo recorre). Solo lee."""
    from apps.workflows import recorridos
    from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

    hija = elemento.recorridos.select_related("ejecucion_padre__bloque_operativo").order_by("-pk").first()
    if hija is None or hija.ejecucion_padre is None or hija.ejecucion_padre.bloque_operativo is None:
        return None
    contenedor = hija.ejecucion_padre.bloque_operativo
    secuencia = recorridos.bloques_de_rama(contenedor, "ITEM")
    configuracion = recorridos.configuracion_de(contenedor)
    inicio = next((i for i, b in enumerate(secuencia) if b.clave == configuracion.paso_al_enviar), 0)
    visibles = secuencia[inicio:]
    ejecuciones = list(hija.ejecuciones_etapa.select_related("bloque_operativo", "transicion_bloque_tomada").order_by("orden"))
    ultima = ejecuciones[-1] if ejecuciones else None
    en_curso = hija.estado in recorridos.ESTADOS_EN_CURSO
    detenido = hija.estado == InstanciaWorkflow.Estado.ERROR
    resultado = recorridos.resultado_de(hija)
    devuelto = (
        (en_curso or detenido) and len(ejecuciones) >= 2 and ejecuciones[-2].transicion_bloque_tomada_id is not None
        and ejecuciones[-2].transicion_bloque_tomada.resultado_aprobacion == "DEVUELTA"
    )
    ids = [b.pk for b in visibles]
    actual_id = ultima.bloque_operativo_id if ultima is not None and (en_curso or detenido) else None
    if actual_id in ids:
        indice = ids.index(actual_id)
    elif actual_id is not None:
        indice = 0  # un paso previo a la entrada (p. ej. «Ajustar la idea»): se dibuja sobre el primero, devuelto
    else:
        indice = len(ids)
    terminado = not (en_curso or detenido)
    pasos = []
    for posicion, bloque in enumerate(visibles):
        if terminado or posicion < indice:
            situacion = "completado" if resultado == recorridos.COMPLETADO or posicion < indice else "fuera"
        elif posicion == indice:
            situacion = "detenido" if detenido else ("devuelto" if devuelto else "actual")
        else:
            situacion = "pendiente"
        pasos.append({"nombre": bloque.nombre, "situacion": situacion})
    paso_actual = ultima.bloque_operativo.nombre if actual_id is not None and ultima.bloque_operativo_id else ""
    estado = (
        f"Detenido en «{paso_actual}»" if detenido
        else f"Devuelto · {paso_actual}" if devuelto
        else paso_actual if en_curso
        else trabajo_ops.ESTADO_RECORRIDO.get(resultado, resultado)
    )
    return {
        "pasos": pasos, "paso_actual": paso_actual, "estado": estado, "devuelto": devuelto, "detenido": detenido,
        "terminado": terminado, "resultado": resultado, "hechos": min(indice, len(pasos)), "total": len(pasos),
        "esperando": bool(ultima is not None and ultima.estado == InstanciaEtapa.Estado.EN_ESPERA),
        "contenedor": contenedor,
    }


def confirmacion_de_item(usuario, elemento, hecho):
    """Mensaje tras una acción sobre un ítem: qué ocurrió, dónde quedó y quién continúa (leído del recorrido)."""
    elemento.refresh_from_db()
    nombre = nombre_de_item(elemento)
    hija = elemento.recorridos.order_by("-pk").first()
    if hija is None:
        return f"{hecho}: «{nombre}»."
    fila = trabajo_ops._fila_de_recorrido(usuario, hija, False)
    if fila["con_error"]:
        return f"{hecho}: «{nombre}». Su recorrido se detuvo: {fila['error'] or 'requiere revisión'}."
    if fila["en_curso"] and fila["paso"]:
        quien = f", con {fila['responsable']}" if fila["responsable"] else ""
        return f"{hecho}: «{nombre}». Sigue en «{fila['paso']}»{quien}."
    return f"{hecho}: «{nombre}». Terminó su recorrido ({fila['estado'].lower()})."


def tarea_es_del_ticket(tarea, ticket):
    origen = vinculos.origen_de_tareas([tarea.pk]).get(tarea.pk)
    return origen is not None and origen["ticket_id"] == ticket.pk


def _accion_borradores(usuario, ticket, info):
    """UX-4 — misión de quien prepara los ítems de una colección cuyo recorrido recibe ítems AL ENVIARLOS: registrar
    borradores, enviarlos uno a uno (con lo que falta a cada uno) y, si puede, cerrar el alcance. Solo existe
    mientras el flujo está esperando ese recorrido."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.workflows import recorridos

    parrilla = info["parrilla"]
    ejecucion = recorridos.recorrido_al_enviar(ticket, parrilla)
    if ejecucion is None or not info["opera"]:
        return None
    borradores = [
        {"elemento": e, "nombre": nombre_de_item(e), "faltan": recorridos.faltantes_para_enviar(e, ejecucion)}
        for e in recorridos.borradores_de(ejecucion, parrilla)
    ]
    if parrilla.alcance_cerrado and not borradores:
        return None  # ya se envió todo: lo que queda es de cada recorrido
    avance = recorridos.progreso(ejecucion)
    plural = (parrilla.nombre_elemento or "ítem").lower()
    tabla = parrilla_ops.tabla(ticket, usuario, parrilla=parrilla)
    puede_cerrar = puede_editar_parrilla(usuario, ticket) and not parrilla.alcance_cerrado
    accion = _accion(
        f"borradores-{parrilla.clave}", "borradores",
        f"Prepara y envía cada {plural}" if not parrilla.alcance_cerrado else f"Envía las {plural} pendientes",
        subtitulo=f"{len(borradores)} por enviar · {avance.en_curso} en curso · {avance.completados} terminadas",
        parrilla=parrilla, borradores=borradores, tabla=tabla, puede_cerrar=puede_cerrar,
        puede_agregar=not parrilla.alcance_cerrado,
        que=(
            f"Registra cada {plural} como borrador y envíala a aprobación cuando tenga sus datos. "
            "Cada una avanza por su cuenta."
        ),
    )
    exigidas = recorridos.configuracion_de(ejecucion.bloque_operativo).columnas_requeridas_ingreso
    if exigidas:
        columnas = {c.clave: c.etiqueta for c in parrilla_ops.columnas_de(parrilla)}
        accion["informacion"].append("Para enviar se necesita: " + ", ".join(columnas.get(c, c) for c in exigidas) + ".")
    if not parrilla.alcance_cerrado:
        accion["despues"] = [
            f"Cuando no vayas a agregar más, cierra el alcance: el proceso sigue cuando todas las {plural} terminen."
        ]
    elif borradores:
        accion["impedimentos"].append(
            f"El proceso no sigue mientras queden {plural} sin enviar: envíalas o exclúyelas."
        )
    return accion


# --- Acciones de «Mi actividad» ---------------------------------------------------------------------------


def _accion(clave, tipo, titulo, **extra):
    datos = {
        "clave": clave, "tipo": tipo, "titulo": titulo, "subtitulo": "", "estado": "", "vence": None,
        "que": "", "informacion": [], "requisitos": [], "impedimentos": [], "despues": [],
    }
    datos.update(extra)
    return datos


def _nombre(usuario):
    return seguimiento.nombre_visible(usuario)


def _responsable_de_tarea(tarea):
    if tarea.usuario_responsable_id:
        return _nombre(tarea.usuario_responsable)
    return tarea.equipo_responsable.nombre if tarea.equipo_responsable_id else ""


def _accion_tarea(usuario, ticket, tarea, origen):
    from apps.tareas import autorizacion as tarea_aut
    from apps.tareas.operaciones import tiene_evidencia, verificaciones_obligatorias_pendientes
    from apps.tickets import parrilla as parrilla_ops

    puede_completar = tarea_aut.puede_completar_tarea(usuario, tarea)
    puede_tomar_tarea = tarea_aut.puede_tomar_tarea(usuario, tarea)
    verificaciones = list(tarea.verificaciones.select_related("marcado_por").all())
    subtareas = list(tarea.subtareas.select_related("usuario_responsable", "equipo_responsable").all())
    evidencias = list(tarea.adjuntos.select_related("subido_por").all())
    elemento = None
    ficha = None
    if origen and origen.get("elemento_id"):
        elemento = parrilla_ops.ElementoParrilla.objects.select_related("parrilla").filter(
            pk=origen["elemento_id"], parrilla__ticket=ticket
        ).first()
        if elemento is not None:
            ficha = parrilla_ops.ficha_por_tarea(elemento, usuario)

    tipo_origen = (origen or {}).get("origen")
    accion = _accion(
        f"tarea-{tarea.pk}", "tarea", tarea.titulo,
        subtitulo=(
            f"{elemento.parrilla.nombre_elemento or 'Ítem'} #{elemento.numero}" if elemento is not None
            else ("Subtarea de «" + tarea.tarea_padre.titulo + "»" if tarea.tarea_padre_id else "")
        ),
        estado=tarea.get_estado_display(),
        vence=tarea.fecha_limite,
        que=tarea.descripcion,
        tarea=tarea,
        elemento=elemento,
        ficha=ficha,
        verificaciones=verificaciones,
        subtareas=subtareas,
        evidencias=evidencias,
        responsable=_responsable_de_tarea(tarea),
        puede_tomar=puede_tomar_tarea,
        puede_iniciar=tarea_aut.puede_iniciar_tarea(usuario, tarea),
        puede_completar=puede_completar,
        puede_comentar=tarea_aut.puede_comentar_tarea(usuario, tarea),
        puede_crear_subtarea=tarea_aut.puede_crear_subtarea(usuario, tarea),
        puede_marcar=puede_completar and tarea.estado in (Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO),
        origen=tipo_origen or "",
    )
    if tarea.fecha_prevista:
        accion["informacion"].append(f"Fecha prevista: {timezone.localtime(tarea.fecha_prevista):%d/%m/%Y %H:%M}")
    if tarea.fecha_limite:
        accion["informacion"].append(f"Fecha límite: {timezone.localtime(tarea.fecha_limite):%d/%m/%Y %H:%M}")
    if ficha is not None:
        editables = [c["campo"].etiqueta for c in ficha["celdas"] if c["editable"]]
        accion["informacion"].append("Puedes modificar en el ítem: " + ", ".join(editables) + ".")

    # Requisitos que `completar_tarea` exige (los mismos, leídos del dominio).
    pendientes_verificacion = {i.pk for i in verificaciones_obligatorias_pendientes(tarea)}
    for item in verificaciones:
        if item.obligatorio:
            accion["requisitos"].append({"texto": item.texto, "cumplido": item.pk not in pendientes_verificacion})
    if tarea.requiere_evidencia:
        accion["requisitos"].append({"texto": "Adjuntar al menos una evidencia", "cumplido": tiene_evidencia(tarea)})
    abiertas = [s for s in subtareas if s.estado not in Tarea.ESTADOS_CERRADOS]
    if abiertas:
        accion["requisitos"].append({
            "texto": f"Cerrar las subtareas abiertas ({len(abiertas)})", "cumplido": False,
        })
    ejecucion = _bloque_de_tarea(tarea) if tipo_origen == "FLUJO" else None
    if ejecucion is not None and ejecucion.bloque_operativo_id is not None:
        bloque = ejecucion.bloque_operativo
        if (bloque.configuracion or {}).get("exige_datos_inicio"):
            from apps.tickets.datos_inicio import datos_completos

            accion["requisitos"].append({
                "texto": "Datos de inicio del ticket completos", "cumplido": datos_completos(ticket),
            })
        requeridas = (bloque.configuracion or {}).get("columnas_requeridas") or []
        if requeridas and elemento is not None:
            from apps.tickets.parrilla import completitud_de

            faltan = set(completitud_de(elemento, exigidas=requeridas).pendientes)
            columnas = {c.clave: c.etiqueta for c in parrilla_ops.columnas_de(elemento.parrilla)}
            for clave in requeridas:
                if clave in columnas:
                    accion["requisitos"].append({"texto": columnas[clave], "cumplido": columnas[clave] not in faltan})
        if not accion["que"] and bloque.descripcion:
            accion["que"] = bloque.descripcion
        if not tarea.tarea_padre_id:
            accion["despues"] = despues_de_bloque(bloque)
            accion["etiqueta_avance"] = etiqueta_de_avance(bloque)
    if tarea.tarea_padre_id:
        accion["despues"] = ["Al completarla, la tarea principal podrá cerrarse cuando no le queden subtareas abiertas."]
    elif tipo_origen == "ELEMENTO":
        accion["despues"] = ["Cuando se completen todas las tareas por elemento, el flujo continúa solo."]
    elif tipo_origen == "GACETA":
        accion["despues"] = ["Al completarla, el ítem se presenta otra vez en la gaceta con una versión nueva."]
        accion["etiqueta_avance"] = "Enviar a revisión"
    accion.setdefault("etiqueta_avance", "Completar")
    if elemento is not None:
        accion["subtitulo"] = nombre_de_item(elemento)
        accion["recorrido_item"] = recorrido_de_item(elemento)

    if puede_tomar_tarea and not puede_completar:
        accion["impedimentos"].append("Esta tarea es del equipo: tómala para poder completarla.")
    elif not puede_completar and tarea.estado not in Tarea.ESTADOS_CERRADOS:
        responsable = accion["responsable"]
        accion["impedimentos"].append(
            f"La atiende {responsable}." if responsable else "Todavía no tiene responsable."
        )
    return accion


def _accion_aprobacion(usuario, ticket, aprobacion, origen):
    from apps.aprobaciones import views as aprobaciones_views
    from apps.aprobaciones.autorizacion import puede_aprobar

    puede = puede_aprobar(usuario, aprobacion)
    esquema = aprobacion.esquema
    prorroga = aprobaciones_views._contexto_prorroga(usuario, esquema)
    entregable = aprobaciones_views._contexto_entregable_revisado(usuario, aprobacion)
    item = aprobaciones_views._contexto_item(aprobacion)
    ejecucion = _ejecucion_de_aprobacion(aprobacion) if (origen or {}).get("origen") == "FLUJO" else None
    participaciones = list(
        esquema.participaciones.select_related("aprobador_usuario", "aprobador_equipo").order_by("orden")
    )
    titulo = ejecucion.bloque_operativo.nombre if ejecucion is not None and ejecucion.bloque_operativo_id else (
        "Prórroga de la fecha objetivo" if prorroga else (
            f"Gaceta «{item['gaceta'].nombre}»" if item and item.get("gaceta") else f"Aprobación #{aprobacion.pk}"
        )
    )
    accion = _accion(
        f"aprobacion-{aprobacion.pk}", "aprobacion", titulo,
        subtitulo=f"{item['nombre_elemento']} #{item['elemento'].numero}" if item else "",
        estado=aprobacion.get_estado_display(),
        que=(ejecucion.bloque_operativo.descripcion if ejecucion is not None and ejecucion.bloque_operativo_id else ""),
        aprobacion=aprobacion,
        esquema=esquema,
        participaciones=participaciones,
        puede_decidir=puede,
        form=aprobaciones_views._decision_form(aprobacion, es_prorroga=prorroga is not None) if puede else None,
        prorroga=prorroga,
        entregable_revisado=entregable,
        item=item,
    )
    if entregable is not None:
        accion["informacion"].append(f"Revisas el entregable «{entregable['nombre']}» ({entregable['tipo']}).")
    if item is not None:
        # UX-4: el aprobador ve la pieza que decide (valores y archivos de SU ítem; la descarga la autoriza
        # `edicion_por_tarea.puede_ver_elemento_por_intervencion`) y el recorrido de ese ítem.
        from apps.tickets.parrilla import archivos_de

        accion["subtitulo"] = nombre_de_item(item["elemento"])
        accion["item_archivos"] = archivos_de(item["elemento"])
        accion["recorrido_item"] = recorrido_de_item(item["elemento"])
        if item.get("gaceta") is not None:
            # UX-4b: en una gaceta se muestra solo lo que su configuración deja ver.
            pieza = pieza_de_gaceta(item["gaceta"], item["elemento"])
            item["valores"] = pieza["valores"]
            accion["item_archivos"] = pieza["archivos"]
    if prorroga is not None:
        accion["informacion"].append(
            f"Nueva fecha propuesta: {timezone.localtime(prorroga['prorroga'].nueva_fecha_solicitada):%d/%m/%Y}. "
            f"Motivo: {prorroga['prorroga'].motivo}"
        )
    accion["requisitos"].append({
        "texto": "Escribir una observación para rechazar, devolver o excluir", "cumplido": None,
    })
    if esquema.justificacion_al_aprobar:
        accion["requisitos"].append({"texto": "Justificar también al aprobar", "cumplido": None})
    if ejecucion is not None and ejecucion.bloque_operativo_id is not None:
        accion["despues"] = despues_de_bloque(ejecucion.bloque_operativo, esquema)
    elif item and item.get("gaceta"):
        accion["despues"] = [
            "Si apruebas, el ítem queda aprobado en la gaceta y su recorrido continúa.",
            "Si devuelves, el ítem pasa a corrección y vuelve a esta gaceta con una versión nueva.",
        ]
    elif prorroga is not None:
        accion["despues"] = ["Si apruebas, la fecha objetivo vigente del ticket pasa a la nueva fecha."]
    return accion


def pieza_de_gaceta(gaceta, elemento):
    """UX-4b — lo que se MUESTRA de un ítem en una gaceta: solo las columnas de `columnas_visibles` (configuración
    congelada en `GacetaTicket`; vacía = todas, como antes). Devuelve `valores` (texto, sin archivos), `fecha`
    (la primera columna de fecha visible, para el calendario de la tarjeta), `archivos` (de las columnas ARCHIVO
    visibles) y `portada` (el primero que se puede previsualizar). Solo presentación: quién ve el ítem lo decide
    `gacetas.autorizacion` y lo que se versiona no cambia."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.vista_previa import clase_de

    visibles = set((gaceta.configuracion or {}).get("columnas_visibles") or [])
    columnas = [c for c in parrilla_ops.columnas_de(elemento.parrilla) if not visibles or c.clave in visibles]
    textos = {f["clave"]: f["texto"] for f in parrilla_ops.tabla_de_elemento(elemento)}
    por_campo = {v.campo_id: v for v in elemento.valores.all()}
    valores, fecha = [], None
    for campo in columnas:
        if campo.tipo == "ARCHIVO":
            continue
        valor = por_campo.get(campo.pk)
        dia = None
        if valor is not None and campo.tipo == "FECHA":
            dia = valor.valor_fecha
        elif valor is not None and campo.tipo == "FECHA_HORA" and valor.valor_fecha_hora:
            dia = timezone.localtime(valor.valor_fecha_hora).date()
        if dia is not None and fecha is None:
            fecha = {"etiqueta": campo.etiqueta, "valor": dia}
        valores.append({"etiqueta": campo.etiqueta, "texto": textos.get(campo.clave, "—"), "clave": campo.clave})
    de_archivo = {c.pk for c in columnas if c.tipo == "ARCHIVO"}
    archivos = [a for a in parrilla_ops.archivos_de(elemento) if a.campo_elemento_id in de_archivo]
    portada = next((a for a in reversed(archivos) if clase_de(a.nombre_original) in ("imagen", "video")), None)
    return {"valores": valores, "fecha": fecha, "archivos": archivos, "portada": portada}


# Cómo se agrupan las tarjetas de una gaceta para filtrarlas (con muchas piezas).
_GRUPOS_GACETA = (
    ("pendiente", "Por revisar"), ("correccion", "En corrección"), ("aprobada", "Aprobadas"), ("cerrada", "Fuera"),
)


def galeria_gaceta(usuario, grupo):
    """UX-4b — la misión de una gaceta como GALERÍA: una tarjeta por pieza con su portada y solo los datos
    visibles, su estado en la gaceta, sus observaciones y, en la misma tarjeta, lo que la persona puede hacer con
    ella ahora: observar (`gacetas:observar`) o decidir su aprobación de la ronda (`aprobaciones:decidir`). Cada
    operación vuelve a autorizar; aquí solo se presenta."""
    from apps.gacetas import autorizacion as gaceta_aut
    from apps.gacetas.consultas import presentables
    from apps.gacetas.models import ParticipacionItemGaceta
    from apps.tickets.revision_piezas import salidas_de

    gaceta = grupo["gaceta"]
    coordina = gaceta_aut.puede_coordinar(usuario, gaceta)
    decisiones = {
        a["item"]["elemento"].pk: a for a in grupo["acciones"]
        if a["tipo"] == "aprobacion" and a.get("form") is not None and a.get("item")
    }
    Estado = ParticipacionItemGaceta.Estado
    tarjetas = []
    for participacion in (
        ParticipacionItemGaceta.objects.filter(gaceta=gaceta).select_related("elemento__parrilla")
        .prefetch_related("observaciones__autor").order_by("elemento__numero")
    ):
        participacion.gaceta = gaceta
        decision = decisiones.get(participacion.elemento_id)
        puede_observar = gaceta_aut.puede_observar(usuario, participacion)
        if decision is not None or puede_observar:
            situacion = "pendiente"
        elif participacion.estado == Estado.EN_CORRECCION:
            situacion = "correccion"
        elif participacion.estado == Estado.APROBADA:
            situacion = "aprobada"
        elif participacion.estado == Estado.EN_REVISION:
            situacion = "pendiente"
        else:
            situacion = "cerrada"
        observaciones = list(participacion.observaciones.all())
        tarjetas.append({
            "clave": f"g{gaceta.pk}-{participacion.elemento_id}",
            "participacion": participacion,
            "elemento": participacion.elemento,
            "titulo": f"{participacion.elemento.parrilla.nombre_elemento or 'Ítem'} #{participacion.elemento.numero}",
            "estado": participacion.estado,
            "estado_texto": participacion.get_estado_display(),
            "situacion": situacion,
            "observaciones": observaciones,
            "puede_observar": puede_observar,
            "decision": decision,
            # UX-5: en el visor, Aprobar · Corregir · Eliminar (lo que la ronda admite); sin anotación en una gaceta.
            "salidas": salidas_de(decision["form"]) if decision is not None else [],
            "anotable": False,
            "puede_corregir": gaceta_aut.puede_corregir(usuario, participacion),
            "puede_excluir": coordina and not participacion.es_final,
            **pieza_de_gaceta(gaceta, participacion.elemento),
        })
    conteo = {clave: sum(1 for t in tarjetas if t["situacion"] == clave) for clave, _ in _GRUPOS_GACETA}
    return {
        "gaceta": gaceta,
        "tarjetas": tarjetas,
        # UX-5: el menú «Coordinar» de la gaceta (sustituye a su página propia, ya retirada).
        "coordina": coordina,
        "presentables": presentables(gaceta) if coordina else [],
        "filtros": [{"clave": c, "texto": t, "cantidad": conteo[c]} for c, t in _GRUPOS_GACETA if conteo[c]],
        "conteo": conteo,
        "por_hacer": sum(1 for t in tarjetas if t["decision"] is not None or t["puede_observar"]),
    }


def _gaceta_de_accion(accion):
    if accion["tipo"] == "gaceta":
        return accion.get("gaceta")
    if accion["tipo"] == "aprobacion":
        return (accion.get("item") or {}).get("gaceta")
    return None


def gacetas_del_espacio(usuario, rel, acciones):
    """UX-4b — cada gaceta del ticket como ESPACIO DE COLABORACIÓN permanente: la ve todo el que la puede consultar
    (revisores, aprobadores, coordinador, quien consulta el ticket; `rel.gacetas`), con TODAS sus piezas en
    cualquier estado, haya o no algo pendiente para la persona. Lo que le toque hacer (observar, decidir su ronda)
    aparece en la tarjeta de esa pieza. No existe una gaceta que aún no tiene piezas ni se ha abierto."""
    galerias = []
    for info in rel.gacetas:
        gaceta = info["gaceta"]
        propias = [a for a in acciones if getattr(_gaceta_de_accion(a), "pk", None) == gaceta.pk]
        galeria = galeria_gaceta(usuario, {"gaceta": gaceta, "acciones": propias})
        if not galeria["tarjetas"] and gaceta.estado == gaceta.Estado.PENDIENTE:
            continue
        galeria["info"] = info
        galerias.append(galeria)
    return galerias


def _accion_gaceta(usuario, info):
    """Piezas de una gaceta que la persona puede observar ahora (revisora o aprobadora con el periodo abierto)."""
    from apps.gacetas import autorizacion as gaceta_aut
    from apps.gacetas.models import ParticipacionItemGaceta

    gaceta = info["gaceta"]
    piezas = []
    for participacion in ParticipacionItemGaceta.objects.filter(
        gaceta=gaceta, estado=ParticipacionItemGaceta.Estado.EN_REVISION
    ).select_related("elemento__parrilla").order_by("elemento__numero"):
        participacion.gaceta = gaceta
        if not gaceta_aut.puede_observar(usuario, participacion):
            continue
        rondas = list(participacion.rondas.select_related("version").order_by("numero"))
        pieza = pieza_de_gaceta(gaceta, participacion.elemento)
        piezas.append({
            "participacion": participacion,
            "elemento": participacion.elemento,
            "valores": pieza["valores"],
            "archivos": pieza["archivos"],
            "ronda": rondas[-1] if rondas else None,
            "observaciones": list(participacion.observaciones.select_related("autor", "respondida_por")),
        })
    if not piezas:
        return None
    accion = _accion(
        f"gaceta-{gaceta.pk}", "gaceta", f"Revisar la gaceta «{gaceta.nombre}»",
        subtitulo=f"{len(piezas)} pieza{'s' if len(piezas) != 1 else ''} en revisión",
        vence=gaceta.vence_revision_en,
        gaceta=gaceta,
        piezas=piezas,
        que="Revisa cada pieza presentada y registra una observación si algo debe corregirse.",
    )
    if gaceta.vence_revision_en:
        accion["informacion"].append(
            f"El periodo de revisión vence el {timezone.localtime(gaceta.vence_revision_en):%d/%m/%Y %H:%M}."
        )
    accion["despues"] = [
        "Una observación envía la pieza a corrección: su responsable recibe una tarea y, al reenviarla, vuelve a "
        "esta gaceta con una versión nueva.",
    ]
    if info["aprobador"]:
        accion["despues"].append("La decisión (aprobar, devolver, rechazar) se registra en tu aprobación de la pieza.")
    return accion


def _accion_conjunto(usuario, ticket, entregable):
    from apps.tickets import revision_elementos as revision_ops

    motivo = revision_ops.motivo_no_revisable(entregable)
    _, resumen = revision_ops.evaluar(entregable)
    if motivo is None and not (resumen.pendientes or resumen.devueltos):
        return None
    accion = _accion(
        f"revision-{entregable.pk}", "revision", f"Revisar «{entregable.nombre}»",
        subtitulo="Revisión de elementos", estado=f"{resumen.progreso} aprobados",
        que=entregable.descripcion,
        entregable=entregable, resumen=resumen,
        url=reverse("tickets:revision_conjunto", args=[ticket.pk, entregable.pk]),
    )
    if motivo:
        accion["impedimentos"].append(motivo)
    accion["requisitos"].append({"texto": "Escribir una observación al devolver un elemento", "cumplido": None})
    accion["despues"] = [
        "Cuando todos los elementos vigentes estén aprobados y completos, el entregable queda satisfecho y el flujo "
        "continúa solo.",
    ]
    return accion


def _acciones_responsable(usuario, ticket, rel, trabajo):
    """Lo que hace en el ticket mismo quien lo atiende: tomarlo, diligenciar datos de inicio, escribir el
    entregable que el flujo espera, corregir elementos, reintentar un recorrido detenido, atender y entregar un
    servicio sin flujo, resolver o entregar al terminar el flujo."""
    from apps.tickets import finalizacion

    acciones = []
    if ticket.estado == Ticket.Estado.RADICADO and (puede_tomar(usuario, ticket) or puede_iniciar_atencion(usuario, ticket)):
        accion = _accion(
            "tomar", "tomar", "Tomar el ticket" if puede_tomar(usuario, ticket) else "Iniciar la atención",
            puede_tomar=puede_tomar(usuario, ticket), puede_iniciar=puede_iniciar_atencion(usuario, ticket),
            que="Este ticket está radicado y puedes asumir su atención.",
        )
        accion["despues"] = ["Quedarás como responsable y el ticket pasará a «En atención»."]
        acciones.append(accion)

    datos = trabajo_ops.datos_inicio_del_ticket(usuario, ticket)
    if datos is not None and not datos["completo"] and datos["puede_diligenciar"]:
        accion = _accion(
            "datos-inicio", "datos_inicio", "Completar los datos de inicio", datos_inicio=datos,
            que="Este proceso se diligencia durante la ejecución: guarda avance o completa los datos.",
        )
        for pendiente in datos["pendientes"]:
            accion["requisitos"].append({"texto": pendiente, "cumplido": False})
        acciones.append(accion)

    if not rel.responsable_individual or ticket.estado != Ticket.Estado.EN_ATENCION:
        return acciones

    actual = trabajo["trabajo_actual"]
    if actual is not None and actual.get("entregable") is not None and not actual["terminado"]:
        entregable = actual["entregable"]
        entregable.archivos_vigentes = [a for a in entregable.archivos.all() if a.retirado_en is None]
        entregable.operable_ahora = trabajo_ops.entregable_operable_ahora(ticket, entregable)
        if puede_escribir_entregables_finales(usuario, ticket) and entregable.operable_ahora:
            accion = _accion(
                f"entregable-{entregable.pk}", "entregable", f"Completar «{entregable.nombre}»",
                subtitulo=actual["nombre"], que=entregable.descripcion or actual["descripcion"], entregable=entregable,
            )
            accion["requisitos"].append({"texto": f"«{entregable.nombre}» completo", "cumplido": entregable.satisfecho})
            accion["despues"] = ["Cuando quede completo, el flujo continúa solo."]
            acciones.append(accion)
    if actual is not None and actual.get("correccion"):
        accion = _accion(
            "correccion", "correccion", "Corregir los elementos", subtitulo=actual["nombre"],
            correccion=actual["correccion"], puede_reintentar=puede_editar_parrilla(usuario, ticket),
            que="Falta información para generar las tareas por elemento. Corrige lo indicado y reintenta.",
        )
        accion["impedimentos"] = list(actual["correccion"].get("mensajes", []))
        accion["despues"] = ["Al reintentar con la información completa se generan las tareas y el flujo sigue."]
        acciones.append(accion)

    for contenedor in trabajo_ops.recorridos_del_ticket(usuario, ticket):
        for fila in contenedor["filas"]:
            if fila["puede_reintentar"]:
                accion = _accion(
                    f"recorrido-{fila['instancia'].pk}", "recorrido", f"Reintentar el recorrido de {fila['etiqueta']}",
                    subtitulo=fila["paso"], fila=fila,
                    que="Este recorrido se detuvo por un dato faltante. Corrige el ítem y reintenta.",
                )
                if fila["error"]:
                    accion["impedimentos"].append(fila["error"])
                acciones.append(accion)

    sin_flujo = ticket.instancia_workflow_id is None
    flujo_terminado = actual is not None and actual["terminado"]
    if sin_flujo or flujo_terminado:
        entregables = trabajo_ops.entregables_anotados(ticket)
        pendientes = [e for e in entregables if e.obligatorio and not e.satisfecho]
        accion = _accion(
            "atencion" if sin_flujo else "cierre", "atencion",
            "Atender y entregar" if sin_flujo else "Resolver o entregar",
            subtitulo="Atención directa" if sin_flujo else "El flujo de trabajo terminó",
            vence=ticket.fecha_objetivo_vigente,
            entregables=entregables,
            puede_entregar=puede_entregar_ticket(usuario, ticket),
            puede_resolver=puede_resolver_ticket(usuario, ticket),
            puede_solicitar_informacion=puede_solicitar_informacion(usuario, ticket),
            entrega_politica=ticket.entrega_politica,
            que=(
                "Prepara los resultados y entrégalos al solicitante." if ticket.entrega_politica and ticket.solicitante_id
                else "Registra la resolución cuando el trabajo esté terminado."
            ),
        )
        for entregable in entregables:
            if entregable.obligatorio:
                accion["requisitos"].append({"texto": f"«{entregable.nombre}» completo", "cumplido": entregable.satisfecho})
        if ticket.tipo == Ticket.Tipo.PROCESO:
            for regla in finalizacion.evaluar(ticket):
                accion["requisitos"].append({
                    "texto": regla.descripcion + ("" if regla.cumple else f" — {regla.detalle}"), "cumplido": regla.cumple,
                })
        if pendientes and accion["puede_entregar"]:
            accion["impedimentos"].append("Faltan entregables obligatorios para poder entregar.")
        if ticket.entrega_politica and ticket.solicitante_id:
            accion["despues"] = [
                "Al entregar, el solicitante podrá aceptar (el ticket se cierra) u observar (vuelve a atención)."
                if ticket.entrega_politica != "CIERRE_DIRECTO" else "Al entregar, el ticket se cierra sin esperar respuesta."
            ]
        else:
            accion["despues"] = ["Al resolver, el ticket queda «Resuelto» y luego se cierra."]
        acciones.append(accion)
    return acciones


def _acciones_solicitante(usuario, ticket):
    acciones = []
    for solicitud in ticket.solicitudes_informacion.filter(
        estado=SolicitudInformacion.Estado.PENDIENTE, destinatario=usuario
    ).select_related("solicitada_por").prefetch_related("adjuntos"):
        accion = _accion(
            f"solicitud-{solicitud.pk}", "solicitud", "Responder la solicitud de información",
            subtitulo=f"De {_nombre(solicitud.solicitada_por)}", que=solicitud.mensaje, solicitud=solicitud,
        )
        accion["despues"] = ["Tu respuesta queda en el ticket y la recibe quien lo atiende."]
        acciones.append(accion)
    entrega = ticket.entregas.filter(estado=EntregaTicket.Estado.PENDIENTE).prefetch_related(
        "resultados__adjuntos"
    ).order_by("-numero").first()
    if entrega is not None:
        entrega.ticket = ticket
        if puede_responder_entrega(usuario, entrega):
            accion = _accion(
                "entrega", "entrega", "Revisar la entrega", subtitulo=f"Entrega {entrega.numero}",
                vence=entrega.vence_en, entrega=entrega,
                que="Revisa los resultados entregados y dinos si todo está correcto o qué debemos ajustar.",
            )
            if entrega.vence_en:
                accion["informacion"].append(
                    f"Si no respondes antes del {timezone.localtime(entrega.vence_en):%d/%m/%Y %H:%M}, "
                    "el ticket se cerrará automáticamente."
                )
            accion["requisitos"].append({"texto": "Escribir qué ajustar si tienes observaciones", "cumplido": None})
            accion["despues"] = [
                "Si todo está correcto, el ticket queda cerrado.",
                "Si tienes observaciones, vuelve a atención para los ajustes.",
            ]
            acciones.append(accion)
    return acciones


def acciones_de(usuario, ticket, rel, trabajo):
    """Todas las acciones que `usuario` puede realizar ahora en el ticket, en un orden estable: lo propio del
    ticket (tomar, atender, entregar), luego tareas, aprobaciones, gacetas y revisiones, y lo del solicitante."""
    from apps.tareas import autorizacion as tarea_aut

    acciones = []
    acciones.extend(_acciones_responsable(usuario, ticket, rel, trabajo))
    if ticket.estado == Ticket.Estado.EN_ATENCION:
        for info in rel.colecciones:
            accion = _accion_borradores(usuario, ticket, info)
            if accion is not None:
                acciones.append(accion)
    for tarea in rel.tareas:
        if tarea.estado in Tarea.ESTADOS_CERRADOS:
            continue
        if not (tarea_aut.es_responsable_actual(usuario, tarea) or tarea_aut.puede_tomar_tarea(usuario, tarea)):
            continue
        acciones.append(_accion_tarea(usuario, ticket, tarea, rel.origen_tareas.get(tarea.pk)))
    from apps.aprobaciones.autorizacion import puede_aprobar

    for aprobacion in rel.aprobaciones:
        # Una aprobación secuencial que aún no le toca no es una acción (en la bandeja, «en seguimiento»).
        if puede_aprobar(usuario, aprobacion):
            acciones.append(_accion_aprobacion(usuario, ticket, aprobacion, rel.origen_aprobaciones.get(aprobacion.pk)))
    for info in rel.gacetas:
        if (info["revisor"] or info["aprobador"]) and info["gaceta"].periodo_abierto:
            accion = _accion_gaceta(usuario, info)
            if accion is not None:
                acciones.append(accion)
    for entregable in rel.conjuntos:
        accion = _accion_conjunto(usuario, ticket, entregable)
        if accion is not None:
            acciones.append(accion)
    acciones.extend(_acciones_solicitante(usuario, ticket))
    if ticket.estado == Ticket.Estado.RESUELTO and puede_cerrar_ticket(usuario, ticket):
        accion = _accion("cerrar", "cerrar", "Cerrar el ticket", que="El ticket está resuelto. Ciérralo si ya no requiere nada más.")
        accion["despues"] = ["El ticket queda «Cerrado»."]
        acciones.append(accion)
    return acciones


def espera_visible(usuario, ticket, rel, trabajo):
    """Sin acciones: por qué está esperando el ticket, SOLO si la persona puede consultarlo (si no, no se dice)."""
    if not rel.consulta:
        return ""
    if ticket.estado in (Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO):
        return f"El ticket está {ticket.get_estado_display().lower()}."
    if ticket.estado == Ticket.Estado.RESUELTO:
        return "El ticket está resuelto."
    responsable = seguimiento.responsable_visible(ticket)
    quien = responsable["persona"] or responsable["equipo"]
    if ticket.estado == Ticket.Estado.RADICADO:
        return f"Está radicado y espera que {quien} inicie la atención." if quien else "Está radicado y espera que alguien lo tome."
    actual = trabajo["trabajo_actual"]
    if actual is None:
        return f"Lo atiende {quien}." if quien else "Está en atención."
    if actual["con_error"]:
        return "El flujo se detuvo y requiere revisión de quien lo atiende."
    partes = [f"Va en «{actual['nombre']}»" + (f" ({actual['fase']})" if actual["fase"] else "") + "."]
    if actual["pendiente"]:
        partes.append(actual["pendiente"])
    if actual.get("tarea_vinculada") is not None:
        nombre = _responsable_de_tarea(actual["tarea_vinculada"])
        if nombre:
            partes.append(f"La actividad la tiene {nombre}.")
    elif actual.get("participaciones"):
        nombres = trabajo_ops._responsable_de(participaciones=actual["participaciones"])
        if nombres:
            partes.append(f"Espera la decisión de {nombres}.")
    return " ".join(partes)


# --- Proceso (progreso del flujo) --------------------------------------------------------------------------


def proceso(usuario, ticket):
    """Recorrido por fases para quien consulta el ticket: completadas, actual, previstas y las devoluciones
    registradas (rutas DEVUELTA tomadas y fases que se volvieron a recorrer). `None` sin flujo por fases."""
    datos = seguimiento.progreso(ticket)
    if datos is None:
        return None
    from apps.workflows.models import InstanciaWorkflow

    instancia = ticket.instancia_workflow
    ejecuciones = list(
        instancia.ejecuciones_etapa.select_related("bloque_operativo", "fase_workflow", "transicion_bloque_tomada__bloque_destino")
        .order_by("orden")
    )
    visitas = {}
    anterior = None
    for ejecucion in ejecuciones:
        if ejecucion.fase_workflow_id and ejecucion.fase_workflow_id != anterior:
            visitas[ejecucion.fase_workflow_id] = visitas.get(ejecucion.fase_workflow_id, 0) + 1
            anterior = ejecucion.fase_workflow_id
    for fila in datos["fases"]:
        fila["visitas"] = visitas.get(fila["fase"].pk, 0)
    devoluciones = [
        {
            "bloque": e.bloque_operativo.nombre,
            "destino": e.transicion_bloque_tomada.bloque_destino.nombre if e.transicion_bloque_tomada.bloque_destino_id else "",
            "fecha": e.finalizada_en,
        }
        for e in ejecuciones
        if e.transicion_bloque_tomada_id and e.transicion_bloque_tomada.resultado_aprobacion == "DEVUELTA"
        and e.bloque_operativo_id
    ]
    ultima = ejecuciones[-1] if ejecuciones else None
    fase = ultima.fase_workflow if ultima is not None else None
    completadas = sum(1 for f in datos["fases"] if f["situacion"] in (seguimiento.COMPLETADA, seguimiento.NO_APLICO))
    return {
        **datos,
        "bloques": trabajo_ops._bloques_de_la_fase(instancia, ejecuciones, fase),
        "fase_actual": fase,
        "devoluciones": devoluciones,
        "recorridos": trabajo_ops.recorridos_del_ticket(usuario, ticket),
        "con_error": instancia.estado == InstanciaWorkflow.Estado.ERROR,
        "completadas": completadas,
        "total": len(datos["fases"]),
    }


def ruta_de_estados(ticket):
    """UX-4 — recorrido de un ticket SIN flujo por fases (p. ej. un servicio de atención directa): sus estados de
    siempre como etapas (radicado → en atención → resuelto → cerrado). Un ticket cancelado lo dice."""
    orden = [Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION, Ticket.Estado.RESUELTO, Ticket.Estado.CERRADO]
    etiquetas = dict(Ticket.Estado.choices)
    if ticket.estado == Ticket.Estado.CANCELADO:
        return [{"nombre": etiquetas[Ticket.Estado.CANCELADO], "situacion": "actual"}]
    actual = orden.index(ticket.estado) if ticket.estado in orden else 0
    return [
        {
            "nombre": etiquetas[estado],
            "situacion": "completada" if i < actual or (i == actual and estado == Ticket.Estado.CERRADO)
            else "actual" if i == actual else "pendiente",
        }
        for i, estado in enumerate(orden)
    ]


def resumen_progreso(ticket):
    datos = seguimiento.progreso(ticket)
    if datos is None:
        return None
    completadas = sum(1 for f in datos["fases"] if f["situacion"] in (seguimiento.COMPLETADA, seguimiento.NO_APLICO))
    return {"actual": datos["actual"], "completadas": completadas, "total": len(datos["fases"]), "terminado": datos["terminado"]}


# --- Elementos -------------------------------------------------------------------------------------------


def _fecha_relevante(celdas):
    for celda in celdas:
        if celda["tipo"] in ("FECHA", "FECHA_HORA") and celda.get("texto"):
            return celda["texto"]
    return ""


def _etiqueta_de(celdas):
    for celda in celdas:
        if celda["tipo"] in ("TEXTO", "TEXTO_LARGO") and celda.get("texto"):
            texto = celda["texto"]
            return texto if len(texto) <= 60 else texto[:57] + "…"
    return ""


def elementos(usuario, ticket, rel):
    """Una tabla compacta por colección que la persona puede consultar: ítem, fecha relevante, estado real de su
    recorrido, quién lo tiene (si consulta el ticket), pendientes y acceso a su ficha. No mezcla estados: cada
    fila lee SU recorrido (`elemento.recorridos`) y SUS participaciones de gaceta."""
    from apps.gacetas.models import ParticipacionItemGaceta
    from apps.tickets import parrilla as parrilla_ops

    resultado = []
    puede_reintentar = puede_editar_parrilla(usuario, ticket)
    for info in rel.colecciones:
        tabla = parrilla_ops.tabla(ticket, usuario, parrilla=info["parrilla"])
        if tabla is None:
            continue
        ids = [f["elemento"].pk for f in tabla["filas"]]
        gacetas = {}
        for participacion in ParticipacionItemGaceta.objects.filter(elemento_id__in=ids).exclude(
            gaceta__estado="CANCELADA"
        ).select_related("gaceta"):
            gacetas.setdefault(participacion.elemento_id, []).append(participacion)
        filas = []
        for fila in tabla["filas"]:
            elemento = fila["elemento"]
            hija = elemento.recorridos.order_by("-pk").first()
            recorrido = trabajo_ops._fila_de_recorrido(usuario, hija, puede_reintentar) if hija is not None else None
            pendientes = []
            if not fila["completitud"].completo:
                pendientes.append("Incompleto")
            for participacion in gacetas.get(elemento.pk, []):
                if participacion.estado in participacion.ESTADOS_PENDIENTES:
                    pendientes.append(f"{participacion.get_estado_display()} en «{participacion.gaceta.nombre}»")
            for revision in fila.get("revisiones", []):
                if revision["estado"] == "DEVUELTA":
                    pendientes.append("Devuelto en revisión")
            item = recorrido_de_item(elemento) if hija is not None else None
            filas.append({
                "elemento": elemento,
                "nombre": nombre_de_item(elemento),
                "etiqueta": _etiqueta_de(fila["celdas"]),
                "fecha": _fecha_relevante(fila["celdas"]),
                "recorrido": recorrido,
                "item": item,
                "borrador": hija is None and fila["salida"] is None and elemento.retirado_en is None,
                "salida": fila["salida"],
                "pendientes": pendientes,
                "responsable": recorrido["responsable"] if recorrido and rel.consulta else "",
            })
        from apps.workflows import recorridos

        en_espera = recorridos.recorrido_al_enviar(ticket, info["parrilla"])
        contenedor = next((f["item"]["contenedor"] for f in filas if f["item"]), None) or (
            en_espera.bloque_operativo if en_espera is not None else None
        )
        resultado.append({
            **info, "tabla": tabla, "filas": filas,
            "etapas": _etapas_de(filas, _pasos_visibles(contenedor)) if contenedor is not None else None,
            "etapa_borradores": recorridos.configuracion_de(contenedor).etapa_borradores if contenedor is not None else "",
            # UX-4: los borradores de esta colección se envían a su recorrido desde la fila.
            "enviar": info["opera"] and en_espera is not None,
        })
    return resultado


def _pasos_visibles(contenedor):
    """Nombres de los pasos del ítem que se dibujan (desde el paso por el que entra un ítem enviado)."""
    from apps.workflows import recorridos

    secuencia = recorridos.bloques_de_rama(contenedor, "ITEM")
    paso = recorridos.configuracion_de(contenedor).paso_al_enviar
    inicio = next((i for i, b in enumerate(secuencia) if b.clave == paso), 0)
    return [b.nombre for b in secuencia[inicio:]]


def _etapas_de(filas, nombres=None):
    """UX-4 — el recorrido de la colección como etapas con su conteo (cuántos ítems hay en cada paso), para el
    recorrido gráfico: borradores · cada paso del ítem · terminados. Sale de las filas ya leídas."""
    if nombres is None:
        pasos = next((f["item"]["pasos"] for f in filas if f["item"]), None)
        if pasos is None:
            return None
        nombres = [p["nombre"] for p in pasos]
    etapas = [{"nombre": "Borradores", "cantidad": sum(1 for f in filas if f["borrador"]), "devueltos": 0}]
    etapas += [{"nombre": nombre, "cantidad": 0, "devueltos": 0} for nombre in nombres]
    terminados = {"nombre": "Terminadas", "cantidad": 0, "devueltos": 0}
    por_nombre = {e["nombre"]: e for e in etapas[1:]}
    for fila in filas:
        item = fila["item"]
        if item is None:
            continue
        if item["terminado"]:
            terminados["cantidad"] += 1
            continue
        actual = next((p for p in item["pasos"] if p["situacion"] in ("actual", "devuelto", "detenido")), None)
        if actual is not None and actual["nombre"] in por_nombre:
            por_nombre[actual["nombre"]]["cantidad"] += 1
            if actual["situacion"] == "devuelto":
                por_nombre[actual["nombre"]]["devueltos"] += 1
    return [*etapas, terminados]


SITUACION_EN_ETAPA = {
    "completado": ("Superó esta etapa", "resuelto"),
    "actual": ("En esta etapa", "en_atencion"),
    "devuelto": ("Devuelto en esta etapa", "radicado"),
    "detenido": ("Detenido en esta etapa", "cancelado"),
}


def vista_etapa(colecciones, clave):
    """UX-4c — una etapa del recorrido de los ítems, para CONSULTA (solo lectura): los ítems que están en ella o ya
    pasaron por ella, con su situación en esa etapa y sus datos tal como están. Sirve para volver a etapas ya
    aprobadas o enviadas sin poder cambiarlas: aquí no hay ningún formulario y lo editable sigue en la misión. Sale
    de las filas que ya leyó `elementos` (la colección que la persona consulta); `None` si `clave` no es una etapa
    de ese recorrido o nadie ha llegado a ella."""
    from apps.tickets import parrilla as parrilla_ops

    coleccion = next((c for c in colecciones or [] if c.get("etapas")), None)
    if coleccion is None or not clave:
        return None
    nombres = [e["nombre"] for e in coleccion["etapas"][:-1]]
    if clave not in nombres:
        return None
    borradores = clave == nombres[0]
    items = []
    for fila in coleccion["filas"]:
        elemento = fila["elemento"]
        if elemento.retirado_en is not None:
            continue
        if borradores:
            if fila["salida"] is not None:
                texto, tono = fila["salida"].get_tipo_display(), "cancelado"
            elif fila["borrador"]:
                texto, tono = "Borrador", "borrador"
            else:
                texto, tono = "Enviado", "resuelto"
        else:
            paso = next((p for p in (fila["item"] or {}).get("pasos", []) if p["nombre"] == clave), None)
            if paso is None or paso["situacion"] not in SITUACION_EN_ETAPA:
                continue
            texto, tono = SITUACION_EN_ETAPA[paso["situacion"]]
        items.append({
            "elemento": elemento,
            "nombre": fila["nombre"],
            "fecha": fila["fecha"],
            "situacion": texto,
            "tono": tono,
            "datos": parrilla_ops.tabla_de_elemento(elemento),
        })
    nombre = (coleccion.get("etapa_borradores") or clave) if borradores else clave
    return {
        "clave": clave,
        "nombre": nombre,
        "nombre_elemento": coleccion["tabla"].get("nombre_elemento") or "Ítems",
        "items": items,
    }


def puede_ver_ficha(usuario, ticket, elemento):
    """La misma población que ya consulta un ítem en el dominio: quien consulta su colección, quien lo trabaja
    por una tarea abierta (3.1) o revisa/aprueba una gaceta en la que participa (Fase 3)."""
    from apps.gacetas.autorizacion import puede_ver_elemento
    from apps.tickets.edicion_por_tarea import puede_editar_por_tarea, puede_ver_elemento_por_intervencion

    return (
        puede_consultar_coleccion(usuario, elemento.parrilla, ticket)
        or puede_editar_por_tarea(usuario, elemento, ticket)
        or puede_ver_elemento(usuario, elemento)
        or puede_ver_elemento_por_intervencion(usuario, elemento)  # UX-4: su aprobador o quien hace su actividad
    )


def ficha(usuario, ticket, elemento):
    """La ficha de UN ítem: datos (editables según quién es), archivos, su recorrido individual con actividades y
    decisiones, sus gacetas (rondas, observaciones), versiones y salida del alcance. El recorrido y sus actores
    solo se muestran a quien consulta la colección; quien solo trabaja o revisa el ítem ve sus datos, archivos,
    versiones y observaciones."""
    from apps.gacetas.models import ParticipacionItemGaceta
    from apps.tickets import parrilla as parrilla_ops
    from apps.workflows import recorridos as recorridos_ops
    from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa, TareaWorkflow

    parrilla = elemento.parrilla
    consulta_coleccion = puede_consultar_coleccion(usuario, parrilla, ticket)
    opera = puede_operar_coleccion(usuario, parrilla, ticket) and elemento.retirado_en is None
    celdas = None
    if opera:
        tabla = parrilla_ops.tabla(ticket, usuario, parrilla=parrilla)
        fila = next((f for f in tabla["filas"] if f["elemento"].pk == elemento.pk), None)
        celdas = [{**c, "editable": True} for c in fila["celdas"]] if fila else None
    por_tarea = None if opera else parrilla_ops.ficha_por_tarea(elemento, usuario)
    if por_tarea is not None:
        celdas = por_tarea["celdas"]
    datos = {
        "elemento": elemento,
        "nombre_elemento": parrilla.nombre_elemento or "Ítem",
        "celdas": celdas,
        "celdas_valor": [c for c in celdas or [] if c.get("editable") and not c["es_archivo"]],
        "celdas_archivo": [c for c in celdas or [] if c.get("editable") and c["es_archivo"]],
        "valores": parrilla_ops.tabla_de_elemento(elemento) if celdas is None else None,
        "archivos": parrilla_ops.archivos_de(elemento),
        "opera": opera,
        "edita_por_tarea": por_tarea is not None,
        "puede_versionar": opera or por_tarea is not None,
        "versiones": parrilla_ops.versiones_de(elemento),
        "salida": parrilla_ops.decision_de_salida(elemento),
        "consulta_coleccion": consulta_coleccion,
        "pasos": [],
        "actividades": [],
        "recorrido": None,
        "gacetas": [],
    }
    from apps.tickets.edicion_por_tarea import puede_editar_por_tarea

    datos["ver_versiones"] = consulta_coleccion or puede_editar_por_tarea(usuario, elemento, ticket)

    from apps.gacetas import autorizacion as gaceta_aut

    for participacion in ParticipacionItemGaceta.objects.filter(elemento=elemento).exclude(
        gaceta__estado="CANCELADA"
    ).select_related("gaceta").order_by("pk"):
        if not gaceta_aut.puede_consultar_gaceta(usuario, participacion.gaceta):
            continue
        participacion.gaceta.ticket = ticket
        datos["gacetas"].append({
            "participacion": participacion,
            "rondas": list(participacion.rondas.select_related("version", "tarea_correccion").order_by("numero")),
            "observaciones": list(participacion.observaciones.select_related("autor", "respondida_por")),
            "puede_observar": gaceta_aut.puede_observar(usuario, participacion),
            "puede_corregir": gaceta_aut.puede_corregir(usuario, participacion),
        })

    if not consulta_coleccion:
        return datos
    hija = elemento.recorridos.order_by("-pk").first()
    if hija is None:
        return datos
    datos["recorrido"] = trabajo_ops._fila_de_recorrido(usuario, hija, puede_editar_parrilla(usuario, ticket))
    ejecuciones = list(hija.ejecuciones_etapa.select_related("bloque_operativo", "transicion_bloque_tomada").order_by("orden"))
    vistos = set()
    for ejecucion in ejecuciones:
        if ejecucion.bloque_operativo_id is None:
            continue
        vistos.add(ejecucion.bloque_operativo_id)
        resultado = ""
        if ejecucion.transicion_bloque_tomada_id and ejecucion.transicion_bloque_tomada.resultado_aprobacion:
            resultado = ejecucion.transicion_bloque_tomada.get_resultado_aprobacion_display()
        datos["pasos"].append({
            "nombre": ejecucion.bloque_operativo.nombre,
            "tipo": trabajo_ops.TIPOS_BLOQUE.get(ejecucion.bloque_operativo.tipo, ""),
            "situacion": (
                "completado" if ejecucion.estado == InstanciaEtapa.Estado.COMPLETADA
                else ("con_error" if ejecucion.estado == InstanciaEtapa.Estado.ERROR else "actual")
            ),
            "resultado": resultado,
        })
    if hija.estado in recorridos_ops.ESTADOS_EN_CURSO and hija.ejecucion_padre_id:
        contenedor = hija.ejecucion_padre.bloque_operativo
        if contenedor is not None:
            for bloque in recorridos_ops.bloques_de_rama(contenedor, "ITEM"):
                if bloque.pk not in vistos:
                    datos["pasos"].append({
                        "nombre": bloque.nombre, "tipo": trabajo_ops.TIPOS_BLOQUE.get(bloque.tipo, ""),
                        "situacion": "pendiente", "resultado": "",
                    })
    from apps.aprobaciones.autorizacion import puede_consultar_aprobacion
    from apps.tareas.autorizacion import puede_consultar_tarea

    for vinculo in TareaWorkflow.objects.filter(instancia_etapa__instancia_workflow=hija).select_related(
        "tarea__usuario_responsable", "tarea__equipo_responsable", "instancia_etapa__bloque_operativo"
    ).order_by("instancia_etapa__orden"):
        tarea = vinculo.tarea
        datos["actividades"].append({
            "tipo": "Actividad", "nombre": tarea.titulo, "estado": tarea.get_estado_display(),
            "quien": _responsable_de_tarea(tarea), "fecha": tarea.completada_en or tarea.fecha_limite,
            "url": reverse("tareas:detalle", args=[tarea.pk]) if puede_consultar_tarea(usuario, tarea) else "",
        })
    for vinculo in EsquemaAprobacionWorkflow.objects.filter(instancia_etapa__instancia_workflow=hija).select_related(
        "esquema", "instancia_etapa__bloque_operativo"
    ).order_by("instancia_etapa__orden"):
        for aprobacion in vinculo.esquema.participaciones.select_related("aprobador_usuario", "aprobador_equipo", "decidido_por"):
            datos["actividades"].append({
                "tipo": "Aprobación", "nombre": vinculo.instancia_etapa.bloque_operativo.nombre if vinculo.instancia_etapa.bloque_operativo_id else "Aprobación",
                "estado": aprobacion.get_estado_display(),
                "quien": _nombre(aprobacion.decidido_por) if aprobacion.decidido_por_id else (
                    _nombre(aprobacion.aprobador_usuario) if aprobacion.aprobador_usuario_id
                    else (aprobacion.aprobador_equipo.nombre if aprobacion.aprobador_equipo_id else "")
                ),
                "observacion": aprobacion.observacion,
                "fecha": aprobacion.decidida_en,
                "url": reverse("aprobaciones:detalle", args=[aprobacion.pk]) if puede_consultar_aprobacion(usuario, aprobacion) else "",
            })
    datos["actividades"].sort(key=lambda a: (a["fecha"] is None, a["fecha"] or timezone.now()))
    return datos


# --- Contexto lateral ------------------------------------------------------------------------------------


def contexto_lateral(ticket, rel):
    if not rel.consulta:
        return None
    from apps.tickets import plazos

    return {
        "responsable": seguimiento.responsable_visible(ticket),
        "solicitante": _nombre(ticket.solicitante) if ticket.solicitante_id else "",
        "fecha_solicitada": plazos.fecha_solicitada(ticket),
        "progreso": resumen_progreso(ticket),
        "compromiso_ampliado": ticket.fecha_objetivo_original is not None
        and ticket.fecha_objetivo_vigente != ticket.fecha_objetivo_original,
    }


def pestanas_de(ticket, rel, colecciones, hay_proceso):
    """Solo las pestañas que tienen algo para ESTA persona y ESTE ticket (nunca una pestaña vacía)."""
    visibles = {"actividad"}
    if colecciones:
        visibles.add("elementos")
    if hay_proceso:
        visibles.add("proceso")
    if rel.consulta:
        visibles.add("informacion")
    return [(clave, etiqueta) for clave, etiqueta in PESTANAS if clave in visibles]


def acciones_generales(usuario, ticket, rel):
    """Acciones del encabezado (las de siempre, solo si su `puede_*` las permite)."""
    return {
        "puede_cancelar": puede_cancelar_ticket(usuario, ticket),
        "puede_reabrir": puede_reabrir_ticket(usuario, ticket),
        "puede_comentar": puede_comentar_ticket(usuario, ticket),
        "ver_detalle": rel.consulta,
    }


# --- UX-4b: tablero guiado (misiones agrupadas, recorrido por etapas, tablas de trabajo) -----------------------


def plural(nombre):
    """Plural español aproximado de un nombre de ítem configurado («Publicación» → «publicaciones»)."""
    nombre = (nombre or "ítem").strip().lower()
    if nombre.endswith("ón"):
        return nombre[:-2] + "ones"
    if nombre[-1:] in "aeiouáéíóú":
        return nombre + "s"
    return nombre + "es"


def _bloque_de_accion(accion):
    if accion["tipo"] == "tarea" and accion.get("tarea") is not None:
        ejecucion = _bloque_de_tarea(accion["tarea"])
        return ejecucion.bloque_operativo if ejecucion is not None else None
    if accion["tipo"] == "aprobacion" and accion.get("aprobacion") is not None:
        ejecucion = _ejecucion_de_aprobacion(accion["aprobacion"])
        return ejecucion.bloque_operativo if ejecucion is not None else None
    return None


def misiones(acciones):
    """Agrupa las acciones de la persona en MISIONES: las del mismo paso sobre ítems distintos se hacen juntas en
    una tabla (revisar N ideas, elaborar N piezas); la preparación de borradores es su propia misión; lo demás
    (publicación con lista de verificación, atención de un servicio…) es una misión por acción. Una GACETA es una
    sola misión con sus observaciones y las decisiones de sus rondas (`galeria_gaceta`)."""
    grupos, por_clave = [], {}
    for accion in acciones:
        bloque = _bloque_de_accion(accion)
        elemento = accion.get("elemento") or (accion.get("item") or {}).get("elemento")
        gaceta = _gaceta_de_accion(accion)
        if accion["tipo"] == "borradores":
            clave, tipo = accion["clave"], "lluvia"
        elif gaceta is not None:
            clave, tipo = f"gaceta-{gaceta.pk}", "gaceta"
        elif bloque is not None and elemento is not None and accion["tipo"] == "aprobacion" and accion.get("form"):
            clave, tipo = f"aprobar-{bloque.clave}", "revision"
        elif bloque is not None and elemento is not None and accion["tipo"] == "tarea" and accion.get("ficha") is not None \
                and not accion.get("verificaciones") and not (accion["tarea"].requiere_evidencia):
            clave, tipo = f"elaborar-{bloque.clave}", "items"
        else:
            clave, tipo = accion["clave"], "accion"
        if clave not in por_clave:
            grupo = {"clave": clave, "tipo": tipo, "bloque": bloque, "acciones": [], "titulo": accion["titulo"]}
            if tipo in ("revision", "items"):
                grupo["titulo"] = bloque.nombre
                grupo["descripcion"] = bloque.descripcion
            elif tipo == "gaceta":
                grupo["titulo"] = gaceta.nombre
                grupo["gaceta"] = gaceta
            por_clave[clave] = grupo
            grupos.append(grupo)
        por_clave[clave]["acciones"].append(accion)
    for grupo in grupos:
        grupo["cantidad"] = len(grupo["acciones"])
        primero = grupo["acciones"][0]
        elemento = primero.get("elemento") or (primero.get("item") or {}).get("elemento")
        nombre = elemento.parrilla.nombre_elemento if elemento is not None else ""
        grupo["nombre_item"] = (nombre or "ítem").lower()
        grupo["plural"] = plural(nombre) if grupo["cantidad"] != 1 else grupo["nombre_item"]
        if grupo["tipo"] == "lluvia":
            grupo["accion"] = grupo["acciones"][0]
        elif grupo["tipo"] == "accion":
            grupo["accion"] = grupo["acciones"][0]
    return grupos


def observacion_de_devolucion(elemento):
    """La última observación con la que una aprobación del recorrido de `elemento` lo devolvió (o "")."""
    from apps.aprobaciones.models import Aprobacion

    devolucion = (
        Aprobacion.objects.filter(
            esquema__vinculo_workflow__instancia_etapa__instancia_workflow__elemento=elemento,
            estado=Aprobacion.Estado.DEVUELTA,
        ).order_by("-decidida_en").first()
    )
    return devolucion.observacion if devolucion is not None else ""


def lluvia(usuario, ticket, accion):
    """La tabla editable de la etapa de borradores: columnas (sin archivos), una fila por borrador con sus celdas y
    lo que le falta para enviarse, y la fila vacía para agregar. Solo presenta: guardar y enviar son operaciones."""
    from apps.workflows import recorridos

    parrilla, tabla = accion["parrilla"], accion["tabla"]
    ejecucion = recorridos.recorrido_al_enviar(ticket, parrilla)
    configuracion = recorridos.configuracion_de(ejecucion.bloque_operativo) if ejecucion is not None else None
    faltan = {b["elemento"].pk: b["faltan"] for b in accion["borradores"]}
    filas = [
        {
            "elemento": f["elemento"], "faltan": faltan[f["elemento"].pk],
            "celdas": [
                {**c, "nombre": f"e{f['elemento'].pk}__campo_{c['campo'].pk}"} for c in f["celdas"] if not c["es_archivo"]
            ],
        }
        for f in tabla["filas"] if f["elemento"].pk in faltan
    ]
    exigidas = set(configuracion.columnas_requeridas_ingreso) if configuracion is not None else set()
    return {
        "parrilla": parrilla,
        "columnas": [
            {"campo": c, "exigida": c.clave in exigidas} for c in tabla["columnas"] if c.tipo != "ARCHIVO"
        ],
        "filas": filas,
        "nueva": [{**c, "nombre": f"n0__campo_{c['campo'].pk}"} for c in tabla["celdas_nuevas"] if not c["es_archivo"]],
        "etapa": (configuracion.etapa_borradores if configuracion else "") or "Borradores",
        "en_conjunto": bool(configuracion and configuracion.en_conjunto),
        "listas": sum(1 for f in filas if not f["faltan"]),
        "incompletas": sum(1 for f in filas if f["faltan"]),
        "puede_agregar": accion["puede_agregar"],
        "puede_enviar": accion["puede_cerrar"] or not (configuracion and configuracion.en_conjunto),
    }


def filas_de_mision(usuario, ticket, grupo):
    """Filas de una misión por ítems (revisar o elaborar): el ítem, sus datos de referencia, lo editable para esta
    persona (celdas de su actividad), sus archivos, la observación si viene devuelto y su recorrido."""
    from apps.tickets.parrilla import archivos_de, tabla_de_elemento
    from apps.tickets.revision_piezas import anotaciones_de, es_de_gaceta, salidas_de
    from apps.tickets.vista_previa import clase_de

    filas = []
    for accion in grupo["acciones"]:
        elemento = accion.get("elemento") or (accion.get("item") or {}).get("elemento")
        editables = [c for c in (accion.get("ficha") or {}).get("celdas", []) if c.get("editable")]
        archivos = archivos_de(elemento)
        portada = next((a for a in reversed(archivos) if clase_de(a.nombre_original) in ("imagen", "video")), None)
        aprobacion = accion.get("aprobacion")
        filas.append({
            # UX-5: la revisión de un ítem es una PIEZA (tarjeta visual + visor), como en la gaceta.
            "clave": f"r{aprobacion.pk if aprobacion is not None else elemento.pk}",
            "titulo": nombre_de_item(elemento),
            "portada": portada,
            "decision": accion if accion.get("form") is not None and aprobacion is not None else None,
            "salidas": salidas_de(accion.get("form")) if aprobacion is not None else [],
            # La anotación visual solo acompaña la aprobación de un ítem del flujo, sobre una IMAGEN (no gaceta).
            "anotable": (
                aprobacion is not None and portada is not None and clase_de(portada.nombre_original) == "imagen"
                and not es_de_gaceta(aprobacion)
            ),
            "anotaciones": anotaciones_de(elemento),
            "observaciones": [],
            "puede_observar": False,
            "puede_corregir": False,
            "puede_excluir": False,
            "accion": accion,
            "elemento": elemento,
            "nombre": nombre_de_item(elemento),
            "valores": [
                v for v in tabla_de_elemento(elemento)
                if v["etiqueta"] not in {c["campo"].etiqueta for c in editables}
            ],
            "editables": [{**c, "nombre": f"campo_{c['campo'].pk}"} for c in editables if not c["es_archivo"]],
            "archivos_editables": [c for c in editables if c["es_archivo"]],
            "archivos": archivos,
            "devuelto": (accion.get("recorrido_item") or {}).get("devuelto", False),
            "observacion": observacion_de_devolucion(elemento) if (accion.get("recorrido_item") or {}).get("devuelto") else "",
            "faltan": [r["texto"] for r in accion.get("requisitos", []) if r["cumplido"] is False],
        })
    return filas


def ruta(proceso, colecciones, ruta_estados, grupo):
    """Las etapas del recorrido gráfico, de izquierda a derecha: la etapa de borradores y cada paso del ítem (con
    cuántos ítems hay en cada uno y cuántos volvieron), y después las fases que siguen al recorrido; sin colección,
    las fases del ticket; sin flujo, sus estados. Marca dónde está la misión de la persona."""
    etapas = []
    coleccion = next((c for c in colecciones or [] if c.get("etapas")), None)
    if coleccion is not None:
        filas = coleccion["filas"]
        en_recorrido = [f for f in filas if f["item"]]
        total = len(en_recorrido)
        nombres = [e["nombre"] for e in coleccion["etapas"][1:-1]]
        for posicion, etapa in enumerate(coleccion["etapas"][:-1]):
            if posicion == 0:
                nombre = coleccion.get("etapa_borradores") or etapa["nombre"]
                cerrado = coleccion["tabla"]["alcance"]["cerrado"]
                etapas.append({
                    "nombre": nombre, "cantidad": etapa["cantidad"], "devueltos": 0, "clave": etapa["nombre"],
                    "situacion": "actual" if etapa["cantidad"] or not cerrado else "completada",
                    "navegable": bool(filas),
                })
                continue
            else:
                pasaron = sum(
                    1 for f in en_recorrido
                    if f["item"]["terminado"] or f["item"]["hechos"] > nombres.index(etapa["nombre"])
                )
                nombre = etapa["nombre"]
            situacion = "actual" if etapa["cantidad"] else ("completada" if total and pasaron >= total else "pendiente")
            etapas.append({
                "nombre": nombre, "cantidad": etapa["cantidad"], "devueltos": etapa["devueltos"], "situacion": situacion,
                "clave": etapa["nombre"], "pasaron": pasaron, "navegable": bool(etapa["cantidad"] or pasaron),
            })
        terminadas = coleccion["etapas"][-1]["cantidad"]
        if proceso:
            fase_recorrido = proceso.get("fase_actual")
            despues = False
            for fase in proceso["fases"]:
                if despues:
                    etapas.append({"nombre": fase["nombre"], "cantidad": None, "devueltos": 0,
                                   "situacion": fase["situacion"], "clave": fase["nombre"]})
                if fase_recorrido is not None and fase["fase"].pk == fase_recorrido.pk:
                    despues = True
        avance = round(100 * terminadas / total) if total else 0
    elif proceso:
        etapas = [{"nombre": f["nombre"], "cantidad": None, "devueltos": 0, "situacion": f["situacion"], "clave": f["nombre"]}
                  for f in proceso["fases"]]
        avance = round(100 * proceso["completadas"] / proceso["total"]) if proceso["total"] else 0
    elif ruta_estados:
        etapas = [{"nombre": e["nombre"], "cantidad": None, "devueltos": 0, "situacion": e["situacion"], "clave": e["nombre"]}
                  for e in ruta_estados]
        hechas = sum(1 for e in ruta_estados if e["situacion"] == "completada")
        avance = round(100 * hechas / len(ruta_estados)) if ruta_estados else 0
    else:
        return None
    mia = ""
    if grupo is not None:
        if grupo["tipo"] == "lluvia":
            mia = "Borradores"
        elif grupo.get("bloque") is not None:
            mia = grupo["bloque"].nombre
    for etapa in etapas:
        etapa["mia"] = bool(mia) and etapa["clave"] == mia
    return {"etapas": etapas, "avance": avance}
