"""Revisión de piezas en el espacio del ticket (UX-5): decidir la aprobación de un ítem desde su visor.

No es otra forma de aprobar: la decisión es la de siempre (`aprobaciones.decisiones.aplicar`, que elige la vía según
el origen — ronda de gaceta, Workflow… — y revalida permisos y estado). Este módulo añade solo lo que la revisión de
piezas exige en la pantalla:

- Tres salidas con nombres de la revisión: Aprobar (APROBADA), Corregir (DEVUELTA) y Eliminar (EXCLUIDA, solo donde
  la aprobación permite excluir: `EsquemaAprobacion.permite_exclusion`). Rechazar no se ofrece aquí.
- Un comentario OBLIGATORIO con cualquier salida (se guarda como la observación de la decisión).
- Una anotación visual opcional (PNG dibujado sobre la imagen del ítem) que acompaña a la decisión
  (`AnotacionRevision`); solo en la aprobación de un ítem del flujo, nunca en una ronda de gaceta.

Todo en UNA transacción: si la decisión no procede, no queda anotación."""

import base64
import binascii

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction

from apps.tickets.models import Adjunto, AnotacionRevision

SALIDAS = (("APROBADA", "Aprobar"), ("DEVUELTA", "Corregir"), ("EXCLUIDA", "Eliminar"))
LONGITUD_COMENTARIO = 2000
MAXIMO_ANOTACION = 8 * 1024 * 1024
_PREFIJO_PNG = "data:image/png;base64,"
_FIRMA_PNG = b"\x89PNG\r\n\x1a\n"


def salidas_de(form):
    """Las salidas de la revisión que esta aprobación admite, en orden (Aprobar · Corregir · Eliminar)."""
    if form is None:
        return []
    admitidas = {valor for valor, _ in form.fields["decision"].choices}
    return [{"valor": valor, "etiqueta": etiqueta} for valor, etiqueta in SALIDAS if valor in admitidas]


def es_de_gaceta(aprobacion):
    return hasattr(aprobacion.esquema, "ronda_gaceta")


def decodificar_anotacion(texto):
    """`data:image/png;base64,…` → `ContentFile` PNG validado (firma y tamaño), o `None` si no llegó nada."""
    texto = (texto or "").strip()
    if not texto:
        return None
    if not texto.startswith(_PREFIJO_PNG):
        raise ValidationError("La anotación debe ser una imagen PNG.")
    try:
        datos = base64.b64decode(texto[len(_PREFIJO_PNG):], validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValidationError("La anotación no es una imagen válida.") from error
    if len(datos) > MAXIMO_ANOTACION or not datos.startswith(_FIRMA_PNG):
        raise ValidationError("La anotación no es una imagen válida.")
    return ContentFile(datos, name="anotacion.png")


def _aprobacion_del_ticket(ticket, aprobacion_id):
    from apps.tickets import vinculos

    aprobacion = vinculos.aprobaciones_del_ticket(ticket).select_related("esquema").filter(pk=aprobacion_id).first()
    if aprobacion is None:
        raise PermissionDenied("Esta aprobación no pertenece al ticket.")
    return aprobacion


@transaction.atomic
def decidir(ticket, aprobacion_id, usuario, *, decision, comentario, anotacion="", archivo_base_id=None):
    """Decide la aprobación `aprobacion_id` del ticket con un comentario obligatorio y, si llega, guarda la
    anotación visual de la pieza. Devuelve la `Aprobacion`."""
    from apps.aprobaciones.autorizacion import puede_aprobar
    from apps.aprobaciones.decisiones import aplicar
    from apps.aprobaciones.views import _contexto_item, _decision_form

    aprobacion = _aprobacion_del_ticket(ticket, aprobacion_id)
    if not puede_aprobar(usuario, aprobacion):
        raise PermissionDenied("No te corresponde decidir esta revisión.")
    form = _decision_form(aprobacion, es_prorroga=getattr(aprobacion.esquema, "prorroga", None) is not None)
    if decision not in {s["valor"] for s in salidas_de(form)}:
        raise ValidationError("Esa salida no está disponible en esta revisión.")
    comentario = (comentario or "").strip()
    if not comentario:
        raise ValidationError("Escribe un comentario: acompaña a cualquier decisión.")
    if len(comentario) > LONGITUD_COMENTARIO:
        raise ValidationError(f"El comentario supera los {LONGITUD_COMENTARIO} caracteres.")
    imagen = decodificar_anotacion(anotacion)
    item = _contexto_item(aprobacion) if imagen is not None else None
    if imagen is not None and (es_de_gaceta(aprobacion) or item is None):
        raise ValidationError("Las anotaciones solo acompañan la aprobación de un ítem, no una gaceta.")

    aplicar(aprobacion, usuario, decision=decision, observacion=comentario)

    if imagen is not None:
        elemento = item["elemento"]
        base = None
        if archivo_base_id:
            base = Adjunto.objects.filter(pk=archivo_base_id, elemento=elemento).first()
        AnotacionRevision.objects.create(
            aprobacion=aprobacion, elemento=elemento, archivo_base=base, imagen=imagen, autor=usuario
        )
    return aprobacion


def anotaciones_de(elemento):
    """Las anotaciones de las revisiones de un ítem, de la más reciente a la más antigua."""
    return list(elemento.anotaciones.select_related("autor", "aprobacion"))
