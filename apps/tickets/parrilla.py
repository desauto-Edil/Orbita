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
- ARCHIVO no está soportado (exigiría ampliar `Adjunto`).

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

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone as zona_utc
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, DefinicionParrilla
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.tickets.autorizacion import puede_consultar_ticket, puede_editar_parrilla
from apps.tickets.models import (
    COLUMNA_POR_TIPO,
    COLUMNAS_REFERENCIA,
    ElementoParrilla,
    ParrillaTicket,
    Ticket,
    ValorElemento,
)
from apps.tickets.operaciones import _es_valor_vacio
from apps.workflows.models import InstanciaWorkflow

ESTADOS_SIN_CAMBIOS = (Ticket.Estado.RESUELTO, Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO)


# --- Snapshot al nacer el Ticket ------------------------------------------------


def crear_parrilla_para_borrador(ticket, servicio):
    """Congela la estructura de elementos del Proceso en `ticket` (recién creado, en BORRADOR). Lo
    llama `operaciones._crear_borrador_de_servicio`, dentro de su transacción. Devuelve la
    `ParrillaTicket`, o `None` si el Proceso no usa elementos (entonces no se crea nada)."""
    definicion = (
        DefinicionParrilla.objects.select_related("formulario__version_activa")
        .filter(servicio=servicio, activa=True)
        .first()
    )
    if definicion is None:
        return None
    version = definicion.formulario.version_activa
    if version is None or version.formulario_id != definicion.formulario_id or version.estado != "ACTIVA":
        raise ValidationError(
            "La estructura de elementos de este proceso no tiene una versión activa utilizable todavía."
        )
    parrilla = ParrillaTicket.objects.create(
        ticket=ticket, formulario_version=version, nombre_elemento=definicion.nombre_elemento
    )
    # 4.G2D1: si el Proceso genera tareas por elemento, este Ticket congela esa configuración. No genera
    # ninguna tarea: la materialización es una operación explícita.
    from apps.tickets.tareas_elemento import congelar_configuracion

    congelar_configuracion(parrilla, servicio)
    return parrilla


# --- Consulta ------------------------------------------------------------------


def parrilla_de(ticket):
    """La `ParrillaTicket` del Ticket, o `None` si su Proceso no usa elementos."""
    return ParrillaTicket.objects.select_related("formulario_version").filter(ticket_id=ticket.pk).first()


def columnas_de(parrilla):
    """Los campos (columnas) de la versión congelada, en orden, con sus opciones."""
    return list(parrilla.formulario_version.campos.prefetch_related("opciones").order_by("orden", "id"))


def elementos_vigentes(parrilla):
    return parrilla.elementos.filter(retirado_en__isnull=True).prefetch_related("valores__campo")


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
    faltantes, invalidos = [], []
    for campo in columnas:
        if not campo.obligatorio or campo.tipo == Campo.TipoCampo.ARCHIVO:
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
                f"«{campo.etiqueta}»: los campos de tipo archivo todavía no se admiten en los elementos."
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


def _aplicar_valores(elemento, parrilla, valores, protegidas=None, protegidas_tarea=frozenset()):
    """Guarda (upsert) los valores indicados; un valor vacío borra el existente. Devuelve
    `(anterior, nuevo)` por clave del campo, para la auditoría. Solo toca los campos presentes.

    `protegidas` (4.G2C2): `{campo_id: entregable}` de columnas que un entregable de conjunto ya
    consumido por el flujo congeló. Reenviar el mismo valor no es un cambio (el formulario de edición
    envía todas las columnas); cambiarlo, borrarlo o escribirlo donde no había se rechaza.

    `protegidas_tarea` (4.G2D2): ids de las columnas de las que salió la Tarea de ESTE elemento; mismas
    reglas, y el resto de columnas del elemento sigue editable."""
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
        instancia.save()
        valor_nuevo = instancia.valor
        if valor_nuevo != actual:
            anterior[campo.clave] = _auditable(actual)
            nuevo[campo.clave] = _auditable(valor_nuevo)
    return anterior, nuevo


# --- Operaciones ------------------------------------------------------------------


def _bloquear(ticket_id, actor):
    """Lock del Ticket, comprobación de que sigue siendo operable y de que `actor` puede editar, y
    lock de su parrilla. Orden de locks: instancia de Workflow (si hay; es el orden del motor y de los
    entregables) → Ticket → parrilla."""
    instancia_id = Ticket.objects.values_list("instancia_workflow_id", flat=True).get(pk=ticket_id)
    if instancia_id is not None:
        InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    if ticket.estado in ESTADOS_SIN_CAMBIOS:
        raise ValidationError("Este ticket ya no admite cambios en sus elementos.")
    if not puede_editar_parrilla(actor, ticket):
        raise PermissionDenied(
            "Solo el responsable individual de un ticket en atención puede modificar sus elementos."
        )
    parrilla = ParrillaTicket.objects.select_for_update(of=("self",)).select_related("formulario_version").filter(
        ticket=ticket
    ).first()
    if parrilla is None:
        raise ValidationError("Este ticket no usa elementos estructurados.")
    return ticket, parrilla


def _auditar(instancia, actor, accion, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=instancia, origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )


