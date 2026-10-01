"""Editor funcional de Workflow — 3.UI.4 (CU-020, RQF-063/064/069).

Dueño del *contenido* de una `WorkflowVersion` (Etapas, Transiciones y sus
configuraciones especializadas) — paralelo a `versionamiento.py`, que es
dueño del contenedor `Workflow`/la propia `WorkflowVersion`. 10 funciones
(4 Etapa + 3 configuración especializada + 3 Transición), un solo archivo,
mismo criterio de "funciones simples" ya usado en todo `apps.workflows`:
ninguna verifica autorización internamente (responsabilidad exclusiva de
la vista, igual que `crear_nueva_version`/`activar_version`/
`crear_workflow`), toda operación compuesta es `@transaction.atomic`, y
cada una registra exactamente un `RegistroAuditoria` — nunca uno por cada
fila secundaria que toque (ej. configurar APROBACION con 5 participantes
audita 1 evento, no 6).

**Resuelve el GAP #1/#2 diagnosticado en 3.UI.4 (decisión aprobada R1,
sin tocar `models.py`)**: a diferencia de `TransicionEtapa.save()` (que sí
invoca `validar_integridad_transicion()` explícitamente), `Etapa.save()` y
las configuraciones especializadas NO invocan `self.clean()` — un
`Etapa.objects.create(...)` directo no dispara la Strategy ni los
`CheckConstraint`. Cada función de este módulo compensa llamando
`full_clean()` explícitamente antes de guardar (dispara `Etapa.clean()` →
`estrategia.validar_configuracion()`, y desde Django 4.1 también
`validate_constraints()` sobre los `CheckConstraint` de coherencia
tipo↔FK) — la Strategy y los constraints siguen siendo la única fuente de
verdad, este módulo no reimplementa sus reglas, solo se asegura de
invocarlas.

**Inmutabilidad (RN-020)**: `WorkflowVersion.exigir_editable()` ya está
en `save()`/`delete()` de `Etapa`/`TransicionEtapa`/las 3 configuraciones
— es la barrera real, no una conveniencia de UX. Cada función de aquí
además la comprueba explícitamente al inicio (falla rápido con mensaje
claro antes de construir nada), pero aunque no lo hiciera, el modelo
igual la haría cumplir — un POST directo contra una versión no BORRADOR
queda rechazado sin código nuevo."""

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria
from apps.workflows.models import (
    ConfiguracionEtapaAprobacion,
    ConfiguracionEtapaTarea,
    Etapa,
    ParticipanteEtapaAprobacion,
    TransicionEtapa,
)

# 3.UI.4, decisión R2: TICKET/GACETA se muestran en el editor (para que el
# usuario sepa que existen) pero no se pueden crear ni asignar desde aquí
# — ninguno tiene Strategy ejecutable todavía (TICKET: `ejecutable=False`;
# GACETA: ni siquiera tiene entrada en `ESTRATEGIAS_POR_TIPO`). Se
# comprueba aquí, en el editor, nunca cambiando `ESTRATEGIAS_POR_TIPO`
# (esa lista sigue siendo del motor/validación, no del editor).
TIPOS_NO_DISPONIBLES_EN_EDITOR = frozenset({Etapa.Tipo.TICKET, Etapa.Tipo.GACETA})


def _tipo_no_disponible(tipo):
    if tipo in TIPOS_NO_DISPONIBLES_EN_EDITOR:
        raise ValidationError(f"El tipo {tipo} todavía no está disponible desde este editor.")


def _full_clean_etapa_metadatos(etapa):
    """`Etapa.clean()` SIEMPRE valida `configuracion` contra la Strategy
    del tipo (vía `full_clean()`) — correcto cuando ya hay una
    configuración real, pero `crear_etapa`/`cambiar_tipo_etapa` dejan
    `configuracion={}` a propósito para TAREA/APROBACION/ESPERA hasta que
    se llame la función `configurar_etapa_*` correspondiente (creación y
    configuración son dos pasos deliberadamente separados). Para
    TAREA/APROBACION eso no causa problema porque su `claves_configuracion`
    está vacía — `{}` siempre es válido para ellas. Pero `EstrategiaEspera`
    SÍ exige `modo` incluso en un dict vacío, así que un `full_clean()`
    incondicional rechazaría la propia creación de una etapa ESPERA antes
    de que exista la oportunidad de configurarla.

    Por eso: si `configuracion` sigue vacía (etapa aún no configurada, sin
    importar el tipo), se valida solo con `clean_fields()` — nombre/tipo/
    descripción, sin tocar la Strategy. Si ya tiene contenido (etapa ya
    configurada, o metadatos editados después), se usa `full_clean()`
    completo — el JSON existente sigue validándose contra la Strategy
    normalmente, nunca se deja pasar un dato corrupto sobre una etapa que
    sí tiene configuración real."""
    if etapa.configuracion:
        etapa.full_clean()
    else:
        etapa.clean_fields()


