"""Definiciones de GACETAS y REGLAS DE FINALIZACIÓN de un Proceso — Fase 3 del modelo configurable.

Una gaceta es una herramienta TRANSVERSAL del Proceso (no una fase): revisión colaborativa de ítems de una o
varias colecciones, con observaciones, correcciones y decisiones. Un Proceso puede tener ninguna, una o varias
(`DefinicionGaceta`, N por Proceso, `clave` estable). Las reglas viven en `configuracion` (contrato JSON cuya
única autoridad es `apps.catalogo.gacetas`); cada cambio incrementa `version` y se audita. Cada Ticket COPIA la
definición vigente al nacer (`apps.gacetas.GacetaTicket.configuracion`): cambiar el Studio nunca altera un
ticket existente. Retirar una definición es desactivarla (`activa=False`).

`ReglaFinalizacion` (1:1 con el Proceso, opcional) dice qué debe cumplirse para resolver el Ticket; también se
congela en el Ticket al nacer (`Ticket.reglas_finalizacion`). La única autoridad del contrato y de su evaluación
es `apps.tickets.finalizacion`.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import UniqueConstraint

from apps.catalogo import claves
from apps.core.models import RegistroBase


class DefinicionGaceta(RegistroBase):
    servicio = models.ForeignKey("catalogo.Servicio", on_delete=models.CASCADE, related_name="definiciones_gaceta")
    clave = models.SlugField(max_length=claves.LARGO_MAXIMO, validators=[claves.validar_clave])
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    orden = models.PositiveIntegerField(default=0)
    # Número de versión de la definición: sube con cada cambio guardado (la copia del Ticket lo conserva).
    version = models.PositiveIntegerField(default=1)
    configuracion = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["servicio", "clave"], name="uq_definiciongaceta_servicio_clave"),
        ]
        ordering = ["servicio_id", "orden", "pk"]

    def clean(self):
        if self.servicio_id is not None:
            servicio = self.servicio
            if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
                raise ValidationError("Las gacetas solo existen en un proceso.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre


class ReglaFinalizacion(RegistroBase):
    servicio = models.OneToOneField("catalogo.Servicio", on_delete=models.CASCADE, related_name="regla_finalizacion")
    configuracion = models.JSONField(default=dict, blank=True)

    def clean(self):
        if self.servicio_id is not None and self.servicio.tipo != "PROCESO":
            raise ValidationError("Las reglas de finalización solo existen en un proceso.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Reglas de finalización de {self.servicio}"
