"""Ticket — incrementos 2.1 (modelo base y borradores), 2.2 (radicación y
respuestas), 2.3 (atención y asignación), 2.4 (comunicación, adjuntos
operativos y solicitud de información), 2.5 (resolución, cierre,
cancelación y reapertura) y 2.C (cierre técnico Sprint 2). CU-014/015/016/
017/018/019; RQF-049/050/051/053/054/056/057/058/059/060/061/118/119;
RN-014/015/016/018.

Desde 2.2, `estado` también produce RADICADO. Desde 2.3, `apps.tickets.
operaciones.tomar_ticket`/`asignar_ticket` producen EN_ATENCION. 2.4
(comunicación/solicitud de información) NO agrega transiciones: ver
docstring de `ComentarioTicket`/`SolicitudInformacion` más abajo. 2.5
agrega las transiciones finales (RN-018) — `resolver_ticket`/
`cerrar_ticket`/`cancelar_ticket`/`reabrir_ticket` en `operaciones.py`,
tabla en `apps.tickets.estados` — produciendo RESUELTO/CERRADO/CANCELADO
y el regreso RESUELTO→EN_ATENCION. Ver docstring de `ResolucionTicket`
(corregida en 2.C: `ForeignKey`, no `OneToOneField`, para admitir varias
resoluciones a lo largo de sucesivos ciclos RESOLVER→REABRIR→RESOLVER).

**Inmutabilidad posterior a la radicación (2.2)**: `Ticket.exigir_editable()`
bloquea las vías ordinarias de escritura de `RespuestaCampo`/
`ArchivoRespuestaCampo` una vez el ticket deja de estar en BORRADOR — mismo
alcance documentado que el resto del proyecto (protege `save()`, no
`QuerySet.update()`/bulk). `TicketServicio`/`RespuestaFormulario` además
impiden que su `formulario_version` congelada cambie una vez creado el
registro, sin importar el estado del ticket.

**`RespuestaFormulario` cuelga de `Ticket`, no de `TicketServicio`** —
mismo criterio de transversalidad ya aplicado a `Formulario` en 1.2: si en
el futuro existe `TicketProceso`, sus respuestas de formulario no deberían
exigir remodelar esta relación.

**Persistencia de `RespuestaCampo` (corrección del usuario sobre la
propuesta original)**: nueve columnas, cada una con integridad referencial
real — `valor_usuario`/`valor_area`/`valor_unidad` son FKs reales a
`apps.core`, nunca un id crudo en `valor_numero`. `FECHA` y `FECHA_HORA`
tienen columnas propias (`DateField`/`DateTimeField` son tipos Django
distintos). El campo tipo ARCHIVO no usa ninguna columna escalar: su valor
vive en `ArchivoRespuestaCampo` (ver más abajo), una entidad separada de
los adjuntos de comunicación (`Adjunto` de Ticket/Comentario, 2.4) —
`ArchivoRespuestaCampo` es una respuesta estructurada de formulario, no
comunicación operativa.

El mapping `TipoCampo -> columna de RespuestaCampo` (`COLUMNA_POR_TIPO`)
pertenece exclusivamente a `apps.tickets` — `apps/catalogo/campos.py`
(Strategy, 1.2) no se modifica para conocer cómo Tickets almacena sus
respuestas; sigue ocupándose únicamente de normalizar/validar valores.

**Integridad garantizada por las vías ordinarias de dominio** (no por
`QuerySet.update()`/bulk, mismo alcance documentado en 1.2): el `save()` de
`RespuestaCampo` exige que `campo.version_id == respuesta_formulario.
formulario_version_id` (un Campo de v4 nunca se guarda en una respuesta
basada en v3) y que, para tipos no-ARCHIVO, quede poblada únicamente la
columna que corresponde a `campo.tipo` — cualquier otra columna con valor
hace fallar el `save()`.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.catalogo.models import Campo, FormularioVersion, Servicio
from apps.core.models import Area, Equipo, RegistroBase, UnidadNegocio


class Ticket(RegistroBase):
    class Tipo(models.TextChoices):
        SERVICIO = "SERVICIO", "Servicio"
        PROCESO = "PROCESO", "Proceso"

    class Estado(models.TextChoices):
        BORRADOR = "BORRADOR", "Borrador"
        RADICADO = "RADICADO", "Radicado"
        EN_ATENCION = "EN_ATENCION", "En atención"
        RESUELTO = "RESUELTO", "Resuelto"
        CERRADO = "CERRADO", "Cerrado"
        CANCELADO = "CANCELADO", "Cancelado"

    class Origen(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        SISTEMA = "SISTEMA", "Sistema"

    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.SERVICIO)
    solicitante = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets_solicitados"
    )
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.BORRADOR)
    origen = models.CharField(max_length=20, choices=Origen.choices, default=Origen.MANUAL)
    # RN-014 (unicidad) — UUID4 sin prefijo/año/formato empresarial (el
    # Excel solo exige unicidad, ver propuesta 2.2 aprobada). NULL mientras
    # es BORRADOR; Postgres no colisiona múltiples NULL en una constraint
    # unique, así que no hace falta un índice parcial.
    radicado = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    # RN-016 — siempre `timezone.now()` en `operaciones.radicar_ticket`,
    # nunca un valor recibido del cliente.
    radicado_en = models.DateTimeField(null=True, blank=True, editable=False)
    # 2.3 (CU-017, RQF-056) — independientes y ambos nullable a propósito:
    # un ticket puede caer en la cola de un `equipo_responsable` antes de
    # que exista un `usuario_responsable` concreto (TOMAR/ASIGNAR lo llena
    # después); una vez poblado, `equipo_responsable` se conserva como
    # contexto y no se limpia automáticamente. No son alternativos entre sí
    # como en `ServicioResponsable` — aquí ambos pueden coexistir.
    usuario_responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tickets_responsable",
    )
    equipo_responsable = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="tickets_responsable"
    )

    def exigir_eliminable(self):
        if self.estado != Ticket.Estado.BORRADOR:
            raise ValidationError(
                "Solo un ticket en estado BORRADOR puede eliminarse físicamente por las vías "
                "ordinarias de dominio."
            )

    def exigir_editable(self):
        """2.2 — una vez el ticket deja de estar en BORRADOR (radicado o
        más allá), sus respuestas/archivos quedan inmutables por las vías
        ordinarias. Consultado por `RespuestaCampo`/`ArchivoRespuestaCampo`
        además del chequeo temprano de `operaciones.guardar_respuestas_borrador`."""
        if self.estado != Ticket.Estado.BORRADOR:
            raise ValidationError(
                "Solo se puede modificar el contenido de un ticket mientras está en BORRADOR."
            )

    def delete(self, *args, **kwargs):
        self.exigir_eliminable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Ticket #{self.pk} ({self.get_estado_display()})"


class HistorialTicket(models.Model):
    """Bitácora operacional de atención — incremento 2.3 (CU-016/CU-017,
    RQF-056/RQF-060). No hereda `RegistroBase` por la misma razón que
    `RegistroAuditoria` (0.4): un evento histórico nunca se actualiza, así
    que un `actualizado_en` insinuaría lo contrario. No reconstruye estado
    (no Event Sourcing) — cada fila es un hecho ya ocurrido, de solo
    lectura hacia atrás; el único punto que escribe aquí es
    `apps.tickets.historial.registrar`.

    `TOMAR` genera una única entrada `TOMADO`, aunque internamente dispare
    RADICADO→EN_ATENCION — no se duplica con un evento `CAMBIO_ESTADO`
    aparte (decisión explícita del usuario). `CAMBIO_ESTADO` genérico
    **nunca** se declara como choice, ni en 2.5: cada transición final
    tiene su propio tipo semántico (`RESUELTO`/`CERRADO`/`CANCELADO`/
    `REABIERTO`), igual que TOMADO/ASIGNADO/REASIGNADO — no se anticipa
    un evento genérico por simetría.

    **2.4** agrega `INFORMACION_SOLICITADA`/`INFORMACION_RESPONDIDA`
    (transición PENDIENTE→RESPONDIDA de `SolicitudInformacion`: evento
    operacional real, mismo tipo que TOMADO/ASIGNADO/REASIGNADO — no
    contenido, sino un cambio de estado con consecuencia). Deliberadamente
    **no** se agregan `COMENTARIO_AGREGADO`/`ADJUNTO_AGREGADO` (decisión
    explícita, propuesta 2.4 aprobada): un `ComentarioTicket`/`Adjunto` ya
    contiene autor+fecha+contenido — una fila espejo aquí duplicaría esa
    misma información sin aportar nada que la sección "Comunicación" del
    detalle no muestre ya.

    **2.5** agrega `RESUELTO`/`CERRADO`/`CANCELADO`/`REABIERTO` — cada una
    con `datos` conteniendo como mínimo `estado_anterior`/`estado_nuevo`
    y, cuando aplica, `motivo` (CANCELAR/REABRIR, V1) o `resolucion_id`
    (RESUELTO — apunta a `ResolucionTicket`, sin duplicar su `descripcion`
    aquí: el contenido del resultado vive en esa entidad, no en el log).
    Desde 2.5, además, cada una de estas 4 transiciones también genera un
    `RegistroAuditoria` (ACTUALIZAR sobre `Ticket`, vía
    `apps.tickets.operaciones._auditar_cambio_estado` — RQF-118: "conservar
    cambios de estado de elementos operativos") — `HistorialTicket` y
    `RegistroAuditoria` **no son sustitutos** (corrección explícita del
    usuario sobre la propuesta 2.5): el primero es la trazabilidad
    operacional visible del Ticket, el segundo la auditoría transversal.
    Las asignaciones de 2.3 (TOMAR/ASIGNAR/REASIGNAR, RQF-119: "conservar
    cambios de responsable y asignación") quedaron pendientes de
    `RegistroAuditoria` en 2.5 (deuda documentada) y se cerraron en **2.C**
    — ver `apps.tickets.operaciones._auditar_cambio_responsable`, usado
    desde esas 3 operaciones.
    """

    class TipoEvento(models.TextChoices):
        RADICADO = "RADICADO", "Radicado"
        TOMADO = "TOMADO", "Tomado"
        ASIGNADO = "ASIGNADO", "Asignado"
        REASIGNADO = "REASIGNADO", "Reasignado"
        INFORMACION_SOLICITADA = "INFORMACION_SOLICITADA", "Información solicitada"
        INFORMACION_RESPONDIDA = "INFORMACION_RESPONDIDA", "Información respondida"
        RESUELTO = "RESUELTO", "Resuelto"
        CERRADO = "CERRADO", "Cerrado"
        CANCELADO = "CANCELADO", "Cancelado"
        REABIERTO = "REABIERTO", "Reabierto"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="historial")
    tipo_evento = models.CharField(max_length=30, choices=TipoEvento.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    datos = models.JSONField(null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"{self.ticket} — {self.get_tipo_evento_display()}"


class TicketServicio(RegistroBase):
    """Especialización de `Ticket` cuando `tipo == SERVICIO` — la única que
    existe en 2.1. `TicketProceso` no se crea todavía (RN-022 la exige solo
    cuando exista Procesos); `Ticket.tipo` ya admite `PROCESO` en el enum
    para no bloquear esa extensión futura, pero ningún flujo de 2.x lo
    produce.
    """

    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="detalle_servicio")
    # PROTECT: borrar un Servicio nunca debe destruir en cascada los
    # tickets que lo referencian (mismo criterio que 1.2 con FormularioVersion).
    servicio = models.ForeignKey(Servicio, on_delete=models.PROTECT, related_name="tickets")
    # Congelada en `operaciones.crear_borrador` — ningún código de
    # apps.tickets vuelve a leer `Formulario.version_activa` después.
    formulario_version = models.ForeignKey(
        FormularioVersion, on_delete=models.PROTECT, related_name="tickets_servicio"
    )

    def _exigir_formulario_version_inmutable(self):
        if self.pk is None:
            return
        anterior_id = TicketServicio.objects.filter(pk=self.pk).values_list(
            "formulario_version_id", flat=True
        ).first()
        if anterior_id is not None and anterior_id != self.formulario_version_id:
            raise ValidationError(
                "La FormularioVersion congelada de un TicketServicio no puede modificarse "
                "después de creado (2.2 — invariante TicketServicio.formulario_version == "
                "RespuestaFormulario.formulario_version)."
            )

    def save(self, *args, **kwargs):
        self._exigir_formulario_version_inmutable()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.servicio} — {self.ticket}"


class TicketContextoAtencion(RegistroBase):
    """Snapshot, al radicar, de los `ServicioContextoAtencion` activos del
    Servicio — 1 fila por contexto copiado. RQF-062/RN-019: la
    configuración posterior del Servicio no debe alterar cómo se
    interpreta un ticket ya radicado, mismo criterio que `FormularioVersion`
    en 2.2 — por eso esto es una copia, nunca una consulta en vivo a
    `ServicioContextoAtencion` (`apps.tickets.operaciones.radicar_ticket`
    copia TODOS los contextos activos, sin elegir uno solo cuando hay
    varios: un servicio transversal conserva los que tenía).

    Sin `activo`: es un hecho histórico, no una configuración editable —
    ningún flujo de dominio vuelve a escribir estas filas después de
    creadas. `PROTECT` hacia Area/UnidadNegocio (a diferencia del `CASCADE`
    de `ServicioContextoAtencion`, que sí es configuración viva): borrar un
    Área/Unidad nunca debe destruir en cascada la interpretación histórica
    de un ticket ya radicado.
    """

    class TipoAlcance(models.TextChoices):
        AREA = "AREA", "Área"
        UNIDAD = "UNIDAD", "Unidad de negocio"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="contextos_atencion")
    tipo_alcance = models.CharField(max_length=10, choices=TipoAlcance.choices)
    area = models.ForeignKey(Area, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(tipo_alcance="AREA", area__isnull=False, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="UNIDAD", unidad_negocio__isnull=False, area__isnull=True)
                ),
                name="ck_ticketcontextoatencion_alcance_coherente",
            ),
            UniqueConstraint(
                fields=["ticket", "area"], condition=Q(tipo_alcance="AREA"), name="uq_ticketcontexto_area"
            ),
            UniqueConstraint(
                fields=["ticket", "unidad_negocio"],
                condition=Q(tipo_alcance="UNIDAD"),
                name="uq_ticketcontexto_unidad",
            ),
        ]

    def __str__(self):
        return f"{self.ticket} — {self.tipo_alcance}"


class RespuestaFormulario(RegistroBase):
    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="respuesta_formulario")
    formulario_version = models.ForeignKey(
        FormularioVersion, on_delete=models.PROTECT, related_name="respuestas_formulario"
    )

    def _exigir_formulario_version_inmutable(self):
        if self.pk is None:
            return
        anterior_id = RespuestaFormulario.objects.filter(pk=self.pk).values_list(
            "formulario_version_id", flat=True
        ).first()
        if anterior_id is not None and anterior_id != self.formulario_version_id:
            raise ValidationError(
                "La FormularioVersion congelada de una RespuestaFormulario no puede modificarse "
                "después de creada (2.2 — misma invariante que TicketServicio)."
            )

    def save(self, *args, **kwargs):
        self._exigir_formulario_version_inmutable()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Respuestas de {self.ticket}"


# Única fuente de verdad de dónde vive el valor de cada TipoCampo — de uso
# exclusivo de apps.tickets (ver docstring del módulo).
COLUMNA_POR_TIPO = {
    Campo.TipoCampo.TEXTO: "valor_texto",
    Campo.TipoCampo.TEXTO_LARGO: "valor_texto",
    Campo.TipoCampo.CORREO: "valor_texto",
    Campo.TipoCampo.URL: "valor_texto",
    Campo.TipoCampo.LISTA: "valor_texto",
    Campo.TipoCampo.NUMERO: "valor_numero",
    Campo.TipoCampo.FECHA: "valor_fecha",
    Campo.TipoCampo.FECHA_HORA: "valor_fecha_hora",
    Campo.TipoCampo.BOOLEANO: "valor_booleano",
    Campo.TipoCampo.MULTILISTA: "valor_json",
    Campo.TipoCampo.USUARIO: "valor_usuario",
    Campo.TipoCampo.AREA: "valor_area",
    Campo.TipoCampo.UNIDAD: "valor_unidad",
    Campo.TipoCampo.ARCHIVO: None,  # vive en ArchivoRespuestaCampo, no aquí
}

_COLUMNAS_ESCALARES = ("valor_texto", "valor_numero", "valor_fecha", "valor_fecha_hora", "valor_booleano", "valor_json")
# Pública (sin guion bajo): `operaciones.py` también la necesita para saber
# cuándo un valor normalizado se asigna por `_id` en vez de por valor directo.
COLUMNAS_REFERENCIA = ("valor_usuario", "valor_area", "valor_unidad")
_COLUMNAS_VALOR = _COLUMNAS_ESCALARES + COLUMNAS_REFERENCIA


class RespuestaCampo(RegistroBase):
    respuesta_formulario = models.ForeignKey(
        RespuestaFormulario, on_delete=models.CASCADE, related_name="respuestas_campo"
    )
    campo = models.ForeignKey(Campo, on_delete=models.PROTECT, related_name="respuestas")

    valor_texto = models.TextField(blank=True, default="")
    valor_numero = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    valor_fecha = models.DateField(null=True, blank=True)
    valor_fecha_hora = models.DateTimeField(null=True, blank=True)
    valor_booleano = models.BooleanField(null=True, blank=True)
    valor_json = models.JSONField(null=True, blank=True)
    valor_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    valor_area = models.ForeignKey(Area, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    valor_unidad = models.ForeignKey(
        UnidadNegocio, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            UniqueConstraint(fields=["respuesta_formulario", "campo"], name="uq_respuestacampo_campo"),
        ]

    @property
    def valor(self):
        """Valor Python normalizado de esta respuesta, sin importar en qué
        columna física vive — usado por `operaciones.py` para reconstruir
        el estado actual antes de reevaluar reglas condicionales, y por las
        plantillas para mostrar el valor guardado."""
        columna = COLUMNA_POR_TIPO.get(self.campo.tipo)
        if columna is None:
            return None
        if columna in COLUMNAS_REFERENCIA:
            return getattr(self, f"{columna}_id")
        return getattr(self, columna)

    def _columna_poblada(self, nombre):
        if nombre in COLUMNAS_REFERENCIA:
            return getattr(self, f"{nombre}_id") is not None
        valor = getattr(self, nombre)
        return valor is not None and valor != ""

    def _exigir_columna_unica(self):
        columna_esperada = COLUMNA_POR_TIPO.get(self.campo.tipo)
        for nombre in _COLUMNAS_VALOR:
            if self._columna_poblada(nombre) and nombre != columna_esperada:
                raise ValidationError(
                    f"La columna '{nombre}' no corresponde al tipo de campo {self.campo.tipo} "
                    f"(columna esperada: {columna_esperada!r})."
                )

    def exigir_integridad(self):
        if self.campo.version_id != self.respuesta_formulario.formulario_version_id:
            raise ValidationError(
                "El campo debe pertenecer a la misma FormularioVersion congelada en la "
                "respuesta (RespuestaCampo.campo.version debe coincidir con "
                "RespuestaFormulario.formulario_version)."
            )
        self._exigir_columna_unica()

    def save(self, *args, **kwargs):
        self.exigir_integridad()
        self.respuesta_formulario.ticket.exigir_editable()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.respuesta_formulario} — {self.campo}"


class ArchivoRespuestaCampo(RegistroBase):
    """Respuesta estructurada de un Campo tipo ARCHIVO (corrección del
    usuario) — conceptualmente distinta de `Adjunto` de Ticket/Comentario/
    SolicitudInformacion/RespuestaSolicitudInformacion (comunicación/
    soporte operativo, 2.4): esto es la propia respuesta del formulario,
    no un archivo de apoyo a una conversación.

    Las restricciones de formato/tamaño son las ya definidas en
    `Campo.configuracion` (RQF-007: "restricciones... configuradas") — se
    validan reutilizando `EstrategiaArchivo.validar_valor()`
    (`apps/catalogo/campos.py`, sin modificar), nunca un límite nuevo
    inventado aquí. Desde 2.4, `apps.tickets.operaciones._validar_archivo_tecnico`
    agrega además el único control técnico mínimo compartido con `Adjunto`
    (archivo no vacío, con nombre) — ver esa función para la justificación
    de por qué no hay un límite de tamaño/extensión propio aquí.
    """

    respuesta_campo = models.OneToOneField(RespuestaCampo, on_delete=models.CASCADE, related_name="archivo")
    archivo = models.FileField(upload_to="tickets/respuestas/%Y/%m/")
    nombre_original = models.CharField(max_length=255)
    tipo_mime = models.CharField(max_length=255, blank=True)
    tamano_bytes = models.PositiveIntegerField()
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    def exigir_integridad(self):
        if self.respuesta_campo.campo.tipo != Campo.TipoCampo.ARCHIVO:
            raise ValidationError(
                "Solo un RespuestaCampo de campo tipo ARCHIVO puede tener un "
                "ArchivoRespuestaCampo asociado."
            )

    def save(self, *args, **kwargs):
        self.exigir_integridad()
        self.respuesta_campo.respuesta_formulario.ticket.exigir_editable()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre_original


def eliminar_archivo_fisico(sender, instance, **kwargs):
    """Receptor de `pre_delete` (conectado en `apps.py`). Necesario porque
    un `ticket.delete()` en cascada no invoca el `.delete()` de Python de
    cada `ArchivoRespuestaCampo`/`Adjunto` hijo (Django borra en cascada a
    nivel de Collector/SQL) — `pre_delete` sí se dispara siempre, en
    cascada o en un delete directo, y es el único punto confiable para
    liberar el archivo del storage antes de perder la fila que lo
    referencia. Genérico a propósito (no lee ningún campo propio de
    `ArchivoRespuestaCampo`): `apps.py` conecta esta misma función también
    para `Adjunto` (2.4), sin duplicar el receptor."""
    if instance.archivo:
        instance.archivo.delete(save=False)


class ComentarioTicket(models.Model):
    """Comunicación general de un Ticket — 2.4 (CU-018/RQF-057: "comentarios
    y respuestas identificando autor y fecha"). Cronológica y plana
    (**sin threading**: un comentario nunca responde a otro — decisión
    explícita del usuario, RQF-057 no implica árbol de respuestas). No
    hereda `RegistroBase` por la misma razón que `HistorialTicket`/
    `RegistroAuditoria`: es append-only (L de la propuesta 2.4 aprobada,
    sin edición/eliminación ordinaria) — un `actualizado_en` insinuaría lo
    contrario. El Excel tampoco distingue comentarios internos/públicos
    (verificado explícitamente contra RQF-057 y las Reglas de negocio: no
    existe esa distinción) — no se inventa aquí."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comentarios")
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    contenido = models.TextField()
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"{self.ticket} — comentario de {self.autor}"


class SolicitudInformacion(models.Model):
    """Solicitud de información adicional — 2.4 (CU-018/RQF-058: "solicitar
    y responder información adicional dentro del ticket"). Entidad propia,
    no `ComentarioTicket(tipo="SOLICITUD")` (propuesta 2.4 aprobada,
    alternativa B: tiene ciclo de vida real — PENDIENTE/RESPONDIDA — que un
    comentario no tiene).

    `destinatario` es SIEMPRE `ticket.solicitante` en 2.4 (R.1 aprobado):
    fijado exclusivamente por `apps.tickets.operaciones.solicitar_informacion`,
    nunca recibido de un formulario/POST. Se conserva como FK explícita
    (no inferida en cada consulta) para que quede registrado con quién
    quedó esta solicitud concretamente, aunque hoy coincida siempre con el
    solicitante del Ticket.

    `estado` es un valor **denormalizado**: la garantía real de "como
    máximo una respuesta" la da el `OneToOneField` de
    `RespuestaSolicitudInformacion.solicitud` a nivel de base de datos
    (imposible crear una segunda fila con la misma solicitud); `estado` se
    mantiene sincronizado exclusivamente por
    `apps.tickets.operaciones.responder_solicitud`, bajo el mismo lock que
    crea esa respuesta — no existe una vía ordinaria que los desincronice.

    No hereda `RegistroBase`: `mensaje`/`solicitada_por`/`solicitada_en`
    son inmutables desde su creación (mismo criterio que `ComentarioTicket`).
    No modifica `Ticket.estado` ni `apps.tickets.estados` — es una máquina
    de estados completamente separada (instrucción explícita del usuario:
    no se inventa un `PENDIENTE_INFORMACION` en `Ticket.Estado`).
    """

    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        RESPONDIDA = "RESPONDIDA", "Respondida"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="solicitudes_informacion")
    solicitada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    destinatario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    mensaje = models.TextField()
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.PENDIENTE)
    solicitada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["solicitada_en"]

    def __str__(self):
        return f"{self.ticket} — solicitud de {self.solicitada_por}"


