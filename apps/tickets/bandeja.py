"""«Mi trabajo» agrupado por TICKET (UX-1, incremento 1).

Composición de LECTURA sobre consultas que ya existen (`apps.tareas.consultas`, `apps.aprobaciones.consultas`,
`revision_elementos.revisiones_pendientes_para`, `parrilla.colecciones_de_participante`,
`trabajo.tickets_a_cargo`, `gacetas.consultas.gacetas_visibles_para`): no hay modelo propio ni regla nueva de
quién ve qué. Cada pendiente se ubica en su Ticket con `apps.tickets.vinculos` (consultas por lote) y la bandeja
muestra UNA entrada por Ticket con todo lo que la persona tiene en él:

- «Requieren tu atención»: hay algo que la persona puede hacer ahora (completar o tomar una tarea, decidir una
  aprobación cuando le toca, revisar piezas de una gaceta o elementos de un conjunto, escribir el entregable
  que el flujo espera, corregir elementos, resolver o entregar el ticket que atiende).
- «En seguimiento»: participa (responsable, participante de una colección, aprobador que espera su turno,
  revisor de una gaceta sin piezas en revisión…) pero ahora no tiene una acción inmediata.

Lo que no pertenece a ningún Ticket (tareas o aprobaciones independientes) sigue apareciendo aparte, con su
enlace al detalle técnico de siempre. Los enlaces técnicos de cada pendiente se conservan para no romper los
accesos directos; la entrada habitual es «Abrir», que lleva al espacio del Ticket (`tickets:espacio`)."""

from dataclasses import dataclass, field
from datetime import datetime, time

from django.urls import reverse
from django.utils import timezone

from apps.tareas.models import Tarea
from apps.tickets import vinculos
from apps.tickets.models import Ticket


@dataclass
class Pendiente:
    """Una cosa concreta que la persona tiene en un Ticket. `clave` es la misma que usa «Mi actividad» del
    espacio del Ticket para destacarla (`?accion=`)."""

    clave: str
    tipo: str
    titulo: str
    detalle: str = ""
    estado: str = ""
    url_tecnica: str = ""
    vence: object = None
    por_tomar: bool = False
    accion: bool = True

    @property
    def vencida(self):
        return self.accion and self.vence is not None and _como_instante(self.vence) < timezone.now()


@dataclass
class Grupo:
    ticket: Ticket
    pendientes: list = field(default_factory=list)
    participacion: list = field(default_factory=list)

    def agregar(self, pendiente, rol):
        self.pendientes.append(pendiente)
        if rol and rol not in self.participacion:
            self.participacion.append(rol)

    @property
    def acciones(self):
        return [p for p in self.pendientes if p.accion]

    @property
    def en_seguimiento(self):
        return [p for p in self.pendientes if not p.accion]

    @property
    def requiere_atencion(self):
        return bool(self.acciones)

    @property
    def proximo_vencimiento(self):
        fechas = [p.vence for p in (self.acciones or self.pendientes) if p.vence is not None]
        return min(fechas, key=_como_instante) if fechas else None

    @property
    def vencido(self):
        proximo = self.proximo_vencimiento
        return proximo is not None and self.requiere_atencion and _como_instante(proximo) < timezone.now()

    @property
    def siguiente(self):
        """Lo que la fila de la bandeja muestra como «siguiente acción»: la acción que vence antes (o la primera)
        y, si no hay acciones, lo primero que la persona sigue."""
        acciones = self.acciones
        if acciones:
            return min(acciones, key=lambda p: (p.vence is None, _como_instante(p.vence) if p.vence is not None else 0))
        return self.pendientes[0] if self.pendientes else None

    def coincide(self, texto):
        """Búsqueda de la bandeja (UX-2): código, nombre del servicio, etiqueta de la ejecución o título de un
        pendiente. Solo filtra lo que ya se construyó; no consulta nada."""
        if not texto:
            return True
        texto = texto.casefold()
        campos = [self.ticket.codigo or "", self.ticket.detalle_servicio.servicio.nombre, self.ticket.etiqueta or ""]
        campos += [p.titulo for p in self.pendientes]
        return any(texto in campo.casefold() for campo in campos)

    @property
    def url(self):
        destino = reverse("tickets:espacio", args=[self.ticket.pk])
        acciones = self.acciones
        return f"{destino}?accion={acciones[0].clave}" if len(acciones) == 1 else destino


