from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.models import RegistroBase


class ProgramacionProceso(RegistroBase):
    """Sprint 4.G1 — CUÁNDO Órbita genera automáticamente una ejecución de un Proceso.

    Pertenece al Proceso (`Servicio` de `tipo=PROCESO`), nunca al Workflow: dos Procesos que
    comparten el mismo flujo pueden tener programaciones distintas. Es configuración operativa
    y mutable (no versionada): cada ejecución generada guarda su propia foto
    (`tickets.EjecucionProgramada.programacion_foto`), así que cambiar o pausar la programación
    solo afecta a lo que se genere desde ahora.

    La fila existe = el Proceso se configuró como «Programado»; `activa` la pausa. «Manual» es
    no tener una programación activa. Un Proceso con programación activa NO se ofrece en el
    catálogo (`visibilidad.servicios_visibles_para`): se inicia por programación, no por el
    portal.

    `responsable_inicial` REUTILIZA `ServicioResponsable` (la lista de quienes pueden atender el
    Proceso): no se duplica Usuario/Equipo aquí, y como quien atiende ya es responsable
    configurado, `puede_tomar` funciona sin reglas nuevas.

    `activada_desde` (fecha LOCAL) evita generar periodos históricos: solo cuentan las fechas de
    creación posteriores o iguales a ella. `ultimo_intento_en`/`ultimo_error` son la
    información mínima de operación (no hay una consola de errores todavía).
    """

    class Frecuencia(models.TextChoices):
        MENSUAL = "MENSUAL", "Mensual"

    class Periodo(models.TextChoices):
        MES_ACTUAL = "MES_ACTUAL", "Mes actual"
        MES_SIGUIENTE = "MES_SIGUIENTE", "Mes siguiente"

    servicio = models.OneToOneField(
        "catalogo.Servicio", on_delete=models.CASCADE, related_name="programacion"
    )
    activa = models.BooleanField(default=True)
    frecuencia = models.CharField(max_length=20, choices=Frecuencia.choices, default=Frecuencia.MENSUAL)
    dia_creacion = models.PositiveSmallIntegerField()
    periodo = models.CharField(max_length=20, choices=Periodo.choices, default=Periodo.MES_SIGUIENTE)
    activada_desde = models.DateField(null=True, blank=True)
    responsable_inicial = models.ForeignKey(
        "catalogo.ServicioResponsable", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    ultimo_intento_en = models.DateTimeField(null=True, blank=True, editable=False)
    ultimo_error = models.TextField(blank=True, default="", editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(dia_creacion__gte=1, dia_creacion__lte=28),
                name="ck_programacion_dia_1_a_28",
                violation_error_message="El día de creación debe estar entre 1 y 28.",
            ),
            models.CheckConstraint(
                condition=Q(frecuencia="MENSUAL"),
                name="ck_programacion_frecuencia_v1",
                violation_error_message="Por ahora solo existe la frecuencia mensual.",
            ),
            models.CheckConstraint(
                condition=Q(periodo__in=["MES_ACTUAL", "MES_SIGUIENTE"]),
                name="ck_programacion_periodo_valido",
            ),
            models.CheckConstraint(
                condition=Q(activa=False) | Q(activada_desde__isnull=False, responsable_inicial__isnull=False),
                name="ck_programacion_activa_completa",
                violation_error_message="Una programación activa necesita responsable inicial y fecha de activación.",
            ),
        ]

    def clean(self):
        if self.servicio_id is None:
            return
        servicio = self.servicio
        if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
            raise ValidationError("Solo un Proceso puede programarse.")
        if self.responsable_inicial_id is not None and self.responsable_inicial.servicio_id != self.servicio_id:
            raise ValidationError("El responsable inicial debe ser un responsable de este proceso.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Programación de {self.servicio} (día {self.dia_creacion}, {self.get_periodo_display().lower()})"
