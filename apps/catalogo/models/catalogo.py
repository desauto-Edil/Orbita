from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.catalogo.models.formularios import Formulario
from apps.core.models import Area, Equipo, RegistroBase, UnidadNegocio


class Categoria(RegistroBase):
    """Organiza el catálogo de servicios. RQF-029 (CU-010)."""

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class Servicio(RegistroBase):
    """RQF-030, RQF-031 (CU-010).

    `alcance_visibilidad` (RN-038) gobierna quién puede *consultar* este
    servicio (ver `ServicioVisibilidad` y `apps/catalogo/visibilidad.py`) —
    no confundir con `catalogo.administrar` (`apps/catalogo/admin.py`), que
    gobierna quién puede *gestionarlo*. Son dos autorizaciones distintas y
    deliberadamente separadas.
    """

    class AlcanceVisibilidad(models.TextChoices):
        PUBLICO_INTERNO = "PUBLICO_INTERNO", "Público interno"
        RESTRINGIDO = "RESTRINGIDO", "Restringido"

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, related_name="servicios")
    instrucciones = models.TextField(blank=True)
    activo = models.BooleanField(default=True)
    alcance_visibilidad = models.CharField(
        max_length=20,
        choices=AlcanceVisibilidad.choices,
        default=AlcanceVisibilidad.RESTRINGIDO,
    )
    # RQF-034 (CU-010): asociación opcional a la plantilla transversal de
    # formulario (1.2). Apunta a `Formulario` (la identidad), nunca a una
    # `FormularioVersion` concreta — la versión vigente siempre se resuelve
    # vía `formulario.version_activa`. `null=True` porque el flujo real es
    # "crea Servicio → crea Formulario → asocia → activa": la asociación es
    # posterior a la creación del Servicio, no obligatoria desde el inicio.
    formulario = models.ForeignKey(
        Formulario, on_delete=models.PROTECT, null=True, blank=True, related_name="servicios"
    )

    def __str__(self):
        return self.nombre


class ServicioVisibilidad(RegistroBase):
    """Concesión de visibilidad para servicios RESTRINGIDO. RQF-032, RN-038.

    Sin efecto sobre servicios PUBLICO_INTERNO (ya visibles para cualquier
    autenticado): solo se consulta cuando `Servicio.alcance_visibilidad`
    es RESTRINGIDO. La ausencia de filas activas no otorga visibilidad
    (RN-038, explícito) — no es una regla inferida por el código.
    """

    class TipoAlcance(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        AREA = "AREA", "Área"
        UNIDAD = "UNIDAD", "Unidad de negocio"

    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE, related_name="visibilidad")
    tipo_alcance = models.CharField(max_length=10, choices=TipoAlcance.choices)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="servicios_visibles",
    )
    area = models.ForeignKey(
        Area, on_delete=models.CASCADE, null=True, blank=True, related_name="servicios_visibles"
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="servicios_visibles",
    )
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            # Coherencia de alcance, mismo patrón que AsignacionRol (0.3).
            models.CheckConstraint(
                check=(
                    Q(tipo_alcance="USUARIO", usuario__isnull=False, area__isnull=True, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="AREA", area__isnull=False, usuario__isnull=True, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="UNIDAD", unidad_negocio__isnull=False, usuario__isnull=True, area__isnull=True)
                ),
                name="ck_serviciovisibilidad_alcance_coherente",
            ),
            # Una UniqueConstraint parcial POR RAMA, no combinada: Postgres
            # no trata NULL=NULL como duplicado incluso en constraints
            # condicionales, así que una sola constraint sobre
            # [servicio, usuario, area, unidad_negocio] nunca se dispararía
            # para AREA ni UNIDAD (lección ya aplicada en AsignacionRol).
            UniqueConstraint(
                fields=["servicio", "usuario"],
                condition=Q(activo=True, tipo_alcance="USUARIO"),
                name="uq_visibilidad_usuario_activa",
            ),
            UniqueConstraint(
                fields=["servicio", "area"],
                condition=Q(activo=True, tipo_alcance="AREA"),
                name="uq_visibilidad_area_activa",
            ),
            UniqueConstraint(
                fields=["servicio", "unidad_negocio"],
                condition=Q(activo=True, tipo_alcance="UNIDAD"),
                name="uq_visibilidad_unidad_activa",
            ),
        ]

    def __str__(self):
        return f"{self.servicio} — {self.tipo_alcance}"


