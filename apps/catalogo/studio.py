"""Studio (4.4) — superficie empresarial única para configurar un
Servicio/Proceso completo: General, Entrada, Ejecución, Salida y
Publicación.

Coordina dominios YA EXISTENTES (Servicio, Formulario/Campo/Opción/Regla,
Workflow vía `apps.catalogo.ejecucion`, DefinicionEntregable) — no crea
ningún modelo, ningún motor paralelo, ninguna segunda autorización. Cada
acción vuelve a invocar la operación de dominio real (que sigue siendo la
única autoridad); este módulo solo traduce HTTP ↔ esas operaciones y
decide qué mostrar en cada pestaña.

Autorización: `catalogo.administrar` es el gate base de todo el Studio.
La pestaña Entrada además exige `formulario.administrar`; la pestaña
Ejecución además exige `workflows.administrar` (mismos permisos que ya
gobiernan esos dominios — nada nuevo, nada hardcodeado por rol).

4.4.1 — Studio también crea el Servicio/Proceso (BORRADOR), concede
visibilidad (`ServicioVisibilidad`, en General junto a `alcance_visibilidad`:
una sola superficie) y configura responsables (`ServicioResponsable`, en
Publicación). Todo vía operaciones de `apps.catalogo.operaciones`, sobre los
modelos existentes.
"""

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.catalogo import evaluacion
from apps.catalogo import experiencia_proceso as proceso_ui
from apps.catalogo.entregables import configurar_definicion_entregable, retirar_definicion_entregable
from apps.catalogo.revision_elementos import configuracion_de as configuracion_de_conjunto
from apps.catalogo.revision_elementos import errores_de_version_de_columnas
from apps.catalogo.revision_elementos import version_de_columnas as version_de_columnas_parrilla
from apps.catalogo.tareas_elemento import (
    columnas_por_destino,
    configuracion_de as configuracion_tareas_de,
    configuracion_activa_de as configuracion_tareas_activa,
    configurar_tareas_elemento,
    desactivar_tareas_elemento,
    errores_de_version_para_tareas,
)
from apps.catalogo.tareas_elemento import DESTINOS as DESTINOS_TAREA
from apps.catalogo.terminos_busqueda import (
    MAX_TERMINOS_POR_SERVICIO,
    cambiar_estado_termino,
    crear_termino,
    editar_termino,
    eliminar_termino,
)
from apps.catalogo.configuracion_ejecucion import (
    activar_configuracion_ejecucion,
    agregar_bloque_operativo,
    conectar_bloques_operativos,
    editar_bloque_operativo,
    editar_transicion_bloque_operativo,
    eliminar_bloque_operativo,
    eliminar_transicion_bloque_operativo,
    preparar_configuracion_ejecucion,
    validar_configuracion_ejecucion,
)
from apps.catalogo.ejecucion import (
    TIPOS_BLOQUE,
    agregar_bloque,
    agregar_bloque_en_flujo,
    configurar_bloque,
    configurar_bloque_en_flujo,
    conectar_bloques,
    conectar_bloques_en_flujo,
    crear_flujo,
    crear_copia_de_ejecucion,
    desconectar_bloques,
    desconectar_bloques_en_flujo,
    editar_bloque,
    editar_bloque_en_flujo,
    eliminar_bloque,
    eliminar_bloque_en_flujo,
    plantillas_de_ejecucion,
    preparar_ejecucion,
    publicar_ejecucion,
    servicios_que_comparten,
    vincular_ejecucion,
)
from apps.catalogo.forms import (
    ActividadConfigForm,
    AprobacionConfigForm,
    BloqueEditarForm,
    BloqueGeneralForm,
    CampoForm,
    CierreAlcanceForm,
    DefinicionGacetaForm,
    GacetaBloqueForm,
    RecorridoConfigForm,
    ReglasFinalizacionForm,
    TIPO_BLOQUE_GACETA,
    TIPO_BLOQUE_GRUPO,
    TIPO_BLOQUE_RECORRIDO,
    TIPOS_CONTENIDOS_CHOICES,
    CondicionalForm,
    DefinicionEntregableForm,
    EntregableConfigForm,
    EsperaConfigForm,
    FallbackForm,
    FormularioForm,
    InicioProcesoForm,
    ParrillaForm,
    MAX_APROBADORES,
    OpcionCampoForm,
    ParticipanteAprobacionForm,
    ParticipanteAprobacionFormSet,
    PoliticaEntregaForm,
    PoliticaProrrogaForm,
    ReglaCondicionalForm,
    ResponsableForm,
    RutaAprobacionForm,
    ServicioCreacionForm,
    ServicioGeneralForm,
    TIPOS_BLOQUE_CHOICES,
    TIPO_BLOQUE_TAREAS_ELEMENTOS,
    TIPOS_BLOQUE_LEGACY_CHOICES,
    TIPOS_BLOQUE_DESCRIPCION,
    TerminoServicioForm,
    TiempoObjetivoForm,
    VisibilidadForm,
)
from apps.catalogo.models import (
    BloqueOperativo,
    Campo,
    ConfiguracionEjecucionVersion,
    DefinicionEntregable,
    FormularioVersion,
    OpcionCampo,
    ProgramacionProceso,
    ReglaCondicional,
    Servicio,
    ServicioResponsable,
    ServicioVisibilidad,
    TerminoServicio,
    TransicionBloqueOperativo,
)
from apps.catalogo.operaciones import (
    activar_servicio,
    agregar_responsable,
    asociar_formulario_nuevo,
    conceder_visibilidad,
    configurar_datos_inicio,
    configurar_politica_entrega,
    configurar_politica_prorroga,
    configurar_tiempo_objetivo,
    crear_categoria,
    crear_servicio,
    datos_inicio_configurados,
    editar_servicio_general,
    retirar_responsable,
    retirar_visibilidad,
)
from apps.catalogo.parrilla import (
    agregar_coleccion,
    configurar_parrilla,
    definicion_de,
    definiciones_de,
    desactivar_parrilla,
)
from apps.catalogo.programacion import (
    asegurar_responsable_inicial,
    programacion_activa_de,
    configurar_programacion,
    desactivar_programacion,
    errores_de_compatibilidad,
)
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.tickets.programadas import proxima_ejecucion
from apps.core.autorizacion import usuario_tiene_permiso
from apps.workflows.autorizacion import puede_administrar_workflows, puede_vincular_workflows
from apps.workflows.models import Etapa, FaseWorkflow, TransicionEtapa, Workflow, WorkflowVersion
from apps.workflows.variables import referencias_disponibles

# Orden de la dependencia natural (4.E2): el formulario, luego lo que el servicio ENTREGA (los
# bloques ENTREGABLE del flujo eligen entre esas definiciones) y por último el flujo.
TABS = ("general", "entrada", "salida", "ejecucion", "publicacion")
TAB_LABELS = {
    "general": "Básico",
    "entrada": "Formulario",
    "ejecucion": "Flujo",
    "salida": "Salida",
    "publicacion": "Publicar",
}
# Fase 4.A — estados visibles de cada sección (las claves CSS se conservan: complete/optional/attention).
ESTADO_SIN_CONFIGURAR, ESTADO_EN_PROGRESO = "empty", "progress"
ESTADO_COMPLETA, ESTADO_OPCIONAL, ESTADO_ATENCION = "complete", "optional", "attention"
ETIQUETAS_ESTADO = {
    ESTADO_SIN_CONFIGURAR: "Sin configurar", ESTADO_EN_PROGRESO: "En progreso", ESTADO_COMPLETA: "Completa",
    ESTADO_OPCIONAL: "Opcional", ESTADO_ATENCION: "Requiere atención",
}

ETIQUETAS_BLOQUE = {v: k for k, v in TIPOS_BLOQUE.items()}
ETIQUETAS_BLOQUE_DISPLAY = {
    "ACTIVIDAD": "Actividad", "ENTREGABLE": "Entregable", "APROBACION": "Aprobación", "ESPERA": "Espera",
    "DECISION": "Decisión", "TAREAS_ELEMENTOS": "Tareas por elemento",
    "GRUPO_ACTIVIDADES": "Actividades en paralelo", "RECORRIDO_ITEMS": "Recorrido por ítem", "GACETA": "Gaceta",
}
ETIQUETAS_RAMA = {"ITEM": "Para cada ítem", "GENERAL": "General, en paralelo"}


def _puede_catalogo(usuario):
    return usuario_tiene_permiso(usuario, "catalogo.administrar")


def _puede_formulario(usuario):
    return usuario_tiene_permiso(usuario, "formulario.administrar")


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _errores_de_form(form):
    """Fase 4.A — los errores de un formulario con el NOMBRE VISIBLE del campo que falla («Etiqueta: …»), en vez
    de un «Revise los datos» genérico."""
    partes = []
    for nombre, lista in form.errors.items():
        campo = form.fields.get(nombre)
        etiqueta = "" if nombre == "__all__" or campo is None else (campo.label or nombre.replace("_", " ").capitalize())
        partes.extend(f"{etiqueta}: {mensaje}" if etiqueta else mensaje for mensaje in lista)
    return "; ".join(partes)


def _volver(pk, tab):
    return f"{reverse('catalogo:studio', args=[pk])}?tab={tab}"


# Grupos del asistente de Básico, en orden (el JS solo navega entre los que aplican al registro).
GRUPOS_BASICO = ("identidad", "inicio", "responsables", "visibilidad", "tiempos")


# --- Traducción de errores técnicos (punto 36) ---------------------------

_PATRONES_IGNORAR = (
    "Debe existir exactamente una etapa INICIO",
    "Debe existir al menos una etapa FIN",
    "no debe tener transiciones entrantes",
    "no debe tener transiciones salientes",
)


def _traducir_error_ejecucion(mensaje):
    if any(patron in mensaje for patron in _PATRONES_IGNORAR):
        return None
    m = re.search(r"etapa APROBACION «([^»]+)».*resultado_aprobacion=(\w+) \(tiene 0\)", mensaje)
    if m:
        etiqueta = {"APROBADA": "aprobada", "RECHAZADA": "rechazada", "DEVUELTA": "devuelta"}.get(m.group(2), m.group(2).lower())
        return f"La aprobación «{m.group(1)}» no tiene definida una ruta para cuando es {etiqueta}."
    m = re.search(r"etapa APROBACION «([^»]+)» no tiene ConfiguracionEtapaAprobacion", mensaje)
    if m:
        return f"La aprobación «{m.group(1)}» no está configurada todavía."
    m = re.search(r"etapa APROBACION «([^»]+)» no tiene ningún participante", mensaje)
    if m:
        return f"La aprobación «{m.group(1)}» no tiene aprobadores asignados."
    m = re.search(r"etapas no son alcanzables desde INICIO: (.+)\.", mensaje)
    if m:
        return f"Hay bloques sin conectar al flujo: {m.group(1)}."
    if "Ninguna etapa FIN es alcanzable" in mensaje:
        return "El flujo no llega a su fin — revise las conexiones."
    m = re.search(r"etapa CONDICION «([^»]+)» debe tener exactamente una transición de", mensaje)
    if m:
        return f"La decisión «{m.group(1)}» no tiene definida una ruta «en otro caso»."
    m = re.search(r"etapa CONDICION «([^»]+)» debe tener al menos una transición condicional", mensaje)
    if m:
        return f"La decisión «{m.group(1)}» no tiene ninguna condición configurada."
    m = re.search(r"etapa «([^»]+)».*todavía no está disponible para activar", mensaje)
    if m:
        return f"El bloque «{m.group(1)}» usa una capacidad que todavía no está disponible."
    if "Se requiere un formulario con una versión ACTIVA propia" in mensaje:
        return "La entrada no tiene una versión activa — publique los cambios de Entrada primero."
    if "La ejecución no tiene una versión activa utilizable" in mensaje:
        return "La ejecución no tiene una versión activa — publique los cambios de Ejecución primero."
    # Degradación controlada: sin patrón conocido, se muestra el mensaje
    # original (preferible a ocultarlo) — ver informe de entrega, punto 17.
    return mensaje


# --- Listado (entrada administrativa) -------------------------------------


@login_required
def studio_lista_view(request):
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicios = Servicio.objects.select_related("categoria").order_by("nombre")
    contexto = {"servicios": servicios, "titulo_pagina": "Studio"}
    return render(request, "catalogo/studio_lista.html", contexto)


def _aplicar_inicio(servicio, actor, datos):
    """4.G1 — «¿Cómo inicia este proceso?» dentro del guardado del Proceso: Programado crea o
    actualiza la programación (y deja al responsable elegido como responsable del proceso); Manual
    la pausa. Se llama dentro de la misma transacción que crea o edita el proceso."""
    if datos["modo"] == InicioProcesoForm.PROGRAMADO:
        responsable = asegurar_responsable_inicial(servicio, actor, datos["responsable_inicial"])
        configurar_programacion(
            servicio, actor, dia_creacion=datos["dia_creacion"], periodo=datos["periodo"],
            responsable_inicial=responsable,
        )
    else:
        desactivar_programacion(servicio, actor)
    # Fase 2: en un proceso programado los datos de inicio siempre se completan en la ejecución; en uno
    # manual, según lo elegido.
    configurar_datos_inicio(
        servicio, actor,
        en_ejecucion=datos["modo"] == InicioProcesoForm.PROGRAMADO or bool(datos.get("datos_inicio_en_ejecucion")),
    )


@login_required
def studio_crear_view(request):
    """4.4.1, brecha 1 — alta de un Servicio/Proceso sin Django Admin. Crea solo el BORRADOR (sin
    Workflow/Formulario/Entregables) y redirige a su pestaña General para completarlo.

    4.G1: si el tipo es Proceso, el mismo formulario pregunta «¿Cómo inicia este proceso?» (Manual o
    Programado) y, en Programado, su calendario y responsable inicial. El JS solo muestra u oculta;
    el servidor valida."""
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = ServicioCreacionForm(request.POST)
        form_inicio = InicioProcesoForm(request.POST)
        es_proceso = request.POST.get("tipo") == Servicio.Tipo.PROCESO
        valido = form.is_valid()
        if es_proceso:
            valido = form_inicio.is_valid() and valido
        if valido:
            datos = form.cleaned_data
            try:
                with transaction.atomic():
                    categoria = datos["categoria"] or crear_categoria(request.user, nombre=datos["categoria_nueva"])
                    servicio = crear_servicio(
                        request.user, nombre=datos["nombre"], descripcion=datos.get("descripcion", ""),
                        categoria=categoria, tipo=datos["tipo"], instrucciones=datos.get("instrucciones", ""),
                    )
                    programado = es_proceso and form_inicio.cleaned_data["modo"] == InicioProcesoForm.PROGRAMADO
                    if programado:
                        _aplicar_inicio(servicio, request.user, form_inicio.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, _mensaje_error(exc))
            else:
                if programado:
                    messages.success(
                        request,
                        "Creado como borrador y programado. Completa su flujo y publícalo: Órbita empezará "
                        "a crear sus ejecuciones cuando esté publicado.",
                    )
                else:
                    messages.success(request, "Creado como borrador. Completa Básico paso a paso; nada es definitivo hasta publicar.")
                return redirect(_volver(servicio.pk, "general") + "&asistente=1")
    else:
        # Sin tipo preseleccionado: se elige con las tarjetas Servicio / Proceso.
        form = ServicioCreacionForm()
        form_inicio = InicioProcesoForm(
            initial={"modo": InicioProcesoForm.MANUAL, "frecuencia": "MENSUAL",
                     "periodo": ProgramacionProceso.Periodo.MES_SIGUIENTE}
        )
    contexto = {"form": form, "form_inicio": form_inicio, "titulo_pagina": "Crear en Studio"}
    return render(request, "catalogo/studio_crear.html", contexto)


# --- Helpers de composición de contexto -----------------------------------


# 4.G2B1 — el editor de campos del Formulario sirve a DOS usos independientes del mismo Servicio:
# la entrada inicial del ticket (`Servicio.formulario`) y las columnas de la estructura de elementos
# de un Proceso (`DefinicionParrilla.formulario`). `uso` decide cuál; nunca se mezclan.
USO_ENTRADA = "entrada"
USO_PARRILLA = "parrilla"


def _coleccion(request):
    """Fase 1 — clave de la colección de ítems que se está editando (`?coleccion=`); `None` = la
    colección por defecto del Proceso."""
    return request.POST.get("coleccion") or request.GET.get("coleccion") or None


def _formulario_de_uso(servicio, uso=USO_ENTRADA, coleccion=None):
    if uso == USO_PARRILLA:
        definicion = definicion_de(servicio, coleccion)
        return definicion.formulario if definicion is not None else None
    return servicio.formulario


def _volver_entrada(pk, uso=USO_ENTRADA, coleccion=None):
    destino = _volver(pk, "entrada")
    if uso != USO_PARRILLA:
        return destino
    return f"{destino}&uso={USO_PARRILLA}" + (f"&coleccion={coleccion}" if coleccion else "")


_URLS_EDITOR = {
    USO_ENTRADA: {
        "version": "catalogo:studio_entrada_version",
        "activar": "catalogo:studio_entrada_activar",
        "campo_crear": "catalogo:studio_campo_crear",
        "campo_editar": "catalogo:studio_campo_editar",
        "campo_eliminar": "catalogo:studio_campo_eliminar",
        "campo_mover": "catalogo:studio_campo_mover",
        "opcion_crear": "catalogo:studio_opcion_crear",
        "opcion_editar": "catalogo:studio_opcion_editar",
        "opcion_eliminar": "catalogo:studio_opcion_eliminar",
    },
    USO_PARRILLA: {
        "version": "catalogo:studio_parrilla_version",
        "activar": "catalogo:studio_parrilla_activar",
        "campo_crear": "catalogo:studio_parrilla_campo_crear",
        "campo_editar": "catalogo:studio_parrilla_campo_editar",
        "campo_eliminar": "catalogo:studio_parrilla_campo_eliminar",
        "campo_mover": "catalogo:studio_parrilla_campo_mover",
        "opcion_crear": "catalogo:studio_parrilla_opcion_crear",
        "opcion_editar": "catalogo:studio_parrilla_opcion_editar",
        "opcion_eliminar": "catalogo:studio_parrilla_opcion_eliminar",
    },
}


def _urls_editor(uso):
    """Nombres de URL del editor de campos para cada uso (el template los resuelve con `{% url %}`)."""
    return _URLS_EDITOR[uso]


def _version_entrada_editable(servicio, uso=USO_ENTRADA, coleccion=None):
    formulario = _formulario_de_uso(servicio, uso, coleccion)
    if formulario is None:
        return None
    return formulario.versiones.filter(estado=FormularioVersion.Estado.BORRADOR).order_by("-numero").first()


def _variables_para_decisiones(servicio, bloques=()):
    """Referencias que una DECISION de este servicio puede usar (4.B0): datos del ticket,
    los campos del formulario de entrada (borrador si lo hay, si no el activo) y el
    resultado de sus bloques de aprobación. Solo ayuda de configuración: no valida."""
    campos = []
    if servicio.formulario_id is not None:
        version = _version_entrada_editable(servicio) or servicio.formulario.version_activa
        if version is not None:
            campos = list(version.campos.all())
    referencias = referencias_disponibles(campos=campos, bloques=bloques)
    # Fase 2: dentro del recorrido de un ítem, una decisión puede usar las columnas del ítem.
    referencias += [
        (referencia, f"Valor del ítem: {etiqueta}")
        for referencia, etiqueta in _opciones_bloques(servicio)["columnas_condicion"]
    ]
    return referencias


def _version_ejecucion_editable(servicio):
    if servicio.workflow_id is None:
        return None
    if servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        return None
    return servicio.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()


def _configuracion_ejecucion_editable(servicio):
    if servicio.workflow_id is None or servicio.workflow.modo != Workflow.Modo.PLANTILLA_FASES:
        return None
    return servicio.configuraciones_ejecucion.filter(
        estado=ConfiguracionEjecucionVersion.Estado.BORRADOR
    ).order_by("-numero").first()


def _resumen_actor(tipo, usuario, equipo, columna=""):
    if tipo == "COLUMNA":
        return f"Columna «{columna}» del ítem" if columna else "Columna del ítem (sin elegir)"
    if tipo == "USUARIO" and usuario:
        return str(usuario)
    if tipo == "EQUIPO" and equipo:
        return str(equipo)
    if tipo == "RESPONSABLE_TICKET":
        return "Responsable actual del Ticket"
    if tipo == "SOLICITANTE":
        return "Solicitante"
    return "Sin asignar"


def _transicion_principal(etapa):
    """Transición que continúa el camino principal desde `etapa`:
    APROBADA en una APROBACION, el fallback («en otro caso») en una DECISION,
    la única salida en el resto. Devuelve `(transicion | None, ambiguo)`:
    `ambiguo` es True si un bloque lineal tiene varias salidas (no se adivina
    cuál es la principal)."""
    salientes = list(etapa.transiciones_salientes.all())
    if etapa.tipo == Etapa.Tipo.APROBACION:
        candidatas = [t for t in salientes if t.resultado_aprobacion == "APROBADA"]
    elif etapa.tipo == Etapa.Tipo.CONDICION:
        candidatas = [t for t in salientes if t.es_fallback]
    else:
        candidatas = salientes
    if len(candidatas) == 1:
        return candidatas[0], False
    return None, len(candidatas) > 1


