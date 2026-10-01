"""Operaciones de dominio de Aprobaciones — Sprint 3.4 (CU-024/025).

Mismo patrón de concurrencia que `apps/tareas/operaciones.py` y
`apps/workflows/motor.py`: `@transaction.atomic` + `select_for_update()` +
revalidación de estado bajo lock — todo Postgres, sin locks de Redis.

Orden de locks — **corregido tras ejecutar la prueba de concurrencia
obligatoria** (ver informe final, hallazgo de implementación): la primera
versión de este módulo bloqueaba primero la `Aprobacion` propia y solo
después `EsquemaAprobacion`; al cerrar, necesitaba bloquear TAMBIÉN las
demás filas del mismo esquema (para marcarlas `NO_REQUERIDA`) — eso abría
una ventana real de interbloqueo entre dos decisiones concurrentes del
mismo esquema (confirmado por Postgres: `DeadlockDetected` real al correr
`ConcurrenciaEsquemaTests.test_dos_aprobadas_concurrentes_cierran_una_sola_vez`
en `apps/aprobaciones/tests.py`; el caso exacto era el mismo mecanismo que
el usuario pidió probar con cuidado — dos aprobadores PARALELA+CUALQUIERA
decidiendo simultáneamente).

Orden definitivo: `resolver_aprobacion` bloquea `EsquemaAprobacion`
PRIMERO, y a continuación TODAS las `Aprobacion` de ese esquema en una
sola consulta ordenada por `pk` (nunca por partes, nunca en dos pasadas) —
así ninguna transacción avanza más allá de ese punto sin ya tener
adquiridos todos los locks que podría necesitar (incluido el de marcar
otras participaciones `NO_REQUERIDA` al cerrar). La integración con
Workflow (`apps.workflows.integracion.resolver_aprobacion_workflow`)
continúa la cadena bloqueando después `InstanciaEtapa` → `InstanciaWorkflow`,
en ese orden ya establecido por `apps.workflows.motor` — ver ese módulo. Es
una cadena estrictamente unidireccional (`aprobaciones` → `workflows`,
nunca al revés), así que no hay ciclo posible de locks entre los dos
dominios.

RN-026 (inmutabilidad) se protege ÚNICAMENTE por la API de dominio:
`resolver_aprobacion()` revalida `estado == PENDIENTE` bajo lock antes de
escribir la única decisión — no hay señal `pre_save` (instrucción
explícita del usuario: eso no es lo que protege realmente la
inmutabilidad).

Cierre de esquema (`_evaluar_cierre_esquema`) NO decide "¿mi propia
decisión cierra el esquema?" — reevalúa el estado agregado COMPLETO de
todas las participaciones bajo el lock de `EsquemaAprobacion`, con
prioridad RECHAZADA > DEVUELTA > APROBADA (decisión aprobada explícita: el
escenario de una APROBADA y una RECHAZADA verdaderamente concurrentes en
PARALELA no debía depender de qué transacción ganara la carrera por el
lock — ver docstring de `_evaluar_cierre_esquema`). Esto es válido para
decisiones ya comprometidas o que esta misma transacción acaba de grabar —
**no** puede reabrir un esquema que ya cerró y cuya continuación de
Workflow ya se disparó (eso exigiría deshacer una transición ya tomada,
algo que nadie ha pedido y que RN-026 prohíbe de todas formas: una
decisión, y el cierre del esquema que de ella se deriva, no se
reescriben).

Este módulo no importa nada de `apps.workflows` — la integración vive del
lado de `apps/workflows/integracion.py`, nunca aquí."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.aprobaciones.autorizacion import puede_aprobar, puede_reasignar_aprobacion
from apps.aprobaciones.models import Aprobacion, EsquemaAprobacion, ReasignacionAprobacion
from apps.core.auditoria import registrar_evento
from apps.core.models import RegistroAuditoria


@transaction.atomic
def crear_esquema_aprobacion(*, modo, politica=None, participantes, actor=None):
    """`participantes`: lista ordenada de tuplas
    `(tipo_aprobador, usuario_o_equipo)` — `tipo_aprobador` es
    `Aprobacion.TipoAprobador.USUARIO`/`EQUIPO`. `orden` se deriva de la
    posición en la lista (1, 2, 3...). Sin actor explícito, la creación
    se audita como SISTEMA (por ejemplo, desde la Strategy del motor).

    Sin `puede_*` interno: mismo criterio que
    `apps.tareas.operaciones.crear_tarea`/`apps.workflows.motor.
    iniciar_workflow` — primitiva de dominio invocada por
    `EstrategiaAprobacion`; ningún CU documenta un flujo de "crear esquema
    manual" en 3.4."""
    if not participantes:
        raise ValidationError("Un esquema de aprobación requiere al menos un participante.")
    if modo == EsquemaAprobacion.Modo.PARALELA and not politica:
        raise ValidationError("modo=PARALELA requiere una política (TODOS/CUALQUIERA).")
    if modo == EsquemaAprobacion.Modo.SECUENCIAL and politica:
        raise ValidationError("modo=SECUENCIAL no admite política de cierre propia.")

    esquema = EsquemaAprobacion.objects.create(modo=modo, politica=politica or "")
    for orden, (tipo_aprobador, aprobador) in enumerate(participantes, start=1):
        if tipo_aprobador == Aprobacion.TipoAprobador.USUARIO:
            Aprobacion.objects.create(
                esquema=esquema, orden=orden, tipo_aprobador=tipo_aprobador, aprobador_usuario=aprobador
            )
        elif tipo_aprobador == Aprobacion.TipoAprobador.EQUIPO:
            Aprobacion.objects.create(
                esquema=esquema, orden=orden, tipo_aprobador=tipo_aprobador, aprobador_equipo=aprobador
            )
        else:
            raise ValidationError(f"tipo_aprobador inválido: {tipo_aprobador}.")
    registrar_evento(
        accion=RegistroAuditoria.Accion.CREAR,
        instancia=esquema,
        origen=RegistroAuditoria.Origen.USUARIO if actor is not None else RegistroAuditoria.Origen.SISTEMA,
        usuario=actor,
        datos_nuevos={
            "modo": esquema.modo,
            "politica": esquema.politica,
            "cantidad_aprobaciones": esquema.participaciones.count(),
        },
    )
    return esquema


