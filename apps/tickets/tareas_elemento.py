"""Tareas por elemento de un Ticket de Proceso — Sprint 4.G2D1.

Un Proceso con estructura de elementos (`catalogo.DefinicionParrilla`) puede configurar que cada elemento
origine una `apps.tareas.Tarea` (`catalogo.ConfiguracionTareasElemento`). Este módulo es el lado del Ticket:

- **Snapshot** (`congelar_configuracion`): al nacer el Ticket se COPIA, dentro de su `ParrillaTicket`, la
  configuración activa como `ConfiguracionTareasTicket` (campos de la versión congelada, resueltos por
  clave estable). Cambiar Studio después no altera ningún Ticket existente.
- **Materialización** (`materializar_tareas`): operación EXPLÍCITA de dominio, transaccional e idempotente,
  que crea una Tarea por elemento VIGENTE mediante `apps.tareas.operaciones.crear_tarea` y la enlaza con
  `TareaElemento`. Nada la dispara sola: ni agregar una fila ni crear el Ticket; la invoca el dominio o el
  bloque TAREAS_ELEMENTOS del flujo al entrar (4.G2D2).

La Tarea sigue siendo la de siempre: asignación, historial, subtareas y adjuntos son los de `apps.tareas`;
`TareaWorkflow` no interviene. Los datos de la Tarea se LEEN de las columnas del elemento en el momento de
materializar y se copian: editar después la fila no modifica una Tarea ya creada.

Todo o nada: si algún elemento por generar es inválido no se crea ninguna Tarea. Un segundo llamado no
duplica nada (a lo sumo una Tarea por elemento y configuración); si entre dos llamados se agregaron
elementos, el segundo genera solo las de los nuevos.

Esta operación es una PRIMITIVA de dominio (como `crear_tarea`): no decide quién puede invocarla.

Ejecución (4.G2D2). El bloque TAREAS_ELEMENTOS del Workflow (`workflows.estrategias.EstrategiaTareasElementos`)
entra por `entrar_al_bloque`: materializa lo que falte asociando las Tareas a ESA ejecución del bloque
(`TareaElemento.instancia_etapa`) y espera a que no quede ninguna abierta (`progreso_de`). Completar la
última libera el bloque (`workflows.integracion.continuar_por_tareas_elementos`). Una vez que el flujo
consumió el conjunto (`consumido_por_flujo`) no se generan tareas adicionales. Las Tareas son de
`apps.tareas`: iniciar, asignar y completar usan sus operaciones, y la evidencia obligatoria
(`Tarea.requiere_evidencia`) la hace cumplir `completar_tarea`. Un elemento con tarea no se retira mientras
la tarea esté abierta y sus columnas usadas no cambian (`columnas_protegidas_de`). Un PROCESO no se resuelve
con tareas por elemento abiertas (`exigir_sin_tareas_abiertas`).

Recuperación (4.G2E). Si al entrar al bloque falta información corregible (`RequisitoPorCorregir`: una
fila incompleta, un responsable sin persona activa, el conjunto sin aprobar…) el bloque NO falla: queda
EN_ESPERA con `resultado["correccion"]` (`datos_de_correccion`), sin generar tareas, y quien atiende corrige
con las operaciones existentes y reintenta explícitamente (`workflows.integracion.reintentar_tareas_elementos`).
"""

from dataclasses import dataclass
from datetime import date, datetime, time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, DefinicionEntregable
from apps.catalogo.tareas_elemento import DESTINOS, configuracion_activa_de
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria, Usuario
from apps.tareas.models import Tarea
from apps.tareas.operaciones import cancelar_tarea, crear_tarea
from apps.tickets.models import (
    ConfiguracionTareasTicket,
    ParrillaTicket,
    TareaElemento,
    Ticket,
)
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

ESTADOS_OPERATIVOS = (Ticket.Estado.RADICADO, Ticket.Estado.EN_ATENCION)
LARGO_TITULO = Tarea._meta.get_field("titulo").max_length


