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

    class Tipo(models.TextChoices):
        SERVICIO = "SERVICIO", "Servicio"
        PROCESO = "PROCESO", "Proceso"

    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.SERVICIO)
    workflow = models.ForeignKey(
        "workflows.Workflow", on_delete=models.PROTECT, null=True, blank=True, related_name="servicios"
    )
    configuracion_ejecucion_activa = models.ForeignKey(
        "catalogo.ConfiguracionEjecucionVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )

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

    class PoliticaEntrega(models.TextChoices):
        """4.5 — qué ocurre después de entregar formalmente el resultado al
        solicitante. Vacío (default) = sin política definida: los Tickets de
        este Servicio conservan el flujo anterior a 4.5 (resolver y cerrar
        manualmente, sin entrega formal). Ninguna política admite esperar
        indefinidamente."""

        CIERRE_DIRECTO = "CIERRE_DIRECTO", "Cierre al entregar, sin esperar respuesta"
        PERIODO_OBSERVACIONES = "PERIODO_OBSERVACIONES", "Periodo de observaciones"

    politica_entrega = models.CharField(
        max_length=30, choices=PoliticaEntrega.choices, blank=True, default=""
    )
    # Solo con PERIODO_OBSERVACIONES: días que el solicitante tiene para
    # aceptar u observar antes del cierre automático. Se congela en cada
    # Ticket (`Ticket.entrega_dias_observacion`) al crear su borrador.
    dias_observacion = models.PositiveSmallIntegerField(null=True, blank=True)

    class UnidadTiempo(models.TextChoices):
        HORAS = "HORAS", "Horas"
        DIAS = "DIAS", "Días"

    # 4.A1 — tiempo objetivo de atención. Pertenece al Servicio/Proceso, no al
    # Workflow (un mismo flujo puede servir a servicios con tiempos distintos).
    # Vacío = sin compromiso temporal. Cada Ticket congela estos tres valores al
    # crear su borrador (`Ticket.tiempo_objetivo_*`); cambiarlos aquí no altera
    # tickets existentes. `habiles` = lunes a viernes, sin festivos (el cálculo
    # vive en `apps.tickets.tiempos`).
    tiempo_objetivo_cantidad = models.PositiveSmallIntegerField(null=True, blank=True)
    tiempo_objetivo_unidad = models.CharField(max_length=10, choices=UnidadTiempo.choices, blank=True, default="")
    tiempo_objetivo_habiles = models.BooleanField(default=False)

    class PoliticaProrroga(models.TextChoices):
        NO_PERMITE = "NO_PERMITE", "No permite prórrogas"
        SIN_APROBACION = "SIN_APROBACION", "Prórroga directa, sin aprobación"
        CON_APROBACION = "CON_APROBACION", "Prórroga con aprobación"

    # 4.A2 — política de prórroga de la fecha objetivo. Vacío = sin política
    # definida (equivale a NO_PERMITE: así se comportan los tickets anteriores).
    # Cada Ticket congela política y aprobador al crear su borrador. Solo con
    # CON_APROBACION hay UN aprobador fijo (usuario o equipo, los mismos tipos
    # que `ServicioResponsable`); el Servicio no puede quedar con esa política
    # sin aprobador (constraint), y publicarlo exige que siga activo.
    politica_prorroga = models.CharField(max_length=30, choices=PoliticaProrroga.choices, blank=True, default="")
    prorroga_aprobador_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    prorroga_aprobador_equipo = models.ForeignKey(
        Equipo, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # 4.C1 — marca al ÚNICO Servicio interno que respalda al Ticket General (la
    # entrada para pedir algo que no está catalogado). Es un Servicio normal para
    # el dominio (formulario, tiempo objetivo, prórroga, entrega, visibilidad,
    # Ticket/TicketServicio), pero nunca se ofrece como Servicio catalogado: la
    # exclusión vive en `apps.catalogo.visibilidad.servicios_visibles_para`. Solo
    # se cambia mediante `apps.catalogo.ticket_general` (no es editable aquí ni en
    # Admin): un servicio con tickets no puede dejar de serlo, así el origen de un
    # ticket se deduce siempre de `TicketServicio.servicio` sin duplicarlo.
    es_ticket_general = models.BooleanField(default=False)

    class Meta:
        constraints = [
            # A lo sumo UN Servicio marcado como Ticket General.
            models.UniqueConstraint(
                fields=["es_ticket_general"],
                condition=Q(es_ticket_general=True),
                name="uq_servicio_ticket_general_unico",
            ),
            models.CheckConstraint(
                condition=Q(es_ticket_general=False) | Q(tipo="SERVICIO"),
                name="ck_servicio_ticket_general_es_servicio",
                violation_error_message="El ticket general debe ser un Servicio, no un Proceso.",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        politica_prorroga="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=False,
                        prorroga_aprobador_equipo__isnull=True,
                    )
                    | Q(
                        politica_prorroga="CON_APROBACION",
                        prorroga_aprobador_usuario__isnull=True,
                        prorroga_aprobador_equipo__isnull=False,
                    )
                    | (
                        ~Q(politica_prorroga="CON_APROBACION")
                        & Q(prorroga_aprobador_usuario__isnull=True, prorroga_aprobador_equipo__isnull=True)
                    )
                ),
                name="ck_servicio_prorroga_coherente",
                violation_error_message="La prórroga con aprobación necesita exactamente un aprobador (usuario o equipo).",
            ),
            models.CheckConstraint(
                condition=(
                    Q(tiempo_objetivo_cantidad__isnull=True, tiempo_objetivo_unidad="", tiempo_objetivo_habiles=False)
                    | Q(
                        tiempo_objetivo_cantidad__isnull=False, tiempo_objetivo_cantidad__gte=1,
                        tiempo_objetivo_unidad__in=["HORAS", "DIAS"],
                    )
                ),
                name="ck_servicio_tiempo_objetivo_coherente",
                violation_error_message="El tiempo objetivo necesita cantidad (1 o más) y unidad, o ninguno de los dos.",
            ),
            models.CheckConstraint(
                condition=(
                    Q(politica_entrega="PERIODO_OBSERVACIONES", dias_observacion__isnull=False, dias_observacion__gte=1)
                    | (~Q(politica_entrega="PERIODO_OBSERVACIONES") & Q(dias_observacion__isnull=True))
                ),
                name="ck_servicio_politica_entrega_coherente",
            ),
        ]

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


