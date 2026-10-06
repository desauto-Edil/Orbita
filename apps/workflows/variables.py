"""Resolución de variables de una DECISION/CONDICION (Sprint 4.B0).

    BLOQUE produce información → CONTEXTO la conserva → DECISIÓN la evalúa →
    la TRANSICIÓN decide el camino.

Este módulo es la capa de LECTURA entre el contexto/Ticket y la decisión: dada la
referencia que escribió quien configuró la ruta, devuelve su valor o
`INEXISTENTE` (ver `apps.workflows.contexto`). No evalúa operadores ni elige
rutas, y no escribe nada.

Referencias admitidas (`variable` de una transición):

    ticket.estado | ticket.tipo | ticket.origen | ticket.es_general
    ticket.solicitante | ticket.responsable | ticket.equipo_responsable
    ticket.radicado_en | ticket.fecha_objetivo | ticket.fecha_objetivo_original
    formulario.<clave del campo>
    aprobaciones.<clave del bloque>.resultado
    <nombre plano>                     (contrato anterior: `variables` del contexto)

- `ticket.*` se lee en vivo del Ticket vinculado (solo datos estables, sin volcar el
  modelo: el solicitante/responsable se publican como nombre de usuario).
- `formulario.<clave>` se resuelve contra el formulario CONGELADO del Ticket
  (`TicketServicio.formulario_version`) por la clave estable del `Campo`, no por
  etiqueta ni pk: sobrevive a renombrar la etiqueta y a nuevas versiones. El valor
  conserva su tipo lógico (bool, Decimal, date, datetime, texto, lista, id de
  usuario/área/unidad). Un campo existente sin respuesta vale `None`; una clave que
  el formulario del Ticket no tiene es INEXISTENTE — así un Workflow compartido por
  dos Servicios con formularios distintos sigue funcionando en el que no tiene el
  campo, y la DECISION puede caer en su fallback sin error.
- `aprobaciones.<clave>.resultado` lo publica el motor al cerrar la aprobación
  (`APROBADA`/`RECHAZADA`/`DEVUELTA`, los mismos valores de `resultado_aprobacion`).
  Solo existen para bloques de una configuración por fases (`BloqueOperativo`); el
  modo LEGACY_EJECUTABLE (`Etapa`) no tiene claves de bloque, así que esas
  referencias son INEXISTENTES allí. `ticket.*`, `formulario.*` y las variables
  planas funcionan igual en ambos modos.

Un ámbito nuevo (4.B1: `entregables.<clave>.satisfecho`) se agrega sumándolo a
`contexto.AMBITOS_BLOQUE` (resultados publicados por bloques) o registrando una
función en `_AMBITOS`; nada más cambia.

Los valores resueltos (Decimal, fechas, objetos) viven solo en memoria durante la
evaluación: nunca se escriben en el JSON del contexto.
"""

from django.core.exceptions import ObjectDoesNotExist

from apps.workflows.contexto import AMBITOS_BLOQUE, INEXISTENTE, leer_resultado_bloque, resolver_variable


def _nombre_usuario(usuario):
    return usuario.get_username() if usuario is not None else None


# Datos del Ticket que un Workflow puede consultar (lista cerrada).
_CAMPOS_TICKET = {
    "estado": lambda t: t.estado,
    "tipo": lambda t: t.tipo,
    "origen": lambda t: t.origen,
    "es_general": lambda t: t.detalle_servicio.servicio.es_ticket_general,
    "solicitante": lambda t: _nombre_usuario(t.solicitante),
    "responsable": lambda t: _nombre_usuario(t.usuario_responsable),
    "equipo_responsable": lambda t: t.equipo_responsable.nombre if t.equipo_responsable_id else None,
    "radicado_en": lambda t: t.radicado_en,
    "fecha_objetivo": lambda t: t.fecha_objetivo_vigente,
    "fecha_objetivo_original": lambda t: t.fecha_objetivo_original,
}