def _camino_principal(version):
    """Recorre el camino principal desde INICIO (solo lectura, no reordena ni
    escribe nada). Devuelve `(nodos, estado, ultimo, conexion)`:

    - `nodos`: etapas visitadas en orden (incluye INICIO y, si se alcanza, FIN).
    - `estado`: «FIN» (el camino llega a FIN), «SIN_SALIDA» (termina en un
      bloque sin salida principal definida), «AMBIGUO» (un bloque lineal con
      varias salidas), «CICLO» (la ruta principal vuelve a un bloque ya
      visitado) o «SIN_INICIO».
    - `ultimo`: último bloque visitado antes de terminar; `conexion`: su
      transición principal (hacia FIN o hacia el bloque que cierra el ciclo).
    """
    etapas = {e.pk: e for e in version.etapas.prefetch_related("transiciones_salientes__etapa_destino")}
    actual = next((e for e in etapas.values() if e.tipo == Etapa.Tipo.INICIO), None)
    if actual is None:
        return [], "SIN_INICIO", None, None
    nodos, visitados = [], set()
    while True:
        visitados.add(actual.pk)
        nodos.append(actual)
        transicion, ambiguo = _transicion_principal(actual)
        if transicion is None:
            return nodos, ("AMBIGUO" if ambiguo else "SIN_SALIDA"), actual, None
        destino = etapas.get(transicion.etapa_destino_id)
        if destino is None:
            return nodos, "SIN_SALIDA", actual, None
        if destino.tipo == Etapa.Tipo.FIN:
            nodos.append(destino)
            return nodos, "FIN", actual, transicion
        if destino.pk in visitados:
            return nodos, "CICLO", actual, transicion
        actual = destino


def _orden_lineal(version):
    """Camino principal desde INICIO (vertical, sin canvas — punto 23). Solo
    orden de presentación; los bloques fuera del camino principal (ramas,
    sin conectar) se agregan al final por antigüedad."""
    nodos, _estado, _ultimo, _conexion = _camino_principal(version)
    orden = [e for e in nodos if e.tipo not in (Etapa.Tipo.INICIO, Etapa.Tipo.FIN)]
    visitados = {e.pk for e in nodos}
    huerfanas = [
        e for e in version.etapas.all()
        if e.pk not in visitados and e.tipo not in (Etapa.Tipo.INICIO, Etapa.Tipo.FIN)
    ]
    orden.extend(sorted(huerfanas, key=lambda e: e.pk))
    return orden


def _destinos_choices(version, *, excluir_pk=None):
    etapas = version.etapas.exclude(tipo=Etapa.Tipo.INICIO)
    if excluir_pk is not None:
        etapas = etapas.exclude(pk=excluir_pk)
    return [(e.pk, "Fin" if e.tipo == Etapa.Tipo.FIN else e.nombre) for e in etapas]


def _bloques_para_presentacion(version):
    resultado = []
    for etapa in _orden_lineal(version):
        tipo_empresarial = ETIQUETAS_BLOQUE.get(etapa.tipo, etapa.tipo)
        fila = {
            "etapa": etapa,
            "tipo_empresarial": tipo_empresarial,
            "tipo_empresarial_display": ETIQUETAS_BLOQUE_DISPLAY.get(tipo_empresarial, tipo_empresarial),
            "resumen": "",
            "puede_agregar_despues": _plan_despues_de(etapa) is not None,
            "form_editar": BloqueEditarForm(
                initial={"nombre": etapa.nombre, "descripcion": etapa.descripcion}, prefix=f"bloque-{etapa.pk}-editar"
            ),
        }
        if etapa.tipo == Etapa.Tipo.TAREA:
            config = getattr(etapa, "configuracion_tarea", None)
            fila["resumen"] = (
                _resumen_actor(config.tipo_responsable, config.usuario_responsable, config.equipo_responsable)
                if config is not None
                else "Sin configurar"
            )
            fila["form_configurar"] = ActividadConfigForm(
                initial=(
                    {
                        "tipo_actor": config.tipo_responsable,
                        "usuario": config.usuario_responsable,
                        "equipo": config.equipo_responsable,
                        "permite_subtareas": config.permite_subtareas,
                    }
                    if config is not None
                    else {}
                ),
                prefix=f"bloque-{etapa.pk}-config",
            )
        elif etapa.tipo == Etapa.Tipo.APROBACION:
            config = getattr(etapa, "configuracion_aprobacion", None)
            participantes = list(config.participantes.order_by("orden")) if config is not None else []
            fila["resumen"] = ", ".join(
                _resumen_actor(p.tipo_aprobador, p.usuario, p.equipo) for p in participantes
            ) or "Sin participantes"
            fila["form_configurar"] = AprobacionConfigForm(
                initial={"modo": config.modo, "politica": config.politica} if config is not None else {},
                prefix=f"bloque-{etapa.pk}-config",
            )
            fila["formset_participantes"] = ParticipanteAprobacionFormSet(
                initial=[{"tipo": p.tipo_aprobador, "usuario": p.usuario, "equipo": p.equipo} for p in participantes]
                or None,
                prefix=f"bloque-{etapa.pk}-participantes",
            )
            destinos = _destinos_choices(version, excluir_pk=etapa.pk)
            rutas = {t.resultado_aprobacion: t for t in etapa.transiciones_salientes.all()}
            fila["rutas"] = rutas
            fila["form_ruta"] = RutaAprobacionForm(
                initial={
                    "destino_aprobada": rutas["APROBADA"].etapa_destino_id if "APROBADA" in rutas else None,
                    "destino_devuelta": rutas["DEVUELTA"].etapa_destino_id if "DEVUELTA" in rutas else "",
                    "destino_rechazada": rutas["RECHAZADA"].etapa_destino_id if "RECHAZADA" in rutas else None,
                },
                destinos=destinos,
                prefix=f"bloque-{etapa.pk}-ruta",
            )
        elif etapa.tipo == Etapa.Tipo.ESPERA:
            cfg = etapa.configuracion or {}
            if cfg.get("modo") == "DURACION":
                fila["resumen"] = f"{cfg.get('duracion_valor')} {(cfg.get('duracion_unidad') or '').lower()}"
            elif cfg.get("modo") == "FECHA":
                fila["resumen"] = f"Hasta {cfg.get('fecha_objetivo')}"
            else:
                fila["resumen"] = "Sin configurar"
            fila["form_configurar"] = EsperaConfigForm(initial=cfg, prefix=f"bloque-{etapa.pk}-config")
        elif etapa.tipo == Etapa.Tipo.CONDICION:
            salientes = list(etapa.transiciones_salientes.all())
            condicionales = [t for t in salientes if not t.es_fallback]
            fallback = next((t for t in salientes if t.es_fallback), None)
            destinos = _destinos_choices(version, excluir_pk=etapa.pk)
            fila["condicionales"] = [
                {
                    "transicion": t,
                    "form": CondicionalForm(
                        initial={
                            "variable": t.variable, "operador": t.operador, "valor": t.valor,
                            "prioridad": t.prioridad, "destino": t.etapa_destino_id,
                        },
                        destinos=destinos,
                        prefix=f"bloque-{etapa.pk}-cond-{t.pk}",
                    ),
                }
                for t in condicionales
            ]
            fila["form_condicional_nuevo"] = CondicionalForm(destinos=destinos, prefix=f"bloque-{etapa.pk}-cond-nuevo")
            fila["fallback"] = fallback
            fila["form_fallback"] = FallbackForm(
                initial={"destino": fallback.etapa_destino_id} if fallback is not None else {},
                destinos=destinos,
                prefix=f"bloque-{etapa.pk}-fallback",
            )
        resultado.append(fila)
    return resultado


# --- Inserción de bloques (4.4.2) -----------------------------------------
#
# Mientras la versión sea BORRADOR el flujo SIEMPRE es editable: que exista
# FIN, un camino completo INICIO→…→FIN o una validación estructural correcta
# nunca bloquea agregar. Hasta 4.4.1 un "gate" (`_puede_insertar_bloque`)
# exigía que el último bloque tuviera una salida directa a FIN y, si no,
# ocultaba el alta: una Aprobación/Decisión recién creada (sin salidas por
# diseño), o cualquier bloque que quedara sin salida tras eliminar otro, dejaba
# el flujo "cerrado". Ahora el punto de inserción se calcula de forma
# determinista, o no se conecta nada si sería adivinar.

_CAMPOS_REGLA = ("nombre", "prioridad", "variable", "operador", "valor", "es_fallback", "resultado_aprobacion")


def _reglas_de(transicion):
    # `editar_transicion` es reemplazo completo: al redirigir solo el destino
    # hay que reenviar las reglas existentes (resultado, condición, fallback).
    return {campo: getattr(transicion, campo) for campo in _CAMPOS_REGLA}


def _reglas_salida_nueva(origen):
    """Reglas de la PRIMERA salida de `origen` cuando aún no tiene ruta
    principal: la ruta «aprobada» de una aprobación, «en otro caso» de una
    decisión, o una salida simple en un bloque lineal."""
    if origen.tipo == Etapa.Tipo.APROBACION:
        return {"resultado_aprobacion": "APROBADA"}
    if origen.tipo == Etapa.Tipo.CONDICION:
        return {"es_fallback": True}
    return {}


def _fin_unico(version):
    fines = list(version.etapas.filter(tipo=Etapa.Tipo.FIN)[:2])
    return fines[0] if len(fines) == 1 else None


def _plan(anterior, conexion, destino, reglas):
    return {"anterior": anterior, "conexion": conexion, "destino": destino, "reglas": reglas}


def _plan_al_final(version):
    """Dónde insertar un bloque nuevo «al final» del camino principal — debe
    calcularse ANTES de crearlo (con el bloque ya creado pero sin conectar,
    aparecería como huérfano y se confundiría consigo mismo como "el
    anterior"). `None` si el final es ambiguo (varias salidas en un bloque
    lineal, o la ruta principal forma un ciclo): entonces no se adivina."""
    _nodos, estado, ultimo, conexion = _camino_principal(version)
    if estado == "FIN":
        return _plan(ultimo, conexion, conexion.etapa_destino, _reglas_de(conexion))
    if estado == "SIN_SALIDA":
        return _plan(ultimo, None, _fin_unico(version), _reglas_salida_nueva(ultimo))
    return None


def _plan_despues_de(etapa):
    """«+ Agregar después» de `etapa`: solo para bloques lineales (ACTIVIDAD,
    ESPERA) con una única salida (A→B se vuelve A→C→B) o ninguna. Si el
    bloque se bifurca (varias salidas, aprobada/devuelta/rechazada,
    condiciones, fallback) devuelve `None`: no se adivina dónde insertar."""
    if etapa.tipo not in (Etapa.Tipo.TAREA, Etapa.Tipo.ESPERA):
        return None
    salientes = list(etapa.transiciones_salientes.select_related("etapa_destino"))
    if len(salientes) == 1:
        conexion = salientes[0]
        return _plan(etapa, conexion, conexion.etapa_destino, _reglas_de(conexion))
    if not salientes:
        return _plan(etapa, None, _fin_unico(etapa.version), {})
    return None


class _AnclaServicio:
    """Dónde se edita el flujo: la pestaña Ejecución de un Servicio/Proceso
    (Studio). Los handlers de bloques (`_h_*`) no saben de dónde vienen: llaman
    a esta ancla, que delega en las operaciones de dominio correspondientes."""

    es_flujo = False

    def __init__(self, servicio):
        self.servicio = servicio
        self.pk = servicio.pk

    def version_editable(self):
        return _version_ejecucion_editable(self.servicio)

    def volver(self):
        return redirect(_volver(self.pk, "ejecucion"))

    def agregar(self, version, actor, **kw):
        return agregar_bloque(self.servicio, version, actor, **kw)

    def configurar(self, version, bloque, actor, **kw):
        return configurar_bloque(self.servicio, version, bloque, actor, **kw)

    def editar(self, version, bloque, actor, **kw):
        return editar_bloque(self.servicio, version, bloque, actor, **kw)

    def conectar(self, version, origen, destino, actor, **kw):
        return conectar_bloques(self.servicio, version, origen, destino, actor, **kw)

    def desconectar(self, version, conexion, actor):
        return desconectar_bloques(self.servicio, version, conexion, actor)

    def eliminar(self, version, bloque, actor):
        return eliminar_bloque(self.servicio, version, bloque, actor)


class _AnclaFlujo:
    """Dónde se edita el flujo: el propio Flujo (Diseñador › Flujos), sin
    Servicio. Mismos handlers, mismas reglas; otro permiso y otra pantalla."""

    es_flujo = True

    def __init__(self, workflow):
        self.workflow = workflow
        self.pk = workflow.pk

    def version_editable(self):
        return self.workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()

    def volver(self):
        return redirect("flujos:lienzo", pk=self.pk)

    def agregar(self, version, actor, **kw):
        return agregar_bloque_en_flujo(self.workflow, version, actor, **kw)

    def configurar(self, version, bloque, actor, **kw):
        return configurar_bloque_en_flujo(self.workflow, version, bloque, actor, **kw)

    def editar(self, version, bloque, actor, **kw):
        return editar_bloque_en_flujo(self.workflow, version, bloque, actor, **kw)

    def conectar(self, version, origen, destino, actor, **kw):
        return conectar_bloques_en_flujo(self.workflow, version, origen, destino, actor, **kw)

    def desconectar(self, version, conexion, actor):
        return desconectar_bloques_en_flujo(self.workflow, version, conexion, actor)

    def eliminar(self, version, bloque, actor):
        return eliminar_bloque_en_flujo(self.workflow, version, bloque, actor)


def _conectar_bloque_nuevo(ancla, version, nuevo_bloque, actor, tipo_empresarial, plan):
    """Enlaza `nuevo_bloque` según `plan`: `anterior → nuevo` (redirigiendo la
    conexión existente, si la hay) y `nuevo → destino`.

    La salida del bloque nuevo hacia `destino` se crea automáticamente para
    ACTIVIDAD/ESPERA (salida única, sin campos obligatorios). Una
    APROBACION/DECISION nueva no recibe una transición genérica: sus salidas
    exigen resultado o condición. Solo cuando `destino` es un bloque real (no
    FIN) se le conecta su ruta principal («aprobada» / «en otro caso») para
    que ese bloque no quede huérfano al insertar en medio; hacia FIN no se
    crea nada y el usuario define las rutas."""
    anterior, conexion, destino = plan["anterior"], plan["conexion"], plan["destino"]
    if conexion is not None:
        ancla.conectar(version, anterior, nuevo_bloque, actor, conexion=conexion, **plan["reglas"])
    else:
        ancla.conectar(version, anterior, nuevo_bloque, actor, **plan["reglas"])
    if destino is None:
        return
    if tipo_empresarial in ("ACTIVIDAD", "ESPERA"):
        ancla.conectar(version, nuevo_bloque, destino, actor)
    elif destino.tipo != Etapa.Tipo.FIN:
        ancla.conectar(version, nuevo_bloque, destino, actor, **_reglas_salida_nueva(nuevo_bloque))


def _filas_aprobadores(prefijo, participantes=None, columnas_actor=(), sin_solicitante=False):
    """Una fila por aprobador posible (hasta `MAX_APROBADORES`), sin enlazarlas a un formset:
    el navegador muestra tantas como diga «¿Cuántos aprobadores participan?» y el servidor solo
    lee esas. `participantes`: dicts `{tipo, usuario, equipo, columna}` ya configurados (o None).
    Fase 2: `columnas_actor` habilita «Persona o equipo de una columna del ítem»."""
    participantes = participantes or []
    return [
        ParticipanteAprobacionForm(
            prefix=f"{prefijo}-{indice}", initial=participantes[indice] if indice < len(participantes) else None,
            columnas_actor=columnas_actor, sin_solicitante=sin_solicitante,
        )
        for indice in range(MAX_APROBADORES)
    ]


def _leer_aprobadores(request, prefijo, cantidad, errores, columnas_actor=()):
    """Los `cantidad` aprobadores enviados como `[(tipo, referencia)]`, o `None` (con el motivo
    en `errores`) si alguno está incompleto. Las filas más allá de `cantidad` se ignoran. Para un
    aprobador por columna (Fase 2) la referencia es la clave de la columna."""
    participantes = []
    for indice in range(cantidad):
        fila = ParticipanteAprobacionForm(request.POST, prefix=f"{prefijo}-{indice}", columnas_actor=columnas_actor)
        if not fila.is_valid():
            motivo = "; ".join(m for lista in fila.errors.values() for m in lista)
            errores.append(f"Aprobador {indice + 1}: {motivo or 'indica quién aprueba.'}")
            return None
        tipo_p = fila.cleaned_data["tipo"]
        referencia = (
            fila.cleaned_data.get("usuario") if tipo_p == "USUARIO"
            else fila.cleaned_data.get("equipo") if tipo_p == "EQUIPO"
            else fila.cleaned_data.get("columna") if tipo_p == "COLUMNA"
            else None
        )
        participantes.append((tipo_p, referencia))
    return participantes


# --- Fase 2: opciones de configuración de bloques (colecciones, columnas y datos con fecha) --------------


def _opciones_bloques(servicio):
    """Lo que los formularios de bloques ofrecen para elegir sin escribir claves: colecciones del proceso,
    columnas de persona/equipo (responsable por ítem), columnas para la condición de ingreso y datos con
    fecha (campos del formulario de inicio y columnas de los ítems). Se calcula una vez por página."""
    from apps.catalogo.configuracion_ejecucion import _columnas_de_coleccion
    from apps.catalogo.models import DefinicionParrilla

    opciones = {
        "colecciones": [], "columnas_actor": [], "columnas_condicion": [], "referencias_fecha": [], "gacetas": [],
        "sin_solicitante": False, "columnas_editables": [],
    }
    if servicio is None:
        return opciones
    # Fase 4.A: un proceso programado no tiene solicitante: no se ofrece como responsable ni aprobador.
    opciones["sin_solicitante"] = programacion_activa_de(servicio) is not None
    from apps.catalogo.models import DefinicionGaceta

    opciones["gacetas"] = [
        (d.clave, d.nombre) for d in DefinicionGaceta.objects.filter(servicio=servicio, activa=True).order_by("orden", "pk")
    ]
    if servicio.formulario_id is not None:
        version = _version_entrada_editable(servicio) or servicio.formulario.version_activa
        for campo in (version.campos.order_by("orden", "pk") if version is not None else []):
            if campo.clave and campo.tipo in ("FECHA", "FECHA_HORA"):
                opciones["referencias_fecha"].append((f"formulario.{campo.clave}", f"Formulario · {campo.etiqueta}"))
    if servicio.tipo != Servicio.Tipo.PROCESO:
        return opciones
    actores = {}
    editables = {}
    for definicion in DefinicionParrilla.objects.filter(servicio=servicio).order_by("orden", "pk"):
        nombre = definicion.nombre_elemento or definicion.clave
        opciones["colecciones"].append((definicion.clave, nombre))
        columnas = sorted(_columnas_de_coleccion(servicio, definicion.clave).values(), key=lambda c: (c.orden, c.pk))
        for campo in columnas:
            # Bloque 3.1: cualquier columna (también ARCHIVO) puede quedar editable por una tarea.
            editables.setdefault(campo.clave, []).append(f"{nombre} · {campo.etiqueta}")
            if campo.tipo == "ARCHIVO":
                continue
            opciones["columnas_condicion"].append((f"item.{campo.clave}", f"{nombre} · {campo.etiqueta}"))
            if campo.tipo in ("USUARIO", "EQUIPO"):
                actores.setdefault(campo.clave, []).append(f"{nombre} · {campo.etiqueta}")
            if campo.tipo in ("FECHA", "FECHA_HORA"):
                opciones["referencias_fecha"].append((f"item.{campo.clave}", f"Ítem: {nombre} · {campo.etiqueta}"))
    opciones["columnas_actor"] = [(clave, " / ".join(etiquetas)) for clave, etiquetas in actores.items()]
    opciones["columnas_editables"] = [(clave, " / ".join(etiquetas)) for clave, etiquetas in editables.items()]
    return opciones


def _form_actividad(datos=None, *, prefix, opciones, initial=None):
    if datos is None:
        initial = {"modo_fechas": "SIN_FECHA", **(initial or {})}
    return ActividadConfigForm(
        datos, prefix=prefix, initial=initial, columnas_actor=opciones["columnas_actor"],
        referencias_fecha=opciones["referencias_fecha"], columnas_editables=opciones.get("columnas_editables", ()),
        # Solo al presentar: al guardar, la regla la aplica el dominio con su mensaje (validación de publicación).
        sin_solicitante=datos is None and opciones.get("sin_solicitante", False),
    )


def _leer_cierre_de_alcance(request, prefix, opciones):
    """`["clave", …]` de las colecciones cuyo alcance se cierra al llegar al bloque (vacío si ninguna)."""
    if not opciones["colecciones"]:
        return []
    form = CierreAlcanceForm(request.POST, prefix=prefix, colecciones=opciones["colecciones"])
    return list(form.cleaned_data.get("cierra_alcance") or []) if form.is_valid() else []


