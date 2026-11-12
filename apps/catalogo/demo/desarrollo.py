"""Demo «Plan de trabajo — Desarrollo de software» — ticket de EJEMPLO (`preparar_demo_orbita --fase ejemplos
--casos desarrollo --solicitante <usuario> [--area-ejemplo <área>]`).

La solicitud ficticia es el desarrollo de la propia plataforma Órbita. Con las operaciones de siempre y cada paso
dado por la persona a la que el dominio se lo asignó (`demo.avance`):

1. El SOLICITANTE radica la solicitud con su información inicial (proceso MANUAL: nunca por programación).
2. El responsable técnico toma el ticket de la Cola, revisa la solicitud («Evaluación técnica», su lista de
   verificación) y arma el PLAN (`contrato.PLAN_ORBITA`: requisitos, arquitectura, catálogo, Studio, workflows,
   tickets, tareas, gacetas, pruebas y despliegue), con él como responsable de cada actividad y fechas en días
   hábiles, y lo envía COMPLETO al aprobador (`recorridos.enviar_conjunto`: cierra el alcance).

`ejemplos` se detiene AHÍ: las 10 actividades esperan la aprobación del aprobador (para ver cómo se le presenta) y
la gaceta ni se abrió. `avanzar` (`--fase avanzar --casos desarrollo`) da el paso siguiente como lo haría él:

3. El aprobador DEVUELVE «Pruebas integrales» con una observación y aprueba las demás; el responsable técnico la
   ajusta y el aprobador la aprueba. Con TODO el plan aprobado, el flujo abre la gaceta «Revisión del plan de
   trabajo» y le presenta las 10 actividades al solicitante.
4. En la gaceta, el solicitante comenta «Despliegue en producción» (vuelve al responsable técnico a corregir); las
   demás esperan sus comentarios o el silencio positivo al vencer el periodo.

Ahí se detiene: la gaceta no está resuelta, así que ninguna actividad está en ejecución. Ambas fases son
repetibles: el ticket del solicitante se reutiliza y cada paso solo se da si el ticket está justo antes de él (lo
que alguien ya decidió en la pantalla se conserva).
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.catalogo.demo import avance, contrato
from apps.catalogo.demo.participantes import Participantes

DEVUELTA = "Pruebas integrales"
OBSERVACION_APROBADOR = (
    "Incluyan pruebas de carga del WebSocket de la conversación y de la cola de Celery antes del despliegue."
)
AJUSTE = (
    "Pruebas automatizadas de cada módulo, pruebas de carga del WebSocket de la conversación y de la cola de Celery, "
    "y pruebas de aceptación con usuarios de cada área."
)
OBSERVADA = "Despliegue en producción"
COMENTARIO_SOLICITANTE = (
    "Antes de salir a producción necesitamos una capacitación para los usuarios de cada área y un manual corto."
)


def respuestas_de_ejemplo(campos, hoy, area):
    valores = {
        "nombre_del_desarrollo": "Órbita — plataforma interna de servicios y procesos",
        "area_solicitante": str(area.pk),
        "necesidad": (
            "Centralizar en una sola plataforma los tickets, servicios, procesos, tareas, aprobaciones y gacetas que "
            "hoy se gestionan por correo, hojas de cálculo y chats."
        ),
        "objetivo": (
            "Que cada empleado solicite servicios e inicie procesos desde un único punto, con seguimiento, tiempos "
            "objetivo y trazabilidad completa."
        ),
        "problema": (
            "Las solicitudes se pierden entre canales, no hay responsables claros ni historial, y las aprobaciones "
            "y revisiones se hacen sin control de versiones."
        ),
        "funcionalidades_solicitadas": (
            "Catálogo de servicios, constructor de formularios, Studio de configuración, motor de flujos, tickets "
            "con espacio de trabajo, tareas, aprobaciones, gacetas de revisión, notificaciones y analítica."
        ),
        "usuarios_destinatarios": "Todos los empleados; ejecutores, aprobadores y administradores de cada área.",
        "prioridad": "alta",
        "fecha_requerida": (hoy + timedelta(days=150)).isoformat(),
    }
    return {campo.pk: valores[campo.clave] for campo in campos if campo.clave in valores}


def sumar_habiles(desde, dias):
    from apps.tickets.tiempos import es_dia_habil

    fecha = desde
    while dias > 0:
        fecha += timedelta(days=1)
        if es_dia_habil(fecha):
            dias -= 1
    return fecha


def filas_del_plan(columnas, responsable, hoy):
    """`[{campo_id: valor}]` del plan de ejemplo: el responsable técnico en cada actividad."""
    filas = []
    for spec in contrato.PLAN_ORBITA:
        prevista = sumar_habiles(hoy, spec.inicio)
        valores = {
            contrato.ACT_NOMBRE: spec.nombre, contrato.ACT_DESCRIPCION: spec.descripcion, contrato.ACT_TIPO: spec.tipo,
            contrato.ACT_RESPONSABLE: str(responsable.pk), contrato.ACT_FECHA_PREVISTA: prevista.isoformat(),
            contrato.ACT_FECHA_LIMITE: sumar_habiles(prevista, spec.duracion).isoformat(),
            contrato.ACT_ENTREGABLE: spec.entregable,
        }
        filas.append({columnas[clave].pk: valor for clave, valor in valores.items()})
    return filas


def _ticket_del_solicitante(servicio, solicitante):
    from apps.tickets.models import Ticket

    return (
        Ticket.objects.filter(detalle_servicio__servicio=servicio, solicitante=solicitante)
        .exclude(estado__in=(Ticket.Estado.BORRADOR, Ticket.Estado.CANCELADO)).order_by("pk").first()
    )


def _radicar(servicio, solicitante, area, hoy):
    from apps.tickets.operaciones import crear_borrador, guardar_respuestas_borrador, radicar_ticket

    ticket = crear_borrador(solicitante, servicio)
    respuestas = respuestas_de_ejemplo(servicio.formulario.version_activa.campos.all(), hoy, area)
    guardar_respuestas_borrador(ticket, solicitante, respuestas)
    radicar_ticket(ticket, solicitante)
    ticket.refresh_from_db()
    return ticket


def _paso_principal(ticket):
    from apps.workflows.models import InstanciaWorkflow

    if ticket.instancia_workflow_id is None:
        return None, ""
    ejecucion = avance.ejecucion_en_espera(InstanciaWorkflow.objects.get(pk=ticket.instancia_workflow_id))
    return ejecucion, avance.nombre_del_paso(ejecucion)


def _por_nombre(ticket, nombre):
    from apps.tickets import parrilla as parrilla_ops

    return next(
        (e for e in parrilla_ops.elementos_en_alcance(parrilla_ops.parrilla_de(ticket))
         if parrilla_ops.valores_de(e).get(contrato.ACT_NOMBRE) == nombre),
        None,
    )


def _aprobar_el_plan(ticket, p, lineas):
    """Cada actividad que espera al aprobador se decide como en la pantalla: «Pruebas integrales» se devuelve UNA
    vez con una observación (el responsable técnico la ajusta y vuelve) y las demás se aprueban. Lo que ya tiene
    decisión o está en otro paso se conserva."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

    aprobadas = 0
    for _ in range(3):  # devolver → ajustar → aprobar
        for hija in InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_PLAN,
        ).select_related("elemento").order_by("elemento__numero"):
            ejecucion = avance.ejecucion_en_espera(hija)
            paso = avance.nombre_del_paso(ejecucion)
            nombre = parrilla_ops.valores_de(hija.elemento).get(contrato.ACT_NOMBRE)
            if paso == "Aprobación de la actividad":
                ya_ajustada = InstanciaEtapa.objects.filter(
                    instancia_workflow=hija, bloque_operativo__nombre="Ajustar la actividad"
                ).exists()
                if nombre == DEVUELTA and not ya_ajustada:
                    avance.decidir(avance.esquema_del_paso(ejecucion), "DEVUELTA", OBSERVACION_APROBADOR)
                    lineas.append(f"{p.aprobador.email} devolvió «{nombre}» con una observación.")
                else:
                    avance.decidir(avance.esquema_del_paso(ejecucion), "APROBADA", "Actividad aprobada.")
                    aprobadas += 1
            elif paso == "Ajustar la actividad":
                tarea = avance.tarea_de(ejecucion)
                actor = avance.responsable_de(tarea)
                columnas = {c.clave: c for c in parrilla_ops.columnas_de(hija.elemento.parrilla)}
                avance.completar(
                    tarea, actor, elemento=hija.elemento, valores={columnas[contrato.ACT_DESCRIPCION].pk: AJUSTE},
                )
                lineas.append(f"{actor.email} ajustó «{nombre}» y la reenvió a aprobación.")
    if aprobadas:
        lineas.append(f"{p.aprobador.email} aprobó {aprobadas} actividades del plan.")