class TerminoServicio(RegistroBase):
    """4.D — palabra o frase con la que una persona puede describir la necesidad
    que cubre un Servicio/Proceso ("presentación", "diapositivas", "días libres").

    Es metadato de DESCUBRIMIENTO del buscador "¿Qué necesitas?": no es un campo
    del formulario, no se le pregunta al solicitante y no tiene efecto en el
    Ticket ni en el Workflow. Pertenece al Servicio (no hay sinónimos globales).
    `termino` se guarda tal como lo escribió quien administra; la comparación
    normaliza ambos lados (`apps.catalogo.normalizacion`). Evitar dos términos
    equivalentes en un mismo Servicio es una regla de dominio
    (`apps.catalogo.terminos_busqueda`), no una constraint: la equivalencia
    depende de la normalización, que puede evolucionar sin migrar datos.
    """

    LARGO_MAXIMO = 100

    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE, related_name="terminos_busqueda")
    termino = models.CharField(max_length=LARGO_MAXIMO)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["termino", "pk"]

    def __str__(self):
        return self.termino


class DefinicionEntregable(RegistroBase):
    """Salida esperada del catálogo. Las ejecuciones conservan su propio snapshot."""

    class Tipo(models.TextChoices):
        TEXTO = "TEXTO", "Texto"
        ARCHIVO = "ARCHIVO", "Archivo"
        ENLACE = "ENLACE", "Enlace"
        CONFIRMACION = "CONFIRMACION", "Confirmación"

    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE, related_name="definiciones_entregables")
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    obligatorio = models.BooleanField(default=False)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["orden", "pk"]

    def __str__(self):
        return self.nombre