def _parsear_configuracion_bloque(
    request, tipo, *, bloque_id=None, servicio=None, incluir_pk=None, version=None, errores=None
):
    errores = errores if errores is not None else []
    prefix = f"bloque-{bloque_id}-config" if bloque_id else "nuevo-config"
    opciones = _opciones_bloques(servicio)
    if tipo == "RECORRIDO_ITEMS":
        form = RecorridoConfigForm(
            request.POST, prefix=prefix, colecciones=opciones["colecciones"], columnas=opciones["columnas_condicion"]
        )
        if not form.is_valid():
            errores.extend(m for lista in form.errors.values() for m in lista)
            return {}, False
        return {"recorrido": form.configuracion()}, True
    if tipo == "GRUPO_ACTIVIDADES":
        return {}, True
    if tipo == "GACETA":
        form = GacetaBloqueForm(request.POST, prefix=prefix, gacetas=opciones["gacetas"])
        if not form.is_valid():
            errores.extend(m for lista in form.errors.values() for m in lista)
            return {}, False
        return {"gaceta_cfg": form.configuracion()}, True
    if tipo == "ENTREGABLE":
        # Solo un bloque de configuración por fases tiene un Servicio del cual elegir entregables.
        if servicio is None:
            return {}, False
        form = EntregableConfigForm(request.POST, servicio=servicio, incluir_pk=incluir_pk, prefix=prefix)
        if not form.is_valid():
            errores.append(_errores_de_form(form))
            return {}, False
        return {"definicion": form.cleaned_data["definicion"]}, True
    if tipo == "ACTIVIDAD":
        form = _form_actividad(request.POST, prefix=prefix, opciones=opciones if version is not None else _opciones_bloques(None))
        if not form.is_valid():
            errores.append(_errores_de_form(form))
            return {}, False
        datos = form.cleaned_data
        configuracion = {
            "tipo_actor": datos.get("tipo_actor") or "",
            "usuario": datos.get("usuario"),
            "equipo": datos.get("equipo"),
            "permite_subtareas": datos.get("permite_subtareas", False),
        }
        if configuracion["tipo_actor"] == "COLUMNA":
            configuracion["columna"] = datos.get("columna") or ""
        if version is not None:
            # Fase 1: fechas, evidencia y lista de verificación. Solo la configuración por fases las
            # admite; el camino LEGACY (`ejecucion._configurar_actividad`) no conoce estas claves.
            configuracion["ampliacion"] = form.ampliacion()
        return configuracion, True
    if tipo == "ESPERA":
        form = EsperaConfigForm(request.POST, prefix=prefix)
        if not form.is_valid():
            errores.append(_errores_de_form(form))
            return {}, False
        datos = form.cleaned_data
        kwargs = {"modo": datos["modo"]}
        if datos["modo"] == "DURACION":
            kwargs["duracion_valor"] = datos["duracion_valor"]
            kwargs["duracion_unidad"] = datos["duracion_unidad"]
        else:
            kwargs["fecha_objetivo"] = datos["fecha_objetivo"].isoformat()
        return kwargs, True
    if tipo == "APROBACION":
        prefix_participantes = f"bloque-{bloque_id}-participantes" if bloque_id else "nuevo-participantes"
        revisables = _bloques_entregable_revisables(version)
        form = AprobacionConfigForm(request.POST, prefix=prefix, bloques_entregable=revisables)
        if not form.is_valid():
            errores.extend(m for lista in form.errors.values() for m in lista)
            return {}, False
        cantidad = form.cleaned_data.get("cantidad")
        if cantidad is not None:
            # 4.F1: «¿Cuántos aprobadores?» manda; los aprobadores son lo único que se guarda.
            participantes = _leer_aprobadores(
                request, prefix_participantes, cantidad, errores,
                columnas_actor=opciones["columnas_actor"] if version is not None else (),
            )
            if participantes is None:
                return {}, False
        else:
            formset = ParticipanteAprobacionFormSet(request.POST, prefix=prefix_participantes)
            if not formset.is_valid():
                return {}, False
            participantes = []
            for datos_p in formset.cleaned_data:
                if not datos_p or datos_p.get("DELETE"):
                    continue
                tipo_p = datos_p["tipo"]
                referencia = datos_p.get("usuario") if tipo_p == "USUARIO" else (datos_p.get("equipo") if tipo_p == "EQUIPO" else None)
                participantes.append((tipo_p, referencia))
        if not participantes:
            errores.append("Indica al menos un aprobador.")
            return {}, False
        configuracion = {
            "modo": form.cleaned_data["modo"],
            "politica": form.cleaned_data.get("politica") or None,
            "participantes": participantes,
        }
        if version is not None:
            # `None` = aprobación general. Solo la configuración por fases (con `version`) lo
            # admite: el camino LEGACY (`ejecucion._configurar_aprobacion`) no conoce la clave.
            revisa = form.cleaned_data.get("revisa")
            configuracion["entregable_revisado"] = (
                next((b for b in revisables if str(b.pk) == str(revisa)), None) if revisa else None
            )
            # Fase 2 — decisiones permitidas (solo la configuración por fases).
            configuracion["permite_exclusion"] = bool(form.cleaned_data.get("permite_exclusion"))
            configuracion["justificacion_al_aprobar"] = bool(form.cleaned_data.get("justificacion_al_aprobar"))
        return configuracion, True
    if tipo in ("DECISION", "TAREAS_ELEMENTOS"):
        return {}, True  # TAREAS_ELEMENTOS no tiene más configuración: vive en la del proceso
    return {}, False


def _concesiones_visibilidad(servicio):
    return list(
        servicio.visibilidad.filter(activo=True).select_related("usuario", "area", "unidad_negocio").order_by("pk")
    )


def _contexto_terminos(servicio):
    """4.D — "Términos de búsqueda" (Básico). El Servicio interno del Ticket General
    no participa en la búsqueda, así que no los ofrece."""
    return {
        "terminos_aplican": not servicio.es_ticket_general,
        "terminos": [
            {
                "obj": termino,
                "form": TerminoServicioForm(
                    initial={"termino": termino.termino}, prefix=f"termino-{termino.pk}-editar"
                ),
            }
            for termino in servicio.terminos_busqueda.all()
        ],
        "form_termino_nuevo": TerminoServicioForm(prefix="termino-nuevo"),
        "max_terminos": MAX_TERMINOS_POR_SERVICIO,
    }


def _contexto_inicio(servicio):
    """4.G1 — «¿Cómo inicia este proceso?»: manual o programado, y cómo va la programación. La
    sección se arma siempre (salvo el Servicio interno del Ticket General) porque el selector de
    Tipo puede convertir un Servicio en Proceso sin recargar; el JS la muestra solo con Proceso."""
    if servicio.es_ticket_general:
        return {"inicio_aplica": False}
    programacion = ProgramacionProceso.objects.filter(servicio=servicio).select_related(
        "responsable_inicial__usuario", "responsable_inicial__equipo"
    ).first()
    activa = programacion is not None and programacion.activa
    inicial = {
        "modo": InicioProcesoForm.PROGRAMADO if activa else InicioProcesoForm.MANUAL,
        "datos_inicio_en_ejecucion": servicio.datos_inicio_en_ejecucion,
    }
    if programacion is not None:
        inicial.update(
            frecuencia=programacion.frecuencia, dia_creacion=programacion.dia_creacion,
            periodo=programacion.periodo,
            responsable_inicial=InicioProcesoForm.valor_responsable(programacion.responsable_inicial),
        )
    else:
        inicial.update(frecuencia="MENSUAL", periodo=ProgramacionProceso.Periodo.MES_SIGUIENTE)
    proxima = None
    if activa:
        creacion, periodo, vencida = proxima_ejecucion(programacion, timezone.localdate())
        proxima = {"fecha": creacion, "periodo": periodo, "vencida": vencida}
    ejecuciones = servicio.ejecuciones_programadas.select_related("ticket")[:5]
    return {
        "inicio_aplica": True,
        "form_inicio": InicioProcesoForm(initial=inicial),
        "programacion": programacion,
        "programacion_activa": activa,
        "proxima_ejecucion": proxima,
        "ejecuciones_programadas": list(ejecuciones),
        # Solo informativo: lo que habría que corregir para poder programarlo.
        "inicio_incompatibilidades": (
            errores_de_compatibilidad(servicio)
            if not activa and servicio.tipo == Servicio.Tipo.PROCESO else []
        ),
    }


def _contexto_responsables(servicio):
    programacion = programacion_activa_de(servicio) if servicio.tipo == Servicio.Tipo.PROCESO else None
    return {
        "responsables": list(
            servicio.responsables.filter(activo=True).select_related("usuario", "equipo").order_by("pk")
        ),
        "form_responsable": ResponsableForm(),
        # Bloque 4.1: el responsable inicial de una programación activa no se retira desde aquí.
        "responsable_inicial_id": programacion.responsable_inicial_id if programacion is not None else None,
    }


FORMULARIO_GENERAL = "form-general-servicio"


def _asociar(form, form_id=FORMULARIO_GENERAL):
    """Fase 4.B — los campos de Básico están repartidos en grupos de la navegación secundaria: se asocian al MISMO
    formulario con el atributo `form` (una sola operación de guardado)."""
    for campo in form.fields.values():
        campo.widget.attrs["form"] = form_id
    return form


def _contexto_general(servicio, ev=None):
    contexto_inicio = _contexto_inicio(servicio)
    if contexto_inicio.get("form_inicio") is not None:
        _asociar(contexto_inicio["form_inicio"])
    from apps.catalogo import estado_basico

    apartados = ev.apartados_basico if ev is not None else estado_basico.apartados(servicio)
    estado, texto = estado_basico.agregado(apartados)
    return {
        **contexto_inicio,
        # Estado de cada apartado (navegación secundaria): solo datos guardados, nunca «visitado».
        "apartados_basico": {a.clave: a for a in apartados},
        "basico_resumen": {"estado": estado, "etiqueta": ETIQUETAS_ESTADO[estado], "texto": texto},
        "form_general": _asociar(ServicioGeneralForm(instance=servicio)),
        "form_prorroga": PoliticaProrrogaForm(
            initial={
                "politica": servicio.politica_prorroga,
                "aprobador_usuario": servicio.prorroga_aprobador_usuario_id,
                "aprobador_equipo": servicio.prorroga_aprobador_equipo_id,
            }
        ),
        "form_tiempo": TiempoObjetivoForm(
            initial={
                "cantidad": servicio.tiempo_objetivo_cantidad,
                "unidad": servicio.tiempo_objetivo_unidad,
                "habiles": servicio.tiempo_objetivo_habiles,
            }
        ),
        "concesiones": _concesiones_visibilidad(servicio),
        "form_visibilidad": VisibilidadForm(),
        "es_restringido": servicio.alcance_visibilidad == Servicio.AlcanceVisibilidad.RESTRINGIDO,
        # 4.G2E: en un Proceso los responsables se configuran en Básico (quién lo atiende es parte de su
        # identidad); en un Servicio siguen en Publicar.
        # Fase 4.A: los responsables generales se configuran en Básico también en un Servicio (Publicar los resume).
        **_contexto_responsables(servicio),
        **_contexto_terminos(servicio),
    }


# Tipos que una columna de colección de ítems no admite. Vacío desde la Fase 1 del modelo configurable
# (ARCHIVO se guarda como `Adjunto` de la rama ELEMENTO); se conserva el punto único por si otro tipo
# deja de ser compatible.
TIPOS_NO_ADMITIDOS_EN_ELEMENTOS = frozenset()


# Fase 2 — tipos que solo tienen sentido en una columna de ítems (el responsable por equipo de un
# recorrido); el formulario de inicio no los ofrece ni los acepta.
TIPOS_SOLO_EN_ELEMENTOS = frozenset({"EQUIPO"})


def _sin_tipos_no_soportados(form):
    form.fields["tipo"].choices = [
        (valor, etiqueta) for valor, etiqueta in form.fields["tipo"].choices
        if valor not in TIPOS_NO_ADMITIDOS_EN_ELEMENTOS
    ]
    return form


def _sin_tipos_de_elementos(form):
    form.fields["tipo"].choices = [
        (valor, etiqueta) for valor, etiqueta in form.fields["tipo"].choices
        if valor not in TIPOS_SOLO_EN_ELEMENTOS
    ]
    return form


def _form_campo_nuevo(es_parrilla):
    form = CampoForm(prefix="campo-nuevo")
    return _sin_tipos_no_soportados(form) if es_parrilla else _sin_tipos_de_elementos(form)


def _contexto_tareas_elemento(servicio):
    """4.G2D1 — lo que necesita el panel «Tareas por elemento»: la configuración vigente y, por cada dato de
    la tarea, las columnas de la estructura que pueden darlo."""
    configuracion = configuracion_tareas_de(servicio)
    disponibles = columnas_por_destino(servicio)
    destinos = [
        {
            "atributo": atributo, "nombre": nombre, "obligatoria": obligatoria, "columnas": disponibles[atributo],
            "actual": getattr(configuracion, atributo) if configuracion is not None else "",
        }
        for atributo, nombre, _, obligatoria in DESTINOS_TAREA
    ]
    # Bloque 3.1: columnas que quien realiza la tarea puede editar en su elemento.
    version = version_de_columnas_parrilla(servicio)
    marcadas = set(configuracion.columnas_editables or []) if configuracion is not None else set()
    editables = [
        {"clave": c.clave, "etiqueta": c.etiqueta, "marcada": c.clave in marcadas}
        for c in (version.campos.order_by("orden", "pk") if version is not None else [])
    ]
    return {"tareas_config": configuracion, "tareas_destinos": destinos, "tareas_editables": editables}


# Icono lineal de cada tipo de campo/columna en la lista del constructor (solo presentación).
ICONOS_TIPO_CAMPO = {
    "TEXTO": "type", "TEXTO_LARGO": "type", "CORREO": "type", "URL": "type", "NUMERO": "hash",
    "FECHA": "calendar", "FECHA_HORA": "calendar-clock", "LISTA": "list", "MULTILISTA": "list",
    "BOOLEANO": "check-circle", "ARCHIVO": "file", "USUARIO": "user", "EQUIPO": "users", "AREA": "layers",
    "UNIDAD": "layers",
}


def _contexto_participantes(definicion):
    """Bloque 3 — participantes configurados de la colección y las opciones (personas y equipos activos)."""
    from django.contrib.auth import get_user_model

    from apps.catalogo.parrilla import participantes_de
    from apps.core.models import Equipo

    usuarios, equipos = participantes_de(definicion)
    return {
        "participantes_usuarios": [u.pk for u in usuarios],
        "participantes_equipos": [e.pk for e in equipos],
        "participantes_opciones_usuarios": get_user_model().objects.filter(is_active=True).order_by(
            "first_name", "last_name", "username"
        ),
        "participantes_opciones_equipos": Equipo.objects.filter(activo=True).order_by("nombre"),
    }


def _contexto_entrada(servicio, uso=USO_ENTRADA, coleccion=None, ev=None):
    es_proceso = servicio.tipo == Servicio.Tipo.PROCESO and not servicio.es_ticket_general
    if uso == USO_PARRILLA and not es_proceso:
        uso = USO_ENTRADA
    es_parrilla = uso == USO_PARRILLA
    # Fase 1: varias colecciones. Una clave desconocida cae en la colección por defecto.
    colecciones = definiciones_de(servicio) if es_proceso else []
    if coleccion is not None and not any(c.clave == coleccion for c in colecciones):
        coleccion = None
    definicion = definicion_de(servicio, coleccion) if es_proceso else None
    es_por_defecto = definicion is None or not colecciones or definicion.pk == colecciones[0].pk
    if es_por_defecto:
        coleccion = None
    formulario = _formulario_de_uso(servicio, uso, coleccion)
    version_activa = formulario.version_activa if formulario else None
    version_borrador = _version_entrada_editable(servicio, uso, coleccion)
    version_mostrada = version_borrador or version_activa
    campos, reglas = [], []
    if version_mostrada is not None:
        campos = list(version_mostrada.campos.prefetch_related("opciones").order_by("orden", "id"))
        reglas = [] if es_parrilla else list(
            ReglaCondicional.objects.filter(campo_origen__version=version_mostrada).select_related(
                "campo_origen", "campo_objetivo"
            )
        )
        # Fase 4.A: las reglas también se editan (antes solo se podían eliminar).
        for regla in reglas:
            regla.form_editar = ReglaCondicionalForm(
                instance=regla, campos_queryset=Campo.objects.filter(version=version_mostrada),
                prefix=f"regla-{regla.pk}-editar",
            )
        for campo in campos:
            campo.icono_tipo = ICONOS_TIPO_CAMPO.get(campo.tipo, "file")
            campo.form_editar = CampoForm(instance=campo, prefix=f"campo-{campo.pk}-editar")
            if es_parrilla:
                _sin_tipos_no_soportados(campo.form_editar)
            elif campo.tipo not in TIPOS_SOLO_EN_ELEMENTOS:
                _sin_tipos_de_elementos(campo.form_editar)
            campo.form_opcion_nueva = OpcionCampoForm(prefix=f"campo-{campo.pk}-opcion-nueva")
            for opcion in campo.opciones.all():
                opcion.form_editar = OpcionCampoForm(instance=opcion, prefix=f"campo-{campo.pk}-opcion-{opcion.pk}")
    if es_proceso:
        # 4.G2E: para qué se usa cada columna (tarea que originan, revisión) y el resumen de las dos piezas.
        usos = proceso_ui.usos_de_columnas(servicio) if es_parrilla else {}
        for campo in campos:
            campo.usos = usos.get(campo.clave, [])
    return {
        "uso": uso,
        "es_parrilla": es_parrilla,
        "es_proceso": es_proceso,
        "tarjetas_proceso": proceso_ui.tarjetas_formulario(servicio, ev) if es_proceso else None,
        "definicion": definicion,
        "colecciones": colecciones,
        "coleccion_por_defecto": es_por_defecto,
        # Se añade a las acciones del editor para que actúen sobre la colección que se está viendo.
        "sufijo_coleccion": f"?coleccion={coleccion}" if es_parrilla and coleccion else "",
        "form_coleccion_nueva": ParrillaForm(prefix="coleccion-nueva") if es_parrilla and colecciones else None,
        # Las tareas por elemento solo existen para la colección por defecto (4.G2D1).
        **(
            _contexto_tareas_elemento(servicio)
            if es_parrilla and es_por_defecto and definicion is not None and definicion.activa
            else {}
        ),
        **(_contexto_participantes(definicion) if es_parrilla and definicion is not None and definicion.activa else {}),
        "form_parrilla": (
            ParrillaForm(initial={
                "nombre_elemento": definicion.nombre_elemento if definicion else "Elemento",
                "permite_altas_extraordinarias": bool(definicion and definicion.permite_altas_extraordinarias),
            })
            if es_parrilla else None
        ),
        "urls_editor": _urls_editor(uso),
        "formulario": formulario,
        "version_entrada_activa": version_activa,
        "version_entrada_borrador": version_borrador,
        "version_entrada": version_mostrada,
        "editable_entrada": version_borrador is not None,
        "campos": campos,
        "reglas": reglas,
        "formulario_form": FormularioForm(initial={"nombre": servicio.nombre}) if formulario is None else None,
        # Un Proceso sin datos de inicio (sin formulario, o solo el técnico vacío de `asegurar_datos_inicio`) se
        # presenta como «no pide datos de inicio», nunca como un formulario configurado.
        "datos_inicio_vacios": es_proceso and not es_parrilla and not datos_inicio_configurados(servicio),
        "campo_form_nuevo": _form_campo_nuevo(es_parrilla) if version_borrador is not None else None,
        "regla_form_nueva": (
            ReglaCondicionalForm(campos_queryset=Campo.objects.filter(version=version_mostrada), prefix="regla-nueva")
            if version_borrador is not None and campos and not es_parrilla
            else None
        ),
    }


def _contexto_bloques(version_borrador, version_mostrada, despues=None):
    """Lo que necesita la lista de bloques y su alta, sea cual sea el ancla
    (Servicio en Studio, Flujo en el Diseñador). `version_borrador` es la
    versión que se puede EDITAR (None si es de solo lectura)."""
    bloques = _bloques_para_presentacion(version_mostrada) if version_mostrada is not None else []
    despues_de = None
    aviso_agregar = ""
    if version_borrador is not None:
        # «+ Agregar después» (?despues=<id>): solo si ese bloque admite una
        # inserción segura; cualquier otro valor se ignora y se agrega al final.
        try:
            candidato = next((f["etapa"] for f in bloques if f["etapa"].pk == int(despues)), None)
        except (TypeError, ValueError):
            candidato = None
        if candidato is not None and _plan_despues_de(candidato) is not None:
            despues_de = candidato
        elif _plan_al_final(version_borrador) is None:
            aviso_agregar = (
                "El flujo no tiene un final único (hay un ciclo o un bloque con varias salidas): "
                "el bloque nuevo se agregará sin conectar y podrás enlazarlo desde las rutas de otros bloques."
            )
    return {
        "bloques": bloques,
        "despues_de": despues_de,
        "aviso_agregar": aviso_agregar,
        "bloques_disponibles": [
            (codigo, etiqueta, TIPOS_BLOQUE_DESCRIPCION[codigo]) for codigo, etiqueta in TIPOS_BLOQUE_LEGACY_CHOICES
        ],
        "bloque_general_form": BloqueGeneralForm(prefix="nuevo") if version_borrador is not None else None,
        "form_actividad_nuevo": ActividadConfigForm(prefix="nuevo-config"),
        "form_aprobacion_nuevo": AprobacionConfigForm(prefix="nuevo-config"),
        "formset_participantes_nuevo": ParticipanteAprobacionFormSet(prefix="nuevo-participantes"),
    }