class RequisitoPorCorregir(ValidationError):
    """Una validación de INFORMACIÓN que quien atiende puede corregir con las operaciones de siempre
    (completar o corregir una fila, agregar un elemento, esperar la aprobación del conjunto). Es un
    `ValidationError` (quien no distingue lo trata igual), pero el bloque TAREAS_ELEMENTOS del flujo NO la
    convierte en error técnico: espera una corrección y un reintento explícito (4.G2E). Las demás
    validaciones (ticket cancelado o resuelto, sin configuración, ya consumido) siguen siendo definitivas."""


# --- Snapshot al nacer el Ticket ------------------------------------------------


def congelar_configuracion(parrilla, servicio):
    """Copia a `parrilla` (recién creada) la configuración de tareas ACTIVA del Proceso. Lo llama
    `parrilla.crear_parrilla_para_borrador`, dentro de la transacción del borrador. Devuelve la
    `ConfiguracionTareasTicket`, o `None` si el Proceso no genera tareas por elemento (no se crea nada).
    Si la configuración no se puede cumplir con la versión congelada se rechaza: un Ticket nunca nace con
    una generación imposible."""
    configuracion = configuracion_activa_de(servicio)
    if configuracion is None:
        return None
    campos = {c.clave: c for c in parrilla.formulario_version.campos.all()}
    faltan = []
    elegidos = {}
    for atributo, nombre, tipos, obligatoria in DESTINOS:
        clave = getattr(configuracion, atributo)
        if not clave:
            if obligatoria:
                faltan.append(f"{nombre} (sin configurar)")
            continue
        campo = campos.get(clave)
        if campo is None or campo.tipo not in tipos:
            faltan.append(f"{nombre} («{clave}»)")
            continue
        elegidos["campo_" + atributo.removeprefix("clave_")] = campo
    if faltan:
        raise ValidationError(
            "Las tareas por elemento de este proceso usan columnas que la estructura de elementos activa no "
            "tiene o no son compatibles: " + ", ".join(faltan) + "."
        )
    # Bloque 3.1: solo las columnas editables que existen en la versión congelada (la publicación ya lo exige).
    editables = [c for c in configuracion.columnas_editables or [] if c in campos]
    return ConfiguracionTareasTicket.objects.create(
        parrilla=parrilla, requiere_evidencia=configuracion.requiere_evidencia, columnas_editables=editables,
        **elegidos
    )


# --- Consulta -------------------------------------------------------------------


def configuracion_de(ticket):
    """La `ConfiguracionTareasTicket` del Ticket, o `None` si no genera tareas por elemento."""
    return (
        ConfiguracionTareasTicket.objects.select_related("parrilla").filter(parrilla__ticket_id=ticket.pk).first()
    )


def tareas_de(ticket):
    """Vínculos elemento ↔ Tarea ya generados para el Ticket (todos, incluso de elementos retirados)."""
    return TareaElemento.objects.select_related("elemento", "tarea").filter(elemento__parrilla__ticket_id=ticket.pk)


# --- Materialización --------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoMaterializacion:
    """Qué hizo `materializar_tareas`: los vínculos NUEVOS y los que ya existían (elementos vigentes)."""

    creadas: tuple = ()
    existentes: tuple = ()


def _bloquear(ticket_id):
    """Locks de la operación, en el orden único del proyecto (4.G3A, `workflows.integracion`): instancia de
    Workflow (si hay) → Ticket → parrilla. Quien cancele el Ticket, complete una Tarea o lo tome a la vez
    espera a la instancia, y luego se revalida (un Ticket cancelado no admite generar tareas). Esta
    operación no toma locks de Tareas existentes: solo inserta, así que no puede cruzarse con ellos."""
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=ticket_id)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    parrilla = (
        ParrillaTicket.objects.select_for_update(of=("self",)).select_related("formulario_version")
        .filter(ticket=ticket).first()
    )
    return ticket, parrilla


