"""Despacho: traduce los eventos de dominio de una transacción en avisos para personas concretas (CU-036).

Reglas transversales (plan `notificaciones_alertas.md`):

1. Nunca se avisa a quien hizo la acción (salvo el acuse de radicación, que es un comprobante para quien radica).
2. Destinatarios por RELACIÓN con el objeto (`destinatarios`), nunca por rol.
3. Un Proceso programado no tiene solicitante: nada se dirige a «solicitante».
4. Lo que llega en lote se AGRUPA por persona (`clave_agrupacion`) y un aviso más específico SUPRIME a uno más
   general del mismo lote (la observación de una gaceta ya cuenta su tarea de corrección).
5. Centro de notificaciones siempre; correo según el catálogo, lo congelado en el Ticket, las reglas por etapa y
   la preferencia de la persona (lo que exige acción no se apaga).

Los avisos por etapa (Studio › Notificaciones) se leen de `Ticket.notificaciones["reglas"]`, la configuración
CONGELADA al nacer el ticket."""

import logging
from dataclasses import dataclass, field

from django.db import transaction

from apps.notificaciones import catalogo, destinatarios as dest

logger = logging.getLogger(__name__)


@dataclass
class Aviso:
    """Un aviso para UNA persona, antes de guardarse."""

    usuario: object
    clave: str
    ticket: object = None
    actor_id: int | None = None
    contexto: dict = field(default_factory=dict)
    agrupacion: str = ""
    # Claves de agrupación (de otros avisos del lote para esta persona) que este aviso vuelve innecesarios.
    suprime: tuple = ()
    # `None` = lo decide el catálogo; `True/False` = lo decidió una regla por etapa.
    correo: bool | None = None
    incluye_actor: bool = False
    cantidad: int = 1


# --- Carga perezosa de objetos ------------------------------------------------------------------------------


def _ticket(ticket_id):
    from apps.tickets.models import Ticket

    if not ticket_id:
        return None
    return (
        Ticket.objects.select_related("detalle_servicio__servicio", "solicitante", "usuario_responsable", "equipo_responsable")
        .filter(pk=ticket_id).first()
    )


def _ticket_de_tarea(tarea_id):
    from apps.tickets import vinculos

    return (vinculos.origen_de_tareas([tarea_id]).get(tarea_id) or {}).get("ticket_id")


def _ticket_de_aprobacion(aprobacion):
    from apps.tickets import vinculos

    return (vinculos.origen_de_aprobaciones([aprobacion]).get(aprobacion.pk) or {})


def _ticket_de_instancia(instancia):
    from apps.tickets.models import Ticket

    return Ticket.objects.filter(instancia_workflow_id=instancia.raiz_id).values_list("pk", flat=True).first()


# --- Traducción por evento ----------------------------------------------------------------------------------


def _para(usuarios, clave, **kwargs):
    return [Aviso(usuario=u, clave=clave, **kwargs) for u in usuarios]


