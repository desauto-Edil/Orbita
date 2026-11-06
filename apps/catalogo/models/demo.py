from django.conf import settings
from django.db import models

from apps.core.models import RegistroBase


class RecursoDemo(RegistroBase):
    """Bloque 5.6 — identidad de los recursos que crea el comando `preparar_demo_orbita`.

    Una fila por CASO de la demostración (parrilla, webinar, pieza gráfica, presentaciones). Es la ÚNICA forma
    de reconocer un recurso de la demo: nunca por nombre ni por eventos de auditoría. Con ella el comando es
    idempotente (un caso ya registrado no se vuelve a crear) y solo verifica, publica, genera ejemplos o retira
    lo que él mismo creó; los Servicios, programaciones y tickets ajenos no se tocan.

    No es configuración del dominio: no la lee ningún motor ni pantalla. Retirar un caso no la borra (marca
    `retirado_en`), como el resto de Órbita, que retira desactivando y conserva el historial.
    """

    class Caso(models.TextChoices):
        PARRILLA = "PARRILLA", "Parrilla mensual de publicaciones"
        WEBINAR = "WEBINAR", "Webinar"
        PIEZA_GRAFICA = "PIEZA_GRAFICA", "Pieza gráfica"
        PRESENTACIONES = "PRESENTACIONES", "Creación de presentaciones comerciales"

    caso = models.CharField(max_length=30, choices=Caso.choices, unique=True)
    version_contrato = models.PositiveIntegerField()
    servicio = models.OneToOneField("catalogo.Servicio", on_delete=models.PROTECT, related_name="recurso_demo")
    workflow = models.ForeignKey(
        "workflows.Workflow", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text="Plantilla de fases propia del caso (los Servicios sin flujo no tienen).",
    )
    categoria = models.ForeignKey("catalogo.Categoria", on_delete=models.PROTECT, related_name="+")
    categoria_creada = models.BooleanField(
        default=False, help_text="La categoría la creó la demo (solo entonces la demo puede retirarla).",
    )
    preparado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    parametros = models.JSONField(
        default=dict, blank=True,
        help_text="Personas, equipos y calendario elegidos al preparar (ids), que reutilizan publicar y ejemplos.",
    )
    retirado_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return f"Demo {self.get_caso_display()}"
