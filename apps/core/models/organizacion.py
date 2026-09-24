from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils import timezone

from .base import RegistroBase


class Area(RegistroBase):
    """RQF-010, RQF-011, RQF-012: catálogo de áreas, independiente de Unidad."""

    nombre = models.CharField(max_length=150)
    codigo = models.CharField(max_length=30, unique=True)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class UnidadNegocio(RegistroBase):
    """RQF-013: catálogo de unidades de negocio, independiente de Área (RN-003)."""

    nombre = models.CharField(max_length=150)
    codigo = models.CharField(max_length=30, unique=True)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class AreaUnidadNegocio(RegistroBase):
    """Relación transversal Área↔Unidad, sin jerarquía. RQF-014, RN-003.

    Sin fecha_inicio/fecha_fin: ningún RQF/RN vigente exige historial
    temporal de esta relación; `activo` basta para desactivar sin eliminar.
    """

    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="unidades_relacionadas")
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.CASCADE, related_name="areas_relacionadas"
    )
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["area", "unidad_negocio"], name="uq_area_unidad_negocio"),
        ]

    def __str__(self):
        return f"{self.area} ↔ {self.unidad_negocio}"


class UsuarioArea(RegistroBase):
    """Pertenencia de usuario a área(s). RQF-016, RN-004, RN-036.

    RN-036: un usuario puede pertenecer a varias áreas, pero solo una
    asociación activa puede ser la principal — forzado por constraint de BD.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="areas"
    )
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="usuarios")
    es_principal = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["usuario", "area"], name="uq_usuario_area"),
            UniqueConstraint(
                fields=["usuario"],
                condition=Q(es_principal=True, activo=True),
                name="uq_usuario_area_principal_activa",
            ),
        ]

    def __str__(self):
        return f"{self.usuario} — {self.area}"


class UsuarioUnidadNegocio(RegistroBase):
    """Pertenencia de usuario a unidad(es) de negocio. RQF-017, RN-004, RN-037.

    RN-037: un usuario puede pertenecer a varias unidades, pero solo una
    asociación activa puede ser la principal — forzado por constraint de BD.
    """

    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="unidades_negocio"
    )
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.CASCADE, related_name="usuarios"
    )
    es_principal = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["usuario", "unidad_negocio"], name="uq_usuario_unidad_negocio"),
            UniqueConstraint(
                fields=["usuario"],
                condition=Q(es_principal=True, activo=True),
                name="uq_usuario_unidad_principal_activa",
            ),
        ]

    def __str__(self):
        return f"{self.usuario} — {self.unidad_negocio}"


class Equipo(RegistroBase):
    """RQF-018: equipos de trabajo compuestos por usuarios."""

    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.nombre


class MiembroEquipo(RegistroBase):
    """Membresía de equipo con vigencia e historial. RQF-019.

    A diferencia de las demás relaciones de este módulo, aquí sí se exige
    vigencia: retirar a un miembro marca `activo=False` y `fecha_fin`, sin
    borrar la fila, preservando el historial de membresías.
    """

    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name="miembros")
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="equipos"
    )
    activo = models.BooleanField(default=True)
    fecha_inicio = models.DateField(default=timezone.now)
    fecha_fin = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.usuario} en {self.equipo}"


class EquipoArea(RegistroBase):
    """Relación transversal equipo↔área. RQF-020, RN-005.

    Sin fecha_inicio/fecha_fin: RQF-020 exige transversalidad, no historial
    temporal de esta relación específica; `activo` basta.
    """

    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name="areas")
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="equipos")
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["equipo", "area"], name="uq_equipo_area"),
        ]

    def __str__(self):
        return f"{self.equipo} ↔ {self.area}"


class EquipoUnidadNegocio(RegistroBase):
    """Relación transversal equipo↔unidad de negocio. RQF-020, RN-005."""

    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name="unidades_negocio")
    unidad_negocio = models.ForeignKey(
        UnidadNegocio, on_delete=models.CASCADE, related_name="equipos"
    )
    activo = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["equipo", "unidad_negocio"], name="uq_equipo_unidad_negocio"),
        ]

    def __str__(self):
        return f"{self.equipo} ↔ {self.unidad_negocio}"
