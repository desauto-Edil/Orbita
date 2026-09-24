"""Validación final de respuestas de Ticket — incremento 2.2 (RQF-043,
RQF-044, RQF-053, RN-012).

Único lugar donde se calcula el estado efectivo (visible/requerido) de cada
Campo de una `FormularioVersion` congelada, reutilizando `EstrategiaCampo`
(`apps/catalogo/campos.py`) y `EspecificacionRegla`
(`apps/catalogo/reglas.py`) sin duplicar su lógica. Lo usan tanto
`apps.tickets.operaciones.guardar_respuestas_borrador` (2.1, para saber qué
ocultar/purgar) como `apps.tickets.operaciones.radicar_ticket` (2.2, para
validar completitud antes de radicar).

**Composición de reglas (decisión aprobada por el usuario)**: dos reglas de
efectos opuestos dentro de la misma familia (MOSTRAR/OCULTAR,
REQUERIR/NO_REQUERIR) sobre el mismo `campo_objetivo` están prohibidas
desde que se configuran (`apps/catalogo/reglas.py::validar_composicion_reglas`,
exigido en `ReglaCondicional.save()`) — por lo tanto, al evaluar aquí, un
campo nunca tiene simultáneamente reglas MOSTRAR y OCULTAR, ni REQUERIR y
NO_REQUERIR: no hay precedencia que resolver en tiempo de ejecución, solo
combinar por OR las reglas del mismo efecto.

`validar_para_radicar()` es de solo lectura: no purga ni modifica ninguna
respuesta (esa responsabilidad es de `guardar_respuestas_borrador`) — un
campo oculto nunca se considera obligatorio, incluso si por cualquier
motivo tuviera un valor persistido residual.
"""

from collections import defaultdict, namedtuple

from django.core.exceptions import ValidationError

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.models import Campo, ReglaCondicional
from apps.catalogo.reglas import EspecificacionRegla

EstadoEfectivo = namedtuple("EstadoEfectivo", ["visible", "requerido"])


def _reglas_por_objetivo_y_efecto(version):
    reglas = (
        ReglaCondicional.objects.filter(campo_origen__version=version)
        .select_related("campo_origen", "campo_objetivo")
        .order_by("id")
    )
    agrupadas = defaultdict(lambda: defaultdict(list))
    for regla in reglas:
        agrupadas[regla.campo_objetivo_id][regla.efecto].append(regla)
    return agrupadas


def estado_efectivo(campo, valores, reglas_por_efecto):
    """`valores` es `{campo_id: valor_crudo_o_normalizado}` con el estado a
    evaluar. `reglas_por_efecto` es `{efecto: [ReglaCondicional, ...]}` —
    únicamente las reglas cuyo `campo_objetivo` es `campo`."""
    reglas_mostrar = reglas_por_efecto.get(ReglaCondicional.Efecto.MOSTRAR, ())
    reglas_ocultar = reglas_por_efecto.get(ReglaCondicional.Efecto.OCULTAR, ())
    if reglas_mostrar:
        visible = any(EspecificacionRegla(r).es_satisfecha_por(valores) for r in reglas_mostrar)
    elif reglas_ocultar:
        visible = not any(EspecificacionRegla(r).es_satisfecha_por(valores) for r in reglas_ocultar)
    else:
        visible = True

    if not visible:
        return EstadoEfectivo(visible=False, requerido=False)  # axioma: oculto -> no requerido

    reglas_requerir = reglas_por_efecto.get(ReglaCondicional.Efecto.REQUERIR, ())
    reglas_no_requerir = reglas_por_efecto.get(ReglaCondicional.Efecto.NO_REQUERIR, ())
    if reglas_requerir:
        alguna_satisfecha = any(EspecificacionRegla(r).es_satisfecha_por(valores) for r in reglas_requerir)
        requerido = True if alguna_satisfecha else campo.obligatorio
    elif reglas_no_requerir:
        alguna_satisfecha = any(EspecificacionRegla(r).es_satisfecha_por(valores) for r in reglas_no_requerir)
        requerido = False if alguna_satisfecha else campo.obligatorio
    else:
        requerido = campo.obligatorio

    return EstadoEfectivo(visible=True, requerido=requerido)


def calcular_estados_efectivos(version, valores):
    """`{campo_id: EstadoEfectivo}` para todos los campos de `version`."""
    reglas_agrupadas = _reglas_por_objetivo_y_efecto(version)
    return {
        campo.id: estado_efectivo(campo, valores, reglas_agrupadas.get(campo.id, {}))
        for campo in version.campos.all()
    }


def validar_para_radicar(ticket):
    """RQF-044/RQF-053/RN-012 — valida el estado YA PERSISTIDO de las
    respuestas del ticket. No modifica nada. Lanza `ValidationError` con un
    diccionario `{campo_id: [mensajes]}` si algún campo visible+requerido
    no tiene valor, o si un valor persistido ya no es válido según su
    `EstrategiaCampo` (reevaluación defensiva — LISTA/MULTILISTA con
    opciones del propio campo, USUARIO/AREA/UNIDAD con referencia
    existente/activa, etc., todo reutilizado sin duplicar).
    """
    respuesta_formulario = ticket.respuesta_formulario
    version = respuesta_formulario.formulario_version
    campos = {c.id: c for c in version.campos.prefetch_related("opciones").all()}
    existentes = {
        rc.campo_id: rc
        for rc in respuesta_formulario.respuestas_campo.select_related("campo", "archivo").all()
    }
    valores = {campo_id: rc.valor for campo_id, rc in existentes.items()}
    reglas_agrupadas = _reglas_por_objetivo_y_efecto(version)

    errores = {}
    for campo_id, campo in campos.items():
        estado = estado_efectivo(campo, valores, reglas_agrupadas.get(campo_id, {}))
        if not estado.visible:
            continue  # oculto: nunca requerido, con o sin valor residual

        existente = existentes.get(campo_id)
        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            tiene_valor = existente is not None and getattr(existente, "archivo", None) is not None
        else:
            tiene_valor = existente is not None

        if not tiene_valor:
            if estado.requerido:
                errores[campo_id] = ["Este campo es obligatorio."]
            continue

        if campo.tipo == Campo.TipoCampo.ARCHIVO:
            continue  # ya validado (extensión/tamaño) al guardarse — nada más que revalidar

        estrategia = ESTRATEGIAS_POR_TIPO[campo.tipo]
        try:
            estrategia.validar_valor(campo, existente.valor)
        except ValidationError as exc:
            errores[campo_id] = exc.messages

    if errores:
        raise ValidationError(errores)
