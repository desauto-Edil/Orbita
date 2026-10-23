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

from apps.catalogo import claves
from apps.catalogo.models import DefinicionEntregable, DefinicionParrilla, Formulario, ParticipanteColeccion, Servicio
from apps.catalogo.versionamiento import crear_nueva_version
from apps.core.auditoria import registrar_evento, serializar
from apps.core.autorizacion import usuario_tiene_permiso
from apps.core.models import RegistroAuditoria


def _exigir_administracion(actor):
    if not usuario_tiene_permiso(actor, "catalogo.administrar"):
        raise PermissionDenied("No tiene autorización para administrar el catálogo.")


def definiciones_de(servicio):
    """Todas las colecciones del Proceso (activas o no), en orden; la primera es la por defecto."""
    return list(
        DefinicionParrilla.objects.select_related("formulario__version_activa")
        .filter(servicio=servicio)
        .order_by("orden", "pk")
    )


def definiciones_activas_de(servicio):
    return [definicion for definicion in definiciones_de(servicio) if definicion.activa]


def definicion_de(servicio, clave=None):
    """La colección `clave` del Proceso (activa o no), o `None`. Sin `clave`: la colección POR DEFECTO
    (la primera por `orden`), la única que conocen las piezas anteriores a las colecciones múltiples
    (tareas por elemento, revisión por conjunto)."""
    consulta = DefinicionParrilla.objects.select_related("formulario__version_activa").filter(servicio=servicio)
    if clave is not None:
        consulta = consulta.filter(clave=clave)
    return consulta.order_by("orden", "pk").first()


def definicion_activa_de(servicio, clave=None):
    definicion = definicion_de(servicio, clave)
    return definicion if definicion is not None and definicion.activa else None


def _limpiar_nombre(nombre):
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValidationError("Indique cómo se llama cada elemento (por ejemplo «Publicación»).")
    return nombre


def _crear_coleccion(servicio, actor, nombre_elemento):
    """Alta de una colección con su `Formulario` de columnas (versión 1 en borrador). La clave se
    genera una vez desde el nombre del ítem, única dentro del Proceso; va al final del orden."""
    existentes = list(DefinicionParrilla.objects.filter(servicio=servicio).values_list("clave", "orden"))
    clave = claves.clave_unica(
        claves.clave_desde_texto(nombre_elemento, por_defecto="elementos"), [c for c, _ in existentes]
    )
    orden = max((o for _, o in existentes), default=0) + 1
    formulario = Formulario(nombre=f"{nombre_elemento} de {servicio.nombre}"[:150])
    formulario.full_clean()
    formulario.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=formulario,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(formulario),
    )
    crear_nueva_version(formulario, actor)
    definicion = DefinicionParrilla.objects.create(
        servicio=servicio, formulario=formulario, nombre_elemento=nombre_elemento, activa=True,
        clave=clave, orden=orden,
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=definicion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None, datos_nuevos=serializar(definicion),
    )
    return definicion


def _proceso_bloqueado(servicio, actor):
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        raise ValidationError("Solo un Proceso puede definir una estructura de elementos.")
    return servicio


def _nombre_valido(nombre_elemento):
    nombre_elemento = _limpiar_nombre(nombre_elemento)
    if len(nombre_elemento) > 60:
        raise ValidationError("El nombre del elemento admite hasta 60 caracteres.")
    return nombre_elemento


@transaction.atomic
def agregar_coleccion(servicio, actor, *, nombre_elemento):
    """Fase 1 — agrega OTRA colección de ítems al Proceso (independiente de las demás: sus propias
    columnas, versiones y clave). La primera colección de un Proceso también puede crearse así."""
    servicio = _proceso_bloqueado(servicio, actor)
    return _crear_coleccion(servicio, actor, _nombre_valido(nombre_elemento))


