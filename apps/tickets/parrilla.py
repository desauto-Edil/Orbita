"""Elementos estructurados de un Ticket de Proceso («parrilla») — Sprint 4.G2B1 y 4.G2B2.

Un Proceso puede definir, de forma OPCIONAL, columnas repetibles (`catalogo.DefinicionParrilla`). Cada
Ticket de ese Proceso congela la versión de columnas al nacer (`ParrillaTicket`) y guarda filas
(`ElementoParrilla`) con valores tipados (`ValorElemento`). Sigue siendo UN Ticket: una fila no es un
ticket, ni tiene flujo ni tarea propios.

Esta base NO incluye revisión, aprobación ni entregables por conjunto (ver `revision_elementos`); las tareas
por elemento (4.G2D1) viven en `tareas_elemento` y nunca se generan al agregar una fila. Lo que sí garantiza:

- los valores se validan con las mismas Strategies que el resto de formularios
  (`apps.catalogo.campos.ESTRATEGIAS_POR_TIPO`);
- un campo ajeno a la versión congelada se rechaza;
- los valores se pueden modificar durante toda la ejecución (a diferencia de `RespuestaCampo`), salvo en
  un elemento retirado;
- retirar un elemento es lógico, ocurre una sola vez y su número no se reutiliza;
- ARCHIVO (Fase 1 del modelo configurable): los archivos de una columna ARCHIVO son `Adjunto` de la rama
  ELEMENTO (`adjuntar_archivo_elemento` / `retirar_archivo_elemento`, retiro lógico); no pasan por
  `ValorElemento`.

Completitud (4.G2B2). `Campo.obligatorio` NO impide crear ni guardar una fila incompleta: una fila
puede ir llenándose. «¿Está completo este elemento?» es una pregunta CALCULADA (`completitud_de`), nunca
un estado guardado, y todavía no la consume ningún flujo. Un campo obligatorio está completo si tiene
un valor guardado que sigue siendo válido para su tipo (un `False` en un booleano es un valor, igual que
en el formulario de entrada).

Responsable y fechas son COLUMNAS de la estructura, no atributos del elemento: un responsable es un
campo de tipo USUARIO (no hay hoy un tipo «equipo» en `Campo`: GAP) y las fechas son campos FECHA o
FECHA_HORA. Ninguna es todavía la fecha límite de una Tarea (fase posterior).

API de consulta para fases posteriores (revisión por conjunto de campos): `parrilla_de`, `columnas_de`,
`elementos_vigentes`, `valores_de`, `completitud_de` y `representacion_de_valores` (representación
determinista de un subconjunto de campos; NO es todavía una huella de revisión).

Quién puede escribir: la misma relación que los entregables finales (`puede_editar_parrilla`:
responsable individual del Ticket en atención). Es una regla PROVISIONAL: se sustituirá o complementará
cuando los elementos se integren con el Flujo (reglas por etapa). Un ticket resuelto, cerrado o
cancelado ya no admite cambios. Las operaciones revalidan bajo lock del Ticket.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone as zona_utc
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, DefinicionParrilla
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets.autorizacion import puede_consultar_ticket, puede_editar_parrilla, puede_operar_coleccion
from apps.tickets.models import (
    COLUMNA_POR_TIPO,
    COLUMNAS_REFERENCIA,
    Adjunto,
    DecisionAlcanceElemento,
    ElementoParrilla,
    ParrillaTicket,
    ParticipanteParrilla,
    Ticket,
    ValorElemento,
    VersionElemento,
)
from apps.tickets.operaciones import _es_valor_vacio
from apps.workflows.models import InstanciaWorkflow

ESTADOS_SIN_CAMBIOS = (Ticket.Estado.RESUELTO, Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO)


# --- Snapshot al nacer el Ticket ------------------------------------------------


def crear_parrilla_para_borrador(ticket, servicio):
    """Congela las colecciones ACTIVAS del Proceso en `ticket` (recién creado, en BORRADOR): una
    `ParrillaTicket` por colección, con su clave, orden, versión de columnas y nombre. Lo llama
    `operaciones._crear_borrador_de_servicio`, dentro de su transacción. Devuelve la colección por
    defecto (la primera), o `None` si el Proceso no usa elementos (entonces no se crea nada)."""
    definiciones = list(
        DefinicionParrilla.objects.select_related("formulario__version_activa").prefetch_related("participantes")
        .filter(servicio=servicio, activa=True)
        .order_by("orden", "pk")
    )
    creadas = []
    for definicion in definiciones:
        version = definicion.formulario.version_activa
        if version is None or version.formulario_id != definicion.formulario_id or version.estado != "ACTIVA":
            raise ValidationError(
                "La estructura de elementos de este proceso no tiene una versión activa utilizable todavía."
            )
        parrilla = ParrillaTicket.objects.create(
            ticket=ticket, clave=definicion.clave, orden=definicion.orden, formulario_version=version,
            nombre_elemento=definicion.nombre_elemento,
            permite_altas_extraordinarias=definicion.permite_altas_extraordinarias,
        )
        # Bloque 3: los participantes de la colección se congelan con ella (cambiar el Studio no altera este ticket).
        ParticipanteParrilla.objects.bulk_create([
            ParticipanteParrilla(parrilla=parrilla, usuario_id=p.usuario_id, equipo_id=p.equipo_id)
            for p in definicion.participantes.all()
        ])
        creadas.append(parrilla)
    if not creadas:
        return None
    por_defecto = creadas[0]
    # 4.G2D1: si el Proceso genera tareas por elemento, este Ticket congela esa configuración. No genera
    # ninguna tarea: la materialización es una operación explícita. Solo aplica a la colección por
    # defecto del Proceso, y solo si la por defecto del Ticket es esa misma colección.
    from apps.catalogo.parrilla import definicion_de
    from apps.tickets.tareas_elemento import congelar_configuracion

    principal = definicion_de(servicio)
    if principal is not None and principal.clave == por_defecto.clave:
        congelar_configuracion(por_defecto, servicio)
    return por_defecto


# --- Consulta ------------------------------------------------------------------


def parrilla_de(ticket, clave=None):
    """La colección `clave` del Ticket (sin `clave`: la por defecto), o `None` si no la tiene."""
    consulta = ParrillaTicket.objects.select_related("formulario_version").filter(ticket_id=ticket.pk)
    if clave is not None:
        consulta = consulta.filter(clave=clave)
    return consulta.order_by("orden", "pk").first()


def parrillas_de(ticket):
    """Todas las colecciones congeladas del Ticket, en orden."""
    return list(
        ParrillaTicket.objects.select_related("formulario_version").filter(ticket_id=ticket.pk).order_by("orden", "pk")
    )


def columnas_de(parrilla):
    """Los campos (columnas) de la versión congelada, en orden, con sus opciones."""
    return list(parrilla.formulario_version.campos.prefetch_related("opciones").order_by("orden", "id"))


def elementos_vigentes(parrilla):
    return parrilla.elementos.filter(retirado_en__isnull=True).prefetch_related("valores__campo")


def elementos_en_alcance(parrilla):
    """Fase 2 — ítems vigentes que siguen en el alcance EFECTIVO (sin decisión de exclusión o rechazo). Es lo
    que recorren los recorridos por ítem, revisa la revisión por conjunto y convierte en tareas el bloque de
    tareas por elemento: un ítem excluido o rechazado ya no cuenta."""
    return (
        elementos_vigentes(parrilla)
        .exclude(decisiones_alcance__tipo__in=DecisionAlcanceElemento.TIPOS_DE_SALIDA)
        .order_by("numero")
    )


def colecciones_de_participante(usuario):
    """Bloque 3 — colecciones de tickets EN ATENCIÓN en las que `usuario` participa (persona congelada o miembro
    activo de un equipo activo congelado) sin ser el responsable individual del ticket. Es su trabajo en «Mi
    trabajo»; cada una se abre en `tickets:coleccion`."""
    if not (getattr(usuario, "is_authenticated", False) and getattr(usuario, "is_active", False)):
        return ParrillaTicket.objects.none()
    return (
        ParrillaTicket.objects.filter(ticket__estado=Ticket.Estado.EN_ATENCION)
        .filter(
            Q(participantes__usuario_id=usuario.pk)
            | Q(
                participantes__equipo__activo=True, participantes__equipo__miembros__usuario_id=usuario.pk,
                participantes__equipo__miembros__activo=True,
            )
        )
        .exclude(ticket__usuario_responsable_id=usuario.pk)
        .distinct()
        .select_related("ticket__detalle_servicio__servicio")
        .order_by("ticket__radicado_en", "orden")
    )


def elementos_para_ticket(ticket, actor):
    """Elementos vigentes del Ticket para quien puede consultarlo."""
    if not puede_consultar_ticket(actor, ticket):
        raise PermissionDenied("No tiene autorización para consultar este ticket.")
    parrilla = parrilla_de(ticket)
    return [] if parrilla is None else list(elementos_vigentes(parrilla))


def valores_de(elemento):
    """Valores guardados de un elemento: `{clave del campo: valor}` (la clave es estable; el valor es
    el tipado de `ValorElemento.valor`). Una columna sin valor no aparece."""
    return {v.campo.clave: v.valor for v in elemento.valores.all()}


# --- Completitud (calculada, nunca guardada) --------------------------------------


@dataclass(frozen=True)
class Completitud:
    """Resultado de `completitud_de`: qué columnas OBLIGATORIAS faltan o ya no son válidas."""

    faltantes: tuple = ()  # [Campo] sin valor
    invalidos: tuple = ()  # [(Campo, mensaje)] con un valor que ya no pasa su validación

    @property
    def completo(self):
        return not self.faltantes and not self.invalidos

    @property
    def pendientes(self):
        """Etiquetas de lo que falta o hay que corregir, en el orden de las columnas."""
        return [c.etiqueta for c in self.faltantes] + [c.etiqueta for c, _ in self.invalidos]


def completitud_de(elemento, columnas=None):
    """«¿Este elemento está completo según su estructura?» Mira las columnas obligatorias de la
    versión CONGELADA: faltante = sin valor; inválido = con un valor que su Strategy ya no acepta
    (p. ej. una persona desactivada después). Las columnas opcionales no cuentan. No escribe nada y no
    depende del estado del Ticket ni de quién consulta. `columnas` evita volver a leerlas al
    evaluar muchas filas."""
    if columnas is None:
        columnas = columnas_de(elemento.parrilla)
    valores = {v.campo_id: v for v in elemento.valores.all()}
    con_archivo = set(elemento.archivos.filter(retirado_en__isnull=True).values_list("campo_elemento_id", flat=True))
    faltantes, invalidos = [], []
    for campo in columnas:
        if not campo.obligatorio:
            continue
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            # Completa con al menos un archivo vigente en esa columna.
            if campo.pk not in con_archivo:
                faltantes.append(campo)
            continue
        guardado = valores.get(campo.pk)
        if guardado is None:
            faltantes.append(campo)
            continue
        try:
            ESTRATEGIAS_POR_TIPO[campo.tipo].validar_valor(campo, guardado.valor)
        except ValidationError as exc:
            invalidos.append((campo, " ".join(exc.messages)))
    return Completitud(faltantes=tuple(faltantes), invalidos=tuple(invalidos))


# --- Representación determinista (insumo de fases posteriores) -----------------------


def _canonico(valor):
    """Forma estable de un valor tipado: no depende de cómo se guardó ni de la zona horaria."""
    if isinstance(valor, bool) or valor is None or isinstance(valor, (int, str)):
        return valor
    if isinstance(valor, Decimal):
        return format(valor.normalize(), "f")
    if isinstance(valor, datetime):
        return (valor.astimezone(zona_utc.utc) if timezone.is_aware(valor) else valor).isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, (list, tuple, set, frozenset)):
        return sorted(str(v) for v in valor)  # una multiselección es un conjunto: su orden no importa
    return str(valor)


def representacion_de_valores(elemento, campos=None):
    """Texto canónico (JSON) de los valores de un SUBCONJUNTO de columnas de `elemento`
    (`campos`: `Campo`s o ids; `None` = todas). El mismo contenido da siempre el mismo texto, sea
    cual sea el orden de consulta o de `campos`: llaves ordenadas por clave y multiselecciones
    ordenadas. Una columna sin valor se incluye como nulo, así «sin valor» y «con valor» difieren.

    Rechaza columnas de otra versión y de tipo ARCHIVO. Es la materia prima de una huella de revisión
    (fase posterior); no calcula ninguna huella ni guarda nada."""
    columnas = {c.pk: c for c in columnas_de(elemento.parrilla)}
    if campos is None:
        elegidas = [c for c in columnas.values() if c.tipo != Campo.TipoCampo.ARCHIVO]
    else:
        elegidas = []
        for llave in campos:
            campo = columnas.get(llave.pk if isinstance(llave, Campo) else llave)
            if campo is None:
                raise ValidationError("El campo no pertenece a la estructura de este ticket.")
            if campo.tipo == Campo.TipoCampo.ARCHIVO:
                raise ValidationError(
                    f"«{campo.etiqueta}»: los campos de tipo archivo todavía no se admiten en los elementos."
                )
            elegidas.append(campo)
    guardados = {v.campo_id: v.valor for v in elemento.valores.all()}
    contenido = {campo.clave: _canonico(guardados.get(campo.pk)) for campo in elegidas}
    return json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- Valores ----------------------------------------------------------------------


def _aware(valor):
    if isinstance(valor, datetime) and timezone.is_naive(valor):
        return timezone.make_aware(valor)
    return valor


def _auditable(valor):
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    return valor


def _asignar(valor_elemento, campo, normalizado):
    columna = COLUMNA_POR_TIPO[campo.tipo]
    if columna in COLUMNAS_REFERENCIA:
        setattr(valor_elemento, f"{columna}_id", normalizado)
    elif columna == "valor_numero":
        valor_elemento.valor_numero = Decimal(str(normalizado))
    elif columna == "valor_fecha_hora":
        valor_elemento.valor_fecha_hora = _aware(normalizado)
    else:
        setattr(valor_elemento, columna, normalizado)


def _resolver_campos(parrilla, valores):
    """`{campo_id: valor_crudo}`. Las llaves pueden ser `Campo` o ids; todas deben ser columnas de
    la versión CONGELADA en la parrilla."""
    campos = {c.pk: c for c in columnas_de(parrilla)}
    resueltos = {}
    for llave, crudo in (valores or {}).items():
        pk = llave.pk if isinstance(llave, Campo) else llave
        campo = campos.get(pk)
        if campo is None:
            raise ValidationError("El campo no pertenece a la estructura de este ticket.")
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            raise ValidationError(
                f"«{campo.etiqueta}»: los archivos de un elemento se adjuntan con su propia acción, no como valor."
            )
        resueltos[pk] = (campo, crudo)
    return resueltos


def _rechazar_columna_consumida(campo, protegidas):
    raise ValidationError(
        f"«{campo.etiqueta}»: el flujo ya consumió esta columna (entregable «{protegidas[campo.pk]}») y su "
        "contenido no puede cambiar."
    )


def _rechazar_columna_con_tarea(campo):
    raise ValidationError(
        f"«{campo.etiqueta}»: de esta columna salió la tarea de este elemento (4.G2D2) y su contenido no puede "
        "cambiar."
    )


def _aplicar_valores(elemento, parrilla, valores, protegidas=None, protegidas_tarea=frozenset(), permitidas=None):
    """Guarda (upsert) los valores indicados; un valor vacío borra el existente. Devuelve
    `(anterior, nuevo)` por clave del campo, para la auditoría. Solo toca los campos presentes.

    `protegidas` (4.G2C2): `{campo_id: entregable}` de columnas que un entregable de conjunto ya
    consumido por el flujo congeló. Reenviar el mismo valor no es un cambio (el formulario de edición
    envía todas las columnas); cambiarlo, borrarlo o escribirlo donde no había se rechaza.

    `protegidas_tarea` (4.G2D2): ids de las columnas de las que salió la Tarea de ESTE elemento; mismas
    reglas, y el resto de columnas del elemento sigue editable.

    `permitidas` (Bloque 3.1): ids de las únicas columnas que `actor` puede cambiar (responsable de una tarea
    abierta del ítem); `None` = todas. Con las mismas reglas: reenviar el valor actual no es un cambio."""
    protegidas = protegidas or {}
    resueltos = _resolver_campos(parrilla, valores)
    existentes = {v.campo_id: v for v in elemento.valores.select_related("campo")}
    anterior, nuevo = {}, {}
    for pk, (campo, crudo) in resueltos.items():
        existente = existentes.get(pk)
        actual = existente.valor if existente is not None else None
        if _es_valor_vacio(crudo):
            if existente is not None:
                if pk in protegidas:
                    _rechazar_columna_consumida(campo, protegidas)
                if pk in protegidas_tarea:
                    _rechazar_columna_con_tarea(campo)
                _exigir_columna_permitida(campo, permitidas)
                existente.delete()
                anterior[campo.clave] = _auditable(actual)
                nuevo[campo.clave] = None
            continue
        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        try:
            estrategia.validar_valor(campo, crudo)
        except ValidationError as exc:
            raise ValidationError(f"«{campo.etiqueta}»: {' '.join(exc.messages)}")
        normalizado = estrategia.normalizar(crudo)
        instancia = existente or ValorElemento(elemento=elemento, campo=campo)
        _asignar(instancia, campo, normalizado)
        if pk in protegidas and instancia.valor != actual:
            _rechazar_columna_consumida(campo, protegidas)
        if pk in protegidas_tarea and instancia.valor != actual:
            _rechazar_columna_con_tarea(campo)
        if instancia.valor != actual:
            _exigir_columna_permitida(campo, permitidas)
        instancia.save()
        valor_nuevo = instancia.valor
        if valor_nuevo != actual:
            anterior[campo.clave] = _auditable(actual)
            nuevo[campo.clave] = _auditable(valor_nuevo)
    return anterior, nuevo


# --- Operaciones ------------------------------------------------------------------


def _bloquear(ticket_id, actor, *, parrilla_id=None, clave=None, operar=False, por_tarea=False):
    """Lock del Ticket, comprobación de que sigue siendo operable y de que `actor` puede editar, y
    lock de la colección (`parrilla_id`, o la de `clave`, o la por defecto). Orden de locks: instancia
    de Workflow (si hay; es el orden del motor y de los entregables) → Ticket → parrilla.

    `operar=True` (Bloque 3) es para CREAR y EDITAR ítems (valores, archivos, versiones): además del responsable
    individual admite a un participante de ESA colección (`puede_operar_coleccion`), y se decide sobre la
    colección ya bloqueada. Sin él (retirar, excluir, cerrar el alcance…) la regla es `puede_editar_parrilla`.
    `por_tarea=True` (Bloque 3.1) deja esa decisión a quien llama (`_bloquear_elemento`), que además admite al
    responsable de una tarea abierta del ítem, limitado a sus columnas."""
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=ticket_id)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    if ticket.estado in ESTADOS_SIN_CAMBIOS:
        raise ValidationError("Este ticket ya no admite cambios en sus elementos.")
    if not operar and not puede_editar_parrilla(actor, ticket):
        raise PermissionDenied(
            "Solo el responsable individual de un ticket en atención puede modificar sus elementos."
        )
    consulta = ParrillaTicket.objects.select_for_update(of=("self",)).select_related("formulario_version").filter(
        ticket=ticket
    )
    if parrilla_id is not None:
        consulta = consulta.filter(pk=parrilla_id)
    elif clave is not None:
        consulta = consulta.filter(clave=clave)
    parrilla = consulta.order_by("orden", "pk").first()
    if parrilla is None:
        raise ValidationError("Este ticket no usa esa estructura de elementos.")
    if operar and not por_tarea and not puede_operar_coleccion(actor, parrilla, ticket):
        raise PermissionDenied(
            "Solo quien atiende el ticket o un participante de esta colección, con el ticket en atención, "
            "puede agregar o editar sus elementos."
        )
    return ticket, parrilla


_MENSAJE_SIN_EDICION = (
    "Solo quien atiende el ticket, un participante de esta colección o el responsable de una tarea abierta de "
    "este ítem, con el ticket en atención, puede editarlo."
)


def _bloquear_elemento(elemento, actor):
    """Bloque 3.1 — locks y autorización para EDITAR un ítem existente (valores, archivos, versiones). Devuelve
    `(ticket, parrilla, elemento_bloqueado, permitidas)`: `permitidas` es `None` si `actor` opera la colección
    (todas las columnas, Bloque 3) o el conjunto de ids de columnas que sus tareas abiertas de ESTE ítem le
    autorizan (`edicion_por_tarea`), decidido después de bloquear la instancia, el Ticket y la colección."""
    from apps.tickets.edicion_por_tarea import columnas_por_tarea

    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    ticket, parrilla = _bloquear(ticket_id, actor, parrilla_id=elemento.parrilla_id, operar=True, por_tarea=True)
    elemento = ElementoParrilla.objects.select_for_update().select_related("parrilla").get(pk=elemento.pk)
    if puede_operar_coleccion(actor, parrilla, ticket):
        return ticket, parrilla, elemento, None
    permitidas = columnas_por_tarea(actor, elemento, ticket)
    if not permitidas:
        raise PermissionDenied(_MENSAJE_SIN_EDICION)
    return ticket, parrilla, elemento, permitidas


def _exigir_columna_permitida(campo, permitidas):
    if permitidas is not None and campo.pk not in permitidas:
        raise PermissionDenied(f"«{campo.etiqueta}»: tu tarea no te permite modificar esta columna.")


def es_coleccion_por_defecto(parrilla):
    """La primera colección del Ticket: la única que conocen la revisión por conjunto (ELEMENTOS) y las
    tareas por elemento, anteriores a las colecciones múltiples."""
    primera = (
        ParrillaTicket.objects.filter(ticket_id=parrilla.ticket_id).order_by("orden", "pk").values_list("pk", flat=True).first()
    )
    return primera == parrilla.pk


def _auditar(instancia, actor, accion, anterior, nuevo):
    # Sin actor es el Sistema (p. ej. el flujo presenta un ítem a una gaceta y se registra su versión).
    registrar_evento(
        accion=accion, instancia=instancia,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )


@transaction.atomic
def crear_elemento(ticket, actor, valores=None, *, clave=None):
    """Agrega una fila al final de una colección del Ticket (`clave`; sin ella, la por defecto), con los
    valores iniciales indicados (`{campo | campo_id: valor}`, opcionales). El número y la posición salen
    del lock de esa colección: cada colección numera sus ítems por separado. Con el alcance cerrado solo
    se agrega por `agregar_elemento_extraordinario`."""
    ticket, parrilla = _bloquear(ticket.pk, actor, clave=clave, operar=True)
    if parrilla.alcance_cerrado:
        raise ValidationError(
            "El alcance de esta colección ya se cerró: un ítem nuevo solo se agrega como alta extraordinaria."
        )
    return _crear_elemento_bloqueado(ticket, parrilla, actor, valores)


def _crear_elemento_bloqueado(ticket, parrilla, actor, valores, *, incorporar=True):
    from apps.tickets import revision_elementos

    if es_coleccion_por_defecto(parrilla):
        # 4.G2C2: no se agregan elementos al conjunto que el flujo ya consumió.
        revision_elementos.exigir_alta_permitida(ticket)
    agregados = parrilla.elementos.aggregate(numero=Max("numero"), orden=Max("orden"))
    elemento = ElementoParrilla.objects.create(
        parrilla=parrilla, numero=(agregados["numero"] or 0) + 1, orden=(agregados["orden"] or 0) + 1
    )
    _, nuevo = _aplicar_valores(elemento, parrilla, valores)
    _auditar(
        elemento, actor, RegistroAuditoria.Accion.CREAR, None,
        {"ticket_id": ticket.pk, "numero": elemento.numero, "orden": elemento.orden, "valores": nuevo},
    )
    if incorporar:
        # Fase 2: si el flujo está recorriendo esta colección y admite ítems nuevos, el ítem entra a su recorrido.
        from apps.workflows import recorridos

        recorridos.incorporar_elemento(ticket, elemento, actor=actor)
        _ingreso_automatico(elemento, actor)
    return elemento


def _ingreso_automatico(elemento, actor):
    """Fase 3 — ingreso PROGRESIVO a las gacetas que reciben ítems «al cumplir la condición»: cada cambio del
    ítem vuelve a evaluarlo (idempotente: un ítem que ya participa no se duplica)."""
    from apps.gacetas.operaciones import ingresar_automaticamente

    ingresar_automaticamente(elemento, actor, ingreso="AL_CUMPLIR_CONDICION")


@transaction.atomic
def actualizar_valores(elemento, actor, valores):
    """Modifica los valores indicados de una fila vigente (los demás no se tocan; un valor vacío
    borra el existente). Se puede hacer durante toda la ejecución del Ticket."""
    ticket, parrilla, elemento, permitidas = _bloquear_elemento(elemento, actor)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se puede modificar.")
    _exigir_en_alcance(elemento)
    from apps.tickets import revision_elementos

    # 4.G2C2: las columnas revisadas de un entregable consumido por el flujo no cambian; las ajenas sí.
    from apps.tickets import tareas_elemento

    protegidas = revision_elementos.columnas_protegidas(ticket) if es_coleccion_por_defecto(parrilla) else {}
    anterior, nuevo = _aplicar_valores(
        elemento, parrilla, valores, protegidas=protegidas,
        protegidas_tarea=tareas_elemento.columnas_protegidas_de(elemento), permitidas=permitidas,
    )
    if nuevo:
        _auditar(
            elemento, actor, RegistroAuditoria.Accion.ACTUALIZAR,
            {"numero": elemento.numero, "valores": anterior}, {"numero": elemento.numero, "valores": nuevo},
        )
        _ingreso_automatico(elemento, actor)
    return elemento


@transaction.atomic
def retirar_elemento(elemento, actor):
    """Retiro LÓGICO: el elemento y sus valores se conservan, pero deja de aparecer entre los
    vigentes. Su número no se reutiliza. Retirar uno ya retirado se RECHAZA (no se reescribe quién ni
    cuándo lo retiró)."""
    from apps.tickets import revision_elementos

    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    ticket, parrilla = _bloquear(ticket_id, actor, parrilla_id=elemento.parrilla_id)
    elemento = ElementoParrilla.objects.select_for_update().get(pk=elemento.pk)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento ya está retirado.")
    if parrilla.alcance_cerrado:
        raise ValidationError(
            "El alcance de esta colección ya se cerró: para sacar un ítem, exclúyelo o recházalo con una justificación."
        )
    por_defecto = es_coleccion_por_defecto(parrilla)
    if por_defecto:
        # 4.G2C1: no se retira si el flujo ya consumió un entregable de conjunto del Ticket.
        revision_elementos.exigir_retiro_permitido(ticket)
    # 4.G2D2: ni mientras la tarea que originó el elemento siga abierta.
    from apps.tickets import tareas_elemento

    tareas_elemento.exigir_retiro_permitido(elemento)
    elemento.retirado_en = timezone.now()
    elemento.retirado_por = actor
    elemento.save(update_fields=["retirado_en", "retirado_por", "actualizado_en"])
    _auditar(
        elemento, actor, RegistroAuditoria.Accion.ACTUALIZAR,
        {"numero": elemento.numero, "retirado": False}, {"numero": elemento.numero, "retirado": True},
    )
    # Fase 2: si el ítem estaba recorriendo el flujo, su recorrido se detiene (los demás siguen).
    from apps.workflows import recorridos

    recorridos.sacar_elemento(elemento, actor, resultado=recorridos.RETIRADO, motivo="Ítem retirado.")
    # Fase 3: y deja de estar en revisión en sus gacetas.
    from apps.gacetas.operaciones import al_salir_del_alcance

    al_salir_del_alcance(elemento, actor, tipo="EXCLUIDO", justificacion="Ítem retirado.")
    if por_defecto:
        # El conjunto vigente cambió (el elemento sale del agregado): avisa al flujo por el cauce de siempre.
        revision_elementos.avisar_flujo(ticket)
    return elemento


# --- Archivos de columnas ARCHIVO (Fase 1) -------------------------------------------


def archivos_de(elemento, campo=None):
    """Archivos VIGENTES (no retirados) del elemento, opcionalmente de una columna, en orden de carga."""
    consulta = elemento.archivos.filter(retirado_en__isnull=True)
    if campo is not None:
        consulta = consulta.filter(campo_elemento=campo)
    return list(consulta.select_related("subido_por").order_by("creado_en", "pk"))


def _columna_archivo(parrilla, campo):
    campo = next((c for c in columnas_de(parrilla) if c.pk == getattr(campo, "pk", campo)), None)
    if campo is None:
        raise ValidationError("El campo no pertenece a la estructura de este ticket.")
    if campo.tipo != Campo.TipoCampo.ARCHIVO:
        raise ValidationError(f"«{campo.etiqueta}» no es una columna de archivo.")
    return campo


def _exigir_columna_modificable(ticket, parrilla, elemento, campo):
    from apps.tickets import revision_elementos, tareas_elemento

    protegidas = revision_elementos.columnas_protegidas(ticket) if es_coleccion_por_defecto(parrilla) else {}
    if campo.pk in protegidas:
        _rechazar_columna_consumida(campo, protegidas)
    if campo.pk in tareas_elemento.columnas_protegidas_de(elemento):
        _rechazar_columna_con_tarea(campo)


@transaction.atomic
def adjuntar_archivo_elemento(elemento, campo, actor, archivo_subido):
    """Adjunta un archivo a la columna ARCHIVO `campo` de un elemento vigente. Lo puede hacer quien
    edita los elementos (`puede_editar_parrilla`), bajo los mismos locks y protecciones que cambiar un
    valor; la extensión y el tamaño se validan con la Strategy de ARCHIVO (configuración de la columna)."""
    from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO as estrategias
    from apps.tickets.operaciones import _crear_adjunto

    ticket, parrilla, elemento, permitidas = _bloquear_elemento(elemento, actor)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se puede modificar.")
    _exigir_en_alcance(elemento)
    campo = _columna_archivo(parrilla, campo)
    _exigir_columna_permitida(campo, permitidas)
    _exigir_columna_modificable(ticket, parrilla, elemento, campo)
    if archivo_subido is None:
        raise ValidationError("Selecciona un archivo.")
    estrategias[campo.tipo].validar_valor(
        campo, {"nombre": archivo_subido.name, "tamano_mb": archivo_subido.size / (1024 * 1024)}
    )
    adjunto = None
    try:
        adjunto = _crear_adjunto(
            Adjunto.TipoRelacion.ELEMENTO, {"elemento": elemento, "campo_elemento": campo}, archivo_subido, actor
        )
        _auditar(
            adjunto, actor, RegistroAuditoria.Accion.CREAR, None,
            {
                "elemento_id": elemento.pk, "numero": elemento.numero, "campo": campo.clave,
                "nombre_original": adjunto.nombre_original, "tamano_bytes": adjunto.tamano_bytes,
            },
        )
        _ingreso_automatico(elemento, actor)
    except Exception:
        # El almacenamiento no participa en la transacción SQL: se limpia el archivo recién escrito.
        if adjunto is not None:
            adjunto.archivo.delete(save=False)
        raise
    return adjunto


@transaction.atomic
def retirar_archivo_elemento(adjunto, actor):
    """Retiro LÓGICO de un archivo de un elemento (se conserva para la historia y las versiones)."""
    adjunto = Adjunto.objects.select_related("elemento").get(pk=adjunto.pk)
    if adjunto.tipo_relacion != Adjunto.TipoRelacion.ELEMENTO:
        raise ValidationError("El archivo no pertenece a un elemento.")
    ticket, parrilla, elemento, permitidas = _bloquear_elemento(adjunto.elemento, actor)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se puede modificar.")
    adjunto = Adjunto.objects.select_for_update().get(pk=adjunto.pk)
    if adjunto.retirado_en is not None:
        return adjunto
    _exigir_en_alcance(elemento)
    _exigir_columna_permitida(adjunto.campo_elemento, permitidas)
    _exigir_columna_modificable(ticket, parrilla, elemento, adjunto.campo_elemento)
    adjunto.retirado_en = timezone.now()
    adjunto.save(update_fields=["retirado_en", "actualizado_en"])
    _auditar(
        adjunto, actor, RegistroAuditoria.Accion.ACTUALIZAR, {"retirado_en": None},
        {"retirado_en": adjunto.retirado_en, "elemento_id": elemento.pk, "campo": adjunto.campo_elemento.clave},
    )
    return adjunto


# --- Versiones de un ítem (Fase 1, Memento) ------------------------------------------


def contenido_de(elemento):
    """Contenido canónico del ítem HOY: valores de todas sus columnas (como `representacion_de_valores`)
    y sus archivos vigentes por columna. Es lo que fotografía una `VersionElemento`."""
    valores = json.loads(representacion_de_valores(elemento))
    archivos = [
        {
            "campo": adjunto.campo_elemento.clave, "adjunto_id": adjunto.pk,
            "nombre": adjunto.nombre_original, "tamano_bytes": adjunto.tamano_bytes,
        }
        for adjunto in elemento.archivos.filter(retirado_en__isnull=True)
        .select_related("campo_elemento").order_by("campo_elemento__orden", "pk")
    ]
    return {"valores": valores, "archivos": archivos}


def huella_de(contenido):
    texto = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def versiones_de(elemento):
    return list(elemento.versiones.select_related("creada_por").order_by("numero"))


def _registrar_version(elemento, actor, motivo=""):
    """Primitiva SIN autorización (quien llama ya bloqueó y autorizó). Si el contenido no cambió desde la
    última versión, devuelve esa misma (no hay dos versiones seguidas iguales)."""
    contenido = contenido_de(elemento)
    huella = huella_de(contenido)
    ultima = elemento.versiones.order_by("-numero").first()
    if ultima is not None and ultima.huella == huella:
        return ultima
    version = VersionElemento.objects.create(
        elemento=elemento, numero=(ultima.numero + 1) if ultima else 1, huella=huella, contenido=contenido,
        motivo=(motivo or "").strip()[:200], creada_por=actor,
    )
    _auditar(
        version, actor, RegistroAuditoria.Accion.CREAR, None,
        {"elemento_id": elemento.pk, "numero_elemento": elemento.numero, "version": version.numero, "huella": huella},
    )
    return version


@transaction.atomic
def registrar_version(elemento, actor, motivo=""):
    """Registra una versión del ítem tal como está ahora (quien edita los elementos). Idempotente: sin
    cambios desde la última versión no crea otra."""
    # Bloque 3.1: el responsable de una tarea abierta del ítem también versiona (una versión fotografía el
    # ítem; no cambia ninguna columna). La foto sigue siendo completa: no se versiona «solo lo autorizado».
    _ticket, _parrilla, elemento, _permitidas = _bloquear_elemento(elemento, actor)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado: no se registran versiones nuevas.")
    return _registrar_version(elemento, actor, motivo)


# --- Alcance de una colección (Fase 1) ------------------------------------------------


def decision_de_salida(elemento):
    """La decisión EXCLUIDO/RECHAZADO del ítem, o `None` si sigue en el alcance."""
    return elemento.decisiones_alcance.filter(tipo__in=DecisionAlcanceElemento.TIPOS_DE_SALIDA).first()


def _exigir_en_alcance(elemento):
    decision = decision_de_salida(elemento)
    if decision is not None:
        raise ValidationError(
            f"El ítem #{elemento.numero} fue {decision.get_tipo_display().lower()} y ya no se modifica."
        )


@transaction.atomic
def cerrar_alcance(ticket, actor, *, clave=None):
    """Cierra el alcance de una colección del Ticket (una sola vez): fija la foto del alcance ORIGINAL
    (ítems vigentes, con su número y la huella de su contenido) y, desde ese momento, no se agregan ni
    retiran ítems por la vía ordinaria."""
    ticket, parrilla = _bloquear(ticket.pk, actor, clave=clave)
    if parrilla.alcance_cerrado:
        raise ValidationError("El alcance de esta colección ya está cerrado.")
    _cerrar_alcance_bloqueado(parrilla, actor)
    # Fase 2: un recorrido que esperaba el cierre del alcance puede terminar ahora.
    from apps.workflows import recorridos

    recorridos.reevaluar_ticket(ticket)
    return parrilla


def _cerrar_alcance_bloqueado(parrilla, actor, *, bloque=None):
    foto = [
        {"elemento_id": e.pk, "numero": e.numero, "huella": huella_de(contenido_de(e))}
        for e in parrilla.elementos.filter(retirado_en__isnull=True).order_by("numero")
    ]
    parrilla.alcance_cerrado_en = timezone.now()
    parrilla.alcance_cerrado_por = actor
    parrilla.alcance_original = foto
    parrilla.save(update_fields=["alcance_cerrado_en", "alcance_cerrado_por", "alcance_original", "actualizado_en"])
    nuevo = {"alcance_cerrado": True, "coleccion": parrilla.clave, "elementos": [f["numero"] for f in foto]}
    if bloque is not None:
        nuevo["por_flujo"] = {"bloque": bloque.clave, "nombre": bloque.nombre}
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=parrilla,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores={"alcance_cerrado": False}, datos_nuevos=nuevo,
    )
    return parrilla


def cerrar_alcance_por_flujo(ticket, claves, *, bloque=None):
    """Fase 2 — cierre AUTOMÁTICO del alcance al llegar a un bloque configurado (`apps.workflows.efectos`).
    Lo ejecuta el Sistema (sin actor ni autorización de persona), dentro del motor y con la instancia
    principal ya bloqueada; orden instancia → Ticket → colección. Idempotente: una colección ya cerrada, o que
    el Ticket no tiene, se ignora. Devuelve las colecciones que se cerraron ahora."""
    ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
    if ticket.estado in ESTADOS_SIN_CAMBIOS:
        return []
    cerradas = []
    for parrilla in (
        ParrillaTicket.objects.select_for_update(of=("self",)).filter(ticket=ticket, clave__in=list(claves))
        .order_by("orden", "pk")
    ):
        if parrilla.alcance_cerrado:
            continue
        cerradas.append(_cerrar_alcance_bloqueado(parrilla, None, bloque=bloque))
    return cerradas


def _justificacion(texto):
    texto = (texto or "").strip()
    if not texto:
        raise ValidationError("La decisión necesita una justificación.")
    return texto


@transaction.atomic
def decidir_salida_de_alcance(elemento, actor, *, tipo, justificacion, origen=DecisionAlcanceElemento.Origen.MANUAL):
    """EXCLUYE o RECHAZA un ítem: sale del alcance efectivo, con justificación obligatoria. Es final para
    ese ítem (no se repite ni se revierte) y no exige otra aprobación. Quedan el alcance original, la
    decisión, quién, cuándo y por qué."""
    if tipo not in DecisionAlcanceElemento.TIPOS_DE_SALIDA:
        raise ValidationError("Decisión de alcance no válida.")
    justificacion = _justificacion(justificacion)
    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    _bloquear(ticket_id, actor, parrilla_id=elemento.parrilla_id)
    elemento = ElementoParrilla.objects.select_for_update().get(pk=elemento.pk)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado: no forma parte del alcance.")
    _exigir_en_alcance(elemento)
    decision = _crear_decision_de_salida(elemento, actor, tipo=tipo, justificacion=justificacion, origen=origen)
    # Fase 2: el recorrido en curso del ítem se detiene con esta decisión (los de los demás ítems siguen).
    from apps.workflows import recorridos

    recorridos.sacar_elemento(elemento, actor, resultado=tipo, motivo=justificacion)
    return decision


def _crear_decision_de_salida(elemento, actor, *, tipo, justificacion, origen):
    """Registra la salida del alcance y aplica sus efectos (Fase 2): la tarea por elemento abierta de ese ítem
    ya no corresponde y se cancela, y el flujo reevalúa los agregados que dependían del ítem (revisión por
    conjunto y bloque de tareas por elemento). Debe llamarse con el Ticket, la colección y el ítem bloqueados."""
    decision = DecisionAlcanceElemento.objects.create(
        elemento=elemento, tipo=tipo, justificacion=justificacion, origen=origen, decidido_por=actor
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=decision,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=None,
        datos_nuevos={
            "elemento_id": elemento.pk, "numero": elemento.numero, "tipo": tipo, "origen": origen,
            "justificacion": justificacion,
        },
    )
    from apps.gacetas.operaciones import al_salir_del_alcance
    from apps.tickets import revision_elementos, tareas_elemento

    # Fase 3: sus participaciones pendientes en gacetas se cierran con el mismo resultado.
    al_salir_del_alcance(elemento, actor, tipo=tipo, justificacion=justificacion)
    tareas_canceladas = tareas_elemento.cancelar_tarea_de_elemento(
        elemento, actor, motivo=f"El ítem #{elemento.numero} salió del alcance: {justificacion}"
    )
    if es_coleccion_por_defecto(elemento.parrilla):
        ticket = Ticket.objects.get(pk=elemento.parrilla.ticket_id)
        revision_elementos.avisar_flujo(ticket)
        if tareas_canceladas:
            from apps.workflows.integracion import continuar_por_tareas_elementos

            continuar_por_tareas_elementos(tareas_canceladas[0])
    return decision


@transaction.atomic
def registrar_salida_por_flujo(
    elemento, actor, *, tipo, justificacion, origen=DecisionAlcanceElemento.Origen.APROBACION, detener_recorrido=False,
):
    """Fase 2 — el FLUJO saca un ítem del alcance: una aprobación de su recorrido lo RECHAZÓ o lo EXCLUYÓ
    (`workflows.integracion`). Origen APROBACION, sin otra aprobación y con la observación del aprobador como
    justificación. No exige ser el responsable del ticket (lo autorizó la aprobación). Idempotente: si el ítem ya
    salió del alcance, devuelve esa decisión.

    Fase 3 — también la usa una GACETA (`origen="GACETA"`): entonces `detener_recorrido=True` detiene el
    recorrido del ítem si seguía en curso (la aprobación de un recorrido lo termina quien llama)."""
    justificacion = _justificacion(justificacion)
    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=ticket_id)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    Ticket.objects.select_for_update().get(pk=ticket_id)
    ParrillaTicket.objects.select_for_update(of=("self",)).get(pk=elemento.parrilla_id)
    elemento = ElementoParrilla.objects.select_for_update().select_related("parrilla").get(pk=elemento.pk)
    existente = decision_de_salida(elemento)
    if existente is not None:
        return existente
    decision = _crear_decision_de_salida(elemento, actor, tipo=tipo, justificacion=justificacion, origen=origen)
    if detener_recorrido:
        from apps.workflows import recorridos

        recorridos.sacar_elemento(elemento, actor, resultado=tipo, motivo=justificacion)
    return decision


def excluir_elemento(elemento, actor, justificacion):
    return decidir_salida_de_alcance(
        elemento, actor, tipo=DecisionAlcanceElemento.Tipo.EXCLUIDO, justificacion=justificacion
    )


def rechazar_elemento(elemento, actor, justificacion):
    return decidir_salida_de_alcance(
        elemento, actor, tipo=DecisionAlcanceElemento.Tipo.RECHAZADO, justificacion=justificacion
    )


@transaction.atomic
def agregar_elemento_extraordinario(ticket, actor, valores=None, *, clave=None, justificacion):
    """Agrega un ítem DESPUÉS de cerrar el alcance, solo si la colección lo permite (congelado en el
    Ticket), con justificación obligatoria. Queda registrado como ALTA_EXTRAORDINARIA."""
    justificacion = _justificacion(justificacion)
    ticket, parrilla = _bloquear(ticket.pk, actor, clave=clave)
    if not parrilla.alcance_cerrado:
        raise ValidationError("El alcance sigue abierto: agrega el ítem de la forma habitual.")
    if not parrilla.permite_altas_extraordinarias:
        raise ValidationError("Esta colección no admite altas extraordinarias después de cerrar su alcance.")
    elemento = _crear_elemento_bloqueado(ticket, parrilla, actor, valores, incorporar=False)
    decision = DecisionAlcanceElemento.objects.create(
        elemento=elemento, tipo=DecisionAlcanceElemento.Tipo.ALTA_EXTRAORDINARIA, justificacion=justificacion,
        decidido_por=actor,
    )
    _auditar(
        decision, actor, RegistroAuditoria.Accion.CREAR, None,
        {"elemento_id": elemento.pk, "numero": elemento.numero, "tipo": decision.tipo, "justificacion": justificacion},
    )
    # Ya registrada el alta, el ítem entra al recorrido en curso de su colección (si lo admite).
    from apps.workflows import recorridos

    recorridos.incorporar_elemento(ticket, elemento, actor=actor)
    _ingreso_automatico(elemento, actor)
    return elemento


def alcance_de(parrilla):
    """Alcance de una colección, CALCULADO: original (foto al cerrar, o `None` si sigue abierto), altas
    extraordinarias, excluidos, rechazados y efectivo (= vigentes que no salieron del alcance)."""
    vigentes = list(parrilla.elementos.filter(retirado_en__isnull=True).order_by("numero"))
    decisiones = list(
        DecisionAlcanceElemento.objects.filter(elemento__parrilla=parrilla)
        .select_related("elemento", "decidido_por").order_by("decidido_en", "pk")
    )
    salida = {d.elemento_id: d for d in decisiones if d.tipo in DecisionAlcanceElemento.TIPOS_DE_SALIDA}
    return {
        "cerrado": parrilla.alcance_cerrado,
        "cerrado_en": parrilla.alcance_cerrado_en,
        "cerrado_por": parrilla.alcance_cerrado_por,
        "original": [f["numero"] for f in (parrilla.alcance_original or [])] if parrilla.alcance_cerrado else None,
        "altas_extraordinarias": [d for d in decisiones if d.tipo == DecisionAlcanceElemento.Tipo.ALTA_EXTRAORDINARIA],
        "excluidos": [d for d in decisiones if d.tipo == DecisionAlcanceElemento.Tipo.EXCLUIDO],
        "rechazados": [d for d in decisiones if d.tipo == DecisionAlcanceElemento.Tipo.RECHAZADO],
        "efectivo": [e for e in vigentes if e.pk not in salida],
        "salida": salida,
    }


# --- Presentación mínima (vista del Ticket) ------------------------------------------

_INPUT_POR_TIPO = {
    Campo.TipoCampo.TEXTO: "text",
    Campo.TipoCampo.CORREO: "email",
    Campo.TipoCampo.URL: "url",
    Campo.TipoCampo.NUMERO: "number",
    Campo.TipoCampo.FECHA: "date",
    Campo.TipoCampo.FECHA_HORA: "datetime-local",
}
_REFERENCIAS = (Campo.TipoCampo.USUARIO, Campo.TipoCampo.AREA, Campo.TipoCampo.UNIDAD, Campo.TipoCampo.EQUIPO)


def _opciones_base(campo):
    """`[(valor, etiqueta)]` de una columna de selección (LISTA/MULTILISTA) o de referencia
    organizacional (usuario, área, unidad). Se calcula UNA vez por columna y página."""
    if campo.tipo in (Campo.TipoCampo.LISTA, Campo.TipoCampo.MULTILISTA):
        return [(o.valor, o.etiqueta) for o in campo.opciones.all()]
    if campo.tipo in _REFERENCIAS:
        return [(str(o.pk), str(o)) for o in ESTRATEGIAS_POR_TIPO[campo.tipo].queryset(campo)]
    return []


def _opciones_de(campo, base, seleccion):
    """`[(valor, etiqueta, seleccionada)]`. Una referencia ya guardada que dejó de estar disponible
    (p. ej. una persona desactivada) se conserva en la lista para no perder el valor."""
    opciones = [(valor, etiqueta, valor in seleccion) for valor, etiqueta in base]
    if campo.tipo in _REFERENCIAS:
        conocidas = {valor for valor, _ in base}
        modelo = ESTRATEGIAS_POR_TIPO[campo.tipo].modelo
        for pk in sorted(seleccion - conocidas):
            objeto = modelo.objects.filter(pk=pk).first()
            if objeto is not None:
                opciones.append((str(objeto.pk), f"{objeto} (no disponible)", True))
    return opciones


def _como_texto_de_input(campo, valor):
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return timezone.localtime(valor).strftime("%Y-%m-%dT%H:%M") if timezone.is_aware(valor) else valor.strftime("%Y-%m-%dT%H:%M")
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return format(valor.normalize(), "f")
    return str(valor)


def _texto_legible(campo, valor, opciones):
    if valor in (None, "", []):
        return "—"
    if campo.tipo == Campo.TipoCampo.BOOLEANO:
        return "Sí" if valor else "No"
    if campo.tipo in (Campo.TipoCampo.LISTA, Campo.TipoCampo.MULTILISTA, *_REFERENCIAS):
        elegidas = [etiqueta for _, etiqueta, marcada in opciones if marcada]
        return ", ".join(elegidas) or "—"
    if isinstance(valor, datetime) and timezone.is_aware(valor):
        return timezone.localtime(valor).strftime("%d/%m/%Y %H:%M")
    if isinstance(valor, date) and not isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    return _como_texto_de_input(campo, valor)


def _celda(campo, valor_elemento, bases, faltante=False, archivos=()):
    if campo.tipo == Campo.TipoCampo.ARCHIVO:
        return {
            "campo": campo, "soportado": True, "es_archivo": True, "tipo": campo.tipo,
            "obligatorio": campo.obligatorio, "faltante": faltante, "archivos": list(archivos),
            "texto": ", ".join(a.nombre_original for a in archivos),
        }
    valor = valor_elemento.valor if valor_elemento is not None else None
    if campo.tipo == Campo.TipoCampo.MULTILISTA:
        seleccion = {str(v) for v in (valor or [])}
    elif valor is None:
        seleccion = set()
    else:
        seleccion = {str(valor)}
    opciones = _opciones_de(campo, bases.get(campo.pk, []), seleccion)
    return {
        "campo": campo,
        "soportado": True,
        "es_archivo": False,
        "tipo": campo.tipo,
        "obligatorio": campo.obligatorio,
        "faltante": faltante,
        "input_type": _INPUT_POR_TIPO.get(campo.tipo, "text"),
        "valor": _como_texto_de_input(campo, valor),
        "marcado": bool(valor) if campo.tipo == Campo.TipoCampo.BOOLEANO else False,
        "opciones": opciones,
        "texto": _texto_legible(campo, valor, opciones),
    }


def tabla_de_elemento(elemento):
    """`[{"etiqueta", "texto"}]` de las columnas de UN ítem, en orden y en lenguaje legible (los archivos por
    su nombre). Solo lectura; lo usa, p. ej., quien decide la aprobación de un ítem (Fase 2)."""
    columnas = columnas_de(elemento.parrilla)
    bases = {c.pk: _opciones_base(c) for c in columnas}
    por_campo = {v.campo_id: v for v in elemento.valores.select_related("campo")}
    archivos = archivos_de(elemento)
    filas = []
    for campo in columnas:
        celda = _celda(campo, por_campo.get(campo.pk), bases, archivos=[a for a in archivos if a.campo_elemento_id == campo.pk])
        filas.append({"etiqueta": campo.etiqueta, "texto": celda["texto"] or "—"})
    return filas


def ficha_por_tarea(elemento, usuario):
    """Bloque 3.1 — UN ítem tal como lo ve el responsable de una tarea abierta que lo trabaja: todas sus celdas
    (solo lectura) con `editable` en las columnas que su tarea autoriza. `None` si no le autoriza ninguna. La
    autorización real la repite cada operación bajo lock."""
    from apps.tickets.edicion_por_tarea import columnas_por_tarea

    permitidas = columnas_por_tarea(usuario, elemento)
    if not permitidas:
        return None
    columnas = columnas_de(elemento.parrilla)
    bases = {c.pk: _opciones_base(c) for c in columnas}
    por_campo = {v.campo_id: v for v in elemento.valores.select_related("campo")}
    archivos = archivos_de(elemento)
    celdas = []
    for campo in columnas:
        celda = _celda(campo, por_campo.get(campo.pk), bases, archivos=[a for a in archivos if a.campo_elemento_id == campo.pk])
        celda["editable"] = campo.pk in permitidas
        celdas.append(celda)
    return {
        "elemento": elemento, "celdas": celdas, "nombre_elemento": elemento.parrilla.nombre_elemento or "Ítem",
        "celdas_valor": [c for c in celdas if c["editable"] and not c["es_archivo"]],
        "celdas_archivo": [c for c in celdas if c["editable"] and c["es_archivo"]],
    }


def columnas_editables_para(usuario, elemento):
    """Las columnas que `usuario` puede enviar al guardar `elemento`: todas si opera la colección; si no, las de
    sus tareas abiertas (Bloque 3.1). Solo filtra lo que se lee del formulario; la regla vive en la operación."""
    columnas = columnas_de(elemento.parrilla)
    if puede_operar_coleccion(usuario, elemento.parrilla):
        return columnas
    from apps.tickets.edicion_por_tarea import columnas_por_tarea

    permitidas = columnas_por_tarea(usuario, elemento)
    return [c for c in columnas if c.pk in permitidas]


def texto_de_valor_canonico(campo, valor):
    """Texto legible de un valor tal como lo guarda una `VersionElemento` (forma canónica: ids de referencias,
    claves de opciones, fechas ISO). Si la referencia ya no existe, muestra el valor guardado."""
    if valor in (None, "", []):
        return "—"
    if campo is None:
        return str(valor)
    if campo.tipo == Campo.TipoCampo.BOOLEANO:
        return "Sí" if valor else "No"
    if campo.tipo in (Campo.TipoCampo.LISTA, Campo.TipoCampo.MULTILISTA):
        etiquetas = {o.valor: o.etiqueta for o in campo.opciones.all()}
        valores = valor if isinstance(valor, list) else [valor]
        return ", ".join(etiquetas.get(str(v), str(v)) for v in valores)
    if campo.tipo in _REFERENCIAS:
        objeto = ESTRATEGIAS_POR_TIPO[campo.tipo].modelo.objects.filter(pk=valor).first()
        return str(objeto) if objeto is not None else str(valor)
    return str(valor)


def tablas(ticket, usuario):
    """Una `tabla` por colección del Ticket, en orden (lista vacía si no usa elementos)."""
    return [tabla(ticket, usuario, parrilla=parrilla) for parrilla in parrillas_de(ticket)]


def tabla(ticket, usuario, *, parrilla=None):
    """Lo que necesita la vista del Ticket: columnas, filas VIGENTES con sus celdas y su completitud,
    filas retiradas (aparte, solo lectura) y si `usuario` puede editar. Sin `parrilla`: la colección por
    defecto. `None` si el Ticket no usa elementos estructurados."""
    parrilla = parrilla or parrilla_de(ticket)
    if parrilla is None:
        return None
    columnas = columnas_de(parrilla)
    bases = {c.pk: _opciones_base(c) for c in columnas}

    hay_archivos = any(c.tipo == Campo.TipoCampo.ARCHIVO for c in columnas)

    def celdas_de(elemento, completitud):
        por_campo = {v.campo_id: v for v in elemento.valores.all()}
        faltan = {c.pk for c in completitud.faltantes} | {c.pk for c, _ in completitud.invalidos}
        archivos = archivos_de(elemento) if hay_archivos else []
        return [
            _celda(
                c, por_campo.get(c.pk), bases, faltante=c.pk in faltan,
                archivos=[a for a in archivos if a.campo_elemento_id == c.pk],
            )
            for c in columnas
        ]

    alcance = alcance_de(parrilla)
    filas = []
    for elemento in elementos_vigentes(parrilla).prefetch_related("versiones"):
        completitud = completitud_de(elemento, columnas)
        filas.append({
            "elemento": elemento, "completitud": completitud, "celdas": celdas_de(elemento, completitud),
            "salida": alcance["salida"].get(elemento.pk),
            "n_versiones": len(elemento.versiones.all()),
        })
    retiradas = [
        {"elemento": elemento, "celdas": celdas_de(elemento, Completitud())}
        for elemento in parrilla.elementos.filter(retirado_en__isnull=False)
        .select_related("retirado_por").prefetch_related("valores__campo").order_by("numero")
    ]
    return {
        "parrilla": parrilla,
        "es_por_defecto": es_coleccion_por_defecto(parrilla),
        "nombre_elemento": parrilla.nombre_elemento,
        "columnas": columnas,
        "hay_obligatorias": any(c.obligatorio for c in columnas),
        "filas": filas,
        "n_incompletas": sum(1 for f in filas if not f["completitud"].completo),
        "retiradas": retiradas,
        "n_retirados": len(retiradas),
        # Al dar de alta solo se piden los valores: los archivos se adjuntan después en la fila.
        "celdas_nuevas": [_celda(c, None, bases) for c in columnas if c.tipo != Campo.TipoCampo.ARCHIVO],
        "alcance": alcance,
        # Retirar, excluir, cerrar el alcance y altas extraordinarias: solo el responsable individual.
        "puede_editar": puede_editar_parrilla(usuario, ticket) and ticket.estado not in ESTADOS_SIN_CAMBIOS,
        # Bloque 3: crear y editar ítems (valores, archivos, versiones): también los participantes de la colección.
        "puede_operar": puede_operar_coleccion(usuario, parrilla, ticket),
    }


def valores_desde_post(post, columnas):
    """`{campo_id: crudo}` a partir de un formulario HTML: cada columna se llama `campo_<id>`. Un
    checkbox ausente es «No marcado»; un campo ausente (que no es checkbox) no se toca."""
    valores = {}
    for campo in columnas:
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            continue
        nombre = f"campo_{campo.pk}"
        if campo.tipo == Campo.TipoCampo.MULTILISTA:
            if nombre in post:
                valores[campo.pk] = post.getlist(nombre)
        elif campo.tipo == Campo.TipoCampo.BOOLEANO:
            valores[campo.pk] = nombre in post
        elif nombre in post:
            valores[campo.pk] = post.get(nombre)
    return valores
