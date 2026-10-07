"""Fecha requerida por el solicitante frente al tiempo objetivo del servicio (4.F2).

El campo que representa el plazo lo marca explícitamente quien configura el formulario
(`Campo.es_fecha_requerida`); nada se infiere por nombre o tipo. La fecha pedida se lee de las
respuestas CONGELADAS del ticket (`RespuestaCampo`), que ya son el snapshot de lo que se
solicitó: no se copia a otro lugar ni depende de la configuración vigente del formulario.

La fecha objetivo NUNCA se calcula aquí: es `tiempos.fecha_objetivo_de` (4.A1, con su regla de
días hábiles). Esto solo la compara. Es información, no una regla: no bloquea la radicación, no
cambia prioridad, SLA ni concede prórroga."""

from django.utils import timezone

from apps.catalogo.models import Campo
from apps.tickets import tiempos
from apps.tickets.models import RespuestaCampo

_UNIDADES = {
    tiempos.UNIDAD_HORAS: ("hora", "horas"),
    tiempos.UNIDAD_DIAS: ("día", "días"),
}


def descripcion_tiempo(ticket):
    """«5 días hábiles», «8 horas»…; `""` si el ticket no tiene tiempo objetivo."""
    cantidad = ticket.tiempo_objetivo_cantidad
    if cantidad is None:
        return ""
    singular, plural = _UNIDADES.get(ticket.tiempo_objetivo_unidad, ("", ""))
    texto = f"{cantidad} {singular if cantidad == 1 else plural}"
    if ticket.tiempo_objetivo_habiles:
        texto += " hábil" if cantidad == 1 else " hábiles"
    return texto


def fecha_solicitada(ticket):
    """La fecha que pidió el solicitante, o `None` si el formulario no tiene fecha requerida o
    no la diligenció. Dict: `etiqueta`, `valor` (date o datetime) y `con_hora`."""
    if ticket.pk is None or not hasattr(ticket, "respuesta_formulario"):
        return None
    respuesta = (
        ticket.respuesta_formulario.respuestas_campo.filter(campo__es_fecha_requerida=True)
        .select_related("campo")
        .first()
    )
    if respuesta is None:
        return None
    con_hora = respuesta.campo.tipo == Campo.TipoCampo.FECHA_HORA
    valor = respuesta.valor_fecha_hora if con_hora else respuesta.valor_fecha
    if valor is None:
        return None
    return {"etiqueta": respuesta.campo.etiqueta, "valor": valor, "con_hora": con_hora}


def evaluar_plazo(ticket, *, ahora=None):
    """Compara la fecha pedida con la fecha objetivo del servicio.

    Ticket radicado: la fecha objetivo ORIGINAL que se fijó al radicar. Borrador: la que
    resultaría de radicar ahora (mismo cálculo, sobre el tiempo objetivo ya congelado).
    `None` si no hay fecha requerida diligenciada o el servicio no tiene tiempo objetivo.
    `anticipada` es verdadera cuando se pidió algo antes de lo establecido."""
    pedida = fecha_solicitada(ticket)
    if pedida is None:
        return None
    objetivo = ticket.fecha_objetivo_original
    if objetivo is None:
        objetivo = tiempos.fecha_objetivo_de(ticket, ahora or timezone.now())
    if objetivo is None:
        return None
    if pedida["con_hora"]:
        anticipada = pedida["valor"] < objetivo
    else:
        # Una fecha sin hora se compara por día: pedir el mismo día del objetivo no es anticiparse.
        anticipada = pedida["valor"] < timezone.localtime(objetivo).date()
    return {
        **pedida,
        "objetivo": objetivo,
        "tiempo": descripcion_tiempo(ticket),
        "anticipada": anticipada,
    }