def _avisos_de_ticket(evento):
    from apps.tickets.entregas import CAUSA_ACEPTACION
    from apps.tickets.models import Ticket

    ticket = _ticket(evento.ticket_id)
    if ticket is None:
        return []
    tipo = evento.clave.split(":", 1)[1]
    datos = evento.datos
    base = {"ticket": ticket, "actor_id": evento.actor_id}
    # Quien recibe el ticket ya sabe, por este aviso, que las actividades pendientes del ticket pasan a él.
    asignado = {"suprime": (f"TAREA_ASIGNADA:t{ticket.pk}",)}
    avisos = []
    if tipo == "RADICADO":
        avisos += _para(dest.solicitante(ticket), "TICKET_RADICADO", incluye_actor=True, **base)
        if ticket.origen == Ticket.Origen.PROGRAMACION:
            avisos += _para(dest.responsable(ticket), "TICKET_GENERADO", **base)
        elif ticket.usuario_responsable_id:
            avisos += _para(dest.personas([ticket.usuario_responsable_id]), "TICKET_ASIGNADO", contexto={"dirigido": True},
                            **asignado, **base)
        elif ticket.equipo_responsable_id:
            avisos += _para(dest.pueden_tomar(ticket), "TICKET_EN_COLA", **base)
    elif tipo in ("TOMADO", "ATENCION_INICIADA"):
        avisos += _para(dest.solicitante(ticket), "TICKET_EN_ATENCION", **base)
    elif tipo in ("ASIGNADO", "REASIGNADO"):
        if datos.get("usuario_id"):
            avisos += _para(dest.personas([datos["usuario_id"]]), "TICKET_ASIGNADO", **asignado, **base)
            if tipo == "ASIGNADO":
                # Asignar a una persona inicia la atención (RADICADO → EN_ATENCION), igual que tomarlo.
                avisos += _para(dest.solicitante(ticket), "TICKET_EN_ATENCION", **base)
        elif datos.get("equipo_id"):
            avisos += _para(dest.miembros_de(datos["equipo_id"]), "TICKET_EN_COLA", **base)
        anterior = datos.get("usuario_anterior_id")
        if anterior and anterior != datos.get("usuario_id"):
            avisos += _para(dest.personas([anterior]), "TICKET_REASIGNADO_FUERA", **base)
    elif tipo == "INFORMACION_SOLICITADA":
        solicitud = ticket.solicitudes_informacion.filter(pk=datos.get("solicitud_id")).first()
        if solicitud:
            avisos += _para(dest.personas([solicitud.destinatario_id]), "INFORMACION_SOLICITADA",
                            contexto={"extracto": solicitud.mensaje}, **base)
    elif tipo == "INFORMACION_RESPONDIDA":
        solicitud = ticket.solicitudes_informacion.filter(pk=datos.get("solicitud_id")).first()
        quienes = dest.unir(dest.personas([solicitud.solicitada_por_id]) if solicitud else [], dest.responsable(ticket))
        avisos += _para(quienes, "INFORMACION_RESPONDIDA", **base)
    elif tipo == "COMENTARIO":
        avisos += _para(dest.unir(dest.solicitante(ticket), dest.responsable(ticket)), "COMENTARIO",
                        contexto={"extracto": datos.get("contenido", "")}, **base)
    elif tipo == "PRORROGA_APROBADA":
        avisos += _para(dest.unir(dest.solicitante(ticket), dest.responsable(ticket)), "FECHA_OBJETIVO_CAMBIADA", **base)
    elif tipo == "PRORROGA_RECHAZADA":
        from apps.tickets.models import ProrrogaTicket

        prorroga = ProrrogaTicket.objects.filter(pk=datos.get("prorroga_id")).first()
        quienes = dest.unir(dest.personas([prorroga.solicitada_por_id]) if prorroga else [], dest.responsable(ticket))
        avisos += _para(quienes, "PRORROGA_RECHAZADA", contexto={"extracto": datos.get("observaciones", "")}, **base)
    elif tipo == "ENTREGADO":
        avisos += _para(dest.solicitante(ticket), "ENTREGA_DISPONIBLE", contexto={"vence_en": datos.get("vence_en")}, **base)
    elif tipo == "ENTREGA_OBSERVADA":
        entrega = ticket.entregas.filter(pk=datos.get("entrega_id")).first()
        avisos += _para(dest.responsable(ticket), "ENTREGA_OBSERVADA",
                        contexto={"extracto": entrega.observaciones if entrega else ""}, **base)
    elif tipo == "RESUELTO":
        avisos += _para(dest.solicitante(ticket), "TICKET_RESUELTO", **base)
    elif tipo == "CERRADO":
        avisos += _para(dest.solicitante(ticket), "TICKET_CERRADO", **base)
        if datos.get("causa") == CAUSA_ACEPTACION:
            avisos += _para(dest.responsable(ticket), "ENTREGA_ACEPTADA", **base)
        elif evento.actor_id is None:
            avisos += _para(dest.responsable(ticket), "TICKET_CERRADO", **base)
    elif tipo in ("CANCELADO", "REABIERTO"):
        avisos += _para(dest.unir(dest.solicitante(ticket), dest.responsable(ticket)), f"TICKET_{tipo}",
                        contexto={"extracto": datos.get("motivo", "")}, **base)
    return avisos