def _fases_para_resumen(version):
    if version is None:
        return []
    return list(version.fases.order_by("orden", "pk"))


def _serializar_configuracion_bloque_operativo(tipo, config_kwargs):
    if tipo == "ACTIVIDAD":
        usuario = config_kwargs.get("usuario")
        equipo = config_kwargs.get("equipo")
        serializada = {
            "tipo_actor": config_kwargs.get("tipo_actor") or "",
            "usuario_id": usuario.pk if usuario is not None else None,
            "equipo_id": equipo.pk if equipo is not None else None,
            "permite_subtareas": config_kwargs.get("permite_subtareas", False),
            **(config_kwargs.get("ampliacion") or {}),
        }
        if config_kwargs.get("columna"):
            serializada["columna"] = config_kwargs["columna"]
        return serializada
    if tipo == "RECORRIDO_ITEMS":
        return dict(config_kwargs.get("recorrido") or {})
    if tipo == "GACETA":
        return dict(config_kwargs.get("gaceta_cfg") or {})
    if tipo == "ESPERA":
        return dict(config_kwargs)
    if tipo == "ENTREGABLE":
        return {}  # la referencia vive en `BloqueOperativo.definicion_entregable`, no en el JSON
    if tipo == "APROBACION":
        participantes = []
        for tipo_p, referencia in config_kwargs.get("participantes", []):
            participante = {
                "tipo": tipo_p,
                "usuario_id": referencia.pk if tipo_p == "USUARIO" and referencia is not None else None,
                "equipo_id": referencia.pk if tipo_p == "EQUIPO" and referencia is not None else None,
            }
            if tipo_p == "COLUMNA":
                participante["columna"] = referencia or ""
            participantes.append(participante)
        serializada = {
            "modo": config_kwargs.get("modo"),
            "politica": config_kwargs.get("politica") or "",
            "participantes": participantes,
        }
        for opcion in ("permite_exclusion", "justificacion_al_aprobar"):
            if config_kwargs.get(opcion):
                serializada[opcion] = True
        return serializada
    return {}


def _inicial_actividad_ampliada(cfg):
    from apps.catalogo.actividades import normalizar

    datos, _errores = normalizar(cfg)
    previsto, limite = datos["plazo_previsto"] or {}, datos["plazo_limite"] or {}
    return {
        "requiere_evidencia": datos["requiere_evidencia"],
        "plazo_habiles": datos["plazo_habiles"],
        "plazo_previsto_cantidad": previsto.get("cantidad"),
        "plazo_previsto_unidad": previsto.get("unidad") or "DIAS",
        "plazo_limite_cantidad": limite.get("cantidad"),
        "plazo_limite_unidad": limite.get("unidad") or "DIAS",
        "verificacion_obligatoria": "\n".join(i["texto"] for i in datos["verificacion"] if i["obligatorio"]),
        "verificacion_opcional": "\n".join(i["texto"] for i in datos["verificacion"] if not i["obligatorio"]),
        "fecha_prevista_campo": datos["fecha_prevista_campo"],
        "fecha_limite_campo": datos["fecha_limite_campo"],
        "exige_datos_inicio": datos["exige_datos_inicio"],
        "columnas_editables": datos["columnas_editables"],
    }


def _config_inicial_para_formulario(bloque):
    cfg = bloque.configuracion or {}
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        inicial = {
            "tipo_actor": cfg.get("tipo_actor") or "",
            "usuario": cfg.get("usuario_id"),
            "equipo": cfg.get("equipo_id"),
            "columna": cfg.get("columna") or "",
            "permite_subtareas": cfg.get("permite_subtareas", False),
            **_inicial_actividad_ampliada(cfg),
        }
        inicial["modo_fechas"] = ActividadConfigForm.modo_fechas_de(inicial)
        return inicial
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        return {
            "modo": cfg.get("modo"), "politica": cfg.get("politica") or "",
            "permite_exclusion": bool(cfg.get("permite_exclusion")),
            "justificacion_al_aprobar": bool(cfg.get("justificacion_al_aprobar")),
        }
    if bloque.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS:
        condicion = cfg.get("condicion_ingreso") or {}
        return {
            "coleccion": cfg.get("coleccion") or "",
            "condicion_variable": condicion.get("variable") or "",
            "condicion_operador": condicion.get("operador") or "",
            "condicion_valor": condicion.get("valor") or "",
            "incorporar_nuevos": bool(cfg.get("incorporar_nuevos")),
            "espera_alcance_cerrado": bool(cfg.get("espera_alcance_cerrado")),
        }
    return cfg


def _participantes_iniciales_operativos(bloque):
    participantes = []
    for item in (bloque.configuracion or {}).get("participantes", []):
        participantes.append(
            {
                "tipo": item.get("tipo"),
                "usuario": item.get("usuario_id"),
                "equipo": item.get("equipo_id"),
                "columna": item.get("columna") or "",
            }
        )
    return participantes or None


DESTINO_FINALIZAR = "FIN"


def _destinos_bloque_choices(version_config, *, excluir_pk=None, contenedor_id=None, rama=""):
    """Bloques a los que puede ir una ruta, más «Finalizar el flujo» (4.E2): un destino especial,
    no un bloque (el vocabulario V1 no incluye un tipo FIN). Fase 2: una ruta solo conecta bloques del mismo
    ámbito: los del flujo principal entre sí, o los de la misma rama de un recorrido (donde «finalizar»
    termina el recorrido de ESE ítem)."""
    bloques = (
        version_config.bloques.select_related("fase").filter(contenedor_id=contenedor_id, rama=rama or "")
        .order_by("fase__orden", "orden", "pk")
    )
    if excluir_pk is not None:
        bloques = bloques.exclude(pk=excluir_pk)
    opciones = [(bloque.pk, f"{bloque.fase.nombre} - {bloque.nombre}") for bloque in bloques]
    fin = "Terminar el recorrido del ítem" if contenedor_id is not None else "Finalizar el flujo"
    return opciones + [(DESTINO_FINALIZAR, fin)]


def _destinos_de(bloque):
    return _destinos_bloque_choices(
        bloque.version, excluir_pk=bloque.pk, contenedor_id=bloque.contenedor_id, rama=bloque.rama
    )


def _destino_inicial(transicion):
    return DESTINO_FINALIZAR if transicion.finaliza else transicion.bloque_destino_id


def _resolver_destino_operativo(version, valor):
    """`(bloque | None, finaliza)` a partir de lo elegido en un selector de destino."""
    if valor == DESTINO_FINALIZAR:
        return None, True
    return get_object_or_404(BloqueOperativo, pk=valor, version=version), False


def _bloques_entregable_revisables(version_config):
    """Bloques ENTREGABLE de la configuración que una aprobación puede revisar: con una
    definición de contenido revisable (texto, enlace o archivo)."""
    if version_config is None:
        return []
    return list(
        version_config.bloques.filter(
            tipo=BloqueOperativo.Tipo.ENTREGABLE,
            definicion_entregable__tipo__in=BloqueOperativo.TIPOS_ENTREGABLE_REVISABLES,
        )
        .select_related("fase", "definicion_entregable")
        .order_by("fase__orden", "orden", "pk")
    )


def _resumen_bloque_operativo(bloque):
    cfg = bloque.configuracion or {}
    resumen = _resumen_bloque_base(bloque, cfg)
    claves = cfg.get("cierra_alcance") or []
    if claves:
        resumen += f" · cierra el alcance de {', '.join(claves)}"
    return resumen


def _resumen_bloque_base(bloque, cfg):
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        return _resumen_actor(cfg.get("tipo_actor"), None, None, cfg.get("columna") or "")
    if bloque.tipo == BloqueOperativo.Tipo.GRUPO_ACTIVIDADES:
        return f"{bloque.contenidos.count()} actividad(es) en paralelo"
    if bloque.tipo == BloqueOperativo.Tipo.GACETA:
        espera = "espera su resolución" if cfg.get("esperar", "RESOLUCION") == "RESOLUCION" else "sin esperar"
        return f"Gaceta «{cfg.get('gaceta') or 'sin elegir'}» · {espera}"
    if bloque.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS:
        texto = f"Recorre «{cfg.get('coleccion') or 'sin colección'}»"
        if cfg.get("condicion_ingreso"):
            texto += " · con condición de ingreso"
        if cfg.get("espera_alcance_cerrado"):
            texto += " · espera el cierre del alcance"
        return texto
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        resumen = f"{len(cfg.get('participantes') or [])} aprobador(es)"
        if bloque.entregable_revisado_id:
            resumen += f" · revisa «{bloque.entregable_revisado.nombre}»"
        return resumen
    if bloque.tipo == BloqueOperativo.Tipo.ESPERA:
        if cfg.get("modo") == "DURACION":
            return f"{cfg.get('duracion_valor')} {(cfg.get('duracion_unidad') or '').lower()}"
        if cfg.get("modo") == "FECHA":
            return f"Hasta {cfg.get('fecha_objetivo')}"
    if bloque.tipo == BloqueOperativo.Tipo.ENTREGABLE:
        definicion = bloque.definicion_entregable
        return f"Entregable: {definicion.nombre}" if definicion is not None else "Sin entregable"
    if bloque.tipo == BloqueOperativo.Tipo.DECISION:
        return "Rutas condicionales"
    if bloque.tipo == BloqueOperativo.Tipo.TAREAS_ELEMENTOS:
        return "Una tarea por elemento del ticket"
    return "Sin configurar"


def _ramas_de(contenedor):
    """Fase 2 — qué se puede agregar dentro de un contenedor: `[(rama, etiqueta, [(tipo, etiqueta)])]`."""
    ramas = ["ITEM", "GENERAL"] if contenedor.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS else ["GENERAL"]
    return [(rama, ETIQUETAS_RAMA[rama], TIPOS_CONTENIDOS_CHOICES[rama]) for rama in ramas]


def _bloque_operativo_para_presentacion(bloque, editable, bloques_entregable=(), opciones=None):
    if opciones is None:
        opciones = _opciones_bloques(bloque.version.servicio)
    fila = {
        "bloque": bloque,
        "tipo_empresarial": bloque.tipo,
        "tipo_empresarial_display": ETIQUETAS_BLOQUE_DISPLAY.get(bloque.tipo, bloque.tipo),
        "resumen": _resumen_bloque_operativo(bloque),
        "es_contenedor": bloque.tipo in BloqueOperativo.TIPOS_CONTENEDOR,
        "rama_display": ETIQUETAS_RAMA.get(bloque.rama, ""),
        "en_recorrido": bloque.rama == BloqueOperativo.Rama.ITEM,
        "rama_general": bloque.rama == BloqueOperativo.Rama.GENERAL,
    }
    if fila["es_contenedor"]:
        # Fase 2: los bloques contenidos se muestran (y editan) dentro de su contenedor, por rama.
        fila["contenidos"] = [
            _bloque_operativo_para_presentacion(contenido, editable, bloques_entregable, opciones)
            for contenido in bloque.contenidos.select_related("entregable_revisado").order_by("rama", "orden", "pk")
        ]
        fila["ramas"] = _ramas_de(bloque)
        if editable:
            # Fase 4.A: dentro de un contenedor también se agrega por intención («¿Qué necesitas que suceda?»).
            from apps.catalogo import intenciones

            servicio = bloque.version.servicio
            fila["ramas_intenciones"] = [
                (rama, etiqueta, intenciones.opciones_para(servicio, ambito=rama, configuracion=bloque.version))
                for rama, etiqueta, _tipos in fila["ramas"]
            ]
        if editable and bloque.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS:
            destinos_item = _destinos_bloque_choices(bloque.version, contenedor_id=bloque.pk, rama="ITEM")
            fila["form_decision_item"] = CondicionalForm(destinos=destinos_item, prefix="nuevo-cond")
            fila["form_decision_item_otro"] = FallbackForm(destinos=destinos_item, prefix="nuevo-fallback")
            fila["form_ruta_item"] = RutaAprobacionForm(
                destinos=destinos_item, en_recorrido=True, permite_vacios=True, prefix="nuevo-ruta",
            )
    if not editable:
        return fila
    fila["form_editar"] = BloqueEditarForm(
        initial={"nombre": bloque.nombre, "descripcion": bloque.descripcion},
        prefix=f"bloque-{bloque.pk}-editar",
    )
    if opciones["colecciones"]:
        fila["form_cierre"] = CierreAlcanceForm(
            initial={"cierra_alcance": (bloque.configuracion or {}).get("cierra_alcance") or []},
            colecciones=opciones["colecciones"], prefix=f"bloque-{bloque.pk}-config",
        )
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        fila["form_configurar"] = _form_actividad(
            initial=_config_inicial_para_formulario(bloque), prefix=f"bloque-{bloque.pk}-config", opciones=opciones
        )
    elif bloque.tipo == BloqueOperativo.Tipo.GACETA:
        cfg = bloque.configuracion or {}
        fila["form_configurar"] = GacetaBloqueForm(
            initial={
                "gaceta": cfg.get("gaceta"), "presentar_items": cfg.get("presentar_items", True),
                "esperar": cfg.get("esperar", "RESOLUCION"),
            },
            prefix=f"bloque-{bloque.pk}-config", gacetas=opciones["gacetas"],
        )
    elif bloque.tipo == BloqueOperativo.Tipo.RECORRIDO_ITEMS:
        fila["form_configurar"] = RecorridoConfigForm(
            initial=_config_inicial_para_formulario(bloque), prefix=f"bloque-{bloque.pk}-config",
            colecciones=opciones["colecciones"], columnas=opciones["columnas_condicion"],
        )
    elif bloque.tipo == BloqueOperativo.Tipo.ENTREGABLE:
        fila["form_configurar"] = EntregableConfigForm(
            initial={"definicion": bloque.definicion_entregable_id},
            servicio=bloque.version.servicio, incluir_pk=bloque.definicion_entregable_id,
            prefix=f"bloque-{bloque.pk}-config",
        )
    elif bloque.tipo == BloqueOperativo.Tipo.ESPERA:
        fila["form_configurar"] = EsperaConfigForm(
            initial=_config_inicial_para_formulario(bloque), prefix=f"bloque-{bloque.pk}-config"
        )
    elif bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        participantes_actuales = len((bloque.configuracion or {}).get("participantes") or [])
        fila["form_configurar"] = AprobacionConfigForm(
            initial={
                **_config_inicial_para_formulario(bloque),
                "revisa": bloque.entregable_revisado_id or "",
                "cantidad": max(participantes_actuales, 1),
            },
            prefix=f"bloque-{bloque.pk}-config",
            bloques_entregable=bloques_entregable,
        )
        iniciales = _participantes_iniciales_operativos(bloque) or []
        fila["filas_aprobadores"] = _filas_aprobadores(
            f"bloque-{bloque.pk}-participantes", iniciales, columnas_actor=opciones["columnas_actor"],
            sin_solicitante=opciones.get("sin_solicitante", False),
        )
        fila["cantidad_aprobadores"] = max(len(iniciales), 1)
        destinos = _destinos_de(bloque)
        rutas = {t.resultado_aprobacion: t for t in bloque.transiciones_salientes.all()}
        fila["rutas"] = rutas
        if not fila["rama_general"]:
            # Una rama general es solo esta aprobación: no tiene rutas.
            fila["form_ruta"] = RutaAprobacionForm(
                initial={
                    "destino_aprobada": _destino_inicial(rutas["APROBADA"]) if "APROBADA" in rutas else None,
                    "destino_devuelta": _destino_inicial(rutas["DEVUELTA"]) if "DEVUELTA" in rutas else "",
                    "destino_rechazada": _destino_inicial(rutas["RECHAZADA"]) if "RECHAZADA" in rutas else None,
                },
                destinos=destinos,
                en_recorrido=fila["en_recorrido"],
                prefix=f"bloque-{bloque.pk}-ruta",
            )
    elif bloque.tipo == BloqueOperativo.Tipo.DECISION:
        salientes = list(bloque.transiciones_salientes.all())
        condicionales = [t for t in salientes if not t.es_fallback]
        fallback = next((t for t in salientes if t.es_fallback), None)
        destinos = _destinos_de(bloque)
        fila["condicionales"] = [
            {
                "transicion": transicion,
                "form": CondicionalForm(
                    initial={
                        "variable": transicion.variable,
                        "operador": transicion.operador,
                        "valor": transicion.valor,
                        "prioridad": transicion.prioridad,
                        "destino": _destino_inicial(transicion),
                    },
                    destinos=destinos,
                    prefix=f"bloque-{bloque.pk}-cond-{transicion.pk}",
                ),
            }
            for transicion in condicionales
        ]
        fila["form_condicional_nuevo"] = CondicionalForm(
            destinos=destinos, prefix=f"bloque-{bloque.pk}-cond-nuevo"
        )
        fila["fallback"] = fallback
        fila["form_fallback"] = FallbackForm(
            initial={"destino": _destino_inicial(fallback)} if fallback is not None else {},
            destinos=destinos,
            prefix=f"bloque-{bloque.pk}-fallback",
        )
    return fila


def _contexto_configuracion_operativa(servicio):
    workflow = servicio.workflow
    version_activa = workflow.version_activa if workflow else None
    config_borrador = _configuracion_ejecucion_editable(servicio)
    config_activa = servicio.configuracion_ejecucion_activa
    config_mostrada = config_borrador or config_activa
    # Fase 4.A: si la configuración está sobre una versión anterior del flujo, se muestran SUS fases (con sus
    # bloques, nada desaparece de la vista) y no se edita hasta actualizarla (panel «El flujo cambió»).
    from apps.catalogo import reconciliacion as _reconciliacion

    pendiente_reconciliar = _reconciliacion.requiere_reconciliacion(servicio)
    editable = config_borrador is not None and not pendiente_reconciliar
    errores_configuracion = validar_configuracion_ejecucion(config_borrador) if config_borrador is not None else []
    bloques_entregable = _bloques_entregable_revisables(config_mostrada)
    destinos_decision = _destinos_bloque_choices(config_mostrada) if config_mostrada is not None else []
    fases = []
    opciones_fase = _opciones_bloques(servicio)
    version_de_fases = config_mostrada.workflow_version if pendiente_reconciliar else version_activa
    for fase in _fases_para_resumen(version_de_fases):
        bloques = []
        if config_mostrada is not None:
            bloques = [
                _bloque_operativo_para_presentacion(bloque, editable, bloques_entregable, opciones_fase)
                # Fase 2: los bloques contenidos se presentan dentro de su contenedor.
                for bloque in config_mostrada.bloques.filter(fase=fase, contenedor__isnull=True)
                .select_related("entregable_revisado")
                .order_by("orden", "pk")
            ]
        fases.append({"fase": fase, "bloques": bloques})
    opciones = _opciones_bloques(servicio)
    disponibles = [*TIPOS_BLOQUE_CHOICES]
    if configuracion_tareas_activa(servicio):
        disponibles.append(TIPO_BLOQUE_TAREAS_ELEMENTOS)
    disponibles.append(TIPO_BLOQUE_GRUPO)
    if opciones["colecciones"]:
        # Fase 2: un recorrido por ítem solo tiene sentido si el proceso tiene colecciones de ítems.
        disponibles.append(TIPO_BLOQUE_RECORRIDO)
    if opciones["gacetas"]:
        # Fase 3: el bloque GACETA solo se ofrece si el proceso ya tiene gacetas activas.
        disponibles.append(TIPO_BLOQUE_GACETA)
    from apps.catalogo import dependencias, intenciones, lienzo, reconciliacion

    analisis = reconciliacion.analizar(servicio)
    # Fase 4.B — lienzo: tarjetas y conexiones reales (flujo_api) e inspector con el editor de CADA actividad
    # (incluidas las de dentro de un contenedor).
    filas_inspector = []

    def _aplanar(filas):
        for fila in filas:
            filas_inspector.append(fila)
            _aplanar(fila.get("contenidos") or [])

    for item in fases:
        _aplanar(item["bloques"])
    datos_lienzo = (
        lienzo.datos(servicio, config_mostrada, errores=errores_configuracion, resumen_de=_resumen_bloque_operativo)
        if config_mostrada is not None else None
    )
    return {
        "lienzo": datos_lienzo,
        "pendientes_flujo": len(errores_configuracion) + len(dependencias.detectar(servicio, config_mostrada)),
        "lienzo_json": lienzo.json_para_dibujar(datos_lienzo) if datos_lienzo else None,
        "filas_inspector": filas_inspector,
        # Fase 4.A — «¿Qué necesitas que suceda?» (capa de intención) y lo que la configuración necesita.
        "intenciones_principal": (
            intenciones.opciones_para(servicio, configuracion=config_mostrada) if editable else []
        ),
        "reconciliacion": analisis,
        "fases_vigentes": list(version_activa.fases.order_by("orden", "pk")) if version_activa else [],
        "dependencias": dependencias.detectar(servicio, config_mostrada),
        "form_actividad_informacion": _form_actividad(
            prefix="nuevo-config", opciones=opciones, initial={"requiere_evidencia": True},
        ),
        "form_entregable_resultado": EntregableConfigForm(servicio=servicio, prefix="nuevo-config", revision=False),
        "form_entregable_revision": EntregableConfigForm(servicio=servicio, prefix="nuevo-config", revision=True),
        "form_ruta_nueva": (
            RutaAprobacionForm(destinos=destinos_decision, permite_vacios=True, prefix="nuevo-ruta")
            if editable else None
        ),
        "recorrido_proceso": (
            proceso_ui.recorrido_flujo(servicio, config_mostrada, _resumen_bloque_operativo)
            if proceso_ui.aplica(servicio) else []
        ),
        "usa_plantilla_fases": True,
        "version_ejecucion_activa": version_activa,
        "version_ejecucion_borrador": None,
        "version_ejecucion": version_activa,
        "configuracion_ejecucion_activa": config_activa,
        "configuracion_ejecucion_borrador": config_borrador,
        "configuracion_ejecucion": config_mostrada,
        "errores_configuracion_ejecucion": errores_configuracion,
        "configuracion_ejecucion_publicable": editable and not errores_configuracion,
        "editable_ejecucion": editable,
        "fases_configuracion": fases,
        "bloques_disponibles": [
            # 4.G2D2: «Tareas por elemento» solo se ofrece si el proceso ya las configuró.
            (codigo, etiqueta, TIPOS_BLOQUE_DESCRIPCION[codigo]) for codigo, etiqueta in disponibles
        ],
        "opciones_bloques": opciones,
        "form_gaceta_bloque_nuevo": GacetaBloqueForm(prefix="nuevo-config", gacetas=opciones["gacetas"]),
        "form_recorrido_nuevo": RecorridoConfigForm(
            prefix="nuevo-config", colecciones=opciones["colecciones"], columnas=opciones["columnas_condicion"]
        ),
        "form_cierre_nuevo": (
            CierreAlcanceForm(prefix="nuevo-config", colecciones=opciones["colecciones"])
            if opciones["colecciones"] else None
        ),
        "variables_decision": _variables_para_decisiones(
            servicio, config_mostrada.bloques.all() if config_mostrada is not None else ()
        ),
        "bloque_general_form": BloqueGeneralForm(prefix="nuevo") if editable else None,
        "form_actividad_nuevo": _form_actividad(prefix="nuevo-config", opciones=opciones),
        "form_entregable_nuevo": EntregableConfigForm(servicio=servicio, prefix="nuevo-config"),
        "servicio_sin_entregables": not servicio.definiciones_entregables.filter(activo=True).exists(),
        # Fase 4.A: con varios aprobadores la pregunta habitual es «todos o cualquiera» (en paralelo); el orden
        # secuencial queda en las opciones avanzadas.
        "form_aprobacion_nuevo": AprobacionConfigForm(
            prefix="nuevo-config", bloques_entregable=bloques_entregable,
            initial={"cantidad": 1, "modo": "PARALELA", "politica": "TODOS"},
        ),
        "filas_aprobadores_nuevo": _filas_aprobadores(
            "nuevo-participantes", columnas_actor=opciones["columnas_actor"],
            sin_solicitante=opciones.get("sin_solicitante", False),
        ),
        # 4.F1: una DECISION se configura al crearla (condición + rutas), no después.
        "form_decision_condicion_nuevo": (
            CondicionalForm(destinos=destinos_decision, prefix="nuevo-cond") if editable else None
        ),
        "form_decision_otro_caso_nuevo": (
            FallbackForm(destinos=destinos_decision, prefix="nuevo-fallback") if editable else None
        ),
    }


