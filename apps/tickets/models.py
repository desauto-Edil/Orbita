"""Ticket — incrementos 2.1 (modelo base y borradores) y 2.2 (radicación y
respuestas). CU-014, CU-015; RQF-049/050/051/053/054; RN-014/015/016.

Desde 2.2, `estado` también produce RADICADO (vía
`apps.tickets.operaciones.radicar_ticket`) — EN_ATENCION/RESUELTO/CERRADO/
CANCELADO siguen sin producirse (2.3+).

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
from django.db.models import UniqueConstraint

from apps.catalogo.models import Campo, FormularioVersion, Servicio
from apps.core.models import Area, RegistroBase, UnidadNegocio


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
    usuario) — conceptualmente distinta de `Adjunto` de Ticket/Comentario
    (comunicación/soporte operativo, 2.4): esto es la propia respuesta del
    formulario, no un archivo de apoyo a una conversación.

    Las restricciones de formato/tamaño son las ya definidas en
    `Campo.configuracion` (RQF-007: "restricciones... configuradas") — se
    validan reutilizando `EstrategiaArchivo.validar_valor()`
    (`apps/catalogo/campos.py`, sin modificar), nunca un límite nuevo
    inventado aquí.
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
    cada `ArchivoRespuestaCampo` hijo (Django borra en cascada a nivel de
    Collector/SQL) — `pre_delete` sí se dispara siempre, en cascada o en un
    delete directo, y es el único punto confiable para liberar el archivo
    del storage antes de perder la fila que lo referencia."""
    if instance.archivo:
        instance.archivo.delete(save=False)