class ResolutorVariables:
    """Resuelve referencias para UNA evaluación de decisión. Carga perezosamente
    (y guarda) el Ticket, el formulario congelado y sus respuestas: como mucho tres
    consultas por decisión, sin importar cuántas condiciones tenga."""

    def __init__(self, instancia, contexto=None):
        """`contexto`: el diccionario con el que el motor está evaluando (puede ser
        más reciente que el de `instancia` cargado aparte); por defecto, el de `instancia`."""
        self.instancia = instancia
        if contexto is None:
            contexto = instancia.contexto if instancia is not None else {}
        self.contexto = contexto
        self._ticket = None
        self._ticket_cargado = False
        self._formulario = None

    # --- Ticket -------------------------------------------------------------

    @property
    def ticket(self):
        if not self._ticket_cargado:
            self._ticket = self._buscar_ticket()
            self._ticket_cargado = True
        return self._ticket

    def _buscar_ticket(self):
        if self.instancia is None or self.instancia.pk is None:
            return None
        from apps.tickets.models import Ticket

        consulta = Ticket.objects.select_related(
            "solicitante", "usuario_responsable", "equipo_responsable", "detalle_servicio__servicio"
        )
        ticket = consulta.filter(instancia_workflow=self.instancia).first()
        if ticket is not None:
            return ticket
        # Durante `radicar_ticket` el motor corre ANTES de que el Ticket guarde la
        # relación: solo en ese arranque se usa el `ticket_id` que la radicación
        # pasó (mismas condiciones que `apps.workflows.actores._ticket_vigente`).
        ticket_id = (self.contexto.get("datos_iniciales") or {}).get("ticket_id")
        if not isinstance(ticket_id, int) or isinstance(ticket_id, bool):
            return None
        return consulta.filter(
            pk=ticket_id, estado=Ticket.Estado.BORRADOR, instancia_workflow__isnull=True,
            detalle_servicio__servicio__workflow_id=self.instancia.workflow_version.workflow_id,
        ).first()

    def _del_ticket(self, segmentos):
        if len(segmentos) != 1 or self.ticket is None:
            return INEXISTENTE
        lector = _CAMPOS_TICKET.get(segmentos[0])
        if lector is None:
            return INEXISTENTE
        try:
            return lector(self.ticket)
        except ObjectDoesNotExist:
            return INEXISTENTE

    # --- Formulario congelado del Ticket ---------------------------------------

    @property
    def formulario(self):
        """`{clave: valor}` de los campos del formulario congelado del Ticket
        (valor `None` si no hay respuesta). Vacío si no hay Ticket/formulario."""
        if self._formulario is None:
            self._formulario = self._cargar_formulario()
        return self._formulario

    def _cargar_formulario(self):
        ticket = self.ticket
        if ticket is None:
            return {}
        from apps.catalogo.models import Campo
        from apps.tickets.models import RespuestaCampo

        try:
            version = ticket.detalle_servicio.formulario_version
        except ObjectDoesNotExist:
            return {}
        campos = list(Campo.objects.filter(version=version).exclude(clave=""))
        respuestas = {
            r.campo_id: r
            for r in RespuestaCampo.objects.filter(
                respuesta_formulario__ticket=ticket, campo__in=[c.pk for c in campos]
            ).select_related("campo")
        }
        valores = {}
        for campo in campos:
            respuesta = respuestas.get(campo.pk)
            valores[campo.clave] = respuesta.valor if respuesta is not None else None
        return valores

    def _del_formulario(self, segmentos):
        if len(segmentos) != 1:
            return INEXISTENTE
        return self.formulario.get(segmentos[0], INEXISTENTE)

    # --- API --------------------------------------------------------------------

    def resolver(self, nombre):
        """Valor de `nombre` o `INEXISTENTE`. Lo estructurado (`ticket.`, `formulario.`,
        `<ámbito>.<clave>.<campo>`) se resuelve primero; si no existe, se prueba como
        variable plana (compatibilidad con el contrato anterior a 4.B0)."""
        nombre = (nombre or "").strip()
        partes = nombre.split(".")
        if len(partes) > 1:
            ambito, resto = partes[0], partes[1:]
            valor = INEXISTENTE
            if ambito == "ticket":
                valor = self._del_ticket(resto)
            elif ambito == "formulario":
                valor = self._del_formulario(resto)
            elif ambito in AMBITOS_BLOQUE and len(resto) == 2:
                valor = leer_resultado_bloque(self.contexto, ambito, resto[0], resto[1])
            if valor is not INEXISTENTE:
                return valor
        return resolver_variable(self.contexto, nombre)


def obtener_variable_ejecucion(instancia, nombre, contexto=None):
    """Valor de la referencia `nombre` en la ejecución `instancia`
    (`InstanciaWorkflow`), o `INEXISTENTE`. Para evaluar varias condiciones de una
    misma decisión use un único `ResolutorVariables`."""
    return ResolutorVariables(instancia, contexto).resolver(nombre)


def referencias_disponibles(*, campos=(), bloques=()):
    """Referencias que una DECISION puede usar, para mostrarlas al configurar (Studio).
    `campos`: iterable de `Campo` (con clave); `bloques`: iterable de `BloqueOperativo`.
    Devuelve una lista de `(referencia, descripcion)`."""
    lista = [
        ("ticket.estado", "Estado del ticket (BORRADOR, RADICADO, EN_ATENCION, …)"),
        ("ticket.tipo", "Tipo del ticket (SERVICIO o PROCESO)"),
        ("ticket.es_general", "Verdadero si es un ticket general"),
        ("ticket.solicitante", "Nombre de usuario de quien solicita"),
        ("ticket.responsable", "Nombre de usuario del responsable (vacío si aún no hay)"),
        ("ticket.fecha_objetivo", "Fecha objetivo vigente de atención"),
    ]
    for campo in campos:
        if campo.clave:
            lista.append((f"formulario.{campo.clave}", f"Respuesta del campo «{campo.etiqueta}»"))
    for bloque in bloques:
        if bloque.tipo == "APROBACION" and bloque.clave:
            lista.append(
                (f"aprobaciones.{bloque.clave}.resultado", f"Resultado de «{bloque.nombre}» (APROBADA, RECHAZADA o DEVUELTA)")
            )
    return lista
