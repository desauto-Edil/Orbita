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
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.catalogo.entregables import configurar_definicion_entregable, retirar_definicion_entregable
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
    CondicionalForm,
    DefinicionEntregableForm,
    EntregableConfigForm,
    EsperaConfigForm,
    FallbackForm,
    FormularioForm,
    OpcionCampoForm,
    ParticipanteAprobacionFormSet,
    PoliticaEntregaForm,
    PoliticaProrrogaForm,
    ReglaCondicionalForm,
    ResponsableForm,
    RutaAprobacionForm,
    ServicioCreacionForm,
    ServicioGeneralForm,
    TIPOS_BLOQUE_CHOICES,
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
    configurar_politica_entrega,
    configurar_politica_prorroga,
    configurar_tiempo_objetivo,
    crear_categoria,
    crear_servicio,
    editar_servicio_general,
    retirar_responsable,
    retirar_visibilidad,
    validar_publicacion,
)
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.core.autorizacion import usuario_tiene_permiso
from apps.workflows.autorizacion import puede_administrar_workflows, puede_vincular_workflows
from apps.workflows.models import Etapa, FaseWorkflow, TransicionEtapa, Workflow, WorkflowVersion
from apps.workflows.variables import referencias_disponibles

# Orden de la dependencia natural (4.E2): el formulario, luego lo que el servicio ENTREGA (los
# bloques ENTREGABLE del flujo eligen entre esas definiciones) y por último el flujo.
TABS = ("general", "entrada", "salida", "ejecucion", "publicacion")
TAB_LABELS = {
    "general": "Basico",
    "entrada": "Formulario",
    "ejecucion": "Flujo",
    "salida": "Salida",
    "publicacion": "Publicar",
}

ETIQUETAS_BLOQUE = {v: k for k, v in TIPOS_BLOQUE.items()}
ETIQUETAS_BLOQUE_DISPLAY = {
    "ACTIVIDAD": "Actividad", "ENTREGABLE": "Entregable", "APROBACION": "Aprobación", "ESPERA": "Espera",
    "DECISION": "Decisión",
}


def _puede_catalogo(usuario):
    return usuario_tiene_permiso(usuario, "catalogo.administrar")


def _puede_formulario(usuario):
    return usuario_tiene_permiso(usuario, "formulario.administrar")


def _mensaje_error(exc):
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _volver(pk, tab):
    return f"{reverse('catalogo:studio', args=[pk])}?tab={tab}"


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


@login_required
def studio_crear_view(request):
    """4.4.1, brecha 1 — alta de un Servicio/Proceso sin Django Admin. Crea
    solo el BORRADOR (sin Workflow/Formulario/Entregables) y redirige a su
    pestaña General para completarlo."""
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = ServicioCreacionForm(request.POST)
        if form.is_valid():
            datos = form.cleaned_data
            try:
                with transaction.atomic():
                    categoria = datos["categoria"] or crear_categoria(request.user, nombre=datos["categoria_nueva"])
                    servicio = crear_servicio(
                        request.user, nombre=datos["nombre"], descripcion=datos.get("descripcion", ""),
                        categoria=categoria, tipo=datos["tipo"], instrucciones=datos.get("instrucciones", ""),
                    )
            except ValidationError as exc:
                form.add_error(None, _mensaje_error(exc))
            else:
                messages.success(request, "Creado como borrador. Complete su configuración en las pestañas.")
                return redirect(_volver(servicio.pk, "general"))
    else:
        form = ServicioCreacionForm(initial={"tipo": Servicio.Tipo.SERVICIO})
    return render(request, "catalogo/studio_crear.html", {"form": form, "titulo_pagina": "Crear en Studio"})


# --- Helpers de composición de contexto -----------------------------------


def _version_entrada_editable(servicio):
    if servicio.formulario_id is None:
        return None
    return servicio.formulario.versiones.filter(estado=FormularioVersion.Estado.BORRADOR).order_by("-numero").first()