def _como_instante(valor):
    """Fechas (`date`) y fechas-hora en una misma escala para ordenar: una fecha vence al final del día local."""
    if isinstance(valor, datetime):
        return valor if timezone.is_aware(valor) else timezone.make_aware(valor)
    return timezone.make_aware(datetime.combine(valor, time.max))


_ROL_TAREA = {"FLUJO": "Actividades", "ELEMENTO": "Tareas por elemento", "GACETA": "Corrección"}
_ROL_APROBACION = {"FLUJO": "Aprobación", "GACETA": "Aprobación de gaceta", "PRORROGA": "Aprobación de prórroga"}


def _pendiente_de_tarea(tarea):
    abierta = tarea.estado not in Tarea.ESTADOS_CERRADOS
    por_tomar = abierta and tarea.usuario_responsable_id is None
    return Pendiente(
        clave=f"tarea-{tarea.pk}",
        tipo="tarea",
        titulo=tarea.titulo,
        detalle="Subtarea" if tarea.tarea_padre_id else "Tarea",
        estado=tarea.get_estado_display(),
        url_tecnica=reverse("tareas:detalle", args=[tarea.pk]),
        vence=tarea.fecha_limite,
        por_tomar=por_tomar,
        accion=abierta,
    )


def _pendiente_de_aprobacion(usuario, aprobacion):
    from apps.aprobaciones.autorizacion import puede_aprobar

    le_toca = puede_aprobar(usuario, aprobacion)
    return Pendiente(
        clave=f"aprobacion-{aprobacion.pk}",
        tipo="aprobacion",
        titulo=f"Aprobación #{aprobacion.pk}",
        detalle="Aprobación" if le_toca else "Aprobación · espera su turno",
        estado=aprobacion.get_estado_display(),
        url_tecnica=reverse("aprobaciones:detalle", args=[aprobacion.pk]),
        accion=le_toca,
    )


