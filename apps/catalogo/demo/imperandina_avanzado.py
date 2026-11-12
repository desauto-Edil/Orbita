"""Demo «Parrilla mensual - Imperandina» — EJEMPLO AVANZADO (`preparar_demo_orbita --fase avanzar --casos imperandina`).

Toma el ticket de prueba que ya existe (el de `--fase ejemplos`; nunca crea otro) y lo lleva, con las operaciones
reales del dominio, hasta DESPUÉS de la gaceta: las ideas se aprueban, la pieza se produce (archivo de prueba), se
aprueba y llega a la «Revisión de contenidos» (una de ellas con un comentario de Mercadeo Imperandina, su corrección
y una segunda ronda). En la gaceta nadie aprueba (regla de oro): cuando todas las piezas llegaron, la coordinadora
cierra el periodo y queda aceptado lo que no tiene comentarios pendientes. Cada pieza queda en uno de dos puntos
posteriores a la gaceta:

- PENDIENTE DE PUBLICACIÓN: espera «Publicación», que hace cualquiera de Mercadeo BPO subiendo la evidencia (la
  lista «Evidencia de publicación» del ítem; nadie «toma» la actividad, responde emm);
- PUBLICADA: un integrante del equipo ya subió la prueba y completó la publicación (su recorrido terminó): los
  interesados ven la pieza con su prueba desde la gaceta.

No completa todas las actividades, no resuelve la gaceta (solo cierra su periodo) ni el ticket, no cambia el contrato oficial ni la
programación mensual, y no escribe ningún estado: cada paso lo da la persona a la que el dominio se lo asignó, por la
vía de la pantalla (`demo.avance`), con sus permisos, versiones del ítem y auditoría.

Repetible: cada ítem continúa DESDE DONDE ESTÉ (también si alguien lo avanzó a mano) y se detiene en su punto
objetivo. Lo que ya está en su punto o más adelante no se toca; lo que está en un estado que la demo no conoce
(detenido, fuera del alcance…) se informa y se deja como está.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from apps.catalogo.demo import avance, contrato
from apps.catalogo.demo.participantes import Participantes

PUBLICACION = "Publicación"
PUBLICADA = "publicada"  # objetivo: «Publicación» completada con su evidencia (recorrido terminado)
REVISION = "Revisión de contenidos"
ESPERA_CIERRE = "espera el cierre del periodo de la gaceta"
# Orden de los pasos del recorrido de cada publicación (contrato v3), para saber si un ítem ya pasó su objetivo.
SECUENCIA = (
    "Ajustar la idea", "Aprobación de la idea", "Producción", "Aprobación de la pieza", REVISION, PUBLICACION, PUBLICADA,
)


@dataclass(frozen=True)
class AvanceSpec:
    objetivo: str  # PUBLICACION (queda esperando la evidencia) | PUBLICADA (publicación completada con su prueba)
    observacion: str = ""  # observación de Mercadeo Imperandina en la gaceta (primera ronda), si la hay
    respuesta: str = ""  # respuesta de quien corrige


# Por número de ítem (las cinco ideas del contrato, en su orden). Las dos primeras ya se publicaron.
AVANCE = {
    1: AvanceSpec(PUBLICADA),
    2: AvanceSpec(
        PUBLICADA,
        observacion="El cierre de marca dura muy poco: déjenlo al menos 3 segundos y agreguen el logo de Imperandina.",
        respuesta="Cierre de marca ampliado a 3 segundos con el logo; se adjunta la versión corregida.",
    ),
    3: AvanceSpec(PUBLICACION),
    4: AvanceSpec(PUBLICACION),
    5: AvanceSpec(PUBLICACION),
}
COLORES = {1: (37, 99, 235), 2: (220, 38, 38), 3: (5, 150, 105), 4: (217, 119, 6), 5: (124, 58, 237)}


def _ticket(recurso):
    from apps.tickets.models import EjecucionProgramada, Ticket

    ejecucion = EjecucionProgramada.objects.filter(servicio=recurso.servicio).order_by("-periodo_inicio").first()
    return Ticket.objects.get(pk=ejecucion.ticket_id) if ejecucion is not None else None


def _hija(ticket, elemento):
    from apps.workflows.models import InstanciaWorkflow

    return (
        InstanciaWorkflow.objects.filter(instancia_padre_id=ticket.instancia_workflow_id, elemento=elemento)
        .order_by("-pk").first()
    )


def _archivo(elemento, columna, version):
    numero = elemento.numero
    return avance.imagen_png(f"pieza-{numero}-v{version}.png", COLORES.get(numero, (23, 24, 27)))


def _reemplazar_archivo(elemento, columna, actor):
    """Archivos que se adjuntan al avanzar: los vigentes de la columna se retiran (retiro lógico, quedan en la
    historia y en las versiones) y se adjunta la versión siguiente."""
    from apps.tickets import parrilla as parrilla_ops

    vigentes = parrilla_ops.archivos_de(elemento, columna)
    for adjunto in vigentes:
        parrilla_ops.retirar_archivo_elemento(adjunto, actor)
    total = elemento.archivos.filter(campo_elemento=columna).count()
    return [(columna, _archivo(elemento, columna, total + 1))]


def _revisor(p, participacion):
    """Una persona del equipo revisor del contrato que puede observar ahora (en el orden del contrato)."""
    from apps.core.models import Usuario
    from apps.gacetas.autorizacion import puede_observar

    spec = next(e for e in contrato.CONTRATOS[contrato.IMPERANDINA].equipos if e.atributo == "equipo_revisor")
    for referencia in spec.miembros:
        usuario = Usuario.objects.por_referencia(referencia).filter(is_active=True).first()
        if usuario is not None and puede_observar(usuario, participacion):
            return usuario
    raise ValidationError("Nadie del equipo revisor puede registrar la observación ahora.")


def _paso_gaceta(ticket, ejecucion, elemento, spec, p, columna):
    """Lo que la pieza necesita en la gaceta: el comentario del ejemplo o la corrección de su responsable. `None` si
    ya no queda nada por hacer en ella: espera el cierre del periodo (nadie aprueba en una gaceta)."""
    from apps.gacetas import operaciones as gacetas_ops
    from apps.gacetas.models import GacetaTicket, ObservacionGaceta, ParticipacionItemGaceta, RondaRevisionGaceta

    clave = (ejecucion.bloque_operativo.configuracion or {}).get("gaceta") or ""
    gaceta = GacetaTicket.objects.get(ticket=ticket, clave=clave)
    participacion = ParticipacionItemGaceta.objects.get(gaceta=gaceta, elemento=elemento)
    ronda = RondaRevisionGaceta.objects.get(participacion=participacion, numero=participacion.ronda)
    if participacion.estado == ParticipacionItemGaceta.Estado.EN_REVISION:
        if spec.observacion and not ObservacionGaceta.objects.filter(participacion=participacion).exists():
            revisor = _revisor(p, participacion)
            gacetas_ops.observar(participacion, revisor, spec.observacion)
            return f"comentario de {revisor.email} en la gaceta"
        return None
    if participacion.estado == ParticipacionItemGaceta.Estado.EN_CORRECCION:
        tarea = ronda.tarea_correccion
        if tarea is None:
            raise ValidationError(f"El ítem #{elemento.numero} está en corrección sin tarea de corrección.")
        actor = avance.responsable_de(tarea)
        for observacion in ObservacionGaceta.objects.filter(
            participacion=participacion, atendida_en__isnull=True, respuesta=""
        ).order_by("creada_en"):
            gacetas_ops.responder_observacion(observacion, actor, spec.respuesta or "Corregido según la observación.")
        avance.completar(
            tarea, actor, elemento=elemento, archivos=_reemplazar_archivo(elemento, columna, actor), verificar=False
        )
        return f"{actor.email} corrigió y reenvió a la gaceta"
    raise ValidationError(
        f"El ítem #{elemento.numero} tiene el resultado «{participacion.get_estado_display()}» en la gaceta."
    )


def _dar_publicacion(tarea, elemento):
    """«Publicación» es del equipo Mercadeo BPO: la hace quien responde por ella (sin tomarla), sube la prueba de
    cada punto de la lista «Evidencia de publicación» (archivo de prueba) y la completa."""
    from apps.tickets import evidencias
    from apps.tickets import parrilla as parrilla_ops

    actor = tarea.responde_por or avance.responsable_de(tarea)
    lista = next(
        (c for c in parrilla_ops.columnas_de(elemento.parrilla) if c.clave == contrato.EVIDENCIA_PUBLICACION), None
    )
    if lista is not None:
        marcadas = {m.punto for m in evidencias.marcas_vigentes(elemento=elemento, campo=lista)}
        for valor, _etiqueta in evidencias.puntos_de(lista):
            if valor not in marcadas:
                prueba = avance.imagen_png(f"publicada-{elemento.numero}.png", COLORES.get(elemento.numero, (23, 24, 27)))
                evidencias.marcar_punto_elemento(elemento, lista, valor, actor, archivo=prueba)
    avance.completar(tarea, actor, verificar=False)
    return f"publicada por {actor.email} con su evidencia"


def _dar_un_paso(ticket, ejecucion, elemento, spec, p, columna):
    """Da el paso que el ítem está esperando, con la persona asignada. Devuelve una descripción breve."""
    paso = avance.nombre_del_paso(ejecucion)
    if paso in ("Aprobación de la idea", "Aprobación de la pieza"):
        esquema = avance.esquema_del_paso(ejecucion)
        texto = "Idea aprobada para producir." if paso == "Aprobación de la idea" else "Pieza aprobada para la gaceta."
        quienes = avance.decidir(esquema, "APROBADA", texto)
        if not quienes:
            raise ValidationError(f"«{paso}» del ítem #{elemento.numero} no tiene una persona que decida.")
        return f"{paso.lower()} por {quienes[0].email}"
    if paso == REVISION:
        return _paso_gaceta(ticket, ejecucion, elemento, spec, p, columna) or ESPERA_CIERRE
    tarea = avance.tarea_de(ejecucion)
    if tarea is None:
        raise ValidationError(f"«{paso}» del ítem #{elemento.numero} no tiene su tarea.")
    if paso == PUBLICACION:
        return _dar_publicacion(tarea, elemento)
    actor = avance.responsable_de(tarea)
    if paso == "Producción":
        avance.completar(tarea, actor, elemento=elemento, archivos=_reemplazar_archivo(elemento, columna, actor))
        return f"producida por {actor.email} (archivo de prueba)"
    if paso == "Ajustar la idea":
        avance.completar(tarea, actor, elemento=elemento)
        return f"«{paso}» completada por {actor.email}"
    raise ValidationError(f"El ítem #{elemento.numero} espera «{paso}», un paso que la demo no da.")


def _avanzar_item(ticket, elemento, spec, p, columna):
    """Avanza un ítem hasta su objetivo. Devuelve `(estado_final, pasos_dados)`."""
    pasos = []
    for _ in range(20):
        hija = _hija(ticket, elemento)
        if hija is None:
            return "sin recorrido (no se envió)", pasos
        ejecucion = avance.ejecucion_en_espera(hija)
        if ejecucion is None:
            if spec.objetivo == PUBLICADA and hija.estado == "COMPLETADA":
                return "publicada con su evidencia", pasos
            return f"recorrido {hija.get_estado_display().lower()} (se conserva)", pasos
        paso = avance.nombre_del_paso(ejecucion)
        if paso == spec.objetivo:
            return f"pendiente de {paso.lower()}", pasos
        if paso in SECUENCIA and SECUENCIA.index(paso) > SECUENCIA.index(spec.objetivo):
            return f"ya está en «{paso}», más adelante que el objetivo (se conserva)", pasos
        paso_dado = _dar_un_paso(ticket, ejecucion, elemento, spec, p, columna)
        if paso_dado == ESPERA_CIERRE:
            return f"en la gaceta: {ESPERA_CIERRE}", pasos
        pasos.append(paso_dado)
        elemento.refresh_from_db()
    raise ValidationError(f"El ítem #{elemento.numero} no llegó a «{spec.objetivo}»: revisa su recorrido.")


def avanzar(recurso):
    """Lleva el ticket de prueba de Imperandina a los estados posteriores a la gaceta. Devuelve el informe."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.models import Ticket
    from apps.workflows import recorridos

    servicio = recurso.servicio
    if not servicio.activo:
        return ["Primero publica el caso (--fase publicar --casos imperandina)."]
    ticket = _ticket(recurso)
    if ticket is None:
        return ["Primero crea el ticket de prueba (--fase ejemplos --casos imperandina)."]
    if ticket.estado != Ticket.Estado.EN_ATENCION:
        return [f"El ticket {ticket.codigo} está {ticket.get_estado_display().lower()}: no se avanza."]
    p = Participantes.desde_parametros(recurso.parametros)
    parrilla = parrilla_ops.parrilla_de(ticket)
    columna = next(c for c in parrilla_ops.columnas_de(parrilla) if c.clave == contrato.ARCHIVO_PIEZA)
    lineas = [f"Ticket {ticket.codigo} «{ticket.etiqueta}» (no se crea otro)."]
    if recorridos.recorrido_al_enviar(ticket, parrilla) is not None and not parrilla.alcance_cerrado:
        enviados = recorridos.enviar_conjunto(ticket, parrilla, p.planificador)
        lineas.append(f"{p.planificador.email} envió la lluvia de ideas ({len(enviados)} ideas) a aprobación.")
        parrilla.refresh_from_db()
    pasos_por_item = {}
    for ronda in (1, 2):
        estados = {}
        for elemento in parrilla_ops.elementos_en_alcance(parrilla):
            spec = AVANCE.get(elemento.numero)
            if spec is None:
                continue
            estado, pasos = _avanzar_item(ticket, elemento, spec, p, columna)
            estados[elemento.pk] = estado
            pasos_por_item.setdefault(elemento.pk, []).extend(pasos)
        esperan = [pk for pk, estado in estados.items() if ESPERA_CIERRE in estado]
        if ronda == 2 or not esperan:
            break
        cierre = _cerrar_periodo(ticket, p)
        lineas.append(cierre)
        if not cierre.startswith("Periodo"):
            break
    for elemento in parrilla_ops.elementos_en_alcance(parrilla):
        nombre = parrilla_ops.nombre_de(elemento)
        if elemento.numero not in AVANCE:
            lineas.append(f"  #{elemento.numero} «{nombre}»: fuera del ejemplo (no se toca).")
            continue
        pasos = pasos_por_item.get(elemento.pk) or []
        detalle = f" — {'; '.join(pasos)}" if pasos else ""
        lineas.append(f"  #{elemento.numero} «{nombre[:48]}»: {estados.get(elemento.pk, '')}{detalle}.")
    lineas.append(
        "El ticket sigue en atención: queda por publicar (subir la evidencia), resolver la gaceta y cerrar la parrilla."
    )
    return lineas


def _cerrar_periodo(ticket, p):
    """La coordinadora cierra el periodo de la «Revisión de contenidos»: queda aceptado lo que no tenga comentarios
    pendientes (nadie aprueba en una gaceta). Si aún faltan piezas por llegar (protección 5.3), se informa."""
    from apps.gacetas import operaciones as gacetas_ops
    from apps.gacetas.models import GacetaTicket

    gaceta = GacetaTicket.objects.filter(ticket=ticket, estado=GacetaTicket.Estado.ABIERTA).order_by("pk").first()
    if gaceta is None:
        return "La gaceta ya no está abierta: no se cierra su periodo."
    try:
        gacetas_ops.cerrar_periodo(gaceta, p.planificador)
    except ValidationError as exc:
        return f"La gaceta sigue abierta: {'; '.join(exc.messages)}"
    return (
        f"Periodo de «{gaceta.nombre}» cerrado por {p.planificador.email}: quedó aceptado lo que no tenía "
        "comentarios pendientes."
    )