def _evaluar_cierre_esquema(esquema, participaciones):
    """Núcleo de cierre — ver docstring del módulo. `esquema` y TODAS las
    filas en `participaciones` ya llegan bloqueadas por
    `resolver_aprobacion` (mismo lock adquirido de una sola vez, ver su
    docstring sobre el orden) — esta función no adquiere ningún lock
    nuevo, precisamente para no reabrir la ventana de interbloqueo que
    motivó el rediseño.

    Reevalúa TODA la participación conocida (incluida la que
    `resolver_aprobacion` acaba de mutar en el mismo objeto Python, ya
    reflejada en `participaciones`) con prioridad
    RECHAZADA > DEVUELTA > APROBADA."""
    if esquema.resultado is not None:
        return esquema

    Estado = Aprobacion.Estado

    if any(p.estado == Estado.RECHAZADA for p in participaciones):
        resultado = EsquemaAprobacion.Resultado.RECHAZADA
    elif any(p.estado == Estado.DEVUELTA for p in participaciones):
        resultado = EsquemaAprobacion.Resultado.DEVUELTA
    else:
        aprobadas = [p for p in participaciones if p.estado == Estado.APROBADA]
        pendientes = [p for p in participaciones if p.estado == Estado.PENDIENTE]
        resultado = None
        if aprobadas:
            if esquema.modo == EsquemaAprobacion.Modo.PARALELA:
                if esquema.politica == EsquemaAprobacion.Politica.CUALQUIERA:
                    resultado = EsquemaAprobacion.Resultado.APROBADA
                elif esquema.politica == EsquemaAprobacion.Politica.TODOS and not pendientes:
                    resultado = EsquemaAprobacion.Resultado.APROBADA
            else:  # SECUENCIAL
                if not pendientes:
                    resultado = EsquemaAprobacion.Resultado.APROBADA

    if resultado is None:
        return esquema  # sigue en curso

    esquema.resultado = resultado
    esquema.resuelto_en = timezone.now()
    esquema.save(update_fields=["resultado", "resuelto_en", "actualizado_en"])

    pendientes_restantes = [p.pk for p in participaciones if p.estado == Estado.PENDIENTE]
    if pendientes_restantes:
        Aprobacion.objects.filter(pk__in=pendientes_restantes).update(estado=Estado.NO_REQUERIDA)

    return esquema


