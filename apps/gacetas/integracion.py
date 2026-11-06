"""Gaceta ↔ Workflow — Fase 3 del modelo configurable.

La gaceta NO es una fase ni depende del flujo para recibir ítems (entran a mano, solos o al terminar su
recorrido). El bloque GACETA es una forma OPCIONAL de coordinarla desde la instancia principal:

    BloqueOperativo.configuracion = {"gaceta": <clave>, "presentar_items": bool, "esperar": "RESOLUCION"|"NINGUNA"}

- Al entrar: abre la gaceta si seguía sin abrir y, si se pide, presenta los ítems en el alcance que cumplen sus
  requisitos (idempotente).
- `esperar=NINGUNA`: el flujo sigue (la gaceta opera en paralelo con el resto del proceso).
- `esperar=RESOLUCION`: espera (motivo GACETA) hasta que la gaceta se resuelva; `continuar_por_gaceta` lo libera.
- En ambos casos publica `gacetas.<clave_bloque>.{total, aprobados, rechazados, excluidos, resuelta}` para que
  una DECISION posterior elija la ruta. No hay otro motor ni condiciones paralelas.

Bloque 5.2 — revisión colaborativa DEL ÍTEM: el mismo bloque GACETA, dentro del recorrido de un ítem (rama
ITEM, configuración `{"gaceta": <clave>}`), revisa SOLO ese ítem y deja SOLO su recorrido esperando:

- `entrar_item`: abre la gaceta si seguía sin abrir, presenta el ítem (idempotente: la participación es única
  por gaceta+ítem) y espera (motivo GACETA). Si el ítem ya estaba aprobado (presentado antes a mano), continúa
  siempre que la versión aprobada sea la de hoy. Si no puede entrar (incompleto, gaceta resuelta o cerrada,
  condición no cumplida en una gaceta OBLIGATORIA) es un ERROR funcional de ESE recorrido, visible y
  reintentable (`workflows.integracion.reintentar_recorrido`), nunca un ingreso perdido en silencio. Una gaceta
  opcional cuya condición el ítem no cumple se salta y queda registrado (`resultado = NO_INGRESA`).
- `continuar_por_item`: cuando la gaceta APRUEBA al ítem (`operaciones._finalizar`), reanuda el recorrido de ese
  ítem que la espera en su paso y publica `gacetas.<clave_paso>.resultado`. Devolver lo manda a corrección y el
  recorrido sigue esperando; rechazar o excluir lo saca del alcance y cancela SOLO su recorrido (mecanismo de la
  Fase 3). Idempotente: un segundo aviso ya no encuentra la espera.

Bloque 5.3 — protección del cierre: una gaceta que un recorrido usa como paso por ítem no cierra su periodo ni se
resuelve mientras haya ítems en el alcance que todavía deban llegar a ese paso (`llegadas_pendientes`), ni mientras
el alcance de la colección siga abierto y puedan entrar ítems nuevos; tampoco se resuelve con ítems en revisión o
corrección (cada uno se decide en su recorrido). La regla vive en `operaciones.cerrar_periodo` / `resolver` /
`_resolver_si_corresponde`; la aprobación individual sigue reanudando su recorrido sin esperar el cierre, y la
cancelación del ticket no pasa por aquí.

Sin bloqueos circulares: la resolución de la gaceta no depende de la instancia principal (solo la avisa), los
recorridos de ítems son subinstancias independientes y las decisiones van por `apps.aprobaciones`. Orden de
locks: instancia principal → (subinstancia) → Ticket → gaceta → participación. Toda operación de la gaceta
bloquea primero la principal (`operaciones._bloquear`), así que reanudar una subinstancia desde una decisión
respeta el orden «principal → hija» del motor.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta, RondaRevisionGaceta
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

NO_INGRESA = "NO_INGRESA"
RONDAS_APROBADAS = (RondaRevisionGaceta.Resultado.APROBADA, RondaRevisionGaceta.Resultado.ACEPTADA_AUTOMATICA)


def entrar_al_bloque(ticket, bloque):
    """Entrada del bloque GACETA (la llama su Strategy, dentro del motor). Devuelve `(esperar, resumen)`."""
    from apps.gacetas import operaciones

    cfg = bloque.configuracion or {}
    gaceta = GacetaTicket.objects.select_for_update().filter(ticket=ticket, clave=cfg.get("gaceta") or "").first()
    if gaceta is None:
        raise ValidationError("Este ticket no tiene la gaceta del bloque (se configuró después de crearlo).")
    if gaceta.estado == GacetaTicket.Estado.CANCELADA:
        raise ValidationError("La gaceta de este bloque está cancelada.")
    operaciones._abrir_bloqueada(gaceta)
    if cfg.get("presentar_items", True) and gaceta.estado == GacetaTicket.Estado.ABIERTA:
        operaciones.presentar_por_flujo(gaceta)
    gaceta.refresh_from_db()
    esperar = cfg.get("esperar", "RESOLUCION") == "RESOLUCION" and gaceta.estado != GacetaTicket.Estado.RESUELTA
    return esperar, operaciones.resumen(gaceta)


@transaction.atomic
def continuar_por_gaceta(gaceta):
    """La gaceta se resolvió: si la instancia principal del ticket la está esperando en un bloque GACETA, sigue
    por su ruta con el resumen publicado. Idempotente (un segundo aviso ya no encuentra esa espera)."""
    from apps.gacetas.operaciones import resumen
    from apps.workflows.motor import continuar_espera_externa

    instancia_id = gaceta.ticket.instancia_workflow_id
    if instancia_id is None:
        return False
    instancia = InstanciaWorkflow.objects.select_for_update().get(pk=instancia_id)
    if instancia.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return False
    ejecucion = instancia.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if (
        ejecucion is None
        or ejecucion.estado != InstanciaEtapa.Estado.EN_ESPERA
        or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.GACETA
        or ejecucion.bloque_operativo is None
        or (ejecucion.bloque_operativo.configuracion or {}).get("gaceta") != gaceta.clave
    ):
        return False
    datos = resumen(gaceta)
    continuar_espera_externa(
        ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.GACETA, resultado_bloque=("gacetas", datos),
        resultado_ejecucion=datos,
    )
    return True


# --- Bloque 5.2: revisión colaborativa del ítem ---------------------------------------------------------------


def _ronda_vigente(participacion):
    return RondaRevisionGaceta.objects.select_related("version").filter(
        participacion=participacion, numero=participacion.ronda
    ).first()


def _datos_item(gaceta, elemento, participacion, *, resultado):
    """Lo que el paso publica (`gacetas.<clave_paso>.*`) y deja en su ejecución."""
    ronda = _ronda_vigente(participacion) if participacion is not None else None
    return {
        "gaceta": gaceta.clave, "elemento_id": elemento.pk, "resultado": resultado,
        "participacion_id": participacion.pk if participacion is not None else None,
        "ronda": participacion.ronda if participacion is not None else 0,
        "version": ronda.version.numero if ronda is not None and ronda.version_id else None,
    }


def _motivo_aprobacion_no_vigente(participacion, elemento, *, verificar_version):
    """Por qué la aprobación del ítem no vale para reanudar su recorrido, o `None`. Vale la que cerró la ronda
    VIGENTE de la participación (aprobada o aceptada); con `verificar_version`, además, la versión aprobada
    debe ser el contenido actual del ítem (se usa cuando el ítem llega a su paso ya aprobado de antes)."""
    from apps.tickets.parrilla import contenido_de, huella_de

    ronda = _ronda_vigente(participacion)
    if ronda is None or ronda.resultado not in RONDAS_APROBADAS:
        return "la gaceta no registró la aprobación de su ronda vigente"
    if verificar_version and ronda.version_id and ronda.version.huella != huella_de(contenido_de(elemento)):
        return (
            f"la gaceta aprobó su versión {ronda.version.numero}, pero el ítem cambió después y la misma gaceta no "
            "lo revisa dos veces; restablece la versión aprobada y reintenta, o exclúyelo"
        )
    return None


def entrar_item(ticket, bloque, elemento):
    """Entrada del paso GACETA en el recorrido de `elemento` (la llama su Strategy, dentro del motor, con la
    principal y la subinstancia bloqueadas). Devuelve `(esperar, datos)`; `ValidationError` = error funcional
    reintentable de ESE recorrido."""
    from apps.catalogo.gacetas import errores_de_uso_por_item
    from apps.gacetas import operaciones
    from apps.tickets.models import ElementoParrilla
    from apps.workflows.recorridos import _cumple_ingreso

    clave = (bloque.configuracion or {}).get("gaceta") or ""
    gaceta = GacetaTicket.objects.select_for_update().filter(ticket=ticket, clave=clave).first()
    if gaceta is None:
        raise ValidationError("Este ticket no tiene la gaceta de este paso (se configuró después de crearlo).")
    if gaceta.estado == GacetaTicket.Estado.CANCELADA:
        raise ValidationError(f"La gaceta «{gaceta.nombre}» está cancelada.")
    elemento = ElementoParrilla.objects.select_related("parrilla").get(pk=elemento.pk)
    numero = f"#{elemento.numero}"
    participacion = ParticipacionItemGaceta.objects.select_for_update().filter(gaceta=gaceta, elemento=elemento).first()
    if participacion is not None:
        # Ya estaba presentado (reintento, o presentación manual previa): se sigue su estado en la gaceta.
        if participacion.estado == ParticipacionItemGaceta.Estado.APROBADA:
            motivo = _motivo_aprobacion_no_vigente(participacion, elemento, verificar_version=True)
            if motivo:
                raise ValidationError(f"El ítem {numero} no puede continuar desde «{gaceta.nombre}»: {motivo}.")
            return False, _datos_item(gaceta, elemento, participacion, resultado=participacion.estado)
        if participacion.estado in ParticipacionItemGaceta.ESTADOS_PENDIENTES:
            return True, _datos_item(gaceta, elemento, participacion, resultado=participacion.estado)
        raise ValidationError(
            f"El ítem {numero} ya tiene el resultado «{participacion.get_estado_display()}» en «{gaceta.nombre}»: "
            "su recorrido no continúa."
        )
    cfg = gaceta.configuracion or {}
    incompatibles = errores_de_uso_por_item(cfg)
    if incompatibles:
        raise ValidationError(
            f"La gaceta «{gaceta.nombre}» de este ticket no admite revisar ítems en su recorrido: "
            + "; ".join(incompatibles) + "."
        )
    if not _cumple_ingreso(elemento, cfg.get("condicion_ingreso")):
        if cfg.get("obligatoria"):
            # Una gaceta obligatoria nunca se omite en silencio.
            raise ValidationError(
                f"El ítem {numero} no cumple la condición de ingreso de la gaceta obligatoria «{gaceta.nombre}»: "
                "corrígelo y reintenta el recorrido, o exclúyelo."
            )
        return False, _datos_item(gaceta, elemento, None, resultado=NO_INGRESA)
    if gaceta.estado == GacetaTicket.Estado.RESUELTA:
        raise ValidationError(f"La gaceta «{gaceta.nombre}» ya se resolvió: el ítem {numero} no puede entrar a revisión.")
    operaciones._abrir_bloqueada(gaceta)
    motivo = operaciones._motivo_para_no_ingresar(gaceta, elemento)
    if motivo:
        raise ValidationError(
            f"El ítem {numero} no puede entrar a la gaceta «{gaceta.nombre}»: {motivo} Corrígelo y reintenta el recorrido."
        )
    with transaction.atomic():
        # Punto de guardado: si la ronda no se puede abrir (p. ej. sin aprobadores activos) no queda una
        # participación a medias.
        participacion = operaciones._presentar_bloqueado(
            gaceta, elemento, None, origen=ParticipacionItemGaceta.Origen.FLUJO, silencioso=False
        )
    return True, _datos_item(gaceta, elemento, participacion, resultado=participacion.estado)


@transaction.atomic
def continuar_por_item(participacion):
    """La gaceta APROBÓ al ítem de `participacion`: reanuda el recorrido de ese ítem que la espera en un paso
    GACETA de esa gaceta. Se llama desde `operaciones._finalizar`, con la principal, el Ticket, la gaceta y la
    participación ya bloqueados. Los recorridos de los demás ítems no se tocan. Idempotente. Devuelve cuántos
    recorridos reanudó."""
    from apps.workflows.motor import bloquear_con_padre, continuar_espera_externa

    if participacion.estado != ParticipacionItemGaceta.Estado.APROBADA:
        return 0
    gaceta, elemento = participacion.gaceta, participacion.elemento
    if _motivo_aprobacion_no_vigente(participacion, elemento, verificar_version=False):
        return 0
    reanudados = 0
    for hija_id in InstanciaWorkflow.objects.filter(
        elemento_id=elemento.pk, instancia_padre__isnull=False, estado=InstanciaWorkflow.Estado.EN_ESPERA
    ).order_by("pk").values_list("pk", flat=True):
        hija = bloquear_con_padre(hija_id)
        if hija.estado != InstanciaWorkflow.Estado.EN_ESPERA:
            continue
        ejecucion = (
            InstanciaEtapa.objects.select_for_update(of=("self",)).select_related("bloque_operativo")
            .filter(instancia_workflow_id=hija.pk, estado=InstanciaEtapa.Estado.EN_ESPERA)
            .order_by("-orden").first()
        )
        if (
            ejecucion is None
            or ejecucion.motivo_espera != InstanciaEtapa.MotivoEspera.GACETA
            or ejecucion.bloque_operativo is None
            or ejecucion.bloque_operativo.rama != "ITEM"
            or (ejecucion.bloque_operativo.configuracion or {}).get("gaceta") != gaceta.clave
        ):
            continue
        datos = _datos_item(gaceta, elemento, participacion, resultado=participacion.estado)
        continuar_espera_externa(
            ejecucion, motivo_espera=InstanciaEtapa.MotivoEspera.GACETA, resultado_bloque=("gacetas", datos),
            resultado_ejecucion=datos,
        )
        reanudados += 1
    return reanudados


# --- Bloque 5.3: protección del cierre ------------------------------------------------------------------------


@dataclass(frozen=True)
class Llegadas:
    """Lo que todavía puede llegar a una gaceta por los recorridos de sus ítems. `por_llegar`: números de los ítems
    en el alcance cuyo recorrido (en curso, detenido o aún sin lanzar) no ha pasado el paso de la gaceta ni la
    está esperando con la revisión en curso. `alcances_abiertos`: colecciones cuyo alcance sigue abierto mientras
    el recorrido aún puede recibir ítems nuevos."""

    usada_por_item: bool = False
    por_llegar: tuple = ()
    alcances_abiertos: tuple = ()

    @property
    def bloquea(self):
        return bool(self.por_llegar or self.alcances_abiertos)


def llegadas_pendientes(gaceta):
    """Solo lee. Los pasos se buscan en la configuración CONGELADA de la instancia principal del ticket (la que
    ejecutan sus recorridos), por la clave de la gaceta. Una gaceta que ningún recorrido usa (gaceta general)
    devuelve `Llegadas()` y conserva su comportamiento."""
    from apps.catalogo.models import BloqueOperativo
    from apps.tickets.models import Ticket
    from apps.tickets.parrilla import elementos_en_alcance, parrilla_de
    from apps.workflows import recorridos

    ticket = Ticket.objects.filter(pk=gaceta.ticket_id).first()
    principal = (
        InstanciaWorkflow.objects.filter(pk=ticket.instancia_workflow_id).first()
        if ticket is not None and ticket.instancia_workflow_id else None
    )
    if principal is None or principal.configuracion_ejecucion_version_id is None:
        return Llegadas()
    pasos = list(
        BloqueOperativo.objects.select_related("contenedor").filter(
            version_id=principal.configuracion_ejecucion_version_id, tipo=BloqueOperativo.Tipo.GACETA,
            rama=BloqueOperativo.Rama.ITEM, configuracion__gaceta=gaceta.clave,
        )
    )
    if not pasos:
        return Llegadas()
    activos = (*recorridos.ESTADOS_EN_CURSO, InstanciaWorkflow.Estado.ERROR)
    principal_activa = principal.estado in activos
    en_revision = set(
        ParticipacionItemGaceta.objects.filter(
            gaceta=gaceta, estado__in=ParticipacionItemGaceta.ESTADOS_PENDIENTES
        ).values_list("elemento_id", flat=True)
    )
    por_llegar, abiertas = {}, []
    for paso in pasos:
        contenedor = paso.contenedor
        configuracion = recorridos.configuracion_de(contenedor)
        parrilla = parrilla_de(ticket, configuracion.coleccion) if configuracion.coleccion else None
        if parrilla is None:
            continue
        en_alcance = {e.pk: e.numero for e in elementos_en_alcance(parrilla)}
        hijas = list(
            InstanciaWorkflow.objects.filter(
                instancia_padre_id=principal.pk, ejecucion_padre__bloque_operativo_id=contenedor.pk,
                elemento__isnull=False,
            ).only("pk", "estado", "elemento_id")
        )
        pasaron = set(
            InstanciaEtapa.objects.filter(
                instancia_workflow_id__in=[h.pk for h in hijas], bloque_operativo_id=paso.pk,
                estado=InstanciaEtapa.Estado.COMPLETADA,
            ).values_list("instancia_workflow__elemento_id", flat=True)
        )
        terminados = {h.elemento_id for h in hijas if h.estado not in activos}
        # Recorridos en curso o detenidos (un ingreso fallido es un ERROR reintentable: sigue por llegar).
        for hija in hijas:
            if hija.estado in activos and hija.elemento_id in en_alcance and hija.elemento_id not in pasaron | en_revision:
                por_llegar[hija.elemento_id] = en_alcance[hija.elemento_id]
        ejecucion = (
            InstanciaEtapa.objects.filter(instancia_workflow_id=principal.pk, bloque_operativo_id=contenedor.pk)
            .order_by("-orden").first()
        )
        estado_contenedor = ejecucion.estado if ejecucion is not None else None
        # Sin lanzar (o detenido al lanzar, reintentable) mientras la principal sigue viva.
        por_lanzar = principal_activa and estado_contenedor not in (
            InstanciaEtapa.Estado.EN_ESPERA, InstanciaEtapa.Estado.COMPLETADA
        )
        recibiendo = estado_contenedor == InstanciaEtapa.Estado.EN_ESPERA
        if por_lanzar:
            # El recorrido aún no se lanzó: cada ítem del alcance que cumpla su condición de ingreso llegará.
            for elemento in elementos_en_alcance(parrilla):
                if (
                    elemento.pk not in pasaron | terminados | en_revision
                    and recorridos._cumple_ingreso(elemento, configuracion.condicion_ingreso)
                ):
                    por_llegar[elemento.pk] = elemento.numero
        if not parrilla.alcance_cerrado and (por_lanzar or (recibiendo and configuracion.incorporar_nuevos)):
            if parrilla.nombre_elemento not in abiertas:
                abiertas.append(parrilla.nombre_elemento)
    return Llegadas(
        usada_por_item=True, por_llegar=tuple(sorted(por_llegar.values())), alcances_abiertos=tuple(abiertas)
    )


def bloqueos_de_cierre(gaceta, *, resolver):
    """Por qué la gaceta todavía no puede cerrar su periodo (`resolver=False`) o resolverse (`resolver=True`)
    por estar en uso en recorridos por ítem; lista vacía si puede. Las gacetas generales nunca tienen bloqueos
    aquí (siguen con su regla `al_resolver_pendientes`)."""
    llegadas = llegadas_pendientes(gaceta)
    if not llegadas.usada_por_item:
        return []
    accion = "resolverla" if resolver else "cerrar su periodo"
    motivos = []
    if llegadas.por_llegar:
        motivos.append(
            f"La gaceta «{gaceta.nombre}» revisa cada ítem en su recorrido y todavía faltan ítems por llegar: "
            + ", ".join(f"#{n}" for n in llegadas.por_llegar)
            + f". Cuando lleguen y se decidan, o se excluyan, podrás {accion}."
        )
    for nombre in llegadas.alcances_abiertos:
        motivos.append(
            f"El alcance de «{nombre}» sigue abierto y pueden llegar ítems nuevos a la gaceta «{gaceta.nombre}»: "
            f"ciérralo antes de {accion}."
        )
    if resolver:
        pendientes = list(
            ParticipacionItemGaceta.objects.filter(
                gaceta=gaceta, estado__in=ParticipacionItemGaceta.ESTADOS_PENDIENTES
            ).select_related("elemento").order_by("elemento__numero")
        )
        if pendientes:
            motivos.append(
                f"Hay ítems de la gaceta «{gaceta.nombre}» en revisión o corrección: "
                + ", ".join(f"#{p.elemento.numero}" for p in pendientes)
                + ". Cada uno espera en su recorrido: apruébalos, recházalos o exclúyelos antes de resolverla."
            )
    return motivos