class RespuestaSolicitudInformacion(models.Model):
    """Respuesta única y final a una `SolicitudInformacion` — 2.4 (R.3
    aprobado: entidad propia, NO campos embebidos en `SolicitudInformacion`).
    `OneToOneField` hacia `solicitud`: la propia existencia de esta fila
    ES la marca de "ya respondida" a nivel de base de datos — Postgres
    rechaza una segunda fila para la misma solicitud sin necesidad de
    lógica adicional, que es la protección real contra doble respuesta
    (`apps.tickets.operaciones.responder_solicitud` además usa
    `select_for_update()` para que la segunda petición concurrente falle
    con un `ValidationError` explícito en vez de un `IntegrityError` crudo).
    Inmutable desde su creación (no hereda `RegistroBase`, mismo criterio
    que `ComentarioTicket`)."""

    solicitud = models.OneToOneField(
        SolicitudInformacion, on_delete=models.CASCADE, related_name="respuesta"
    )
    respondida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    contenido = models.TextField()
    respondida_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Respuesta a {self.solicitud}"


class ResolucionTicket(models.Model):
    """Resultado del trabajo sobre un Ticket — 2.5 (CU-019/RQF-059),
    corregido en 2.C. Entidad propia (no un campo en `Ticket` ni solo
    `HistorialTicket.datos`, decisión explícita del usuario): a diferencia
    del motivo de cerrar/cancelar/reabrir, esto es contenido del dominio
    con evidencia propia (adjuntos vía `Adjunto.TipoRelacion.RESOLUCION`),
    del mismo tipo que `RespuestaSolicitudInformacion` en 2.4.

    `ForeignKey` hacia `ticket` (2.C — **corrección** de un `OneToOneField`
    original de 2.5): `RESUELTO → REABRIR → EN_ATENCION` es una transición
    aprobada explícitamente, y un ticket reabierto puede volver a
    resolverse — el `OneToOneField` original lo bloqueaba
    contradictoriamente (un segundo `RESOLVER` legítimo tras un `REABRIR`
    rompía contra la unicidad). Ahora **cada ejecución válida de
    `RESOLVER` crea una nueva fila**; ninguna se actualiza ni se elimina
    — la resolución anterior permanece intacta como historia (`ticket.
    resoluciones.order_by("resuelto_en")` reconstruye el ciclo completo:
    Resolución #1 → REABIERTO → Resolución #2 → ...). El bloqueo real
    contra una segunda resolución *dentro del mismo ciclo de atención* NO
    lo da esta entidad — lo da `apps.tickets.estados` (`RESOLVER` solo
    existe desde `EN_ATENCION`; tras la primera resolución el ticket queda
    `RESUELTO`, así que un segundo intento sin `REABRIR` de por medio falla
    ahí, antes de llegar a crear una fila aquí).

    `descripcion` es obligatoria (V1 aprobado). No hereda `RegistroBase`:
    inmutable desde su creación, mismo criterio que `ComentarioTicket`/
    `SolicitudInformacion`/`RespuestaSolicitudInformacion` — ninguna vía
    ordinaria de dominio permite reescribir una resolución ya registrada.
    """

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="resoluciones")
    resuelto_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    descripcion = models.TextField()
    resuelto_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["resuelto_en"]

    def __str__(self):
        return f"Resolución de {self.ticket}"


