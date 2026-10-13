"""Revisión individual de los elementos de un Ticket por un entregable de CONJUNTO — Sprint 4.G2C1.

Un `EntregableTicket` de tipo ELEMENTOS controla el conjunto de `ElementoParrilla` vigentes de la ÚNICA
parrilla del Ticket y registra una revisión por elemento. No crea Tickets, Workflows ni
`EsquemaAprobacion` por fila, y no usa `apps.aprobaciones`.

Qué se copia al nacer el Ticket (`materializar_conjunto`, dentro de la materialización de entregables):
las columnas revisadas (los `Campo` de la versión CONGELADA, resueltos por clave estable) y los
revisores. Desde ahí el Ticket no depende de la configuración del Proceso.

Decisiones. Cada aprobación o devolución es una fila `RevisionElemento` nueva (append-only). El estado
VIGENTE de un elemento para el entregable se CALCULA comparando la última decisión con el contenido
actual de las columnas revisadas (`huella`, SHA-256 de `parrilla.representacion_de_valores`):

- sin decisiones → PENDIENTE;
- última decisión con la huella de hoy → APROBADA o DEVUELTA;
- última decisión con otra huella (las columnas revisadas cambiaron) → PENDIENTE: una aprobación queda
  invalidada y una devolución, corregida, y hace falta una nueva revisión. El historial no se toca.

Cambiar columnas AJENAS a las revisadas no altera la huella, y cada elemento se evalúa por separado:
las demás filas conservan sus decisiones.

Agregado. El entregable está satisfecho solo si hay al menos un elemento vigente y TODOS están
APROBADOS y completos respecto de sus columnas revisadas. Un conjunto vacío no lo está. Retirar un
elemento lo excluye del agregado (conserva su historial); se rechaza si el flujo ya consumió el
entregable (`consumido_por_flujo`). `EntregableTicket.satisfecho` sigue siendo el único contrato que
lee el flujo; los avisos al flujo reutilizan el de 4.B1 sin cambiar el motor.

Integración con el Workflow (4.G2C2). El flujo SOLO lee `EntregableTicket.satisfecho` (aviso existente de
4.B1, `avisar_flujo`); no hay otro motor de continuación. Un entregable está CONSUMIDO cuando un bloque
ENTREGABLE del flujo que lo referencia ya se completó y el flujo no vuelve a esperarlo
(`consumido_por_flujo`). Desde entonces es historia: no se aprueba ni se devuelve, no se agregan ni
retiran elementos y no cambian sus columnas revisadas (las columnas ajenas siguen editables con las
autorizaciones de siempre). Antes del consumo todo sigue abierto: se corrige lo devuelto, se conservan las
aprobaciones de lo que no cambió y el flujo continúa cuando el conjunto queda satisfecho. Además solo se
revisa en el turno del entregable: si un bloque del flujo lo referencia, el flujo debe estar esperándolo
(`motivo_no_revisable`); no se revisa por adelantado.

Autorización. Editar elementos y revisarlos son permisos distintos: edita el responsable individual
(`puede_editar_parrilla`); revisa quien esté configurado como revisor (persona o equipo,
`puede_revisar_conjunto`). El responsable NO revisa por serlo. Toda decisión revalida bajo lock en
el orden del motor: instancia de Workflow (si hay) → Ticket → parrilla.
"""

import hashlib
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, TextChoices
from django.utils import timezone

from apps.catalogo.models import DefinicionEntregable
from apps.core.auditoria import registrar_evento
from apps.core.models import MiembroEquipo, RegistroAuditoria
from apps.tickets import parrilla as parrilla_ops
from apps.tickets.autorizacion import puede_revisar_conjunto
from apps.tickets.models import (
    ColumnaRevisadaEntregable,
    EntregableTicket,
    ParrillaTicket,
    RevisionElemento,
    RevisorEntregable,
    Ticket,
)
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

TIPO = DefinicionEntregable.Tipo.ELEMENTOS


class Estado(TextChoices):
    """Estado VIGENTE de un elemento para un entregable (calculado; ver el docstring del módulo)."""

    PENDIENTE = "PENDIENTE", "Pendiente"
    APROBADA = "APROBADA", "Aprobada"
    DEVUELTA = "DEVUELTA", "Devuelta"


# --- Snapshot al nacer el Ticket -----------------------------------------------------


