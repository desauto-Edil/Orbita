"""Estructura de elementos de un Proceso — Sprint 4.G2B1 (definición).

Un Proceso puede, de forma OPCIONAL, definir columnas repetibles (`DefinicionParrilla`). Las columnas
son los `Campo` de un `Formulario` PROPIO de esa definición, independiente de la entrada inicial del
Proceso (`Servicio.formulario`): se editan con el mismo editor de campos del Studio y se versionan
con las mismas operaciones (`versionamiento.crear_nueva_version` / `activar_version`).

Aquí solo vive la configuración (activar, nombrar, desactivar). Los elementos de cada ejecución
están en `apps.tickets.parrilla`. Nada de esto depende de que el Proceso sea manual o programado.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import DefinicionEntregable, DefinicionParrilla, Formulario, Servicio
from apps.catalogo.versionamiento import crear_nueva_version
from apps.core.auditoria import registrar_evento, serializar
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


def definicion_de(servicio):
    """La `DefinicionParrilla` del Proceso (activa o no), o `None`."""
    return DefinicionParrilla.objects.select_related("formulario__version_activa").filter(servicio=servicio).first()


def definicion_activa_de(servicio):
    definicion = definicion_de(servicio)
    return definicion if definicion is not None and definicion.activa else None


def _limpiar_nombre(nombre):
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValidationError("Indique cómo se llama cada elemento (por ejemplo «Publicación»).")
    return nombre


@transaction.atomic
def configurar_parrilla(servicio, actor, *, nombre_elemento):
    """Activa la estructura de elementos del Proceso o actualiza cómo se llama cada elemento.

    La primera vez crea el `Formulario` de columnas (con su versión 1 en borrador, lista para editar);
    después reutiliza el mismo y reactiva la definición si estaba desactivada. No toca los Tickets ya
    creados: cada uno conserva la versión de columnas y el nombre que congeló."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Solo un Proceso puede definir una estructura de elementos.")
    nombre_elemento = _limpiar_nombre(nombre_elemento)
    if len(nombre_elemento) > 60:
        raise ValidationError("El nombre del elemento admite hasta 60 caracteres.")

    definicion = DefinicionParrilla.objects.select_for_update().filter(servicio=servicio).first()
    if definicion is None:
        formulario = Formulario(nombre=f"Elementos de {servicio.nombre}"[:150])
        formulario.full_clean()
        formulario.save()
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=formulario,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=None, datos_nuevos=serializar(formulario),
        )
        crear_nueva_version(formulario, actor)
        definicion = DefinicionParrilla.objects.create(
            servicio=servicio, formulario=formulario, nombre_elemento=nombre_elemento, activa=True
        )
        registrar_evento(
            accion=RegistroAuditoria.Accion.CREAR, instancia=definicion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=None, datos_nuevos=serializar(definicion),
        )
        return definicion

    anterior = serializar(definicion)
    definicion.nombre_elemento = nombre_elemento
    definicion.activa = True
    definicion.save()
    nuevo = serializar(definicion)
    if anterior != nuevo:
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores=anterior, datos_nuevos=nuevo,
        )
    return definicion


@transaction.atomic
def desactivar_parrilla(servicio, actor):
    """Los Tickets NUEVOS del Proceso dejan de tener elementos. Se conserva la definición y sus
    columnas (reactivar es volver a configurarla); los Tickets ya creados no cambian."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    definicion = DefinicionParrilla.objects.select_for_update().filter(servicio=servicio).first()
    if definicion is None or not definicion.activa:
        return definicion
    # 4.G2C1: un entregable de revisión de elementos no tiene qué revisar sin la estructura.
    usa = servicio.definiciones_entregables.filter(activo=True, tipo=DefinicionEntregable.Tipo.ELEMENTOS).first()
    if usa is not None:
        raise ValidationError(
            f"No se puede desactivar la estructura de elementos: el entregable «{usa.nombre}» revisa sus "
            "elementos. Retira ese entregable primero."
        )
    # 4.G2D2: lo mismo si un bloque del flujo convierte los elementos en tareas.
    from apps.catalogo.tareas_elemento import bloque_que_la_usa

    bloque = bloque_que_la_usa(servicio)
    if bloque is not None:
        raise ValidationError(
            f"No se puede desactivar la estructura de elementos: el bloque «{bloque.nombre}» del flujo genera "
            "tareas a partir de ellos. Quita ese bloque primero."
        )
    anterior = serializar(definicion)
    definicion.activa = False
    definicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=serializar(definicion),
    )
    return definicion


def errores_de_parrilla(servicio):
    """Por qué la estructura de elementos del Proceso no se podría usar HOY (lista vacía = bien o no
    la usa). Solo lectura; la comprueba `validar_publicacion` para que un Ticket nunca falle al nacer."""
    definicion = definicion_activa_de(servicio)
    if definicion is None:
        return []
    version = definicion.formulario.version_activa
    if version is None or version.estado != "ACTIVA":
        return [
            "La estructura de elementos no tiene una versión activa: define sus columnas y actívala "
            "en la pestaña Formulario."
        ]
    if not version.campos.exists():
        return ["La estructura de elementos no tiene columnas: agrega al menos una en la pestaña Formulario."]
    if version.campos.filter(tipo="ARCHIVO").exists():
        return ["Las columnas de tipo archivo todavía no se admiten en los elementos: quítalas de la estructura."]
    return []
