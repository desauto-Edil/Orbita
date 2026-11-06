"""Recorridos independientes por ítem y actividades en paralelo — Fase 2 del modelo configurable.

Un bloque CONTENEDOR (`GRUPO_ACTIVIDADES` o `RECORRIDO_ITEMS`) de la instancia principal lanza SUBINSTANCIAS
(`InstanciaWorkflow` hija) que ejecuta el MISMO motor (`apps.workflows.motor`). No hay otro motor ni tickets
adicionales: cada subinstancia tiene su propio punto activo (la invariante del motor no cambia), sus propias
Tareas y aprobaciones (`apps.tareas` / `apps.aprobaciones`, sin duplicar nada) y su propia trazabilidad
(`InstanciaEtapa`, auditoría). La principal espera en el contenedor y este módulo la libera cuando la última
subinstancia termina.

Qué se lanza (`lanzar`), una sola vez por ejecución del contenedor (idempotente por las restricciones únicas de
`InstanciaWorkflow`):

- Rama GENERAL: una subinstancia por cada bloque general del contenedor (actividades generales que corren en
  paralelo entre sí y con los recorridos de los ítems).
- Rama ITEM (solo RECORRIDO_ITEMS): una subinstancia por cada ítem EN EL ALCANCE de la colección configurada
  (vigente y sin decisión de salida) que cumpla la condición de ingreso, si la hay. Recorre la secuencia de
  bloques ITEM del contenedor. Los ítems avanzan en momentos distintos; una devolución vuelve atrás SOLO en el
  recorrido de su ítem.

Configuración del RECORRIDO_ITEMS (`BloqueOperativo.configuracion`):

    coleccion              clave de la colección del Proceso (obligatoria)
    condicion_ingreso      {"variable": "item.<clave>", "operador": ..., "valor": ...} (opcional)
    incorporar_nuevos      bool: un ítem que se agrega mientras el bloque espera entra a su recorrido
    espera_alcance_cerrado bool: el bloque no termina mientras el alcance de la colección siga abierto

UX-4 (opcionales; sin ellas todo funciona como antes):

    ingreso                "AUTOMATICO" (por defecto: entra al lanzarse o al agregarse) | "AL_ENVIAR": cada ítem
                           es un BORRADOR (en el alcance, sin recorrido) hasta que alguien lo ENVÍA
                           (`enviar_elemento`); nunca se lanza solo
    paso_al_enviar         clave del bloque ITEM por el que entra un ítem enviado (por defecto, el primero). Así
                           un paso anterior (p. ej. «Ajustar la idea») solo existe cuando una ruta «Devuelta»
                           vuelve a él
    columnas_requeridas_ingreso  claves de columna que deben estar completas para enviar
    envio                  "INDIVIDUAL" (por defecto: cada ítem se envía por separado) | "CONJUNTO": los
                           borradores se envían TODOS JUNTOS (`enviar_conjunto`) y ese envío cierra el alcance:
                           ninguno avanza antes y después no se agregan más (solo se corrigen los devueltos)
    etapa_borradores       nombre visible de la etapa de borradores (p. ej. «Lluvia de ideas»)

Un borrador no es un estado guardado: es un ítem en el alcance sin subinstancia en ESTA ejecución. Con ingreso
AL_ENVIAR el contenedor no termina mientras quede alguno (`Progreso.borradores`): se envía o sale del alcance.

Cuándo termina el contenedor (`progreso(...).terminado`): no queda ninguna subinstancia en curso ni con error
y, si se configuró, el alcance de la colección ya se cerró. Al terminar publica `recorridos.<clave>.*` o
`grupos.<clave>.*` (totales por resultado) y las aprobaciones de las ramas generales quedan disponibles como
`aprobaciones.<clave>.resultado` en la principal, para que una DECISION posterior las use.

Resultado de un recorrido de ítem (`contexto["resultado_recorrido"]` de la subinstancia): COMPLETADO (recorrió
su secuencia), EXCLUIDO o RECHAZADO (su ítem salió del alcance por una aprobación o una decisión manual),
RETIRADO o CANCELADO. Un rechazo o una exclusión solo afecta a SU ítem: los demás siguen.

Orden de locks: instancia principal → subinstancias → Ticket → colección → tareas/aprobaciones → etapa
(`motor.bloquear_con_padre`). Todo ocurre dentro de la transacción de quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.workflows.contexto import INEXISTENTE, evaluar_operador
from apps.workflows.models import InstanciaEtapa, InstanciaWorkflow

TIPO_GRUPO = "GRUPO_ACTIVIDADES"
TIPO_RECORRIDO = "RECORRIDO_ITEMS"
MOTIVOS = (InstanciaEtapa.MotivoEspera.RECORRIDO, InstanciaEtapa.MotivoEspera.GRUPO)
AMBITO_POR_TIPO = {TIPO_GRUPO: "grupos", TIPO_RECORRIDO: "recorridos"}

COMPLETADO = "COMPLETADO"
EXCLUIDO = "EXCLUIDO"
RECHAZADO = "RECHAZADO"
RETIRADO = "RETIRADO"
CANCELADO = "CANCELADO"
ESTADOS_EN_CURSO = (InstanciaWorkflow.Estado.EN_EJECUCION, InstanciaWorkflow.Estado.EN_ESPERA)


# --- Configuración -----------------------------------------------------------------------------------------


INGRESO_AUTOMATICO = "AUTOMATICO"
INGRESO_AL_ENVIAR = "AL_ENVIAR"
INGRESOS = (INGRESO_AUTOMATICO, INGRESO_AL_ENVIAR)
ENVIO_INDIVIDUAL = "INDIVIDUAL"
ENVIO_CONJUNTO = "CONJUNTO"
ENVIOS = (ENVIO_INDIVIDUAL, ENVIO_CONJUNTO)


@dataclass(frozen=True)
class ConfiguracionRecorrido:
    coleccion: str = ""
    condicion_ingreso: dict | None = None
    incorporar_nuevos: bool = False
    espera_alcance_cerrado: bool = False
    ingreso: str = INGRESO_AUTOMATICO
    paso_al_enviar: str = ""
    columnas_requeridas_ingreso: tuple = ()
    envio: str = ENVIO_INDIVIDUAL
    etapa_borradores: str = ""

    @property
    def al_enviar(self):
        return self.ingreso == INGRESO_AL_ENVIAR

    @property
    def en_conjunto(self):
        return self.al_enviar and self.envio == ENVIO_CONJUNTO


def configuracion_de(bloque):
    cfg = bloque.configuracion or {}
    condicion = cfg.get("condicion_ingreso")
    if not (isinstance(condicion, dict) and condicion.get("variable") and condicion.get("operador")):
        condicion = None
    return ConfiguracionRecorrido(
        coleccion=str(cfg.get("coleccion") or ""),
        condicion_ingreso=condicion,
        incorporar_nuevos=bool(cfg.get("incorporar_nuevos")),
        espera_alcance_cerrado=bool(cfg.get("espera_alcance_cerrado")),
        ingreso=cfg.get("ingreso") if cfg.get("ingreso") in INGRESOS else INGRESO_AUTOMATICO,
        paso_al_enviar=str(cfg.get("paso_al_enviar") or ""),
        columnas_requeridas_ingreso=tuple(
            c for c in (cfg.get("columnas_requeridas_ingreso") or ()) if isinstance(c, str) and c
        ),
        envio=cfg.get("envio") if cfg.get("envio") in ENVIOS else ENVIO_INDIVIDUAL,
        etapa_borradores=str(cfg.get("etapa_borradores") or "")[:60],
    )


def errores_de_configuracion(cfg, *, claves_bloques_item=(), claves_columnas=None):
    """UX-4 — errores de las claves nuevas de un RECORRIDO_ITEMS (las de siempre las valida el Studio). Lista
    vacía = válida. `claves_bloques_item` y `claves_columnas` permiten comprobar las referencias al publicar."""
    cfg = cfg or {}
    errores = []
    ingreso = cfg.get("ingreso") or INGRESO_AUTOMATICO
    if ingreso not in INGRESOS:
        errores.append("El modo de ingreso del recorrido no es válido.")
    paso = cfg.get("paso_al_enviar") or ""
    if paso and ingreso != INGRESO_AL_ENVIAR:
        errores.append("El paso de entrada solo aplica cuando los ítems entran al enviarse.")
    if paso and claves_bloques_item and paso not in claves_bloques_item:
        errores.append("El paso por el que entra un ítem enviado no es un paso de este recorrido.")
    envio = cfg.get("envio") or ENVIO_INDIVIDUAL
    if envio not in ENVIOS:
        errores.append("El modo de envío del recorrido no es válido.")
    elif envio == ENVIO_CONJUNTO and ingreso != INGRESO_AL_ENVIAR:
        errores.append("Enviar los ítems en conjunto solo aplica cuando los ítems entran al enviarse.")
    if len(str(cfg.get("etapa_borradores") or "")) > 60:
        errores.append("El nombre de la etapa de borradores es demasiado largo (máximo 60 caracteres).")
    requeridas = cfg.get("columnas_requeridas_ingreso") or []
    if not isinstance(requeridas, (list, tuple)) or not all(isinstance(c, str) and c for c in requeridas):
        errores.append("Las columnas exigidas para enviar deben indicarse por su clave.")
    elif claves_columnas is not None:
        faltan = [c for c in requeridas if c not in claves_columnas]
        if faltan:
            errores.append(f"Las columnas exigidas para enviar no son de la colección ({', '.join(faltan)}).")
    return errores


def bloques_de_rama(contenedor, rama):
    return list(contenedor.contenidos.filter(rama=rama).order_by("orden", "pk"))


# --- Lectura -----------------------------------------------------------------------------------------------


def _ticket_de(instancia):
    from apps.tickets.models import Ticket

    ticket = Ticket.objects.filter(instancia_workflow_id=instancia.raiz_id).first()
    if ticket is not None:
        return ticket
    # El motor ejecuta los primeros bloques ANTES de que la radicación guarde `Ticket.instancia_workflow` (igual
    # que `actores._ticket_vigente`): si el recorrido es el primer bloque, solo en ese arranque se usa el
    # `ticket_id` que la radicación dejó en los datos iniciales de la instancia principal.
    raiz = instancia if instancia.pk == instancia.raiz_id else InstanciaWorkflow.objects.filter(pk=instancia.raiz_id).first()
    ticket_id = ((raiz.contexto if raiz else {}) or {}).get("datos_iniciales", {}).get("ticket_id")
    if not isinstance(ticket_id, int) or isinstance(ticket_id, bool):
        return None
    return Ticket.objects.filter(
        pk=ticket_id, estado=Ticket.Estado.BORRADOR, instancia_workflow__isnull=True,
        detalle_servicio__servicio__workflow_id=instancia.workflow_version.workflow_id,
    ).first()


def _coleccion(ticket, clave):
    from apps.tickets.parrilla import parrilla_de

    parrilla = parrilla_de(ticket, clave) if clave else None
    if parrilla is None:
        raise ValidationError(
            "Este ticket no tiene la colección que recorre el bloque (se agregó o cambió después de crearlo)."
        )
    return parrilla


def _cumple_ingreso(elemento, condicion):
    if not condicion:
        return True
    variable = str(condicion.get("variable") or "")
    if not variable.startswith("item."):
        return True
    clave = variable.split(".", 1)[1]
    valores = {v.campo.clave: v.valor for v in elemento.valores.select_related("campo")}
    columnas = {c.clave for c in elemento.parrilla.formulario_version.campos.all()}
    actual = valores.get(clave) if clave in columnas else INEXISTENTE
    return evaluar_operador(condicion.get("operador"), actual, str(condicion.get("valor") or ""))


def resultado_de(hija):
    """Resultado de una subinstancia: el que quedó registrado o, si sigue abierta, su estado."""
    registrado = (hija.contexto or {}).get("resultado_recorrido")
    if registrado:
        return registrado
    if hija.estado == InstanciaWorkflow.Estado.COMPLETADA:
        return COMPLETADO
    if hija.estado == InstanciaWorkflow.Estado.CANCELADA:
        return CANCELADO
    return hija.estado


@dataclass(frozen=True)
class Progreso:
    total: int = 0
    en_curso: int = 0
    con_error: int = 0
    completados: int = 0
    excluidos: int = 0
    rechazados: int = 0
    retirados: int = 0
    cancelados: int = 0
    pendiente_alcance: bool = False
    borradores: int = 0  # UX-4: ítems en el alcance aún sin enviar (ingreso AL_ENVIAR)

    @property
    def terminado(self):
        return self.en_curso == 0 and self.con_error == 0 and not self.pendiente_alcance and self.borradores == 0

    def datos(self):
        return {
            "total": self.total, "en_curso": self.en_curso, "con_error": self.con_error,
            "completados": self.completados, "completadas": self.completados, "excluidos": self.excluidos,
            "rechazados": self.rechazados, "retirados": self.retirados, "cancelados": self.cancelados,
            "pendiente_alcance": self.pendiente_alcance, "borradores": self.borradores,
        }


def progreso(ejecucion):
    """Estado agregado de las subinstancias que lanzó `ejecucion` (la ejecución del contenedor). Solo lee."""
    conteo = {"en_curso": 0, "con_error": 0, COMPLETADO: 0, EXCLUIDO: 0, RECHAZADO: 0, RETIRADO: 0, CANCELADO: 0}
    hijas = list(InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk))
    for hija in hijas:
        if hija.estado in ESTADOS_EN_CURSO:
            conteo["en_curso"] += 1
        elif hija.estado == InstanciaWorkflow.Estado.ERROR:
            conteo["con_error"] += 1
        else:
            conteo[resultado_de(hija) if resultado_de(hija) in conteo else COMPLETADO] += 1
    pendiente_alcance = False
    borradores = 0
    bloque = ejecucion.bloque_operativo
    if bloque is not None and bloque.tipo == TIPO_RECORRIDO:
        configuracion = configuracion_de(bloque)
        if configuracion.espera_alcance_cerrado or configuracion.al_enviar:
            ticket = _ticket_de(ejecucion.instancia_workflow)
            from apps.tickets.parrilla import parrilla_de

            parrilla = parrilla_de(ticket, configuracion.coleccion) if ticket is not None else None
            # Con ingreso AL_ENVIAR siempre se espera el cierre del alcance: si no, nadie podría enviar después.
            pendiente_alcance = parrilla is not None and not parrilla.alcance_cerrado
            if configuracion.al_enviar and parrilla is not None:
                borradores = len(borradores_de(ejecucion, parrilla, hijas=hijas))
    return Progreso(
        total=len(hijas), en_curso=conteo["en_curso"], con_error=conteo["con_error"],
        completados=conteo[COMPLETADO], excluidos=conteo[EXCLUIDO], rechazados=conteo[RECHAZADO],
        retirados=conteo[RETIRADO], cancelados=conteo[CANCELADO], pendiente_alcance=pendiente_alcance,
        borradores=borradores,
    )


def borradores_de(ejecucion, parrilla, *, hijas=None):
    """UX-4 — ítems de la colección EN EL ALCANCE que todavía no tienen recorrido en esta ejecución (con ingreso
    AL_ENVIAR, son los borradores por enviar). Solo lee."""
    from apps.tickets.parrilla import elementos_en_alcance

    if hijas is None:
        hijas = InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk)
    con_recorrido = {h.elemento_id for h in hijas if h.elemento_id}
    return [e for e in elementos_en_alcance(parrilla) if e.pk not in con_recorrido]


def subinstancias_de(ejecucion):
    return list(
        InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk)
        .select_related("elemento__parrilla", "bloque_rama").order_by("pk")
    )


# --- Lanzar ------------------------------------------------------------------------------------------------


def lanzar(ejecucion, *, actor=None):
    """Lanza lo que falte para la ejecución de un contenedor (ver el docstring del módulo). Idempotente: una
    rama o un ítem que ya tiene su subinstancia en ESTA ejecución no se vuelve a lanzar. Se llama dentro del
    motor, con la instancia principal bloqueada."""
    from apps.workflows.motor import iniciar_subinstancia

    bloque = ejecucion.bloque_operativo
    padre = ejecucion.instancia_workflow
    if padre.instancia_padre_id is not None:
        raise ValidationError("Un contenedor no puede ejecutarse dentro de otro recorrido.")
    existentes = InstanciaWorkflow.objects.filter(ejecucion_padre_id=ejecucion.pk)
    ramas_lanzadas = set(existentes.exclude(bloque_rama__isnull=True).values_list("bloque_rama_id", flat=True))
    for general in bloques_de_rama(bloque, "GENERAL"):
        if general.pk not in ramas_lanzadas:
            iniciar_subinstancia(padre, ejecucion, general, bloque_rama=general, actor=actor)
    if bloque.tipo != TIPO_RECORRIDO:
        return
    secuencia = bloques_de_rama(bloque, "ITEM")
    if not secuencia:
        return
    configuracion = configuracion_de(bloque)
    ticket = _ticket_de(padre)
    if ticket is None:
        raise ValidationError("El recorrido por ítem necesita una ejecución vinculada a un Ticket.")
    parrilla = _coleccion(ticket, configuracion.coleccion)
    if configuracion.al_enviar:
        return  # UX-4: cada ítem entra cuando alguien lo envía (`enviar_elemento`), nunca solo
    from apps.tickets.parrilla import elementos_en_alcance

    con_recorrido = set(existentes.exclude(elemento__isnull=True).values_list("elemento_id", flat=True))
    for elemento in elementos_en_alcance(parrilla):
        if elemento.pk in con_recorrido or not _cumple_ingreso(elemento, configuracion.condicion_ingreso):
            continue
        iniciar_subinstancia(padre, ejecucion, secuencia[0], elemento=elemento, actor=actor)


def _ejecucion_vigente_en_espera(instancia_id):
    """La ejecución vigente de la instancia principal si está esperando a sus subinstancias, o `None`."""
    padre = InstanciaWorkflow.objects.filter(pk=instancia_id).first()
    if padre is None or padre.estado != InstanciaWorkflow.Estado.EN_ESPERA:
        return None
    ultima = padre.ejecuciones_etapa.select_related("bloque_operativo").order_by("-orden").first()
    if ultima is None or ultima.estado != InstanciaEtapa.Estado.EN_ESPERA or ultima.motivo_espera not in MOTIVOS:
        return None
    return ultima


def incorporar_elemento(ticket, elemento, *, actor=None):
    """Un ítem nuevo entra al recorrido que la instancia principal está esperando, si ese recorrido es de su
    colección, admite ítems nuevos y el ítem cumple la condición de ingreso. Lo llaman las operaciones de la
    colección (alta normal o extraordinaria) con la instancia principal ya bloqueada. Devuelve la subinstancia
    o `None`."""
    if ticket.instancia_workflow_id is None:
        return None
    ejecucion = _ejecucion_vigente_en_espera(ticket.instancia_workflow_id)
    if ejecucion is None or ejecucion.bloque_operativo.tipo != TIPO_RECORRIDO:
        return None
    bloque = ejecucion.bloque_operativo
    configuracion = configuracion_de(bloque)
    if configuracion.al_enviar or not configuracion.incorporar_nuevos or configuracion.coleccion != elemento.parrilla.clave:
        return None
    secuencia = bloques_de_rama(bloque, "ITEM")
    if not secuencia or InstanciaWorkflow.objects.filter(ejecucion_padre=ejecucion, elemento=elemento).exists():
        return None
    if not _cumple_ingreso(elemento, configuracion.condicion_ingreso):
        return None
    from apps.workflows.motor import iniciar_subinstancia

    padre = InstanciaWorkflow.objects.select_for_update().get(pk=ticket.instancia_workflow_id)
    ejecucion = InstanciaEtapa.objects.select_for_update().get(pk=ejecucion.pk)
    return iniciar_subinstancia(padre, ejecucion, secuencia[0], elemento=elemento, actor=actor)


# --- Terminar ----------------------------------------------------------------------------------------------


def _fusionar_resultados_generales(padre, ejecucion):
    """Las aprobaciones de las ramas GENERALES quedan publicadas también en la principal
    (`aprobaciones.<clave>.resultado`), para que un bloque posterior las consulte. Las de los ítems no: cada
    ítem tiene la suya y una DECISION de la principal no puede elegir entre ellas."""
    destino = padre.contexto.setdefault("resultados_bloques", {})
    for hija in InstanciaWorkflow.objects.filter(ejecucion_padre=ejecucion, bloque_rama__isnull=False):
        for ambito, por_clave in ((hija.contexto or {}).get("resultados_bloques") or {}).items():
            destino.setdefault(ambito, {}).update(por_clave or {})


@transaction.atomic
def evaluar_espera(ejecucion_id):
    """Si la ejecución de un contenedor sigue esperando y ya no queda nada en curso, la libera y la instancia
    principal continúa por su ruta (con el resumen publicado). Idempotente: si ya continuó, o si no es la
    ejecución vigente, no hace nada. Devuelve `True` si continuó."""
    from apps.workflows.motor import bloquear_con_padre, continuar_espera_externa

    instancia_id = InstanciaEtapa.objects.filter(pk=ejecucion_id).values_list("instancia_workflow_id", flat=True).first()
    if instancia_id is None:
        return False
    padre = bloquear_con_padre(instancia_id)
    ejecucion = _ejecucion_vigente_en_espera(padre.pk)
    if ejecucion is None or ejecucion.pk != ejecucion_id:
        return False
    avance = progreso(ejecucion)
    if not avance.terminado:
        ejecucion.resultado = {**(ejecucion.resultado or {}), **avance.datos()}
        ejecucion.save(update_fields=["resultado", "actualizado_en"])
        return False
    _fusionar_resultados_generales(padre, ejecucion)
    padre.save(update_fields=["contexto", "actualizado_en"])
    continuar_espera_externa(
        ejecucion,
        motivo_espera=ejecucion.motivo_espera,
        resultado_bloque=(AMBITO_POR_TIPO[ejecucion.bloque_operativo.tipo], avance.datos()),
        resultado_ejecucion=avance.datos(),
    )
    return True


def reevaluar_ticket(ticket):
    """Vuelve a evaluar el contenedor que la instancia principal del Ticket esté esperando (p. ej. tras cerrar
    el alcance de una colección). Idempotente."""
    if ticket.instancia_workflow_id is None:
        return False
    ejecucion = _ejecucion_vigente_en_espera(ticket.instancia_workflow_id)
    if ejecucion is None:
        return False
    return evaluar_espera(ejecucion.pk)


def recorridos_activos_de(elemento):
    """Subinstancias de `elemento` que siguen en curso (o con error) en cualquier ejecución."""
    return list(
        InstanciaWorkflow.objects.filter(
            elemento=elemento,
            estado__in=(*ESTADOS_EN_CURSO, InstanciaWorkflow.Estado.ERROR),
        ).order_by("pk")
    )


def sacar_elemento(elemento, actor, *, resultado, motivo=""):
    """El ítem salió del alcance (excluido o rechazado manualmente) o se retiró: su recorrido en curso se
    detiene (sus Tareas y aprobaciones abiertas se cancelan, nada se borra) con ese resultado, y la principal
    se reevalúa. Los recorridos de los demás ítems no se tocan. Debe llamarse con la instancia principal
    bloqueada (las operaciones de la colección ya la bloquean)."""
    from apps.workflows.integracion import cancelar_subinstancia

    activos = recorridos_activos_de(elemento)
    for hija in activos:
        cancelar_subinstancia(hija, actor, resultado=resultado, motivo=motivo)
    if not activos:
        # UX-4: un BORRADOR (sin recorrido) que sale del alcance puede ser lo último que el contenedor esperaba.
        reevaluar_ticket(elemento.parrilla.ticket)


# --- Envío de un borrador (UX-4) ---------------------------------------------------------------------------


def recorrido_al_enviar(ticket, parrilla):
    """La ejecución del RECORRIDO_ITEMS con ingreso AL_ENVIAR que la instancia principal del Ticket está
    esperando para `parrilla`, o `None` (todavía no llegó, ya terminó, o el recorrido no se envía a mano)."""
    if ticket.instancia_workflow_id is None:
        return None
    ejecucion = _ejecucion_vigente_en_espera(ticket.instancia_workflow_id)
    if ejecucion is None or ejecucion.bloque_operativo.tipo != TIPO_RECORRIDO:
        return None
    configuracion = configuracion_de(ejecucion.bloque_operativo)
    if not configuracion.al_enviar or configuracion.coleccion != parrilla.clave:
        return None
    return ejecucion


def faltantes_para_enviar(elemento, ejecucion):
    """Etiquetas de las columnas que el recorrido exige y el ítem aún no tiene (lista vacía = puede enviarse)."""
    from apps.tickets.parrilla import completitud_de

    exigidas = configuracion_de(ejecucion.bloque_operativo).columnas_requeridas_ingreso
    if not exigidas:
        return []
    return list(completitud_de(elemento, exigidas=exigidas).pendientes)


@transaction.atomic
def enviar_elemento(elemento, actor, valores=None):
    """UX-4 — «Enviar»: guarda los valores indicados (como «Guardar borrador»), comprueba lo que el recorrido
    exige y hace entrar AL ítem a su recorrido por `paso_al_enviar`, todo en UNA transacción (si algo falla no
    queda nada guardado ni enviado). Solo con ingreso AL_ENVIAR, con la principal esperando ese recorrido y con
    el ítem en el alcance y sin recorrido todavía. Autoriza quien puede operar la colección (la misma regla que
    editar el ítem: responsable individual o participante; `parrilla._bloquear_elemento`).

    Orden de locks: instancia principal → Ticket → colección → ítem (el del módulo)."""
    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.autorizacion import puede_operar_coleccion
    from apps.tickets.models import ElementoParrilla, Ticket
    from apps.workflows.motor import bloquear_con_padre, iniciar_subinstancia

    ticket = Ticket.objects.get(pk=elemento.parrilla.ticket_id)
    if ticket.instancia_workflow_id is None:
        raise ValidationError("Este ticket no tiene un flujo al que enviar el ítem.")
    padre = bloquear_con_padre(ticket.instancia_workflow_id)
    if valores:
        parrilla_ops.actualizar_valores(elemento, actor, valores)
    elemento = ElementoParrilla.objects.select_related("parrilla").get(pk=elemento.pk)
    parrilla = elemento.parrilla
    ticket.refresh_from_db()
    if not puede_operar_coleccion(actor, parrilla, ticket):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("No puedes enviar ítems de esta colección.")
    if elemento.retirado_en is not None or parrilla_ops.decision_de_salida(elemento) is not None:
        raise ValidationError(f"El ítem #{elemento.numero} ya no está en el alcance.")
    ejecucion = recorrido_al_enviar(ticket, parrilla)
    if ejecucion is None:
        raise ValidationError("Ahora no se pueden enviar ítems de esta colección: el flujo no los está recibiendo.")
    ejecucion = InstanciaEtapa.objects.select_for_update(of=("self",)).select_related("bloque_operativo").get(pk=ejecucion.pk)
    if InstanciaWorkflow.objects.filter(ejecucion_padre=ejecucion, elemento=elemento).exists():
        raise ValidationError(f"El ítem #{elemento.numero} ya se envió.")
    configuracion = configuracion_de(ejecucion.bloque_operativo)
    if configuracion.en_conjunto and not parrilla.alcance_cerrado:
        etapa = configuracion.etapa_borradores or "los borradores"
        raise ValidationError(f"Se envían todos juntos: completa {etapa.lower()} y envíala a aprobación.")
    faltan = faltantes_para_enviar(elemento, ejecucion)
    if faltan:
        raise ValidationError("Para enviarlo falta: " + ", ".join(faltan) + ".")
    return iniciar_subinstancia(padre, ejecucion, _paso_de_entrada(ejecucion), elemento=elemento, actor=actor)


def _paso_de_entrada(ejecucion):
    configuracion = configuracion_de(ejecucion.bloque_operativo)
    secuencia = bloques_de_rama(ejecucion.bloque_operativo, "ITEM")
    if not secuencia:
        raise ValidationError("El recorrido no tiene pasos configurados.")
    if not configuracion.paso_al_enviar:
        return secuencia[0]
    inicial = next((b for b in secuencia if b.clave == configuracion.paso_al_enviar), None)
    if inicial is None:
        raise ValidationError("El paso por el que entran los ítems enviados ya no existe en el recorrido.")
    return inicial


@transaction.atomic
def enviar_conjunto(ticket, parrilla, actor, *, cambios=None, nuevos=None):
    """UX-4 — «Enviar <etapa de borradores> a aprobación»: en UNA transacción guarda lo escrito (`cambios`:
    `{elemento: {campo_id: valor}}`, `nuevos`: `[{campo_id: valor}]`), comprueba que CADA borrador tenga lo que se
    exige, hace entrar a todos a su recorrido y CIERRA el alcance (después no se agregan más; los devueltos se
    corrigen en su recorrido). Si un solo borrador está incompleto no se envía ninguno. Lo hace quien cierra el
    alcance (`puede_editar_parrilla`, el responsable individual en atención). Devuelve los ítems enviados.

    Orden de locks: instancia principal → Ticket → colección → ítems (el del módulo)."""
    from django.core.exceptions import PermissionDenied

    from apps.tickets import parrilla as parrilla_ops
    from apps.tickets.autorizacion import puede_editar_parrilla
    from apps.workflows.motor import bloquear_con_padre, iniciar_subinstancia

    if ticket.instancia_workflow_id is None:
        raise ValidationError("Este ticket no tiene un flujo al que enviar los ítems.")
    padre = bloquear_con_padre(ticket.instancia_workflow_id)
    ticket.refresh_from_db()
    if not puede_editar_parrilla(actor, ticket):
        raise PermissionDenied("Solo quien atiende el ticket envía el conjunto y cierra el alcance.")
    parrilla_ops.guardar_filas(ticket, actor, clave=parrilla.clave, cambios=cambios, nuevos=nuevos)
    parrilla.refresh_from_db()
    ejecucion = recorrido_al_enviar(ticket, parrilla)
    if ejecucion is None:
        raise ValidationError("Ahora no se pueden enviar ítems de esta colección: el flujo no los está recibiendo.")
    ejecucion = InstanciaEtapa.objects.select_for_update(of=("self",)).select_related("bloque_operativo").get(pk=ejecucion.pk)
    borradores = borradores_de(ejecucion, parrilla)
    if not borradores:
        raise ValidationError("Registra al menos un ítem antes de enviar.")
    errores = [
        f"«{parrilla_ops.nombre_de(e)}»: falta {', '.join(faltan)}"
        for e in borradores if (faltan := faltantes_para_enviar(e, ejecucion))
    ]
    if errores:
        raise ValidationError(errores)
    inicial = _paso_de_entrada(ejecucion)
    for elemento in borradores:
        iniciar_subinstancia(padre, ejecucion, inicial, elemento=elemento, actor=actor)
    if not parrilla.alcance_cerrado:
        parrilla_ops.cerrar_alcance(ticket, actor, clave=parrilla.clave)
    return borradores