def _variables_para_decisiones(servicio, bloques=()):
    """Referencias que una DECISION de este servicio puede usar (4.B0): datos del ticket,
    los campos del formulario de entrada (borrador si lo hay, si no el activo) y el
    resultado de sus bloques de aprobación. Solo ayuda de configuración: no valida."""
    campos = []
    if servicio.formulario_id is not None:
        version = _version_entrada_editable(servicio) or servicio.formulario.version_activa
        if version is not None:
            campos = list(version.campos.all())
    return referencias_disponibles(campos=campos, bloques=bloques)


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


def _resumen_actor(tipo, usuario, equipo):
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


def _parsear_configuracion_bloque(request, tipo, *, bloque_id=None, servicio=None, incluir_pk=None, version=None):
    prefix = f"bloque-{bloque_id}-config" if bloque_id else "nuevo-config"
    if tipo == "ENTREGABLE":
        # Solo un bloque de configuración por fases tiene un Servicio del cual elegir entregables.
        if servicio is None:
            return {}, False
        form = EntregableConfigForm(request.POST, servicio=servicio, incluir_pk=incluir_pk, prefix=prefix)
        if not form.is_valid():
            return {}, False
        return {"definicion": form.cleaned_data["definicion"]}, True
    if tipo == "ACTIVIDAD":
        form = ActividadConfigForm(request.POST, prefix=prefix)
        if not form.is_valid():
            return {}, False
        datos = form.cleaned_data
        return {
            "tipo_actor": datos.get("tipo_actor") or "",
            "usuario": datos.get("usuario"),
            "equipo": datos.get("equipo"),
            "permite_subtareas": datos.get("permite_subtareas", False),
        }, True
    if tipo == "ESPERA":
        form = EsperaConfigForm(request.POST, prefix=prefix)
        if not form.is_valid():
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
        formset = ParticipanteAprobacionFormSet(request.POST, prefix=prefix_participantes)
        if not (form.is_valid() and formset.is_valid()):
            return {}, False
        participantes = []
        for datos_p in formset.cleaned_data:
            if not datos_p or datos_p.get("DELETE"):
                continue
            tipo_p = datos_p["tipo"]
            referencia = datos_p.get("usuario") if tipo_p == "USUARIO" else (datos_p.get("equipo") if tipo_p == "EQUIPO" else None)
            participantes.append((tipo_p, referencia))
        if not participantes:
            return {}, False
        revisa = form.cleaned_data.get("revisa")
        return {
            "modo": form.cleaned_data["modo"],
            "politica": form.cleaned_data.get("politica") or None,
            "participantes": participantes,
            # `None` = aprobación general; solo la configuración por fases (con `version`) lo usa.
            "entregable_revisado": next((b for b in revisables if str(b.pk) == str(revisa)), None) if revisa else None,
        }, True
    if tipo == "DECISION":
        return {}, True
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


def _contexto_general(servicio):
    return {
        "form_general": ServicioGeneralForm(instance=servicio),
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
        **_contexto_terminos(servicio),
    }