def _avisos_de_tarea(evento):
    from apps.tareas.models import Tarea

    tipo = evento.clave.split(":", 1)[1]
    if tipo not in ("CREADA", "ASIGNADA", "REASIGNADA", "DELEGADA"):
        return []
    tarea = Tarea.objects.filter(pk=evento.objetos.get("tarea")).first()
    if tarea is None or tarea.estado in Tarea.ESTADOS_CERRADOS:
        return []
    ticket_id = _ticket_de_tarea(tarea.pk)
    ticket = _ticket(ticket_id)
    agrupacion = f"TAREA_ASIGNADA:t{ticket_id}" if ticket_id else f"TAREA_ASIGNADA:tarea{tarea.pk}"
    return _para(
        dest.de_tarea(tarea), "TAREA_ASIGNADA", ticket=ticket, actor_id=evento.actor_id, agrupacion=agrupacion,
        contexto={"tarea_id": tarea.pk, "tarea": tarea.titulo, "fecha_limite": tarea.fecha_limite,
                  "delegada": tipo == "DELEGADA"},
    )


def _avisos_de_aprobacion(evento):
    from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion

    tipo = evento.clave.split(":", 1)[1]
    esquema = EsquemaAprobacion.objects.filter(pk=evento.objetos.get("esquema")).first()
    if esquema is None:
        return []
    avisos = []
    turno = []
    if tipo in ("ESQUEMA_CREADO", "DECIDIDA"):
        turno = dest.aprobaciones_en_turno(esquema)
        if tipo == "DECIDIDA":
            # Solo es «nuevo turno» el de la siguiente en una secuencia (en PARALELA ya se avisó a todas).
            turno = turno if esquema.modo == EsquemaAprobacion.Modo.SECUENCIAL else []
    elif tipo == "REASIGNADA":
        turno = [a for a in dest.aprobaciones_en_turno(esquema) if a.pk == evento.objetos.get("aprobacion")]
    for aprobacion in turno:
        origen = _ticket_de_aprobacion(aprobacion)
        ticket = _ticket(origen.get("ticket_id"))
        avisos += _para(
            dest.de_aprobacion(aprobacion), "APROBACION_TURNO", ticket=ticket, actor_id=evento.actor_id,
            agrupacion=f"APROBACION_TURNO:{aprobacion.pk}",
            contexto={"aprobacion_id": aprobacion.pk, "que": _que_se_aprueba(aprobacion, origen)},
        )
    if tipo == "DECIDIDA" and esquema.resultado in (EsquemaAprobacion.Resultado.DEVUELTA, EsquemaAprobacion.Resultado.RECHAZADA):
        decidida = Aprobacion.objects.filter(pk=evento.objetos.get("aprobacion")).first()
        origen = _ticket_de_aprobacion(decidida) if decidida else {}
        if origen.get("origen") == "FLUJO":
            ticket = _ticket(origen.get("ticket_id"))
            avisos += _para(
                dest.responsable(ticket), "TRABAJO_DEVUELTO", ticket=ticket, actor_id=evento.actor_id,
                contexto={"rechazo": esquema.resultado == EsquemaAprobacion.Resultado.RECHAZADA,
                          "que": _que_se_aprueba(decidida, origen), "extracto": decidida.observacion},
            )
            avisos += _reglas(ticket, "APROBACION_DEVUELTA", _clave_bloque_de_aprobacion(decidida), evento)
    return avisos


