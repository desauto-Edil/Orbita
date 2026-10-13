from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.catalogo import claves
from apps.core.models import Equipo, RegistroBase


class DefinicionParrilla(RegistroBase):
    """Sprint 4.G2B1 — estructura repetible OPCIONAL de un Proceso: las COLUMNAS de los elementos
    (filas) que cada ejecución del Proceso puede tener (p. ej. una parrilla de contenidos).

    Pertenece al Proceso (`Servicio` de `tipo=PROCESO`), no a su programación ni a su flujo: sirve
    igual a un Proceso manual que a uno programado, y un Proceso sin esta fila funciona exactamente
    como antes. Un Servicio no puede tenerla (`clean()`, que `save()` ejecuta siempre).

    Las columnas NO son un motor nuevo: son los `Campo` de un `Formulario` (versionado, con claves
    estables y las mismas Strategies de tipo). Ese `Formulario` es exclusivo de esta definición y
    NUNCA es `Servicio.formulario` (la entrada inicial del Ticket): son dos usos independientes de la
    misma plantilla genérica, y ninguno puede ocupar el lugar del otro.

    Cada Ticket congela la `FormularioVersion` ACTIVA al nacer (`tickets.ParrillaTicket`): cambiar
    columnas aquí solo afecta a los Tickets que nazcan después. `activa=False` deja de ofrecerla a
    Tickets nuevos sin tocar los ya creados."""

    servicio = models.OneToOneField(
        "catalogo.Servicio", on_delete=models.CASCADE, related_name="definicion_parrilla"
    )
    formulario = models.OneToOneField(
        "catalogo.Formulario", on_delete=models.PROTECT, related_name="definicion_parrilla"
    )
    nombre_elemento = models.CharField(
        max_length=60, default="Elemento",
        help_text="Cómo se llama cada fila (p. ej. «Publicación»).",
    )
    activa = models.BooleanField(default=True)

    def clean(self):
        if self.servicio_id is not None:
            servicio = self.servicio
            if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
                raise ValidationError("Solo un Proceso puede definir una estructura de elementos.")
        if self.formulario_id is not None:
            from apps.catalogo.models.catalogo import Servicio

            if Servicio.objects.filter(formulario_id=self.formulario_id).exists():
                raise ValidationError(
                    "Ese formulario es la entrada inicial de un servicio o proceso: la estructura de "
                    "elementos necesita su propio formulario de columnas."
                )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Elementos de {self.servicio}"


class ConfiguracionTareasElemento(RegistroBase):
    """Sprint 4.G2D1 — cómo un Proceso convierte cada ELEMENTO de su estructura (`DefinicionParrilla`)
    en una `apps.tareas.Tarea`. Es configuración OPCIONAL y mutable; no es un sistema de tareas nuevo ni
    agrega campos al elemento: cada dato de la Tarea sale de una COLUMNA, identificada por la `clave`
    ESTABLE del `Campo` (única dentro de una versión y copiada al versionar), nunca por pk ni etiqueta.

    - `clave_titulo` (TEXTO), `clave_descripcion` (TEXTO | TEXTO_LARGO, opcional), `clave_responsable`
      (USUARIO) y `clave_fecha_limite` (FECHA | FECHA_HORA): la compatibilidad la valida
      `apps.catalogo.tareas_elemento`.
    - `requiere_evidencia`: solo se guarda (aquí y en el vínculo de cada Tarea); exigir la evidencia al
      completar es de una fase posterior.
    - `activa=False` deja de ofrecer la generación a los Tickets que nazcan después; no toca ningún Ticket
      ya creado ni las Tareas ya generadas.

    Cada Ticket congela su propia copia al nacer (`tickets.ConfiguracionTareasTicket`): lo que se cambie
    aquí después no altera a los Tickets existentes."""

    definicion = models.OneToOneField(
        DefinicionParrilla, on_delete=models.CASCADE, related_name="configuracion_tareas"
    )
    activa = models.BooleanField(default=True)
    clave_titulo = models.SlugField(max_length=claves.LARGO_MAXIMO)
    clave_descripcion = models.SlugField(max_length=claves.LARGO_MAXIMO, blank=True)
    clave_responsable = models.SlugField(max_length=claves.LARGO_MAXIMO)
    clave_fecha_limite = models.SlugField(max_length=claves.LARGO_MAXIMO)
    requiere_evidencia = models.BooleanField(default=False)

    def __str__(self):
        return f"Tareas por elemento de {self.definicion}"


class ColumnaRevisadaDefinicion(RegistroBase):
    """Sprint 4.G2C1 — una columna de la estructura de elementos que participa en la revisión de un
    `DefinicionEntregable` de tipo ELEMENTOS.

    Se identifica por la `clave` ESTABLE del `Campo` (única dentro de una versión de columnas y copiada
    al versionar), nunca por su pk ni su etiqueta: así sobrevive a las nuevas versiones de la estructura.
    Cada Ticket resuelve estas claves contra la versión que congeló y guarda su propia copia
    (`tickets.ColumnaRevisadaEntregable`): esta fila es configuración mutable y ninguna revisión
    histórica depende de ella."""

    definicion = models.ForeignKey(
        "catalogo.DefinicionEntregable", on_delete=models.CASCADE, related_name="columnas_revisadas"
    )
    clave = models.SlugField(max_length=claves.LARGO_MAXIMO)

    class Meta:
        ordering = ["pk"]
        constraints = [UniqueConstraint(fields=["definicion", "clave"], name="uq_columnarevisada_definicion_clave")]

    def __str__(self):
        return f"{self.definicion} · {self.clave}"


class RevisorDefinicion(RegistroBase):
    """Sprint 4.G2C1 — quién puede revisar los elementos de un entregable ELEMENTOS: una persona o un
    equipo reales (`apps.core`), nunca el responsable del Ticket por serlo. Es configuración mutable:
    cada Ticket congela su propia copia (`tickets.RevisorEntregable`) al nacer."""

    definicion = models.ForeignKey(
        "catalogo.DefinicionEntregable", on_delete=models.CASCADE, related_name="revisores"
    )
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(usuario__isnull=False, equipo__isnull=True) | Q(usuario__isnull=True, equipo__isnull=False)),
                name="ck_revisordefinicion_usuario_o_equipo",
            ),
            UniqueConstraint(
                fields=["definicion", "usuario"], condition=Q(usuario__isnull=False),
                name="uq_revisordefinicion_usuario",
            ),
            UniqueConstraint(
                fields=["definicion", "equipo"], condition=Q(equipo__isnull=False), name="uq_revisordefinicion_equipo"
            ),
        ]

    def __str__(self):
        return f"{self.definicion} · {self.usuario or self.equipo}"