# --- Etapa (4) --------------------------------------------------------


@transaction.atomic
def crear_etapa(version, actor, *, tipo, nombre, descripcion=""):
    version.exigir_editable()
    _tipo_no_disponible(tipo)
    etapa = Etapa(version=version, tipo=tipo, nombre=nombre, descripcion=descripcion, configuracion={})
    _full_clean_etapa_metadatos(etapa)
    etapa.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(etapa),
    )
    return etapa


@transaction.atomic
def editar_etapa(etapa, actor, *, nombre=None, descripcion=None):
    """Solo metadatos comunes (`nombre`/`descripcion`) — nunca `tipo`
    (ver `cambiar_tipo_etapa`, operación separada y deliberadamente más
    cautelosa) ni `configuracion` (ver las 3 funciones de configuración)."""
    etapa.version.exigir_editable()
    anterior = serializar(etapa)
    campos = []
    if nombre is not None:
        etapa.nombre = nombre
        campos.append("nombre")
    if descripcion is not None:
        etapa.descripcion = descripcion
        campos.append("descripcion")
    if not campos:
        return etapa
    _full_clean_etapa_metadatos(etapa)
    etapa.save(update_fields=campos + ["actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(etapa),
    )
    return etapa


@transaction.atomic
def cambiar_tipo_etapa(etapa, actor, *, nuevo_tipo):
    """Decisión R6: bloquea si la etapa tiene transiciones SALIENTES —
    cambiar de tipo puede volver esas transiciones semánticamente
    incompatibles con las reglas del nuevo tipo (ej. CONDICION→HITO deja
    condicionales que HITO no admite; APROBACION→TAREA deja transiciones
    con `resultado_aprobacion` que TAREA no admite). Las transiciones
    ENTRANTES no bloquean: sus reglas dependen del tipo de su propia
    `etapa_origen`, no de esta etapa como destino.

    Limpieza consciente de configuración especializada (nunca queda
    huérfana ni se reinterpreta bajo el nuevo tipo): borra
    `configuracion_tarea`/`configuracion_aprobacion` si existían y
    resetea `Etapa.configuracion` a `{}` — todo en la misma transacción
    que el cambio de tipo."""
    etapa.version.exigir_editable()
    if nuevo_tipo == etapa.tipo:
        return etapa
    _tipo_no_disponible(nuevo_tipo)
    if etapa.transiciones_salientes.exists():
        raise ValidationError(
            "Elimine primero las transiciones salientes antes de cambiar el tipo de la etapa."
        )
    anterior = serializar(etapa)

    configuracion_tarea = getattr(etapa, "configuracion_tarea", None)
    if configuracion_tarea is not None:
        configuracion_tarea.delete()
    configuracion_aprobacion = getattr(etapa, "configuracion_aprobacion", None)
    if configuracion_aprobacion is not None:
        configuracion_aprobacion.delete()

    etapa.configuracion = {}
    etapa.tipo = nuevo_tipo
    _full_clean_etapa_metadatos(etapa)
    etapa.save(update_fields=["tipo", "configuracion", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(etapa),
    )
    return etapa


@transaction.atomic
def eliminar_etapa(etapa, actor):
    """Decisión R3: bloquea si tiene CUALQUIER transición (entrante o
    saliente) — nunca se depende del `CASCADE` de la FK como si fuera la
    regla funcional; el usuario elimina las conexiones primero, a
    sabiendas. La auditoría se registra ANTES de `delete()` (Django limpia
    el `pk` de la instancia tras borrar — `registrar_evento` lo necesita)."""
    etapa.version.exigir_editable()
    if etapa.transiciones_salientes.exists() or etapa.transiciones_entrantes.exists():
        raise ValidationError("Elimine primero las transiciones conectadas a esta etapa antes de eliminarla.")
    anterior = serializar(etapa)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )
    etapa.delete()


# --- Configuración especializada (3) -----------------------------------


@transaction.atomic
def configurar_etapa_tarea(
    etapa, actor, *, tipo_responsable="", usuario_responsable=None, equipo_responsable=None, permite_subtareas=False
):
    etapa.version.exigir_editable()
    if etapa.tipo != Etapa.Tipo.TAREA:
        raise ValidationError("Solo una etapa TAREA admite esta configuración.")
    if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.USUARIO and usuario_responsable is None:
        raise ValidationError("Seleccione un usuario responsable.")
    if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.EQUIPO and equipo_responsable is None:
        raise ValidationError("Seleccione un equipo responsable.")

    configuracion_previa = getattr(etapa, "configuracion_tarea", None)
    anterior = serializar(configuracion_previa) if configuracion_previa is not None else None
    configuracion = configuracion_previa or ConfiguracionEtapaTarea(etapa=etapa)
    configuracion.tipo_responsable = tipo_responsable
    configuracion.usuario_responsable = (
        usuario_responsable if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.USUARIO else None
    )
    configuracion.equipo_responsable = (
        equipo_responsable if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.EQUIPO else None
    )
    configuracion.permite_subtareas = permite_subtareas
    configuracion.full_clean()
    configuracion.save()

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(configuracion),
    )
    return configuracion


