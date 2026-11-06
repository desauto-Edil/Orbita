"""ELIMINAR un caso de la demostración (UX-4): borrado FÍSICO de lo que la demo creó, para volver a prepararla desde
cero. Es una herramienta de desarrollo explícita (`--fase eliminar --confirmar-eliminar`), no una operación del
producto: en Órbita nada se borra (retirar es desactivar), y por eso esta fase solo toca lo que cuelga de la demo.

Qué borra: los tickets del servicio del caso con todo lo que colgaba de ellos (ejecuciones del flujo y sus
subinstancias, tareas y subtareas, aprobaciones, gacetas, ítems, entregables, historial, adjuntos y sus archivos),
la configuración del servicio (formularios, colecciones, flujo configurado, programación), el servicio, su plantilla
de fases si nadie más la usa y la categoría si la creó la demo y nadie más la usa. Qué NO borra: personas, equipos,
áreas, permisos ni la auditoría (registro append-only, referencias genéricas).

Cómo: el Collector de Django no sirve aquí (los PROTECT forman ciclos: ítems ↔ versiones, instancia ↔ subinstancias,
ticket ↔ ejecución). Se REÚNE primero todo lo que depende de la demo recorriendo las claves foráneas inversas hasta
un punto fijo; si algo ajeno a la demo depende de ella, se detiene sin borrar nada. Después se borra por SQL directo
(las claves foráneas de PostgreSQL son DEFERRABLE, el orden no importa dentro de la transacción) y se fuerza la
comprobación de integridad antes de terminar, también al simular. Los archivos se borran solo tras el commit.
"""

from collections import defaultdict

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction

# Apps cuyos objetos pueden borrarse si dependen de la demo. `core` (personas, equipos, áreas, permisos) nunca.
_APPS_BORRABLES = {"tickets", "workflows", "tareas", "aprobaciones", "gacetas", "catalogo"}


def _referencias():
    """(modelo, campo) de cada clave foránea u 1:1 concreta del proyecto, incluidas las tablas M2M automáticas."""
    pares = []
    for modelo in django_apps.get_models(include_auto_created=True):
        for campo in modelo._meta.concrete_fields:
            if campo.is_relation and (campo.many_to_one or campo.one_to_one):
                pares.append((modelo, campo))
    return pares


def _propios():
    """Modelos que la demo crea para sí y a los que se llega por una clave foránea DIRECTA (no inversa)."""
    from apps.aprobaciones.models import EsquemaAprobacion
    from apps.tareas.models import Tarea
    from apps.workflows.models import InstanciaWorkflow

    return {Tarea, EsquemaAprobacion, InstanciaWorkflow}


def _ajeno(modelo, campo):
    return ValidationError(
        f"Se detuvo sin borrar nada: {modelo._meta.verbose_name} ({modelo._meta.label}.{campo.name}) depende de la demo "
        "y no es de ella."
    )


def _reunir(raices, servicio, workflow_raiz):
    """Todo lo que depende de `raices` (modelo → pks) por claves foráneas inversas, más las tareas, aprobaciones y
    ejecuciones a las que apunta lo reunido. Devuelve (reunido, anulaciones)."""
    from apps.catalogo.models import Categoria, Servicio
    from apps.workflows.models import Workflow

    referencias = _referencias()
    propios = _propios()
    reunido = defaultdict(set)
    anulaciones = defaultdict(set)  # (modelo, campo) → pks ajenos que solo hay que dejar en NULL
    pendiente = [(modelo, set(pks)) for modelo, pks in raices.items()]

    def agregar(modelo, pks):
        nuevos = {pk for pk in pks if pk is not None} - reunido[modelo]
        if nuevos:
            reunido[modelo] |= nuevos
            pendiente.append((modelo, nuevos))

    while pendiente:
        destino, pks = pendiente.pop()
        reunido[destino] |= pks
        concreto = destino._meta.concrete_model
        # Hacia atrás: quien apunta a estos objetos.
        for modelo, campo in referencias:
            if campo.related_model is None or campo.related_model._meta.concrete_model is not concreto:
                continue
            filas = set(
                modelo._base_manager.filter(**{f"{campo.name}__in": concreto._base_manager.filter(pk__in=pks)})
                .values_list("pk", flat=True)
            )
            filas -= reunido[modelo]
            if not filas:
                continue
            etiqueta = modelo._meta.app_label
            if modelo._meta.auto_created:
                agregar(modelo, filas)  # vínculo M2M: solo es la relación
            elif etiqueta not in _APPS_BORRABLES:
                if campo.null and campo.remote_field.on_delete is models.SET_NULL:
                    anulaciones[(modelo, campo)] |= filas
                else:
                    raise _ajeno(modelo, campo)
            elif modelo is Servicio and filas - {servicio.pk}:
                raise ValidationError(f"Se detuvo sin borrar nada: otro servicio usa algo de la demo ({campo.name}).")
            elif modelo is Categoria:
                raise ValidationError("Se detuvo sin borrar nada: una categoría depende de la demo.")
            elif modelo is Workflow and workflow_raiz is None:
                raise ValidationError("Se detuvo sin borrar nada: la plantilla de fases la usa otro servicio.")
            else:
                agregar(modelo, filas)
        # Hacia adelante, solo lo propio: tareas, aprobaciones y ejecuciones creadas para la demo.
        for campo in destino._meta.concrete_fields:
            if campo.is_relation and (campo.many_to_one or campo.one_to_one) and campo.related_model in propios:
                objetivo = campo.related_model
                ids = set(destino._base_manager.filter(pk__in=pks).values_list(campo.attname, flat=True))
                agregar(objetivo, ids)
    return reunido, anulaciones


