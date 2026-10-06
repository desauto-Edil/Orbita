"""Compromiso temporal básico del Ticket — Sprint 4.A1.

Único lugar donde se calcula una fecha objetivo. Ninguna vista, formulario ni
plantilla suma días u horas: todos consultan este módulo. Es deliberadamente
independiente de los modelos (no importa `apps.catalogo` ni `Ticket`) para que
pueda sustituirse más adelante por un calendario configurable (festivos,
horarios) sin tocar el resto del dominio: basta con reemplazar
`calcular_fecha_objetivo`.

Regla de esta versión — hábil = lunes a viernes, sin festivos ni horario:

- **No hábil** (`habiles=False`): suma tiempo corrido (`HORAS`: horas reloj;
  `DIAS`: días calendario conservando la hora local).
- **`DIAS` hábiles**: cuenta días lunes-viernes a partir del día SIGUIENTE al
  inicio (el día de radicación no cuenta) y conserva la hora local. Un inicio
  en sábado o domingo cuenta desde el lunes: sábado + 1 día hábil = lunes.
- **`HORAS` hábiles**: horas reloj, pero el sábado y el domingo no corren. Un
  inicio en fin de semana arranca el reloj el lunes a las 00:00 locales.

La hora local es la zona horaria activa de Django. La aritmética hábil se hace
sobre la hora local sin ajuste de horario de verano (la plataforma opera en una
zona sin él): un día es siempre una vuelta de reloj de 24 h locales.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

UNIDAD_HORAS = "HORAS"
UNIDAD_DIAS = "DIAS"
UNIDADES = (UNIDAD_HORAS, UNIDAD_DIAS)

MAX_CANTIDAD = 999

_LUNES_A_VIERNES = 5  # `date.weekday()` < 5 → día hábil


def es_dia_habil(fecha):
    return fecha.weekday() < _LUNES_A_VIERNES


def _a_local_naive(momento):
    if timezone.is_naive(momento):
        raise ValueError("La fecha de inicio debe tener zona horaria.")
    return timezone.localtime(momento).replace(tzinfo=None)


def _a_aware(local_naive):
    return timezone.make_aware(local_naive)


def _inicio_de_dia(fecha):
    return datetime.combine(fecha, time.min)


def _sumar_dias_habiles(local, cantidad):
    fecha = local.date()
    restante = cantidad
    while restante > 0:
        fecha += timedelta(days=1)
        if es_dia_habil(fecha):
            restante -= 1
    return datetime.combine(fecha, local.time())


def _sumar_horas_habiles(local, cantidad):
    if not es_dia_habil(local.date()):
        proximo_lunes = local.date() + timedelta(days=7 - local.weekday())
        local = _inicio_de_dia(proximo_lunes)
    restante = timedelta(hours=cantidad)
    while True:
        sabado = local.date() + timedelta(days=_LUNES_A_VIERNES - local.weekday())
        limite = _inicio_de_dia(sabado)
        disponible = limite - local
        if restante <= disponible:
            return local + restante
        restante -= disponible
        local = _inicio_de_dia(sabado + timedelta(days=2))


def calcular_fecha_objetivo(desde, cantidad, unidad, habiles=False):
    """Fecha límite (aware) de un compromiso de `cantidad` `unidad` desde
    `desde` (aware). Lanza `ValueError` ante datos inválidos: quien llama ya
    validó la configuración, esto es defensa de dominio."""
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or not 1 <= cantidad <= MAX_CANTIDAD:
        raise ValueError(f"La cantidad debe ser un entero de 1 a {MAX_CANTIDAD}.")
    if unidad not in UNIDADES:
        raise ValueError(f"La unidad debe ser una de {UNIDADES}.")
    local = _a_local_naive(desde)

    if habiles:
        if unidad == UNIDAD_DIAS:
            return _a_aware(_sumar_dias_habiles(local, cantidad))
        return _a_aware(_sumar_horas_habiles(local, cantidad))

    if unidad == UNIDAD_DIAS:
        return _a_aware(local + timedelta(days=cantidad))
    return desde + timedelta(hours=cantidad)


def fecha_objetivo_de(compromiso, desde):
    """Fecha objetivo según el snapshot temporal de `compromiso` (un `Ticket`:
    `tiempo_objetivo_cantidad/unidad/habiles`), o `None` si no tiene compromiso
    temporal (Servicio sin tiempo objetivo o Ticket anterior a 4.A1)."""
    cantidad = getattr(compromiso, "tiempo_objetivo_cantidad", None)
    if cantidad is None:
        return None
    return calcular_fecha_objetivo(
        desde, cantidad, compromiso.tiempo_objetivo_unidad, bool(compromiso.tiempo_objetivo_habiles)
    )
