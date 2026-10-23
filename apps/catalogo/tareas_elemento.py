"""Tareas por elemento — Sprint 4.G2D1 (configuración).

Un Proceso con estructura de elementos (`DefinicionParrilla`) puede, de forma OPCIONAL, indicar cómo cada
elemento se convierte en una `apps.tareas.Tarea`: qué columna da el título, la descripción (opcional), el
responsable y la fecha límite, y si la Tarea pide evidencia. Aquí vive solo la configuración MUTABLE del
catálogo (`ConfiguracionTareasElemento`); las columnas se guardan por la `clave` ESTABLE del `Campo`.

Cada Ticket resuelve y COPIA esto al nacer (`apps.tickets.tareas_elemento`): ninguna Tarea ni Ticket
existente depende de lo que aquí se cambie después. La generación de Tareas NO ocurre aquí.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import Campo, ConfiguracionTareasElemento, DefinicionParrilla, Servicio
from apps.catalogo.parrilla import definicion_activa_de, definicion_de
from apps.catalogo.revision_elementos import version_de_columnas
from apps.core.auditoria import registrar_evento, serializar
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria

T = Campo.TipoCampo

# Qué tipo de columna puede dar cada dato de la Tarea.
TIPOS_TITULO = (T.TEXTO,)
TIPOS_DESCRIPCION = (T.TEXTO, T.TEXTO_LARGO)
TIPOS_RESPONSABLE = (T.USUARIO,)
TIPOS_FECHA_LIMITE = (T.FECHA, T.FECHA_HORA)

# (atributo de la configuración, nombre para el usuario, tipos admitidos, ¿obligatoria?)
DESTINOS = (
    ("clave_titulo", "título", TIPOS_TITULO, True),
    ("clave_descripcion", "descripción", TIPOS_DESCRIPCION, False),
    ("clave_responsable", "responsable", TIPOS_RESPONSABLE, True),
    ("clave_fecha_limite", "fecha límite", TIPOS_FECHA_LIMITE, True),
)


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


def configuracion_de(servicio):
    """La `ConfiguracionTareasElemento` del Proceso (activa o no), o `None`."""
    definicion = definicion_de(servicio)
    if definicion is None:
        return None
    return ConfiguracionTareasElemento.objects.filter(definicion=definicion).first()


def configuracion_activa_de(servicio):
    configuracion = configuracion_de(servicio)
    return configuracion if configuracion is not None and configuracion.activa else None


def columnas_por_destino(servicio):
    """`{atributo: [Campo]}` con las columnas de la versión vigente que pueden ocupar cada destino."""
    version = version_de_columnas(servicio)
    campos = list(version.campos.order_by("orden", "id")) if version is not None else []
    return {atributo: [c for c in campos if c.tipo in tipos] for atributo, _, tipos, _ in DESTINOS}


def _claves(**elegidas):
    return {atributo: (elegidas.get(atributo) or "").strip() for atributo, *_ in DESTINOS}


def errores_de_claves(campos_por_clave, claves, *, contexto=""):
    """Reglas de compatibilidad entre cada clave elegida y su destino, contra `campos_por_clave`
    (`{clave: Campo}` de UNA versión de columnas). Lista de mensajes (vacía = bien)."""
    errores = []
    usadas = {}
    for atributo, nombre, tipos, obligatoria in DESTINOS:
        clave = claves.get(atributo, "")
        if not clave:
            if obligatoria:
                errores.append(f"Elige la columna que da el {nombre} de la tarea.")
            continue
        campo = campos_por_clave.get(clave)
        if campo is None:
            errores.append(f"La columna «{clave}» ({nombre}) no existe en la estructura de elementos{contexto}.")
            continue
        if campo.tipo not in tipos:
            admitidos = ", ".join(str(T(t).label).lower() for t in tipos)
            errores.append(
                f"La columna «{campo.etiqueta}» no sirve como {nombre} de la tarea: debe ser de tipo {admitidos}."
            )
            continue
        if clave in usadas:
            errores.append(
                f"La columna «{campo.etiqueta}» no puede dar a la vez el {usadas[clave]} y el {nombre} de la tarea."
            )
        usadas.setdefault(clave, nombre)
    return errores


def errores_de_editables(campos_por_clave, claves, editables, *, contexto=""):
    """Bloque 3.1 — las columnas que el responsable de la tarea puede editar en su elemento existen en la
    versión y no son las que originan la Tarea (esas no cambian mientras la Tarea está abierta)."""
    errores = []
    origen = {clave for clave in claves.values() if clave}
    for clave in editables or []:
        campo = campos_por_clave.get(clave)
        if campo is None:
            errores.append(f"La columna editable «{clave}» no existe en la estructura de elementos{contexto}.")
        elif clave in origen:
            errores.append(
                f"La columna «{campo.etiqueta}» origina la tarea: no puede editarla quien la realiza."
            )
    return errores


def _editables_limpias(editables):
    from apps.catalogo.actividades import columnas_editables

    errores = []
    limpias = columnas_editables(list(editables or []), errores)
    if errores:
        raise ValidationError(errores)
    return limpias


def validar_configuracion(servicio, columnas_editables=(), **claves):
    """Reglas de la configuración. Lanza `ValidationError`; devuelve las claves limpias."""
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Solo un Proceso puede generar tareas a partir de sus elementos.")
    if definicion_activa_de(servicio) is None:
        raise ValidationError(
            "Este proceso no usa elementos estructurados: actívalos en la pestaña Formulario "
            "(«Elementos del proceso») antes de configurar sus tareas."
        )
    version = version_de_columnas(servicio)
    if version is None:
        raise ValidationError("La estructura de elementos todavía no tiene columnas.")
    limpias = _claves(**claves)
    campos = {c.clave: c for c in version.campos.all()}
    errores = errores_de_claves(campos, limpias)
    errores += errores_de_editables(campos, limpias, _editables_limpias(columnas_editables))
    if errores:
        raise ValidationError(errores)
    return limpias


@transaction.atomic
def configurar_tareas_elemento(
    servicio, actor, *, clave_titulo, clave_responsable, clave_fecha_limite, clave_descripcion="",
    requiere_evidencia=False, columnas_editables=(),
):
    """Crea o actualiza la configuración (y la deja ACTIVA). Solo afecta a los Tickets que nazcan
    después: cada Ticket conserva la copia que congeló. `columnas_editables` (Bloque 3.1): claves de las
    columnas que el responsable de la Tarea abierta puede editar en su elemento."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    limpias = validar_configuracion(
        servicio, clave_titulo=clave_titulo, clave_descripcion=clave_descripcion,
        clave_responsable=clave_responsable, clave_fecha_limite=clave_fecha_limite,
        columnas_editables=columnas_editables,
    )
    editables = _editables_limpias(columnas_editables)
    definicion = DefinicionParrilla.objects.select_for_update().get(servicio=servicio)
    configuracion = ConfiguracionTareasElemento.objects.select_for_update().filter(definicion=definicion).first()
    if configuracion is None:
        configuracion = ConfiguracionTareasElemento.objects.create(
            definicion=definicion, activa=True, requiere_evidencia=bool(requiere_evidencia),
            columnas_editables=editables, **limpias
        )
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=configuracion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=None, datos_nuevos=serializar(configuracion),
        )
        return configuracion
    anterior = serializar(configuracion)
    for atributo, valor in limpias.items():
        setattr(configuracion, atributo, valor)
    configuracion.requiere_evidencia = bool(requiere_evidencia)
    configuracion.columnas_editables = editables
    configuracion.activa = True
    configuracion.save()
    nuevo = serializar(configuracion)
    if anterior != nuevo:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=configuracion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=anterior, datos_nuevos=nuevo,
        )
    return configuracion


