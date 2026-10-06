"""Diseñador (Fase D1) — entrada unificada para quien configura Órbita.

Unifica la EXPERIENCIA de dos herramientas que ya existen, sin tocar sus
dominios ni crear modelos:

  Flujos      el catálogo de `Workflow` (definición reutilizable) y sus
              versiones. Hoy se abre con la vista técnica existente.
  Servicios   Servicios y Procesos (`Servicio`), que se configuran en Studio.

Autorización (nunca por nombre de rol; cada sección depende de capacidades
reales que ya existen):

  catalogo.administrar               configurar Servicios/Procesos
  workflows.administrar              crear, modificar, versionar y publicar Flujos
  workflows.consultar                ver Flujos y su definición (solo lectura)
  workflows.vincular                 elegir un Flujo PUBLICADO para un Servicio
                                     (sin poder modificarlo)

Quién entra al Diseñador y qué ve:

  entra            `catalogo.administrar` o `workflows.consultar|administrar`.
  Sección Servicios  `catalogo.administrar`.
  Sección Flujos     `workflows.consultar|administrar` (biblioteca completa) o
                     `workflows.vincular` (solo los Flujos publicados, sin
                     acceso a su definición).
  "Nuevo flujo" y "Abrir flujo"  solo con `workflows.administrar` /
                     `workflows.consultar` respectivamente.

Este módulo importa los dominios de forma perezosa (mismo criterio que
`navegacion.py`): `core` no depende de ellos de forma permanente.
"""

from apps.core.autorizacion import usuario_tiene_permiso

LIMITE_RECIENTES = 5


def capacidades(usuario):
    """Qué puede hacer `usuario` dentro del Diseñador, resuelto una sola vez."""
    from apps.workflows.autorizacion import (
        puede_administrar_workflows,
        puede_consultar_workflows,
        puede_vincular_workflows,
    )

    gestionar_servicios = usuario_tiene_permiso(usuario, "catalogo.administrar")
    administrar_flujos = puede_administrar_workflows(usuario)
    consultar_flujos = administrar_flujos or puede_consultar_workflows(usuario)
    vincular_flujos = administrar_flujos or puede_vincular_workflows(usuario)
    return {
        "gestionar_servicios": gestionar_servicios,
        "administrar_flujos": administrar_flujos,
        "consultar_flujos": consultar_flujos,
        "vincular_flujos": vincular_flujos,
        # Quién entra: la misma regla que la navegación global.
        "accede": gestionar_servicios or consultar_flujos,
        "ve_servicios": gestionar_servicios,
        "ve_flujos": consultar_flujos or vincular_flujos,
    }


def accede_al_disenador(usuario):
    """Regla mínima de la navegación global: `catalogo.administrar` o poder
    consultar Flujos (`workflows.consultar|administrar`)."""
    from apps.workflows.autorizacion import puede_consultar_workflows

    return usuario_tiene_permiso(usuario, "catalogo.administrar") or puede_consultar_workflows(usuario)


def pestanas(caps, activa):
    """Pestañas LOCALES del Diseñador (no son navegación global)."""
    tabs = [{"clave": "inicio", "etiqueta": "Inicio", "url_name": "core:disenador"}]
    if caps["ve_flujos"]:
        tabs.append({"clave": "flujos", "etiqueta": "Flujos", "url_name": "core:disenador_flujos"})
    if caps["ve_servicios"]:
        tabs.append({"clave": "servicios", "etiqueta": "Servicios", "url_name": "core:disenador_servicios"})
    for tab in tabs:
        tab["activa"] = tab["clave"] == activa
    return tabs


# --- Flujos ---------------------------------------------------------------


def _flujos(caps, *, con_servicios=False):
    """Flujos visibles para el usuario, con lo necesario para la biblioteca:
    estado, si hay una versión en diseño y cuántos servicios lo usan (y, si se
    pide, cuáles)."""
    from django.db.models import Count, Exists, OuterRef, Prefetch

    from apps.catalogo.models import Servicio
    from apps.workflows.models import Workflow, WorkflowVersion

    en_diseno = WorkflowVersion.objects.filter(workflow=OuterRef("pk"), estado=WorkflowVersion.Estado.BORRADOR)
    consulta = (
        Workflow.objects.select_related("version_activa")
        .filter(modo=Workflow.Modo.PLANTILLA_FASES)
        .annotate(
            n_servicios=Count("servicios", distinct=True),
            n_versiones=Count("versiones", distinct=True),
            tiene_borrador=Exists(en_diseno),
        )
        .order_by("nombre")
    )
    if con_servicios:
        consulta = consulta.prefetch_related(
            Prefetch("servicios", queryset=Servicio.objects.only("id", "nombre", "workflow_id").order_by("nombre")),
            Prefetch(
                "versiones",
                queryset=WorkflowVersion.objects.prefetch_related("fases").order_by("-numero"),
                to_attr="versiones_resumen",
            ),
        )
    if not caps["consultar_flujos"]:
        # Quien solo puede vincular ve únicamente lo que podría vincular.
        consulta = consulta.filter(version_activa__isnull=False)
    return consulta


def biblioteca_de_flujos(caps):
    from apps.workflows.models import WorkflowVersion

    flujos = list(_flujos(caps, con_servicios=True))
    for flujo in flujos:
        usados = list(flujo.servicios.all())
        versiones = list(getattr(flujo, "versiones_resumen", []))
        version_tarjeta = (
            next((v for v in versiones if v.estado == WorkflowVersion.Estado.BORRADOR), None)
            or flujo.version_activa
            or (versiones[0] if versiones else None)
        )
        fases = list(version_tarjeta.fases.all()) if version_tarjeta else []
        flujo.usado_por = usados[:3]
        flujo.usado_por_extra = max(len(usados) - 3, 0)
        flujo.version_tarjeta = version_tarjeta
        flujo.fases_resumen = fases[:4]
        flujo.fases_extra = max(len(fases) - 4, 0)
        flujo.n_fases = len(fases)
    return flujos


def resumen_de_flujos(caps):
    """`{"publicados", "en_diseno", "total"}` y los flujos con trabajo en curso."""
    flujos = list(_flujos(caps))
    publicados = sum(1 for f in flujos if f.version_activa_id is not None)
    en_diseno = [f for f in flujos if f.tiene_borrador]
    return {
        "publicados": publicados,
        "en_diseno": len(en_diseno),
        "total": len(flujos),
        "recientes": sorted(en_diseno, key=lambda f: f.actualizado_en, reverse=True)[:LIMITE_RECIENTES],
    }


# --- Servicios y Procesos ----------------------------------------------------


def _servicios():
    from apps.catalogo.models import Servicio

    return Servicio.objects.select_related("categoria", "workflow")


def lista_de_servicios():
    return list(_servicios().order_by("nombre"))


def resumen_de_servicios():
    from django.db.models import Count, Q

    cuentas = _servicios().aggregate(
        publicados=Count("pk", filter=Q(activo=True)),
        borradores=Count("pk", filter=Q(activo=False)),
    )
    recientes = list(_servicios().filter(activo=False).order_by("-actualizado_en")[:LIMITE_RECIENTES])
    return {**cuentas, "total": cuentas["publicados"] + cuentas["borradores"], "recientes": recientes}


def ticket_general():
    """Estado del Ticket General para la tarjeta de Diseñador › Servicios
    (4.C1): lo calcula el dominio de catálogo; aquí solo se compone."""
    from apps.catalogo.ticket_general import estado_para_administracion

    return estado_para_administracion()