def _acciones_del_responsable(usuario, ticket):
    """Lo que el responsable INDIVIDUAL de un ticket en atención puede hacer ahora en el ticket mismo (no en una
    tarea o aprobación, que se cuentan aparte). Lee el punto vigente del flujo con `trabajo.contexto_trabajo_actual`;
    las acciones las sigue autorizando cada operación."""
    from apps.tickets import datos_inicio
    from apps.tickets import trabajo as trabajo_ops
    from apps.tickets.autorizacion import puede_diligenciar_datos_inicio

    resultado = []
    if datos_inicio.requiere_diligenciamiento(ticket) and puede_diligenciar_datos_inicio(usuario, ticket):
        resultado.append(Pendiente(clave="datos-inicio", tipo="datos_inicio", titulo="Completar los datos de inicio"))
    # UX-4: si el flujo recibe ítems al ENVIARLOS, preparar y enviar es trabajo de quien opera la colección.
    from apps.tickets.autorizacion import puede_operar_coleccion
    from apps.tickets.parrilla import parrillas_de
    from apps.workflows import recorridos

    for parrilla in parrillas_de(ticket):
        ejecucion = recorridos.recorrido_al_enviar(ticket, parrilla)
        if ejecucion is not None and puede_operar_coleccion(usuario, parrilla, ticket):
            pendientes = len(recorridos.borradores_de(ejecucion, parrilla))
            if parrilla.alcance_cerrado and not pendientes:
                continue  # ya se envió todo
            resultado.append(Pendiente(
                clave=f"borradores-{parrilla.clave}", tipo="borradores",
                titulo=f"Preparar y enviar: {(parrilla.nombre_elemento or 'ítems').lower()}",
                detalle=f"{pendientes} por enviar" if pendientes else "Agrega o cierra el alcance",
            ))
    datos = trabajo_ops.contexto_trabajo_actual(usuario, ticket)
    trabajo = datos["trabajo_actual"]
    if ticket.instancia_workflow_id is None:
        resultado.append(Pendiente(
            clave="atencion", tipo="atencion", titulo="Atender y entregar", detalle="Sin flujo: atención directa",
            vence=ticket.fecha_objetivo_vigente,
        ))
    elif trabajo is not None and trabajo["terminado"]:
        resultado.append(Pendiente(
            clave="cierre", tipo="cierre", titulo="Resolver o entregar", detalle="El flujo terminó",
            vence=ticket.fecha_objetivo_vigente,
        ))
    elif trabajo is not None and trabajo.get("entregable") is not None:
        entregable = trabajo["entregable"]
        resultado.append(Pendiente(
            clave=f"entregable-{entregable.pk}", tipo="entregable", titulo=f"Completar «{entregable.nombre}»",
            detalle=trabajo["nombre"], vence=ticket.fecha_objetivo_vigente,
        ))
    elif trabajo is not None and trabajo.get("correccion"):
        resultado.append(Pendiente(
            clave="correccion", tipo="correccion", titulo="Corregir los elementos", detalle=trabajo["nombre"],
        ))
    elif trabajo is not None:
        resultado.append(Pendiente(
            clave="seguimiento", tipo="seguimiento", titulo=trabajo["nombre"],
            detalle="Detenido: requiere revisión" if trabajo["con_error"] else (trabajo["pendiente"] or trabajo["tipo"]),
            vence=ticket.fecha_objetivo_vigente, accion=False,
        ))
    return resultado


def _gacetas_de(usuario):
    """Gacetas activas de las que la persona es revisora o aprobadora: las que tienen piezas en revisión con el
    periodo abierto son trabajo suyo (puede observar); las demás, seguimiento."""
    from apps.gacetas import autorizacion as gaceta_aut
    from apps.gacetas.consultas import gacetas_visibles_para
    from apps.gacetas.models import ParticipacionItemGaceta

    resultado = []
    for gaceta in gacetas_visibles_para(usuario):
        revisor = gaceta_aut.es_revisor(usuario, gaceta)
        en_revision = 0
        if revisor and gaceta.periodo_abierto:
            en_revision = ParticipacionItemGaceta.objects.filter(
                gaceta=gaceta, estado=ParticipacionItemGaceta.Estado.EN_REVISION
            ).count()
        resultado.append((gaceta, Pendiente(
            clave=f"gaceta-{gaceta.pk}", tipo="gaceta", titulo=gaceta.nombre,
            detalle="Revisión de gaceta",
            estado=f"{en_revision} pieza{'s' if en_revision != 1 else ''} en revisión" if en_revision else gaceta.get_estado_display(),
            url_tecnica=reverse("tickets:espacio", args=[gaceta.ticket_id]) + f"#gaceta-{gaceta.pk}",
            vence=gaceta.vence_revision_en if en_revision else None,
            accion=bool(en_revision),
        ), "Revisión de gaceta" if revisor else "Gaceta"))
    return resultado


