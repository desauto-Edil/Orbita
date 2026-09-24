"""Specification Pattern para reglas condicionales (CU-012, RQF-043).

`EspecificacionRegla` resuelve únicamente "¿se cumple esta condición dadas
estas respuestas?". Reutiliza `EstrategiaCampo.normalizar()` (`campos.py`)
para interpretar valores según el tipo del campo origen (comparar fechas
como fechas, no como texto), sin duplicar esa lógica — pero no conoce nada
de configuración de campos: esa separación es deliberada (ver propuesta 1.2
aprobada, Strategy y Specification no se mezclan).

También vive aquí `validar_integridad_regla`, que `ReglaCondicional.clean()`
delega íntegramente — no se duplica en ningún otro punto de entrada.
"""

from __future__ import annotations

import operator as _op

from django.core.exceptions import ValidationError

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO


def _contiene(a, b):
    if a is None:
        return False
    return b in a


def _no_contiene(a, b):
    return not _contiene(a, b)


def _esta_vacio(a, _b):
    return a is None or a == "" or a == []


def _no_esta_vacio(a, b):
    return not _esta_vacio(a, b)


OPERADORES = {
    "IGUAL_A": _op.eq,
    "DISTINTO_DE": _op.ne,
    "CONTIENE": _contiene,
    "NO_CONTIENE": _no_contiene,
    "MAYOR_QUE": _op.gt,
    "MENOR_QUE": _op.lt,
    "ESTA_VACIO": _esta_vacio,
    "NO_ESTA_VACIO": _no_esta_vacio,
}

_TIPOS = set(ESTRATEGIAS_POR_TIPO)

# Cada operador se restringe a los tipos donde su semántica tiene sentido
# real (RQF-043) — ninguno se implementó "porque existía en la lista".
OPERADOR_TIPOS_COMPATIBLES = {
    "IGUAL_A": _TIPOS - {"MULTILISTA", "ARCHIVO"},
    "DISTINTO_DE": _TIPOS - {"MULTILISTA", "ARCHIVO"},
    "CONTIENE": {"TEXTO", "TEXTO_LARGO", "MULTILISTA"},
    "NO_CONTIENE": {"TEXTO", "TEXTO_LARGO", "MULTILISTA"},
    "MAYOR_QUE": {"NUMERO", "FECHA", "FECHA_HORA"},
    "MENOR_QUE": {"NUMERO", "FECHA", "FECHA_HORA"},
    "ESTA_VACIO": _TIPOS,
    "NO_ESTA_VACIO": _TIPOS,
}


class EspecificacionRegla:
    def __init__(self, regla):
        self.regla = regla

    def es_satisfecha_por(self, respuestas):
        """`respuestas` es un dict `{campo_id: valor_crudo}` — hoy solo lo
        ejercitan las pruebas con datos simulados (no hay `RespuestaFormulario`
        todavía); Sprint 2 lo alimentará con respuestas reales de un Ticket.
        """
        estrategia = ESTRATEGIAS_POR_TIPO[self.regla.campo_origen.tipo]
        valor_actual = estrategia.normalizar(respuestas.get(self.regla.campo_origen_id))
        if self.regla.operador in ("CONTIENE", "NO_CONTIENE"):
            # `regla.valor` es el elemento/subcadena buscado, no un valor
            # completo del campo — normalizarlo con la estrategia del campo
            # origen es incorrecto para MULTILISTA (`normalizar()` allí
            # produce una lista, y `list("URGENTE")` descompondría el string
            # en caracteres en vez de tratarlo como un único elemento).
            valor_regla = self.regla.valor
        else:
            valor_regla = estrategia.normalizar(self.regla.valor) if self.regla.valor != "" else None
        funcion = OPERADORES[self.regla.operador]
        try:
            return bool(funcion(valor_actual, valor_regla))
        except TypeError:
            # Comparación entre valores incompatibles en tiempo de evaluación
            # (p. ej. sin respuesta aún, con MAYOR_QUE) — no satisface la
            # condición, no es un error del sistema.
            return False


_EFECTO_OPUESTO = {
    "MOSTRAR": "OCULTAR",
    "OCULTAR": "MOSTRAR",
    "REQUERIR": "NO_REQUERIR",
    "NO_REQUERIR": "REQUERIR",
}


def validar_composicion_reglas(regla):
    """2.2 (RQF-043, decisión aprobada del usuario) — un mismo
    `campo_objetivo` no puede tener reglas de efectos opuestos dentro de la
    misma familia: `MOSTRAR`+`OCULTAR` (visibilidad) o `REQUERIR`+
    `NO_REQUERIR` (obligatoriedad) simultáneas se rechazan al guardar, en
    vez de resolverse con una precedencia en tiempo de evaluación. Varias
    reglas del MISMO efecto sobre el mismo objetivo sí son válidas (se
    combinan por OR — ver `apps/tickets/validaciones.py`). MOSTRAR/OCULTAR
    y REQUERIR/NO_REQUERIR son familias independientes: una regla MOSTRAR y
    una REQUERIR sobre el mismo objetivo nunca entran en conflicto entre
    sí.
    """
    if not regla.campo_objetivo_id or regla.efecto not in _EFECTO_OPUESTO:
        return
    opuesto = _EFECTO_OPUESTO[regla.efecto]
    existe_opuesta = (
        type(regla)
        .objects.filter(campo_objetivo_id=regla.campo_objetivo_id, efecto=opuesto)
        .exclude(pk=regla.pk)
        .exists()
    )
    if existe_opuesta:
        raise ValidationError(
            f"Ya existe una regla con efecto {opuesto} para este campo objetivo; no puede "
            f"combinarse con {regla.efecto} sobre el mismo campo."
        )


def validar_integridad_regla(regla):
    """Validaciones de integridad de `ReglaCondicional` (propuesta 1.2,
    sección E): compatibilidad operador/tipo, misma `FormularioVersion`, sin
    autorreferencia. No se valida ausencia de ciclos: la evaluación (Sprint
    2) es de una sola pasada sobre respuestas ya capturadas — cada regla lee
    la respuesta de `campo_origen`, nunca el resultado de otra regla — así
    que un ciclo A→B, B→A son dos reglas independientes sin riesgo de
    recursión infinita.
    """
    if regla.campo_origen_id and regla.campo_objetivo_id and regla.campo_origen_id == regla.campo_objetivo_id:
        raise ValidationError("Un campo no puede depender de sí mismo.")

    if (
        regla.campo_origen_id
        and regla.campo_objetivo_id
        and regla.campo_origen.version_id != regla.campo_objetivo.version_id
    ):
        raise ValidationError(
            "campo_origen y campo_objetivo deben pertenecer a la misma versión del formulario."
        )

    if regla.campo_origen_id:
        tipos_compatibles = OPERADOR_TIPOS_COMPATIBLES.get(regla.operador, set())
        if regla.campo_origen.tipo not in tipos_compatibles:
            raise ValidationError(
                f"El operador {regla.operador} no es compatible con el tipo de "
                f"campo_origen ({regla.campo_origen.tipo})."
            )