def _contexto_entrada(servicio):
    formulario = servicio.formulario
    version_activa = formulario.version_activa if formulario else None
    version_borrador = _version_entrada_editable(servicio)
    version_mostrada = version_borrador or version_activa
    campos, reglas = [], []
    if version_mostrada is not None:
        campos = list(version_mostrada.campos.prefetch_related("opciones").order_by("orden", "id"))
        reglas = list(
            ReglaCondicional.objects.filter(campo_origen__version=version_mostrada).select_related(
                "campo_origen", "campo_objetivo"
            )
        )
        for campo in campos:
            campo.form_editar = CampoForm(instance=campo, prefix=f"campo-{campo.pk}-editar")
            campo.form_opcion_nueva = OpcionCampoForm(prefix=f"campo-{campo.pk}-opcion-nueva")
            for opcion in campo.opciones.all():
                opcion.form_editar = OpcionCampoForm(instance=opcion, prefix=f"campo-{campo.pk}-opcion-{opcion.pk}")
    return {
        "formulario": formulario,
        "version_entrada_activa": version_activa,
        "version_entrada_borrador": version_borrador,
        "version_entrada": version_mostrada,
        "editable_entrada": version_borrador is not None,
        "campos": campos,
        "reglas": reglas,
        "formulario_form": FormularioForm(initial={"nombre": servicio.nombre}) if formulario is None else None,
        "campo_form_nuevo": CampoForm(prefix="campo-nuevo") if version_borrador is not None else None,
        "regla_form_nueva": (
            ReglaCondicionalForm(campos_queryset=Campo.objects.filter(version=version_mostrada), prefix="regla-nueva")
            if version_borrador is not None and campos
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
        return {
            "tipo_actor": config_kwargs.get("tipo_actor") or "",
            "usuario_id": usuario.pk if usuario is not None else None,
            "equipo_id": equipo.pk if equipo is not None else None,
            "permite_subtareas": config_kwargs.get("permite_subtareas", False),
        }
    if tipo == "ESPERA":
        return dict(config_kwargs)
    if tipo == "ENTREGABLE":
        return {}  # la referencia vive en `BloqueOperativo.definicion_entregable`, no en el JSON
    if tipo == "APROBACION":
        participantes = []
        for tipo_p, referencia in config_kwargs.get("participantes", []):
            participantes.append(
                {
                    "tipo": tipo_p,
                    "usuario_id": referencia.pk if tipo_p == "USUARIO" and referencia is not None else None,
                    "equipo_id": referencia.pk if tipo_p == "EQUIPO" and referencia is not None else None,
                }
            )
        return {
            "modo": config_kwargs.get("modo"),
            "politica": config_kwargs.get("politica") or "",
            "participantes": participantes,
        }
    return {}


def _config_inicial_para_formulario(bloque):
    cfg = bloque.configuracion or {}
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        return {
            "tipo_actor": cfg.get("tipo_actor") or "",
            "usuario": cfg.get("usuario_id"),
            "equipo": cfg.get("equipo_id"),
            "permite_subtareas": cfg.get("permite_subtareas", False),
        }
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        return {"modo": cfg.get("modo"), "politica": cfg.get("politica") or ""}
    return cfg


def _participantes_iniciales_operativos(bloque):
    participantes = []
    for item in (bloque.configuracion or {}).get("participantes", []):
        participantes.append(
            {
                "tipo": item.get("tipo"),
                "usuario": item.get("usuario_id"),
                "equipo": item.get("equipo_id"),
            }
        )
    return participantes or None


DESTINO_FINALIZAR = "FIN"


def _destinos_bloque_choices(version_config, *, excluir_pk=None):
    """Bloques a los que puede ir una ruta, más «Finalizar el flujo» (4.E2): un destino especial,
    no un bloque (el vocabulario V1 no incluye un tipo FIN)."""
    bloques = version_config.bloques.select_related("fase").order_by("fase__orden", "orden", "pk")
    if excluir_pk is not None:
        bloques = bloques.exclude(pk=excluir_pk)
    opciones = [(bloque.pk, f"{bloque.fase.nombre} - {bloque.nombre}") for bloque in bloques]
    return opciones + [(DESTINO_FINALIZAR, "Finalizar el flujo")]


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
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        return _resumen_actor(cfg.get("tipo_actor"), None, None)
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
    return "Sin configurar"


def _bloque_operativo_para_presentacion(bloque, editable, bloques_entregable=()):
    fila = {
        "bloque": bloque,
        "tipo_empresarial": bloque.tipo,
        "tipo_empresarial_display": ETIQUETAS_BLOQUE_DISPLAY.get(bloque.tipo, bloque.tipo),
        "resumen": _resumen_bloque_operativo(bloque),
    }
    if not editable:
        return fila
    fila["form_editar"] = BloqueEditarForm(
        initial={"nombre": bloque.nombre, "descripcion": bloque.descripcion},
        prefix=f"bloque-{bloque.pk}-editar",
    )
    if bloque.tipo == BloqueOperativo.Tipo.ACTIVIDAD:
        fila["form_configurar"] = ActividadConfigForm(
            initial=_config_inicial_para_formulario(bloque), prefix=f"bloque-{bloque.pk}-config"
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
        fila["form_configurar"] = AprobacionConfigForm(
            initial={**_config_inicial_para_formulario(bloque), "revisa": bloque.entregable_revisado_id or ""},
            prefix=f"bloque-{bloque.pk}-config",
            bloques_entregable=bloques_entregable,
        )
        fila["formset_participantes"] = ParticipanteAprobacionFormSet(
            initial=_participantes_iniciales_operativos(bloque), prefix=f"bloque-{bloque.pk}-participantes"
        )
        destinos = _destinos_bloque_choices(bloque.version, excluir_pk=bloque.pk)
        rutas = {t.resultado_aprobacion: t for t in bloque.transiciones_salientes.all()}
        fila["rutas"] = rutas
        fila["form_ruta"] = RutaAprobacionForm(
            initial={
                "destino_aprobada": _destino_inicial(rutas["APROBADA"]) if "APROBADA" in rutas else None,
                "destino_devuelta": _destino_inicial(rutas["DEVUELTA"]) if "DEVUELTA" in rutas else "",
                "destino_rechazada": _destino_inicial(rutas["RECHAZADA"]) if "RECHAZADA" in rutas else None,
            },
            destinos=destinos,
            prefix=f"bloque-{bloque.pk}-ruta",
        )
    elif bloque.tipo == BloqueOperativo.Tipo.DECISION:
        salientes = list(bloque.transiciones_salientes.all())
        condicionales = [t for t in salientes if not t.es_fallback]
        fallback = next((t for t in salientes if t.es_fallback), None)
        destinos = _destinos_bloque_choices(bloque.version, excluir_pk=bloque.pk)
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
    editable = config_borrador is not None
    errores_configuracion = validar_configuracion_ejecucion(config_borrador) if config_borrador is not None else []
    bloques_entregable = _bloques_entregable_revisables(config_mostrada)
    fases = []
    for fase in _fases_para_resumen(version_activa):
        bloques = []
        if config_mostrada is not None:
            bloques = [
                _bloque_operativo_para_presentacion(bloque, editable, bloques_entregable)
                for bloque in config_mostrada.bloques.filter(fase=fase)
                .select_related("entregable_revisado")
                .order_by("orden", "pk")
            ]
        fases.append({"fase": fase, "bloques": bloques})
    return {
        "usa_plantilla_fases": True,
        "version_ejecucion_activa": version_activa,
        "version_ejecucion_borrador": None,
        "version_ejecucion": version_activa,
        "configuracion_ejecucion_activa": config_activa,
        "configuracion_ejecucion_borrador": config_borrador,
        "configuracion_ejecucion": config_mostrada,
        "errores_configuracion_ejecucion": errores_configuracion,
        "configuracion_ejecucion_publicable": config_borrador is not None and not errores_configuracion,
        "editable_ejecucion": editable,
        "fases_configuracion": fases,
        "bloques_disponibles": [
            (codigo, etiqueta, TIPOS_BLOQUE_DESCRIPCION[codigo]) for codigo, etiqueta in TIPOS_BLOQUE_CHOICES
        ],
        "variables_decision": _variables_para_decisiones(
            servicio, config_mostrada.bloques.all() if config_mostrada is not None else ()
        ),
        "bloque_general_form": BloqueGeneralForm(prefix="nuevo") if editable else None,
        "form_actividad_nuevo": ActividadConfigForm(prefix="nuevo-config"),
        "form_entregable_nuevo": EntregableConfigForm(servicio=servicio, prefix="nuevo-config"),
        "servicio_sin_entregables": not servicio.definiciones_entregables.filter(activo=True).exists(),
        "form_aprobacion_nuevo": AprobacionConfigForm(prefix="nuevo-config", bloques_entregable=bloques_entregable),
        "formset_participantes_nuevo": ParticipanteAprobacionFormSet(prefix="nuevo-participantes"),
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
        "plantilla_tiene_decisiones": any(f["tipo_empresarial"] == "DECISION" for f in plantilla_bloques),
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
        definicion.form_editar = DefinicionEntregableForm(
            initial={
                "nombre": definicion.nombre, "descripcion": definicion.descripcion, "tipo": definicion.tipo,
                "obligatorio": definicion.obligatorio, "orden": definicion.orden,
            },
            prefix=f"entregable-{definicion.pk}-editar",
        )
    return {
        "definiciones": definiciones,
        "entregable_form_nuevo": DefinicionEntregableForm(prefix="entregable-nuevo"),
        "form_politica": PoliticaEntregaForm(
            initial={"politica": servicio.politica_entrega or None, "dias_observacion": servicio.dias_observacion}
        ),
        "politica_actual": servicio.politica_entrega,
    }


def _contexto_publicacion(servicio):
    checklist = [
        {"ok": bool(servicio.nombre and servicio.categoria_id), "texto": "Información general"},
    ]
    formulario = servicio.formulario
    version_form_activa = formulario.version_activa if formulario else None
    entrada_ok = version_form_activa is not None and version_form_activa.estado == FormularioVersion.Estado.ACTIVA
    checklist.append(
        {"ok": entrada_ok, "texto": "Formulario de entrada" if entrada_ok else "Sin formulario con una versión activa"}
    )
    try:
        validar_publicacion(servicio)
        publicable = True
        errores = []
    except ValidationError as exc:
        publicable = False
        errores = [m for m in (_traducir_error_ejecucion(msg) for msg in exc.messages) if m]

    tiene_entregables = servicio.definiciones_entregables.filter(activo=True).exists()
    checklist.append(
        {"ok": tiene_entregables, "texto": "Entregables configurados" if tiene_entregables else "Sin entregables configurados (opcional)"}
    )
    checklist.append({"ok": publicable, "texto": "Ejecución lista para publicar" if publicable else "La ejecución tiene pendientes (ver abajo)"})

    # 4.4.1 — resumen real de visibilidad y responsables. Informativo: no son
    # requisitos de publicación del dominio (`validar_publicacion` no los
    # exige) y Studio no cambia esa regla — solo avisa.
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

    # 4.5 — informativo (no bloquea): sin política, los Tickets conservan el
    # flujo anterior (resolver y cerrar manualmente, sin entrega formal).
    checklist.append(
        {
            "ok": bool(servicio.politica_entrega),
            "texto": "Entrega al solicitante definida"
            if servicio.politica_entrega
            else "Sin política de entrega: los tickets se resolverán y cerrarán manualmente",
        }
    )

    # Publicar un flujo compartido cambia la ejecución de otros servicios:
    # confirmación reforzada (solo cuando hay un borrador por publicar).
    impacto_compartido = []
    if servicio.workflow_id is not None and _version_ejecucion_editable(servicio) is not None:
        impacto_compartido = list(servicios_que_comparten(servicio.workflow, excluir=servicio))
    return {
        "impacto_compartido": impacto_compartido,
        "checklist": checklist, "errores": errores, "publicable": publicable,
        "concesiones": concesiones, "es_restringido": es_restringido,
        "responsables": responsables, "form_responsable": ResponsableForm(),
    }


# --- Vista principal (GET, dispatch por pestaña) --------------------------


def _estado_secciones(servicio):
    formulario = servicio.formulario
    version_form = formulario.version_activa if formulario else None
    formulario_ok = version_form is not None and version_form.estado == FormularioVersion.Estado.ACTIVA

    workflow = servicio.workflow
    version_wf = workflow.version_activa if workflow else None
    if workflow is None and servicio.tipo == Servicio.Tipo.SERVICIO:
        flujo_estado, flujo_texto = "optional", "Opcional"
    elif workflow is None:
        flujo_estado, flujo_texto = "attention", "Requiere atencion"
    elif workflow.modo == Workflow.Modo.PLANTILLA_FASES:
        config_activa = servicio.configuracion_ejecucion_activa
        config_borrador = _configuracion_ejecucion_editable(servicio)
        if config_activa is not None and config_activa.workflow_version_id == workflow.version_activa_id:
            flujo_estado, flujo_texto = "complete", "Configurada"
        elif config_borrador is not None and not validar_configuracion_ejecucion(config_borrador):
            flujo_estado, flujo_texto = "incomplete", "Lista para activar"
        elif config_activa is not None:
            flujo_estado, flujo_texto = "attention", "Requiere actualizacion"
        else:
            flujo_estado, flujo_texto = "attention", "Requiere configuracion"
    elif version_wf is not None and version_wf.estado == WorkflowVersion.Estado.ACTIVA:
        flujo_estado, flujo_texto = "complete", f"v{version_wf.numero} publicada"
    else:
        flujo_estado, flujo_texto = "attention", "Requiere publicar flujo"

    tiene_salida = (
        servicio.definiciones_entregables.filter(activo=True).exists()
        or bool(servicio.politica_entrega)
    )
    try:
        validar_publicacion(servicio)
        publicable = True
    except ValidationError:
        publicable = False

    basico_ok = servicio.nombre and servicio.categoria_id and servicio.tipo in Servicio.Tipo.values
    return [
        {
            "clave": "general",
            "etiqueta": TAB_LABELS["general"],
            "estado": "complete" if basico_ok else "attention",
            "texto": "Completa" if basico_ok else "Requiere atencion",
        },
        {
            "clave": "entrada",
            "etiqueta": TAB_LABELS["entrada"],
            "estado": "complete" if formulario_ok else "attention",
            "texto": f"v{version_form.numero} activa" if formulario_ok else "Requiere version activa",
        },
        {
            "clave": "salida",
            "etiqueta": TAB_LABELS["salida"],
            "estado": "complete" if tiene_salida else "optional",
            "texto": "Configurada" if tiene_salida else "Opcional segun dominio",
        },
        {"clave": "ejecucion", "etiqueta": TAB_LABELS["ejecucion"], "estado": flujo_estado, "texto": flujo_texto},
        {
            "clave": "publicacion",
            "etiqueta": TAB_LABELS["publicacion"],
            "estado": "complete" if servicio.activo else ("incomplete" if publicable else "attention"),
            "texto": "Publicado" if servicio.activo else ("Listo para publicar" if publicable else "Con pendientes"),
        },
    ]


@login_required
def studio_view(request, pk):
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    tab = request.GET.get("tab")
    if tab not in TABS:
        tab = "general"

    contexto = {
        "servicio": servicio,
        "tab": tab,
        "titulo_pagina": servicio.nombre,
        "secciones": _estado_secciones(servicio),
        "puede_formulario": _puede_formulario(request.user),
        "puede_ejecucion": puede_administrar_workflows(request.user),
        "puede_vincular_workflow": puede_vincular_workflows(request.user),
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
    else:
        contexto.update(constructores[tab](servicio))
    return render(request, "catalogo/studio.html", contexto)


# --- GENERAL ---------------------------------------------------------------


@login_required
def studio_general_guardar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = ServicioGeneralForm(request.POST, instance=servicio)
    if not form.is_valid():
        messages.error(request, "Revise los datos generales.")
        return redirect(_volver(pk, "general"))
    datos = form.cleaned_data
    try:
        editar_servicio_general(
            servicio, request.user, nombre=datos["nombre"], descripcion=datos.get("descripcion", ""),
            categoria=datos["categoria"], tipo=datos["tipo"], instrucciones=datos.get("instrucciones", ""),
            alcance_visibilidad=datos["alcance_visibilidad"],
        )
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
def studio_entrada_version_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        if servicio.formulario_id is None:
            form = FormularioForm(request.POST)
            if not form.is_valid():
                messages.error(request, "Indique un nombre para el formulario.")
                return redirect(_volver(pk, "entrada"))
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
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_entrada_activar_view(request, pk, version_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = get_object_or_404(FormularioVersion, pk=version_id, formulario=servicio.formulario)
    try:
        activar_version(servicio.formulario, version, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Formulario activado.")
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_campo_guardar_view(request, pk, campo_id=None):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    instancia = get_object_or_404(Campo, pk=campo_id, version=version) if campo_id is not None else None
    prefix = f"campo-{campo_id}-editar" if campo_id is not None else "campo-nuevo"
    form = CampoForm(request.POST, instance=instancia, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise los datos del campo.")
        return redirect(_volver(pk, "entrada"))
    campo = form.save(commit=False)
    campo.version = version
    campo.configuracion = form.configuracion_desde_tipo(form.cleaned_data["tipo"])
    try:
        campo.full_clean()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver(pk, "entrada"))
    campo.save()
    messages.success(request, "Campo guardado.")
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_campo_eliminar_view(request, pk, campo_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    try:
        campo.delete()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
    else:
        messages.success(request, "Campo eliminado.")
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_opcion_guardar_view(request, pk, campo_id, opcion_id=None):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    instancia = get_object_or_404(OpcionCampo, pk=opcion_id, campo=campo) if opcion_id is not None else None
    prefix = f"campo-{campo_id}-opcion-{opcion_id}" if opcion_id is not None else f"campo-{campo_id}-opcion-nueva"
    form = OpcionCampoForm(request.POST, instance=instancia, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise los datos de la opción.")
        return redirect(_volver(pk, "entrada"))
    opcion = form.save(commit=False)
    opcion.campo = campo
    try:
        opcion.full_clean()
    except ValidationError as exc:
        messages.error(request, _mensaje_error(exc))
        return redirect(_volver(pk, "entrada"))
    opcion.save()
    messages.success(request, "Opción guardada.")
    return redirect(_volver(pk, "entrada"))


@login_required
def studio_opcion_eliminar_view(request, pk, campo_id, opcion_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not (_puede_catalogo(request.user) and _puede_formulario(request.user)):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    version = _version_entrada_editable(servicio)
    if version is None:
        messages.error(request, "No hay un borrador de entrada editable.")
        return redirect(_volver(pk, "entrada"))
    campo = get_object_or_404(Campo, pk=campo_id, version=version)
    opcion = get_object_or_404(OpcionCampo, pk=opcion_id, campo=campo)
    opcion.delete()
    messages.success(request, "Opción eliminada.")
    return redirect(_volver(pk, "entrada"))


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
        messages.error(request, "Revise la regla condicional.")
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


def _h_bloque_operativo_guardar(request, servicio, bloque_id=None):
    version = _configuracion_operativa_o_error(servicio)
    if bloque_id is None:
        general_form = BloqueGeneralForm(request.POST, prefix="nuevo")
        if not general_form.is_valid():
            messages.error(request, "Revise el nombre y tipo del bloque.")
            return redirect(_volver(servicio.pk, "ejecucion"))
        try:
            fase = get_object_or_404(FaseWorkflow, pk=int(request.POST.get("fase_id", "")), version=version.workflow_version)
        except ValueError:
            messages.error(request, "Seleccione una fase valida.")
            return redirect(_volver(servicio.pk, "ejecucion"))
        tipo = general_form.cleaned_data["tipo"]
        config_kwargs, config_valido = _parsear_configuracion_bloque(request, tipo, servicio=servicio, version=version)
        if not config_valido:
            messages.error(request, "Revise la configuracion del bloque.")
            return redirect(_volver(servicio.pk, "ejecucion"))
        try:
            agregar_bloque_operativo(
                version,
                request.user,
                fase=fase,
                tipo=tipo,
                nombre=general_form.cleaned_data["nombre"],
                descripcion=general_form.cleaned_data.get("descripcion", ""),
                configuracion=_serializar_configuracion_bloque_operativo(tipo, config_kwargs),
                definicion_entregable=config_kwargs.get("definicion"),
                entregable_revisado=config_kwargs.get("entregable_revisado"),
            )
        except ValidationError as exc:
            messages.error(request, _mensaje_error(exc))
        else:
            messages.success(request, "Bloque agregado.")
        return redirect(_volver(servicio.pk, "ejecucion"))

    bloque = get_object_or_404(BloqueOperativo, pk=bloque_id, version=version)
    editar_form = BloqueEditarForm(request.POST, prefix=f"bloque-{bloque_id}-editar")
    if not editar_form.is_valid():
        messages.error(request, "Revise el nombre del bloque.")
        return redirect(_volver(servicio.pk, "ejecucion"))
    config_kwargs, config_valido = _parsear_configuracion_bloque(
        request, bloque.tipo, bloque_id=bloque_id, servicio=servicio, incluir_pk=bloque.definicion_entregable_id,
        version=version,
    )
    if not config_valido:
        messages.error(request, "Revise la configuracion del bloque.")
        return redirect(_volver(servicio.pk, "ejecucion"))
    extra = {}
    if bloque.tipo == BloqueOperativo.Tipo.APROBACION:
        extra["entregable_revisado"] = config_kwargs.get("entregable_revisado")  # None = aprobación general
    try:
        editar_bloque_operativo(
            version,
            bloque,
            request.user,
            nombre=editar_form.cleaned_data["nombre"],
            descripcion=editar_form.cleaned_data.get("descripcion", ""),
            configuracion=_serializar_configuracion_bloque_operativo(bloque.tipo, config_kwargs),
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
    destinos = _destinos_bloque_choices(version, excluir_pk=bloque.pk)
    form = RutaAprobacionForm(request.POST, destinos=destinos, prefix=f"bloque-{bloque_id}-ruta")
    if not form.is_valid():
        messages.error(request, "Revise las rutas de la aprobacion.")
        return redirect(_volver(servicio.pk, "ejecucion"))
    datos = form.cleaned_data
    mapa = {
        "APROBADA": datos["destino_aprobada"],
        "RECHAZADA": datos["destino_rechazada"],
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
    destinos = _destinos_bloque_choices(version, excluir_pk=bloque.pk)
    prefix = f"bloque-{bloque_id}-cond-{condicional_id}" if condicional_id else f"bloque-{bloque_id}-cond-nuevo"
    form = CondicionalForm(request.POST, destinos=destinos, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise la condicion.")
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
    destinos = _destinos_bloque_choices(version, excluir_pk=bloque.pk)
    form = FallbackForm(request.POST, destinos=destinos, prefix=f"bloque-{bloque_id}-fallback")
    if not form.is_valid():
        messages.error(request, "Revise la ruta alternativa.")
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
    form = DefinicionEntregableForm(request.POST, prefix=prefix)
    if not form.is_valid():
        messages.error(request, "Revise los datos del entregable.")
        return redirect(_volver(pk, "salida"))
    try:
        configurar_definicion_entregable(
            servicio, request.user, definicion=definicion, nombre=form.cleaned_data["nombre"],
            descripcion=form.cleaned_data.get("descripcion", ""), tipo=form.cleaned_data["tipo"],
            obligatorio=form.cleaned_data.get("obligatorio", False), orden=form.cleaned_data.get("orden", 0),
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
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    try:
        if servicio.workflow_id is not None and servicio.workflow.modo == Workflow.Modo.PLANTILLA_FASES:
            activar_servicio(servicio, request.user)
        elif servicio.workflow_id is not None:
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
        else:
            activar_servicio(servicio, request.user)
    except ValidationError as exc:
        mensajes = [m for m in (_traducir_error_ejecucion(msg) for msg in exc.messages) if m] or exc.messages
        messages.error(request, "; ".join(mensajes))
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


@login_required
def studio_responsable_agregar_view(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    form = ResponsableForm(request.POST)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errores in form.errors.values() for e in errores))
        return redirect(_volver(pk, "publicacion"))
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
    return redirect(_volver(pk, "publicacion"))


@login_required
def studio_responsable_retirar_view(request, pk, responsable_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _puede_catalogo(request.user):
        raise PermissionDenied
    servicio = get_object_or_404(Servicio, pk=pk)
    responsable = get_object_or_404(ServicioResponsable, pk=responsable_id, servicio=servicio)
    retirar_responsable(responsable, request.user)
    messages.success(request, "Responsable retirado.")
    return redirect(_volver(pk, "publicacion"))


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