def materializar_conjunto(entregable, definicion):
    """Copia al Ticket las columnas revisadas y los revisores de `definicion` (ELEMENTOS). Lo llama
    `entregables.materializar_entregables` dentro de su transacción, con el entregable recién creado
    y la parrilla del Ticket ya congelada. Si la configuración no se puede cumplir para este Ticket
    se rechaza: un Ticket nunca nace con una revisión imposible."""
    parrilla = ParrillaTicket.objects.select_related("formulario_version").filter(ticket_id=entregable.ticket_id).first()
    if parrilla is None:
        raise ValidationError(
            f"El entregable «{definicion.nombre}» revisa los elementos del proceso, pero el proceso no tiene "
            "una estructura de elementos activa."
        )
    campos = {c.clave: c for c in parrilla.formulario_version.campos.all()}
    claves = list(definicion.columnas_revisadas.values_list("clave", flat=True))
    faltan = [c for c in claves if c not in campos]
    if not claves or faltan:
        raise ValidationError(
            f"El entregable «{definicion.nombre}» revisa columnas que la estructura de elementos activa no "
            "tiene" + (": " + ", ".join(faltan) if faltan else " (no hay ninguna configurada)") + "."
        )
    revisores = list(definicion.revisores.all())
    if not revisores:
        raise ValidationError(f"El entregable «{definicion.nombre}» no tiene quién lo revise.")
    for clave in claves:
        ColumnaRevisadaEntregable.objects.create(entregable=entregable, campo=campos[clave])
    for revisor in revisores:
        RevisorEntregable.objects.create(entregable=entregable, usuario=revisor.usuario, equipo=revisor.equipo)


# --- Consulta y huella -----------------------------------------------------------------


def columnas_revisadas_de(entregable):
    """Los `Campo` que se revisan, en el orden de la estructura."""
    return [
        c.campo for c in entregable.columnas_revisadas.select_related("campo").order_by("campo__orden", "campo_id")
    ]


def _huella(contenido):
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


def contenido_y_huella(elemento, campos):
    """`(representación canónica, huella)` de las columnas revisadas del elemento ahora mismo."""
    contenido = parrilla_ops.representacion_de_valores(elemento, campos)
    return contenido, _huella(contenido)


def historial_de(entregable, elemento=None):
    """Decisiones registradas, de la más reciente a la más antigua (todas, nunca solo la vigente)."""
    consulta = entregable.revisiones.select_related("revisor", "elemento")
    if elemento is not None:
        consulta = consulta.filter(elemento_id=elemento.pk)
    return list(consulta.order_by("-pk"))


@dataclass(frozen=True)
class Resumen:
    """Agregado de un entregable de conjunto sobre los elementos VIGENTES."""

    total: int = 0
    aprobados: int = 0
    pendientes: int = 0
    devueltos: int = 0
    incompletos: int = 0  # elementos a los que les faltan columnas obligatorias de las revisadas

    @property
    def satisfecho(self):
        return self.total > 0 and self.aprobados == self.total and self.incompletos == 0

    @property
    def progreso(self):
        return f"{self.aprobados}/{self.total}"


@dataclass(frozen=True)
class EstadoElemento:
    elemento: object
    estado: str
    decision: object  # última RevisionElemento o None
    cambio_desde_la_decision: bool  # hubo decisión, pero las columnas revisadas ya cambiaron
    completitud: object


def evaluar(entregable):
    """`([EstadoElemento], Resumen)` de los elementos vigentes del Ticket para este entregable."""
    parrilla = ParrillaTicket.objects.filter(ticket_id=entregable.ticket_id).first()
    if parrilla is None:
        return [], Resumen()
    campos = columnas_revisadas_de(entregable)
    ultimas = {}
    for revision in entregable.revisiones.order_by("pk"):
        ultimas[revision.elemento_id] = revision
    estados = []
    for elemento in parrilla_ops.elementos_vigentes(parrilla):
        _, huella = contenido_y_huella(elemento, campos)
        ultima = ultimas.get(elemento.pk)
        vigente = ultima is not None and ultima.huella == huella
        estado = Estado(ultima.decision) if vigente else Estado.PENDIENTE
        estados.append(EstadoElemento(
            elemento=elemento, estado=estado, decision=ultima,
            cambio_desde_la_decision=ultima is not None and not vigente,
            completitud=parrilla_ops.completitud_de(elemento, campos),
        ))
    resumen = Resumen(
        total=len(estados),
        aprobados=sum(1 for e in estados if e.estado == Estado.APROBADA),
        pendientes=sum(1 for e in estados if e.estado == Estado.PENDIENTE),
        devueltos=sum(1 for e in estados if e.estado == Estado.DEVUELTA),
        incompletos=sum(1 for e in estados if not e.completitud.completo),
    )
    return estados, resumen