def _archivos(reunido):
    """Los archivos guardados en campos FileField de lo reunido (adjuntos del ticket, ítems, entregables, tareas)."""
    archivos = []
    for modelo, pks in reunido.items():
        campos = [c for c in modelo._meta.concrete_fields if isinstance(c, models.FileField)]
        if not campos or not pks:
            continue
        for fila in modelo._base_manager.filter(pk__in=pks).only(*(c.name for c in campos)):
            for campo in campos:
                archivo = getattr(fila, campo.name)
                if archivo and archivo.name:
                    archivos.append((archivo.storage, archivo.name))
    return archivos


@transaction.atomic
def eliminar(recurso, actor):
    """Borra físicamente el caso `recurso` de la demo. Devuelve las líneas del informe. `actor` ya fue autorizado
    por el comando (`catalogo.administrar`); la auditoría append-only no se toca."""
    from apps.aprobaciones.models import EsquemaAprobacion
    from apps.catalogo.models import Categoria, Servicio
    from apps.tareas.models import Tarea
    from apps.tickets import vinculos
    from apps.tickets.models import Ticket
    from apps.workflows.models import InstanciaWorkflow, Workflow

    servicio, workflow = recurso.servicio, recurso.workflow
    categoria = recurso.categoria if recurso.categoria_creada else None
    if workflow is not None and Servicio.objects.filter(workflow=workflow).exclude(pk=servicio.pk).exists():
        workflow = None  # la usa otro servicio: se conserva
    tickets = list(Ticket.objects.filter(detalle_servicio__servicio=servicio))
    raices = defaultdict(set)
    raices[type(recurso)].add(recurso.pk)
    raices[Servicio].add(servicio.pk)
    raices[Ticket] |= {t.pk for t in tickets}
    for ticket in tickets:
        raices[Tarea] |= set(vinculos.tareas_del_ticket(ticket).values_list("pk", flat=True))
        raices[EsquemaAprobacion] |= set(vinculos.aprobaciones_del_ticket(ticket).values_list("esquema_id", flat=True))
        raices[InstanciaWorkflow] |= set(vinculos.instancias_del_ticket(ticket))
    if workflow is not None:
        raices[Workflow].add(workflow.pk)

    reunido, anulaciones = _reunir(raices, servicio, workflow)
    archivos = _archivos(reunido)

    try:
        with transaction.atomic():
            for (modelo, campo), pks in anulaciones.items():
                modelo._base_manager.filter(pk__in=pks).update(**{campo.name: None})
            for modelo, pks in reunido.items():
                if pks:
                    consulta = modelo._base_manager.filter(pk__in=pks)
                    consulta._raw_delete(consulta.db)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # comprueba la integridad ya, también al simular
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")  # y vuelve al modo de Django para lo que siga
    except IntegrityError as exc:
        raise ValidationError(f"Se detuvo sin borrar nada: algo ajeno sigue apuntando a la demo ({exc}).".strip())

    def _borrar_archivos():
        for almacen, nombre in archivos:
            try:
                almacen.delete(nombre)
            except OSError:
                pass

    transaction.on_commit(_borrar_archivos)  # al simular no hay commit: los archivos quedan

    total = sum(len(pks) for pks in reunido.values())
    lineas = [
        f"Eliminados {len(tickets)} ticket(s), {len(reunido.get(Tarea, ()))} tarea(s), "
        f"{len(reunido.get(EsquemaAprobacion, ()))} esquema(s) de aprobación y {len(archivos)} archivo(s) "
        f"({total} registros en total), con la configuración del servicio «{servicio.nombre}».",
    ]
    if workflow is not None:
        lineas.append(f"Plantilla de fases «{workflow.nombre}» eliminada.")
    if categoria is not None and not Servicio.objects.filter(categoria=categoria).exists():
        Categoria.objects.filter(pk=categoria.pk).delete()
        lineas.append(f"Categoría «{categoria.nombre}» eliminada.")
    lineas.append("Personas, equipos, permisos y auditoría se conservan. Ya se puede volver a preparar.")
    return lineas