@transaction.atomic
def configurar_etapa_aprobacion(etapa, actor, *, modo, politica=None, participantes):
    """Decisión R4: reemplazo completo y atómico. `participantes`: lista
    ordenada de tuplas `(tipo_aprobador, usuario_o_equipo)` — mismo
    formato que `apps.aprobaciones.operaciones.crear_esquema_aprobacion`;
    `orden` se deriva de la posición (1, 2, 3...), garantizando por
    construcción que nunca hay órdenes duplicados en el resultado.

    Se borra la configuración anterior (participantes incluidos, vía
    `CASCADE` de `ParticipanteEtapaAprobacion.configuracion`) y se crea
    de nuevo — pero al vivir dentro de una única `@transaction.atomic`,
    si CUALQUIER `full_clean()` posterior falla (configuración o
    cualquier participante), Django revierte también ese borrado: la
    configuración previa nunca queda destruida de verdad ante un dato
    inválido. No se preservan los `pk` de los participantes anteriores —
    decisión aprobada: no hace falta, la consistencia y la atomicidad son
    lo que importa, no la identidad de esas filas."""
    etapa.version.exigir_editable()
    if etapa.tipo != Etapa.Tipo.APROBACION:
        raise ValidationError("Solo una etapa APROBACION admite esta configuración.")
    if not participantes:
        raise ValidationError("Debe indicar al menos un participante.")
    if modo == ConfiguracionEtapaAprobacion.Modo.PARALELA and not politica:
        raise ValidationError("modo=PARALELA requiere una política (TODOS/CUALQUIERA).")
    if modo == ConfiguracionEtapaAprobacion.Modo.SECUENCIAL and politica:
        raise ValidationError("modo=SECUENCIAL no admite política de cierre propia.")

    configuracion_previa = getattr(etapa, "configuracion_aprobacion", None)
    anterior = None
    if configuracion_previa is not None:
        anterior = {
            "configuracion": serializar(configuracion_previa),
            "participantes": [serializar(p) for p in configuracion_previa.participantes.order_by("orden")],
        }
        configuracion_previa.delete()

    configuracion = ConfiguracionEtapaAprobacion(etapa=etapa, modo=modo, politica=politica or "")
    configuracion.full_clean()
    configuracion.save()

    nuevos = []
    for orden, (tipo_aprobador, aprobador) in enumerate(participantes, start=1):
        if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.USUARIO:
            participante = ParticipanteEtapaAprobacion(
                configuracion=configuracion, orden=orden, tipo_aprobador=tipo_aprobador, usuario=aprobador
            )
        elif tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.EQUIPO:
            participante = ParticipanteEtapaAprobacion(
                configuracion=configuracion, orden=orden, tipo_aprobador=tipo_aprobador, equipo=aprobador
            )
        elif tipo_aprobador in ("RESPONSABLE_TICKET", "SOLICITANTE") and aprobador is None:
            participante = ParticipanteEtapaAprobacion(
                configuracion=configuracion, orden=orden, tipo_aprobador=tipo_aprobador
            )
        else:
            raise ValidationError(f"tipo_aprobador inválido o referencia incompatible: {tipo_aprobador}.")
        participante.full_clean()
        participante.save()
        nuevos.append(participante)

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos={
            "configuracion": serializar(configuracion),
            "participantes": [serializar(p) for p in nuevos],
        },
    )
    return configuracion