def resumen_conjunto(entregable):
    return evaluar(entregable)[1]


def conjunto_satisfecho(entregable):
    """Regla agregada que lee `EntregableTicket.satisfecho` para un entregable ELEMENTOS."""
    return resumen_conjunto(entregable).satisfecho


def estado_de(entregable, elemento):
    """Estado vigente de UN elemento (vigente o no) para el entregable."""
    campos = columnas_revisadas_de(entregable)
    ultima = entregable.revisiones.filter(elemento_id=elemento.pk).order_by("-pk").first()
    if ultima is None:
        return Estado.PENDIENTE
    _, huella = contenido_y_huella(elemento, campos)
    return Estado(ultima.decision) if ultima.huella == huella else Estado.PENDIENTE


# --- Decisiones -------------------------------------------------------------------------


def _bloquear_revision(entregable, actor):
    """Locks en el orden del motor (instancia → ticket → parrilla), comprobación de que el Ticket
    admite cambios y de que `actor` es revisor de ESTE entregable. Devuelve el entregable fresco y la
    parrilla bloqueada."""
    ticket_id, instancia_id = EntregableTicket.objects.values_list(
        "ticket_id", "ticket__instancia_workflow_id"
    ).get(pk=entregable.pk)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    entregable = EntregableTicket.objects.get(pk=entregable.pk)
    if entregable.tipo != TIPO:
        raise ValidationError("Este entregable no revisa elementos.")
    if ticket.estado in parrilla_ops.ESTADOS_SIN_CAMBIOS:
        raise ValidationError("Este ticket ya no admite cambios en sus elementos.")
    if not puede_revisar_conjunto(actor, entregable):
        raise PermissionDenied("No está autorizado a revisar los elementos de este entregable.")
    parrilla = ParrillaTicket.objects.select_for_update(of=("self",)).filter(ticket=ticket).first()
    if parrilla is None:
        raise ValidationError("Este ticket no usa elementos estructurados.")
    return entregable, parrilla


def avisar_flujo(ticket):
    """Avisa al Workflow (aviso EXISTENTE de 4.B1, idempotente; no cambia el motor) que algún
    entregable de conjunto del Ticket pudo quedar satisfecho. Si ningún bloque lo espera, no hace nada."""
    from apps.tickets.entregables import _continuar_flujo

    for entregable in ticket.entregables.filter(tipo=TIPO):
        _continuar_flujo(entregable)


def _decidir(entregable, elemento, actor, decision, observacion):
    entregable, parrilla = _bloquear_revision(entregable, actor)
    motivo = motivo_no_revisable(entregable)
    if motivo:
        raise ValidationError(motivo)
    elemento = parrilla_ops.ElementoParrilla.objects.select_for_update().get(pk=elemento.pk)
    if elemento.parrilla_id != parrilla.pk:
        raise ValidationError("El elemento no pertenece al ticket de este entregable.")
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se revisa.")
    observacion = (observacion or "").strip()
    campos = columnas_revisadas_de(entregable)
    actual = estado_de(entregable, elemento)
    if decision == RevisionElemento.Decision.APROBADA:
        completitud = parrilla_ops.completitud_de(elemento, campos)
        if not completitud.completo:
            raise ValidationError(
                "No se puede aprobar un elemento incompleto. Falta o hay que corregir: "
                + ", ".join(completitud.pendientes) + "."
            )
        if actual == Estado.APROBADA:
            raise ValidationError("El elemento ya está aprobado con este contenido.")
    else:
        if not observacion:
            raise ValidationError("Una devolución exige una observación.")
        if actual == Estado.DEVUELTA:
            raise ValidationError("El elemento ya está devuelto: espera a que se corrija.")
    contenido, huella = contenido_y_huella(elemento, campos)
    revision = RevisionElemento.objects.create(
        entregable=entregable, elemento=elemento, decision=decision, observacion=observacion,
        revisor=actor, decidido_en=timezone.now(), huella=huella, contenido=contenido,
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=revision, origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor, datos_anteriores={"estado": actual.value},
        datos_nuevos={
            "entregable_id": entregable.pk, "elemento": elemento.numero, "decision": decision,
            "observacion": observacion, "huella": huella,
        },
    )
    avisar_flujo(Ticket.objects.get(pk=entregable.ticket_id))
    return revision