@transaction.atomic
def crear_elemento(ticket, actor, valores=None):
    """Agrega una fila al final de la parrilla del Ticket, con los valores iniciales indicados
    (`{campo | campo_id: valor}`, opcionales). El número y la posición salen del lock de la parrilla."""
    from apps.tickets import revision_elementos

    ticket, parrilla = _bloquear(ticket.pk, actor)
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
    return elemento


@transaction.atomic
def actualizar_valores(elemento, actor, valores):
    """Modifica los valores indicados de una fila vigente (los demás no se tocan; un valor vacío
    borra el existente). Se puede hacer durante toda la ejecución del Ticket."""
    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    ticket, parrilla = _bloquear(ticket_id, actor)
    elemento = ElementoParrilla.objects.select_for_update().get(pk=elemento.pk)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento está retirado y ya no se puede modificar.")
    from apps.tickets import revision_elementos

    # 4.G2C2: las columnas revisadas de un entregable consumido por el flujo no cambian; las ajenas sí.
    from apps.tickets import tareas_elemento

    anterior, nuevo = _aplicar_valores(
        elemento, parrilla, valores, protegidas=revision_elementos.columnas_protegidas(ticket),
        protegidas_tarea=tareas_elemento.columnas_protegidas_de(elemento),
    )
    if nuevo:
        _auditar(
            elemento, actor, RegistroAuditoria.Accion.ACTUALIZAR,
            {"numero": elemento.numero, "valores": anterior}, {"numero": elemento.numero, "valores": nuevo},
        )
    return elemento


@transaction.atomic
def retirar_elemento(elemento, actor):
    """Retiro LÓGICO: el elemento y sus valores se conservan, pero deja de aparecer entre los
    vigentes. Su número no se reutiliza. Retirar uno ya retirado se RECHAZA (no se reescribe quién ni
    cuándo lo retiró)."""
    from apps.tickets import revision_elementos

    ticket_id = ParrillaTicket.objects.values_list("ticket_id", flat=True).get(pk=elemento.parrilla_id)
    ticket, _ = _bloquear(ticket_id, actor)
    elemento = ElementoParrilla.objects.select_for_update().get(pk=elemento.pk)
    if elemento.retirado_en is not None:
        raise ValidationError("El elemento ya está retirado.")
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
    # El conjunto vigente cambió (el elemento sale del agregado): avisa al flujo por el cauce de siempre.
    revision_elementos.avisar_flujo(ticket)
    return elemento


# --- Presentación mínima (vista del Ticket) ------------------------------------------

_INPUT_POR_TIPO = {
    Campo.TipoCampo.TEXTO: "text",
    Campo.TipoCampo.CORREO: "email",
    Campo.TipoCampo.URL: "url",
    Campo.TipoCampo.NUMERO: "number",
    Campo.TipoCampo.FECHA: "date",
    Campo.TipoCampo.FECHA_HORA: "datetime-local",
}
_REFERENCIAS = (Campo.TipoCampo.USUARIO, Campo.TipoCampo.AREA, Campo.TipoCampo.UNIDAD)


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


def _celda(campo, valor_elemento, bases, faltante=False):
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
        "soportado": campo.tipo != Campo.TipoCampo.ARCHIVO,
        "tipo": campo.tipo,
        "obligatorio": campo.obligatorio,
        "faltante": faltante,
        "input_type": _INPUT_POR_TIPO.get(campo.tipo, "text"),
        "valor": _como_texto_de_input(campo, valor),
        "marcado": bool(valor) if campo.tipo == Campo.TipoCampo.BOOLEANO else False,
        "opciones": opciones,
        "texto": _texto_legible(campo, valor, opciones),
    }


def tabla(ticket, usuario):
    """Lo que necesita la vista del Ticket: columnas, filas VIGENTES con sus celdas y su completitud,
    filas retiradas (aparte, solo lectura) y si `usuario` puede editar. `None` si el Ticket no usa
    elementos estructurados."""
    parrilla = parrilla_de(ticket)
    if parrilla is None:
        return None
    columnas = columnas_de(parrilla)
    bases = {c.pk: _opciones_base(c) for c in columnas}

    def celdas_de(elemento, completitud):
        por_campo = {v.campo_id: v for v in elemento.valores.all()}
        faltan = {c.pk for c in completitud.faltantes} | {c.pk for c, _ in completitud.invalidos}
        return [_celda(c, por_campo.get(c.pk), bases, faltante=c.pk in faltan) for c in columnas]

    filas = []
    for elemento in elementos_vigentes(parrilla):
        completitud = completitud_de(elemento, columnas)
        filas.append({"elemento": elemento, "completitud": completitud, "celdas": celdas_de(elemento, completitud)})
    retiradas = [
        {"elemento": elemento, "celdas": celdas_de(elemento, Completitud())}
        for elemento in parrilla.elementos.filter(retirado_en__isnull=False)
        .select_related("retirado_por").prefetch_related("valores__campo").order_by("numero")
    ]
    return {
        "parrilla": parrilla,
        "nombre_elemento": parrilla.nombre_elemento,
        "columnas": columnas,
        "hay_obligatorias": any(c.obligatorio for c in columnas),
        "filas": filas,
        "n_incompletas": sum(1 for f in filas if not f["completitud"].completo),
        "retiradas": retiradas,
        "n_retirados": len(retiradas),
        "celdas_nuevas": [_celda(c, None, bases) for c in columnas],
        "puede_editar": puede_editar_parrilla(usuario, ticket) and ticket.estado not in ESTADOS_SIN_CAMBIOS,
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
