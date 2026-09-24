from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils import timezone

from .base import RegistroBase
from .organizacion import Area, UnidadNegocio


class Permiso(RegistroBase):
    """Unidad atómica de autorización. RQF-022 (CU-007): administrable por
    un Administrador de Plataforma, no es un catálogo cerrado de desarrollo.
    """

    codigo = models.CharField(max_length=100, unique=True)
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.codigo


class RolFuncional(RegistroBase):
    """Agrupador configurable de permisos. RQF-021, RN-007.

    Deliberadamente sin campo `codigo`: la ausencia de un identificador
    estable ayuda a que nadie compare por él, pero RN-007 se garantiza en
    realidad de forma arquitectónica en `apps/core/autorizacion.py` — ese
    módulo es el único lugar donde se resuelven decisiones de autorización,
    y solo lo hace a través de los permisos efectivos asociados al rol
    (`RolPermiso` → `Permiso.codigo`), nunca por `nombre` ni por ningún otro
    identificador del rol.
    """

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class RolPermiso(RegistroBase):
    """Asociación rol↔permiso. RQF-022.

    Sin vigencia: RQF-022 no exige fecha_inicio/fecha_fin para esta
    relación (a diferencia de la asignación de rol a usuario, RQF-023).
    """

    rol = models.ForeignKey(RolFuncional, on_delete=models.CASCADE, related_name="rolpermiso")
    permiso = models.ForeignKey(Permiso, on_delete=models.CASCADE, related_name="rolpermiso")
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["rol", "permiso"], name="uq_rol_permiso"),
        ]

    def __str__(self):
        return f"{self.rol} → {self.permiso}"


class AsignacionRol(RegistroBase):
    """Asignación de rol a usuario con alcance y vigencia. RQF-023/024/025.

    Alcance limitado a GLOBAL/AREA/UNIDAD (RQF-025, RN-008): SERVICIO y
    PROCESO se añaden por migración de esquema cuando existan esos dominios,
    sin cambiar cómo se resuelven estos tres.
    """

    class TipoAlcance(models.TextChoices):
        GLOBAL = "GLOBAL", "Global"
        AREA = "AREA", "Área"
        UNIDAD = "UNIDAD", "Unidad de negocio"

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asignaciones_rol"
    )
    rol = models.ForeignKey(RolFuncional, on_delete=models.CASCADE, related_name="asignaciones")
    tipo_alcance = models.CharField(max_length=10, choices=TipoAlcance.choices)
    area = models.ForeignKey(
        Area, on_delete=models.CASCADE, related_name="asignaciones_rol", null=True, blank=True
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio,
        on_delete=models.CASCADE,
        related_name="asignaciones_rol",
        null=True,
        blank=True,
    )
    activo = models.BooleanField(default=True)
    fecha_inicio = models.DateField(default=timezone.now)
    fecha_fin = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            # Coherencia de alcance: GLOBAL sin área/unidad; AREA exige área
            # y rechaza unidad; UNIDAD exige unidad y rechaza área.
            models.CheckConstraint(
                check=(
                    Q(tipo_alcance="GLOBAL", area__isnull=True, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="AREA", area__isnull=False, unidad_negocio__isnull=True)
                    | Q(tipo_alcance="UNIDAD", unidad_negocio__isnull=False, area__isnull=True)
                ),
                name="ck_asignacionrol_alcance_coherente",
            ),
            # Evita duplicar una asignación activa idéntica (mismo usuario +
            # rol + alcance) sin impedir conservar una asignación histórica
            # (activo=False) y crear una nueva después. Una constraint por
            # rama, no una combinada: `area` y `unidad_negocio` nunca son
            # NULL a la vez en la misma fila (lo garantiza la constraint de
            # coherencia de arriba), y Postgres no trata NULL=NULL como
            # duplicado — una única constraint que incluyera ambos campos
            # nunca se dispararía para AREA ni para UNIDAD, porque cada rama
            # deja NULL exactamente el campo que no le corresponde.
            UniqueConstraint(
                fields=["usuario", "rol"],
                condition=Q(activo=True, tipo_alcance="GLOBAL"),
                name="uq_asignacion_global_activa",
            ),
            UniqueConstraint(
                fields=["usuario", "rol", "area"],
                condition=Q(activo=True, tipo_alcance="AREA"),
                name="uq_asignacion_area_activa",
            ),
            UniqueConstraint(
                fields=["usuario", "rol", "unidad_negocio"],
                condition=Q(activo=True, tipo_alcance="UNIDAD"),
                name="uq_asignacion_unidad_activa",
            ),
        ]

    def __str__(self):
        return f"{self.usuario} — {self.rol} ({self.tipo_alcance})"