@transaction.atomic
def aprobar_elemento(entregable, elemento, actor, observacion=""):
    """Aprueba el contenido ACTUAL de las columnas revisadas del elemento. Exige un revisor del
    entregable y que el elemento esté vigente y completo respecto de esas columnas."""
    return _decidir(entregable, elemento, actor, RevisionElemento.Decision.APROBADA, observacion)


@transaction.atomic
def devolver_elemento(entregable, elemento, actor, observacion):
    """Devuelve el elemento para corrección. La observación es obligatoria."""
    return _decidir(entregable, elemento, actor, RevisionElemento.Decision.DEVUELTA, observacion)


# --- Retiro de elementos -------------------------------------------------------------------


def consumido_por_flujo(entregable):
    """¿Ya consumió el flujo este entregable? Sí cuando un bloque ENTREGABLE que referencia su definición
    se completó y el flujo NO está esperándolo de nuevo (un segundo bloque que vuelva a pedirlo lo
    reabre: así nunca queda atrapado sin poder corregir). Solo lee la ejecución; no la modifica. Sin flujo
    no hay consumo."""
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=entregable.ticket_id)
    if instancia_id is None:
        return False
    completado = InstanciaEtapa.objects.filter(
        instancia_workflow_id=instancia_id, estado=InstanciaEtapa.Estado.COMPLETADA,
        bloque_operativo__tipo="ENTREGABLE", bloque_operativo__definicion_entregable_id=entregable.definicion_id,
    ).exists()
    if not completado:
        return False
    ultima = (
        InstanciaEtapa.objects.filter(instancia_workflow_id=instancia_id)
        .select_related("bloque_operativo").order_by("-orden").first()
    )
    esperando = (
        ultima is not None and ultima.estado == InstanciaEtapa.Estado.EN_ESPERA
        and ultima.motivo_espera == InstanciaEtapa.MotivoEspera.ENTREGABLE
        and ultima.bloque_operativo is not None
        and ultima.bloque_operativo.definicion_entregable_id == entregable.definicion_id
    )
    return not esperando


def motivo_no_revisable(entregable):
    """Por qué NO se puede revisar este entregable AHORA según el flujo (texto), o `""` si se puede.
    Reutiliza la instancia activa del Workflow: un entregable consumido es historia y uno que el flujo aún
    no alcanzó es trabajo futuro (misma regla `trabajo.entregable_operable_ahora` que protege la escritura
    de los demás entregables)."""
    from apps.tickets.trabajo import entregable_operable_ahora

    if consumido_por_flujo(entregable):
        return "El flujo ya consumió este entregable: queda como historial y no admite nuevas revisiones."
    ticket = Ticket.objects.select_related("instancia_workflow").get(pk=entregable.ticket_id)
    if not entregable_operable_ahora(ticket, entregable):
        return "Todavía no es el turno de este entregable en el flujo: se podrá revisar cuando el flujo llegue a su paso."
    return ""


def columnas_protegidas(ticket):
    """`{campo_id: nombre del entregable}` de las columnas revisadas de los entregables de conjunto del
    Ticket que el flujo ya CONSUMIÓ: su contenido no puede cambiar. Cada entregable decide por sí mismo
    (no se usa el estado global del Ticket)."""
    protegidas = {}
    for entregable in ticket.entregables.filter(tipo=TIPO):
        if consumido_por_flujo(entregable):
            for campo in columnas_revisadas_de(entregable):
                protegidas[campo.pk] = entregable.nombre
    return protegidas


def exigir_alta_permitida(ticket):
    """Agregar un elemento cambiaría retroactivamente el conjunto que el flujo ya consumió."""
    for entregable in ticket.entregables.filter(tipo=TIPO):
        if consumido_por_flujo(entregable):
            raise ValidationError(
                f"No se pueden agregar elementos: el flujo ya consumió el entregable «{entregable.nombre}» "
                "con el conjunto actual."
            )


def exigir_retiro_permitido(ticket):
    """No se retiran elementos cuando el flujo ya dio por cumplido un entregable de conjunto del
    Ticket: el agregado que consumió dejaría de describir lo que había. (Conservador: se rechaza el
    retiro, no se reabre el flujo.)"""
    for entregable in ticket.entregables.filter(tipo=TIPO):
        if consumido_por_flujo(entregable):
            raise ValidationError(
                f"No se puede retirar: el flujo ya consumió el entregable «{entregable.nombre}» con los "
                "elementos actuales."
            )