class ServicioResponsable(RegistroBase):
    """Personas o equipos autorizados para atender el servicio. RQF-033.

    RN-009: independiente de la pertenencia organizacional — pertenecer al
    área/unidad de un servicio no otorga responsabilidad automáticamente.
    """

    class TipoResponsable(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        EQUIPO = "EQUIPO", "Equipo"

    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE, related_name="responsables")
    tipo_responsable = models.CharField(max_length=10, choices=TipoResponsable.choices)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="servicios_responsable",
    )
    equipo = models.ForeignKey(
        Equipo, on_delete=models.CASCADE, null=True, blank=True, related_name="servicios_responsable"
    )
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(tipo_responsable="USUARIO", usuario__isnull=False, equipo__isnull=True)
                    | Q(tipo_responsable="EQUIPO", equipo__isnull=False, usuario__isnull=True)
                ),
                name="ck_servicioresponsable_tipo_coherente",
            ),
            UniqueConstraint(
                fields=["servicio", "usuario"],
                condition=Q(activo=True, tipo_responsable="USUARIO"),
                name="uq_responsable_usuario_activo",
            ),
            UniqueConstraint(
                fields=["servicio", "equipo"],
                condition=Q(activo=True, tipo_responsable="EQUIPO"),
                name="uq_responsable_equipo_activo",
            ),
        ]

    def __str__(self):
        return f"{self.servicio} — {self.tipo_responsable}"


class ServicioContextoAtencion(RegistroBase):
    """Contexto organizacional de enrutamiento de atención. RQF-061 (CU-017),
    decisión aprobada 2.3 (alternativa B de la micropropuesta).

    Responde exclusivamente "¿en qué área/unidad se atiende operativamente
    este servicio?" — deliberadamente separado de `ServicioResponsable`
    (RQF-033, "quién puede atender") y de la asignación concreta del Ticket
    (`Ticket.usuario_responsable`/`equipo_responsable`, "quién atiende ESTE
    caso"). Coincidir con un contexto AREA/UNIDAD nunca concede por sí solo
    capacidad de atender (RQF-028, RN-009) — solo acota el alcance que
    evalúa el permiso `tickets.atender`; la capacidad real de tomar sigue
    viniendo de `ServicioResponsable`/la relación operacional vigente (ver
    `apps.tickets.autorizacion`).

    Un servicio transversal admite múltiples filas activas simultáneas
    (varias AREA y/o varias UNIDAD) — no se impone cardinalidad 1, mismo
    criterio que el resto del modelo organizacional (RN-003/004/005). No
    existe un tipo de alcance GLOBAL aquí: la ausencia de filas no equivale
    a alcance global (esa lectura fue rechazada explícitamente) — un Ticket
    sin contextos configurados sigue siendo alcanzable solo por relaciones
    directas o por `tickets.atender` en alcance GLOBAL.
    """

    class TipoAlcance(models.TextChoices):
        AREA = "AREA", "Área"
        UNIDAD = "UNIDAD", "Unidad de negocio"

    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE, related_name="contextos_atencion")
    tipo_alcance = models.CharField(max_length=10, choices=TipoAlcance.choices)
    area = models.ForeignKey(
        Area, on_delete=models.CASCADE, null=True, blank=True, related_name="contextos_atencion_servicio"
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="contextos_atencion_servicio",
    )
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(tipo_alcance="AREA", area__isnull=False, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="UNIDAD", unidad_negocio__isnull=False, area__isnull=True)
                ),
                name="ck_contextoatencion_alcance_coherente",
            ),
            UniqueConstraint(
                fields=["servicio", "area"],
                condition=Q(activo=True, tipo_alcance="AREA"),
                name="uq_contextoatencion_area_activo",
            ),
            UniqueConstraint(
                fields=["servicio", "unidad_negocio"],
                condition=Q(activo=True, tipo_alcance="UNIDAD"),
                name="uq_contextoatencion_unidad_activo",
            ),
        ]

    def __str__(self):
        return f"{self.servicio} — {self.tipo_alcance}"
