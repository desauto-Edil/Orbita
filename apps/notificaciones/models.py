"""Notificaciones y alertas — adelanto del Sprint 7 (CU-033 parcial, CU-034 parcial, CU-035, CU-036, CU-037).

- `Notificacion`: lo que una persona ve en su centro de notificaciones. Nace SIEMPRE de un evento de dominio que
  ya ocurrió (RN-032: no sustituye ni altera la operación que la originó) o de una alerta del evaluador periódico.
  Append-only salvo la marca de lectura.
- `EnvioCorreo`: bandeja de salida del correo de una notificación (outbox). Un envío cuyo resultado es incierto
  no se reintenta a ciegas (criterio de `apps.core.correo`).
- `Alerta`: historia de alertas, vencimientos, recordatorios y escalamientos (RQF-103). Una alerta se emite una
  sola vez por `clave` (objeto + situación + ocurrencia) y se resuelve sola cuando la situación desaparece.
- `PreferenciaNotificacion`: si una persona quiere recibir por correo un grupo de avisos (RQF-107). Lo que exige
  acción no se apaga (lo decide el catálogo de eventos, no esta tabla).
- `ConfiguracionNotificaciones`: valores globales por defecto (una sola fila), editables en Configuración ›
  Notificaciones. Cada Servicio/Proceso los puede cambiar (`ConfiguracionNotificacionesServicio`) y cada Ticket
  congela el resultado al nacer (`Ticket.notificaciones`).
- `ConfiguracionNotificacionesServicio` + `ReglaNotificacion`: Studio › Notificaciones. Pertenecen al
  Servicio/Proceso, NUNCA al Workflow compartido, y referencian fases, actividades y gacetas por su CLAVE estable.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.core.models import Equipo, RegistroBase


class Grupo(models.TextChoices):
    TICKETS = "TICKETS", "Mis tickets"
    ATENCION = "ATENCION", "Tickets que atiendo"
    TRABAJO = "TRABAJO", "Mis actividades y aprobaciones"
    GACETAS = "GACETAS", "Gacetas"
    ALERTAS = "ALERTAS", "Alertas y recordatorios"


class Notificacion(models.Model):
    destinatario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notificaciones"
    )
    evento = models.CharField(max_length=60)
    grupo = models.CharField(max_length=20, choices=Grupo.choices)
    titulo = models.CharField(max_length=200)
    resumen = models.TextField()
    # `[[etiqueta, valor], …]` — los 3 a 5 datos clave que acompañan la frase (también en el correo).
    hechos = models.JSONField(default=list, blank=True)
    # Extracto corto de lo que hay que corregir (observación, motivo). Nunca la conversación completa.
    extracto = models.CharField(max_length=300, blank=True, default="")
    ticket = models.ForeignKey(
        "tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="avisos"
    )
    # Ruta INTERNA (`/tickets/12/`): solo si el destinatario puede abrir el objeto. Al abrir se vuelve a autorizar.
    enlace = models.CharField(max_length=500, blank=True, default="")
    # Actor nulo = Sistema (alertas, procesos programados, cierres automáticos).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    requiere_accion = models.BooleanField(default=False)
    es_alerta = models.BooleanField(default=False)
    # Varios avisos iguales de una misma operación (p. ej. 5 tareas por elemento) se agrupan en uno.
    cantidad = models.PositiveIntegerField(default=1)
    clave_agrupacion = models.CharField(max_length=200, blank=True, default="")
    creada_en = models.DateTimeField(auto_now_add=True, db_index=True)
    leida_en = models.DateTimeField(null=True, blank=True)
    # Conversación del ticket: el mensaje llegó con la persona FUERA DE LÍNEA y su correo espera unos minutos
    # (`correo_conversacion`): si para entonces sigue sin leer, sale UN correo con todo lo pendiente de ese canal.
    correo_pendiente_desde = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-creada_en", "-pk"]
        indexes = [
            models.Index(fields=["destinatario", "leida_en"], name="ix_notif_destinatario_leida"),
            models.Index(fields=["destinatario", "-creada_en"], name="ix_notif_destinatario_fecha"),
        ]

    def __str__(self):
        return f"{self.titulo} → {self.destinatario_id}"

    @property
    def leida(self):
        return self.leida_en is not None


class EnvioCorreo(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        ENVIADO = "ENVIADO", "Enviado"
        FALLIDO = "FALLIDO", "Falló"
        # Se pidió a Google pero no se pudo confirmar: NO se reintenta (evita duplicados).
        INCIERTO = "INCIERTO", "Sin confirmar"
        OMITIDO = "OMITIDO", "Omitido"

    notificacion = models.OneToOneField(Notificacion, on_delete=models.CASCADE, related_name="envio_correo")
    correo = models.EmailField()
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.PENDIENTE)
    intentos = models.PositiveSmallIntegerField(default=0)
    mensaje_id = models.CharField(max_length=120, blank=True, default="")
    error = models.CharField(max_length=300, blank=True, default="")
    creado_en = models.DateTimeField(auto_now_add=True)
    enviado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado_en", "-pk"]
        indexes = [models.Index(fields=["estado", "creado_en"], name="ix_envio_estado_fecha")]

    def __str__(self):
        return f"{self.correo} ({self.estado})"


class Alerta(models.Model):
    class Estado(models.TextChoices):
        ACTIVA = "ACTIVA", "Activa"
        RESUELTA = "RESUELTA", "Resuelta"

    tipo = models.CharField(max_length=60)
    # La SITUACIÓN: tipo + objeto + marca (p. ej. `ALERTA_TICKET_POR_VENCER:ticket:12:2026-11-14T17:00`). Mientras
    # la situación siga existiendo la alerta está ACTIVA; cuando desaparece, el evaluador la resuelve.
    base = models.CharField(max_length=200, db_index=True)
    # Única: la situación + su ocurrencia (`…#2` = segundo recordatorio). Garantiza «una sola vez por umbral».
    clave = models.CharField(max_length=210, unique=True)
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.CASCADE, null=True, blank=True, related_name="alertas")
    # Objeto concreto sin GenericForeignKey: `tarea` / `aprobacion` / `gaceta` / `participacion` / `solicitud` /
    # `entrega` / `programacion` / `ticket` y su pk.
    objeto_tipo = models.CharField(max_length=30)
    objeto_id = models.PositiveBigIntegerField()
    ocurrencia = models.PositiveSmallIntegerField(default=1)
    escalamiento = models.BooleanField(default=False)
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.ACTIVA)
    destinatarios = models.PositiveSmallIntegerField(default=0)
    emitida_en = models.DateTimeField(auto_now_add=True)
    resuelta_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-emitida_en", "-pk"]
        indexes = [
            models.Index(fields=["estado", "objeto_tipo"], name="ix_alerta_estado_objeto"),
            models.Index(fields=["objeto_tipo", "objeto_id"], name="ix_alerta_objeto"),
        ]

    def __str__(self):
        return self.clave


class PreferenciaNotificacion(RegistroBase):
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preferencias_notificacion"
    )
    grupo = models.CharField(max_length=20, choices=Grupo.choices)
    correo = models.BooleanField(default=True)

    class Meta:
        constraints = [UniqueConstraint(fields=["usuario", "grupo"], name="uq_preferencia_usuario_grupo")]

    def __str__(self):
        return f"{self.usuario_id} · {self.grupo}: {'correo' if self.correo else 'solo Órbita'}"


def _dias(**kwargs):
    return models.PositiveSmallIntegerField(validators=[MinValueValidator(0), MaxValueValidator(60)], **kwargs)


class ParametrosRecordatorio(models.Model):
    """Los parámetros de alertas y recordatorios. Abstracto: la fila global los exige (`null=False`) y la del
    Servicio los deja vacíos para heredar el global."""

    por_vencer_porcentaje = models.PositiveSmallIntegerField(
        "Avisar «próximo a vencer» al", validators=[MinValueValidator(10), MaxValueValidator(99)],
        help_text="Porcentaje del tiempo objetivo ya consumido.",
    )
    sin_tomar_horas = models.PositiveSmallIntegerField(
        "Avisar si un ticket sigue sin tomar después de (horas)", validators=[MinValueValidator(1), MaxValueValidator(720)],
    )
    tarea_dias_antes = _dias(verbose_name="Avisar una actividad próxima a vencer (días antes de su fecha límite)")
    tarea_dias_sin_fecha = _dias(verbose_name="Recordar una actividad sin fecha límite (días después de asignada)")
    aprobacion_dias = _dias(verbose_name="Recordar una aprobación pendiente (días después de llegar)")
    informacion_dias = _dias(verbose_name="Recordar información solicitada sin respuesta (días)")
    repetir_cada_dias = models.PositiveSmallIntegerField(
        "Repetir el recordatorio cada (días)", validators=[MinValueValidator(1), MaxValueValidator(30)],
    )
    maximo_recordatorios = models.PositiveSmallIntegerField(
        "Máximo de recordatorios", validators=[MinValueValidator(1), MaxValueValidator(10)],
    )
    escalar = models.BooleanField("Escalar lo vencido")
    escalar_horas = models.PositiveSmallIntegerField(
        "Escalar después de vencido (horas)", validators=[MinValueValidator(0), MaxValueValidator(720)],
    )

    class Meta:
        abstract = True


class ConfiguracionNotificaciones(RegistroBase, ParametrosRecordatorio):
    """Valores globales por defecto (una sola fila, como `ConfiguracionSistema`)."""

    class Meta:
        verbose_name = "configuración de notificaciones"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def actual(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults=dict(DEFAULTS))
        return obj


DEFAULTS = {
    "por_vencer_porcentaje": 80,
    "sin_tomar_horas": 8,
    "tarea_dias_antes": 1,
    "tarea_dias_sin_fecha": 2,
    "aprobacion_dias": 1,
    "informacion_dias": 3,
    "repetir_cada_dias": 1,
    "maximo_recordatorios": 3,
    "escalar": True,
    "escalar_horas": 24,
}
CAMPOS_PARAMETROS = tuple(DEFAULTS)


class ConfiguracionNotificacionesServicio(RegistroBase):
    """Studio › Notificaciones de UN Servicio/Proceso. `None` en un parámetro = usa el valor global."""

    class EscalarA(models.TextChoices):
        SUPERVISION = "SUPERVISION", "Quien supervisa la atención (alcance de área o unidad)"
        USUARIO = "USUARIO", "Una persona"
        EQUIPO = "EQUIPO", "Un equipo"

    servicio = models.OneToOneField(
        "catalogo.Servicio", on_delete=models.CASCADE, related_name="configuracion_notificaciones"
    )
    # `{clave_evento: bool}` — solo los cambios respecto del catálogo; lo que exige acción no se puede apagar.
    correo_eventos = models.JSONField(default=dict, blank=True)
    por_vencer_porcentaje = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(10), MaxValueValidator(99)]
    )
    sin_tomar_horas = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(720)])
    tarea_dias_antes = _dias(null=True, blank=True)
    tarea_dias_sin_fecha = _dias(null=True, blank=True)
    aprobacion_dias = _dias(null=True, blank=True)
    informacion_dias = _dias(null=True, blank=True)
    repetir_cada_dias = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(30)])
    maximo_recordatorios = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(10)])
    escalar = models.BooleanField(null=True, blank=True)
    escalar_horas = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(720)])
    escalar_a = models.CharField(max_length=12, choices=EscalarA.choices, default=EscalarA.SUPERVISION)
    escalar_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    escalar_equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # `{clave_bloque: {"dias_antes": n, "repetir_cada_dias": n, "maximo": n}}` — recordatorio propio de una actividad.
    recordatorios_actividad = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "notificaciones del servicio"

    def __str__(self):
        return f"Notificaciones de {self.servicio_id}"

    def clean(self):
        if self.escalar_a == self.EscalarA.USUARIO and self.escalar_usuario_id is None:
            raise ValidationError("Elige a quién se escala.")
        if self.escalar_a == self.EscalarA.EQUIPO and self.escalar_equipo_id is None:
            raise ValidationError("Elige el equipo al que se escala.")


class ReglaNotificacion(RegistroBase):
    """«Cuando pase esto en esta etapa, avisar a estas personas». Retirar = desactivar (nunca se borra)."""

    class Momento(models.TextChoices):
        FASE_ALCANZADA = "FASE_ALCANZADA", "El ticket llega a una fase"
        ACTIVIDAD_INICIADA = "ACTIVIDAD_INICIADA", "Una actividad empieza"
        ACTIVIDAD_COMPLETADA = "ACTIVIDAD_COMPLETADA", "Una actividad se completa"
        APROBACION_DEVUELTA = "APROBACION_DEVUELTA", "Una aprobación devuelve o rechaza"
        GACETA_ABIERTA = "GACETA_ABIERTA", "Una gaceta se abre"
        GACETA_RESUELTA = "GACETA_RESUELTA", "Una gaceta se resuelve"

    class Destinatario(models.TextChoices):
        SOLICITANTE = "SOLICITANTE", "Quien solicitó"
        RESPONSABLE_TICKET = "RESPONSABLE_TICKET", "El responsable del ticket"
        EQUIPO_RESPONSABLE = "EQUIPO_RESPONSABLE", "El equipo responsable del ticket"
        RESPONSABLE_ACTIVIDAD = "RESPONSABLE_ACTIVIDAD", "El responsable de la actividad"
        COORDINADOR_GACETA = "COORDINADOR_GACETA", "Quien coordina la gaceta"
        USUARIO = "USUARIO", "Una persona"
        EQUIPO = "EQUIPO", "Un equipo"
        CAMPO_FORMULARIO = "CAMPO_FORMULARIO", "La persona de un campo del formulario"
        COLUMNA_ITEM = "COLUMNA_ITEM", "La persona o equipo de una columna del ítem"

    servicio = models.ForeignKey("catalogo.Servicio", on_delete=models.CASCADE, related_name="reglas_notificacion")
    momento = models.CharField(max_length=24, choices=Momento.choices)
    # Clave ESTABLE de la fase (`FaseWorkflow.clave`), de la actividad (`BloqueOperativo.clave`) o de la gaceta
    # (`DefinicionGaceta.clave`), según el momento. Renombrar o versionar no la cambia.
    clave_objetivo = models.CharField(max_length=60)
    nombre_objetivo = models.CharField(max_length=150, blank=True, default="")
    destinatario = models.CharField(max_length=24, choices=Destinatario.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # `Campo.clave` (USUARIO/EQUIPO) del formulario de inicio o de la colección del ítem.
    campo_clave = models.CharField(max_length=60, blank=True, default="")
    correo = models.BooleanField(default=True)
    mensaje = models.CharField(max_length=280, blank=True, default="")
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["servicio_id", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(destinatario="USUARIO") | Q(usuario__isnull=False),
                name="ck_regla_notif_usuario",
            ),
            models.CheckConstraint(
                condition=~Q(destinatario="EQUIPO") | Q(equipo__isnull=False),
                name="ck_regla_notif_equipo",
            ),
            models.CheckConstraint(
                condition=~Q(destinatario__in=["CAMPO_FORMULARIO", "COLUMNA_ITEM"]) | ~Q(campo_clave=""),
                name="ck_regla_notif_campo",
            ),
        ]

    def __str__(self):
        return f"{self.get_momento_display()} «{self.nombre_objetivo or self.clave_objetivo}» → {self.get_destinatario_display()}"
