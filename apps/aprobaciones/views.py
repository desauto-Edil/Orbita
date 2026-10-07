"""Vistas de Aprobaciones — 3.UI.2 (CU-024/025).

Interfaz propia de Órbita: reemplaza a Django Admin (que en `apps/
aprobaciones/admin.py` es y sigue siendo de solo lectura) para el trabajo
humano normal sobre `Aprobacion`. Mismo patrón exacto que `apps/tareas/
views.py` — su precedente más directo, ya validado en 3.UI.1:
function-based, sin DRF, PRG, sin lógica de dominio en la vista.

**Autorización — misma regla obligatoria que 3.UI.1**: cada vista de
acción vuelve a cargar el objeto y llama explícitamente al `puede_*` del
dominio ANTES de invocar la operación; si no corresponde, deja propagar
`PermissionDenied` sin capturarlo (Django lo convierte en 403). El
`try/except` alrededor de la operación solo atrapa `ValidationError`.

**Misma excepción documentada al límite `apps.aprobaciones` nunca importa
`apps.workflows`** que ya existe para `apps/tareas/views.py` (ver su
docstring): esa regla protege la capa de DOMINIO de Aprobaciones
(`models.py`/`operaciones.py`/`autorizacion.py`/`consultas.py`), para que
`Aprobacion` siga siendo reutilizable fuera de Workflow. La capa HTTP no
es dominio. Esta vista necesita:

1. Decidir, para UNA sola acción (decidir), cuál API pública de dominio
   invocar: si el `EsquemaAprobacion` de la aprobación está vinculado a
   una `InstanciaEtapa` (`hasattr(aprobacion.esquema, "vinculo_workflow")`
   — verdadero solo por la relación inversa que `apps.workflows.models.
   EsquemaAprobacionWorkflow` declara, sin que este módulo necesite saber
   qué es esa relación), la única forma correcta de decidir es
   `apps.workflows.integracion.resolver_aprobacion_workflow` (resuelve la
   `Aprobacion` y, si el esquema cierra, reanuda el motor en una sola
   transacción) — nunca `apps.aprobaciones.operaciones.resolver_aprobacion`
   a secas, que dejaría el Workflow colgado en EN_ESPERA si el esquema ya
   cerró. Una aprobación independiente (sin vínculo) nunca tiene esa
   relación, así que cae siempre en la rama `resolver_aprobacion` sin
   necesitar ninguna comprobación adicional.
2. Presentar, solo quien lo tenga, contexto legible de Workflow/versión/
   etapa — sin ese vínculo la pantalla sigue funcionando (bloque
   condicional), y sin convertirlo en requisito universal del modelo
   `Aprobacion`."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse, HttpResponseNotAllowed
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET

from apps.aprobaciones.autorizacion import (
    es_aprobador_directo,
    puede_aprobar,
    puede_consultar_aprobacion,
    puede_reasignar_aprobacion,
)
from apps.aprobaciones.consultas import aprobaciones_pendientes_para, aprobaciones_visibles_para
from apps.aprobaciones.forms import DecisionAprobacionForm, ReasignacionAprobacionForm
from apps.aprobaciones.models import Aprobacion
from apps.aprobaciones.operaciones import reasignar_aprobacion, resolver_aprobacion
from apps.tickets.autorizacion import puede_consultar_ticket
from apps.tickets.models import Ticket
from apps.tickets.prorrogas import resolver_prorroga_por_aprobacion
from apps.workflows.integracion import resolver_aprobacion_workflow
from apps.workflows.models import EsquemaAprobacionWorkflow, InstanciaEtapa


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _prorroga_del_esquema(esquema):
    """4.A2 — la `ProrrogaTicket` que originó este esquema, o `None`. Misma
    relación inversa por la que `vinculo_workflow` identifica trabajo de
    Workflow: aprobaciones no necesita saber qué es una prórroga, solo que
    existe quien debe enterarse cuando el esquema cierra."""
    return getattr(esquema, "prorroga", None)


def _contexto_prorroga(usuario, esquema):
    prorroga = _prorroga_del_esquema(esquema)
    if prorroga is None:
        return None
    ticket = prorroga.ticket
    return {
        "prorroga": prorroga,
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "puede_ver_ticket": puede_consultar_ticket(usuario, ticket),
    }


def _contexto_workflow_por_esquema(esquema_ids):
    """Resuelve, en una sola consulta, el contexto Workflow de los esquemas indicados que estén
    vinculados — sin N+1 por fila de bandeja. Los esquemas sin vínculo simplemente no aparecen en
    el dict resultante; el llamador trata su ausencia como "sin Workflow".

    Dos formas (4.E2): LEGACY (`Etapa`: flujo / versión / etapa) y POR FASES (`BloqueOperativo`: la
    ejecución no tiene `etapa`, sino fase y bloque). `nombre` es siempre lo que se está haciendo
    (etapa o bloque) y `por_fases` dice cuál de las dos formas es."""
    vinculos = EsquemaAprobacionWorkflow.objects.filter(esquema_id__in=esquema_ids).select_related(
        "instancia_etapa__etapa__version__workflow",
        "instancia_etapa__bloque_operativo",
        "instancia_etapa__fase_workflow",
        "instancia_etapa__instancia_workflow__workflow_version__workflow",
    )
    contexto = {}
    for vinculo in vinculos:
        ejecucion = vinculo.instancia_etapa
        if ejecucion.bloque_operativo_id:
            version = ejecucion.instancia_workflow.workflow_version
            contexto[vinculo.esquema_id] = {
                "workflow": version.workflow,
                "version": version,
                "etapa": None,
                "fase": ejecucion.fase_workflow,
                "bloque": ejecucion.bloque_operativo,
                "nombre": ejecucion.bloque_operativo.nombre,
                "por_fases": True,
            }
        else:
            etapa = ejecucion.etapa
            contexto[vinculo.esquema_id] = {
                "workflow": etapa.version.workflow,
                "version": etapa.version,
                "etapa": etapa,
                "fase": None,
                "bloque": None,
                "nombre": etapa.nombre,
                "por_fases": False,
            }
    return contexto


def _contexto_entregable_revisado(usuario, aprobacion):
    """4.E2 — si esta aprobación REVISA un entregable (`BloqueOperativo.entregable_revisado`),
    qué entregable es y en qué estado está según el dominio; `None` para una aprobación general.
    El contenido NO se copia: se lee del `EntregableTicket` del Ticket. El aprobador (o quien
    pueda consultar el ticket) ve el valor y descarga los archivos por la vista de adjuntos del
    Ticket; el resto solo ve el nombre y el estado."""
    vinculo = (
        EsquemaAprobacionWorkflow.objects.filter(esquema_id=aprobacion.esquema_id)
        .select_related("instancia_etapa__bloque_operativo__entregable_revisado__definicion_entregable")
        .first()
    )
    if vinculo is None:
        return None
    ejecucion = vinculo.instancia_etapa
    bloque = ejecucion.bloque_operativo
    if bloque is None or bloque.entregable_revisado_id is None:
        return None
    definicion = bloque.entregable_revisado.definicion_entregable
    ticket = (
        Ticket.objects.filter(instancia_workflow_id=ejecucion.instancia_workflow_id)
        .select_related("detalle_servicio__servicio")
        .first()
    )
    entregable = (
        ticket.entregables.filter(definicion_id=definicion.pk).first() if ticket is not None else None
    )
    if entregable is None:
        return None
    puede_ver_ticket = puede_consultar_ticket(usuario, ticket)
    es_revisor = any(es_aprobador_directo(usuario, p) for p in aprobacion.esquema.participaciones.all())
    ver_contenido = puede_ver_ticket or es_revisor
    revision = InstanciaEtapa.objects.filter(
        instancia_workflow_id=ejecucion.instancia_workflow_id, bloque_operativo_id=bloque.pk
    ).count()
    return {
        "nombre": entregable.nombre,
        "tipo": entregable.get_tipo_display(),
        "entregado": entregable.satisfecho,
        "ticket": ticket,
        "servicio": ticket.detalle_servicio.servicio,
        "puede_ver_ticket": puede_ver_ticket,
        "ver_contenido": ver_contenido,
        "texto": entregable.texto if ver_contenido else "",
        "enlace": entregable.enlace if ver_contenido else "",
        "archivos": (
            list(entregable.archivos.filter(retirado_en__isnull=True).order_by("creado_en"))
            if ver_contenido
            else []
        ),
        "revision": revision,
    }


@login_required
def bandeja_view(request):
    """RQF-078 — "Pendientes" (`aprobaciones_pendientes_para`) y "Todas"
    (`aprobaciones_visibles_para`), ambas consultas ya existentes de
    `apps.aprobaciones.consultas`, usadas sin reescribir su `QuerySet`."""
    tab = request.GET.get("tab")
    if tab == "todas":
        aprobaciones = aprobaciones_visibles_para(request.user)
    else:
        tab = "pendientes"
        aprobaciones = aprobaciones_pendientes_para(request.user)
    aprobaciones = aprobaciones.select_related("esquema", "aprobador_usuario", "aprobador_equipo").order_by(
        "-creado_en"
    )

    contexto_workflow = _contexto_workflow_por_esquema({a.esquema_id for a in aprobaciones})
    filas = [
        {"aprobacion": aprobacion, "contexto_workflow": contexto_workflow.get(aprobacion.esquema_id)}
        for aprobacion in aprobaciones
    ]
    contexto = {"tab": tab, "filas": filas, "titulo_pagina": "Aprobaciones"}
    return render(request, "aprobaciones/lista.html", contexto)


@login_required
@require_GET
def vista_previa_view(request, pk):
    """Fragmento de LECTURA para el panel de vista previa de Mi trabajo. Misma
    autorización de objeto que el detalle (`puede_consultar_aprobacion`); la
    decisión se registra al abrirla, nunca desde aquí."""
    aprobacion = get_object_or_404(
        Aprobacion.objects.select_related("esquema", "aprobador_usuario", "aprobador_equipo"), pk=pk
    )
    if not puede_consultar_aprobacion(request.user, aprobacion):
        raise PermissionDenied
    participaciones = list(
        aprobacion.esquema.participaciones.select_related("aprobador_usuario", "aprobador_equipo").order_by("orden")
    )
    contexto = {
        "aprobacion": aprobacion,
        "esquema": aprobacion.esquema,
        "contexto_workflow": _contexto_workflow_por_esquema([aprobacion.esquema_id]).get(aprobacion.esquema_id),
        "participaciones": participaciones,
        "decididas": sum(1 for p in participaciones if p.decidida_en is not None),
        "puede_decidir": puede_aprobar(request.user, aprobacion),
        "contexto_prorroga": _contexto_prorroga(request.user, aprobacion.esquema),
        "entregable_revisado": _contexto_entregable_revisado(request.user, aprobacion),
    }
    return HttpResponse(render_to_string("aprobaciones/_vista_previa.html", contexto))


@login_required
def detalle_view(request, pk):
    aprobacion = get_object_or_404(
        Aprobacion.objects.select_related("esquema", "aprobador_usuario", "aprobador_equipo", "decidido_por"),
        pk=pk,
    )
    if not puede_consultar_aprobacion(request.user, aprobacion):
        raise PermissionDenied

    puede_decidir = puede_aprobar(request.user, aprobacion)
    puede_reasignar = puede_reasignar_aprobacion(request.user, aprobacion)
    contexto_workflow = _contexto_workflow_por_esquema([aprobacion.esquema_id]).get(aprobacion.esquema_id)
    contexto_prorroga = _contexto_prorroga(request.user, aprobacion.esquema)

    contexto = {
        "aprobacion": aprobacion,
        "esquema": aprobacion.esquema,
        "contexto_workflow": contexto_workflow,
        "contexto_prorroga": contexto_prorroga,
        "entregable_revisado": _contexto_entregable_revisado(request.user, aprobacion),
        "participaciones": aprobacion.esquema.participaciones.select_related(
            "aprobador_usuario", "aprobador_equipo", "decidido_por"
        ).all(),
        "reasignaciones": aprobacion.reasignaciones.select_related(
            "aprobador_anterior_usuario",
            "aprobador_anterior_equipo",
            "aprobador_nuevo_usuario",
            "aprobador_nuevo_equipo",
            "reasignado_por",
        ).all(),
        "puede_decidir": puede_decidir,
        "puede_reasignar": puede_reasignar,
        "decision_form": (
            DecisionAprobacionForm(permite_devolver=contexto_prorroga is None) if puede_decidir else None
        ),
        "reasignacion_form": ReasignacionAprobacionForm() if puede_reasignar else None,
        "titulo_pagina": f"Aprobación #{aprobacion.pk}",
    }
    return render(request, "aprobaciones/detalle.html", contexto)


@login_required
def decidir_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    aprobacion = get_object_or_404(Aprobacion.objects.select_related("esquema"), pk=pk)
    if not puede_aprobar(request.user, aprobacion):
        raise PermissionDenied
    es_prorroga = _prorroga_del_esquema(aprobacion.esquema) is not None
    form = DecisionAprobacionForm(request.POST, permite_devolver=not es_prorroga)
    if not form.is_valid():
        messages.error(request, "Revise los datos de la decisión.")
        return redirect("aprobaciones:detalle", pk=pk)
    try:
        if es_prorroga:
            # 4.A2: la decisión también aplica (o no) la nueva fecha del Ticket,
            # en una sola transacción — nunca `resolver_aprobacion` a secas.
            resolver_prorroga_por_aprobacion(
                aprobacion,
                request.user,
                decision=form.cleaned_data["decision"],
                observacion=form.cleaned_data.get("observacion", ""),
            )
        elif hasattr(aprobacion.esquema, "vinculo_workflow"):
            resolver_aprobacion_workflow(
                aprobacion,
                request.user,
                decision=form.cleaned_data["decision"],
                observacion=form.cleaned_data.get("observacion", ""),
            )
        else:
            resolver_aprobacion(
                aprobacion,
                request.user,
                decision=form.cleaned_data["decision"],
                observacion=form.cleaned_data.get("observacion", ""),
            )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Decisión registrada.")
    return redirect("aprobaciones:detalle", pk=pk)


@login_required
def reasignar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    aprobacion = get_object_or_404(Aprobacion, pk=pk)
    if not puede_reasignar_aprobacion(request.user, aprobacion):
        raise PermissionDenied
    form = ReasignacionAprobacionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revise los datos de la reasignación.")
        return redirect("aprobaciones:detalle", pk=pk)
    try:
        reasignar_aprobacion(
            aprobacion,
            request.user,
            nuevo_aprobador_usuario=form.cleaned_data["usuario"],
            nuevo_aprobador_equipo=form.cleaned_data["equipo"],
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Aprobación reasignada.")
    return redirect("aprobaciones:detalle", pk=pk)