def _exigir_requisitos(ticket, parrilla):
    """Requisitos del Ticket (no de cada elemento). Devuelve la `ConfiguracionTareasTicket`."""
    if consumido_por_flujo(ticket):
        raise ValidationError(
            "El flujo ya consumió las tareas por elemento de este ticket: no se generan tareas adicionales."
        )
    if ticket.estado not in ESTADOS_OPERATIVOS:
        raise ValidationError(
            "Este ticket ya no admite generar tareas por elemento."
            if ticket.estado != Ticket.Estado.BORRADOR
            else "Este ticket todavía no está radicado: no se pueden generar tareas por elemento."
        )
    if ticket.tipo != Ticket.Tipo.PROCESO:
        raise ValidationError("Solo un ticket de tipo proceso genera tareas por elemento.")
    if parrilla is None:
        raise ValidationError("Este ticket no usa elementos estructurados.")
    configuracion = ConfiguracionTareasTicket.objects.filter(parrilla=parrilla).first()
    if configuracion is None:
        raise ValidationError("Este ticket no tiene configurada la generación de tareas por elemento.")
    pendientes = [
        e.nombre
        for e in ticket.entregables.filter(tipo=DefinicionEntregable.Tipo.ELEMENTOS, obligatorio=True)
        if not e.satisfecho
    ]
    if pendientes:
        raise RequisitoPorCorregir(
            "Antes de generar las tareas, el conjunto de elementos debe estar aprobado y vigente: "
            + ", ".join(f"«{n}»" for n in pendientes) + "."
        )
    return configuracion


def _fecha_limite(campo, valor):
    """`Tarea.fecha_limite` desde una columna FECHA (final del día LOCAL del proyecto) o FECHA_HORA."""
    if campo.tipo == Campo.TipoCampo.FECHA:
        if not isinstance(valor, date) or isinstance(valor, datetime):
            raise ValidationError("no es una fecha válida")
        return timezone.make_aware(datetime.combine(valor, time.max), timezone.get_current_timezone())
    if not isinstance(valor, datetime):
        raise ValidationError("no es una fecha y hora válidas")
    if timezone.is_naive(valor):
        return timezone.make_aware(valor, timezone.get_current_timezone())
    return valor


def _datos_de_tarea(elemento, configuracion, columnas):
    """Lo que `crear_tarea` necesita para `elemento`, o `ValidationError` con TODO lo que le falta."""
    problemas = []
    completitud = _completitud(elemento, columnas)
    if not completitud.completo:
        problemas.append("faltan datos obligatorios: " + ", ".join(completitud.pendientes))
    valores = {v.campo_id: v for v in elemento.valores.all()}

    def leer(campo, nombre):
        guardado = valores.get(campo.pk)
        if guardado is None or guardado.valor in (None, ""):
            problemas.append(f"falta {nombre} («{campo.etiqueta}»)")
            return None
        try:
            ESTRATEGIAS_POR_TIPO[campo.tipo].validar_valor(campo, guardado.valor)
        except ValidationError as exc:
            problemas.append(f"{nombre} («{campo.etiqueta}») no es válido: {' '.join(exc.messages)}")
            return None
        return guardado.valor

    titulo = leer(configuracion.campo_titulo, "el título")
    if titulo is not None:
        titulo = str(titulo).strip()
        if not titulo:
            problemas.append(f"falta el título («{configuracion.campo_titulo.etiqueta}»)")
        elif len(titulo) > LARGO_TITULO:
            problemas.append(f"el título excede {LARGO_TITULO} caracteres")
    descripcion = ""
    if configuracion.campo_descripcion_id is not None:
        guardado = valores.get(configuracion.campo_descripcion_id)
        if guardado is not None and guardado.valor not in (None, ""):
            descripcion = str(guardado.valor).strip()
    usuario = None
    pk_responsable = leer(configuracion.campo_responsable, "el responsable")
    if pk_responsable is not None:
        usuario = Usuario.objects.filter(pk=pk_responsable, is_active=True).first()
        if usuario is None:
            problemas.append(f"el responsable («{configuracion.campo_responsable.etiqueta}») no es una persona activa")
    limite = None
    valor_fecha = leer(configuracion.campo_fecha_limite, "la fecha límite")
    if valor_fecha is not None:
        try:
            limite = _fecha_limite(configuracion.campo_fecha_limite, valor_fecha)
        except ValidationError as exc:
            problemas.append(f"la fecha límite («{configuracion.campo_fecha_limite.etiqueta}») {' '.join(exc.messages)}")
    if problemas:
        raise ValidationError(f"Elemento #{elemento.numero}: " + "; ".join(problemas) + ".")
    return {"titulo": titulo, "descripcion": descripcion, "usuario_responsable": usuario, "fecha_limite": limite}


