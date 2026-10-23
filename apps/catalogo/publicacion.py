"""Versiones y publicación de un Servicio/Proceso — Fase 4.A.

Antes de 4.A cada pieza se activaba por separado («Activar versión» del formulario y de cada colección,
«Activar configuración» del flujo) y «Actualizar publicación» no publicaba nada nuevo en un Servicio ya
publicado. Ahora se distinguen tres acciones:

- GUARDAR BORRADOR: cada formulario de edición guarda en el borrador de su pieza; no cambia lo vigente.
- ACTIVAR una pieza: la acción local de su sección (se conserva), pone en vigor SOLO esa pieza.
- PUBLICAR CAMBIOS (`publicar_cambios`): activa en UNA transacción todos los borradores pendientes (formulario de
  inicio, colecciones, configuración del flujo) y publica el Servicio. Si algo falla, no cambia nada.

Sin sistema de versiones paralelo: usa `FormularioVersion`, `ConfiguracionEjecucionVersion` y las operaciones de
activación existentes. Lo que se guarda directamente (gacetas, entregables, tiempos, políticas, reglas de cierre)
se informa como «aplica a los tickets nuevos al guardar». Ningún ticket existente cambia: cada uno congeló lo
suyo al nacer.
"""

from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.catalogo.models import ConfiguracionEjecucionVersion, FormularioVersion


@dataclass
class Pieza:
    clave: str
    etiqueta: str
    seccion: tuple  # (pestaña, uso)
    activa: object = None  # número de la versión vigente (o None)
    borrador: object = None  # objeto de la versión en borrador pendiente (o None)
    nota: str = ""
    permiso: str = ""  # capacidad que exige activarla
    detalle: list = field(default_factory=list)

    @property
    def pendiente(self):
        return self.borrador is not None


def _borrador_de(formulario):
    if formulario is None:
        return None
    return formulario.versiones.filter(estado=FormularioVersion.Estado.BORRADOR).order_by("-numero").first()


def piezas(servicio):
    """Las piezas versionadas del Servicio con su versión vigente y su borrador pendiente."""
    from apps.catalogo.parrilla import definiciones_de
    from apps.catalogo import reconciliacion
    from apps.catalogo.operaciones import datos_inicio_configurados

    lista = []
    formulario = servicio.formulario
    activa = formulario.version_activa if formulario is not None else None
    # El formulario técnico vacío de un Proceso sin datos de inicio no es una pieza que se configure ni se publique.
    if datos_inicio_configurados(servicio) or servicio.tipo != "PROCESO" or servicio.es_ticket_general:
        lista.append(Pieza(
            "formulario", "Formulario de inicio" if servicio.tipo != "PROCESO" else "Datos de inicio",
            ("entrada", ""), activa=activa.numero if activa else None, borrador=_borrador_de(formulario),
            permiso="formulario.administrar",
        ))
    if servicio.tipo == "PROCESO" and not servicio.es_ticket_general:
        for definicion in definiciones_de(servicio):
            if not definicion.activa:
                continue
            vigente = definicion.formulario.version_activa
            lista.append(Pieza(
                f"coleccion:{definicion.clave}", f"Colección «{definicion.nombre_elemento}»", ("entrada", "parrilla"),
                activa=vigente.numero if vigente else None, borrador=_borrador_de(definicion.formulario),
                permiso="formulario.administrar",
            ))
    workflow = servicio.workflow
    if workflow is not None and workflow.modo == "PLANTILLA_FASES":
        activa_cfg = servicio.configuracion_ejecucion_activa
        borrador = servicio.configuraciones_ejecucion.filter(
            estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
        ).order_by("-numero").first()
        nota = ""
        if reconciliacion.requiere_reconciliacion(servicio):
            nota = (
                f"El flujo «{workflow.nombre}» publicó la versión {workflow.version_activa.numero}; esta configuración "
                "sigue sobre una anterior hasta que la actualices en la sección Flujo."
            )
        lista.append(Pieza(
            "flujo", f"Configuración del flujo «{workflow.nombre}»", ("ejecucion", ""),
            activa=activa_cfg.numero if activa_cfg else None,
            borrador=borrador if borrador is not None and borrador.workflow_version_id == workflow.version_activa_id else None,
            nota=nota, permiso="workflows.administrar",
        ))
    return lista


def aplican_al_guardar(servicio):
    """Lo que no tiene borrador: entra en vigor para los tickets nuevos en cuanto se guarda (informativo)."""
    from apps.catalogo.models import DefinicionGaceta

    filas = []
    if servicio.tipo == "PROCESO" and not servicio.es_ticket_general:
        for gaceta in DefinicionGaceta.objects.filter(servicio=servicio, activa=True).order_by("orden", "pk"):
            filas.append(f"Gaceta «{gaceta.nombre}» (v{gaceta.version})")
        filas.append("Condiciones de finalización")
        filas.append("Tareas por elemento")
    filas += ["Resultados y revisiones de Salida", "Tiempo objetivo y prórroga", "Entrega al solicitante"]
    return filas