def _que_se_aprueba(aprobacion, origen):
    """Nombre legible de lo que se aprueba: el bloque del flujo, la gaceta o la prórroga."""
    if origen.get("origen") == "PRORROGA":
        return "una prórroga de la fecha objetivo"
    if origen.get("origen") == "GACETA":
        from apps.gacetas.models import RondaRevisionGaceta

        ronda = RondaRevisionGaceta.objects.filter(esquema_id=aprobacion.esquema_id).select_related(
            "participacion__gaceta").first()
        return f"la gaceta «{ronda.participacion.gaceta.nombre}»" if ronda else "una pieza de la gaceta"
    from apps.workflows.models import EsquemaAprobacionWorkflow

    vinculo = EsquemaAprobacionWorkflow.objects.filter(esquema_id=aprobacion.esquema_id).select_related(
        "instancia_etapa__bloque_operativo", "instancia_etapa__etapa").first()
    if vinculo:
        bloque = vinculo.instancia_etapa.bloque_operativo or vinculo.instancia_etapa.etapa
        if bloque is not None:
            return f"«{bloque.nombre}»"
    return "una aprobación"


def _clave_bloque_de_aprobacion(aprobacion):
    from apps.workflows.models import EsquemaAprobacionWorkflow

    if aprobacion is None:
        return ""
    vinculo = EsquemaAprobacionWorkflow.objects.filter(esquema_id=aprobacion.esquema_id).select_related(
        "instancia_etapa__bloque_operativo").first()
    bloque = vinculo.instancia_etapa.bloque_operativo if vinculo else None
    return bloque.clave if bloque is not None else ""


def _avisos_de_gaceta(evento):
    from apps.gacetas.models import GacetaTicket, ObservacionGaceta, ParticipacionItemGaceta

    tipo = evento.clave.split(":", 1)[1]
    gaceta = GacetaTicket.objects.select_related("ticket").filter(pk=evento.objetos.get("gaceta")).first()
    if gaceta is None:
        return []
    ticket = _ticket(gaceta.ticket_id)
    base = {"ticket": ticket, "actor_id": evento.actor_id}
    contexto = {"gaceta": gaceta.nombre, "vence_en": gaceta.vence_revision_en}
    avisos = []
    if tipo == "ABIERTA":
        avisos += _para(dest.revisores_gaceta(gaceta), "GACETA_ABIERTA", contexto=contexto, **base)
        avisos += _reglas(ticket, "GACETA_ABIERTA", gaceta.clave, evento)
    elif tipo == "OBSERVACION":
        observacion = ObservacionGaceta.objects.filter(pk=evento.objetos.get("observacion")).select_related(
            "participacion__elemento", "ronda__tarea_correccion").first()
        if observacion is not None:
            tarea = observacion.ronda.tarea_correccion if observacion.ronda_id else None
            quienes = dest.de_tarea(tarea) if tarea is not None else dest.coordinadores_gaceta(gaceta)
            avisos += _para(
                quienes, "GACETA_OBSERVACION", agrupacion=f"GACETA_OBSERVACION:{observacion.participacion_id}",
                suprime=(f"TAREA_ASIGNADA:t{gaceta.ticket_id}",),
                contexto={**contexto, "item": _nombre_item(observacion.participacion.elemento), "extracto": observacion.texto},
                **base,
            )
    elif tipo == "NUEVA_RONDA":
        participacion = ParticipacionItemGaceta.objects.filter(pk=evento.objetos.get("participacion")).select_related("elemento").first()
        avisos += _para(dest.revisores_gaceta(gaceta), "GACETA_NUEVA_RONDA",
                        agrupacion=f"GACETA_NUEVA_RONDA:{gaceta.pk}",
                        contexto={**contexto, "item": _nombre_item(participacion.elemento) if participacion else ""}, **base)
    elif tipo == "PERIODO_CERRADO":
        avisos += _para(dest.coordinadores_gaceta(gaceta), "GACETA_PERIODO_CERRADO", contexto=contexto, **base)
    elif tipo == "RESUELTA":
        avisos += _para(dest.unir(dest.coordinadores_gaceta(gaceta), dest.responsable(ticket)), "GACETA_RESUELTA",
                        contexto=contexto, **base)
        avisos += _reglas(ticket, "GACETA_RESUELTA", gaceta.clave, evento)
    return avisos


