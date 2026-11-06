"""Studio › Notificaciones (CU-036 «eventos configurados», CU-033 parcial): la configuración de avisos de UN
Servicio/Proceso — avisos base, avisos por etapa y recordatorios — y su foto CONGELADA en cada Ticket.

- Pertenece al Servicio/Proceso, nunca al Workflow compartido (el mismo flujo puede notificar distinto en cada
  servicio, como el tiempo objetivo).
- Las reglas apuntan a fases, actividades y gacetas por su CLAVE estable: renombrar o publicar otra versión no las
  rompe; si la clave desaparece, la sección lo marca «Requiere atención» (`reglas_huerfanas`) sin bloquear publicar.
- Cada Ticket congela la foto al nacer (`Ticket.notificaciones`): cambiar el Studio afecta solo a tickets nuevos.
  Los valores GLOBALES (Configuración › Notificaciones) se leen en vivo para lo que el Servicio no cambió.
- Toda mutación se audita (`auditoria.auditar_guardado`)."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.auditoria import auditar_guardado, serializar
from apps.notificaciones import catalogo
from apps.notificaciones.models import (
    CAMPOS_PARAMETROS,
    ConfiguracionNotificaciones,
    ConfiguracionNotificacionesServicio,
    ReglaNotificacion,
)

Momento = ReglaNotificacion.Momento
Destinatario = ReglaNotificacion.Destinatario

MOMENTOS_DE_FASE = {Momento.FASE_ALCANZADA}
MOMENTOS_DE_ACTIVIDAD = {Momento.ACTIVIDAD_INICIADA, Momento.ACTIVIDAD_COMPLETADA, Momento.APROBACION_DEVUELTA}
MOMENTOS_DE_GACETA = {Momento.GACETA_ABIERTA, Momento.GACETA_RESUELTA}


def de_servicio(servicio):
    return ConfiguracionNotificacionesServicio.objects.filter(servicio=servicio).first()


def _obtener(servicio):
    configuracion = de_servicio(servicio)
    if configuracion is None:
        configuracion = ConfiguracionNotificacionesServicio(servicio=servicio)
    return configuracion


def _es_programado(servicio):
    from apps.catalogo.programacion import programacion_activa_de

    return servicio.tipo == "PROCESO" and programacion_activa_de(servicio) is not None


# --- Foto congelada en el Ticket ----------------------------------------------------------------------------


def foto(servicio):
    """La configuración que un Ticket congela al nacer, o `None` si el Servicio no cambió nada."""
    configuracion = de_servicio(servicio)
    reglas = list(ReglaNotificacion.objects.filter(servicio=servicio, activa=True).order_by("pk"))
    if configuracion is None and not reglas:
        return None
    datos = {"version": 1, "reglas": [_regla_a_dict(r) for r in reglas]}
    if configuracion is not None:
        datos["correo"] = dict(configuracion.correo_eventos or {})
        datos["parametros"] = {c: getattr(configuracion, c) for c in CAMPOS_PARAMETROS if getattr(configuracion, c) is not None}
        datos["escalar_a"] = {
            "tipo": configuracion.escalar_a,
            "usuario_id": configuracion.escalar_usuario_id,
            "equipo_id": configuracion.escalar_equipo_id,
        }
        datos["recordatorios_actividad"] = dict(configuracion.recordatorios_actividad or {})
    return datos


def _regla_a_dict(regla):
    return {
        "id": regla.pk, "momento": regla.momento, "clave_objetivo": regla.clave_objetivo,
        "nombre_objetivo": regla.nombre_objetivo, "destinatario": regla.destinatario,
        "usuario_id": regla.usuario_id, "equipo_id": regla.equipo_id, "campo_clave": regla.campo_clave,
        "correo": regla.correo, "mensaje": regla.mensaje,
    }


def congelar_en_ticket(ticket, servicio):
    """Llamado al crear el borrador (`tickets.operaciones._crear_borrador_de_servicio`)."""
    from apps.tickets.models import Ticket

    datos = foto(servicio)
    if datos is not None:
        ticket.notificaciones = datos
        Ticket.objects.filter(pk=ticket.pk).update(notificaciones=datos)


_CACHE_GLOBALES = {"valor": None, "hasta": 0.0}
SEGUNDOS_CACHE_GLOBALES = 30


def globales():
    """Valores generales (una consulta cada 30 s como mucho: las listas los piden por fila)."""
    import time

    ahora = time.monotonic()
    if _CACHE_GLOBALES["valor"] is None or ahora >= _CACHE_GLOBALES["hasta"]:
        fila = ConfiguracionNotificaciones.actual()
        _CACHE_GLOBALES.update(valor={c: getattr(fila, c) for c in CAMPOS_PARAMETROS}, hasta=ahora + SEGUNDOS_CACHE_GLOBALES)
    return dict(_CACHE_GLOBALES["valor"])


def olvidar_globales():
    _CACHE_GLOBALES["valor"] = None


def parametros_de(ticket=None):
    """Parámetros efectivos de alertas y recordatorios: los congelados en el Ticket sobre los globales vigentes."""
    efectivos = globales()
    if ticket is not None:
        efectivos.update(((ticket.notificaciones or {}).get("parametros")) or {})
    return efectivos


def escalamiento_de(ticket):
    return ((ticket.notificaciones or {}).get("escalar_a") if ticket is not None else None) or {"tipo": "SUPERVISION"}


def recordatorio_de_actividad(ticket, clave_bloque):
    if ticket is None or not clave_bloque:
        return {}
    return ((ticket.notificaciones or {}).get("recordatorios_actividad") or {}).get(clave_bloque) or {}


# --- Opciones del Studio ------------------------------------------------------------------------------------


def _configuracion_de_flujo(servicio):
    """La configuración del flujo que se está diseñando: el borrador si existe; si no, la activa."""
    from apps.catalogo.models import ConfiguracionEjecucionVersion

    return (
        ConfiguracionEjecucionVersion.objects.filter(servicio=servicio)
        .exclude(estado="HISTORICA").select_related("workflow_version").order_by("-numero").first()
    )


def objetivos(servicio):
    """`{"fases": [(clave, nombre)], "actividades": [(clave, nombre, fase)], "gacetas": [(clave, nombre)]}`."""
    from apps.catalogo.models import DefinicionGaceta

    configuracion = _configuracion_de_flujo(servicio)
    fases, actividades = [], []
    if configuracion is not None:
        for fase in configuracion.workflow_version.fases.order_by("orden", "pk"):
            if fase.clave:
                fases.append((fase.clave, fase.nombre))
        for bloque in configuracion.bloques.select_related("fase").order_by("fase__orden", "orden", "pk"):
            if bloque.clave and bloque.tipo not in ("DECISION",):
                actividades.append((bloque.clave, bloque.nombre, bloque.fase.nombre, bloque.tipo))
    gacetas = [
        (g.clave, g.nombre) for g in DefinicionGaceta.objects.filter(servicio=servicio, activa=True).order_by("orden", "pk")
    ]
    return {"fases": fases, "actividades": actividades, "gacetas": gacetas}


def campos_de_persona(servicio):
    """Campos USUARIO/EQUIPO del formulario de inicio y de las colecciones: `[(clave, etiqueta, origen)]`."""
    from apps.catalogo.models import Campo
    from apps.catalogo.parrilla import definiciones_de

    tipos = ("USUARIO", "EQUIPO")
    salida = []
    formulario = getattr(servicio, "formulario", None)
    version = getattr(formulario, "version_activa", None) if formulario is not None else None
    if version is not None:
        for campo in Campo.objects.filter(version=version, tipo__in=tipos).order_by("orden", "pk"):
            salida.append((campo.clave, campo.etiqueta, "formulario"))
    if servicio.tipo == "PROCESO":
        for definicion in definiciones_de(servicio):
            version = getattr(definicion.formulario, "version_activa", None)
            if version is None:
                continue
            for campo in Campo.objects.filter(version=version, tipo__in=tipos).order_by("orden", "pk"):
                salida.append((campo.clave, f"{campo.etiqueta} ({definicion.nombre_elemento or definicion.clave})", "item"))
    return salida


def reglas_de(servicio):
    return list(
        ReglaNotificacion.objects.filter(servicio=servicio, activa=True).select_related("usuario", "equipo").order_by("pk")
    )


def reglas_huerfanas(servicio, reglas=None):
    """Reglas cuya fase, actividad o gaceta ya no existe en la configuración que se diseña."""
    opciones = objetivos(servicio)
    fases = {c for c, _ in opciones["fases"]}
    actividades = {a[0] for a in opciones["actividades"]}
    gacetas = {c for c, _ in opciones["gacetas"]}
    huerfanas = []
    for regla in reglas if reglas is not None else reglas_de(servicio):
        if regla.momento in MOMENTOS_DE_FASE and regla.clave_objetivo not in fases:
            huerfanas.append(regla)
        elif regla.momento in MOMENTOS_DE_ACTIVIDAD and regla.clave_objetivo not in actividades:
            huerfanas.append(regla)
        elif regla.momento in MOMENTOS_DE_GACETA and regla.clave_objetivo not in gacetas:
            huerfanas.append(regla)
    return huerfanas


def avisos_base(servicio):
    """Avisos base con su estado de correo efectivo para el Studio."""
    configuracion = de_servicio(servicio)
    ajustes = (configuracion.correo_eventos if configuracion else None) or {}
    return [
        {
            "clave": a.clave, "etiqueta": a.etiqueta, "grupo": a.grupo, "requiere_accion": a.requiere_accion,
            "correo": catalogo.correo_por_defecto(a.clave, ajustes),
        }
        for a in catalogo.configurables(programado=_es_programado(servicio))
    ]


def estado(servicio):
    """`(clave_estado, texto)` para la navegación del Studio: Opcional · Completa · Requiere atención."""
    reglas = reglas_de(servicio)
    if reglas_huerfanas(servicio, reglas):
        return "attention", "Hay avisos de etapas que ya no existen"
    if reglas or de_servicio(servicio) is not None:
        partes = []
        if reglas:
            partes.append(f"{len(reglas)} aviso{'s' if len(reglas) != 1 else ''} por etapa")
        return "complete", " · ".join(partes) or "Personalizada"
    return "optional", "Avisos base"


# --- Mutaciones (Studio) -------------------------------------------------------------------------------------


@transaction.atomic
def guardar_avisos_base(servicio, actor, correo_por_aviso):
    """`correo_por_aviso`: `{clave: bool}`. Solo se guardan los cambios respecto del catálogo; lo que exige acción
    no se puede apagar (se ignora)."""
    configuracion = _obtener(servicio)
    anterior = serializar(configuracion) if configuracion.pk else None
    ajustes = {}
    for definicion in catalogo.configurables(programado=_es_programado(servicio)):
        if definicion.requiere_accion or definicion.clave not in correo_por_aviso:
            continue
        valor = bool(correo_por_aviso[definicion.clave])
        if valor != definicion.correo:
            ajustes[definicion.clave] = valor
    configuracion.correo_eventos = ajustes
    configuracion.save()
    auditar_guardado(configuracion, actor, anterior)
    return configuracion


@transaction.atomic
def guardar_recordatorios(servicio, actor, *, parametros, escalar_a, escalar_usuario=None, escalar_equipo=None):
    """`parametros`: `{campo: valor|None}` (None = usa el global)."""
    configuracion = _obtener(servicio)
    anterior = serializar(configuracion) if configuracion.pk else None
    for campo in CAMPOS_PARAMETROS:
        if campo in parametros:
            setattr(configuracion, campo, parametros[campo])
    configuracion.escalar_a = escalar_a or ConfiguracionNotificacionesServicio.EscalarA.SUPERVISION
    configuracion.escalar_usuario = escalar_usuario if configuracion.escalar_a == "USUARIO" else None
    configuracion.escalar_equipo = escalar_equipo if configuracion.escalar_a == "EQUIPO" else None
    configuracion.full_clean(exclude=["servicio"])
    configuracion.save()
    auditar_guardado(configuracion, actor, anterior)
    return configuracion


@transaction.atomic
def guardar_recordatorio_actividad(servicio, actor, clave_bloque, *, dias_antes=None, repetir_cada_dias=None, maximo=None):
    """Recordatorio propio de una actividad (sin valores = vuelve a los del Servicio)."""
    actividades = {a[0] for a in objetivos(servicio)["actividades"]}
    if clave_bloque not in actividades:
        raise ValidationError("Esa actividad no existe en el flujo de este servicio.")
    configuracion = _obtener(servicio)
    anterior = serializar(configuracion) if configuracion.pk else None
    propios = dict(configuracion.recordatorios_actividad or {})
    valores = {k: v for k, v in {"dias_antes": dias_antes, "repetir_cada_dias": repetir_cada_dias, "maximo": maximo}.items() if v is not None}
    if valores:
        propios[clave_bloque] = valores
    else:
        propios.pop(clave_bloque, None)
    configuracion.recordatorios_actividad = propios
    configuracion.save()
    auditar_guardado(configuracion, actor, anterior)
    return configuracion


@transaction.atomic
def agregar_regla(servicio, actor, *, momento, clave_objetivo, destinatario, usuario=None, equipo=None,
                  campo_clave="", correo=True, mensaje=""):
    if momento not in Momento.values:
        raise ValidationError("Elige cuándo se avisa.")
    if destinatario not in Destinatario.values:
        raise ValidationError("Elige a quién se avisa.")
    opciones = objetivos(servicio)
    if momento in MOMENTOS_DE_FASE:
        nombres = dict(opciones["fases"])
    elif momento in MOMENTOS_DE_ACTIVIDAD:
        nombres = {a[0]: a[1] for a in opciones["actividades"]}
    else:
        nombres = dict(opciones["gacetas"])
    if clave_objetivo not in nombres:
        raise ValidationError("Elige una fase, actividad o gaceta del flujo de este servicio.")
    if destinatario == Destinatario.SOLICITANTE and _es_programado(servicio):
        raise ValidationError("Un proceso programado no tiene solicitante.")
    if destinatario == Destinatario.RESPONSABLE_ACTIVIDAD and momento not in MOMENTOS_DE_ACTIVIDAD:
        raise ValidationError("«El responsable de la actividad» solo aplica a avisos de una actividad.")
    if destinatario == Destinatario.COORDINADOR_GACETA and momento not in MOMENTOS_DE_GACETA:
        raise ValidationError("«Quien coordina la gaceta» solo aplica a avisos de una gaceta.")
    if destinatario == Destinatario.USUARIO and usuario is None:
        raise ValidationError("Elige la persona a la que se avisa.")
    if destinatario == Destinatario.EQUIPO and equipo is None:
        raise ValidationError("Elige el equipo al que se avisa.")
    if destinatario in (Destinatario.CAMPO_FORMULARIO, Destinatario.COLUMNA_ITEM):
        origen = "formulario" if destinatario == Destinatario.CAMPO_FORMULARIO else "item"
        validos = {c for c, _, o in campos_de_persona(servicio) if o == origen}
        if campo_clave not in validos:
            raise ValidationError("Elige un campo de persona o equipo.")
    regla = ReglaNotificacion.objects.create(
        servicio=servicio, momento=momento, clave_objetivo=clave_objetivo, nombre_objetivo=nombres[clave_objetivo][:150],
        destinatario=destinatario,
        usuario=usuario if destinatario == Destinatario.USUARIO else None,
        equipo=equipo if destinatario == Destinatario.EQUIPO else None,
        campo_clave=campo_clave if destinatario in (Destinatario.CAMPO_FORMULARIO, Destinatario.COLUMNA_ITEM) else "",
        correo=bool(correo), mensaje=(mensaje or "").strip()[:280],
    )
    auditar_guardado(regla, actor)
    return regla


@transaction.atomic
def retirar_regla(regla, actor):
    """Retirar = desactivar (nunca se borra). Solo afecta a tickets nuevos."""
    anterior = serializar(regla)
    regla.activa = False
    regla.save(update_fields=["activa", "actualizado_en"])
    auditar_guardado(regla, actor, anterior)
    return regla