# --- Vista del conjunto (UI mínima) ---------------------------------------------------------


def vista_conjunto(entregable, usuario, ticket):
    """Lo que necesita la pantalla de revisión: resumen N/N, columnas revisadas y, por elemento
    vigente, su estado, valores, última observación e historial."""
    campos = columnas_revisadas_de(entregable)
    bases = {c.pk: parrilla_ops._opciones_base(c) for c in campos}
    estados, resumen = evaluar(entregable)
    historial = historial_de(entregable)
    por_elemento = {}
    for revision in historial:
        por_elemento.setdefault(revision.elemento_id, []).append(revision)
    autorizado = (
        puede_revisar_conjunto(usuario, entregable) and ticket.estado not in parrilla_ops.ESTADOS_SIN_CAMBIOS
    )
    motivo_bloqueo = motivo_no_revisable(entregable) if autorizado else ""
    puede_revisar = autorizado and not motivo_bloqueo
    filas = []
    for item in estados:
        valores = {v.campo_id: v for v in item.elemento.valores.all()}
        celdas = []
        pendientes = {c.pk for c in item.completitud.faltantes} | {c.pk for c, _ in item.completitud.invalidos}
        for campo in campos:
            celda = parrilla_ops._celda(campo, valores.get(campo.pk), bases)
            celdas.append({"campo": campo, "texto": celda["texto"], "falta": campo.pk in pendientes})
        filas.append({
            "elemento": item.elemento, "estado": item.estado, "estado_texto": item.estado.label,
            "decision": item.decision, "cambio": item.cambio_desde_la_decision,
            "completitud": item.completitud, "celdas": celdas,
            "historial": por_elemento.get(item.elemento.pk, []),
            "puede_aprobar": puede_revisar and item.estado != Estado.APROBADA and item.completitud.completo,
            "puede_devolver": puede_revisar and item.estado != Estado.DEVUELTA,
        })
    return {
        "entregable": entregable, "resumen": resumen, "columnas": campos, "filas": filas,
        "puede_revisar": puede_revisar,
        "motivo_bloqueo": motivo_bloqueo,
        "n_historial": len(historial),
    }


# --- Para quien edita y para la bandeja de quien revisa (4.G2C2) ---------------------------


def elementos_a_corregir(entregable):
    """Elementos DEVUELTOS que siguen esperando corrección, con la observación del revisor:
    `[{"numero", "observacion"}]`. Es lo que el responsable debe atender."""
    estados, _ = evaluar(entregable)
    return [
        {"numero": e.elemento.numero, "observacion": e.decision.observacion}
        for e in estados if e.estado == Estado.DEVUELTA and e.decision is not None
    ]


def revisiones_pendientes_para(usuario):
    """Conjuntos que `usuario` debe revisar AHORA, para «Mi trabajo»: entregables de revisión de elementos
    de Tickets en atención en los que está configurado como revisor (persona o miembro activo de un
    equipo), que el flujo espera o no referencia (ni futuros ni consumidos) y que aún tienen elementos
    pendientes o devueltos. Un conjunto vacío, aprobado por completo o consumido no aparece. Solo lectura.
    Devuelve `[{"entregable", "ticket", "servicio", "resumen"}]`, el más antiguo primero."""
    if not (getattr(usuario, "is_authenticated", False) and getattr(usuario, "is_active", False)):
        return []
    equipos = MiembroEquipo.objects.filter(usuario=usuario, activo=True).values("equipo_id")
    candidatos = (
        EntregableTicket.objects.filter(tipo=TIPO, ticket__estado=Ticket.Estado.EN_ATENCION)
        .filter(Q(revisores__usuario=usuario) | Q(revisores__equipo_id__in=equipos))
        .select_related("ticket__detalle_servicio__servicio")
        .distinct().order_by("ticket__radicado_en", "pk")
    )
    filas = []
    for entregable in candidatos:
        if motivo_no_revisable(entregable):
            continue
        resumen = resumen_conjunto(entregable)
        if resumen.pendientes + resumen.devueltos == 0:
            continue
        filas.append({
            "entregable": entregable, "ticket": entregable.ticket,
            "servicio": entregable.ticket.detalle_servicio.servicio, "resumen": resumen,
        })
    return filas