def _ancla_urls_servicio(servicio):
    """URLs que usa la lista de bloques (`catalogo/_ejecucion_bloques.html`)
    cuando el ancla es un Servicio."""
    return {
        "pk": servicio.pk,
        "url_guardar_nuevo": "catalogo:studio_bloque_crear",
        "url_editar": "catalogo:studio_bloque_editar",
        "url_eliminar": "catalogo:studio_bloque_eliminar",
        "url_ruta": "catalogo:studio_ruta_aprobacion_guardar",
        "url_cond_crear": "catalogo:studio_condicional_crear",
        "url_cond_editar": "catalogo:studio_condicional_editar",
        "url_cond_eliminar": "catalogo:studio_condicional_eliminar",
        "url_fallback": "catalogo:studio_fallback_guardar",
        "pagina": f"{reverse('catalogo:studio', args=[servicio.pk])}?tab=ejecucion",
        "pagina_despues": f"{reverse('catalogo:studio', args=[servicio.pk])}?tab=ejecucion&despues=",
    }


def _contexto_ejecucion(servicio, despues=None, plantilla=None):
    workflow = servicio.workflow
    version_activa = workflow.version_activa if workflow else None
    version_borrador = _version_ejecucion_editable(servicio)
    version_mostrada = version_borrador or version_activa
    # Sin ejecución todavía: se ofrece partir de un flujo ya publicado, con
    # vista previa de sus etapas (solo lectura; `?plantilla=<id>`).
    plantillas, plantilla_elegida, plantilla_bloques = [], None, []
    if workflow is None:
        plantillas = list(plantillas_de_ejecucion())
        for flujo in plantillas:
            flujo.fases_resumen = _fases_para_resumen(flujo.version_activa)
            flujo.n_bloques = len(flujo.fases_resumen)
            flujo.recorrido = [fase.nombre for fase in flujo.fases_resumen[:5]]
            flujo.recorrido_extra = max(flujo.n_bloques - len(flujo.recorrido), 0)
        plantilla_elegida = next((p for p in plantillas if str(p.pk) == str(plantilla)), None)
        if plantilla_elegida is not None:
            plantilla_bloques = [
                {"etapa": fase, "tipo_empresarial_display": "Fase", "resumen": fase.descripcion}
                for fase in plantilla_elegida.fases_resumen
            ]
    elif workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        compartido_con = list(servicios_que_comparten(workflow, excluir=servicio))
        contexto = {
            "compartido_con": compartido_con,
            "plantillas": plantillas,
            "plantilla_elegida": plantilla_elegida,
            "plantilla_bloques": plantilla_bloques,
            "plantilla_tiene_decisiones": False,
            "workflow": workflow,
            "ancla": _ancla_urls_servicio(servicio),
        }
        contexto.update(_contexto_configuracion_operativa(servicio))
        return contexto
    # Workflow compartido: se advierte con qué servicios (regla de reutilización).
    compartido_con = list(servicios_que_comparten(workflow, excluir=servicio)) if workflow is not None else []
    contexto = {
        "compartido_con": compartido_con,
        "plantillas": plantillas,
        "plantilla_elegida": plantilla_elegida,
        "plantilla_bloques": plantilla_bloques,
        # Las fases de una plantilla (D3) no son bloques: no tienen tipo empresarial.
        "plantilla_tiene_decisiones": any(f.get("tipo_empresarial") == "DECISION" for f in plantilla_bloques),
        "workflow": workflow,
        "version_ejecucion_activa": version_activa,
        "version_ejecucion_borrador": version_borrador,
        "version_ejecucion": version_mostrada,
        "editable_ejecucion": version_borrador is not None,
        "ancla": _ancla_urls_servicio(servicio),
        "variables_decision": _variables_para_decisiones(servicio),
    }
    contexto.update(_contexto_bloques(version_borrador, version_mostrada, despues))
    return contexto


def _contexto_salida(servicio):
    definiciones = list(servicio.definiciones_entregables.filter(activo=True).order_by("orden", "pk"))
    for definicion in definiciones:
        inicial = {
            "nombre": definicion.nombre, "descripcion": definicion.descripcion, "tipo": definicion.tipo,
            "obligatorio": definicion.obligatorio, "orden": definicion.orden,
        }
        if definicion.tipo == DefinicionEntregable.Tipo.ELEMENTOS:
            claves, usuarios, equipos = configuracion_de_conjunto(definicion)
            inicial.update({"columnas_revisadas": claves, "revisores_usuarios": usuarios, "revisores_equipos": equipos})
        definicion.form_editar = DefinicionEntregableForm(
            initial=inicial, prefix=f"entregable-{definicion.pk}-editar", servicio=servicio
        )
    organizacion = proceso_ui.organizar_salida(servicio, definiciones) if proceso_ui.aplica(servicio) else {}
    return {
        **organizacion,
        **_contexto_gacetas(servicio),
        "definiciones": definiciones,
        "entregable_form_nuevo": DefinicionEntregableForm(prefix="entregable-nuevo", servicio=servicio),
        "form_politica": PoliticaEntregaForm(
            initial={"politica": servicio.politica_entrega or None, "dias_observacion": servicio.dias_observacion}
        ),
        "politica_actual": servicio.politica_entrega,
    }


def _forms_gaceta_kwargs(servicio):
    opciones = _opciones_bloques(servicio)
    return {
        "colecciones": opciones["colecciones"], "columnas": opciones["columnas_condicion"],
        "columnas_actor": opciones["columnas_actor"], "columnas_editables": opciones["columnas_editables"],
    }


def _contexto_gacetas(servicio):
    """Fase 3 — sección «Gacetas» y «Reglas de cierre» de Salida (solo en un Proceso)."""
    from apps.catalogo.gacetas import reglas_de
    from apps.catalogo.models import DefinicionGaceta

    if not proceso_ui.aplica(servicio):
        return {"gacetas_aplica": False}
    kwargs = _forms_gaceta_kwargs(servicio)
    gacetas = list(DefinicionGaceta.objects.filter(servicio=servicio).order_by("-activa", "orden", "pk"))
    for definicion in gacetas:
        definicion.form_editar = DefinicionGacetaForm(
            initial=DefinicionGacetaForm.inicial(definicion), prefix=f"gaceta-{definicion.pk}", **kwargs
        )
    return {
        "gacetas_aplica": True,
        "gacetas": gacetas,
        "gaceta_form_nueva": DefinicionGacetaForm(prefix="gaceta-nueva", **kwargs),
        "reglas_form": ReglasFinalizacionForm(initial=reglas_de(servicio)),
    }


@login_required
def studio_gaceta_guardar_view(request, pk, definicion_id=None):
    """Fase 3 — crear o editar una gaceta del Proceso (Studio › Salida)."""
    from apps.catalogo.gacetas import configurar_gaceta
    from apps.catalogo.models import DefinicionGaceta

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    definicion = get_object_or_404(DefinicionGaceta, pk=definicion_id, servicio=servicio) if definicion_id else None
    prefix = f"gaceta-{definicion_id}" if definicion_id else "gaceta-nueva"
    form = DefinicionGacetaForm(request.POST, prefix=prefix, **_forms_gaceta_kwargs(servicio))
    if not form.is_valid():
        messages.error(request, "Revisa la gaceta: " + "; ".join(m for lista in form.errors.values() for m in lista))
        return redirect(_volver(pk, "salida"))
    try:
        configurar_gaceta(
            servicio, request.user, nombre=form.cleaned_data["nombre"],
            descripcion=form.cleaned_data.get("descripcion", ""), configuracion=form.configuracion(), definicion=definicion,
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Gaceta guardada. Aplica a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "salida"))


@login_required
def studio_gaceta_estado_view(request, pk, definicion_id):
    from apps.catalogo.gacetas import cambiar_estado_gaceta
    from apps.catalogo.models import DefinicionGaceta

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    definicion = get_object_or_404(DefinicionGaceta, pk=definicion_id, servicio_id=pk)
    activa = request.POST.get("activa") == "1"
    try:
        cambiar_estado_gaceta(definicion, request.user, activa=activa)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Gaceta reactivada." if activa else "Gaceta retirada para los tickets nuevos.")
    return redirect(_volver(pk, "salida"))


@login_required
def studio_reglas_finalizacion_view(request, pk):
    from apps.catalogo.gacetas import configurar_reglas_finalizacion

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = ReglasFinalizacionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisa las reglas de cierre.")
        return redirect(_volver(pk, "salida"))
    try:
        configurar_reglas_finalizacion(servicio, request.user, form.configuracion())
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Reglas de cierre guardadas. Aplican a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "salida"))


LISTO, ATENCION, OPCIONAL = "listo", "atencion", "opcional"
ETIQUETA_VERIFICACION = {LISTO: "Listo", ATENCION: "Requiere atención", OPCIONAL: "Opcional"}
CLASE_VERIFICACION = {LISTO: "complete", ATENCION: "attention", OPCIONAL: "optional"}


DONDE = {"general": "Básico", "entrada": "Formulario", "salida": "Salida", "ejecucion": "Flujo", "publicacion": "Publicar"}


def _fila(nombre, estado, lineas, tab, uso="", errores=(), donde=""):
    errores = list(errores)
    if errores:
        estado = ATENCION
    return {
        "nombre": nombre, "estado": estado, "etiqueta": ETIQUETA_VERIFICACION[estado],
        "clase": CLASE_VERIFICACION[estado], "lineas": [l for l in lineas if l], "errores": errores,
        "tab": tab, "uso": uso, "url": "", "donde": donde or DONDE.get(tab, ""),
    }


def _verificacion(servicio, *, checklist, resumen, errores_por_seccion, concesiones, es_restringido, responsables):
    """Fase 4.B — UNA lista de verificación para «Revisión y publicación»: cada fila dice qué es, su estado
    (Listo · Requiere atención · Opcional), una explicación breve y dónde corregirlo. Junta el resumen del Proceso,
    la lista de comprobación y los pendientes de publicación (agrupados por sección), sin repetirlos. Solo lee."""
    grupos = {g["seccion"]: g["errores"] for g in errores_por_seccion}
    destinos = {g["seccion"]: g for g in errores_por_seccion}
    filas = []
    textos = {item["texto"]: item["ok"] for item in checklist}

    def texto_de(*opciones):
        return next((t for t in opciones if t in textos), "")

    if proceso_ui.aplica(servicio):
        mapa = {"ok": LISTO, "pendiente": ATENCION, "neutro": OPCIONAL}
        for tarjeta in resumen.get("tarjetas") or []:
            clave, _etiqueta, tab, uso = tarjeta["seccion"]
            filas.append(_fila(
                tarjeta["titulo"], mapa.get(tarjeta.get("estado"), OPCIONAL), tarjeta.get("lineas") or [], tab, uso,
                grupos.pop(clave, []), donde=_etiqueta,
            ))
    else:
        # Bloque 2: los bloqueos de acceso se muestran en su fila (Visibilidad / Responsables), no en «General».
        generales = grupos.pop("general", [])
        sin_acceso = [m for m in generales if m.startswith("Nadie puede iniciarlo")]
        sin_atencion = [m for m in generales if m.startswith("Nadie puede atenderlo")]
        otros = [m for m in generales if m not in sin_acceso and m not in sin_atencion]
        filas.append(_fila("Información general", LISTO, [], "general", errores=otros))
        entrada = texto_de("Formulario de entrada", "Sin formulario con una versión activa")
        filas.append(_fila(
            "Formulario de entrada", LISTO if textos.get(entrada) else ATENCION, [entrada], "entrada",
            errores=grupos.pop("entrada", []),
        ))
        if servicio.workflow_id is None:
            filas.append(_fila("Flujo", OPCIONAL, ["Sin flujo: se atiende como solicitud simple."], "ejecucion",
                               errores=grupos.pop("ejecucion", [])))
        else:
            filas.append(_fila("Flujo", LISTO, [f"Flujo «{servicio.workflow.nombre}»."], "ejecucion",
                               errores=grupos.pop("ejecucion", [])))
        entregables = texto_de("Entregables configurados", "Sin entregables configurados (opcional)")
        filas.append(_fila(
            "Entregables", LISTO if textos.get(entregables) else OPCIONAL, [entregables], "salida",
            errores=grupos.pop("salida", []),
        ))
        visibilidad = texto_de(
            "Visibilidad configurada", "Restringido y sin nadie con acceso: nadie lo encontrará en el catálogo"
        )
        lineas = [visibilidad]
        if es_restringido and concesiones:
            lineas.append("Pueden encontrarlo: " + ", ".join(
                str(c.usuario or c.area or c.unidad_negocio) for c in concesiones
            ) + ".")
        elif not es_restringido:
            lineas.append("Público interno: cualquier persona de la organización.")
        filas.append(_fila(
            "Visibilidad", LISTO if textos.get(visibilidad) else ATENCION, lineas, "general", errores=sin_acceso,
        ))
        texto_resp = texto_de("Responsables configurados", "Sin responsables configurados")
        nombres = [str(r.usuario if r.tipo_responsable == "USUARIO" else r.equipo) for r in responsables]
        filas.append(_fila(
            "Responsables", LISTO if responsables else OPCIONAL,
            [texto_resp, ", ".join(nombres) + "." if nombres else ""], "general", errores=sin_atencion,
        ))
    if not (servicio.tipo == Servicio.Tipo.PROCESO and programacion_activa_de(servicio) is not None):
        entrega = texto_de(
            "Entrega al solicitante definida", "Sin política de entrega: los tickets se resolverán y cerrarán manualmente"
        )
        filas.append(_fila(
            "Entrega al solicitante", LISTO if servicio.politica_entrega else OPCIONAL,
            [entrega, servicio.get_politica_entrega_display() if servicio.politica_entrega else ""], "salida",
        ))
    base = reverse("catalogo:studio", args=[servicio.pk])
    for fila in filas:
        fila["url"] = f"{base}?tab={fila['tab']}" + (f"&uso={fila['uso']}" if fila["uso"] else "")
    for clave, errores in grupos.items():
        fila = _fila("Pendientes de " + destinos[clave]["etiqueta"], ATENCION, [], "publicacion", errores=errores,
                     donde=destinos[clave]["etiqueta"])
        fila["url"] = destinos[clave]["url"]
        filas.append(fila)
    return filas


def _cabecera_revision(servicio, responsables):
    """Resumen compacto del encabezado de «Revisión y publicación»."""
    modalidad = ""
    if servicio.tipo == Servicio.Tipo.PROCESO:
        modalidad = "Programado" if programacion_activa_de(servicio) is not None else "Manual"
    nombres = [str(r.usuario if r.tipo_responsable == "USUARIO" else r.equipo) for r in responsables]
    return {
        "tipo": servicio.get_tipo_display(), "modalidad": modalidad,
        "responsable": ", ".join(nombres[:2]) + (f" y {len(nombres) - 2} más" if len(nombres) > 2 else "") or "Sin definir",
        "flujo": servicio.workflow.nombre if servicio.workflow_id else "Sin flujo",
    }


def _es_legacy(servicio):
    return servicio.workflow_id is not None and servicio.workflow.modo != Workflow.Modo.PLANTILLA_FASES


