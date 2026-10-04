from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from apps.core.auditoria import registrar_evento, serializar
from apps.core.models import RegistroAuditoria
from apps.workflows.models import FaseWorkflow, TransicionFaseWorkflow, Workflow
from apps.workflows.versionamiento import crear_workflow


def validar_estructura_fases(version):
    """Validacion minima para activar una plantilla de fases."""
    errores = []
    if version.workflow.modo != Workflow.Modo.PLANTILLA_FASES:
        return ["La version no pertenece a una plantilla de fases."]

    fases = list(version.fases.all())
    if not fases:
        return ["La plantilla no tiene fases."]

    for fase in fases:
        if fase.version_id != version.pk:
            errores.append(f"La fase {fase.nombre} no pertenece a esta version.")
        for transicion in fase.transiciones_salientes.all():
            if transicion.fase_destino.version_id != version.pk:
                errores.append(f"La transicion {transicion} conecta fases de versiones distintas.")

    iniciales = [fase for fase in fases if not fase.transiciones_entrantes.exists()]
    finales = [fase for fase in fases if not fase.transiciones_salientes.exists()]
    if len(iniciales) != 1:
        errores.append(f"Debe existir exactamente una fase inicial (hay {len(iniciales)}).")
    if len(finales) != 1:
        errores.append(f"Debe existir exactamente una fase final (hay {len(finales)}).")

    if len(iniciales) == 1:
        adyacencia = {
            fase.pk: [t.fase_destino_id for t in fase.transiciones_salientes.all()] for fase in fases
        }
        visitadas = {iniciales[0].pk}
        pendientes = [iniciales[0].pk]
        while pendientes:
            actual = pendientes.pop()
            for siguiente in adyacencia.get(actual, []):
                if siguiente not in visitadas:
                    visitadas.add(siguiente)
                    pendientes.append(siguiente)
        huerfanas = [fase for fase in fases if fase.pk not in visitadas]
        if huerfanas:
            nombres = ", ".join(fase.nombre for fase in huerfanas)
            errores.append(f"Las siguientes fases no son alcanzables desde la fase inicial: {nombres}.")

    return errores


@transaction.atomic
def crear_plantilla_fases(actor, *, nombre, descripcion=""):
    return crear_workflow(
        actor,
        nombre=nombre,
        descripcion=descripcion,
        modo=Workflow.Modo.PLANTILLA_FASES,
    )


@transaction.atomic
def agregar_fase(version, actor, *, nombre, descripcion="", orden=None):
    version.exigir_editable()
    if version.workflow.modo != Workflow.Modo.PLANTILLA_FASES:
        raise ValidationError("Solo una plantilla de fases admite fases.")
    if orden is None:
        ultimo = version.fases.aggregate(maximo=Max("orden"))["maximo"] or 0
        orden = ultimo + 1
    fase = FaseWorkflow(version=version, nombre=nombre, descripcion=descripcion, orden=orden)
    fase.full_clean()
    fase.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=fase,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(fase),
    )
    return fase


@transaction.atomic
def editar_fase(fase, actor, *, nombre=None, descripcion=None, orden=None):
    fase.version.exigir_editable()
    anterior = serializar(fase)
    campos = []
    if nombre is not None:
        fase.nombre = nombre
        campos.append("nombre")
    if descripcion is not None:
        fase.descripcion = descripcion
        campos.append("descripcion")
    if orden is not None:
        fase.orden = orden
        campos.append("orden")
    if not campos:
        return fase
    fase.full_clean()
    fase.save(update_fields=campos + ["actualizado_en"])
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=fase,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=serializar(fase),
    )
    return fase


@transaction.atomic
def eliminar_fase(fase, actor):
    fase.version.exigir_editable()
    anterior = serializar(fase)
    fase.delete()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=fase,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )


@transaction.atomic
def conectar_fases(origen, destino, actor, *, nombre="", prioridad=0):
    origen.version.exigir_editable()
    transicion = TransicionFaseWorkflow(
        fase_origen=origen,
        fase_destino=destino,
        nombre=nombre,
        prioridad=prioridad,
    )
    transicion.full_clean()
    transicion.save()
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=None,
        datos_nuevos=serializar(transicion),
    )
    return transicion


@transaction.atomic
def eliminar_transicion_fase(transicion, actor):
    transicion.fase_origen.version.exigir_editable()
    anterior = serializar(transicion)
    transicion.delete()
    registrar_evento(
        accion=RegistroAuditoria.Accion.ELIMINAR,
        instancia=transicion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores=anterior,
        datos_nuevos=None,
    )