def _exigir_permiso(actor, codigo, etiqueta):
    from apps.core.autorizacion import usuario_tiene_permiso
    from apps.workflows.autorizacion import puede_administrar_workflows

    permitido = puede_administrar_workflows(actor) if codigo == "workflows.administrar" else usuario_tiene_permiso(actor, codigo)
    if not permitido:
        raise PermissionDenied(f"No tienes permiso para activar «{etiqueta}».")


def _activar(servicio, pieza, actor, *, verificar_permisos=True):
    from apps.catalogo.configuracion_ejecucion import activar_configuracion_ejecucion
    from apps.catalogo.parrilla import definicion_de
    from apps.catalogo.revision_elementos import errores_de_version_de_columnas
    from apps.catalogo.tareas_elemento import errores_de_version_para_tareas
    from apps.catalogo.versionamiento import activar_version

    if verificar_permisos:
        _exigir_permiso(actor, pieza.permiso, pieza.etiqueta)
    if pieza.clave == "flujo":
        activar_configuracion_ejecucion(servicio, pieza.borrador, actor)
        return
    version = pieza.borrador
    if pieza.clave.startswith("coleccion:"):
        por_defecto = definicion_de(servicio)
        if por_defecto is not None and por_defecto.formulario_id == version.formulario_id:
            errores = errores_de_version_de_columnas(servicio, version) + errores_de_version_para_tareas(servicio, version)
            if errores:
                raise ValidationError(errores)
    try:
        activar_version(version.formulario, version, actor)
    except ValueError as exc:
        raise ValidationError(f"{pieza.etiqueta}: {exc}")


@transaction.atomic
def publicar_cambios(servicio, actor, *, activar=None, verificar_permisos=True, bloquear=True):
    """Activa los borradores pendientes y publica, todo o nada. Devuelve `[etiqueta vN activada]` (vacía si no
    había borradores: entonces solo publica, o no hace nada si ya estaba publicado). `activar`: la operación que
    publica el Servicio (por defecto `operaciones.activar_servicio`; la vista pasa la suya). `bloquear=False` solo
    para una simulación que siempre se deshace: no toma el lock de la fila del Servicio (publicar de verdad lo toma
    siempre y revalida)."""
    from apps.catalogo.models import Servicio
    from apps.catalogo.operaciones import activar_servicio

    activar_servicio = activar or activar_servicio

    consulta = Servicio.objects.select_for_update() if bloquear else Servicio.objects
    servicio = consulta.get(pk=servicio.pk)
    activadas = []
    for pieza in piezas(servicio):  # formulario y colecciones antes que el flujo (el flujo puede leer sus campos)
        if not pieza.pendiente:
            continue
        _activar(servicio, pieza, actor, verificar_permisos=verificar_permisos)
        activadas.append(f"{pieza.etiqueta} v{pieza.borrador.numero}")
        servicio.refresh_from_db()
    activar_servicio(servicio, actor)
    return activadas


class _Simulacion(Exception):
    pass


def simular(servicio, actor, *, verificar_permisos=True, activar=None, bloquear=True):
    """Qué pasaría al publicar, SIN cambiar nada: `(publicable, errores, activaria)`. Ejecuta la publicación real
    dentro de una transacción que siempre se deshace, así los errores son exactamente los que daría publicar.
    `verificar_permisos=False` evalúa la CONFIGURACIÓN sin el permiso por pieza de quien consulta (lo usa la
    evaluación del Studio, `apps.catalogo.evaluacion`); publicar de verdad siempre los exige."""
    resultado = {}
    try:
        with transaction.atomic():
            resultado["activaria"] = publicar_cambios(
                servicio, actor, activar=activar, verificar_permisos=verificar_permisos, bloquear=bloquear
            )
            raise _Simulacion
    except _Simulacion:
        return True, [], resultado["activaria"]
    except ValidationError as exc:
        return False, list(exc.messages), _pendientes_por_activar(servicio)
    except PermissionDenied as exc:
        return False, [str(exc) or "No tienes permiso para publicar."], _pendientes_por_activar(servicio)


def _pendientes_por_activar(servicio):
    """Lo que entraría en vigor al publicar (mismo texto que `publicar_cambios`), aunque hoy algo lo impida."""
    return [f"{p.etiqueta} v{p.borrador.numero}" for p in piezas(servicio) if p.pendiente]


def permisos_que_faltan(servicio, actor):
    """Piezas pendientes que `actor` no puede activar (lo que `_exigir_permiso` le negaría al publicar)."""
    faltan = []
    for pieza in piezas(servicio):
        if not pieza.pendiente:
            continue
        try:
            _exigir_permiso(actor, pieza.permiso, pieza.etiqueta)
        except PermissionDenied as exc:
            faltan.append(str(exc))
    return faltan