def _contexto_publicacion(servicio, actor=None, ev=None):
    """Publicar (Fase 4.A; Bloque 2): qué está activo, qué está pendiente y entrará en vigor al publicar, qué aplica
    al guardar, errores que bloquean y advertencias. Todo sale de la MISMA evaluación que la navegación
    (`apps.catalogo.evaluacion`: publicación simulada y deshecha), más el permiso por pieza de quien publica. El
    flujo LEGACY conserva su publicación de siempre."""
    from apps.catalogo import dependencias, publicacion

    ev = ev or evaluacion.evaluar(servicio, actor)
    checklist = [
        {"ok": bool(servicio.nombre and servicio.categoria_id), "texto": "Información general"},
    ]
    datos = ev.datos_inicio
    # Formulario de un Servicio: listo si hay versión activa o un borrador que se activará al publicar sin errores.
    entrada_ok = (datos.version is not None or datos.borrador) and not ev.bloqueos_de(evaluacion.DATOS)
    checklist.append({
        "ok": entrada_ok, "texto": "Formulario de entrada" if entrada_ok else "Sin formulario con una versión activa",
    })

    piezas = ev.piezas
    pendientes = ev.pendientes
    legacy = _es_legacy(servicio)
    activaria = ev.activaria
    mensajes = list(ev.bloqueos)
    faltan_permisos = (
        publicacion.permisos_que_faltan(servicio, actor) if actor is not None and ev.simulada and ev.pendientes else []
    )
    publicable = ev.publicable and not faltan_permisos
    errores = [m for m in (_traducir_error_ejecucion(msg) for msg in mensajes) if m] + faltan_permisos

    tiene_entregables = servicio.definiciones_entregables.filter(activo=True).exists()
    checklist.append(
        {"ok": tiene_entregables, "texto": "Entregables configurados" if tiene_entregables else "Sin entregables configurados (opcional)"}
    )
    checklist.append({"ok": publicable, "texto": "Lista para publicar" if publicable else "Hay pendientes que corregir (ver abajo)"})

    concesiones = _concesiones_visibilidad(servicio)
    es_restringido = servicio.alcance_visibilidad == Servicio.AlcanceVisibilidad.RESTRINGIDO
    visibilidad_ok = (not es_restringido) or bool(concesiones)
    checklist.append(
        {
            "ok": visibilidad_ok,
            "texto": "Visibilidad configurada" if visibilidad_ok else "Restringido y sin nadie con acceso: nadie lo encontrará en el catálogo",
        }
    )
    responsables = list(
        servicio.responsables.filter(activo=True).select_related("usuario", "equipo").order_by("pk")
    )
    checklist.append(
        {
            "ok": bool(responsables),
            "texto": "Responsables configurados" if responsables else "Sin responsables configurados",
        }
    )
    checklist.append(
        {
            "ok": bool(servicio.politica_entrega),
            "texto": "Entrega al solicitante definida"
            if servicio.politica_entrega
            else "Sin política de entrega: los tickets se resolverán y cerrarán manualmente",
        }
    )

    # Publicar un flujo LEGACY compartido cambia la ejecución de otros servicios: confirmación reforzada.
    impacto_compartido = []
    if legacy and _version_ejecucion_editable(servicio) is not None:
        impacto_compartido = list(servicios_que_comparten(servicio.workflow, excluir=servicio))
    es_proceso = proceso_ui.aplica(servicio)
    resumen = (
        proceso_ui.resumen_publicacion(
            servicio, servicio.configuracion_ejecucion_activa, _configuracion_ejecucion_editable(servicio), ev
        )
        if es_proceso else {"tarjetas": [], "avisos": []}
    )
    advertencias = [d for d in dependencias.detectar(servicio) if not d.bloquea]
    errores_seccion = ev.grupos_traducidos(_traducir_error_ejecucion)
    verificacion = _verificacion(
        servicio, checklist=checklist, resumen=resumen, errores_por_seccion=errores_seccion,
        concesiones=concesiones, es_restringido=es_restringido, responsables=responsables,
    )
    if faltan_permisos:
        # No es un problema de la configuración: es de quien publica (publicar de verdad lo vuelve a exigir).
        verificacion.append(_fila("Permisos para publicar", ATENCION, [], "publicacion", errores=faltan_permisos))
    if servicio.activo and not pendientes:
        accion_publicar = ""  # nada que publicar
    elif servicio.activo:
        accion_publicar = "Publicar cambios"
    else:
        accion_publicar = "Publicar"
    return {
        "impacto_compartido": impacto_compartido,
        "errores_por_seccion": errores_seccion,
        "verificacion": verificacion,
        "cabecera_revision": _cabecera_revision(servicio, responsables),
        "resumen_proceso": resumen,
        "checklist": checklist, "errores": errores, "publicable": publicable,
        "concesiones": concesiones, "es_restringido": es_restringido,
        "responsables": responsables, "form_responsable": ResponsableForm(),
        "piezas_version": piezas, "piezas_pendientes": pendientes, "activaria": activaria,
        "aplican_al_guardar": publicacion.aplican_al_guardar(servicio),
        "advertencias_dependencias": advertencias,
        "accion_publicar": accion_publicar, "es_legacy": legacy,
    }


# --- Vista principal (GET, dispatch por pestaña) --------------------------


def _estado_formulario(ev):
    """Formulario (Bloque 2), sobre la evaluación central (`apps.catalogo.evaluacion`):

    - Un bloqueo real de la sección (tras activar los borradores) es «Requiere atención», salvo que la sección tenga
      un borrador en curso: entonces es trabajo «En progreso» (Publicar lo lista como pendiente).
    - SERVICIO: necesita su formulario (sin él, «Sin configurar»).
    - PROCESO: datos de inicio y colecciones son opcionales; con UNA pieza activa y válida la sección está completa
      (todas las colecciones cuentan); sin ninguna, «Opcional». El formulario técnico vacío no cuenta."""
    servicio = ev.servicio
    datos, colecciones = ev.datos_inicio, ev.colecciones
    bloqueos = ev.bloqueos_de(evaluacion.DATOS, evaluacion.ELEMENTOS)
    en_curso = ev.pendiente_en("formulario", "coleccion:")
    es_servicio = servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general
    if es_servicio and servicio.formulario_id is None:
        # Falta una pieza obligatoria que nunca se creó: «Sin configurar» (Publicar la lista como pendiente).
        return ESTADO_SIN_CONFIGURAR, "Sin configurar"
    if bloqueos:
        return (ESTADO_EN_PROGRESO, "Falta completar un borrador") if en_curso else (ESTADO_ATENCION, "Requiere atención")
    sufijo = " · cambios sin activar" if en_curso else ""
    if servicio.tipo != Servicio.Tipo.PROCESO or servicio.es_ticket_general:
        if servicio.formulario_id is None:
            return ESTADO_SIN_CONFIGURAR, "Sin configurar"
        if datos.version is not None:
            return ESTADO_COMPLETA, f"v{datos.version.numero} activa{sufijo}"
        return ESTADO_EN_PROGRESO, "Borrador listo para activar al publicar"
    partes = []
    if datos.configurada and datos.activa_valida:
        partes.append(f"Formulario de inicio · {datos.campos} campo{'s' if datos.campos != 1 else ''}")
    validas = [c for c in colecciones if c.activa_valida]
    if validas:
        partes.append(f"{len(validas)} colecci{'ones' if len(validas) != 1 else 'ón'}")
    if partes:
        return ESTADO_COMPLETA, " · ".join(partes) + sufijo
    if en_curso:
        return ESTADO_EN_PROGRESO, "Cambios sin activar"
    return ESTADO_OPCIONAL, "Opcional: sin formulario de inicio ni colecciones"


def _estado_salida(ev):
    """Salida: opcional en ambos tipos; «Completa» solo con algo configurado (nunca por haberla visitado)."""
    servicio = ev.servicio
    if ev.bloqueos_de(evaluacion.SALIDA):
        return ESTADO_ATENCION, "Requiere atención"
    configurada = servicio.definiciones_entregables.filter(activo=True).exists() or bool(servicio.politica_entrega)
    if not configurada and proceso_ui.aplica(servicio):
        from apps.catalogo.models import DefinicionGaceta, ReglaFinalizacion

        configurada = (
            DefinicionGaceta.objects.filter(servicio=servicio, activa=True).exists()
            or ReglaFinalizacion.objects.filter(servicio=servicio).exists()
        )
    return (ESTADO_COMPLETA, "Configurada") if configurada else (ESTADO_OPCIONAL, "Opcional")


def _estado_flujo(ev):
    """Flujo: opcional en un Servicio, obligatorio y ejecutable en un Proceso. Un borrador válido no es un error."""
    from apps.catalogo import reconciliacion

    servicio = ev.servicio
    workflow = servicio.workflow
    if workflow is None:
        if servicio.tipo == Servicio.Tipo.SERVICIO:
            return ESTADO_OPCIONAL, "Opcional: sin flujo"
        return ESTADO_SIN_CONFIGURAR, "Necesita un flujo"
    bloqueos = ev.bloqueos_de(evaluacion.FLUJO)
    if workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        en_curso = ev.pendiente_en("flujo")
        if reconciliacion.requiere_reconciliacion(servicio):
            return ESTADO_ATENCION, "El flujo cambió: actualiza la configuración"
        if bloqueos:
            return (ESTADO_EN_PROGRESO, "En progreso: faltan datos") if en_curso else (ESTADO_ATENCION, "Requiere atención")
        if servicio.configuracion_ejecucion_activa is not None:
            return ESTADO_COMPLETA, "Configurada" + (" · cambios sin activar" if en_curso else "")
        if en_curso:
            return ESTADO_EN_PROGRESO, "Lista para activar al publicar"
        return ESTADO_SIN_CONFIGURAR, "Sin configurar"
    version_wf = workflow.version_activa
    if version_wf is not None and version_wf.estado == WorkflowVersion.Estado.ACTIVA and not bloqueos:
        return ESTADO_COMPLETA, f"v{version_wf.numero} publicada"
    return ESTADO_ATENCION, "Requiere publicar el flujo"


def _estado_publicar(ev):
    servicio = ev.servicio
    if servicio.activo and not ev.pendientes:
        return (ESTADO_COMPLETA, "Publicado") if ev.publicable else (ESTADO_ATENCION, "Publicado · con pendientes")
    if not ev.publicable:
        return ESTADO_ATENCION, "Con pendientes"
    return ESTADO_EN_PROGRESO, "Cambios sin publicar" if servicio.activo else "Listo para publicar"


def _estado_secciones(servicio, ev=None):
    """Estado de las cinco secciones (Sin configurar · En progreso · Completa · Opcional · Requiere atención), todas
    sobre la MISMA evaluación que usa «Revisión y publicación» (`apps.catalogo.evaluacion`): «Requiere atención»
    solo por un bloqueo real tras activar los borradores, en la sección donde se corrige. Solo lectura."""
    from apps.catalogo import estado_basico

    ev = ev or evaluacion.evaluar(servicio)
    # Básico: se deriva de sus apartados (fuente única `estado_basico`); un bloqueo de Básico que sus apartados no
    # explican (p. ej. el tipo) también pide atención.
    general = estado_basico.agregado(ev.apartados_basico)
    if ev.bloqueos_de(evaluacion.GENERAL) and general[0] == ESTADO_COMPLETA:
        general = (ESTADO_ATENCION, "Requiere atención")
    estados = {
        "general": general, "entrada": _estado_formulario(ev), "salida": _estado_salida(ev),
        "ejecucion": _estado_flujo(ev), "publicacion": _estado_publicar(ev),
    }
    return [
        {
            "clave": clave, "etiqueta": TAB_LABELS[clave], "estado": estados[clave][0], "texto": estados[clave][1],
            "estado_etiqueta": ETIQUETAS_ESTADO[estados[clave][0]],
        }
        for clave in TABS
    ]


def _navegacion(secciones, tab):
    """Atrás / Continuar / Omitir y progreso (Fase 4.A). Son enlaces: navegar nunca guarda, activa ni publica, y
    omitir no borra nada (solo se ofrece si la sección actual es opcional)."""
    claves = [s["clave"] for s in secciones]
    indice = claves.index(tab)
    actual = secciones[indice]
    listas = sum(1 for s in secciones if s["estado"] in (ESTADO_COMPLETA, ESTADO_OPCIONAL))
    return {
        "anterior": secciones[indice - 1] if indice > 0 else None,
        "siguiente": secciones[indice + 1] if indice + 1 < len(secciones) else None,
        "omitible": actual["estado"] == ESTADO_OPCIONAL,
        "posicion": indice + 1,
        "total": len(secciones),
        "listas": listas,
        "porcentaje": int(listas * 100 / len(secciones)),
    }


@login_required
def studio_view(request, pk):
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    tab = request.GET.get("tab")
    if tab not in TABS:
        tab = "general"

    # Bloque 2: UNA evaluación por petición para la navegación y «Revisión y publicación».
    ev = evaluacion.evaluar(servicio, request.user)
    secciones = _estado_secciones(servicio, ev)
    contexto = {
        "servicio": servicio,
        "tab": tab,
        "titulo_pagina": servicio.nombre,
        "secciones": secciones,
        "navegacion": _navegacion(secciones, tab),
        "puede_formulario": _puede_formulario(request.user),
        "puede_ejecucion": puede_administrar_workflows(request.user),
        "puede_vincular_workflow": puede_vincular_workflows(request.user),
        # 4.G1: un Proceso programado no tiene solicitante; las secciones orientadas a quien solicita
        # (entrega formal, catálogo, búsqueda) no se le muestran.
        "proceso_programado": servicio.tipo == Servicio.Tipo.PROCESO and programacion_activa_de(servicio) is not None,
        # 4.G2E: la misma estructura de cinco secciones; un Proceso explica cómo se relacionan sus piezas.
        "es_proceso": proceso_ui.aplica(servicio),
    }
    constructores = {
        "general": _contexto_general,
        "entrada": _contexto_entrada,
        "ejecucion": _contexto_ejecucion,
        "salida": _contexto_salida,
        "publicacion": _contexto_publicacion,
    }
    if tab == "ejecucion":
        contexto.update(_contexto_ejecucion(servicio, request.GET.get("despues"), request.GET.get("plantilla")))
    elif tab == "entrada":
        uso = USO_PARRILLA if request.GET.get("uso") == USO_PARRILLA else USO_ENTRADA
        contexto.update(_contexto_entrada(servicio, uso, request.GET.get("coleccion") or None, ev))
    elif tab == "publicacion":
        contexto.update(_contexto_publicacion(servicio, request.user, ev))
    else:
        contexto.update(_contexto_general(servicio, ev) if tab == "general" else constructores[tab](servicio))
    if tab == "general":
        # Básico como asistente: al llegar desde «Crear borrador» (`asistente=1`) los grupos se desbloquean al
        # avanzar; un registro existente navega libremente. `grupo` abre un grupo concreto (solo los conocidos).
        contexto["basico_progresivo"] = request.GET.get("asistente") == "1"
        grupo = request.GET.get("grupo") or ""
        contexto["basico_grupo"] = grupo if grupo in GRUPOS_BASICO else ""
    # Fase 4.A: sin el permiso que exige la acción, sus controles no se ofrecen (antes se veían y respondían 403).
    if tab == "ejecucion" and not contexto["puede_ejecucion"] and contexto.get("editable_ejecucion"):
        contexto["editable_ejecucion"] = False
        contexto["sin_permiso_flujo"] = True
    if tab == "entrada" and not contexto["puede_formulario"]:
        contexto["sin_permiso_formulario"] = True
        contexto["editable_entrada"] = False
        contexto["campo_form_nuevo"] = None
        contexto["regla_form_nueva"] = None
    return render(request, "catalogo/studio.html", contexto)


# --- GENERAL ---------------------------------------------------------------


