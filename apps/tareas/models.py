"""Tareas operacionales — Sprint 3.3 (CU-021/022/023, RQF-071 a 077, RN-024).

Dominio reutilizable e independiente de Workflow: RQF-071 dice explícitamente
que la bandeja consolida "tareas asignadas al usuario provenientes de
tickets o procesos" — Tarea no es un artefacto exclusivo del motor. Por eso
`apps/tareas` **nunca importa `apps/workflows`**, ni siquiera de forma
diferida: la integración vive del lado de `apps/workflows` (ver
`apps/workflows/models.py::TareaWorkflow` y `apps/workflows/integracion.py`),
corrección explícita del usuario sobre la propuesta original (que proponía
una FK `Tarea → InstanciaEtapa`).

Ciclo de vida propio, sin mezclarse con `InstanciaEtapa.Estado`/
`InstanciaWorkflow.Estado` (dominios distintos, corrección aprobada):

    PENDIENTE → EN_PROGRESO → COMPLETADA

"VENCIDA" (RQF-077) es una condición derivada (`fecha_limite < ahora AND
estado != COMPLETADA`), nunca un estado persistido — mismo criterio que
`Ticket` (que tampoco tiene un estado `VENCIDO` propio). Sin `CANCELADA`:
ningún CU/RQF de Tarea la documenta.

`origen` usa exactamente el mismo vocabulario MANUAL/SISTEMA ya establecido
en todo el proyecto (`Ticket.Origen`, `RegistroAuditoria.Origen`) — nunca
"WORKFLOW" como valor: que una Tarea provenga de un Workflow concreto lo
dice la existencia de una fila `TareaWorkflow` en `apps.workflows`, no un
valor dentro de este dominio (así Tarea sigue sin saber qué la originó).
Ninguna Tarea de origen SISTEMA inventa un usuario técnico (W.9,
corrección aprobada): `creada_por` queda `NULL`, garantizado por constraint.

`prioridad` no existe: ningún RQF/RN de Tarea la documenta (W.8, corrección
aprobada) — no se adopta por costumbre.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import CheckConstraint, Q

from apps.core.models import Equipo, RegistroBase


class Tarea(RegistroBase):
    """Unidad de trabajo humano operacional (CU-021).

    `usuario_responsable`/`equipo_responsable` coexisten (nunca se limpian
    entre sí), mismo patrón exacto que `apps.tickets.models.Ticket` —
    respaldado por RQF-077 ("consultar tareas del equipo").

    `completada_por`/`completada_en` satisfacen RN-024 literalmente
    ("completar una tarea debe quedar asociado al usuario que realizó la
    acción y al momento de ejecución") sin necesitar consultar el
    historial operacional para un dato tan básico.

    `permite_subtareas` (RQF-074: "cuando la configuración lo permita")
    vive aquí, en la instancia, no solo en la configuración de la Etapa que
    la originó — así una Tarea independiente (fuera de Workflow) también
    puede declararlo, y `apps.tareas` nunca necesita leer
    `apps.workflows.ConfiguracionEtapaTarea` para decidir si acepta
    subtareas (ver `EstrategiaTarea`, que copia el valor al crear).

    `tarea_padre` — un solo nivel en V1 (decisión de diseño, sin respaldo
    documental de profundidad — W.5, corrección aprobada): una subtarea
    nunca puede tener a su vez otra subtarea, validado en `save()`.
    """

    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        EN_PROGRESO = "EN_PROGRESO", "En progreso"
        COMPLETADA = "COMPLETADA", "Completada"

    class Origen(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        SISTEMA = "SISTEMA", "Sistema"

    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.MANUAL)
    # RQF-072/077 — fuente de este valor: ver nota en
    # `apps/workflows/models.py::ConfiguracionEtapaTarea` (W.2, corrección
    # aprobada) sobre por qué 3.3 no deriva automáticamente esta fecha
    # desde la Etapa que origina la Tarea.
    fecha_limite = models.DateTimeField(null=True, blank=True)
    creada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    usuario_responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tareas_responsable",
    )
    equipo_responsable = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="tareas_responsable"
    )
    iniciada_en = models.DateTimeField(null=True, blank=True)
    completada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    completada_en = models.DateTimeField(null=True, blank=True)
    permite_subtareas = models.BooleanField(default=False)
    tarea_padre = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="subtareas"
    )

    class Meta:
        constraints = [
            CheckConstraint(
                check=Q(origen="SISTEMA", creada_por__isnull=True) | ~Q(origen="SISTEMA"),
                name="ck_tarea_sistema_sin_creador",
            ),
        ]
        ordering = ["-creado_en"]

    def _exigir_un_solo_nivel(self):
        if self.tarea_padre_id and self.tarea_padre.tarea_padre_id:
            raise ValidationError(
                "Una subtarea no puede tener a su vez otra subtarea (V1: un solo nivel)."
            )

    def clean(self):
        self._exigir_un_solo_nivel()

    def save(self, *args, **kwargs):
        self._exigir_un_solo_nivel()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.titulo


class ComentarioTarea(models.Model):
    """Comunicación general de una Tarea — RQF-076 ("adjuntar evidencias y
    comentarios"). Cronológica y plana, sin threading — mismo criterio que
    `apps.tickets.models.ComentarioTicket` (ningún RQF de Tarea exige árbol
    de respuestas). Append-only: no hereda `RegistroBase` (un
    `actualizado_en` insinuaría edición, que ninguna vía ordinaria permite)."""

    tarea = models.ForeignKey(Tarea, on_delete=models.CASCADE, related_name="comentarios")
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    contenido = models.TextField()
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"{self.tarea} — comentario de {self.autor}"


class AdjuntoTarea(RegistroBase):
    """Evidencia adjunta — RQF-076. Mismo patrón que
    `apps.tickets.models.Adjunto`: 2 padres conocidos de antemano (la Tarea
    directamente, o un comentario suyo), discriminador + `CheckConstraint`
    de coherencia, sin `GenericForeignKey` (mismo criterio que el resto del
    proyecto). El archivo permanece protegido por autorización: 3.3 no
    construye ninguna vista de descarga (backend únicamente) — cuando 3.UI
    la construya, debe gatear el acceso con
    `apps.tareas.autorizacion.puede_consultar_tarea` antes de servir el
    archivo, igual que ya hace Tickets; no debe enlazarse `MEDIA_URL`
    directamente."""

    class TipoRelacion(models.TextChoices):
        TAREA = "TAREA", "Tarea"
        COMENTARIO = "COMENTARIO", "Comentario"

    tipo_relacion = models.CharField(max_length=15, choices=TipoRelacion.choices)
    tarea = models.ForeignKey(Tarea, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos")
    comentario = models.ForeignKey(
        ComentarioTarea, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    archivo = models.FileField(upload_to="tareas/adjuntos/%Y/%m/")
    nombre_original = models.CharField(max_length=255)
    tipo_mime = models.CharField(max_length=255, blank=True)
    tamano_bytes = models.PositiveIntegerField()
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            CheckConstraint(
                check=(
                    Q(tipo_relacion="TAREA", tarea__isnull=False, comentario__isnull=True)
                    | Q(tipo_relacion="COMENTARIO", comentario__isnull=False, tarea__isnull=True)
                ),
                name="ck_adjuntotarea_relacion_coherente",
            ),
        ]

    def __str__(self):
        return self.nombre_original


class DelegacionTarea(models.Model):
    """Delegación temporal — CU-022/RQF-075, W.1 (corrección aprobada).

    RQF-075 distingue "reasignar **o** delegar" como dos verbos con efecto
    distinto: reasignar cambia el responsable formal; delegar NO lo toca
    (`usuario_responsable`/`equipo_responsable` de `Tarea` permanecen
    intactos) — durante `[desde, hasta)` el delegado obtiene la misma
    relación operacional que el responsable directo (ver
    `apps.tareas.autorizacion.es_responsable_actual`), sin necesidad de un
    job que revierta nada al vencer: "vigencia resuelve el problema"
    (instrucción explícita del usuario, sin Celery, sin estado propio de
    Delegación).

    Append-only (no hereda `RegistroBase`, mismo criterio que
    `ComentarioTarea`/`HistorialTicket`): una delegación ya creada no se
    edita — se crea una nueva vigencia, o su ventana simplemente vence.
    """

    tarea = models.ForeignKey(Tarea, on_delete=models.CASCADE, related_name="delegaciones")
    delegado_a = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    delegada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    desde = models.DateTimeField()
    hasta = models.DateTimeField()
    motivo = models.TextField(blank=True)
    creada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            CheckConstraint(check=Q(desde__lt=models.F("hasta")), name="ck_delegaciontarea_vigencia_valida"),
        ]
        ordering = ["-desde"]

    def clean(self):
        if self.desde is not None and self.hasta is not None and self.desde >= self.hasta:
            raise ValidationError("La vigencia de la delegación debe cumplir desde < hasta.")
        if self.tarea_id and self.tarea.usuario_responsable_id == self.delegado_a_id:
            raise ValidationError("No tiene sentido delegar la tarea a su propio responsable directo.")

    def __str__(self):
        return f"{self.tarea} delegada a {self.delegado_a} ({self.desde} – {self.hasta})"


class HistorialTarea(models.Model):
    """Bitácora operacional de Tarea — RQF-075 ("conservando trazabilidad"),
    W.4 (corrección aprobada). Misma razón funcional que ya justificó
    `apps.tickets.models.HistorialTicket`: varios verbos de asignación con
    matices semánticos distintos (TOMAR≠ASIGNAR≠REASIGNAR≠DELEGAR) que un
    diff genérico de `RegistroAuditoria` no distingue por sí solo — ambos
    mecanismos siguen siendo responsabilidades distintas, no sustitutos
    (mismo criterio que Tickets).

    Sin evento `CREADA` (revisado explícitamente, W.4): `Tarea.creado_en` +
    `creada_por` + `origen` ya son la foto completa de creación — a
    diferencia de una reasignación, no hay un "antes" que reconstruir, así
    que un evento aquí solo duplicaría columnas que la propia Tarea ya
    tiene."""

    class TipoEvento(models.TextChoices):
        TOMADA = "TOMADA", "Tomada"
        ASIGNADA = "ASIGNADA", "Asignada"
        REASIGNADA = "REASIGNADA", "Reasignada"
        DELEGADA = "DELEGADA", "Delegada"
        INICIADA = "INICIADA", "Iniciada"
        COMPLETADA = "COMPLETADA", "Completada"

    tarea = models.ForeignKey(Tarea, on_delete=models.CASCADE, related_name="historial")
    tipo_evento = models.CharField(max_length=15, choices=TipoEvento.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    datos = models.JSONField(null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"{self.tarea} — {self.get_tipo_evento_display()}"