@transaction.atomic
def desactivar_tareas_elemento(servicio, actor):
    """Los Tickets NUEVOS dejan de poder generar tareas por elemento. La configuración se conserva
    (reactivarla es volver a configurarla); los Tickets ya creados y sus Tareas no cambian."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    definicion = DefinicionParrilla.objects.filter(servicio=servicio).first()
    configuracion = (
        ConfiguracionTareasElemento.objects.select_for_update().filter(definicion=definicion).first()
        if definicion is not None else None
    )
    if configuracion is None or not configuracion.activa:
        return configuracion
    usa = bloque_que_la_usa(servicio)
    if usa is not None:
        raise ValidationError(
            f"No se pueden desactivar las tareas por elemento: el bloque «{usa.nombre}» del flujo las usa. "
            "Quita ese bloque primero."
        )
    anterior = serializar(configuracion)
    configuracion.activa = False
    configuracion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=configuracion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=serializar(configuracion),
    )
    return configuracion


def bloque_que_la_usa(servicio):
    """El bloque TAREAS_ELEMENTOS de una configuración de ejecución vigente o en borrador del Proceso (o
    `None`). Mientras exista, ni las tareas por elemento ni la estructura de elementos se desactivan."""
    from apps.catalogo.models import BloqueOperativo, ConfiguracionEjecucionVersion

    return (
        BloqueOperativo.objects.filter(
            version__servicio=servicio, tipo=BloqueOperativo.Tipo.TAREAS_ELEMENTOS,
            version__estado__in=(ConfiguracionEjecucionVersion.Estado.ACTIVA, ConfiguracionEjecucionVersion.Estado.BORRADOR),
        ).first()
    )


def claves_de(configuracion):
    return {atributo: getattr(configuracion, atributo) for atributo, *_ in DESTINOS}


def errores_de_tareas_elemento(servicio):
    """Por qué la configuración de tareas por elemento no se podría usar HOY con la versión ACTIVA de la
    estructura (lista vacía = bien o no la usa). Solo lectura; la comprueba `validar_publicacion` para
    que un Ticket nunca falle al nacer."""
    configuracion = configuracion_activa_de(servicio)
    if configuracion is None:
        return []
    definicion = definicion_activa_de(servicio)
    if definicion is None:
        return []  # sin estructura de elementos activa la configuración no se usa (el Ticket no tiene parrilla)
    version = definicion.formulario.version_activa
    if version is None or version.estado != "ACTIVA":
        return []  # lo señala `errores_de_parrilla`
    campos = {c.clave: c for c in version.campos.all()}
    errores = errores_de_claves(campos, claves_de(configuracion), contexto=" activa")
    errores += errores_de_editables(campos, claves_de(configuracion), configuracion.columnas_editables, contexto=" activa")
    return [f"Tareas por elemento: {e}" for e in errores]


def errores_de_version_para_tareas(servicio, version):
    """Antes de ACTIVAR una versión de la estructura: ¿seguiría sirviendo cada columna de la
    configuración de tareas? (Quitarla o cambiarle el tipo dejaría Tickets nuevos sin poder nacer.)"""
    configuracion = configuracion_activa_de(servicio)
    if configuracion is None:
        return []
    campos = {c.clave: c for c in version.campos.all()}
    errores = errores_de_claves(campos, claves_de(configuracion))
    errores += errores_de_editables(campos, claves_de(configuracion), configuracion.columnas_editables)
    return [f"Tareas por elemento: {e}" for e in errores]