@login_required
def studio_general_guardar_view(request, pk):
    """Guarda Básico. 4.G1: en un Proceso incluye «¿Cómo inicia?» (Manual/Programado) en el mismo
    formulario y la misma transacción: si la programación no es válida no se guarda nada."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = ServicioGeneralForm(request.POST, instance=servicio)
    if not form.is_valid():
        messages.error(request, "Revise los datos generales: " + _errores_de_form(form))
        return redirect(_volver(pk, "general"))
    datos = form.cleaned_data
    # Solo un Proceso (no el Servicio interno del Ticket General) y solo si el formulario trae la
    # sección «Inicio»: un envío sin ella no toca la programación.
    con_inicio = (
        datos["tipo"] == Servicio.Tipo.PROCESO and not servicio.es_ticket_general and "modo" in request.POST
    )
    form_inicio = InicioProcesoForm(request.POST) if con_inicio else None
    if con_inicio and not form_inicio.is_valid():
        messages.error(request, "; ".join(e for errores in form_inicio.errors.values() for e in errores))
        return redirect(_volver(pk, "general"))
    try:
        with transaction.atomic():
            editar_servicio_general(
                servicio, request.user, nombre=datos["nombre"], descripcion=datos.get("descripcion", ""),
                categoria=datos["categoria"], tipo=datos["tipo"], instrucciones=datos.get("instrucciones", ""),
                alcance_visibilidad=datos["alcance_visibilidad"],
            )
            if con_inicio:
                _aplicar_inicio(servicio, request.user, form_inicio.cleaned_data)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Información general actualizada.")
    return redirect(_volver(pk, "general"))


@login_required
def studio_tiempo_objetivo_guardar_view(request, pk):
    """4.A1 — tiempo objetivo de atención, dentro de la pestaña Básico."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = TiempoObjetivoForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "general"))
    try:
        configurar_tiempo_objetivo(
            servicio, request.user, cantidad=form.cleaned_data.get("cantidad"),
            unidad=form.cleaned_data.get("unidad") or "", habiles=form.cleaned_data.get("habiles", False),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(
            request, "Tiempo objetivo guardado. Aplica a los tickets que se creen desde ahora."
        )
    return redirect(_volver(pk, "general"))


@login_required
def studio_parrilla_participantes_view(request, pk):
    """Bloque 3 — guarda los participantes de una colección (personas y equipos activos). La regla y la auditoría
    están en `catalogo.parrilla.configurar_participantes`."""
    from django.contrib.auth import get_user_model

    from apps.catalogo.parrilla import configurar_participantes
    from apps.core.models import Equipo

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    coleccion = _coleccion(request)

    def _ids(nombre):
        return [int(v) for v in request.POST.getlist(nombre) if str(v).isdigit()]

    usuarios = list(get_user_model().objects.filter(pk__in=_ids("usuarios")))
    equipos = list(Equipo.objects.filter(pk__in=_ids("equipos")))
    try:
        configurar_participantes(servicio, request.user, usuarios=usuarios, equipos=equipos, clave=coleccion)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Participantes guardados. Aplican a los tickets que se creen desde ahora.")
    return redirect(_volver_entrada(pk, USO_PARRILLA, coleccion))


@login_required
def studio_parrilla_configurar_view(request, pk):
    """4.G2B1 — activa, renombra o desactiva la estructura de elementos de un Proceso (dentro de la
    pestaña Formulario). Las columnas se editan con el mismo editor de campos (`uso=parrilla`)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    coleccion = _coleccion(request)
    try:
        if request.POST.get("accion") == "agregar":
            # Fase 1: otra colección independiente (sus propias columnas, versiones y clave).
            form = ParrillaForm(request.POST, prefix="coleccion-nueva")
            if not form.is_valid():
                messages.error(request, "Indique cómo se llama cada ítem de la nueva colección (hasta 60 caracteres).")
            else:
                nueva = agregar_coleccion(servicio, request.user, nombre_elemento=form.cleaned_data["nombre_elemento"])
                messages.success(request, "Colección agregada: define sus columnas y activa la versión.")
                return redirect(_volver_entrada(pk, USO_PARRILLA, nueva.clave))
        elif request.POST.get("accion") == "desactivar":
            desactivar_parrilla(servicio, request.user, coleccion)
            messages.success(request, "Los tickets nuevos de este proceso ya no tendrán esta colección.")
        else:
            form = ParrillaForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Indique cómo se llama cada elemento (hasta 60 caracteres).")
            else:
                configurar_parrilla(
                    servicio, request.user, nombre_elemento=form.cleaned_data["nombre_elemento"], clave=coleccion,
                    permite_altas_extraordinarias=form.cleaned_data["permite_altas_extraordinarias"],
                )
                messages.success(request, "Elementos estructurados listos: define sus columnas y activa la versión.")
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    return redirect(_volver_entrada(pk, USO_PARRILLA, coleccion))


@login_required
def studio_parrilla_tareas_view(request, pk):
    """4.G2D1 — configura (o desactiva) cómo cada elemento del proceso origina una tarea, dentro de la
    pestaña Formulario › Elementos del proceso. No genera tareas: eso es una operación de dominio."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        if request.POST.get("accion") == "desactivar":
            desactivar_tareas_elemento(servicio, request.user)
            messages.success(request, "Los tickets nuevos de este proceso ya no generarán tareas por elemento.")
        else:
            configurar_tareas_elemento(
                servicio, request.user,
                clave_titulo=request.POST.get("clave_titulo", ""),
                clave_descripcion=request.POST.get("clave_descripcion", ""),
                clave_responsable=request.POST.get("clave_responsable", ""),
                clave_fecha_limite=request.POST.get("clave_fecha_limite", ""),
                requiere_evidencia="requiere_evidencia" in request.POST,
                columnas_editables=request.POST.getlist("columnas_editables"),
            )
            messages.success(request, "Tareas por elemento guardadas. Aplican a los tickets que se creen desde ahora.")
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    return redirect(_volver_entrada(pk, USO_PARRILLA))


@login_required
def studio_prorroga_guardar_view(request, pk):
    """4.A2 — política de prórroga, dentro de la pestaña Básico."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = PoliticaProrrogaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "general"))
    try:
        configurar_politica_prorroga(
            servicio, request.user, politica=form.cleaned_data["politica"],
            aprobador_usuario=form.cleaned_data.get("aprobador_usuario"),
            aprobador_equipo=form.cleaned_data.get("aprobador_equipo"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Política de prórroga guardada. Aplica a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "general"))


# --- ENTRADA -----------------------------------------------------------


@login_required
def studio_entrada_version_view(request, pk, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        if uso == USO_PARRILLA:
            formulario = _formulario_de_uso(servicio, uso, _coleccion(request))
            if formulario is None:
                messages.error(request, "Este proceso todavía no usa elementos estructurados.")
                return redirect(_volver_entrada(pk, uso, _coleccion(request)))
            crear_nueva_version(formulario, request.user)
        elif servicio.formulario_id is None:
            form = FormularioForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Indique un nombre para el formulario.")
                return redirect(_volver_entrada(pk, uso, _coleccion(request)))
            asociar_formulario_nuevo(
                servicio, request.user, nombre=form.cleaned_data["nombre"],
                descripcion=form.cleaned_data.get("descripcion", ""),
            )
        else:
            crear_nueva_version(servicio.formulario, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Entrada lista para editar.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_entrada_activar_view(request, pk, version_id, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    formulario = _formulario_de_uso(servicio, uso, _coleccion(request))
    if formulario is None:
        raise Http404
    version = get_object_or_404(FormularioVersion, pk=version_id, formulario=formulario)
    try:
        if uso == USO_PARRILLA:
            # 4.G2C1: la nueva versión no puede quitar columnas que un entregable de revisión necesita.
            errores = errores_de_version_de_columnas(servicio, version) + errores_de_version_para_tareas(
                servicio, version
            )
            if errores:
                raise ValueError(" ".join(errores))
        activar_version(formulario, version, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Formulario activado.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_campo_guardar_view(request, pk, campo_id=None, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio, uso, _coleccion(request))
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    instancia = get_object_or_404(Campo, pk=campo_id, version=version) if campo_id is not None else None
    prefix = f"campo-{campo_id}-editar" if campo_id is not None else "campo-nuevo"
    form = CampoForm(request.POST, instance=instancia, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revisa el campo: " + _errores_de_form(form))
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    if uso != USO_PARRILLA and form.cleaned_data["tipo"] in TIPOS_SOLO_EN_ELEMENTOS:
        messages.error(request, "Un campo de tipo equipo solo existe en las columnas de los elementos del proceso.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo = form.save(commit=False)
    campo.version = version
    campo.configuracion = form.configuracion_desde_tipo(form.cleaned_data["tipo"])
    try:
        campo.full_clean()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo.save()
    messages.success(request, "Campo guardado.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_campo_eliminar_view(request, pk, campo_id, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio, uso, _coleccion(request))
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    try:
        campo.delete()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Campo eliminado.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_opcion_guardar_view(request, pk, campo_id, opcion_id=None, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio, uso, _coleccion(request))
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    instancia = get_object_or_404(OpcionCampo, pk=opcion_id, campo=campo) if opcion_id is not None else None
    prefix = f"campo-{campo_id}-opcion-{opcion_id}" if opcion_id is not None else f"campo-{campo_id}-opcion-nueva"
    form = OpcionCampoForm(request.POST, instance=instancia, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revisa la opción: " + _errores_de_form(form))
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    opcion = form.save(commit=False)
    opcion.campo = campo
    try:
        opcion.full_clean()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    opcion.save()
    messages.success(request, "Opción guardada.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_opcion_eliminar_view(request, pk, campo_id, opcion_id, uso=USO_ENTRADA):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio, uso, _coleccion(request))
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    opcion = get_object_or_404(OpcionCampo, pk=opcion_id, campo=campo)
    opcion.delete()
    messages.success(request, "Opción eliminada.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


@login_required
def studio_regla_guardar_view(request, pk, regla_id=None):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    campos_qs = Campo.objects.filter(version=version)
    instancia = get_object_or_404(ReglaCondicional, pk=regla_id, campo_origen__version=version) if regla_id is not None else None
    prefix = f"regla-{regla_id}-editar" if regla_id is not None else "regla-nueva"
    form = ReglaCondicionalForm(request.POST, instance=instancia, campos_queryset=campos_qs, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revisa la regla: " + _errores_de_form(form))
        return redirect(_volver(pk, "entrada"))
    regla = form.save(commit=False)
    try:
        regla.full_clean()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver(pk, "entrada"))
    regla.save()
    messages.success(request, "Regla guardada.")
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_regla_eliminar_view(request, pk, regla_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    regla = get_object_or_404(ReglaCondicional, pk=regla_id, campo_origen__version=version)
    regla.delete()
    messages.success(request, "Regla eliminada.")
    return redirect(_volver(pk, "entrada"))


# --- EJECUCIÓN -----------------------------------------------------------


@login_required
def studio_ejecucion_configurar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
            preparar_configuracion_ejecucion(servicio, request.user)
        else:
            preparar_ejecucion(
                servicio, request.user, confirmar_compartido=request.POST.get("confirmo_compartido") == "1"
            )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ejecución lista para configurar.")
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_ejecucion_activar_configuracion_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _configuracion_ejecucion_editable(servicio)
    if version is None:
        messages.error(request, "No hay una configuracion operativa en borrador para activar.")
        return redirect(_volver(pk, "ejecucion"))
    try:
        activar_configuracion_ejecucion(servicio, version, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Configuracion operativa activada.")
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_ejecucion_crear_flujo_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    nombre = (request.POST.get("nombre") or "").strip() or servicio.nombre
    try:
        workflow = crear_flujo(request.user, nombre=nombre, descripcion=servicio.descripcion)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver(pk, "ejecucion"))
    request.session[f"retorno_servicio_flujo_{workflow.pk}"] = servicio.pk
    messages.success(request, "Flujo creado en la biblioteca. Disenalo, publicalo y vuelve para vincularlo.")
    return redirect("flujos:lienzo", pk=workflow.pk)


@login_required
def studio_ejecucion_vincular_view(request, pk):
    """Usa un flujo ya publicado como la ejecución del servicio (compartido).
    Solo exige poder vincular: no concede modificar el flujo."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_vincular_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        workflow = Workflow.objects.get(pk=int(request.POST.get("plantilla", "")))
    except (ValueError, Workflow.DoesNotExist):
        messages.error(request, "Elige un flujo de la lista.")
        return redirect(_volver(pk, "ejecucion"))
    try:
        vincular_ejecucion(servicio, request.user, workflow)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(
            request,
            f"Este elemento usará el flujo «{workflow.nombre}». Si necesitas cambios propios, "
            "crea una copia desde la pestaña Ejecución.",
        )
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_ejecucion_copia_view(request, pk):
    """Desvincula SOLO a este servicio de un flujo compartido, con copia propia."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        crear_copia_de_ejecucion(servicio, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(
            request,
            "Ahora este elemento tiene su propia copia del flujo. Los demás servicios siguen usando el original.",
        )
    return redirect(_volver(pk, "ejecucion"))


def _h_bloque_guardar(request, ancla, bloque_id=None):
    """Alta (sin `bloque_id`) o edición de un bloque. Compartido por Studio
    (ancla = Servicio) y el Diseñador de Flujos (ancla = Flujo)."""
    if bloque_id is None:
        general_form = BloqueGeneralForm(request.POST, prefix="nuevo")
        if not general_form.is_valid():
            messages.error(request, "Revise el nombre y tipo del bloque.")
            return ancla.volver()
        tipo = general_form.cleaned_data["tipo"]
        version = ancla.version_editable()
        if version is None:
            messages.error(request, "Configure primero la ejecución.")
            return ancla.volver()
        config_kwargs, config_valido = _parsear_configuracion_bloque(request, tipo)
        if not config_valido:
            messages.error(request, "Revise la configuración del bloque.")
            return ancla.volver()
        despues_id = request.POST.get("despues_de", "")
        try:
            with transaction.atomic():
                # El punto de inserción se calcula ANTES de crear el bloque
                # (ver `_plan_al_final`). No hay ningún "gate": un BORRADOR
                # siempre admite nuevos bloques.
                if despues_id:
                    try:
                        referencia = Etapa.objects.get(pk=int(despues_id), version=version)
                    except (ValueError, Etapa.DoesNotExist):
                        raise ValidationError("El bloque indicado no pertenece a esta ejecución.")
                    plan = _plan_despues_de(referencia)
                    if plan is None:
                        raise ValidationError(
                            "No se puede agregar después de este bloque porque se bifurca: "
                            "defina sus rutas o agregue el bloque al final."
                        )
                else:
                    plan = _plan_al_final(version)
                etapa = ancla.agregar(
                    version, request.user, tipo=tipo, nombre=general_form.cleaned_data["nombre"],
                    descripcion=general_form.cleaned_data.get("descripcion", ""), **config_kwargs,
                )
                if plan is not None:
                    _conectar_bloque_nuevo(ancla, version, etapa, request.user, tipo, plan)
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            if plan is None:
                messages.warning(
                    request,
                    "Bloque agregado sin conectar: el flujo no tiene un final único. "
                    "Enlácelo desde las rutas de otro bloque.",
                )
            else:
                messages.success(request, "Bloque agregado.")
        return ancla.volver()

    bloque = get_object_or_404(Etapa, pk=bloque_id)
    version = bloque.version
    editar_form = BloqueEditarForm(request.POST, prefix=f"bloque-{bloque_id}-editar")
    if not editar_form.is_valid():
        messages.error(request, "Revise el nombre del bloque.")
        return ancla.volver()
    tipo = ETIQUETAS_BLOQUE.get(bloque.tipo, bloque.tipo)
    config_kwargs, config_valido = _parsear_configuracion_bloque(request, tipo, bloque_id=bloque_id)
    if not config_valido:
        messages.error(request, "Revise la configuración del bloque.")
        return ancla.volver()
    try:
        with transaction.atomic():
            ancla.editar(
                version, bloque, request.user, nombre=editar_form.cleaned_data["nombre"],
                descripcion=editar_form.cleaned_data.get("descripcion", ""),
            )
            if tipo != "DECISION":
                ancla.configurar(version, bloque, request.user, **config_kwargs)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Bloque actualizado.")
    return ancla.volver()


def _h_bloque_eliminar(request, ancla, bloque_id):
    bloque = get_object_or_404(Etapa, pk=bloque_id)
    version = bloque.version
    try:
        with transaction.atomic():
            entrantes = list(bloque.transiciones_entrantes.all())
            salientes = list(bloque.transiciones_salientes.select_related("etapa_destino"))
            # Caso seguro (A→X→B): un bloque lineal con exactamente una
            # entrada y una salida se "puentea" — A pasa a apuntar a B, así el
            # flujo queda continuo y editable. En cualquier otro caso
            # (bifurcaciones, varias entradas) no se adivina: solo se
            # desconecta, como antes.
            if (
                bloque.tipo in (Etapa.Tipo.TAREA, Etapa.Tipo.ESPERA)
                and len(entrantes) == 1
                and len(salientes) == 1
                and salientes[0].etapa_destino_id != bloque.pk
                and entrantes[0].etapa_origen_id != bloque.pk
                and entrantes[0].etapa_origen_id != salientes[0].etapa_destino_id
            ):
                entrante, saliente = entrantes[0], salientes[0]
                destino = saliente.etapa_destino
                ancla.desconectar(version, saliente, request.user)
                ancla.conectar(
                    version, entrante.etapa_origen, destino, request.user,
                    conexion=entrante, **_reglas_de(entrante),
                )
            else:
                for transicion in entrantes + salientes:
                    ancla.desconectar(version, transicion, request.user)
            ancla.eliminar(version, bloque, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Bloque eliminado.")
    return ancla.volver()


def _h_ruta_aprobacion_guardar(request, ancla, bloque_id):
    bloque = get_object_or_404(Etapa, pk=bloque_id, tipo=Etapa.Tipo.APROBACION)
    version = bloque.version
    destinos = _destinos_choices(version, excluir_pk=bloque.pk)
    form = RutaAprobacionForm(request.POST, destinos=destinos, prefix=f"bloque-{bloque_id}-ruta")
    if not form.is_valid():
        messages.error(request, "Revise las rutas de la aprobación.")
        return ancla.volver()
    datos = form.cleaned_data
    mapa = {
        "APROBADA": datos["destino_aprobada"], "RECHAZADA": datos["destino_rechazada"],
        "DEVUELTA": datos.get("destino_devuelta") or None,
    }
    try:
        with transaction.atomic():
            existentes = {t.resultado_aprobacion: t for t in bloque.transiciones_salientes.all()}
            for resultado, destino_pk in mapa.items():
                if destino_pk is None:
                    conexion = existentes.get(resultado)
                    if conexion is not None:
                        ancla.desconectar(version, conexion, request.user)
                    continue
                destino = get_object_or_404(Etapa, pk=destino_pk, version=version)
                ancla.conectar(
                    version, bloque, destino, request.user,
                    conexion=existentes.get(resultado), resultado_aprobacion=resultado,
                )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Rutas actualizadas.")
    return ancla.volver()


def _h_condicional_guardar(request, ancla, bloque_id, condicional_id=None):
    bloque = get_object_or_404(Etapa, pk=bloque_id, tipo=Etapa.Tipo.CONDICION)
    version = bloque.version
    destinos = _destinos_choices(version, excluir_pk=bloque.pk)
    prefix = f"bloque-{bloque_id}-cond-{condicional_id}" if condicional_id is not None else f"bloque-{bloque_id}-cond-nuevo"
    form = CondicionalForm(request.POST, destinos=destinos, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise la condición.")
        return ancla.volver()
    datos = form.cleaned_data
    destino = get_object_or_404(Etapa, pk=datos["destino"], version=version)
    conexion = None
    if condicional_id is not None:
        conexion = get_object_or_404(TransicionEtapa, pk=condicional_id, etapa_origen=bloque, es_fallback=False)
    try:
        ancla.conectar(
            version, bloque, destino, request.user, conexion=conexion,
            variable=datos["variable"], operador=datos["operador"], valor=datos["valor"], prioridad=datos["prioridad"],
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Condición guardada.")
    return ancla.volver()


def _h_condicional_eliminar(request, ancla, bloque_id, condicional_id):
    bloque = get_object_or_404(Etapa, pk=bloque_id, tipo=Etapa.Tipo.CONDICION)
    conexion = get_object_or_404(TransicionEtapa, pk=condicional_id, etapa_origen=bloque, es_fallback=False)
    try:
        ancla.desconectar(bloque.version, conexion, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Condición eliminada.")
    return ancla.volver()


def _h_fallback_guardar(request, ancla, bloque_id):
    bloque = get_object_or_404(Etapa, pk=bloque_id, tipo=Etapa.Tipo.CONDICION)
    version = bloque.version
    destinos = _destinos_choices(version, excluir_pk=bloque.pk)
    form = FallbackForm(request.POST, destinos=destinos, prefix=f"bloque-{bloque_id}-fallback")
    if not form.is_valid():
        messages.error(request, "Revise el destino.")
        return ancla.volver()
    destino = get_object_or_404(Etapa, pk=form.cleaned_data["destino"], version=version)
    conexion = bloque.transiciones_salientes.filter(es_fallback=True).first()
    try:
        ancla.conectar(version, bloque, destino, request.user, conexion=conexion, es_fallback=True)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ruta «en otro caso» guardada.")
    return ancla.volver()


def _servicio_para_bloques(request, pk):
    """Gate común de los endpoints de bloques de Studio: solo POST y ambos
    permisos (catálogo + administrar workflows)."""
    if request.method != "POST":
        raise _MetodoNoPermitido
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    return _AnclaServicio(get_object_or_404(Servicio, pk=pk))


class _MetodoNoPermitido(Exception):
    pass


def _con_ancla_de_servicio(manejador):
    """Convierte un handler `_h_*` en la vista de Studio de siempre."""

    def vista(request, pk, *args, **kwargs):
        try:
            ancla = _servicio_para_bloques(request, pk)
        except _MetodoNoPermitido:
            return HttpResponseNotAllowed(["POST"])
        return manejador(request, ancla, *args, **kwargs)

    return vista


def _configuracion_operativa_o_error(servicio):
    version = _configuracion_ejecucion_editable(servicio)
    if version is None:
        raise ValidationError("No hay una configuracion operativa en borrador.")
    return version


def _parsear_decision_inicial(request, version, errores, *, contenedor=None, rama=""):
    """Condición y rutas que la experiencia de creación envía junto con una DECISION (4.F1).

    `None` si el envío no las trae (creación sin configuración, como antes: el bloque queda
    pendiente y la publicación lo señalará). Si las trae, TODAS son obligatorias —condición,
    «si se cumple» y «si no se cumple»— y `None` + `errores` si algo falta: así nunca se crea
    una decisión que parece lista y no lo está."""
    if "nuevo-cond-variable" not in request.POST:
        return None
    destinos = _destinos_bloque_choices(
        version, contenedor_id=contenedor.pk if contenedor is not None else None, rama=rama
    )
    condicion = CondicionalForm({"nuevo-cond-prioridad": "0", **request.POST.dict()}, destinos=destinos, prefix="nuevo-cond")
    otro_caso = FallbackForm(request.POST, destinos=destinos, prefix="nuevo-fallback")
    valido = condicion.is_valid()
    valido = otro_caso.is_valid() and valido
    if not valido:
        etiquetas = {"variable": "Variable", "operador": "Operador", "valor": "Valor", "destino": "Destino"}
        for formulario, nombre in ((condicion, "Si se cumple"), (otro_caso, "Si no se cumple")):
            for campo, lista in formulario.errors.items():
                etiqueta = nombre if campo == "destino" else etiquetas.get(campo, campo)
                errores.extend(f"{etiqueta}: {mensaje}" for mensaje in lista)
        return None
    datos = condicion.cleaned_data
    destino, finaliza = _resolver_destino_operativo(version, datos["destino"])
    destino_otro, finaliza_otro = _resolver_destino_operativo(version, otro_caso.cleaned_data["destino"])
    return {
        "condicion": {
            "variable": datos["variable"], "operador": datos["operador"], "valor": datos["valor"],
            "prioridad": datos["prioridad"], "destino": destino, "finaliza": finaliza,
        },
        "otro_caso": {"destino": destino_otro, "finaliza": finaliza_otro},
    }


def _parsear_rutas_iniciales(request, version, *, contenedor=None, rama="", errores):
    """`{resultado: (destino | None, finaliza)}` de las rutas elegidas al crear una aprobación (las vacías se
    omiten), `{}` si el envío no las trae, o `None` + `errores` si alguna no es válida."""
    if not any(clave.startswith("nuevo-ruta-") for clave in request.POST):
        return {}
    destinos = _destinos_bloque_choices(
        version, contenedor_id=contenedor.pk if contenedor is not None else None, rama=rama
    )
    form = RutaAprobacionForm(
        request.POST, destinos=destinos, en_recorrido=rama == BloqueOperativo.Rama.ITEM, permite_vacios=True,
        prefix="nuevo-ruta",
    )
    if not form.is_valid():
        errores.append(_errores_de_form(form))
        return None
    datos = form.cleaned_data
    rutas = {}
    for resultado, campo in (("APROBADA", "destino_aprobada"), ("DEVUELTA", "destino_devuelta"), ("RECHAZADA", "destino_rechazada")):
        valor = datos.get(campo) or ""
        if valor:
            rutas[resultado] = _resolver_destino_operativo(version, valor)
    return rutas


def _h_bloque_operativo_guardar(request, servicio, bloque_id=None):
    from apps.catalogo import intenciones

    version = _configuracion_operativa_o_error(servicio)
    intencion = request.POST.get("intencion", "") if bloque_id is None else ""
    if intencion:
        # Fase 4.A — alta guiada: la opción de «¿Qué necesitas que suceda?» decide el tipo de bloque (capa de
        # traducción `apps.catalogo.intenciones`); el resto del alta es la de siempre.
        ambito = (request.POST.get("rama") or "") if request.POST.get("contenedor_id") else ""
        try:
            tipo_intencion = intenciones.exigir_disponible(servicio, intencion, ambito=ambito, configuracion=version)
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
            return redirect(_volver(servicio.pk, "ejecucion"))
        datos = request.POST.copy()
        datos["nuevo-tipo"] = tipo_intencion
        request.POST = datos
    if bloque_id is None:
        general_form = BloqueGeneralForm(request.POST, prefix="nuevo")
        if not general_form.is_valid():
            messages.error(request, "Revisa la actividad: " + _errores_de_form(general_form))
            return redirect(_volver(servicio.pk, "ejecucion"))
        # Fase 2: un bloque puede agregarse DENTRO de un contenedor (recorrido por ítem / actividades en
        # paralelo), en una rama; entonces va en la fase de su contenedor.
        contenedor, rama = None, ""
        if request.POST.get("contenedor_id"):
            contenedor = get_object_or_404(
                BloqueOperativo, pk=request.POST.get("contenedor_id"), version=version,
                tipo__in=BloqueOperativo.TIPOS_CONTENEDOR,
            )
            rama = request.POST.get("rama") or ""
            fase = contenedor.fase
        else:
            try:
                fase = get_object_or_404(FaseWorkflow, pk=int(request.POST.get("fase_id", "")), version=version.workflow_version)
            except ValueError:
                messages.error(request, "Seleccione una fase valida.")
                return redirect(_volver(servicio.pk, "ejecucion"))
        tipo = general_form.cleaned_data["tipo"]
        errores = []
        config_kwargs, config_valido = _parsear_configuracion_bloque(
            request, tipo, servicio=servicio, version=version, errores=errores
        )
        if not config_valido:
            messages.error(request, "; ".join(errores) or "Revise la configuracion del bloque.")
            return redirect(_volver(servicio.pk, "ejecucion"))
        if intencion:
            try:
                intenciones.exigir_definicion_coherente(intencion, config_kwargs.get("definicion"))
            except ValidationError as exc:
                messages.error(request, _mensaje_error(exc))
                return redirect(_volver(servicio.pk, "ejecucion"))
        configuracion = _serializar_configuracion_bloque_operativo(tipo, config_kwargs)
        cierre = _leer_cierre_de_alcance(request, "nuevo-config", _opciones_bloques(servicio))
        if cierre:
            configuracion["cierra_alcance"] = cierre
        rutas = None
        if tipo == BloqueOperativo.Tipo.APROBACION and rama != BloqueOperativo.Rama.GENERAL:
            # Fase 4.A: las rutas de una aprobación también se pueden elegir al crearla (como al editarla); las
            # que queden «Definir después» se configuran luego y la publicación las señala.
            rutas = _parsear_rutas_iniciales(request, version, contenedor=contenedor, rama=rama, errores=errores)
            if rutas is None and errores:
                messages.error(request, "Revisa las rutas: " + "; ".join(errores))
                return redirect(_volver(servicio.pk, "ejecucion"))
        decision = None
        if tipo == BloqueOperativo.Tipo.DECISION:
            decision = _parsear_decision_inicial(request, version, errores, contenedor=contenedor, rama=rama)
            if decision is None and errores:
                messages.error(request, "Completa la decisión: " + "; ".join(errores))
                return redirect(_volver(servicio.pk, "ejecucion"))
        try:
            # Todo o nada: la decisión y sus rutas se crean juntas; si algo es inválido no
            # queda un bloque a medias.
            with transaction.atomic():
                bloque = agregar_bloque_operativo(
                    version,
                    request.user,
                    fase=fase,
                    tipo=tipo,
                    nombre=general_form.cleaned_data["nombre"],
                    descripcion=general_form.cleaned_data.get("descripcion", ""),
                    configuracion=configuracion,
                    definicion_entregable=config_kwargs.get("definicion"),
                    entregable_revisado=config_kwargs.get("entregable_revisado"),
                    contenedor=contenedor,
                    rama=rama,
                )
                for resultado, (destino, finaliza) in (rutas or {}).items():
                    conectar_bloques_operativos(
                        bloque, destino, request.user, resultado_aprobacion=resultado, finaliza=finaliza
                    )
                if decision is not None:
                    condicion, otro_caso = decision["condicion"], decision["otro_caso"]
                    conectar_bloques_operativos(bloque, condicion.pop("destino"), request.user, **condicion)
                    conectar_bloques_operativos(
                        bloque, otro_caso["destino"], request.user, es_fallback=True, finaliza=otro_caso["finaliza"]
                    )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Decisión agregada y configurada." if decision is not None else "Bloque agregado.")
        return redirect(_volver(servicio.pk, "ejecucion"))

    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version)
    editar_form = BloqueEditarForm(request.POST, prefix=f"bloque-{bloque_id}-editar")
    if not editar_form.is_valid():
        messages.error(request, "Revisa la actividad: " + _errores_de_form(editar_form))
        return redirect(_volver(servicio.pk, "ejecucion"))
    errores = []
    config_kwargs, config_valido = _parsear_configuracion_bloque(
        request, bloque.tipo, bloque_id=bloque_id, servicio=servicio, incluir_pk=bloque.definicion_entregable_id,
        version=version, errores=errores,
    )
    if not config_valido:
        messages.error(request, "; ".join(errores) or "Revise la configuracion del bloque.")
        return redirect(_volver(servicio.pk, "ejecucion"))
    extra = {}
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        extra["entregable_revisado"] = config_kwargs.get("entregable_revisado")  # None = aprobación general
    configuracion = _serializar_configuracion_bloque_operativo(bloque.tipo, config_kwargs)
    cierre = _leer_cierre_de_alcance(request, f"bloque-{bloque_id}-config", _opciones_bloques(servicio))
    if cierre:
        configuracion["cierra_alcance"] = cierre
    try:
        editar_bloque_operativo(
            version,
            bloque,
            request.user,
            nombre=editar_form.cleaned_data["nombre"],
            descripcion=editar_form.cleaned_data.get("descripcion", ""),
            configuracion=configuracion,
            definicion_entregable=config_kwargs.get("definicion"),
            **extra,
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Bloque actualizado.")
    return redirect(_volver(servicio.pk, "ejecucion"))


def _h_bloque_operativo_eliminar(request, servicio, bloque_id):
    version = _configuracion_operativa_o_error(servicio)
    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version)
    try:
        eliminar_bloque_operativo(version, bloque, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Bloque eliminado.")
    return redirect(_volver(servicio.pk, "ejecucion"))


def _h_ruta_aprobacion_operativa(request, servicio, bloque_id):
    version = _configuracion_operativa_o_error(servicio)
    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version, tipo=BloqueOperativo.Tipo.APROBACION)
    destinos = _destinos_de(bloque)
    en_recorrido = bloque.rama == BloqueOperativo.Rama.ITEM
    if bloque.rama == BloqueOperativo.Rama.GENERAL:
        messages.error(request, "Una aprobación general en paralelo no lleva rutas.")
        return redirect(_volver(servicio.pk, "ejecucion"))
    form = RutaAprobacionForm(
        request.POST, destinos=destinos, en_recorrido=en_recorrido, prefix=f"bloque-{bloque_id}-ruta"
    )
    if not form.is_valid():
        messages.error(request, "Revisa las rutas de la aprobación: " + _errores_de_form(form))
        return redirect(_volver(servicio.pk, "ejecucion"))
    datos = form.cleaned_data
    mapa = {
        "APROBADA": datos.get("destino_aprobada") or None,
        # Fase 2: en el recorrido de un ítem, rechazar (o excluir) termina su recorrido: sin ruta.
        "RECHAZADA": None if en_recorrido else datos["destino_rechazada"],
        "DEVUELTA": datos.get("destino_devuelta") or None,
    }
    try:
        existentes = {t.resultado_aprobacion: t for t in bloque.transiciones_salientes.all()}
        for resultado, destino_pk in mapa.items():
            existente = existentes.get(resultado)
            if destino_pk is None:
                if existente is not None:
                    eliminar_transicion_bloque_operativo(existente, request.user)
                continue
            destino, finaliza = _resolver_destino_operativo(version, destino_pk)
            if existente is None:
                conectar_bloques_operativos(
                    bloque, destino, request.user, resultado_aprobacion=resultado, finaliza=finaliza
                )
            else:
                editar_transicion_bloque_operativo(
                    existente, request.user, destino=destino, resultado_aprobacion=resultado, finaliza=finaliza
                )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Rutas de aprobacion actualizadas.")
    return redirect(_volver(servicio.pk, "ejecucion"))


def _h_condicional_operativa(request, servicio, bloque_id, condicional_id=None):
    version = _configuracion_operativa_o_error(servicio)
    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version, tipo=BloqueOperativo.Tipo.DECISION)
    destinos = _destinos_de(bloque)
    prefix = f"bloque-{bloque_id}-cond-{condicional_id}" if condicional_id else f"bloque-{bloque_id}-cond-nuevo"
    form = CondicionalForm(request.POST, destinos=destinos, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revisa la condición: " + _errores_de_form(form))
        return redirect(_volver(servicio.pk, "ejecucion"))
    datos = form.cleaned_data
    destino, finaliza = _resolver_destino_operativo(version, datos["destino"])
    try:
        if condicional_id is None:
            conectar_bloques_operativos(
                bloque,
                destino,
                request.user,
                variable=datos["variable"],
                operador=datos["operador"],
                valor=datos["valor"],
                prioridad=datos["prioridad"],
                finaliza=finaliza,
            )
        else:
            transicion = get_object_or_404(
                TransicionBloqueOperativo, pk=condicional_id, bloque_origen=bloque, es_fallback=False
            )
            editar_transicion_bloque_operativo(
                transicion,
                request.user,
                destino=destino,
                variable=datos["variable"],
                operador=datos["operador"],
                valor=datos["valor"],
                prioridad=datos["prioridad"],
                es_fallback=False,
                resultado_aprobacion="",
                finaliza=finaliza,
            )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Condicion guardada.")
    return redirect(_volver(servicio.pk, "ejecucion"))


def _h_condicional_operativa_eliminar(request, servicio, bloque_id, condicional_id):
    version = _configuracion_operativa_o_error(servicio)
    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version, tipo=BloqueOperativo.Tipo.DECISION)
    transicion = get_object_or_404(
        TransicionBloqueOperativo, pk=condicional_id, bloque_origen=bloque, es_fallback=False
    )
    try:
        eliminar_transicion_bloque_operativo(transicion, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Condicion eliminada.")
    return redirect(_volver(servicio.pk, "ejecucion"))


def _h_fallback_operativo(request, servicio, bloque_id):
    version = _configuracion_operativa_o_error(servicio)
    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version, tipo=BloqueOperativo.Tipo.DECISION)
    destinos = _destinos_de(bloque)
    form = FallbackForm(request.POST, destinos=destinos, prefix=f"bloque-{bloque_id}-fallback")
    if not form.is_valid():
        messages.error(request, "Revisa la ruta alternativa: " + _errores_de_form(form))
        return redirect(_volver(servicio.pk, "ejecucion"))
    destino, finaliza = _resolver_destino_operativo(version, form.cleaned_data["destino"])
    existente = bloque.transiciones_salientes.filter(es_fallback=True).first()
    try:
        if existente is None:
            conectar_bloques_operativos(bloque, destino, request.user, es_fallback=True, finaliza=finaliza)
        else:
            editar_transicion_bloque_operativo(
                existente,
                request.user,
                destino=destino,
                variable="",
                operador="",
                valor="",
                es_fallback=True,
                resultado_aprobacion="",
                finaliza=finaliza,
            )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Ruta alternativa guardada.")
    return redirect(_volver(servicio.pk, "ejecucion"))


@login_required
def studio_bloque_guardar_view(request, pk, bloque_id=None):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_bloque_operativo_guardar(request, servicio, bloque_id)
    return _con_ancla_de_servicio(_h_bloque_guardar)(request, pk, bloque_id)


@login_required
def studio_bloque_eliminar_view(request, pk, bloque_id):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_bloque_operativo_eliminar(request, servicio, bloque_id)
    return _con_ancla_de_servicio(_h_bloque_eliminar)(request, pk, bloque_id)


@login_required
def studio_ruta_aprobacion_guardar_view(request, pk, bloque_id):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_ruta_aprobacion_operativa(request, servicio, bloque_id)
    return _con_ancla_de_servicio(_h_ruta_aprobacion_guardar)(request, pk, bloque_id)


@login_required
def studio_condicional_guardar_view(request, pk, bloque_id, condicional_id=None):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_condicional_operativa(request, servicio, bloque_id, condicional_id)
    return _con_ancla_de_servicio(_h_condicional_guardar)(request, pk, bloque_id, condicional_id)


@login_required
def studio_condicional_eliminar_view(request, pk, bloque_id, condicional_id):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_condicional_operativa_eliminar(request, servicio, bloque_id, condicional_id)
    return _con_ancla_de_servicio(_h_condicional_eliminar)(request, pk, bloque_id, condicional_id)


@login_required
def studio_fallback_guardar_view(request, pk, bloque_id):
    servicio = get_object_or_404(Servicio, pk=pk)
    if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
            raise PermissionDenied
        return _h_fallback_operativo(request, servicio, bloque_id)
    return _con_ancla_de_servicio(_h_fallback_guardar)(request, pk, bloque_id)


@login_required
def studio_bloque_mover_view(request, pk, bloque_id):
    """Fase 4.A — sube o baja una actividad dentro de su fase (cambia el orden de ejecución; solo en borrador)."""
    from apps.catalogo.configuracion_ejecucion import mover_bloque_operativo

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        version = _configuracion_operativa_o_error(servicio)
        bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version)
        mover_bloque_operativo(version, bloque, request.user, direccion=-1 if request.POST.get("direccion") == "subir" else 1)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Orden actualizado en el borrador (se aplica al activar o publicar).")
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_reconciliar_view(request, pk):
    """Fase 4.A — actualiza la configuración del Servicio a la versión vigente de su flujo conservando bloques y
    rutas. Las fases que ya no existen exigen una decisión explícita por fase (`fase-<pk>` = fase vigente o
    QUITAR). Crea un borrador; la configuración activa no cambia hasta activarlo o publicar."""
    from apps.catalogo import reconciliacion

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    resoluciones = {
        clave[len("fase-"):]: valor for clave, valor in request.POST.items() if clave.startswith("fase-") and valor
    }
    try:
        nueva = reconciliacion.reconciliar(servicio, request.user, resoluciones=resoluciones)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(
            request,
            f"Configuración actualizada al flujo vigente como borrador v{nueva.numero}: revisa y actívala o publica.",
        )
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_bloque_fase_view(request, pk, bloque_id):
    """Fase 4.B — cambia una actividad de fase (operación FUNCIONAL: queda al final de la fase destino). El
    lienzo la pide con confirmación explícita; aquí se revalida todo en el dominio."""
    from apps.catalogo.configuracion_ejecucion import cambiar_fase_bloque_operativo

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        version = _configuracion_operativa_o_error(servicio)
        bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version)
        fase = get_object_or_404(FaseWorkflow, pk=request.POST.get("fase_id") or 0, version=version.workflow_version)
        cambiar_fase_bloque_operativo(version, bloque, request.user, fase=fase)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, f"«{bloque.nombre}» ahora se ejecuta al final de «{fase.nombre}» (en el borrador).")
    return redirect(_volver(pk, "ejecucion"))


@login_required
def studio_lienzo_posiciones_view(request, pk):
    """Fase 4.B — guarda POSICIONES VISUALES del lienzo (JSON). No cambia la ejecución ni la configuración."""
    import json

    from django.http import JsonResponse

    from apps.catalogo import lienzo

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and puede_administrar_workflows(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        cuerpo = json.loads(request.body or b"{}")
        guardadas = lienzo.guardar_posiciones(servicio, cuerpo.get("posiciones"))
    except (ValueError, AttributeError):
        return JsonResponse({"ok": False, "error": "Solicitud no válida."}, status=400)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "error": _mensaje_error(exc)}, status=400)
    return JsonResponse({"ok": True, "guardadas": guardadas})


@login_required
def studio_campo_mover_view(request, pk, campo_id, uso=USO_ENTRADA):
    """Fase 4.B — sube o baja un campo/columna en el borrador del formulario (operación segura de dominio)."""
    from apps.catalogo.versionamiento import mover_campo

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio, uso, _coleccion(request))
    if version is None:
        messages.error(request, "No hay un borrador editable: pulsa «Editar formulario» para crear uno.")
        return redirect(_volver_entrada(pk, uso, _coleccion(request)))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    try:
        mover_campo(version, campo, request.user, direccion=-1 if request.POST.get("direccion") == "subir" else 1)
    except (ValueError, ValidationError) as exc:
        messages.error(request, _mensaje_error(exc) if isinstance(exc, ValidationError) else str(exc))
    else:
        messages.success(request, "Orden actualizado en el borrador.")
    return redirect(_volver_entrada(pk, uso, _coleccion(request)))


# --- SALIDA -----------------------------------------------------------


@login_required
def studio_entregable_guardar_view(request, pk, definicion_id=None):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    definicion = get_object_or_404(DefinicionEntregable, pk=definicion_id, servicio=servicio) if definicion_id is not None else None
    prefix = f"entregable-{definicion_id}-editar" if definicion_id is not None else "entregable-nuevo"
    form = DefinicionEntregableForm(request.POST, prefix=prefix, servicio=servicio)
    if not form.is_valid():
        messages.error(request, "Revisa el resultado: " + _errores_de_form(form))
        return redirect(_volver(pk, "salida"))
    try:
        configurar_definicion_entregable(
            servicio, request.user, definicion=definicion, nombre=form.cleaned_data["nombre"],
            descripcion=form.cleaned_data.get("descripcion", ""), tipo=form.cleaned_data["tipo"],
            obligatorio=form.cleaned_data.get("obligatorio", False), orden=form.cleaned_data.get("orden", 0),
            columnas_revisadas=form.cleaned_data.get("columnas_revisadas"),
            revisores_usuarios=form.cleaned_data.get("revisores_usuarios"),
            revisores_equipos=form.cleaned_data.get("revisores_equipos"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Entregable guardado.")
    return redirect(_volver(pk, "salida"))


@login_required
def studio_entregable_retirar_view(request, pk, definicion_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    definicion = get_object_or_404(DefinicionEntregable, pk=definicion_id, servicio=servicio)
    try:
        retirar_definicion_entregable(definicion, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Entregable retirado.")
    return redirect(_volver(pk, "salida"))


# --- TÉRMINOS DE BÚSQUEDA (4.D) -----------------------------------------------


@login_required
def studio_termino_guardar_view(request, pk, termino_id=None):
    """Alta o edición de un término de búsqueda (Básico). No toca el formulario del
    Servicio: son metadatos para descubrirlo desde "¿Qué necesitas?"."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    termino = get_object_or_404(TerminoServicio, pk=termino_id, servicio=servicio) if termino_id is not None else None
    prefix = f"termino-{termino_id}-editar" if termino_id is not None else "termino-nuevo"
    form = TerminoServicioForm(request.POST, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise el término: " + "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "general"))
    try:
        if termino is None:
            crear_termino(servicio, request.user, form.cleaned_data["termino"])
        else:
            editar_termino(termino, request.user, form.cleaned_data["termino"])
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Término guardado.")
    return redirect(_volver(pk, "general"))


