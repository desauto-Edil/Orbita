"""Administración de los términos de búsqueda de un Servicio/Proceso (Sprint 4.D).

Un término es una palabra o frase con la que alguien podría describir la necesidad
que cubre un Servicio ("presentación", "días libres"). Alimenta solo el buscador
"¿Qué necesitas?" (`apps.catalogo.busqueda`): no es un campo del formulario, no se
pregunta al solicitante y no afecta a Tickets ni a Workflows.

Todas las operaciones exigen `catalogo.administrar` y se auditan. Reglas:

- Un término necesita al menos una palabra con significado (no solo "para", "de").
- Dos términos de un MISMO Servicio no pueden ser equivalentes una vez normalizados
  ("Presentación" y "presentacion" son el mismo); entre Servicios distintos sí
  pueden repetirse (dos Servicios pueden cubrir la misma palabra).
- A lo sumo `MAX_TERMINOS_POR_SERVICIO` por Servicio: acota el costo del puntaje.
- El Servicio interno del Ticket General no admite términos: nunca participa en la
  búsqueda (se ofrece como acción alterna).
- Eliminar es posible porque nada guarda referencias a un término (no es
  trazabilidad de Tickets); la auditoría conserva su valor anterior. Desactivar lo
  deja guardado sin que cuente en la búsqueda.

El alta, la edición y el borrado serializan por la fila del Servicio: el chequeo de
equivalencia más el insert no es atómico por sí solo.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalogo import normalizacion
from apps.catalogo.models import TerminoServicio
from apps.catalogo.operaciones import _bloquear_servicio, _exigir_administracion
from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria

MAX_TERMINOS_POR_SERVICIO = 30


def terminos_de(servicio):
    """Todos los términos del Servicio (activos e inactivos), en orden alfabético."""
    return list(servicio.terminos_busqueda.all())


def _limpiar(texto):
    """Texto tal como se guardará: sin espacios sobrantes. Valida largo y contenido."""
    texto = " ".join(str(texto or "").split())
    if not texto:
        raise ValidationError("Escribe el término.")
    if len(texto) > TerminoServicio.LARGO_MAXIMO:
        raise ValidationError(f"El término no puede superar {TerminoServicio.LARGO_MAXIMO} caracteres.")
    if not normalizacion.palabras(texto):
        raise ValidationError("El término necesita al menos una palabra concreta (no solo palabras como «para» o «de»).")
    return texto


def _validar_no_duplicado(servicio, texto, *, excluir_pk=None):
    normalizado = normalizacion.normalizar(texto)
    for existente in servicio.terminos_busqueda.exclude(pk=excluir_pk):
        if normalizacion.normalizar(existente.termino) == normalizado:
            sufijo = "" if existente.activo else " (está desactivado: puedes activarlo)"
            raise ValidationError(f"Ya existe un término equivalente en este servicio: «{existente.termino}»{sufijo}.")


def _auditar(accion, instancia, actor, anterior, nuevo):
    registrar_evento(
        accion=accion, instancia=instancia, origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=nuevo,
    )


def _bloquear_termino(termino):
    """Bloquea el Servicio y luego el término (mismo orden siempre); devuelve el
    término fresco."""
    _bloquear_servicio(termino.servicio)
    return TerminoServicio.objects.select_for_update().get(pk=termino.pk)


@transaction.atomic
def crear_termino(servicio, actor, texto):
    _exigir_administracion(actor)
    servicio = _bloquear_servicio(servicio)
    if servicio.es_ticket_general:
        raise ValidationError("El ticket general no participa en la búsqueda: no admite términos.")
    texto = _limpiar(texto)
    _validar_no_duplicado(servicio, texto)
    if servicio.terminos_busqueda.count() >= MAX_TERMINOS_POR_SERVICIO:
        raise ValidationError(f"Un servicio admite hasta {MAX_TERMINOS_POR_SERVICIO} términos de búsqueda.")
    termino = TerminoServicio.objects.create(servicio=servicio, termino=texto, activo=True)
    _auditar(RegistroAuditoria.Accion.CREAR, termino, actor, None, serializar(termino))
    return termino


@transaction.atomic
def editar_termino(termino, actor, texto):
    _exigir_administracion(actor)
    termino = _bloquear_termino(termino)
    texto = _limpiar(texto)
    if texto == termino.termino:
        return termino
    _validar_no_duplicado(termino.servicio, texto, excluir_pk=termino.pk)
    anterior = serializar(termino)
    termino.termino = texto
    termino.save(update_fields=["termino", "actualizado_en"])
    _auditar(RegistroAuditoria.Accion.ACTUALIZAR, termino, actor, anterior, serializar(termino))
    return termino


@transaction.atomic
def cambiar_estado_termino(termino, actor, *, activo):
    """Activa o desactiva un término. Idempotente: repetir no duplica auditoría."""
    _exigir_administracion(actor)
    termino = _bloquear_termino(termino)
    activo = bool(activo)
    if termino.activo == activo:
        return termino
    anterior = serializar(termino)
    termino.activo = activo
    termino.save(update_fields=["activo", "actualizado_en"])
    _auditar(RegistroAuditoria.Accion.ACTUALIZAR, termino, actor, anterior, serializar(termino))
    return termino


@transaction.atomic
def eliminar_termino(termino, actor):
    _exigir_administracion(actor)
    termino = _bloquear_termino(termino)
    # Se audita antes de borrar: después el objeto pierde su `pk`.
    _auditar(RegistroAuditoria.Accion.ELIMINAR, termino, actor, serializar(termino), None)
    termino.delete()