def _nombre_item(elemento):
    if elemento is None:
        return ""
    try:
        from apps.tickets.espacio import nombre_de_item

        return nombre_de_item(elemento)
    except Exception:  # noqa: BLE001 — un nombre legible nunca impide el aviso.
        return f"Ítem {elemento.numero}"


def _avisos_de_flujo(evento):
    """Avisos por etapa del motor (llegar a una fase, empezar o completar una actividad): solo las reglas."""
    from apps.workflows.models import InstanciaWorkflow

    instancia = InstanciaWorkflow.objects.filter(pk=evento.objetos.get("instancia")).first()
    if instancia is None:
        return []
    ticket = _ticket(_ticket_de_instancia(instancia))
    if ticket is None:
        return []
    momento = {
        "FASE_ALCANZADA": "FASE_ALCANZADA", "BLOQUE_INICIADO": "ACTIVIDAD_INICIADA",
        "BLOQUE_COMPLETADO": "ACTIVIDAD_COMPLETADA",
    }[evento.clave.split(":", 1)[1]]
    return _reglas(ticket, momento, evento.datos.get("clave", ""), evento)


TRADUCTORES = {
    "TICKET": _avisos_de_ticket,
    "TAREA": _avisos_de_tarea,
    "APROBACION": _avisos_de_aprobacion,
    "GACETA": _avisos_de_gaceta,
    "FLUJO": _avisos_de_flujo,
}


# --- Avisos por etapa (Studio › Notificaciones) -------------------------------------------------------------


def _reglas(ticket, momento, clave, evento):
    if ticket is None or not clave:
        return []
    configuracion = ticket.notificaciones or {}
    avisos = []
    for regla in configuracion.get("reglas") or []:
        if regla.get("momento") != momento or regla.get("clave_objetivo") != clave:
            continue
        quienes = _destinatarios_de_regla(ticket, regla, evento)
        avisos += _para(
            quienes, "REGLA_ETAPA", ticket=ticket, actor_id=evento.actor_id, correo=bool(regla.get("correo")),
            agrupacion=f"REGLA:{regla.get('id')}:{evento.objetos.get('ejecucion') or evento.objetos.get('gaceta') or ''}",
            contexto={"momento": momento, "objetivo": regla.get("nombre_objetivo") or evento.datos.get("nombre", ""),
                      "extracto": regla.get("mensaje", ""), "regla": True},
        )
    return avisos


def _destinatarios_de_regla(ticket, regla, evento):
    tipo = regla.get("destinatario")
    if tipo == "SOLICITANTE":
        return dest.solicitante(ticket)
    if tipo == "RESPONSABLE_TICKET":
        return dest.responsable(ticket)
    if tipo == "EQUIPO_RESPONSABLE":
        return dest.equipo_responsable(ticket)
    if tipo == "USUARIO":
        return dest.personas([regla.get("usuario_id")])
    if tipo == "EQUIPO":
        return dest.miembros_de(regla.get("equipo_id"))
    if tipo == "CAMPO_FORMULARIO":
        return dest.de_campo_formulario(ticket, regla.get("campo_clave"))
    if tipo == "COLUMNA_ITEM":
        return dest.de_columna_item(evento.datos.get("elemento_id"), regla.get("campo_clave"))
    if tipo == "RESPONSABLE_ACTIVIDAD":
        return _responsables_de_ejecucion(evento.objetos.get("ejecucion"))
    if tipo == "COORDINADOR_GACETA":
        from apps.gacetas.models import GacetaTicket

        gaceta = GacetaTicket.objects.filter(pk=evento.objetos.get("gaceta")).select_related("ticket").first()
        return dest.coordinadores_gaceta(gaceta) if gaceta else []
    return []


def _responsables_de_ejecucion(ejecucion_id):
    from apps.workflows.models import TareaWorkflow

    if not ejecucion_id:
        return []
    salida = []
    for vinculo in TareaWorkflow.objects.filter(instancia_etapa_id=ejecucion_id).select_related("tarea"):
        salida = dest.unir(salida, dest.de_tarea(vinculo.tarea))
    return salida


