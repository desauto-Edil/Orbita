"""Configuración empresarial de ejecución — Sprint 4.3.

CU-010/CU-020: Servicio.workflow es la única asociación y las Etapas son
los propios bloques. Coordina pertenencia, permisos, borrador y creación
atómica de bloque + configuración. El editor conserva validación y auditoría.
No persiste otro grafo ni distingue un supuesto Workflow «de Studio».

D2 (Diseñador › Flujos): las mismas operaciones de bloques existen también
ancladas a un `Workflow` (`*_en_flujo`), sin Servicio de por medio: se diseña un
flujo reutilizable por sí mismo y solo exige `workflows.administrar` (el
Servicio, cuando existe, exige además `catalogo.administrar`). Ambas variantes
comparten el mismo núcleo (`_agregar_bloque`, `_conectar_bloques`, …): una sola
regla de negocio, dos puntos de entrada. Un Workflow sigue siendo UNA definición
con versiones inmutables; no hay ningún concepto de «flujo de Studio» aparte.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count

from apps.catalogo.models import Servicio
from apps.catalogo.operaciones import _exigir_administracion, activar_servicio
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria
from apps.workflows import editor, versionamiento
from apps.workflows.actores import validar_actor
from apps.workflows.autorizacion import puede_administrar_workflows, puede_vincular_workflows
from apps.workflows.models import Etapa, TransicionEtapa, Workflow, WorkflowVersion


TIPOS_BLOQUE = {
    "ACTIVIDAD": Etapa.Tipo.TAREA,
    "APROBACION": Etapa.Tipo.APROBACION,
    "ESPERA": Etapa.Tipo.ESPERA,
    "DECISION": Etapa.Tipo.CONDICION,
}


def _exigir_configuracion(actor):
    _exigir_administracion(actor)
    # Servicio.workflow puede ser compartido con otros Servicios y con el
    # editor técnico. Administrar catálogo no concede administrar Workflow.
    if not puede_administrar_workflows(actor):
        raise PermissionDenied("Se requiere autorización para administrar la ejecución.")


def _bloquear_version(servicio, version, actor):
    _exigir_configuracion(actor)
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is None:
        raise ValidationError("La definición no tiene una ejecución configurada.")
    # Compartido con crear_nueva_version/activar_version. Relectura bajo
    # lock: un objeto BORRADOR obsoleto no permite editar una versión activa.
    Workflow.objects.select_for_update().get(pk=servicio.workflow_id)
    version = WorkflowVersion.objects.get(pk=version.pk)
    if version.workflow_id != servicio.workflow_id:
        raise ValidationError("La versión no pertenece a la ejecución de esta definición.")
    version.exigir_editable()
    return version


def _exigir_administrar_flujos(actor):
    if not puede_administrar_workflows(actor):
        raise PermissionDenied("Se requiere autorización para administrar flujos.")


def _bloquear_version_de_flujo(workflow, version, actor):
    """Equivalente de `_bloquear_version` cuando el ancla es el propio Flujo."""
    _exigir_administrar_flujos(actor)
    Workflow.objects.select_for_update().get(pk=workflow.pk)
    version = WorkflowVersion.objects.get(pk=version.pk)
    if version.workflow_id != workflow.pk:
        raise ValidationError("La versión no pertenece a este flujo.")
    version.exigir_editable()
    return version


def _asegurar_esqueleto(version, actor):
    """Un flujo nuevo parte de INICIO → FIN para que siempre sea editable."""
    if not version.etapas.exists():
        inicio = editor.crear_etapa(version, actor, tipo=Etapa.Tipo.INICIO, nombre="Inicio")
        fin = editor.crear_etapa(version, actor, tipo=Etapa.Tipo.FIN, nombre="Fin")
        editor.crear_transicion(inicio, fin, actor)


def _etapa(version, bloque, *, permitir_terminales=False):
    etapa = Etapa.objects.get(pk=bloque.pk)
    if etapa.version_id != version.pk:
        raise ValidationError("El bloque no pertenece al borrador indicado.")
    if not permitir_terminales and etapa.tipo not in TIPOS_BLOQUE.values():
        raise ValidationError("Esta operación no modifica los nodos técnicos de inicio/fin.")
    return etapa


@transaction.atomic
def preparar_ejecucion(servicio, actor, *, confirmar_compartido=False):
    """Obtiene un borrador o clona la activa; para un flujo nuevo crea INICIO→FIN.

    Retorna WorkflowVersion. El llamador conecta sus bloques usando las
    transiciones existentes; no se reconstruye ni reordena un grafo implícito.
    No publica ni modifica el valor histórico de Servicio.activo.

    Si el Workflow es compartido con otros servicios y hay que ABRIR un borrador
    nuevo, exige `confirmar_compartido=True`: modificarlo afectará a todos al
    publicarse (alternativa: `crear_copia_de_ejecucion`). Si ya existe un
    borrador, se devuelve tal cual: ya fue una decisión consciente de quien lo
    abrió.
    """
    _exigir_configuracion(actor)
    servicio_original = servicio
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is None:
        workflow = versionamiento.crear_workflow(actor, nombre=servicio.nombre)
        servicio.workflow = workflow
        servicio.save(update_fields=["workflow", "actualizado_en"])
        registrar_evento(
            accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
            origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
            datos_anteriores={"workflow_id": None}, datos_nuevos={"workflow_id": workflow.pk},
        )
    else:
        workflow = Workflow.objects.select_for_update().get(pk=servicio.workflow_id)
    version = workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()
    if version is None:
        otros = list(servicios_que_comparten(workflow, excluir=servicio))
        if otros and not confirmar_compartido:
            raise ValidationError(
                f"Este flujo también lo usan {_nombres(otros)}. Crea una copia propia o confirma que quieres "
                "modificar el flujo compartido."
            )
        version = versionamiento.crear_nueva_version(workflow, actor)
    _asegurar_esqueleto(version, actor)
    servicio_original.refresh_from_db()
    return version


def plantillas_de_ejecucion():
    """Flujos que se pueden elegir para un Servicio/Proceso: los que tienen una
    versión ACTIVA publicada. Un flujo sin versión activa no se ofrece — el
    servicio quedaría ligado a una estructura que nunca fue validada ni
    activada. `n_servicios` informa cuántos servicios lo usan hoy."""
    return (
        Workflow.objects.filter(version_activa__estado=WorkflowVersion.Estado.ACTIVA)
        .select_related("version_activa")
        .annotate(n_servicios=Count("servicios"))
        .order_by("nombre", "pk")
    )


def servicios_que_comparten(workflow, excluir=None):
    """Servicios/Procesos vinculados a `workflow`, sin contar `excluir`.
    Un Workflow es compartido cuando esta consulta no está vacía para
    cualquiera de sus servicios."""
    consulta = Servicio.objects.filter(workflow=workflow).order_by("nombre", "pk")
    if excluir is not None:
        consulta = consulta.exclude(pk=excluir.pk)
    return consulta


def _nombres(servicios):
    return ", ".join(f"«{s.nombre}»" for s in servicios)


@transaction.atomic
def vincular_ejecucion(servicio, actor, workflow):
    """Usa un Workflow YA PUBLICADO como la ejecución de `servicio`.

    El Workflow se COMPARTE (la FK `Servicio.workflow` es 1→N): ningún dato de
    ejecución se comparte, porque cada Ticket crea su propia `InstanciaWorkflow`
    atada a una `WorkflowVersion` concreta (RN-020). Exige poder vincular
    (`workflows.vincular` o `workflows.administrar`) y NO concede modificarlo:
    toda operación de edición sigue exigiendo `workflows.administrar`. No
    publica nada. Retorna la versión activa vinculada."""
    _exigir_administracion(actor)
    if not puede_vincular_workflows(actor):
        raise PermissionDenied("Se requiere autorización para vincular workflows.")
    servicio_original = servicio
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is not None:
        raise ValidationError("Esta definición ya tiene una ejecución configurada.")
    workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
    version = workflow.version_activa
    if version is None or version.estado != WorkflowVersion.Estado.ACTIVA or version.workflow_id != workflow.pk:
        raise ValidationError("El flujo elegido no tiene una versión activa: publícalo antes de usarlo.")
    otros = list(servicios_que_comparten(workflow))
    servicio.workflow = workflow
    servicio.save(update_fields=["workflow", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"workflow_id": None},
        datos_nuevos={"workflow_id": workflow.pk, "vinculo": "EXISTENTE", "version_id": version.pk,
                      "compartido_con": [s.pk for s in otros]},
    )
    servicio_original.refresh_from_db()
    return version


@transaction.atomic
def crear_copia_de_ejecucion(servicio, actor):
    """Desvincula SOLO a `servicio` de un Workflow compartido, dándole una
    copia propia (los demás servicios siguen en el original, que no cambia).

    Si el original tiene versión activa, la copia nace con esa misma
    estructura YA ACTIVA: el servicio —que puede estar publicado y con Tickets
    en curso— no pierde su ejecución mientras se edita; para cambiar algo se
    abre un borrador como en cualquier flujo propio. Si solo hay borrador, la
    copia nace como borrador. Exige poder administrar workflows (crear una
    copia equivale a crear un Workflow que luego se modifica). La clonación es
    la ya existente (`crear_workflow(clonar_desde=…)`)."""
    _exigir_configuracion(actor)
    servicio_original = servicio
    servicio = Servicio.objects.select_for_update().get(pk=servicio.pk)
    if servicio.workflow_id is None:
        raise ValidationError("La definición no tiene una ejecución configurada.")
    origen_wf = Workflow.objects.select_for_update().get(pk=servicio.workflow_id)
    if not servicios_que_comparten(origen_wf, excluir=servicio).exists():
        raise ValidationError("Esta ejecución no se comparte con otros servicios: no hace falta una copia.")
    origen = origen_wf.version_activa or origen_wf.versiones.order_by("-numero").first()
    if origen is None:
        raise ValidationError("El flujo no tiene versiones que copiar.")
    nuevo = versionamiento.crear_workflow(actor, nombre=servicio.nombre, clonar_desde=origen)
    if origen.estado == WorkflowVersion.Estado.ACTIVA:
        versionamiento.activar_version(nuevo, nuevo.versiones.get(estado=WorkflowVersion.Estado.BORRADOR), actor)
    servicio.workflow = nuevo
    servicio.save(update_fields=["workflow", "actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=servicio,
        origen=RegistroAuditoria.Origen.USUARIO, usuario=actor,
        datos_anteriores={"workflow_id": origen_wf.pk},
        datos_nuevos={"workflow_id": nuevo.pk, "vinculo": "COPIA", "copiado_de_workflow_id": origen_wf.pk,
                      "copiado_de_version_id": origen.pk},
    )
    servicio_original.refresh_from_db()
    return nuevo


def _configurar_actividad(etapa, actor, *, tipo_actor="", usuario=None, equipo=None, permite_subtareas=False):
    validar_actor(tipo_actor, usuario=usuario, equipo=equipo)
    return editor.configurar_etapa_tarea(
        etapa, actor, tipo_responsable=tipo_actor, usuario_responsable=usuario,
        equipo_responsable=equipo, permite_subtareas=permite_subtareas,
    )


def _configurar_aprobacion(etapa, actor, *, modo, participantes, politica=""):
    # Mismo contrato de participantes del editor: (tipo, usuario/equipo/None).
    if not participantes:
        raise ValidationError("Debe indicar al menos un participante.")
    for tipo, referencia in participantes:
        validar_actor(
            tipo, usuario=referencia if tipo == "USUARIO" else None,
            equipo=referencia if tipo == "EQUIPO" else None,
        )
        if tipo not in ("USUARIO", "EQUIPO") and referencia is not None:
            raise ValidationError("Un actor dinámico no admite una referencia fija.")
    return editor.configurar_etapa_aprobacion(
        etapa, actor, modo=modo, politica=politica, participantes=participantes,
    )


def _configurar_decision(etapa, actor, **configuracion):
    if configuracion:
        raise ValidationError("Una decisión se configura mediante sus conexiones condicionales y fallback.")


_CONFIGURAR = {
    Etapa.Tipo.TAREA: _configurar_actividad,
    Etapa.Tipo.APROBACION: _configurar_aprobacion,
    Etapa.Tipo.ESPERA: editor.configurar_etapa_espera,
    Etapa.Tipo.CONDICION: _configurar_decision,
}


def _agregar_bloque(version, actor, *, tipo, nombre, descripcion="", **configuracion):
    """ACTIVIDAD/APROBACION/ESPERA/DECISION; alta y configuración indivisibles."""
    if tipo not in TIPOS_BLOQUE:
        raise ValidationError("Tipo de bloque empresarial no soportado.")
    etapa = editor.crear_etapa(version, actor, tipo=TIPOS_BLOQUE[tipo], nombre=nombre, descripcion=descripcion)
    _CONFIGURAR[etapa.tipo](etapa, actor, **configuracion)
    return etapa


def _configurar_bloque(version, bloque, actor, **configuracion):
    etapa = _etapa(version, bloque)
    return _CONFIGURAR[etapa.tipo](etapa, actor, **configuracion)


def _editar_bloque(version, bloque, actor, *, nombre, descripcion=""):
    return editor.editar_etapa(_etapa(version, bloque), actor, nombre=nombre, descripcion=descripcion)


def _conectar_bloques(version, origen, destino, actor, *, conexion=None, **reglas):
    """Crea o redirige una conexión, con las reglas del motor vigente.

    `conexion` permite insertar un bloque redirigiendo INICIO→FIN sin
    generar una segunda salida. Las ramas de aprobación usan resultado;
    las decisiones usan variable/operador/valor/prioridad/fallback.
    """
    origen = _etapa(version, origen, permitir_terminales=True)
    destino = _etapa(version, destino, permitir_terminales=True)
    if conexion is None:
        return editor.crear_transicion(origen, destino, actor, **reglas)
    conexion = TransicionEtapa.objects.get(pk=conexion.pk)
    if conexion.etapa_origen_id != origen.pk:
        raise ValidationError("La conexión no pertenece al bloque de origen.")
    return editor.editar_transicion(conexion, actor, etapa_destino=destino, **reglas)


def _desconectar_bloques(version, conexion, actor):
    conexion = TransicionEtapa.objects.get(pk=conexion.pk)
    _etapa(version, conexion.etapa_origen, permitir_terminales=True)
    return editor.eliminar_transicion(conexion, actor)


def _eliminar_bloque(version, bloque, actor):
    # Conserva la regla del editor: desconectar explícitamente antes de eliminar.
    return editor.eliminar_etapa(_etapa(version, bloque), actor)


# --- Anclado a un Servicio/Proceso (Studio) ---------------------------------


@transaction.atomic
def agregar_bloque(servicio, version, actor, *, tipo, nombre, descripcion="", **configuracion):
    version = _bloquear_version(servicio, version, actor)
    return _agregar_bloque(version, actor, tipo=tipo, nombre=nombre, descripcion=descripcion, **configuracion)


@transaction.atomic
def configurar_bloque(servicio, version, bloque, actor, **configuracion):
    version = _bloquear_version(servicio, version, actor)
    return _configurar_bloque(version, bloque, actor, **configuracion)


@transaction.atomic
def editar_bloque(servicio, version, bloque, actor, *, nombre, descripcion=""):
    version = _bloquear_version(servicio, version, actor)
    return _editar_bloque(version, bloque, actor, nombre=nombre, descripcion=descripcion)


@transaction.atomic
def conectar_bloques(servicio, version, origen, destino, actor, *, conexion=None, **reglas):
    version = _bloquear_version(servicio, version, actor)
    return _conectar_bloques(version, origen, destino, actor, conexion=conexion, **reglas)


@transaction.atomic
def desconectar_bloques(servicio, version, conexion, actor):
    version = _bloquear_version(servicio, version, actor)
    return _desconectar_bloques(version, conexion, actor)


@transaction.atomic
def eliminar_bloque(servicio, version, bloque, actor):
    version = _bloquear_version(servicio, version, actor)
    return _eliminar_bloque(version, bloque, actor)


@transaction.atomic
def publicar_ejecucion(servicio, version, actor, *, confirmar_impacto_compartido=False):
    """Activa la versión y publica el catálogo en una transacción.

    Si el Workflow es compartido, publicar cambia la ejecución de TODOS sus
    servicios para los Tickets nuevos (los ya iniciados conservan su versión):
    exige `confirmar_impacto_compartido=True`, que la UI solo envía tras una
    confirmación reforzada.

    Si falla el formulario, la estructura o cualquier auditoría, tampoco
    queda una nueva versión activa. No duplica eventos de las operaciones.
    """
    version = _bloquear_version(servicio, version, actor)
    otros = list(servicios_que_comparten(version.workflow, excluir=servicio))
    if otros and not confirmar_impacto_compartido:
        raise ValidationError(
            f"Publicar cambiará la ejecución de los tickets nuevos de {_nombres(otros)}, que usan el mismo flujo. "
            "Requiere confirmación reforzada."
        )
    versionamiento.activar_version(version.workflow, version, actor)
    return activar_servicio(servicio, actor)


# --- Anclado a un Flujo (Diseñador › Flujos, D2) -----------------------------


@transaction.atomic
def agregar_bloque_en_flujo(workflow, version, actor, *, tipo, nombre, descripcion="", **configuracion):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _agregar_bloque(version, actor, tipo=tipo, nombre=nombre, descripcion=descripcion, **configuracion)


@transaction.atomic
def configurar_bloque_en_flujo(workflow, version, bloque, actor, **configuracion):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _configurar_bloque(version, bloque, actor, **configuracion)


@transaction.atomic
def editar_bloque_en_flujo(workflow, version, bloque, actor, *, nombre, descripcion=""):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _editar_bloque(version, bloque, actor, nombre=nombre, descripcion=descripcion)


@transaction.atomic
def conectar_bloques_en_flujo(workflow, version, origen, destino, actor, *, conexion=None, **reglas):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _conectar_bloques(version, origen, destino, actor, conexion=conexion, **reglas)


@transaction.atomic
def desconectar_bloques_en_flujo(workflow, version, conexion, actor):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _desconectar_bloques(version, conexion, actor)


@transaction.atomic
def eliminar_bloque_en_flujo(workflow, version, bloque, actor):
    version = _bloquear_version_de_flujo(workflow, version, actor)
    return _eliminar_bloque(version, bloque, actor)


@transaction.atomic
def crear_flujo(actor, *, nombre, descripcion=""):
    """Crea un Flujo NUEVO e independiente de cualquier Servicio: un `Workflow`
    real de la biblioteca, con su v1 en BORRADOR (INICIO → FIN) lista para
    diseñar. Solo exige `workflows.administrar`. Es la misma alta que el
    editor técnico (`versionamiento.crear_workflow`); aquí se le añade el
    esqueleto mínimo para que el flujo nazca editable."""
    _exigir_administrar_flujos(actor)
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValidationError("El flujo necesita un nombre.")
    workflow = versionamiento.crear_workflow(actor, nombre=nombre, descripcion=descripcion or "")
    _asegurar_esqueleto(workflow.versiones.get(estado=WorkflowVersion.Estado.BORRADOR), actor)
    return workflow


@transaction.atomic
def preparar_borrador_de_flujo(workflow, actor, *, confirmar_compartido=False):
    """Devuelve el borrador del flujo o abre uno nuevo (copia de la versión
    activa). Si el flujo lo usan servicios/procesos y hay que ABRIR un borrador
    nuevo, exige `confirmar_compartido=True`: lo que luego se publique afectará
    a las ejecuciones nuevas de todos ellos (alternativa: `duplicar_flujo`).
    Un borrador ya abierto se devuelve tal cual: ya fue una decisión consciente."""
    _exigir_administrar_flujos(actor)
    workflow = Workflow.objects.select_for_update().get(pk=workflow.pk)
    version = workflow.versiones.filter(estado=WorkflowVersion.Estado.BORRADOR).order_by("-numero").first()
    if version is None:
        otros = list(servicios_que_comparten(workflow))
        if otros and not confirmar_compartido:
            raise ValidationError(
                f"Este flujo lo usan {_nombres(otros)}. Crea una copia propia o confirma que quieres "
                "modificar el flujo compartido."
            )
        version = versionamiento.crear_nueva_version(workflow, actor)
    _asegurar_esqueleto(version, actor)
    return version


@transaction.atomic
def publicar_flujo(workflow, version, actor, *, confirmar_impacto_compartido=False):
    """Activa `version` como la vigente del flujo. NO publica ni toca ningún
    Servicio: cada uno sigue con su propio estado de publicación.

    Si el flujo ya tenía una versión activa Y lo usan servicios/procesos, la
    nueva versión cambiará la ejecución de sus tickets NUEVOS (los ya iniciados
    conservan la suya): exige `confirmar_impacto_compartido=True`. La primera
    publicación de un flujo no cambia nada para nadie y no la exige. No es
    irreversible: la versión anterior queda guardada como histórica."""
    version = _bloquear_version_de_flujo(workflow, version, actor)
    flujo = version.workflow
    otros = list(servicios_que_comparten(flujo))
    if otros and flujo.version_activa_id is not None and not confirmar_impacto_compartido:
        raise ValidationError(
            f"Publicar cambiará la ejecución de los tickets nuevos de {_nombres(otros)}, que usan este flujo. "
            "Requiere confirmación reforzada."
        )
    try:
        versionamiento.activar_version(flujo, version, actor)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return flujo


@transaction.atomic
def duplicar_flujo(workflow, actor, *, nombre=None):
    """Crea un Workflow INDEPENDIENTE a partir de `workflow`: preserva el
    original, no cambia a ningún servicio vinculado a él y permite modificar la
    copia. Parte de la versión activa (o, si no hay, de la última) y nace como
    BORRADOR: para vincularla a un servicio hay que publicarla. Reutiliza
    `crear_workflow(clonar_desde=…)`, la misma clonación de siempre."""
    _exigir_administrar_flujos(actor)
    original = Workflow.objects.select_for_update().get(pk=workflow.pk)
    origen = original.version_activa or original.versiones.order_by("-numero").first()
    if origen is None:
        raise ValidationError("El flujo no tiene versiones que copiar.")
    return versionamiento.crear_workflow(
        actor, nombre=(nombre or "").strip() or f"Copia de {original.nombre}",
        descripcion=original.descripcion, clonar_desde=origen,
    )