def _comentar_en_gaceta(ticket, p, lineas):
    from apps.gacetas import operaciones as gacetas_ops
    from apps.gacetas.autorizacion import puede_observar
    from apps.gacetas.models import ObservacionGaceta, ParticipacionItemGaceta

    if ObservacionGaceta.objects.filter(participacion__gaceta__ticket=ticket).exists():
        return  # ya hubo revisión: se conserva lo que hicieron las personas
    participacion = ParticipacionItemGaceta.objects.filter(
        gaceta__ticket=ticket, elemento=_por_nombre(ticket, OBSERVADA)
    ).first()
    if participacion is None or participacion.estado != ParticipacionItemGaceta.Estado.EN_REVISION:
        return
    revisor = next(
        (o for tipo, o in p.revisores if tipo == "USUARIO" and o.is_active and puede_observar(o, participacion)), None
    )
    if revisor is None:
        lineas.append("La gaceta no tiene un revisor persona que pueda comentar: no se registró el comentario.")
        return
    gacetas_ops.observar(participacion, revisor, COMENTARIO_SOLICITANTE)
    lineas.append(f"{revisor.email} comentó «{OBSERVADA}» en la gaceta: vuelve al responsable técnico.")


def ticket_de_ejemplo(recurso, *, solicitante, area, hoy=None):
    """Crea (o continúa) el ticket de ejemplo del solicitante hasta el plan enviado al aprobador. Devuelve el
    informe."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.models import Ticket
    from apps.tickets.operaciones import iniciar_atencion_ticket, tomar_ticket
    from apps.workflows import recorridos

    servicio = recurso.servicio
    if not servicio.activo:
        return ["Primero publica el caso (--fase publicar --casos desarrollo)."]
    if solicitante is None:
        return ["Indica --solicitante: la persona que solicita el desarrollo."]
    p = Participantes.desde_parametros(recurso.parametros)
    if p.desarrollador is None or not p.desarrollador.is_active or p.aprobador is None or not p.aprobador.is_active:
        raise ValidationError("El responsable técnico o el aprobador elegidos al preparar ya no existen o no están activos.")
    if solicitante == p.desarrollador:
        raise ValidationError("El solicitante no puede ser el responsable técnico (quien solicita no atiende su ticket).")
    hoy = hoy or timezone.localdate()
    lineas = []
    ticket = _ticket_del_solicitante(servicio, solicitante)
    if ticket is None:
        if area is None:
            return ["El formulario pide el «Área solicitante»: indica --area-ejemplo (o --area-visibilidad al preparar)."]
        ticket = _radicar(servicio, solicitante, area, hoy)
        lineas.append(f"{solicitante.email} solicitó el desarrollo: {ticket.codigo}.")
    else:
        lineas.append(f"Se reutiliza el ticket {ticket.codigo} de {solicitante.email} (no se crea otro).")
    if ticket.estado == Ticket.Estado.RADICADO:
        # Como en la Cola: lo dirigido a una persona se inicia; lo que espera en la Cola, se toma.
        if ticket.usuario_responsable_id == p.desarrollador.pk:
            iniciar_atencion_ticket(ticket, p.desarrollador)
        else:
            tomar_ticket(ticket, p.desarrollador)
        ticket.refresh_from_db()
        lineas.append(f"{p.desarrollador.email} tomó el ticket e inició la atención.")

    ejecucion, paso = _paso_principal(ticket)
    if paso == "Evaluación técnica":
        tarea = avance.tarea_de(ejecucion)
        avance.completar(tarea, avance.responsable_de(tarea))
        lineas.append(f"{avance.responsable_de(tarea).email} revisó la solicitud (viabilidad, alcance y riesgos).")
        ejecucion, paso = _paso_principal(ticket)
    parrilla = parrilla_ops.parrilla_de(ticket)
    if (
        paso == contrato.RECORRIDO_PLAN and not parrilla.alcance_cerrado
        and recorridos.recorrido_al_enviar(ticket, parrilla) is not None
    ):
        columnas = {c.clave: c for c in parrilla_ops.columnas_de(parrilla)}
        nuevos = [] if parrilla.elementos.exists() else filas_del_plan(columnas, p.desarrollador, hoy)
        enviados = recorridos.enviar_conjunto(ticket, parrilla, p.desarrollador, nuevos=nuevos)
        lineas.append(
            f"{p.desarrollador.email} armó el plan de {len(enviados)} actividades y lo envió completo a "
            f"aprobación de {p.aprobador.email} (alcance cerrado)."
        )
    lineas += resumen(ticket)
    return lineas


def avanzar(recurso):
    """Fase `avanzar`: el aprobador decide el plan (una devuelta, ajuste y aprobación de todo), el flujo abre la
    gaceta y el solicitante comenta una actividad. Devuelve el informe."""
    from apps.tickets import parrilla as parrilla_ops

    servicio = recurso.servicio
    if not servicio.activo:
        return ["Primero publica el caso (--fase publicar --casos desarrollo)."]
    from apps.core.models import Usuario

    solicitante = Usuario.objects.por_referencia(contrato.CONTRATOS[recurso.caso].solicitante).first()
    ticket = _ticket_del_solicitante(servicio, solicitante) if solicitante is not None else None
    if ticket is None:
        return ["Primero crea el ticket de ejemplo (--fase ejemplos --casos desarrollo)."]
    p = Participantes.desde_parametros(recurso.parametros)
    lineas = [f"Ticket {ticket.codigo} (no se crea otro)."]
    _, paso = _paso_principal(ticket)
    if paso == contrato.RECORRIDO_PLAN and parrilla_ops.parrilla_de(ticket).alcance_cerrado:
        _aprobar_el_plan(ticket, p, lineas)
        _, paso = _paso_principal(ticket)
    elif paso == contrato.RECORRIDO_PLAN:
        lineas.append("El plan todavía no se envió a aprobación: corre antes --fase ejemplos.")
    if paso == contrato.REVISION_PLAN:
        _comentar_en_gaceta(ticket, p, lineas)
    lineas += resumen(ticket)
    return lineas


def resumen(ticket):
    """Dónde está cada parte del ticket de ejemplo (solo lee)."""
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
    from apps.workflows.models import InstanciaWorkflow

    ticket.refresh_from_db()
    _, paso = _paso_principal(ticket)
    lineas = [f"Ticket {ticket.codigo}: {ticket.get_estado_display().lower()}; el flujo espera «{paso or '—'}»."]
    gaceta = GacetaTicket.objects.filter(ticket=ticket).first()
    if gaceta is not None:
        conteo = {}
        for estado in ParticipacionItemGaceta.objects.filter(gaceta=gaceta).values_list("estado", flat=True):
            conteo[estado] = conteo.get(estado, 0) + 1
        detalle = ", ".join(f"{k.lower().replace('_', ' ')} {v}" for k, v in sorted(conteo.items())) or "sin actividades"
        lineas.append(f"Gaceta «{gaceta.nombre}» {gaceta.get_estado_display().lower()}: {detalle}.")
    en_ejecucion = InstanciaWorkflow.objects.filter(
        instancia_padre_id=ticket.instancia_workflow_id,
        ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_EJECUCION,
    ).count() if ticket.instancia_workflow_id else 0
    if gaceta is None or gaceta.estado == GacetaTicket.Estado.PENDIENTE:
        por_aprobar = InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_PLAN,
            estado=InstanciaWorkflow.Estado.EN_ESPERA,
        ).count() if ticket.instancia_workflow_id else 0
        lineas.append(f"Actividades del plan esperando aprobación: {por_aprobar} (la gaceta se abre con todo aprobado).")
    lineas.append(
        f"Actividades en ejecución: {en_ejecucion} (la ejecución empieza solo cuando la gaceta del plan se resuelva)."
    )
    return lineas


# --- Demo de CIERRE (`--fase cierre --casos desarrollo|diseno`) ------------------------------------------------------
# Lleva el ticket de ejemplo hasta justo ANTES de resolverlo, para ver cómo se ve el cierre: el plan aprobado, la
# gaceta del plan RESUELTA (la corrección comentada se reenvía y el periodo se cierra con silencio positivo), cada
# actividad ejecutada y su resultado aprobado (en «Diseño de nuevas soluciones», la primera se publica a los
# interesados un día y se acepta al vencer su ventana; las demás no se publican), el acta de entrega adjunta y la
# validación general aprobada. El flujo queda COMPLETADO y el ticket EN ATENCIÓN: resolverlo (y cerrarlo) se hace a
# mano en el espacio del ticket, que es lo que se quiere mostrar. Cada paso solo se da si el ticket está justo antes
# de él: repetir la fase no duplica nada.

ACTA = "Acta de entrega del desarrollo"


def _resolver_gaceta_del_plan(ticket, p, lineas):
    from apps.gacetas import operaciones as gacetas_ops
    from apps.gacetas.autorizacion import puede_corregir
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta

    gaceta = GacetaTicket.objects.filter(ticket=ticket, nombre=contrato.DESARROLLO_CASO.gaceta.nombre).first()
    if gaceta is None or gaceta.estado in GacetaTicket.ESTADOS_FINALES:
        return
    for participacion in ParticipacionItemGaceta.objects.filter(
        gaceta=gaceta, estado=ParticipacionItemGaceta.Estado.EN_CORRECCION
    ).select_related("gaceta"):
        if puede_corregir(p.desarrollador, participacion):
            gacetas_ops.reenviar(participacion, p.desarrollador, "Se agregó la capacitación y el manual corto.")
            lineas.append(f"{p.desarrollador.email} reenvió la corrección pedida en la gaceta del plan.")
    gaceta.refresh_from_db()
    if gaceta.estado == GacetaTicket.Estado.ABIERTA:
        gacetas_ops.cerrar_periodo(gaceta, p.desarrollador)
        lineas.append("Se cerró el periodo de la gaceta del plan: lo que no tenía comentarios quedó aceptado.")
    gaceta.refresh_from_db()
    if gaceta.estado == GacetaTicket.Estado.PERIODO_CERRADO:
        gacetas_ops.resolver(gaceta, p.desarrollador, "Plan revisado por el solicitante.")
    gaceta.refresh_from_db()
    lineas.append(f"Gaceta «{gaceta.nombre}»: {gaceta.get_estado_display().lower()}.")


def _ejecutar_actividades(ticket, p, lineas, *, publicar_primera):
    from apps.gacetas import operaciones as gacetas_ops
    from apps.gacetas.models import ParticipacionItemGaceta
    from apps.workflows.models import InstanciaWorkflow

    ejecutadas = aprobadas = 0
    publicada = False
    for _ in range(4):
        for hija in InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_EJECUCION,
        ).select_related("elemento").order_by("elemento__numero"):
            ejecucion = avance.ejecucion_en_espera(hija)
            paso = avance.nombre_del_paso(ejecucion)
            if paso == "Ejecución de la actividad":
                tarea = avance.tarea_de(ejecucion)
                avance.completar(tarea, avance.responsable_de(tarea), elemento=hija.elemento)
                ejecutadas += 1
            elif paso == contrato.APROBACION_RESULTADO:
                eleccion = None
                if publicar_primera is not None:
                    eleccion = {"publicar": publicar_primera and not publicada, "dias": 1}
                    publicada = publicada or bool(eleccion["publicar"])
                avance.decidir(avance.esquema_del_paso(ejecucion), "APROBADA", "Resultado aprobado.", publicacion=eleccion)
                aprobadas += 1
            elif paso == contrato.PUBLICACION_INTERESADOS:
                # La ventana de un día se da por vencida para la demostración: sin comentarios, queda aceptada.
                for participacion in ParticipacionItemGaceta.objects.filter(
                    elemento=hija.elemento, estado=ParticipacionItemGaceta.Estado.EN_REVISION,
                    vence_revision_en__isnull=False,
                ):
                    ParticipacionItemGaceta.objects.filter(pk=participacion.pk).update(vence_revision_en=timezone.now())
                    gacetas_ops.cerrar_ventana_de_item(participacion)
                    lineas.append("Una actividad se publicó a los interesados un día; sin comentarios, quedó aceptada.")
    if ejecutadas or aprobadas:
        lineas.append(f"Se ejecutaron {ejecutadas} actividades y se aprobaron {aprobadas} resultados.")


def _entregar_y_validar(ticket, p, lineas):
    from apps.tickets.entregables import adjuntar_archivo_entregable

    ejecucion, paso = _paso_principal(ticket)
    if paso == "Entrega del desarrollo":
        entregable = ticket.entregables.filter(definicion__nombre=ACTA).first()
        if entregable is not None:
            archivo = SimpleUploadedFile("acta_de_entrega.txt", b"Acta de entrega de la solucion (archivo de prueba de la demo).",
                                         content_type="text/plain")
            adjuntar_archivo_entregable(entregable, p.desarrollador, archivo)
            lineas.append(f"{p.desarrollador.email} adjuntó el acta de entrega.")
        ejecucion, paso = _paso_principal(ticket)
    if paso == "Validación general":
        avance.decidir(avance.esquema_del_paso(ejecucion), "APROBADA", "Solución validada.")
        lineas.append(f"{p.aprobador.email} aprobó la validación general: el flujo terminó.")


def hasta_el_cierre(recurso):
    """Fase `cierre`: lleva el ticket de ejemplo hasta quedar listo para resolverse. Devuelve el informe."""
    from apps.core.models import Usuario
    from apps.tickets import parrilla as parrilla_ops

    servicio = recurso.servicio
    if not servicio.activo:
        return ["Primero publica el caso (--fase publicar)."]
    solicitante = Usuario.objects.por_referencia(contrato.CONTRATOS[recurso.caso].solicitante).first()
    ticket = _ticket_del_solicitante(servicio, solicitante) if solicitante is not None else None
    if ticket is None:
        return ["Primero crea el ticket de ejemplo (--fase ejemplos)."]
    p = Participantes.desde_parametros(recurso.parametros)
    lineas = [f"Ticket {ticket.codigo} (no se crea otro)."]
    _, paso = _paso_principal(ticket)
    if paso == contrato.RECORRIDO_PLAN and parrilla_ops.parrilla_de(ticket).alcance_cerrado:
        _aprobar_el_plan(ticket, p, lineas)
        _, paso = _paso_principal(ticket)
    if paso == contrato.REVISION_PLAN:
        _comentar_en_gaceta(ticket, p, lineas)
        _resolver_gaceta_del_plan(ticket, p, lineas)
        _, paso = _paso_principal(ticket)
    if paso == contrato.RECORRIDO_EJECUCION:
        _ejecutar_actividades(
            ticket, p, lineas, publicar_primera=True if recurso.caso == contrato.DISENO else None,
        )
    _entregar_y_validar(ticket, p, lineas)
    ticket.refresh_from_db()
    lineas.append(
        f"Ticket {ticket.codigo}: {ticket.get_estado_display().lower()}. Entra como {p.desarrollador.email} "
        "al espacio del ticket › Resumen › Finalizar para resolverlo y ver el cierre."
    )
    return lineas
