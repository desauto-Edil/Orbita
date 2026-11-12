"""Jefaturas y «Mi equipo» — requerimiento nuevo (CU por asignar): que un jefe vea en qué trabajan sus colaboradores.

JEFATURA = relación EXPLÍCITA jefe → colaborador con vigencia (`core.JefaturaUsuario`, mismo patrón que
`MiembroEquipo`): nunca se deduce del área, el equipo ni el rol. Una persona tiene a lo sumo UN jefe directo vigente;
nadie es su propio jefe ni se forman ciclos. Cambiar o quitar el jefe cierra la vigencia sin borrar (se audita).

Lo que la jefatura PERMITE es solo LEER, y solo desde «Mi equipo»: cuántos tickets tiene a cargo cada colaborador
directo, qué tiene pendiente y un resumen de cada ticket (`resumen_de_equipo`, `detalle_de_colaborador`). No concede
ningún `puede_*` sobre tickets, tareas ni aprobaciones (el jefe no toma, reasigna ni decide, y no lee la
conversación del equipo). Solo jefatura DIRECTA (un nivel).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.auditoria import registrar_evento
from apps.core.models import JefaturaUsuario, RegistroAuditoria, Usuario


# --- Lectura ------------------------------------------------------------------------------------------------------


def jefe_de(usuario):
    """El jefe directo vigente de `usuario`, o `None`."""
    if usuario is None or usuario.pk is None:
        return None
    jefatura = JefaturaUsuario.objects.filter(colaborador=usuario, activo=True).select_related("jefe").first()
    return jefatura.jefe if jefatura is not None else None


def colaboradores_de(jefe):
    """Colaboradores DIRECTOS vigentes y activos de `jefe`, por nombre."""
    if not getattr(jefe, "is_authenticated", False):
        return Usuario.objects.none()
    return (
        Usuario.objects.filter(is_active=True, jefaturas__jefe=jefe, jefaturas__activo=True)
        .distinct().order_by("first_name", "last_name", "email")
    )


def tiene_colaboradores(usuario):
    """¿`usuario` tiene personal a cargo? (decide si ve «Mi equipo»)."""
    return getattr(usuario, "is_authenticated", False) and JefaturaUsuario.objects.filter(
        jefe=usuario, activo=True, colaborador__is_active=True
    ).exists()


def es_jefe_de(jefe, colaborador):
    return getattr(jefe, "is_authenticated", False) and JefaturaUsuario.objects.filter(
        jefe=jefe, colaborador=colaborador, activo=True
    ).exists()


# --- Escritura -----------------------------------------------------------------------------------------------------


def _formaria_ciclo(colaborador, jefe):
    """¿Poner a `jefe` como jefe de `colaborador` crearía un ciclo? (sube por la cadena de jefes de `jefe`)."""
    vistos, actual = set(), jefe
    while actual is not None and actual.pk not in vistos:
        if actual.pk == colaborador.pk:
            return True
        vistos.add(actual.pk)
        actual = jefe_de(actual)
    return False


@transaction.atomic
def asignar_jefe(colaborador, jefe, actor):
    """Fija (o quita, con `jefe=None`) el jefe directo de `colaborador`. Cierra la jefatura vigente anterior (si
    cambia) y crea la nueva; repetir el mismo jefe no hace nada. Audita. La autorización (`usuarios.administrar`) es
    de quien llama (Configuración › Usuarios)."""
    vigente = JefaturaUsuario.objects.select_for_update().filter(colaborador=colaborador, activo=True).first()
    if jefe is not None:
        if jefe.pk == colaborador.pk:
            raise ValidationError("Una persona no puede ser su propio jefe.")
        if not jefe.is_active:
            raise ValidationError("El jefe debe ser una persona activa.")
        if _formaria_ciclo(colaborador, jefe):
            raise ValidationError(
                f"{jefe.nombre_visible} ya depende de esta persona: no puede ser su jefe."
            )
    if vigente is not None and jefe is not None and vigente.jefe_id == jefe.pk:
        return vigente
    if vigente is None and jefe is None:
        return None  # sin jefe antes ni ahora: nada que cambiar ni auditar
    anterior = {"jefe_id": vigente.jefe_id if vigente is not None else None}
    if vigente is not None:
        vigente.activo, vigente.fecha_fin = False, timezone.localdate()
        vigente.save(update_fields=["activo", "fecha_fin", "actualizado_en"])
    nueva = JefaturaUsuario.objects.create(jefe=jefe, colaborador=colaborador) if jefe is not None else None
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR, instancia=nueva or vigente,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor, datos_anteriores=anterior,
        datos_nuevos={"colaborador_id": colaborador.pk, "jefe_id": jefe.pk if jefe is not None else None},
    )
    return nueva


# --- Mi equipo (solo lectura) ------------------------------------------------------------------------------------


def _bandeja_de(colaborador):
    """La MISMA bandeja de Mi trabajo de `colaborador` (`tickets.bandeja.construir`), calculada para él."""
    from django.db.models import Q

    from apps.aprobaciones.consultas import aprobaciones_pendientes_para
    from apps.tareas.consultas import tareas_asignadas_a, tareas_disponibles_para_tomar
    from apps.tareas.models import Tarea
    from apps.tickets import bandeja, revision_elementos, seguimiento, trabajo
    from apps.tickets import parrilla as parrilla_ops

    tareas = list(
        Tarea.objects.filter(
            Q(pk__in=tareas_asignadas_a(colaborador).values("pk"))
            | Q(pk__in=tareas_disponibles_para_tomar(colaborador).values("pk"))
        ).exclude(estado=Tarea.Estado.CANCELADA).select_related("usuario_responsable", "equipo_responsable")
    )
    a_cargo = list(trabajo.tickets_a_cargo(colaborador))
    resultado = bandeja.construir(
        colaborador, tareas=tareas, aprobaciones=list(aprobaciones_pendientes_para(colaborador)),
        revisiones=revision_elementos.revisiones_pendientes_para(colaborador),
        colecciones=list(parrilla_ops.colecciones_de_participante(colaborador)), a_cargo=a_cargo,
    )
    filas = resultado["atencion"] + resultado["seguimiento"]
    fases = seguimiento.fases_actuales([g.ticket for g in filas])
    for grupo in filas:
        grupo.fase = fases.get(grupo.ticket.pk, "")
        grupo.situacion = "atencion" if grupo.requiere_atencion else "seguimiento"
    return {"filas": filas, "a_cargo": a_cargo, "atencion": resultado["atencion"]}


def _area_principal(colaborador):
    from apps.core.models import UsuarioArea

    vinculo = UsuarioArea.objects.filter(usuario=colaborador, es_principal=True, activo=True).select_related("area").first()
    return vinculo.area.nombre if vinculo is not None else ""


def _orden_de_fila(grupo):
    """Lo urgente primero: vencidos, luego lo que pide acción por fecha, y al final lo que solo sigue."""
    from apps.tickets.bandeja import _como_instante

    vence = grupo.proximo_vencimiento
    return (
        not grupo.vencido, not grupo.requiere_atencion, vence is None,
        _como_instante(vence) if vence is not None else 0, grupo.ticket.pk,
    )


def _resumen_de(colaborador):
    """Las cifras de una persona y sus tickets ordenados por urgencia. `proxima` = la fecha más cercana en la que vence algo suyo."""
    from apps.core.presencia import en_linea

    datos = _bandeja_de(colaborador)
    atencion = datos["atencion"]
    acciones = [p for g in atencion for p in g.acciones]
    vencidas = sum(1 for p in acciones if p.vencida)
    fechas = [g.proximo_vencimiento for g in atencion if g.proximo_vencimiento is not None]
    filas = sorted(datos["filas"], key=_orden_de_fila)
    return {
        "persona": colaborador,
        "cargo": getattr(getattr(colaborador, "perfil_organizacional", None), "cargo", "") or "",
        "area": _area_principal(colaborador),
        "en_linea": en_linea(colaborador),
        "tickets_activos": len(datos["a_cargo"]),
        "pendientes": len(acciones),
        "vencidos": sum(1 for g in atencion if g.vencido),
        "con_pendientes": len(atencion),
        "acciones_vencidas": vencidas,
        "en_seguimiento": len(datos["filas"]) - len(atencion),
        "proxima": min(fechas, key=_orden_fecha) if fechas else None,
        "tickets": filas,
    }


def _orden_fecha(valor):
    from apps.tickets.bandeja import _como_instante

    return _como_instante(valor)


def resumen_de_equipo(jefe):
    """Vista general de «Mi equipo»: una entrada por colaborador directo con sus tickets activos a cargo, cuántos
    pendientes tiene (acciones), cuántos vencidos, y la lista de sus tickets (para el resumen de cada uno)."""
    return [_resumen_de(colaborador) for colaborador in colaboradores_de(jefe)]


def detalle_de_colaborador(jefe, colaborador):
    """El detalle de UN colaborador directo de `jefe` (su bandeja completa, solo lectura). `None` si no lo es."""
    if not es_jefe_de(jefe, colaborador) or not colaborador.is_active:
        return None
    return _resumen_de(colaborador)
