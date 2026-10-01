"""Aprobaciones — Sprint 3.4 (CU-024/025, RQF-078 a 084, RN-025/026).

Dominio reutilizable e independiente de Workflow, mismo criterio
arquitectónico aprobado para `apps.tareas` en 3.3: `apps/aprobaciones`
**nunca importa `apps/workflows`**, ni siquiera de forma diferida — la
integración vive del lado de `apps/workflows`
(`EsquemaAprobacionWorkflow`, `apps.workflows.integracion.
resolver_aprobacion_workflow`).

Separación explícita, aprobada en el diseño técnico previo:

    EsquemaAprobacion — coordinación y resultado consolidado.
    Aprobacion        — participación/decisión individual, inmutable una
                         vez decidida (RN-026, protegido en
                         `apps.aprobaciones.operaciones`, no aquí).

Sin campo `activa`/`INACTIVA` (decisión aprobada): el participante
accionable en SECUENCIAL se deriva del menor `orden` que siga PENDIENTE
(`apps.aprobaciones.autorizacion.puede_aprobar`) — PARALELA no tiene esa
restricción, cualquier PENDIENTE es accionable.

Sin `EsquemaAprobacion.origen` (ajuste aprobado sobre el diseño previo): en
3.4 el único origen real es Workflow/SISTEMA — no hay todavía un caso
documentado de aprobación manual, así que no se modela un campo que hoy
solo tomaría un valor constante. Si aparece otro origen, se modela
entonces.

Inmutabilidad (RN-026) se protege en `apps.aprobaciones.operaciones`
(`transaction.atomic` + `select_for_update` + revalidar `estado==PENDIENTE`
antes de decidir) — **no** con una señal `pre_save` (instrucción explícita
del usuario: no se llama "constraint de inmutabilidad" a algo que un
`CheckConstraint` estático de PostgreSQL no puede expresar). Los
constraints de este módulo solo protegen coherencia estática entre campos
en un instante dado, nunca una transición en el tiempo."""

from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint

from apps.core.models import Equipo, RegistroBase


class EsquemaAprobacion(RegistroBase):
    """Coordinación y resultado consolidado de un esquema de aprobación
    (CU-025). `resultado is None` ⇔ todavía en curso — mismo criterio de
    minimalismo que `InstanciaWorkflow.finalizada_en`: no hace falta un
    campo `estado` aparte."""

    class Modo(models.TextChoices):
        SECUENCIAL = "SECUENCIAL", "Secuencial"
        PARALELA = "PARALELA", "Paralela"

    class Politica(models.TextChoices):
        TODOS = "TODOS", "Todos deben aprobar"
        CUALQUIERA = "CUALQUIERA", "Cualquiera aprueba"

    class Resultado(models.TextChoices):
        APROBADA = "APROBADA", "Aprobada"
        RECHAZADA = "RECHAZADA", "Rechazada"
        DEVUELTA = "DEVUELTA", "Devuelta"

    modo = models.CharField(max_length=10, choices=Modo.choices)
    # Solo aplica si modo=PARALELA (SECUENCIAL cierra según orden +
    # prioridad de decisión, sin política de cierre propia) — ver
    # CheckConstraint.
    politica = models.CharField(max_length=10, choices=Politica.choices, blank=True)
    resultado = models.CharField(max_length=10, choices=Resultado.choices, null=True, blank=True)
    resuelto_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            CheckConstraint(
                check=(
                    Q(modo="PARALELA", politica__in=["TODOS", "CUALQUIERA"])
                    | Q(modo="SECUENCIAL", politica="")
                ),
                name="ck_esquemaaprobacion_politica_coherente",
            ),
        ]

    def __str__(self):
        return f"Esquema #{self.pk} ({self.get_modo_display()})"


class Aprobacion(RegistroBase):
    """Participación/decisión individual (CU-024) — inmutable una vez que
    `estado` sale de PENDIENTE (RN-026, protegido en
    `apps.aprobaciones.operaciones`, no aquí).

    `aprobador_equipo` (decisión aprobada F, "equipo como un único puesto
    de aprobación"): un único puesto por equipo, nunca una fila por
    integrante — cualquier miembro activo autorizado puede resolverla;
    `decidido_por` registra qué usuario concreto lo hizo, distinto de
    `aprobador_usuario` (que en ese caso permanece `None`)."""

    class TipoAprobador(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        EQUIPO = "EQUIPO", "Equipo"

    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        APROBADA = "APROBADA", "Aprobada"
        RECHAZADA = "RECHAZADA", "Rechazada"
        DEVUELTA = "DEVUELTA", "Devuelta"
        NO_REQUERIDA = "NO_REQUERIDA", "No requerida"

    esquema = models.ForeignKey(EsquemaAprobacion, on_delete=models.CASCADE, related_name="participaciones")
    orden = models.PositiveIntegerField()
    tipo_aprobador = models.CharField(max_length=10, choices=TipoAprobador.choices)
    aprobador_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    aprobador_equipo = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decidido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    observacion = models.TextField(blank=True)
    decidida_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["esquema", "orden"], name="uq_aprobacion_orden"),
            CheckConstraint(
                check=(
                    Q(tipo_aprobador="USUARIO", aprobador_usuario__isnull=False, aprobador_equipo__isnull=True)
                    | Q(tipo_aprobador="EQUIPO", aprobador_equipo__isnull=False, aprobador_usuario__isnull=True)
                ),
                name="ck_aprobacion_tipo_aprobador_coherente",
            ),
            # Coherencia de decisión (corrección aprobada sobre el diseño
            # previo): PENDIENTE/NO_REQUERIDA nunca llevan decisor ni
            # fecha; los 3 resultados humanos siempre los exigen. Esto NO
            # es lo que protege RN-026 (eso lo hace `operaciones.py` bajo
            # lock) — solo evita que un estado terminal quede con datos de
            # decisión a medias.
            CheckConstraint(
                check=(
                    Q(estado__in=["PENDIENTE", "NO_REQUERIDA"], decidido_por__isnull=True, decidida_en__isnull=True)
                    | Q(
                        estado__in=["APROBADA", "RECHAZADA", "DEVUELTA"],
                        decidido_por__isnull=False,
                        decidida_en__isnull=False,
                    )
                ),
                name="ck_aprobacion_decision_coherente",
            ),
        ]
        ordering = ["esquema_id", "orden"]

    def __str__(self):
        return f"Aprobación #{self.pk} de {self.esquema}"


class ReasignacionAprobacion(models.Model):
    """Trazabilidad de reasignación (RQF-083) — append-only, mismo criterio
    que `apps.workflows.models.TareaWorkflow`: la crea una única vez
    `apps.aprobaciones.operaciones.reasignar_aprobacion`, nunca se edita.
    Sin `DelegacionAprobacion` (instrucción explícita: RQF-083 dice
    "reasignar", nunca "delegar" — a diferencia de RQF-075 de Tareas)."""

    aprobacion = models.ForeignKey(Aprobacion, on_delete=models.PROTECT, related_name="reasignaciones")
    aprobador_anterior_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    aprobador_anterior_equipo = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    aprobador_nuevo_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    aprobador_nuevo_equipo = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reasignado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    motivo = models.TextField(blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"Reasignación de {self.aprobacion}"