def _completitud(elemento, columnas):
    from apps.tickets.parrilla import completitud_de

    return completitud_de(elemento, columnas)


@transaction.atomic
def materializar_tareas(ticket, actor=None, *, instancia_etapa=None):
    """Genera una Tarea por cada elemento VIGENTE de la parrilla del Ticket que todavía no la tenga.

    Requisitos (todos se revalidan bajo lock; ver `_exigir_requisitos`): el Ticket es un PROCESO operativo
    (RADICADO o EN_ATENCION), usa elementos y congeló una configuración de tareas, y sus entregables de
    conjunto OBLIGATORIOS están satisfechos y vigentes. Cada elemento por generar debe estar completo y
    tener título, responsable (persona activa) y fecha límite válidos; si alguno no, se rechaza TODA la
    operación con un mensaje por elemento y no se crea nada. Un elemento retirado nunca genera tarea.

    `actor` queda como creador de las Tareas y en la auditoría; sin él (`None`) son tareas de origen
    SISTEMA, sin inventar un usuario técnico. No autoriza: quien la invoque decide si procede.

    `instancia_etapa` (4.G2D2): la ejecución del bloque TAREAS_ELEMENTOS que materializa; queda en cada
    vínculo nuevo. Si el flujo ya consumió el conjunto no se genera nada más."""
    ticket, parrilla = _bloquear(ticket.pk)
    configuracion = _exigir_requisitos(ticket, parrilla)

    from apps.tickets.parrilla import columnas_de, elementos_en_alcance

    # Fase 2: un ítem excluido o rechazado ya no origina tarea.
    vigentes = list(elementos_en_alcance(parrilla))
    if not vigentes:
        raise RequisitoPorCorregir("Este ticket no tiene elementos vigentes de los que generar tareas.")
    ya = {
        v.elemento_id: v
        for v in TareaElemento.objects.select_related("tarea").filter(
            configuracion=configuracion, elemento_id__in=[e.pk for e in vigentes]
        )
    }
    por_generar = [e for e in vigentes if e.pk not in ya]
    columnas = columnas_de(parrilla)
    datos, errores = {}, []
    for elemento in por_generar:
        try:
            datos[elemento.pk] = _datos_de_tarea(elemento, configuracion, columnas)
        except ValidationError as exc:
            errores.extend(exc.messages)
    if errores:
        raise RequisitoPorCorregir(errores)

    creadas = []
    origen = Tarea.Origen.MANUAL if actor is not None else Tarea.Origen.SISTEMA
    for elemento in por_generar:
        tarea = crear_tarea(
            origen=origen, creada_por=actor, requiere_evidencia=configuracion.requiere_evidencia, **datos[elemento.pk]
        )
        vinculo = TareaElemento.objects.create(
            elemento=elemento, tarea=tarea, configuracion=configuracion,
            requiere_evidencia=configuracion.requiere_evidencia, instancia_etapa=instancia_etapa,
        )
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=vinculo,
            origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
            usuario=actor, datos_anteriores=None,
            datos_nuevos={
                "ticket_id": ticket.pk, "elemento_numero": elemento.numero, "tarea_id": tarea.pk,
                "requiere_evidencia": vinculo.requiere_evidencia,
                "instancia_etapa_id": instancia_etapa.pk if instancia_etapa is not None else None,
            },
        )
        creadas.append(vinculo)
    return ResultadoMaterializacion(creadas=tuple(creadas), existentes=tuple(ya[e.pk] for e in vigentes if e.pk in ya))


# --- Progreso y ejecución (4.G2D2) ---------------------------------------------------


@dataclass(frozen=True)
class Progreso:
    """Cómo van las Tareas por elemento de un Ticket. Las CANCELADAS (el Ticket se canceló) no cuentan."""

    completadas: int = 0
    pendientes: int = 0
    en_progreso: int = 0

    @property
    def abiertas(self):
        return self.pendientes + self.en_progreso

    @property
    def total(self):
        return self.completadas + self.abiertas


def ticket_de(tarea):
    """El Ticket de la Tarea generada por un elemento, o `None` si no lo es."""
    vinculo = TareaElemento.objects.select_related("elemento__parrilla__ticket").filter(tarea_id=tarea.pk).first()
    return vinculo.elemento.parrilla.ticket if vinculo is not None else None