# --- Despacho -----------------------------------------------------------------------------------------------


def despachar(eventos):
    """Traduce, filtra, agrupa y guarda los avisos de un lote. Cada evento se traduce aislado: uno que falle no
    impide los demás."""
    avisos = []
    for evento in eventos:
        prefijo = evento.clave.split(":", 1)[0]
        traductor = TRADUCTORES.get(prefijo)
        if traductor is None:
            continue
        try:
            avisos += traductor(evento)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo traducir el evento %s", evento.clave)
    return entregar(avisos)


def entregar(avisos):
    """Guarda los avisos (también los de las alertas) y encola sus correos. Devuelve las notificaciones creadas."""
    from apps.notificaciones import correo

    avisos = [a for a in avisos if _es_valido(a)]
    if not avisos:
        return []
    suprimidas = {(a.usuario.pk, clave) for a in avisos for clave in a.suprime}
    agrupados = {}
    for aviso in avisos:
        if aviso.agrupacion and (aviso.usuario.pk, aviso.agrupacion) in suprimidas:
            continue
        llave = (aviso.usuario.pk, aviso.agrupacion or id(aviso))
        if llave in agrupados:
            existente = agrupados[llave]
            # La misma tarea creada y asignada en la misma operación es UNA actividad, no dos.
            vistas = existente.contexto.setdefault("tarea_ids", {existente.contexto.get("tarea_id")})
            if aviso.contexto.get("tarea_id") is not None and aviso.contexto["tarea_id"] in vistas:
                continue
            vistas.add(aviso.contexto.get("tarea_id"))
            existente.cantidad += aviso.cantidad
            continue
        agrupados[llave] = aviso
    creadas = []
    with transaction.atomic():
        for aviso in agrupados.values():
            try:
                with transaction.atomic():
                    creadas.append(_guardar(aviso))
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo guardar el aviso %s para %s", aviso.clave, aviso.usuario.pk)
    correo.encolar([n for n in creadas if getattr(n, "_con_correo", False)])
    return creadas


def _es_valido(aviso):
    usuario = aviso.usuario
    if usuario is None or not getattr(usuario, "is_active", False):
        return False
    if aviso.actor_id is not None and usuario.pk == aviso.actor_id and not aviso.incluye_actor:
        return False
    return aviso.clave in catalogo.AVISOS


def _guardar(aviso):
    from apps.notificaciones import mensajes, preferencias
    from apps.notificaciones.models import EnvioCorreo, Notificacion

    definicion = catalogo.aviso(aviso.clave)
    contenido = mensajes.componer(aviso)
    notificacion = Notificacion.objects.create(
        destinatario=aviso.usuario,
        evento=aviso.clave,
        grupo=definicion.grupo,
        titulo=contenido["titulo"][:200],
        resumen=contenido["resumen"],
        hechos=contenido["hechos"],
        extracto=contenido["extracto"][:300],
        ticket=aviso.ticket,
        enlace=mensajes.enlace_para(aviso.usuario, aviso),
        actor_id=aviso.actor_id,
        requiere_accion=definicion.requiere_accion,
        es_alerta=definicion.es_alerta,
        cantidad=aviso.cantidad,
        clave_agrupacion=aviso.agrupacion[:200],
    )
    if _quiere_correo(aviso, definicion) and preferencias.acepta_correo(aviso.usuario, definicion):
        EnvioCorreo.objects.create(notificacion=notificacion, correo=aviso.usuario.email)
        notificacion._con_correo = True
    return notificacion


def _quiere_correo(aviso, definicion):
    if not aviso.usuario.email:
        return False
    if aviso.correo is not None:
        return aviso.correo or definicion.requiere_accion
    ajustes = (getattr(aviso.ticket, "notificaciones", None) or {}).get("correo") if aviso.ticket is not None else None
    return catalogo.correo_por_defecto(aviso.clave, ajustes)
