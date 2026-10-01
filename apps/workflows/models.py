"""Definición y ejecución de Workflow.

DEFINICIÓN (3.1, CU-020 "Diseñar y versionar workflow", RQF-063/064/069/070,
RN-020/021): `Workflow → WorkflowVersion → Etapa → TransicionEtapa`.

EJECUCIÓN (3.2, CU-020 "Ejecutar workflow" — mismo código, distinto CU en la
fuente Excel, inconsistencia documentada y no corregida silenciosamente,
W.10): `InstanciaWorkflow → InstanciaEtapa`. Separación estricta respetada
(propuesta aprobada, punto 3): `Etapa`/`WorkflowVersion` (definición
congelada) no ganan ningún campo de estado runtime — toda mutación de
ejecución vive exclusivamente en `InstanciaWorkflow`/`InstanciaEtapa`, que
por eso NO heredan el candado `exigir_editable()` de sus contrapartes de
definición (una instancia en ejecución se muta constantemente por diseño,
no es una versión congelada).

3.3 (CU-021/022/023, RQF-071 a 077) agrega `ConfiguracionEtapaTarea`
(configuración real de una Etapa TAREA — relación, no JSON, W.2) y
`TareaWorkflow` (integra `apps.tareas.Tarea` con `InstanciaEtapa` sin que
`Tarea` conozca a Workflow — corrección arquitectónica aprobada). Las
Strategies de `APROBACION` (Sprint 4) y `GACETA` (Sprint 6) siguen sin
existir — ver `apps/workflows/estrategias.py`.

`Workflow → WorkflowVersion` replica el patrón ya construido para
`Formulario → FormularioVersion` (`apps/catalogo/models/formularios.py`):
misma máquina BORRADOR/ACTIVA/HISTORICA, mismo `exigir_editable()`, mismo
criterio de que una versión fuera de BORRADOR es estructuralmente
inmutable por las vías ordinarias de dominio/Admin (no una escritura masiva
vía `QuerySet.update()`/`bulk_update()` ni acceso directo a la base de
datos — ver esa misma nota en `formularios.py`, aplica aquí sin cambios).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Index, Q, UniqueConstraint

from apps.core.models import Equipo, RegistroBase
from apps.workflows.estrategias import ESTRATEGIAS_POR_TIPO
from apps.workflows.validacion import validar_integridad_transicion


class Workflow(RegistroBase):
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    version_activa = models.ForeignKey(
        "WorkflowVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return self.nombre


class WorkflowVersion(RegistroBase):
    class Estado(models.TextChoices):
        BORRADOR = "BORRADOR", "Borrador"
        ACTIVA = "ACTIVA", "Activa"
        HISTORICA = "HISTORICA", "Histórica"

    # PROTECT (no CASCADE): borrar un Workflow nunca debe destruir en
    # cascada versiones que representan estructura histórica (RN-020) —
    # mismo razonamiento que Formulario/FormularioVersion.
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT, related_name="versiones")
    numero = models.PositiveIntegerField()
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.BORRADOR)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["workflow", "numero"], name="uq_workflowversion_numero"),
        ]
        ordering = ["workflow_id", "numero"]

    def exigir_editable(self):
        if self.estado != self.Estado.BORRADOR:
            raise ValidationError(
                "Esta versión ya no está en borrador: modificarla alteraría estructura histórica "
                "(RN-020). Cree una nueva versión para hacer cambios."
            )

    def __str__(self):
        return f"{self.workflow} — v{self.numero} ({self.get_estado_display()})"


class Etapa(RegistroBase):
    """Unidad configurable dentro de una `WorkflowVersion` (RQF-063/064).

    `tipo` reconoce los 9 valores documentados en RQF-064, pero solo 7
    tienen Strategy disponible en 3.1 — ver `apps/workflows/estrategias.py`.
    `APROBACION`/`GACETA` pueden crearse (para planear su posición en el
    flujo) pero bloquean la activación de la versión
    (`apps.workflows.validacion.validar_estructura`).
    """

    class Tipo(models.TextChoices):
        INICIO = "INICIO", "Inicio"
        TAREA = "TAREA", "Tarea"
        APROBACION = "APROBACION", "Aprobación"
        GACETA = "GACETA", "Gaceta"
        CONDICION = "CONDICION", "Condición"
        ESPERA = "ESPERA", "Espera"
        TICKET = "TICKET", "Ticket"
        HITO = "HITO", "Hito"
        FIN = "FIN", "Fin"

    version = models.ForeignKey(WorkflowVersion, on_delete=models.CASCADE, related_name="etapas")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    # Validado por la Strategy del tipo (`estrategias.py`) — nunca un
    # contenedor libre: claves fuera del esquema de su tipo son rechazadas
    # en clean(). Solo configuración simple: ninguna relación real de
    # dominio (Usuario/Equipo/Área/Unidad/Servicio/Formulario) se guarda
    # aquí — ver docstring de `estrategias.py`.
    configuracion = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["version_id", "id"]

    def clean(self):
        estrategia = ESTRATEGIAS_POR_TIPO.get(self.tipo)
        if estrategia is not None:
            estrategia.validar_configuracion(self.configuracion or {})

    def save(self, *args, **kwargs):
        self.version.exigir_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"


class ConfiguracionEtapaTarea(RegistroBase):
    """Configuración real de una Etapa TAREA — 3.3 (W.2, corrección
    aprobada). `Usuario`/`Equipo` son relaciones reales de dominio (regla
    ya aplicada en todo el proyecto: "configuración simple → JSON, relación
    real → FK/modelo relacional") — por eso viven aquí, en un modelo
    especializado `OneToOne` a `Etapa`, nunca en `Etapa.configuracion`
    (que para TAREA sigue vacía, igual que HITO/FIN).

    Solo `USUARIO`/`EQUIPO` como candidato — revisado explícitamente contra
    RQF-071 a 077/CU-021 a 023 (W.2): ninguno menciona Área/Unidad como
    dimensión de responsable de Tarea (a diferencia de `AsignacionRol`),
    así que no se agregan por analogía con autorización. `tipo_responsable`
    es opcional (`blank=True`): una Etapa TAREA sin candidato configurado
    simplemente crea Tareas sin responsable directo (PENDIENTE, disponible
    para tomar) — CU-021 documenta precondición "Tarea asignada/**visible**",
    no exige asignación previa.

    `permite_subtareas` sí pertenece aquí (RQF-074: "cuando la
    configuración lo permita" es, literalmente, esta configuración) — el
    valor se copia a `Tarea.permite_subtareas` al crearse (ver
    `EstrategiaTarea`), no se consulta desde `apps.tareas` en runtime
    (`apps.tareas` no importa `apps.workflows`).

    4.3 amplía los candidatos con SOLICITANTE y RESPONSABLE_TICKET. No
    guardan una FK fija: el resolutor consulta el Ticket al entrar en la
    etapa. Los USUARIO/EQUIPO y el candidato vacío existentes se conservan.

    **Sin `dias_plazo`** (W.2, corrección aprobada): revisado explícitamente
    contra el Excel — RQF-072 exige que una Tarea *tenga* `fecha_limite`,
    pero ningún RQF/RN dice que la Etapa deba configurar un plazo relativo
    para derivarla, y añadirlo sería inventar una regla de SLA sin
    respaldo. Decisión mínima adoptada: `Tarea.fecha_limite` existe y es
    consultable (RQF-072/077), pero 3.3 no la puebla automáticamente desde
    esta configuración — queda en `NULL` para las Tareas que genera
    `EstrategiaTarea` hasta que un incremento futuro (con su propio
    RQF/RN) determine de dónde debe salir ese valor."""

    class TipoResponsable(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        EQUIPO = "EQUIPO", "Equipo"
        RESPONSABLE_TICKET = "RESPONSABLE_TICKET", "Responsable individual del ticket"
        SOLICITANTE = "SOLICITANTE", "Solicitante del ticket"

    etapa = models.OneToOneField(Etapa, on_delete=models.CASCADE, related_name="configuracion_tarea")
    tipo_responsable = models.CharField(max_length=20, choices=TipoResponsable.choices, blank=True)
    usuario_responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    equipo_responsable = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    permite_subtareas = models.BooleanField(default=False)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["etapa"], name="uq_configuracionetapatarea_etapa"),
            models.CheckConstraint(
                check=(
                    Q(tipo_responsable__in=["", "RESPONSABLE_TICKET", "SOLICITANTE"], usuario_responsable__isnull=True, equipo_responsable__isnull=True)
                    | Q(tipo_responsable="USUARIO", usuario_responsable__isnull=False, equipo_responsable__isnull=True)
                    | Q(tipo_responsable="EQUIPO", equipo_responsable__isnull=False, usuario_responsable__isnull=True)
                ),
                name="ck_configuracionetapatarea_tipo_coherente",
            ),
        ]

    def save(self, *args, **kwargs):
        self.etapa.version.exigir_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.etapa.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Configuración TAREA de {self.etapa}"


class TransicionEtapa(RegistroBase):
    """Conecta dos `Etapa` de la misma `WorkflowVersion` (RQF-063, punto H).

    La condición de una `CONDICION` vive aquí, no en la `Etapa` origen —
    evita duplicarla en dos lugares (punto 9 del incremento aprobado). Una
    `Etapa CONDICION` tiene exactamente una transición `es_fallback=True`
    (sin variable/operador/valor) y al menos una transición condicional
    completa; se evalúan por `prioridad`, gana la primera que coincide
    (X.4). Para cualquier otra etapa de origen, los cuatro campos
    condicionales/fallback deben permanecer vacíos/inactivos — ver
    `apps.workflows.validacion.validar_integridad_transicion`.
    """

    class Operador(models.TextChoices):
        IGUAL_A = "IGUAL_A", "Igual a"
        DISTINTO_DE = "DISTINTO_DE", "Distinto de"
        CONTIENE = "CONTIENE", "Contiene"
        NO_CONTIENE = "NO_CONTIENE", "No contiene"
        MAYOR_QUE = "MAYOR_QUE", "Mayor que"
        MENOR_QUE = "MENOR_QUE", "Menor que"
        ESTA_VACIO = "ESTA_VACIO", "Está vacío"
        NO_ESTA_VACIO = "NO_ESTA_VACIO", "No está vacío"

    etapa_origen = models.ForeignKey(Etapa, on_delete=models.CASCADE, related_name="transiciones_salientes")
    etapa_destino = models.ForeignKey(Etapa, on_delete=models.CASCADE, related_name="transiciones_entrantes")
    nombre = models.CharField(max_length=150, blank=True)
    prioridad = models.PositiveIntegerField(default=0)
    variable = models.CharField(max_length=150, blank=True)
    operador = models.CharField(max_length=20, choices=Operador.choices, blank=True)
    valor = models.CharField(max_length=255, blank=True)
    es_fallback = models.BooleanField(default=False)
    # 3.4 (CU-024/025) — semántica explícita de APROBACION, deliberadamente
    # SIN reutilizar variable/operador/valor (diseño técnico aprobado: esos
    # campos están pensados para comparar un valor de contexto arbitrario,
    # no para una de 3 etiquetas cerradas; reutilizarlos también exigiría
    # sortear la prohibición explícita ya existente en
    # `validar_integridad_transicion` para cualquier etapa que no sea
    # CONDICION). Mutuamente excluyente con los 4 campos de arriba — ver
    # esa misma función.
    resultado_aprobacion = models.CharField(
        max_length=10,
        choices=[("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta")],
        blank=True,
    )

    class Meta:
        ordering = ["etapa_origen_id", "prioridad", "id"]

    def clean(self):
        validar_integridad_transicion(self)

    def save(self, *args, **kwargs):
        self.etapa_origen.version.exigir_editable()
        validar_integridad_transicion(self)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.etapa_origen.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        etiqueta = f" [{self.nombre}]" if self.nombre else ""
        return f"{self.etapa_origen} → {self.etapa_destino}{etiqueta}"


class InstanciaWorkflow(RegistroBase):
    """Ejecución concreta de una `WorkflowVersion` — 3.2 (CU-020 "Ejecutar
    workflow", RQF-065). `creado_en` (de `RegistroBase`) ES el instante de
    inicio: no se agrega un `iniciada_en` redundante (W.1) porque la
    instancia no tiene fase de borrador, se crea exactamente cuando el
    workflow arranca.

    `workflow_version` — nunca `Workflow` directo (RN-020: "una instancia
    de workflow ejecuta una versión concreta... y no cambia automáticamente
    cuando se publica otra versión"); `PROTECT` porque una versión con
    instancias en ejecución o finalizadas nunca debe poder desaparecer
    (RQF-070).

    `finalizada_en` se fija ÚNICAMENTE al alcanzar `COMPLETADA` (única
    terminación empresarial en 3.2, corrección aprobada) — nunca al entrar
    en `ERROR`: ese es un corte anómalo, no una finalización. `actualizado_en`
    (de `RegistroBase`) ya registra cuándo cambió por última vez, ERROR
    incluido, sin necesidad de un segundo campo."""

    class Estado(models.TextChoices):
        EN_EJECUCION = "EN_EJECUCION", "En ejecución"
        EN_ESPERA = "EN_ESPERA", "En espera"
        COMPLETADA = "COMPLETADA", "Completada"
        ERROR = "ERROR", "Error"

    workflow_version = models.ForeignKey(
        WorkflowVersion, on_delete=models.PROTECT, related_name="instancias"
    )
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.EN_EJECUCION)
    # JSON puro (namespaces `datos_iniciales`/`resultados_etapas`/`variables`
    # — ver `apps/workflows/contexto.py`); `encoder=DjangoJSONEncoder` es
    # solo defensa adicional (mismo criterio que
    # `RegistroAuditoria.datos_nuevos`), la disciplina real es que el motor
    # y las Strategies solo escriben tipos JSON ya normalizados (fechas como
    # texto ISO), nunca objetos ORM ni `datetime` crudos.
    contexto = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    # `null=True`: sin usuario cuando el origen del inicio es SISTEMA (mismo
    # criterio actor/origen que `RegistroAuditoria` — nunca se inventa un
    # "usuario técnico" para satisfacer esta FK).
    iniciado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    finalizada_en = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Instancia de {self.workflow_version} (#{self.pk})"


class InstanciaEtapa(RegistroBase):
    """Una EJECUCIÓN de una `Etapa` dentro de una `InstanciaWorkflow` — no
    "la etapa": 3.1 permite ciclos (`A → B → A`), así que una misma `Etapa`
    puede tener varias filas `InstanciaEtapa` dentro de la misma instancia
    (una por cada vez que se ejecuta).

    `etapa` usa `PROTECT` (no `CASCADE`): nunca debe poder borrarse una
    `Etapa` que ya fue ejecutada. En la práctica esto ya es inalcanzable
    por las vías ordinarias (`Etapa.delete()` exige
    `version.exigir_editable()`, y una versión BORRADOR nunca tiene
    instancias — solo se ejecutan versiones ACTIVA/HISTORICA), pero
    `PROTECT` es la defensa correcta a nivel de base de datos de todas
    formas.

    `orden` — contador secuencial GLOBAL de la instancia (1, 2, 3... sin
    reiniciar por etapa, W.3): distingue la primera/segunda/tercera
    ejecución de una misma `Etapa` sin necesitar un segundo contador por
    par `(instancia, etapa)`, y sirve directamente como línea de tiempo
    completa para RQF-086 ("consultar etapa actual, historial...").

    `transicion_tomada` — qué `TransicionEtapa` se siguió al completar esta
    ejecución (`None` mientras está pendiente/en curso/en espera, o si es
    un FIN sin salida). Es la pieza clave de
    `apps.workflows.motor._localizar_punto_continuacion` (W.2): permite
    determinar sin ambigüedad el punto de continuación de una instancia
    cortada por `LIMITE_AVANCES_AUTOMATICOS_POR_INVOCACION` sin tener que
    volver a invocar a la Strategy (que podría no ser determinista en un
    tipo futuro) ni repartir esa lógica en varias consultas.

    `finalizada_en` sigue el mismo criterio que en `InstanciaWorkflow`: solo
    se fija al llegar a `COMPLETADA`, nunca en `ERROR` ni en `EN_ESPERA`
    (que es una pausa, no una finalización).

    `motivo_espera` (3.3, W.3) — por qué esta ejecución está `EN_ESPERA`,
    poblado exclusivamente por el motor a partir de
    `ResultadoEjecucionEtapa.motivo_espera` (nunca inferido de `etapa.tipo`:
    RN-021). Existe para que el scheduler de 3.2.x (`apps.workflows.tasks`)
    seleccione exclusivamente esperas `TEMPORAL` y nunca intente reanudar
    una `TAREA` pendiente — sin este campo, distinguir el motivo exigiría
    repetir lógica de detección en más de un lugar, justo lo que 3.2.x ya
    evitó con una única función de candidatos. 3.4 (diseño aprobado) agrega
    `APROBACION` — mismo mecanismo genérico, `continuar_espera_externa` ya
    era motivo-agnóstico, solo faltaba el valor. `TICKET`/`GACETA` siguen
    sin agregarse (instrucción explícita: no anticipar)."""

    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        EN_EJECUCION = "EN_EJECUCION", "En ejecución"
        EN_ESPERA = "EN_ESPERA", "En espera"
        COMPLETADA = "COMPLETADA", "Completada"
        ERROR = "ERROR", "Error"

    class MotivoEspera(models.TextChoices):
        TEMPORAL = "TEMPORAL", "Espera temporal"
        TAREA = "TAREA", "Tarea"
        APROBACION = "APROBACION", "Aprobación"

    instancia_workflow = models.ForeignKey(
        InstanciaWorkflow, on_delete=models.CASCADE, related_name="ejecuciones_etapa"
    )
    etapa = models.ForeignKey(Etapa, on_delete=models.PROTECT, related_name="ejecuciones")
    orden = models.PositiveIntegerField()
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.PENDIENTE)
    iniciada_en = models.DateTimeField(null=True, blank=True)
    finalizada_en = models.DateTimeField(null=True, blank=True)
    resultado = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    error = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    motivo_espera = models.CharField(max_length=10, choices=MotivoEspera.choices, null=True, blank=True)
    transicion_tomada = models.ForeignKey(
        TransicionEtapa, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            UniqueConstraint(fields=["instancia_workflow", "orden"], name="uq_instanciaetapa_orden"),
        ]
        indexes = [
            Index(fields=["instancia_workflow", "-orden"], name="idx_instanciaetapa_ultima"),
        ]
        ordering = ["instancia_workflow_id", "orden"]

    def __str__(self):
        return f"{self.etapa} — ejecución #{self.orden} de {self.instancia_workflow}"


class TareaWorkflow(models.Model):
    """Integra `apps.tareas.Tarea` con `InstanciaEtapa` — 3.3, corrección
    arquitectónica aprobada sobre la propuesta original (que proponía una
    FK `Tarea → InstanciaEtapa`). RQF-071 demuestra que Tarea es un
    dominio reutilizable ("provenientes de tickets o procesos"): la
    relación vive del lado integrador (`apps.workflows`), nunca contamina
    el modelo base `Tarea` con una FK hacia Workflow — `apps.tareas` no
    importa `apps.workflows` en ningún módulo.

    Ambos lados `OneToOneField` (únicos): "1 ejecución TAREA → 1 Tarea" y
    "1 Tarea de Workflow → 1 ejecución", instrucción explícita. Esto
    también es lo que permite que `apps.workflows.integracion.
    completar_tarea_workflow` distinga sin ambigüedad una Tarea principal
    (tiene fila aquí) de una subtarea (nunca la tiene, W.6): completar una
    subtarea jamás encuentra un vínculo que reanudar.

    Append-only (no hereda `RegistroBase`): la crea una única vez
    `EstrategiaTarea.ejecutar()`, nunca se edita."""

    tarea = models.OneToOneField("tareas.Tarea", on_delete=models.PROTECT, related_name="vinculo_workflow")
    instancia_etapa = models.OneToOneField(
        InstanciaEtapa, on_delete=models.PROTECT, related_name="tarea_workflow"
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tarea} ↔ {self.instancia_etapa}"


class ConfiguracionEtapaAprobacion(RegistroBase):
    """Configuración real de una Etapa APROBACION — 3.4 (CU-024/025,
    RQF-081/082), mismo patrón exacto que `ConfiguracionEtapaTarea`:
    relación real (`ParticipanteEtapaAprobacion`), nunca JSON.

    `modo`/`politica` son la PLANTILLA de diseño — `apps.aprobaciones.
    operaciones.crear_esquema_aprobacion` las lee (vía `EstrategiaAprobacion`)
    para producir el `EsquemaAprobacion` real de cada ejecución, misma
    separación "definición congelada vs. ejecución" que ya existe entre
    `Etapa`/`InstanciaEtapa`. `politica` solo aplica si `modo=PARALELA`
    (mismo constraint condicional que `apps.aprobaciones.models.
    EsquemaAprobacion.politica`)."""

    class Modo(models.TextChoices):
        SECUENCIAL = "SECUENCIAL", "Secuencial"
        PARALELA = "PARALELA", "Paralela"

    class Politica(models.TextChoices):
        TODOS = "TODOS", "Todos deben aprobar"
        CUALQUIERA = "CUALQUIERA", "Cualquiera aprueba"

    etapa = models.OneToOneField(Etapa, on_delete=models.CASCADE, related_name="configuracion_aprobacion")
    modo = models.CharField(max_length=10, choices=Modo.choices)
    politica = models.CharField(max_length=10, choices=Politica.choices, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["etapa"], name="uq_configuracionetapaaprobacion_etapa"),
            models.CheckConstraint(
                check=(
                    Q(modo="PARALELA", politica__in=["TODOS", "CUALQUIERA"]) | Q(modo="SECUENCIAL", politica="")
                ),
                name="ck_configuracionetapaaprobacion_politica_coherente",
            ),
        ]

    def save(self, *args, **kwargs):
        self.etapa.version.exigir_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.etapa.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Configuración APROBACION de {self.etapa}"


class ParticipanteEtapaAprobacion(RegistroBase):
    """Un participante de la plantilla de `ConfiguracionEtapaAprobacion`
    — mismo vocabulario `USUARIO`/`EQUIPO` que `ConfiguracionEtapaTarea`.
    4.3 agrega SOLICITANTE/RESPONSABLE_TICKET sin referencia fija; se
    resuelven a un aprobador real al crear el esquema de esa ejecución.
    `orden` define la secuencia cuando `modo=SECUENCIAL` (sin efecto de
    bloqueo cuando `modo=PARALELA`, solo presentación) — mismo criterio
    que `Aprobacion.orden` en `apps.aprobaciones`."""

    class TipoAprobador(models.TextChoices):
        USUARIO = "USUARIO", "Usuario"
        EQUIPO = "EQUIPO", "Equipo"
        RESPONSABLE_TICKET = "RESPONSABLE_TICKET", "Responsable individual del ticket"
        SOLICITANTE = "SOLICITANTE", "Solicitante del ticket"

    configuracion = models.ForeignKey(
        ConfiguracionEtapaAprobacion, on_delete=models.CASCADE, related_name="participantes"
    )
    orden = models.PositiveIntegerField()
    tipo_aprobador = models.CharField(max_length=20, choices=TipoAprobador.choices)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    equipo = models.ForeignKey(Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [
            UniqueConstraint(fields=["configuracion", "orden"], name="uq_participanteetapaaprobacion_orden"),
            models.CheckConstraint(
                check=(
                    Q(tipo_aprobador="USUARIO", usuario__isnull=False, equipo__isnull=True)
                    | Q(tipo_aprobador="EQUIPO", equipo__isnull=False, usuario__isnull=True)
                    | Q(tipo_aprobador__in=["RESPONSABLE_TICKET", "SOLICITANTE"], usuario__isnull=True, equipo__isnull=True)
                ),
                name="ck_participanteetapaaprobacion_tipo_coherente",
            ),
        ]
        ordering = ["configuracion_id", "orden"]

    def save(self, *args, **kwargs):
        self.configuracion.etapa.version.exigir_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.configuracion.etapa.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Participante #{self.orden} de {self.configuracion}"


class EsquemaAprobacionWorkflow(models.Model):
    """Integra `apps.aprobaciones.EsquemaAprobacion` con `InstanciaEtapa` —
    3.4, mismo patrón exacto que `TareaWorkflow` (misma corrección
    arquitectónica: la relación vive del lado integrador `apps.workflows`,
    `apps.aprobaciones` nunca importa `apps.workflows`). Ambos lados
    `OneToOneField`. Append-only: la crea una única vez
    `EstrategiaAprobacion.ejecutar()`, nunca se edita."""

    esquema = models.OneToOneField(
        "aprobaciones.EsquemaAprobacion", on_delete=models.PROTECT, related_name="vinculo_workflow"
    )
    instancia_etapa = models.OneToOneField(
        InstanciaEtapa, on_delete=models.PROTECT, related_name="esquema_aprobacion"
    )
    creado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.esquema} ↔ {self.instancia_etapa}"
