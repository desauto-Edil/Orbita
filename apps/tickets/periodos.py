"""Cálculo de periodos de los Procesos programados (Sprint 4.G1).

Módulo PURO: solo fechas. No importa Django ORM, Celery ni modelos, y no lee el reloj: quien
lo llama decide cuál es «hoy» (la fecha LOCAL de la organización, `timezone.localdate()`). Así
la reconciliación, las pruebas y el Studio comparten una única regla y nada se calcula con
cadenas ni con desfases UTC escritos a mano.

Vocabulario:

- **Fecha de creación**: el día del mes (1 a 28) en que Órbita debe generar la ejecución.
- **Periodo**: el lapso que cubre la ejecución. En V1 es un MES calendario: el mismo mes de la
  fecha de creación (`MES_ACTUAL`) o el siguiente (`MES_SIGUIENTE`).
- **Etiqueta**: nombre estable de la ejecución («Noviembre 2026»). Identifica la EJECUCIÓN; no
  reemplaza el nombre del Proceso.

El día de creación se limita a 1..28 para que exista en todos los meses (sin reglas especiales
para el 29, 30 y 31).
"""

import calendar
from collections import namedtuple
from datetime import date

MENSUAL = "MENSUAL"
MES_ACTUAL = "MES_ACTUAL"
MES_SIGUIENTE = "MES_SIGUIENTE"

FRECUENCIAS = (MENSUAL,)
PERIODOS = (MES_ACTUAL, MES_SIGUIENTE)

DIA_MINIMO = 1
DIA_MAXIMO = 28

MESES = (
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)

Periodo = namedtuple("Periodo", ["inicio", "fin", "etiqueta"])


def validar_dia(dia):
    """`dia` debe ser un entero de 1 a 28 (no se acepta `bool`)."""
    if isinstance(dia, bool) or not isinstance(dia, int) or not DIA_MINIMO <= dia <= DIA_MAXIMO:
        raise ValueError(f"El día de creación debe ser un entero de {DIA_MINIMO} a {DIA_MAXIMO}.")
    return dia


def _sumar_meses(anio, mes, cantidad):
    indice = anio * 12 + (mes - 1) + cantidad
    return indice // 12, indice % 12 + 1


def periodo_mensual(anio, mes):
    """El mes calendario completo: del día 1 al último día (febrero y bisiestos incluidos)."""
    ultimo = calendar.monthrange(anio, mes)[1]
    return Periodo(date(anio, mes, 1), date(anio, mes, ultimo), f"{MESES[mes - 1]} {anio}")


def periodo_de_creacion(fecha_creacion, periodo):
    """El `Periodo` que cubre la ejecución creada en `fecha_creacion`: el mes de esa fecha
    (`MES_ACTUAL`) o el siguiente (`MES_SIGUIENTE`, con cambio de año en diciembre)."""
    if periodo not in PERIODOS:
        raise ValueError("Periodo desconocido.")
    desfase = 1 if periodo == MES_SIGUIENTE else 0
    anio, mes = _sumar_meses(fecha_creacion.year, fecha_creacion.month, desfase)
    return periodo_mensual(anio, mes)


def creacion_vencida_mas_reciente(hoy, dia):
    """La fecha de creación más reciente que ya llegó (<= `hoy`): el `dia` del mes de `hoy`
    si ya pasó (o es hoy) y el del mes anterior si todavía no.

    Es la base de la reconciliación: no pregunta «¿hoy es el día 25?» sino «¿qué ejecución ya
    debería existir?», así que un Beat apagado el día 25 se recupera el 26."""
    dia = validar_dia(dia)
    if hoy.day >= dia:
        return date(hoy.year, hoy.month, dia)
    anio, mes = _sumar_meses(hoy.year, hoy.month, -1)
    return date(anio, mes, dia)


def creacion_siguiente(hoy, dia):
    """La primera fecha de creación POSTERIOR a `hoy` (el `dia` de este mes si todavía no llega, y
    si no, el del mes siguiente)."""
    dia = validar_dia(dia)
    if hoy.day < dia:
        return date(hoy.year, hoy.month, dia)
    anio, mes = _sumar_meses(hoy.year, hoy.month, 1)
    return date(anio, mes, dia)


def creaciones_entre(desde, hasta, dia):
    """Todas las fechas de creación (en orden) con `desde <= fecha <= hasta`. Sirve para
    DETECTAR periodos que no se generaron; nunca para generarlos en bloque."""
    dia = validar_dia(dia)
    fechas = []
    anio, mes = desde.year, desde.month
    while (anio, mes) <= (hasta.year, hasta.month):
        fecha = date(anio, mes, dia)
        if desde <= fecha <= hasta:
            fechas.append(fecha)
        anio, mes = _sumar_meses(anio, mes, 1)
    return fechas