@transaction.atomic
def resolver_aprobacion(aprobacion, actor, *, decision, observacion=""):
    """CU-024. `decision` ∈ {APROBADA, RECHAZADA, DEVUELTA} — los mismos 3
    valores de `Aprobacion.Estado` que representan una decisión humana
    (PENDIENTE/NO_REQUERIDA nunca lo son). Observación obligatoria en
    RECHAZADA/DEVUELTA, opcional en APROBADA (RQF-080, decisión
    aprobada)."""
    if decision not in (Aprobacion.Estado.APROBADA, Aprobacion.Estado.RECHAZADA, Aprobacion.Estado.DEVUELTA):
        raise ValidationError(f"Decisión inválida: {decision}.")
    if decision in (Aprobacion.Estado.RECHAZADA, Aprobacion.Estado.DEVUELTA) and not observacion:
        raise ValidationError("La observación es obligatoria para rechazar o devolver (RQF-080).")

    esquema = EsquemaAprobacion.objects.select_for_update().get(pk=aprobacion.esquema_id)
    participaciones = list(Aprobacion.objects.select_for_update().filter(esquema=esquema).order_by("pk"))
    por_pk = {p.pk: p for p in participaciones}
    aprobacion = por_pk.get(aprobacion.pk)
    if aprobacion is None:
        raise ValidationError("La aprobación indicada no pertenece a este esquema.")
    if aprobacion.estado != Aprobacion.Estado.PENDIENTE:
        raise ValidationError("Esta aprobación ya fue decidida — no puede modificarse (RN-026).")
    if not puede_aprobar(actor, aprobacion):
        raise PermissionDenied("No tiene autorización para decidir esta aprobación.")

    estado_anterior = aprobacion.estado
    aprobacion.estado = decision
    aprobacion.decidido_por = actor
    aprobacion.observacion = observacion
    aprobacion.decidida_en = timezone.now()
    aprobacion.save(update_fields=["estado", "decidido_por", "observacion", "decidida_en", "actualizado_en"])

    esquema = _evaluar_cierre_esquema(esquema, participaciones)
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=aprobacion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={"estado": estado_anterior},
        datos_nuevos={"estado": aprobacion.estado, "observacion": aprobacion.observacion},
    )
    return aprobacion, esquema


@transaction.atomic
def reasignar_aprobacion(aprobacion, actor, *, nuevo_aprobador_usuario=None, nuevo_aprobador_equipo=None, motivo=""):
    """RQF-083. Solo reasignación — sin `DelegacionAprobacion` (RQF-083
    dice "reasignar", nunca "delegar")."""
    if nuevo_aprobador_usuario is None and nuevo_aprobador_equipo is None:
        raise ValidationError("Debe indicar un nuevo usuario o equipo aprobador.")
    if nuevo_aprobador_usuario is not None and nuevo_aprobador_equipo is not None:
        raise ValidationError("Indique un nuevo aprobador de un solo tipo (usuario o equipo).")

    aprobacion = Aprobacion.objects.select_for_update().get(pk=aprobacion.pk)
    if aprobacion.estado != Aprobacion.Estado.PENDIENTE:
        raise ValidationError("No se puede reasignar una aprobación ya decidida.")
    if not puede_reasignar_aprobacion(actor, aprobacion):
        raise PermissionDenied("No tiene autorización para reasignar esta aprobación.")

    tipo_anterior = aprobacion.tipo_aprobador
    anterior_usuario_id = aprobacion.aprobador_usuario_id
    anterior_equipo_id = aprobacion.aprobador_equipo_id

    if nuevo_aprobador_usuario is not None:
        aprobacion.tipo_aprobador = Aprobacion.TipoAprobador.USUARIO
        aprobacion.aprobador_usuario = nuevo_aprobador_usuario
        aprobacion.aprobador_equipo = None
    else:
        aprobacion.tipo_aprobador = Aprobacion.TipoAprobador.EQUIPO
        aprobacion.aprobador_equipo = nuevo_aprobador_equipo
        aprobacion.aprobador_usuario = None
    aprobacion.save(update_fields=["tipo_aprobador", "aprobador_usuario", "aprobador_equipo", "actualizado_en"])

    ReasignacionAprobacion.objects.create(
        aprobacion=aprobacion,
        aprobador_anterior_usuario_id=anterior_usuario_id,
        aprobador_anterior_equipo_id=anterior_equipo_id,
        aprobador_nuevo_usuario=nuevo_aprobador_usuario,
        aprobador_nuevo_equipo=nuevo_aprobador_equipo,
        reasignado_por=actor,
        motivo=motivo,
    )
    registrar_evento(
        accion=RegistroAuditoria.Accion.ACTUALIZAR,
        instancia=aprobacion,
        origen=RegistroAuditoria.Origen.USUARIO,
        usuario=actor,
        datos_anteriores={
            "tipo_aprobador": tipo_anterior,
            "aprobador_usuario_id": anterior_usuario_id,
            "aprobador_equipo_id": anterior_equipo_id,
        },
        datos_nuevos={
            "tipo_aprobador": aprobacion.tipo_aprobador,
            "aprobador_usuario_id": aprobacion.aprobador_usuario_id,
            "aprobador_equipo_id": aprobacion.aprobador_equipo_id,
            "motivo": motivo,
        },
    )
    return aprobacion