class Adjunto(RegistroBase):
    """Adjunto operativo — 2.4 (CU-018/RQF-052: "adjuntar archivos durante
    la creación y atención del ticket"). Conceptualmente distinto de
    `ArchivoRespuestaCampo` (respuesta estructurada de un Campo de
    formulario, ver su docstring) — este es soporte de comunicación/
    evidencia operativa.

    Puede colgar de 5 padres conocidos de antemano (R.4/2.4 aprobado, rama
    `RESOLUCION` agregada en 2.5): directamente del `Ticket` (evidencia
    suelta, sin exigir un comentario), de un `ComentarioTicket`, de una
    `SolicitudInformacion`, de su `RespuestaSolicitudInformacion`, o de una
    `ResolucionTicket` (evidencia de resolución). `tipo_relacion` +
    `CheckConstraint` de coherencia (exactamente una FK poblada,
    coincidente con el discriminador) — mismo patrón ya usado 3 veces en
    el proyecto (`ServicioVisibilidad`, `ServicioContextoAtencion`/
    `TicketContextoAtencion`), sin las `UniqueConstraint` parciales de esos
    casos porque aquí la cardinalidad es "varios adjuntos por padre", no
    "uno activo por rama". **Sin `GenericForeignKey`**: son 5 padres fijos
    y conocidos, con integridad referencial real — mismo criterio que
    Sprint 0 fijó para `AsignacionRol` (no se usa por la misma razón que
    ahí).

    Hereda `RegistroBase` (a diferencia de `ComentarioTicket`/
    `SolicitudInformacion`/`RespuestaSolicitudInformacion`/
    `ResolucionTicket`): sigue el mismo criterio que `ArchivoRespuestaCampo`,
    su análogo estructural más cercano — en la práctica nunca se reescribe
    tras crearse (L de la propuesta 2.4: "no reemplazar"), pero no se le
    quita el mixin estándar de auditoría de campo que ya tienen todos los
    adjuntos/archivos del proyecto.
    """

    class TipoRelacion(models.TextChoices):
        TICKET = "TICKET", "Ticket"
        COMENTARIO = "COMENTARIO", "Comentario"
        SOLICITUD = "SOLICITUD", "Solicitud de información"
        RESPUESTA_SOLICITUD = "RESPUESTA_SOLICITUD", "Respuesta a solicitud de información"
        RESOLUCION = "RESOLUCION", "Resolución"

    tipo_relacion = models.CharField(max_length=20, choices=TipoRelacion.choices)
    ticket = models.ForeignKey(
        Ticket, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    comentario = models.ForeignKey(
        ComentarioTicket, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    solicitud = models.ForeignKey(
        SolicitudInformacion, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    respuesta_solicitud = models.ForeignKey(
        RespuestaSolicitudInformacion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="adjuntos",
    )
    resolucion = models.ForeignKey(
        ResolucionTicket, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    archivo = models.FileField(upload_to="tickets/adjuntos/%Y/%m/")
    nombre_original = models.CharField(max_length=255)
    tipo_mime = models.CharField(max_length=255, blank=True)
    tamano_bytes = models.PositiveIntegerField()
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(
                        tipo_relacion="TICKET",
                        ticket__isnull=False,
                        comentario__isnull=True,
                        solicitud__isnull=True,
                        respuesta_solicitud__isnull=True,
                        resolucion__isnull=True,
                    )
                    | Q(
                        tipo_relacion="COMENTARIO",
                        ticket__isnull=True,
                        comentario__isnull=False,
                        solicitud__isnull=True,
                        respuesta_solicitud__isnull=True,
                        resolucion__isnull=True,
                    )
                    | Q(
                        tipo_relacion="SOLICITUD",
                        ticket__isnull=True,
                        comentario__isnull=True,
                        solicitud__isnull=False,
                        respuesta_solicitud__isnull=True,
                        resolucion__isnull=True,
                    )
                    | Q(
                        tipo_relacion="RESPUESTA_SOLICITUD",
                        ticket__isnull=True,
                        comentario__isnull=True,
                        solicitud__isnull=True,
                        respuesta_solicitud__isnull=False,
                        resolucion__isnull=True,
                    )
                    | Q(
                        tipo_relacion="RESOLUCION",
                        ticket__isnull=True,
                        comentario__isnull=True,
                        solicitud__isnull=True,
                        respuesta_solicitud__isnull=True,
                        resolucion__isnull=False,
                    )
                ),
                name="ck_adjunto_relacion_coherente",
            ),
        ]

    @property
    def ticket_relacionado(self):
        """Resuelve el Ticket dueño de este adjunto sin importar la rama —
        único punto que necesita saber esto (autorización de descarga,
        `views.descargar_adjunto_view`, reutiliza `puede_consultar_ticket`)."""
        if self.tipo_relacion == self.TipoRelacion.TICKET:
            return self.ticket
        if self.tipo_relacion == self.TipoRelacion.COMENTARIO:
            return self.comentario.ticket
        if self.tipo_relacion == self.TipoRelacion.SOLICITUD:
            return self.solicitud.ticket
        if self.tipo_relacion == self.TipoRelacion.RESPUESTA_SOLICITUD:
            return self.respuesta_solicitud.solicitud.ticket
        return self.resolucion.ticket

    def __str__(self):
        return self.nombre_original