@transaction.atomic
def configurar_parrilla(servicio, actor, *, nombre_elemento, clave=None, permite_altas_extraordinarias=None):
    """Activa una colección del Proceso o actualiza cómo se llama cada ítem. Sin `clave` actúa sobre la
    colección por defecto (y la crea si el Proceso aún no tiene ninguna).

    La primera vez crea el `Formulario` de columnas (con su versión 1 en borrador, lista para editar);
    después reutiliza el mismo y reactiva la definición si estaba desactivada. No toca los Tickets ya
    creados: cada uno conserva la versión de columnas y el nombre que congeló. La clave NO cambia al
    renombrar: es la identidad estable de la colección."""
    servicio = _proceso_bloqueado(servicio, actor)
    nombre_elemento = _nombre_valido(nombre_elemento)

    consulta = DefinicionParrilla.objects.select_for_update().filter(servicio=servicio)
    if clave is not None:
        consulta = consulta.filter(clave=clave)
    definicion = consulta.order_by("orden", "pk").first()
    if definicion is None and clave is not None:
        raise ValidationError("Esa colección no pertenece a este proceso.")
    if definicion is None:
        definicion = _crear_coleccion(servicio, actor, nombre_elemento)
        if permite_altas_extraordinarias:
            definicion.permite_altas_extraordinarias = True
            definicion.save(update_fields=["permite_altas_extraordinarias", "actualizado_en"])
        return definicion
    anterior = serializar(definicion)
    definicion.nombre_elemento = nombre_elemento
    definicion.activa = True
    if permite_altas_extraordinarias is not None:
        definicion.permite_altas_extraordinarias = bool(permite_altas_extraordinarias)
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
def desactivar_parrilla(servicio, actor, clave=None):
    """Los Tickets NUEVOS del Proceso dejan de tener esa colección (sin `clave`: la por defecto). Se
    conserva la definición y sus columnas (reactivar es volver a configurarla); los Tickets ya creados
    no cambian."""
    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    definiciones = list(DefinicionParrilla.objects.select_for_update().filter(servicio=servicio).order_by("orden", "pk"))
    definicion = next((d for d in definiciones if d.clave == clave), None) if clave is not None else (
        definiciones[0] if definiciones else None
    )
    if definicion is None or not definicion.activa:
        return definicion
    if definicion is not definiciones[0]:
        # Solo la colección por defecto alimenta la revisión por conjunto y las tareas por elemento.
        return _desactivar(definicion, actor)
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
    return _desactivar(definicion, actor)


def _desactivar(definicion, actor):
    anterior = serializar(definicion)
    definicion.activa = False
    definicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=anterior, datos_nuevos=serializar(definicion),
    )
    return definicion


def _errores_de_coleccion(definicion, varias):
    nombre = f"La colección «{definicion.nombre_elemento}»" if varias else "La estructura de elementos"
    version = definicion.formulario.version_activa
    if version is None or version.estado != "ACTIVA":
        return [f"{nombre} no tiene una versión activa: define sus columnas y actívala en la pestaña Formulario."]
    if not version.campos.exists():
        return [f"{nombre} no tiene columnas: agrega al menos una en la pestaña Formulario."]
    return []


def errores_de_parrilla(servicio):
    """Por qué alguna colección activa del Proceso no se podría usar HOY (lista vacía = bien o no las
    usa). Solo lectura; la comprueba `validar_publicacion` para que un Ticket nunca falle al nacer."""
    activas = definiciones_activas_de(servicio)
    errores = []
    for definicion in activas:
        errores.extend(_errores_de_coleccion(definicion, len(activas) > 1))
    return errores


# --- Bloque 3: participantes de una colección ---------------------------------------------------------------


def participantes_de(definicion):
    """`(usuarios, equipos)` configurados como participantes de la colección `definicion`."""
    filas = list(definicion.participantes.select_related("usuario", "equipo"))
    return [f.usuario for f in filas if f.usuario_id], [f.equipo for f in filas if f.equipo_id]


@transaction.atomic
def configurar_participantes(servicio, actor, *, usuarios, equipos, clave=None):
    """Bloque 3 — define quién PARTICIPA en una colección (sin `clave`: la por defecto): personas o equipos ACTIVOS
    que crean y editan sus ítems durante la ejecución sin ser responsables del Ticket. Reemplaza la configuración
    (mismo criterio que los revisores, `revision_elementos.guardar_configuracion`); cada Ticket congela la suya al
    nacer, así que cambiarla no altera tickets ya creados. Auditado."""
    servicio = _proceso_bloqueado(servicio, actor)
    definicion = definicion_de(servicio, clave)
    if definicion is None:
        raise ValidationError("Este proceso no tiene esa colección de ítems.")
    usuarios, equipos = list(dict.fromkeys(usuarios or [])), list(dict.fromkeys(equipos or []))
    if any(not u.is_active for u in usuarios) or any(not e.activo for e in equipos):
        raise ValidationError("Los participantes deben ser personas y equipos activos.")
    definicion = DefinicionParrilla.objects.select_for_update().get(pk=definicion.pk)
    antes_u, antes_e = participantes_de(definicion)
    anterior = {"usuarios": sorted(u.pk for u in antes_u), "equipos": sorted(e.pk for e in antes_e)}
    nuevo = {"usuarios": sorted(u.pk for u in usuarios), "equipos": sorted(e.pk for e in equipos)}
    if anterior == nuevo:
        return definicion
    definicion.participantes.filter(usuario__isnull=False).exclude(usuario_id__in=nuevo["usuarios"]).delete()
    definicion.participantes.filter(equipo__isnull=False).exclude(equipo_id__in=nuevo["equipos"]).delete()
    for usuario in usuarios:
        if usuario.pk not in anterior["usuarios"]:
            ParticipanteColeccion.objects.create(definicion=definicion, usuario=usuario)
    for equipo in equipos:
        if equipo.pk not in anterior["equipos"]:
            ParticipanteColeccion.objects.create(definicion=definicion, equipo=equipo)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=definicion,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"participantes": anterior}, datos_nuevos={"participantes": nuevo},
    )
    return definicion