@transaction.atomic
def configurar_etapa_espera(etapa, actor, *, modo, duracion_valor=None, duracion_unidad=None, fecha_objetivo=None):
    """Construye un dict NUEVO y mínimo para el `modo` elegido — nunca
    conserva claves residuales del modo anterior (DURACION→FECHA no deja
    `duracion_valor`/`duracion_unidad`; FECHA→DURACION no deja
    `fecha_objetivo`) — y lo pasa tal cual por `Etapa.clean()` →
    `EstrategiaEspera.validar_configuracion()` (la Strategy real, nunca
    reimplementada aquí)."""
    etapa.version.exigir_editable()
    if etapa.tipo != Etapa.Tipo.ESPERA:
        raise ValidationError("Solo una etapa ESPERA admite esta configuración.")

    if modo == "DURACION":
        nueva_configuracion = {"modo": "DURACION", "duracion_valor": duracion_valor, "duracion_unidad": duracion_unidad}
    elif modo == "FECHA":
        nueva_configuracion = {"modo": "FECHA", "fecha_objetivo": fecha_objetivo}
    else:
        raise ValidationError(f"modo debe ser DURACION o FECHA (recibido: {modo!r}).")

    anterior = {"configuracion": dict(etapa.configuracion or {})}
    etapa.configuracion = nueva_configuracion
    etapa.full_clean()
    etapa.save(update_fields=["configuracion", "actualizado_en"])

    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=etapa,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos={"configuracion": nueva_configuracion},
    )
    return etapa


# --- Transición (3) -----------------------------------------------------


def _normalizar_campos_transicion(
    etapa_origen, *, nombre, prioridad, variable, operador, valor, es_fallback, resultado_aprobacion
):
    """Normaliza los campos condicionales/de resultado según el tipo de
    `etapa_origen` — nunca decide si la combinación resultante es válida
    (eso sigue siendo, exclusivamente, `validar_integridad_transicion`,
    invocada por `TransicionEtapa.clean()`/`save()`). Solo evita que datos
    ajenos al tipo de origen lleguen a persistirse por un descuido del
    Form/la vista (ej. `resultado_aprobacion` en una transición que sale
    de un HITO)."""
    if etapa_origen.tipo == Etapa.Tipo.CONDICION:
        resultado_aprobacion = ""
    elif etapa_origen.tipo == Etapa.Tipo.APROBACION:
        variable, operador, valor, es_fallback = "", "", "", False
    else:
        variable, operador, valor, es_fallback, resultado_aprobacion = "", "", "", False, ""
    return {
        "nombre": nombre,
        "prioridad": prioridad,
        "variable": variable,
        "operador": operador,
        "valor": valor,
        "es_fallback": es_fallback,
        "resultado_aprobacion": resultado_aprobacion,
    }


@transaction.atomic
def crear_transicion(
    etapa_origen,
    etapa_destino,
    actor,
    *,
    nombre="",
    prioridad=0,
    variable="",
    operador="",
    valor="",
    es_fallback=False,
    resultado_aprobacion="",
):
    etapa_origen.version.exigir_editable()
    campos = _normalizar_campos_transicion(
        etapa_origen,
        nombre=nombre,
        prioridad=prioridad,
        variable=variable,
        operador=operador,
        valor=valor,
        es_fallback=es_fallback,
        resultado_aprobacion=resultado_aprobacion,
    )
    transicion = TransicionEtapa(etapa_origen=etapa_origen, etapa_destino=etapa_destino, **campos)
    transicion.full_clean()
    transicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(transicion),
    )
    return transicion


@transaction.atomic
def editar_transicion(
    transicion,
    actor,
    *,
    etapa_destino,
    nombre="",
    prioridad=0,
    variable="",
    operador="",
    valor="",
    es_fallback=False,
    resultado_aprobacion="",
):
    """Reemplazo completo de los campos mutables (no una edición parcial
    campo a campo): una `TransicionEtapa` se define por su combinación
    completa de campos condicionales/de resultado, editarla a medias
    dejaría estados intermedios sin sentido. Misma normalización que
    `crear_transicion`."""
    transicion.etapa_origen.version.exigir_editable()
    anterior = serializar(transicion)
    campos = _normalizar_campos_transicion(
        transicion.etapa_origen,
        nombre=nombre,
        prioridad=prioridad,
        variable=variable,
        operador=operador,
        valor=valor,
        es_fallback=es_fallback,
        resultado_aprobacion=resultado_aprobacion,
    )
    transicion.etapa_destino = etapa_destino
    for campo, valor_campo in campos.items():
        setattr(transicion, campo, valor_campo)
    transicion.full_clean()
    transicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(transicion),
    )
    return transicion


@transaction.atomic
def eliminar_transicion(transicion, actor):
    transicion.etapa_origen.version.exigir_editable()
    anterior = serializar(transicion)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )
    transicion.delete()
