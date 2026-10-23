"""Reglas de FINALIZACIÓN de un Proceso — Fase 3 del modelo configurable.

Qué debe cumplirse para RESOLVER un ticket de Proceso. Las reglas se configuran en el Proceso
(`catalogo.ReglaFinalizacion`, contrato en `apps.catalogo.gacetas.normalizar_reglas`) y se CONGELAN en el ticket
al crearlo (`Ticket.reglas_finalizacion`; NULL = las reglas por defecto, que un ticket sin gacetas cumple).

Reglas (cada una se puede activar o no):

    actividades_cerradas    ninguna Tarea del ticket abierta (flujo, recorridos, tareas por elemento, correcciones)
    recorridos_terminados   ningún recorrido de ítem o actividad en paralelo en curso o detenido
    aprobaciones_resueltas  ninguna aprobación pendiente del ticket (flujo, recorridos o gacetas)
    gacetas_resueltas       toda gaceta abierta del ticket resuelta; una que nunca se abrió solo cuenta si es
                            OBLIGATORIA (Fase 4.A; las opcionales no bloquean solo por existir)
    sin_correcciones        ningún ítem en corrección en una gaceta
    alcance_cerrado         el alcance de todas sus colecciones cerrado
    items_con_resultado     todo ítem del alcance ORIGINAL vigente con un resultado admitido
                            (APROBADO, COMPLETADO, RECHAZADO, EXCLUIDO — no tiene por qué ser APROBADO)
    minimo_aprobados        al menos N ítems APROBADOS

Esto se SUMA a lo que ya exigía `resolver_ticket` (flujo terminado, entregables obligatorios, tareas por elemento),
sin reemplazarlo. Al resolver, `resumen_resultados` guarda la foto (ítems por resultado, gacetas y reglas
evaluadas) en la `ResolucionTicket`: la decisión final queda trazable.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

ETIQUETAS = {
    "actividades_cerradas": "No quedan actividades abiertas",
    "recorridos_terminados": "Los recorridos por ítem terminaron",
    "aprobaciones_resueltas": "No quedan aprobaciones pendientes",
    "gacetas_resueltas": "Las gacetas están resueltas",
    "sin_correcciones": "No quedan correcciones abiertas",
    "alcance_cerrado": "El alcance de las colecciones está cerrado",
    "items_con_resultado": "Cada ítem tiene un resultado final admitido",
    "minimo_aprobados": "Mínimo de ítems aprobados",
}


@dataclass(frozen=True)
class Regla:
    codigo: str
    descripcion: str
    cumple: bool
    detalle: str = ""


def reglas_del_ticket(ticket):
    from apps.catalogo.gacetas import normalizar_reglas

    return normalizar_reglas(ticket.reglas_finalizacion)[0]


def _ids_de_ejecucion(ticket):
    if ticket.instancia_workflow_id is None:
        return []
    from apps.workflows.integracion import ids_de_ejecucion

    return ids_de_ejecucion(ticket.instancia_workflow_id)


def _tareas_abiertas(ticket):
    from apps.gacetas.models import RondaRevisionGaceta
    from apps.tareas.models import Tarea
    from apps.tickets.models import TareaElemento
    from apps.workflows.models import TareaWorkflow

    abiertas = (Tarea.Estado.PENDIENTE, Tarea.Estado.EN_PROGRESO)
    total = TareaWorkflow.objects.filter(
        instancia_etapa__instancia_workflow_id__in=_ids_de_ejecucion(ticket), tarea__estado__in=abiertas
    ).count()
    total += TareaElemento.objects.filter(elemento__parrilla__ticket=ticket, tarea__estado__in=abiertas).count()
    total += RondaRevisionGaceta.objects.filter(
        participacion__gaceta__ticket=ticket, tarea_correccion__estado__in=abiertas
    ).count()
    return total


def _aprobaciones_pendientes(ticket):
    from apps.aprobaciones.models import EsquemaAprobacion

    ids = _ids_de_ejecucion(ticket)
    flujo = EsquemaAprobacion.objects.filter(
        resultado__isnull=True, vinculo_workflow__instancia_etapa__instancia_workflow_id__in=ids,
        participaciones__estado="PENDIENTE",
    )
    gacetas = EsquemaAprobacion.objects.filter(
        resultado__isnull=True, ronda_gaceta__participacion__gaceta__ticket=ticket, participaciones__estado="PENDIENTE",
    )
    return flujo.distinct().count() + gacetas.distinct().count()


def resultados_de_items(ticket):
    """`{resultado: [ElementoParrilla]}` de los ítems vigentes del ticket (`gacetas.operaciones.resultado_de_item`)."""
    from apps.gacetas.operaciones import resultado_de_item
    from apps.tickets.models import ElementoParrilla

    por_resultado = {}
    for elemento in ElementoParrilla.objects.filter(parrilla__ticket=ticket, retirado_en__isnull=True).order_by(
        "parrilla__orden", "numero"
    ):
        por_resultado.setdefault(resultado_de_item(elemento), []).append(elemento)
    return por_resultado


def evaluar(ticket):
    """`[Regla]` de las reglas ACTIVAS del ticket, con si se cumplen y por qué no. Solo lee."""
    from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
    from apps.tickets.models import ParrillaTicket
    from apps.workflows.models import InstanciaWorkflow

    cfg = reglas_del_ticket(ticket)
    reglas = []

    def agregar(codigo, cumple, detalle=""):
        reglas.append(Regla(codigo, ETIQUETAS[codigo], cumple, "" if cumple else detalle))

    if cfg["actividades_cerradas"]:
        n = _tareas_abiertas(ticket)
        agregar("actividades_cerradas", n == 0, f"{n} actividad(es) abierta(s).")
    if cfg["recorridos_terminados"]:
        n = InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            estado__in=(InstanciaWorkflow.Estado.EN_EJECUCION, InstanciaWorkflow.Estado.EN_ESPERA, InstanciaWorkflow.Estado.ERROR),
        ).count() if ticket.instancia_workflow_id else 0
        agregar("recorridos_terminados", n == 0, f"{n} recorrido(s) en curso o detenido(s).")
    if cfg["aprobaciones_resueltas"]:
        n = _aprobaciones_pendientes(ticket)
        agregar("aprobaciones_resueltas", n == 0, f"{n} aprobación(es) pendiente(s).")
    if cfg["gacetas_resueltas"]:
        sin_resolver = list(
            GacetaTicket.objects.filter(ticket=ticket, estado__in=GacetaTicket.ESTADOS_ACTIVOS).values_list("nombre", flat=True)
        )
        # Fase 4.A: una gaceta OBLIGATORIA que nunca se abrió también impide resolver; una opcional, no.
        sin_resolver += [
            g.nombre for g in GacetaTicket.objects.filter(ticket=ticket, estado=GacetaTicket.Estado.PENDIENTE)
            if (g.configuracion or {}).get("obligatoria")
        ]
        agregar("gacetas_resueltas", not sin_resolver, "Sin resolver: " + ", ".join(sin_resolver) + ".")
    if cfg["sin_correcciones"]:
        n = ParticipacionItemGaceta.objects.filter(
            gaceta__ticket=ticket, estado=ParticipacionItemGaceta.Estado.EN_CORRECCION
        ).exclude(gaceta__estado=GacetaTicket.Estado.CANCELADA).count()
        agregar("sin_correcciones", n == 0, f"{n} ítem(s) en corrección.")
    if cfg["alcance_cerrado"]:
        abiertas = list(
            ParrillaTicket.objects.filter(ticket=ticket, alcance_cerrado_en__isnull=True).values_list("nombre_elemento", flat=True)
        )
        agregar("alcance_cerrado", not abiertas, "Alcance abierto en: " + ", ".join(abiertas) + ".")
    resultados = None
    if cfg["items_con_resultado"]:
        resultados = resultados_de_items(ticket)
        admitidos = set(cfg["resultados_admitidos"])
        fuera = [e for r, lista in resultados.items() if r not in admitidos for e in lista]
        agregar(
            "items_con_resultado", not fuera,
            "Sin resultado admitido: " + ", ".join(f"#{e.numero}" for e in fuera[:10]) + ".",
        )
    if cfg["minimo_aprobados"]:
        resultados = resultados if resultados is not None else resultados_de_items(ticket)
        n = len(resultados.get("APROBADO", []))
        agregar("minimo_aprobados", n >= cfg["minimo_aprobados"], f"Hay {n} de {cfg['minimo_aprobados']} aprobados.")
    return reglas


def exigir(ticket):
    """Rechaza resolver un Proceso con reglas de finalización sin cumplir (nunca un cierre accidental)."""
    pendientes = [r for r in evaluar(ticket) if not r.cumple]
    if pendientes:
        raise ValidationError(
            "No se cumplen las condiciones de cierre: " + " ".join(f"{r.descripcion}: {r.detalle}" for r in pendientes)
        )


def resumen_resultados(ticket):
    """Foto de los resultados al resolver (va en `ResolucionTicket.resumen_resultados`)."""
    from apps.gacetas.models import GacetaTicket

    resultados = resultados_de_items(ticket)
    return {
        "items": {resultado: [e.numero for e in lista] for resultado, lista in resultados.items()},
        "gacetas": [
            {"clave": g.clave, "nombre": g.nombre, "estado": g.estado, "resolucion": g.resolucion}
            for g in GacetaTicket.objects.filter(ticket=ticket).order_by("pk")
        ],
        "reglas": [{"codigo": r.codigo, "cumple": r.cumple} for r in evaluar(ticket)],
    }
