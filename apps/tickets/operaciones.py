"""Operaciones de dominio de Tickets — incremento 2.1 (CU-014 parcial,
CU-015; RQF-049/050/051; RN-015).

Cubre únicamente el ciclo de vida de un BORRADOR: crear, guardar
respuestas (incluidos archivos de campos tipo ARCHIVO) y eliminar. Radicar,
generar el `radicado` y validar obligatoriedad (RQF-053/054) son 2.2 — no
se adelantan aquí.
"""

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, ReglaCondicional
from apps.catalogo.reglas import EspecificacionRegla
from apps.catalogo.visibilidad import servicios_visibles_para
from apps.tickets.autorizacion import es_propietario_borrador
from apps.tickets.models import (
    COLUMNA_POR_TIPO,
    COLUMNAS_REFERENCIA,
    ArchivoRespuestaCampo,
    RespuestaCampo,
    RespuestaFormulario,
    Ticket,
    TicketServicio,
)


@transaction.atomic
def crear_borrador(usuario, servicio):
    """CU-014 (parcial)/RQF-049 — inicia un Ticket de Servicio en BORRADOR.

    La visibilidad del Servicio (`servicios_visibles_para`, 1.1) se valida
    ÚNICAMENTE aquí. Editar/consultar/eliminar el borrador ya creado no
    vuelve a consultarla — ver `apps.tickets.autorizacion`.
    """
    if not servicios_visibles_para(usuario).filter(pk=servicio.pk).exists():
        raise PermissionDenied("El servicio no está activo o no es visible para este usuario.")

    formulario = servicio.formulario
    version = formulario.version_activa if formulario else None
    if version is None:
        raise ValidationError("El servicio no tiene un formulario activo utilizable todavía.")

    ticket = Ticket.objects.create(solicitante=usuario, tipo=Ticket.Tipo.SERVICIO)
    TicketServicio.objects.create(ticket=ticket, servicio=servicio, formulario_version=version)
    RespuestaFormulario.objects.create(ticket=ticket, formulario_version=version)
    return ticket


def _es_valor_vacio(valor_crudo):
    """Señal de "borrar esta respuesta" — un valor ausente/vacío nunca se
    valida como si fuera una respuesta real (RQF-053/obligatoriedad es
    2.2); simplemente se descarta lo que hubiera antes."""
    if valor_crudo is None:
        return True
    if isinstance(valor_crudo, str) and valor_crudo.strip() == "":
        return True
    if isinstance(valor_crudo, (list, tuple)) and len(valor_crudo) == 0:
        return True
    return False


def _es_visible(campo, valores_para_evaluar, reglas_por_objetivo):
    """Un campo objetivo de al menos una regla MOSTRAR es "condicional":
    empieza OCULTO y solo se revela cuando esa condición se satisface (el
    patrón usual de campo condicional — RQF-043). Un campo sin ninguna
    regla MOSTRAR dirigida a él es visible por defecto y solo una OCULTAR
    satisfecha lo esconde. En orden de `id` de regla, la última aplicable
    (satisfecha) gana. Con reglas MOSTRAR/OCULTAR conflictivas sobre el
    mismo campo esto es un comportamiento mínimo y determinista para 2.1
    (última regla aplicable gana) — la composición o precedencia
    definitiva es una propuesta pendiente, explícitamente diferida a antes
    de 2.2 (no resuelta aquí, por instrucción del usuario)."""
    reglas = reglas_por_objetivo.get(campo.id, ())
    depende_de_mostrar = any(regla.efecto == ReglaCondicional.Efecto.MOSTRAR for regla in reglas)
    visible = not depende_de_mostrar
    for regla in reglas:
        if not EspecificacionRegla(regla).es_satisfecha_por(valores_para_evaluar):
            continue
        if regla.efecto == ReglaCondicional.Efecto.OCULTAR:
            visible = False
        elif regla.efecto == ReglaCondicional.Efecto.MOSTRAR:
            visible = True
    return visible


def _upsert_respuesta_escalar(respuesta_formulario, campo, valor_normalizado, existente):
    columna = COLUMNA_POR_TIPO[campo.tipo]
    instancia = existente or RespuestaCampo(respuesta_formulario=respuesta_formulario, campo=campo)
    if columna in COLUMNAS_REFERENCIA:
        setattr(instancia, f"{columna}_id", valor_normalizado)
    elif columna == "valor_numero":
        setattr(instancia, columna, Decimal(str(valor_normalizado)))
    else:
        setattr(instancia, columna, valor_normalizado)
    instancia.save()
    return instancia