@login_required
def studio_termino_estado_view(request, pk, termino_id):
    """Activa o desactiva un término (`activo=1` / `activo=0`)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    termino = get_object_or_404(TerminoServicio, pk=termino_id, servicio_id=pk)
    activo = request.POST.get("activo") == "1"
    cambiar_estado_termino(termino, request.user, activo=activo)
    messages.success(request, "Término activado." if activo else "Término desactivado.")
    return redirect(_volver(pk, "general"))


@login_required
def studio_termino_eliminar_view(request, pk, termino_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    termino = get_object_or_404(TerminoServicio, pk=termino_id, servicio_id=pk)
    eliminar_termino(termino, request.user)
    messages.success(request, "Término eliminado.")
    return redirect(_volver(pk, "general"))


# --- PUBLICACIÓN -----------------------------------------------------------


@login_required
def studio_publicar_view(request, pk):
    """Fase 4.A — «Publicar» / «Publicar cambios»: activa en una sola transacción los borradores pendientes
    (formulario de inicio, colecciones, configuración del flujo) y publica; si algo falla no cambia nada. El
    mensaje dice exactamente qué entró en vigor (nunca «publicado» si no cambió nada). El flujo LEGACY conserva su
    publicación con confirmación reforzada."""
    from apps.catalogo import publicacion

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    estaba_activo = servicio.activo
    try:
        if _es_legacy(servicio):
            version = _version_ejecucion_editable(servicio)
            if version is not None:
                if not puede_administrar_workflows(request.user):
                    raise PermissionDenied
                confirmado = (
                    request.POST.get("confirmo_compartido") == "1"
                    and request.POST.get("confirmacion_nombre", "").strip() == servicio.workflow.nombre
                )
                publicar_ejecucion(servicio, version, request.user, confirmar_impacto_compartido=confirmado)
            else:
                activar_servicio(servicio, request.user)
            activadas = None
        else:
            activadas = publicacion.publicar_cambios(servicio, request.user, activar=activar_servicio)
    except ValidationError as exc:
        mensajes = [m for m in (_traducir_error_ejecucion(msg) for msg in exc.messages) if m] or exc.messages
        messages.error(request, "No se publicó nada: " + "; ".join(mensajes))
    except PermissionDenied as exc:
        messages.error(request, "No se publicó nada: " + (str(exc) or "no tienes permiso para publicar estos cambios."))
    else:
        if activadas:
            messages.success(request, "Publicado. Entraron en vigor: " + ", ".join(activadas) + ".")
        elif activadas is not None and estaba_activo:
            messages.info(request, "No había cambios pendientes: la publicación sigue igual.")
        else:
            messages.success(request, "Publicado correctamente.")
    return redirect(_volver(pk, "publicacion"))


# --- VISIBILIDAD (General) y RESPONSABLES (Publicación) — 4.4.1 ------------


@login_required
def studio_visibilidad_conceder_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = VisibilidadForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "general"))
    datos = form.cleaned_data
    try:
        conceder_visibilidad(
            servicio, request.user, tipo_alcance=datos["tipo_alcance"], usuario=datos.get("usuario"),
            area=datos.get("area"), unidad_negocio=datos.get("unidad_negocio"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Visibilidad concedida.")
    return redirect(_volver(pk, "general"))


@login_required
def studio_visibilidad_retirar_view(request, pk, concesion_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    concesion = get_object_or_404(ServicioVisibilidad, pk=concesion_id, servicio=servicio)
    retirar_visibilidad(concesion, request.user)
    messages.success(request, "Visibilidad retirada.")
    return redirect(_volver(pk, "general"))


def _tab_de_origen(request):
    """Sección del Studio desde la que se envió el formulario (Básico en un Proceso, Publicar por defecto)."""
    tab = request.POST.get("tab")
    return tab if tab in ("general", "publicacion") else "publicacion"


@login_required
def studio_responsable_agregar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    tab = _tab_de_origen(request)
    form = ResponsableForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, tab))
    datos = form.cleaned_data
    try:
        agregar_responsable(
            servicio, request.user, tipo_responsable=datos["tipo_responsable"],
            usuario=datos.get("usuario"), equipo=datos.get("equipo"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Responsable agregado.")
    return redirect(_volver(pk, tab))


@login_required
def studio_responsable_retirar_view(request, pk, responsable_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    responsable = get_object_or_404(ServicioResponsable, pk=responsable_id, servicio=servicio)
    try:
        retirar_responsable(responsable, request.user)
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Responsable retirado.")
    return redirect(_volver(pk, _tab_de_origen(request)))


@login_required
def studio_entrega_guardar_view(request, pk):
    """4.5 — política de entrega, dentro de la pestaña Salida."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = PoliticaEntregaForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "salida"))
    try:
        configurar_politica_entrega(
            servicio, request.user, politica=form.cleaned_data["politica"],
            dias_observacion=form.cleaned_data.get("dias_observacion"),
        )
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Política de entrega guardada. Aplica a los tickets que se creen desde ahora.")
    return redirect(_volver(pk, "salida"))