def progreso_de(ticket):
    """`Progreso` calculado en vivo desde las Tareas (nunca un contador guardado)."""
    cuentas = {Tarea.Estado.COMPLETADA: 0, Tarea.Estado.PENDIENTE: 0, Tarea.Estado.EN_PROGRESO: 0}
    for estado in TareaElemento.objects.filter(elemento__parrilla__ticket_id=ticket.pk).values_list(
        "tarea__estado", flat=True
    ):
        if estado in cuentas:
            cuentas[estado] += 1
    return Progreso(
        completadas=cuentas[Tarea.Estado.COMPLETADA], pendientes=cuentas[Tarea.Estado.PENDIENTE],
        en_progreso=cuentas[Tarea.Estado.EN_PROGRESO],
    )


def consumido_por_flujo(ticket):
    """¿Un bloque TAREAS_ELEMENTOS del flujo ya se completó? Desde entonces el conjunto es historia: no se
    generan tareas adicionales (los datos y las tareas existentes se conservan)."""
    if ticket.instancia_workflow_id is None:
        return False
    return InstanciaEtapa.objects.filter(
        instancia_workflow_id=ticket.instancia_workflow_id, bloque_operativo__tipo="TAREAS_ELEMENTOS",
        estado=InstanciaEtapa.Estado.COMPLETADA,
    ).exists()


def entrar_al_bloque(ticket, instancia_etapa):
    """Entrada de la ejecución `instancia_etapa` del bloque TAREAS_ELEMENTOS (la llama la Strategy, ya bajo el
    lock de la instancia): genera las Tareas que falten, asociadas a esta ejecución, y devuelve el `Progreso`.
    Reintentar la entrada no duplica nada (la unicidad es por elemento y configuración). Si el flujo ya
    consumió el conjunto (el bloque se vuelve a visitar en un ciclo) no genera más: solo informa el progreso.
    Lanza `ValidationError` si los requisitos no se cumplen o algún elemento es inválido (sin efectos)."""
    if not consumido_por_flujo(ticket):
        materializar_tareas(ticket, None, instancia_etapa=instancia_etapa)
    return progreso_de(ticket)


# --- Recuperación operativa (4.G2E) ---------------------------------------------------


def datos_de_correccion(ticket, mensajes, *, reintentos=0):
    """Resultado de una ejecución del bloque que quedó EN_ESPERA porque hay información por corregir. El
    `ticket` y la instancia siguen intactos; no se generó ninguna Tarea de este intento."""
    progreso = progreso_de(ticket)
    return {
        "total": progreso.total, "completadas": progreso.completadas,
        "correccion": {"mensajes": [str(m) for m in mensajes], "reintentos": reintentos},
    }


def correccion_pendiente(ejecucion):
    """`{"mensajes": [...], "reintentos": n}` si la ejecución del bloque espera una corrección de
    información (y un reintento explícito); `None` si no."""
    if ejecucion is None or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA:
        return None
    return (ejecucion.resultado or {}).get("correccion") or None


def _vinculos_abiertos(elemento_o_ticket_filtro):
    return TareaElemento.objects.filter(**elemento_o_ticket_filtro).filter(
        tarea__estado__in=(Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO)
    )


def exigir_sin_tareas_abiertas(ticket):
    """Un PROCESO no se resuelve con Tareas por elemento pendientes o en progreso."""
    abiertas = progreso_de(ticket).abiertas
    if abiertas:
        raise ValidationError(
            f"Quedan tareas por elemento pendientes o en progreso ({abiertas}): complétalas antes de resolver."
        )


def exigir_retiro_permitido(elemento):
    """Un elemento no se retira mientras su Tarea siga abierta (pendiente o en progreso). Con la Tarea
    completada, o cancelada con el Ticket, el elemento se puede retirar y la Tarea se conserva."""
    if _vinculos_abiertos({"elemento_id": elemento.pk}).exists():
        raise ValidationError(
            f"El elemento #{elemento.numero} tiene una tarea pendiente o en progreso: complétala antes de retirarlo."
        )


