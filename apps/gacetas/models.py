"""Gacetas en EJECUCIÓN — Fase 3 del modelo configurable de procesos (Sprint 6, «Gacetas y versionamiento»).

Una gaceta es la revisión COLABORATIVA de ítems de un Ticket de Proceso. Su definición vive en
`apps.catalogo.DefinicionGaceta`; cada Ticket la congela al nacer en `GacetaTicket.configuracion`. La ejecución:

- `GacetaTicket`: la gaceta de ESTE ticket, con su ciclo PENDIENTE → ABIERTA → PERIODO_CERRADO → RESUELTA
  (o CANCELADA con el ticket). El CIERRE DEL PERIODO termina la ventana de observaciones ordinarias; la
  RESOLUCIÓN fija el resultado definitivo de lo pendiente. Son momentos distintos.
- `ParticipacionItemGaceta`: un ítem (`tickets.ElementoParrilla`, su identidad, colección, versiones y evidencias
  son los del ítem) dentro de la gaceta, con su ESTADO DE GACETA (EN_REVISION, EN_CORRECCION, APROBADA,
  RECHAZADA, EXCLUIDA). Es distinto del ESTADO OPERATIVO del ítem (su recorrido): un ítem puede haber terminado
  su recorrido y seguir en revisión. Única por (gaceta, ítem): el ingreso es idempotente.
- `RondaRevisionGaceta`: cada vez que el ítem se presenta (o se re-presenta tras corregir) se abre una ronda con
  la VERSIÓN presentada (`tickets.VersionElemento`) y un esquema de `apps.aprobaciones` (no hay otro motor de
  aprobaciones). La ronda guarda su resultado y la tarea de corrección (`apps.tareas`) si la hubo.
- `ObservacionGaceta`: observaciones de revisores o de quien devolvió, con la respuesta del responsable. Nunca se
  borran; al re-presentar quedan «atendidas».

Las operaciones viven en `apps.gacetas.operaciones`; la autorización en `apps.gacetas.autorizacion`.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.core.models import RegistroBase


class GacetaTicket(RegistroBase):
    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Sin abrir"
        ABIERTA = "ABIERTA", "Abierta"
        PERIODO_CERRADO = "PERIODO_CERRADO", "Periodo de revisión cerrado"
        RESUELTA = "RESUELTA", "Resuelta"
        CANCELADA = "CANCELADA", "Cancelada"

    ESTADOS_ACTIVOS = ("ABIERTA", "PERIODO_CERRADO")
    ESTADOS_FINALES = ("RESUELTA", "CANCELADA")

    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.CASCADE, related_name="gacetas")
    definicion = models.ForeignKey("catalogo.DefinicionGaceta", on_delete=models.PROTECT, related_name="gacetas_ticket")
    clave = models.CharField(max_length=60)
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    version_definicion = models.PositiveIntegerField(default=1)
    configuracion = models.JSONField(default=dict, blank=True)
    estado = models.CharField(max_length=16, choices=Estado.choices, default=Estado.PENDIENTE)
    abierta_en = models.DateTimeField(null=True, blank=True)
    vence_revision_en = models.DateTimeField(null=True, blank=True)
    periodo_cerrado_en = models.DateTimeField(null=True, blank=True)
    resuelta_en = models.DateTimeField(null=True, blank=True)
    resuelta_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolucion = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [UniqueConstraint(fields=["ticket", "clave"], name="uq_gacetaticket_ticket_clave")]
        ordering = ["ticket_id", "pk"]

    @property
    def periodo_abierto(self):
        return self.estado == self.Estado.ABIERTA

    def __str__(self):
        return f"{self.nombre} ({self.ticket})"


class ParticipacionItemGaceta(RegistroBase):
    class Estado(models.TextChoices):
        EN_REVISION = "EN_REVISION", "En revisión"
        EN_CORRECCION = "EN_CORRECCION", "En corrección"
        APROBADA = "APROBADA", "Aprobada"
        RECHAZADA = "RECHAZADA", "Rechazada"
        EXCLUIDA = "EXCLUIDA", "Excluida"

    class Origen(models.TextChoices):
        MANUAL = "MANUAL", "Presentado por una persona"
        AUTOMATICO = "AUTOMATICO", "Ingreso automático"
        FLUJO = "FLUJO", "Presentado por el flujo"

    ESTADOS_PENDIENTES = ("EN_REVISION", "EN_CORRECCION")
    ESTADOS_FINALES = ("APROBADA", "RECHAZADA", "EXCLUIDA")

    gaceta = models.ForeignKey(GacetaTicket, on_delete=models.CASCADE, related_name="participaciones")
    elemento = models.ForeignKey(
        "tickets.ElementoParrilla", on_delete=models.PROTECT, related_name="participaciones_gaceta"
    )
    estado = models.CharField(max_length=14, choices=Estado.choices, default=Estado.EN_REVISION)
    ronda = models.PositiveIntegerField(default=1)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.MANUAL)
    presentada_en = models.DateTimeField(auto_now_add=True)
    presentada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    vence_correccion_en = models.DateTimeField(null=True, blank=True)
    correccion_vencida_en = models.DateTimeField(null=True, blank=True)
    decidida_en = models.DateTimeField(null=True, blank=True)
    decidida_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    aceptada_automaticamente = models.BooleanField(default=False)
    justificacion = models.TextField(blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["gaceta", "elemento"], name="uq_participaciongaceta_gaceta_elemento"),
        ]
        ordering = ["gaceta_id", "pk"]

    @property
    def es_final(self):
        return self.estado in self.ESTADOS_FINALES

    def __str__(self):
        return f"{self.elemento} en {self.gaceta}"


class RondaRevisionGaceta(models.Model):
    class Resultado(models.TextChoices):
        APROBADA = "APROBADA", "Aprobada"
        DEVUELTA = "DEVUELTA", "Devuelta"
        RECHAZADA = "RECHAZADA", "Rechazada"
        EXCLUIDA = "EXCLUIDA", "Excluida"
        OBSERVADA = "OBSERVADA", "Observada"
        ACEPTADA_AUTOMATICA = "ACEPTADA_AUTO", "Aceptada automáticamente"
        RESUELTA = "RESUELTA", "Definida al resolver la gaceta"
        CANCELADA = "CANCELADA", "Cancelada"

    participacion = models.ForeignKey(ParticipacionItemGaceta, on_delete=models.CASCADE, related_name="rondas")
    numero = models.PositiveIntegerField()
    version = models.ForeignKey(
        "tickets.VersionElemento", on_delete=models.PROTECT, null=True, blank=True, related_name="rondas_gaceta"
    )
    esquema = models.OneToOneField(
        "aprobaciones.EsquemaAprobacion", on_delete=models.PROTECT, null=True, blank=True, related_name="ronda_gaceta"
    )
    tarea_correccion = models.OneToOneField(
        "tareas.Tarea", on_delete=models.PROTECT, null=True, blank=True, related_name="ronda_gaceta"
    )
    resultado = models.CharField(max_length=14, choices=Resultado.choices, blank=True)
    abierta_en = models.DateTimeField(auto_now_add=True)
    cerrada_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["participacion", "numero"], name="uq_rondagaceta_participacion_numero"),
        ]
        ordering = ["participacion_id", "numero"]

    def __str__(self):
        return f"Ronda {self.numero} de {self.participacion}"


class ObservacionGaceta(models.Model):
    class Origen(models.TextChoices):
        REVISION = "REVISION", "Observación de revisión"
        DECISION = "DECISION", "Al devolver"

    participacion = models.ForeignKey(ParticipacionItemGaceta, on_delete=models.CASCADE, related_name="observaciones")
    ronda = models.ForeignKey(RondaRevisionGaceta, on_delete=models.PROTECT, related_name="observaciones")
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.REVISION)
    texto = models.TextField()
    creada_en = models.DateTimeField(auto_now_add=True)
    respuesta = models.TextField(blank=True)
    respondida_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    respondida_en = models.DateTimeField(null=True, blank=True)
    atendida_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(texto=""), name="ck_observaciongaceta_con_texto")]
        ordering = ["participacion_id", "creada_en", "pk"]

    def __str__(self):
        return f"Observación de {self.autor} en {self.participacion}"