def _guardar_archivo(respuesta_formulario, campo, archivo_subido, actor, existente):
    """Reutiliza `EstrategiaArchivo.validar_valor()` (`campos.py`, sin
    modificar) contra las restricciones ya configuradas en
    `campo.configuracion` (`extensiones_permitidas`/`tamano_maximo_mb`) —
    ningún límite nuevo se inventa aquí (RQF-007)."""
    estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
    metadata = {"nombre": archivo_subido.name, "tamano_mb": archivo_subido.size / (1024 * 1024)}
    estrategia.validar_valor(campo, metadata)

    instancia = existente or RespuestaCampo(respuesta_formulario=respuesta_formulario, campo=campo)
    if existente is None:
        instancia.save()

    archivo_anterior = getattr(instancia, "archivo", None)
    if archivo_anterior is not None:
        archivo_anterior.delete()  # dispara el pre_delete que libera el archivo físico

    ArchivoRespuestaCampo.objects.create(
        respuesta_campo=instancia,
        archivo=archivo_subido,
        nombre_original=archivo_subido.name,
        tipo_mime=getattr(archivo_subido, "content_type", "") or "",
        tamano_bytes=archivo_subido.size,
        subido_por=actor,
    )
    return instancia


@transaction.atomic
def guardar_respuestas_borrador(ticket, actor, respuestas_crudas):
    """CU-015/RQF-050 — guarda (upsert) respuestas de un borrador.

    `respuestas_crudas` es `{campo_id: valor_crudo}` — solo los campos
    presentes en el dict se tocan en esta llamada; los demás conservan su
    valor previo (o su ausencia). Un valor vacío/`None` para un campo
    presente en el dict borra su respuesta existente.

    Reglas MOSTRAR/OCULTAR se reevalúan en cada llamada contra el conjunto
    completo de respuestas (previas + las de esta llamada): cualquier
    campo que resulte oculto pierde su `RespuestaCampo`, exista o no en
    `respuestas_crudas` — un cambio en OTRA respuesta puede ocultar un
    campo que ya tenía valor guardado.
    """
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede modificar este borrador.")
    if ticket.estado != Ticket.Estado.BORRADOR:
        raise ValidationError("Solo se pueden guardar respuestas mientras el ticket es un borrador.")

    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    campos = {c.id: c for c in version.campos.prefetch_related("opciones")}
    existentes = {
        rc.campo_id: rc
        for rc in respuesta_formulario.respuestas_campo.select_related("campo").all()
    }

    valores_para_evaluar = {campo_id: rc.valor for campo_id, rc in existentes.items()}
    valores_para_evaluar.update(respuestas_crudas)

    reglas = (
        ReglaCondicional.objects.filter(
            campo_origen__version=version,
            efecto__in=[ReglaCondicional.Efecto.MOSTRAR, ReglaCondicional.Efecto.OCULTAR],
        )
        .select_related("campo_origen", "campo_objetivo")
        .order_by("id")
    )
    reglas_por_objetivo = defaultdict(list)
    for regla in reglas:
        reglas_por_objetivo[regla.campo_objetivo_id].append(regla)

    for campo_id, campo in campos.items():
        existente = existentes.get(campo_id)
        visible = _es_visible(campo, valores_para_evaluar, reglas_por_objetivo)

        if not visible:
            if existente is not None:
                existente.delete()
            continue

        if campo_id not in respuestas_crudas:
            continue  # no se modifica lo que no vino en esta llamada

        valor_crudo = respuestas_crudas[campo_id]

        if _es_valor_vacio(valor_crudo):
            if existente is not None:
                existente.delete()
            continue

        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            _guardar_archivo(respuesta_formulario, campo, valor_crudo, actor, existente)
            continue

        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        estrategia.validar_valor(campo, valor_crudo)
        valor_normalizado = estrategia.normalizar(valor_crudo)
        _upsert_respuesta_escalar(respuesta_formulario, campo, valor_normalizado, existente)

    return respuesta_formulario


def eliminar_borrador(ticket, actor):
    """CU-015/RQF-051. `Ticket.delete()` exige `estado == BORRADOR`
    (`Ticket.exigir_eliminable`) y arrastra en cascada `TicketServicio`,
    `RespuestaFormulario`, `RespuestaCampo` y `ArchivoRespuestaCampo`
    (incluido el archivo físico, vía el `pre_delete` de `apps.py`)."""
    if not es_propietario_borrador(actor, ticket):
        raise PermissionDenied("Solo el solicitante puede eliminar este borrador.")
    ticket.delete()
