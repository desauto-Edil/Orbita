"""Reconciliación de la configuración de un Servicio con una versión nueva de su plantilla de fases — Fase 4.A.

Problema que resuelve: la plantilla (Workflow por fases) es compartida y versionada; los BLOQUES viven en la
configuración versionada de cada Servicio (`ConfiguracionEjecucionVersion`), atada a UNA versión de la plantilla.
Antes de 4.A, al publicar la plantilla otra versión, «Editar configuración» creaba una configuración VACÍA: el
diseñador perdía todos sus bloques y rutas.

Regla de 4.A:

- Cada fase tiene una CLAVE ESTABLE (`FaseWorkflow.clave`, copiada al clonar versiones). Los bloques de la
  configuración base (el borrador si existe, si no la activa) se trasladan a la fase de la versión vigente que
  tiene la misma clave (o, solo para fases sin clave, el mismo nombre, si es único).
- Si TODAS las fases con bloques tienen equivalente, la reconciliación es inequívoca y se hace sola al preparar
  la configuración (`preparar_configuracion_ejecucion`): nada se pierde.
- Si alguna fase con bloques ya no existe, es un CONFLICTO: nada se crea hasta que el diseñador decide, por cada
  fase desaparecida, a qué fase vigente mueve sus bloques o si los quita (decisión explícita, auditada).
- Nunca toca la configuración ACTIVA ni las de otros Servicios, ni modifica la plantilla: crea un BORRADOR nuevo.
  Mientras tanto la configuración activa sigue ejecutándose sobre la versión de la plantilla con la que se
  activó (`operaciones.validar_ejecucion`, motor `_iniciar_workflow_por_configuracion`); los tickets existentes
  conservan lo que congelaron.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.catalogo.models import ConfiguracionEjecucionVersion, Servicio
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria

QUITAR = "QUITAR"


@dataclass
class Conflicto:
    fase: object  # FaseWorkflow de la versión anterior que ya no existe
    bloques: list = field(default_factory=list)  # bloques del flujo principal en esa fase


@dataclass
class Analisis:
    base: object  # ConfiguracionEjecucionVersion de la que se parte
    version_destino: object  # WorkflowVersion vigente de la plantilla
    mapa: dict  # {pk fase anterior: FaseWorkflow vigente}
    conflictos: list
    fases_nuevas: list  # fases vigentes que no existían (quedan vacías)

    @property
    def inequivoco(self):
        return not self.conflictos


def base_de(servicio):
    """La configuración de la que se parte: el borrador más reciente o, si no hay, la activa."""
    borrador = servicio.configuraciones_ejecucion.filter(
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
    ).order_by("-numero").first()
    return borrador or servicio.configuracion_ejecucion_activa


def requiere_reconciliacion(servicio):
    """True si el Servicio tiene bloques configurados sobre una versión de la plantilla que ya no es la vigente."""
    workflow = servicio.workflow
    if workflow is None or workflow.modo != "PLANTILLA_FASES" or workflow.version_activa_id is None:
        return False
    base = base_de(servicio)
    return base is not None and base.workflow_version_id != workflow.version_activa_id


def _equivalente(fase, vigentes):
    if fase.clave:
        return next((f for f in vigentes if f.clave == fase.clave), None)
    mismos = [f for f in vigentes if f.nombre.strip().lower() == fase.nombre.strip().lower()]
    return mismos[0] if len(mismos) == 1 else None


def analizar(servicio):
    """`Analisis` de la reconciliación pendiente, o `None` si no hace falta."""
    if not requiere_reconciliacion(servicio):
        return None
    base = base_de(servicio)
    destino = servicio.workflow.version_activa
    vigentes = list(destino.fases.order_by("orden", "pk"))
    mapa, conflictos = {}, []
    usadas = set()
    for fase in base.workflow_version.fases.order_by("orden", "pk"):
        equivalente = _equivalente(fase, vigentes)
        bloques = list(base.bloques.filter(fase=fase, contenedor__isnull=True).order_by("orden", "pk"))
        if equivalente is not None:
            mapa[fase.pk] = equivalente
            usadas.add(equivalente.pk)
        elif bloques:
            conflictos.append(Conflicto(fase=fase, bloques=bloques))
    return Analisis(
        base=base, version_destino=destino, mapa=mapa, conflictos=conflictos,
        fases_nuevas=[f for f in vigentes if f.pk not in usadas],
    )


@transaction.atomic
def reconciliar(servicio, actor, *, resoluciones=None):
    """Crea un BORRADOR de configuración sobre la versión vigente de la plantilla con los bloques y rutas de la
    configuración base. `resoluciones`: `{pk fase anterior: pk fase vigente | QUITAR}` para cada conflicto; si
    falta alguna, no se crea nada (`ValidationError` que la nombra). Devuelve la configuración nueva."""
    from apps.catalogo.configuracion_ejecucion import clonar_contenido, _siguiente_numero
    from apps.catalogo.operaciones import _exigir_administracion

    _exigir_administracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    analisis = analizar(servicio)
    if analisis is None:
        raise ValidationError("La configuración ya corresponde a la versión vigente del flujo.")
    resoluciones = {str(k): str(v) for k, v in (resoluciones or {}).items()}
    vigentes = {f.pk: f for f in analisis.version_destino.fases.all()}
    excluir, movidas, quitadas = set(), {}, []
    pendientes = []
    for conflicto in analisis.conflictos:
        decision = resoluciones.get(str(conflicto.fase.pk), "")
        if decision == QUITAR:
            excluir.update(b.pk for b in conflicto.bloques)
            quitadas.append({"fase": conflicto.fase.nombre, "bloques": [b.nombre for b in conflicto.bloques]})
        elif decision.isdigit() and int(decision) in vigentes:
            movidas[conflicto.fase.pk] = vigentes[int(decision)]
        else:
            pendientes.append(conflicto.fase.nombre)
    if pendientes:
        raise ValidationError(
            "Decide qué hacer con los bloques de las fases que ya no existen en el flujo: " + ", ".join(pendientes) + "."
        )
    mapa = {**analisis.mapa, **movidas}
    base = analisis.base
    nueva = ConfiguracionEjecucionVersion.objects.create(
        servicio=servicio, workflow_version=analisis.version_destino, numero=_siguiente_numero(servicio),
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR,
    )
    clonar_contenido(base, nueva, fase_destino=lambda fase: mapa[fase.pk], excluir=excluir)
    if base.estado == ConfiguracionEjecucionVersion.Estado.BORRADOR:
        # El borrador anterior queda como historia (no se borra): ya no se podría activar sobre la plantilla vigente.
        base.estado = ConfiguracionEjecucionVersion.Estado.HISTORICA
        base.save(update_fields=["estado", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR, instancia=nueva, origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores=None,
        datos_nuevos={
            "reconciliada_desde_id": base.pk, "workflow_version_id": analisis.version_destino.pk,
            "fases_movidas": {str(k): v.pk for k, v in movidas.items()}, "bloques_quitados": quitadas,
        },
    )
    return nueva


def fases_en_uso(workflow):
    """`{clave de fase: [Servicio]}` de las fases que alguna configuración ACTIVA o en BORRADOR de los Servicios
    que usan `workflow` tiene con bloques. Lo usa el lienzo para advertir antes de eliminar o renombrar una fase
    en uso (los Servicios tendrán que reconciliar)."""
    from apps.catalogo.models import BloqueOperativo

    uso = {}
    for bloque in BloqueOperativo.objects.filter(
        version__servicio__workflow=workflow,
        version__estado__in=(ConfiguracionEjecucionVersion.Estado.ACTIVA, ConfiguracionEjecucionVersion.Estado.BORRADOR),
    ).select_related("fase", "version__servicio"):
        clave = bloque.fase.clave or f"nombre:{bloque.fase.nombre.strip().lower()}"
        servicios = uso.setdefault(clave, [])
        if bloque.version.servicio not in servicios:
            servicios.append(bloque.version.servicio)
    return uso


def servicios_afectados_por(version_borrador):
    """Servicios cuyas fases con bloques NO existen en `version_borrador` (borrador de la plantilla por publicar):
    al publicarla tendrán que decidir qué hacer con esos bloques. `[(Servicio, [nombres de fase])]`."""
    vigentes = list(version_borrador.fases.all())
    claves = {f.clave for f in vigentes if f.clave}
    nombres = {f.nombre.strip().lower() for f in vigentes}
    afectados = {}
    for clave, servicios in fases_en_uso(version_borrador.workflow).items():
        if clave.startswith("nombre:"):
            falta = clave[len("nombre:"):] not in nombres
        else:
            falta = clave not in claves
        if falta:
            for servicio in servicios:
                afectados.setdefault(servicio.pk, (servicio, []))[1].append(clave)
    return list(afectados.values())