def columnas_protegidas_de(elemento):
    """`{campo_id}` de las columnas que originaron la Tarea de `elemento` (título, descripción, responsable y
    fecha límite): una vez generada la tarea su contenido no cambia, para conservar los valores y la
    trazabilidad. Vacío si el elemento no tiene tarea. Reenviar el mismo valor no es un cambio."""
    vinculo = TareaElemento.objects.select_related("configuracion").filter(elemento_id=elemento.pk).first()
    if vinculo is None:
        return frozenset()
    configuracion = vinculo.configuracion
    ids = [configuracion.campo_titulo_id, configuracion.campo_responsable_id, configuracion.campo_fecha_limite_id]
    if configuracion.campo_descripcion_id is not None:
        ids.append(configuracion.campo_descripcion_id)
    return frozenset(ids)


def resumen_para_ticket(usuario, ticket):
    """Lo que muestra el espacio de trabajo: totales por estado y, por cada Tarea generada, su responsable,
    fecha límite, estado, si exige evidencia (y si ya la tiene) y si `usuario` puede abrirla (la autorización
    es la de `apps.tareas`; aquí no se decide nada). `None` si el Ticket no generó tareas por elemento."""
    from apps.tareas.autorizacion import puede_consultar_tarea
    from apps.tareas.operaciones import tiene_evidencia

    vinculos = list(
        TareaElemento.objects.select_related(
            "elemento", "tarea__usuario_responsable", "tarea__equipo_responsable"
        ).filter(elemento__parrilla__ticket_id=ticket.pk).order_by("elemento__numero")
    )
    if not vinculos:
        return None
    filas = []
    for v in vinculos:
        tarea = v.tarea
        if tarea.usuario_responsable_id:
            responsable = tarea.usuario_responsable.get_full_name().strip() or tarea.usuario_responsable.get_username()
        else:
            responsable = tarea.equipo_responsable.nombre if tarea.equipo_responsable_id else ""
        filas.append({
            "vinculo": v, "tarea": tarea, "elemento": v.elemento,
            "accesible": puede_consultar_tarea(usuario, tarea),
            "responsable": responsable,
            "fecha_limite": tarea.fecha_limite,
            "abierta": tarea.estado in (Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO),
            "requiere_evidencia": v.requiere_evidencia,
            "con_evidencia": tiene_evidencia(tarea) if v.requiere_evidencia else None,
        })
    return {"progreso": progreso_de(ticket), "tareas": filas}


# --- Cancelación del Ticket (4.G1.5) ------------------------------------------------


def bloquear_tareas_de_elementos(ticket_id):
    """Toma el lock de las Tareas generadas por elemento (y sus subtareas) antes de cancelar el Ticket. Orden
    único del proyecto (4.G3A, ver `workflows.integracion`): llamarla DESPUÉS de bloquear la instancia y el
    Ticket; con la compuerta cerrada, materializar y reintentar no pueden crear tareas mientras tanto, así que
    el conjunto no cambia. Dentro de una transacción."""
    ids = list(
        TareaElemento.objects.filter(elemento__parrilla__ticket_id=ticket_id).values_list("tarea_id", flat=True)
    )
    if ids:
        list(Tarea.objects.select_for_update().filter(Q(pk__in=ids) | Q(tarea_padre_id__in=ids)).order_by("pk"))


def cancelar_tareas_de_elementos(ticket, actor, *, motivo=""):
    """Al cancelar el Ticket, las Tareas por elemento ABIERTAS y sus subtareas pasan a CANCELADA (las
    completadas se conservan; nada se borra). Antes: `bloquear_tareas_de_elementos`."""
    for vinculo in tareas_de(ticket).order_by("tarea_id"):
        cancelar_tarea(vinculo.tarea, actor, motivo=motivo)


def cancelar_tarea_de_elemento(elemento, actor, *, motivo=""):
    """Fase 2 — el ítem salió del alcance: su Tarea por elemento ABIERTA ya no corresponde y pasa a CANCELADA
    (una completada se conserva; nada se borra). Debe llamarse con el Ticket y el ítem bloqueados. Devuelve las
    Tareas canceladas."""
    canceladas = []
    for vinculo in _vinculos_abiertos({"elemento_id": elemento.pk}).select_related("tarea").order_by("tarea_id"):
        canceladas.append(cancelar_tarea(vinculo.tarea, actor, motivo=motivo))
    return canceladas