def construir(usuario, *, tareas, aprobaciones, revisiones, colecciones, a_cargo):
    """Agrupa los pendientes de `usuario` por Ticket. Recibe las listas ya calculadas por la vista (las mismas
    que alimentan las pestañas técnicas) y devuelve `{"atencion", "seguimiento", "sueltos"}`: grupos que requieren
    atención (ordenados por próximo vencimiento y llegada), grupos en seguimiento y tareas/aprobaciones sin Ticket
    (`(tipo, objeto)` para que la vista las pinte como siempre)."""
    grupos = {}
    sueltos = []
    tareas_abiertas = [t for t in tareas if t.estado not in Tarea.ESTADOS_CERRADOS]
    origen_tareas = vinculos.origen_de_tareas([t.pk for t in tareas_abiertas])
    origen_aprobaciones = vinculos.origen_de_aprobaciones(aprobaciones)
    por_ticket = {}

    def poner(ticket_id, pendiente, rol):
        por_ticket.setdefault(ticket_id, []).append((pendiente, rol))

    for tarea in tareas_abiertas:
        origen = origen_tareas.get(tarea.pk)
        if origen is None:
            sueltos.append(("tarea", tarea))
            continue
        poner(origen["ticket_id"], _pendiente_de_tarea(tarea), _ROL_TAREA[origen["origen"]])
    for aprobacion in aprobaciones:
        origen = origen_aprobaciones.get(aprobacion.pk)
        if origen is None:
            sueltos.append(("aprobacion", aprobacion))
            continue
        poner(origen["ticket_id"], _pendiente_de_aprobacion(usuario, aprobacion), _ROL_APROBACION[origen["origen"]])
    for revision in revisiones:
        entregable, ticket, resumen = revision["entregable"], revision["ticket"], revision["resumen"]
        poner(ticket.pk, Pendiente(
            clave=f"revision-{entregable.pk}", tipo="revision", titulo=entregable.nombre,
            detalle="Revisión de elementos", estado=f"{resumen.progreso} aprobados",
            url_tecnica=reverse("tickets:revision_conjunto", args=[ticket.pk, entregable.pk]),
        ), "Revisión de elementos")
    for parrilla in colecciones:
        poner(parrilla.ticket_id, Pendiente(
            clave=f"coleccion-{parrilla.clave}", tipo="coleccion", titulo=parrilla.nombre_elemento or "Colección",
            detalle="Participas en la colección", estado="Puedes agregar y editar ítems",
            url_tecnica=reverse("tickets:coleccion", args=[parrilla.ticket_id, parrilla.clave]), accion=False,
        ), "Participante de colección")
    for gaceta, pendiente, rol in _gacetas_de(usuario):
        poner(gaceta.ticket_id, pendiente, rol)
    a_cargo = list(a_cargo)
    for ticket in a_cargo:
        for pendiente in _acciones_del_responsable(usuario, ticket):
            poner(ticket.pk, pendiente, "Responsable")
        if ticket.pk not in por_ticket:
            por_ticket[ticket.pk] = [(None, "Responsable")]

    tickets = {
        t.pk: t for t in Ticket.objects.filter(pk__in=por_ticket).select_related(
            "detalle_servicio__servicio", "usuario_responsable", "equipo_responsable"
        )
    }
    for ticket_id, entradas in por_ticket.items():
        ticket = tickets.get(ticket_id)
        if ticket is None:
            continue
        g = Grupo(ticket=ticket)
        for pendiente, rol in entradas:
            if pendiente is None:
                if rol not in g.participacion:
                    g.participacion.append(rol)
                continue
            g.agregar(pendiente, rol)
        if ticket.usuario_responsable_id == usuario.pk and "Responsable" not in g.participacion:
            g.participacion.insert(0, "Responsable")
        grupos[ticket_id] = g

    lista = list(grupos.values())
    atencion = sorted(
        (g for g in lista if g.requiere_atencion),
        key=lambda g: (
            g.proximo_vencimiento is None,
            _como_instante(g.proximo_vencimiento) if g.proximo_vencimiento is not None else timezone.now(),
            g.ticket.radicado_en or g.ticket.creado_en,
        ),
    )
    seguimiento = sorted(
        (g for g in lista if not g.requiere_atencion),
        key=lambda g: g.ticket.radicado_en or g.ticket.creado_en,
    )
    return {"atencion": atencion, "seguimiento": seguimiento, "sueltos": sueltos}


def hay_revision_de_gaceta(usuario):
    """¿Tiene la persona piezas por revisar en alguna gaceta? (navegación: el destino Trabajo)."""
    return any(p.accion for _, p, _ in _gacetas_de(usuario))
