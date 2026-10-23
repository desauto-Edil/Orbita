from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.catalogo import claves
from apps.core.models import RegistroBase


class ConfiguracionEjecucionVersion(RegistroBase):
    """Configuracion operativa versionada de un Servicio sobre una plantilla.

    D3.R1 separa la plantilla general (`WorkflowVersion` con fases) del
    contenido especifico del Servicio/Proceso. Esta version es lo que un
    Ticket podra congelar mas adelante junto con la version de Workflow.
    """

    class Estado(models.TextChoices):
        BORRADOR = "BORRADOR", "Borrador"
        ACTIVA = "ACTIVA", "Activa"
        HISTORICA = "HISTORICA", "Historica"

    servicio = models.ForeignKey(
        "catalogo.Servicio", on_delete=models.CASCADE, related_name="configuraciones_ejecucion"
    )
    workflow_version = models.ForeignKey(
        "workflows.WorkflowVersion", on_delete=models.PROTECT, related_name="configuraciones_servicio"
    )
    numero = models.PositiveIntegerField()
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.BORRADOR)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["servicio", "numero"], name="uq_configejec_servicio_numero"),
            UniqueConstraint(
                fields=["servicio"],
                condition=Q(estado="ACTIVA"),
                name="uq_configejec_servicio_activa",
            ),
        ]
        ordering = ["servicio_id", "numero"]

    def exigir_editable(self):
        if self.estado != self.Estado.BORRADOR:
            raise ValidationError(
                "Esta configuracion de ejecucion ya no esta en borrador. Cree una nueva version para cambiarla."
            )

    def clean(self):
        if self.servicio_id is None or self.workflow_version_id is None:
            return
        workflow = self.workflow_version.workflow
        if self.servicio.workflow_id != workflow.pk:
            raise ValidationError("La configuracion debe usar una version del Workflow vinculado al Servicio.")
        if workflow.modo != "PLANTILLA_FASES":
            raise ValidationError("La configuracion operativa solo aplica a plantillas de fases.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.servicio} - configuracion v{self.numero} ({self.get_estado_display()})"


class BloqueOperativo(RegistroBase):
    """Bloque operativo especifico del Servicio dentro de una fase.

    Conserva la semantica empresarial que antes se mezclaba en `Etapa`:
    actividad, aprobacion, espera o decision. D3.R1 aun no ejecuta estos
    bloques; solo deja una fuente editable de verdad para el nuevo dominio.
    """

    class Tipo(models.TextChoices):
        ACTIVIDAD = "ACTIVIDAD", "Actividad"
        APROBACION = "APROBACION", "Aprobacion"
        # Histórico (4.B1): ya NO es configurable en V1 (ver `TIPOS_CONFIGURABLES`), pero
        # el valor se conserva para leer y ejecutar configuraciones ya guardadas.
        ESPERA = "ESPERA", "Espera"
        DECISION = "DECISION", "Decision"
        ENTREGABLE = "ENTREGABLE", "Entregable"
        # 4.G2D2 — solo en un Proceso con tareas por elemento configuradas: convierte los elementos del
        # Ticket en Tareas (`apps.tareas`) y espera a que se completen.
        TAREAS_ELEMENTOS = "TAREAS_ELEMENTOS", "Tareas por elemento"
        # Fase 2 — bloques CONTENEDORES: sus bloques contenidos (`contenedor`) los ejecutan subinstancias del
        # mismo motor. GRUPO_ACTIVIDADES: varias actividades generales en paralelo. RECORRIDO_ITEMS: un
        # recorrido independiente por cada ítem de una colección (rama ITEM) y, opcionalmente, actividades
        # generales concurrentes (rama GENERAL).
        GRUPO_ACTIVIDADES = "GRUPO_ACTIVIDADES", "Actividades en paralelo"
        RECORRIDO_ITEMS = "RECORRIDO_ITEMS", "Recorrido por ítem"
        # Fase 3 — coordina una GACETA del Proceso desde el flujo principal: la abre, presenta los ítems y
        # (opcionalmente) espera su resolución. No es el único ingreso de ítems a la gaceta.
        GACETA = "GACETA", "Gaceta"

    class Rama(models.TextChoices):
        NINGUNA = "", "Flujo principal"
        ITEM = "ITEM", "Para cada ítem"
        GENERAL = "GENERAL", "General, en paralelo"

    # Vocabulario V1 que un administrador puede crear: ESPERA queda fuera. Es una regla
    # de dominio (`configuracion_ejecucion.agregar_bloque_operativo`), no una restricción
    # de `choices`, para no impedir cargar registros históricos.
    TIPOS_CONFIGURABLES = (
        "ACTIVIDAD", "ENTREGABLE", "APROBACION", "DECISION", "TAREAS_ELEMENTOS", "GRUPO_ACTIVIDADES",
        "RECORRIDO_ITEMS", "GACETA",
    )
    TIPOS_CONTENEDOR = ("GRUPO_ACTIVIDADES", "RECORRIDO_ITEMS")
    # Qué puede ir DENTRO de un contenedor, por rama. Un recorrido de ítem es una secuencia (actividades,
    # aprobaciones y decisiones); una rama general es una sola actividad o aprobación que corre en paralelo.
    TIPOS_POR_RAMA = {
        "ITEM": ("ACTIVIDAD", "APROBACION", "DECISION"),
        "GENERAL": ("ACTIVIDAD", "APROBACION"),
    }
    # Entregables que una aprobación puede REVISAR (4.E2): los que tienen un contenido que
    # revisar y volver a entregar corregido. Una CONFIRMACION no tiene nada que revisar.
    TIPOS_ENTREGABLE_REVISABLES = ("TEXTO", "ENLACE", "ARCHIVO")

    version = models.ForeignKey(ConfiguracionEjecucionVersion, on_delete=models.CASCADE, related_name="bloques")
    fase = models.ForeignKey("workflows.FaseWorkflow", on_delete=models.PROTECT, related_name="bloques_operativos")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    nombre = models.CharField(max_length=150)
    # Identidad estable (4.B0): referencia de los resultados del bloque
    # (`aprobaciones.<clave>.resultado`). NO es el nombre ni el pk. Única DENTRO de
    # su configuración (`version`), se genera una vez desde el nombre, se copia al
    # clonar la configuración y solo cambia explícitamente mientras es BORRADOR.
    clave = models.SlugField(
        max_length=claves.LARGO_MAXIMO, blank=True, default="", db_index=False,
        validators=[claves.validar_clave],
        help_text="Identificador estable del bloque para referenciar sus resultados.",
    )
    # ENTREGABLE (4.B1): qué entregable del Servicio exige este punto del flujo. Solo
    # REFERENCIA la definición; el contenido vive en el `EntregableTicket` congelado del
    # Ticket. PROTECT: una definición referenciada no se elimina. Es la referencia
    # estable (el pk de una definición no cambia; retirarla es lógico) y es específica de
    # la configuración del Servicio, nunca del Workflow compartido.
    definicion_entregable = models.ForeignKey(
        "catalogo.DefinicionEntregable", on_delete=models.PROTECT, null=True, blank=True,
        related_name="bloques_operativos",
    )
    # APROBACION (4.E2): qué bloque ENTREGABLE revisa, de forma EXPLÍCITA (nunca por ser el
    # bloque anterior). Opcional: vacío = aprobación general. Solo referencia un bloque de la
    # MISMA configuración; el contenido sigue viviendo en el `EntregableTicket` del Ticket
    # (bloque → `definicion_entregable` → entregable congelado). RESTRICT: un entregable
    # revisado no se elimina mientras alguna aprobación lo referencia (salvo que se borre la
    # configuración completa).
    entregable_revisado = models.ForeignKey(
        "self", on_delete=models.RESTRICT, null=True, blank=True, related_name="aprobaciones_que_lo_revisan",
    )
    descripcion = models.TextField(blank=True)
    orden = models.PositiveIntegerField()
    configuracion = models.JSONField(default=dict, blank=True)
    # Fase 2: un bloque CONTENIDO pertenece a un contenedor (GRUPO_ACTIVIDADES / RECORRIDO_ITEMS) de la misma
    # configuración y fase, y nunca forma parte del recorrido principal. `rama` dice cómo se ejecuta: ITEM (la
    # secuencia de cada ítem) o GENERAL (una actividad general en paralelo). Un bloque del flujo principal no
    # tiene contenedor ni rama.
    contenedor = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="contenidos"
    )
    rama = models.CharField(max_length=10, choices=Rama.choices, blank=True, default="")

    class Meta:
        constraints = [
            UniqueConstraint(fields=["version", "fase", "orden"], name="uq_bloqueop_version_fase_orden"),
            models.CheckConstraint(
                condition=Q(contenedor__isnull=True, rama="") | Q(contenedor__isnull=False, rama__in=["ITEM", "GENERAL"]),
                name="ck_bloqueop_contenedor_rama",
            ),
            UniqueConstraint(
                fields=["version", "clave"], condition=~Q(clave=""), name="uq_bloqueop_version_clave"
            ),
        ]
        ordering = ["version_id", "fase_id", "orden", "id"]

    def clean(self):
        if self.version_id is None or self.fase_id is None:
            return
        if self.fase.version_id != self.version.workflow_version_id:
            raise ValidationError("El bloque debe pertenecer a una fase de la version de Workflow configurada.")
        self._validar_definicion_entregable()
        self._validar_entregable_revisado()
        self._validar_tareas_elementos()
        self._validar_contenedor()
        if self.clave:
            repetida = BloqueOperativo.objects.filter(version_id=self.version_id, clave=self.clave).exclude(pk=self.pk)
            if repetida.exists():
                raise ValidationError({"clave": "Ya existe otro bloque con esa clave en esta configuracion."})

    def _validar_tareas_elementos(self):
        """4.G2D2 — TAREAS_ELEMENTOS solo existe en un Proceso y a lo sumo una vez por configuración (una
        segunda visita a ESE bloque en un ciclo reutiliza las mismas tareas, nunca otro bloque). Que la
        configuración de tareas por elemento del Proceso esté activa se exige al agregarlo y al publicar."""
        if self.tipo != self.Tipo.TAREAS_ELEMENTOS:
            return
        servicio = self.version.servicio
        if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
            raise ValidationError("Las tareas por elemento solo existen en un proceso.")
        if BloqueOperativo.objects.filter(version_id=self.version_id, tipo=self.tipo).exclude(pk=self.pk).exists():
            raise ValidationError("El flujo ya tiene un bloque de tareas por elemento.")

    def _validar_contenedor(self):
        """Fase 2 — un contenedor está en el flujo principal (nunca dentro de otro: un solo nivel de
        subinstancias); un bloque contenido es de la misma configuración y fase que su contenedor, de un tipo
        admitido por su rama, y la rama ITEM solo existe en un RECORRIDO_ITEMS."""
        if self.tipo in (self.Tipo.RECORRIDO_ITEMS, self.Tipo.GACETA):
            servicio = self.version.servicio
            if servicio.tipo != "PROCESO" or servicio.es_ticket_general:
                raise ValidationError("Los recorridos por ítem y las gacetas solo existen en un proceso.")
        if self.contenedor_id is None:
            if self.rama:
                raise ValidationError({"rama": "Solo un bloque dentro de un contenedor tiene rama."})
            return
        contenedor = self.contenedor
        if self.tipo in self.TIPOS_CONTENEDOR:
            raise ValidationError("Un contenedor no puede ir dentro de otro contenedor.")
        if contenedor.tipo not in self.TIPOS_CONTENEDOR:
            raise ValidationError({"contenedor": "Solo un grupo de actividades o un recorrido por ítem contiene bloques."})
        if contenedor.version_id != self.version_id or contenedor.fase_id != self.fase_id:
            raise ValidationError({"contenedor": "El bloque debe estar en la misma configuración y fase que su contenedor."})
        if self.rama not in self.TIPOS_POR_RAMA:
            raise ValidationError({"rama": "Indica si el bloque es para cada ítem o general."})
        if self.rama == self.Rama.ITEM and contenedor.tipo != self.Tipo.RECORRIDO_ITEMS:
            raise ValidationError({"rama": "Un grupo de actividades solo tiene actividades generales."})
        if self.tipo not in self.TIPOS_POR_RAMA[self.rama]:
            raise ValidationError(
                f"«{self.get_tipo_display()}» no se puede agregar como {self.get_rama_display().lower()}."
            )

    @property
    def es_contenido(self):
        return self.contenedor_id is not None

    def _validar_definicion_entregable(self):
        if self.tipo == self.Tipo.ENTREGABLE:
            if self.definicion_entregable_id is None:
                raise ValidationError({"definicion_entregable": "Seleccione el entregable que requiere este bloque."})
            if self.definicion_entregable.servicio_id != self.version.servicio_id:
                raise ValidationError({"definicion_entregable": "El entregable no pertenece a este servicio."})
        elif self.definicion_entregable_id is not None:
            raise ValidationError({"definicion_entregable": "Solo un bloque de tipo entregable referencia un entregable."})

    def _validar_entregable_revisado(self):
        """4.E2 — la relación Aprobación → Entregable es opcional y, si existe, inequívoca:
        un bloque ENTREGABLE de ESTA configuración, con una definición válida del Servicio."""
        if self.tipo != self.Tipo.APROBACION:
            if self.entregable_revisado_id is not None:
                raise ValidationError({"entregable_revisado": "Solo una aprobación puede revisar un entregable."})
            return
        if self.entregable_revisado_id is None:
            return
        revisado = self.entregable_revisado
        if revisado.tipo != self.Tipo.ENTREGABLE:
            raise ValidationError({"entregable_revisado": "Una aprobación solo puede revisar un bloque de entregable."})
        if revisado.version_id != self.version_id:
            raise ValidationError(
                {"entregable_revisado": "El entregable revisado debe pertenecer a la misma configuración."}
            )
        definicion = revisado.definicion_entregable
        if definicion is None or definicion.servicio_id != self.version.servicio_id:
            raise ValidationError(
                {"entregable_revisado": "El bloque de entregable no tiene un entregable válido de este servicio."}
            )
        if definicion.tipo not in self.TIPOS_ENTREGABLE_REVISABLES:
            raise ValidationError(
                {"entregable_revisado": "Solo se puede revisar un entregable de texto, enlace o archivo."}
            )

    def _asegurar_clave(self):
        """Genera la clave desde el nombre SOLO si no tiene. Renombrar el bloque no la cambia."""
        if self.clave:
            return False
        existentes = (
            BloqueOperativo.objects.filter(version_id=self.version_id).exclude(pk=self.pk).values_list("clave", flat=True)
        )
        base = claves.clave_desde_texto(self.nombre, por_defecto=self.tipo.lower() or "bloque")
        self.clave = claves.clave_unica(base, existentes)
        return True

    def save(self, *args, **kwargs):
        self.version.exigir_editable()
        generada = self._asegurar_clave()
        self.clean()
        if generada and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = [*kwargs["update_fields"], "clave"]
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} ({self.get_tipo_display()})"


class TransicionBloqueOperativo(RegistroBase):
    """Ruta entre bloques operativos de una misma configuracion.

    Reusa la semantica existente de `TransicionEtapa`: condiciones para
    DECISION y resultados cerrados para APROBACION. Para recorridos lineales
    simples el runtime puede continuar por orden/fase, pero cualquier ruta
    explicita vive aqui como fuente de verdad editable.
    """

    class Operador(models.TextChoices):
        IGUAL_A = "IGUAL_A", "Igual a"
        DISTINTO_DE = "DISTINTO_DE", "Distinto de"
        CONTIENE = "CONTIENE", "Contiene"
        NO_CONTIENE = "NO_CONTIENE", "No contiene"
        MAYOR_QUE = "MAYOR_QUE", "Mayor que"
        MENOR_QUE = "MENOR_QUE", "Menor que"
        ESTA_VACIO = "ESTA_VACIO", "Esta vacio"
        NO_ESTA_VACIO = "NO_ESTA_VACIO", "No esta vacio"

    bloque_origen = models.ForeignKey(BloqueOperativo, on_delete=models.CASCADE, related_name="transiciones_salientes")
    # 4.E2: una ruta termina en un bloque (`bloque_destino`) O finaliza el flujo
    # (`finaliza=True`, sin destino). Son excluyentes y la restricción de BD lo impone: un
    # destino vacío SIN `finaliza` es un error de configuración, nunca un fin implícito.
    bloque_destino = models.ForeignKey(
        BloqueOperativo, on_delete=models.CASCADE, null=True, blank=True, related_name="transiciones_entrantes"
    )
    finaliza = models.BooleanField(default=False)
    nombre = models.CharField(max_length=150, blank=True)
    prioridad = models.PositiveIntegerField(default=0)
    variable = models.CharField(max_length=150, blank=True)
    operador = models.CharField(max_length=20, choices=Operador.choices, blank=True)
    valor = models.CharField(max_length=255, blank=True)
    es_fallback = models.BooleanField(default=False)
    resultado_aprobacion = models.CharField(
        max_length=10,
        choices=[
            ("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta"),
            # Fase 2 — solo en la aprobación de un recorrido por ítem; hoy sin ruta propia (excluir termina el
            # recorrido del ítem), se admite el valor para no cerrar esa puerta.
            ("EXCLUIDA", "Excluida"),
        ],
        blank=True,
    )

    class Meta:
        ordering = ["bloque_origen_id", "prioridad", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(finaliza=True, bloque_destino__isnull=True)
                    | Q(finaliza=False, bloque_destino__isnull=False)
                ),
                name="ck_transbloque_destino_o_finaliza",
            ),
        ]

    def clean(self):
        if self.bloque_origen_id is None:
            return
        if self.finaliza:
            if self.bloque_destino_id is not None:
                raise ValidationError("Una ruta que finaliza el flujo no tiene bloque destino.")
        else:
            if self.bloque_destino_id is None:
                raise ValidationError("Indique el bloque destino de la ruta o finalice el flujo.")
            if self.bloque_origen.version_id != self.bloque_destino.version_id:
                raise ValidationError("Los bloques conectados deben pertenecer a la misma configuracion.")
            # Fase 2: una ruta no sale ni entra de un contenedor: dentro de un recorrido solo se conecta con
            # bloques de la misma rama del mismo contenedor; en el flujo principal, con bloques principales.
            origen, destino = self.bloque_origen, self.bloque_destino
            if (origen.contenedor_id, origen.rama) != (destino.contenedor_id, destino.rama):
                raise ValidationError(
                    "Una ruta solo conecta bloques del mismo recorrido (o del flujo principal entre sí)."
                )
        tipo = self.bloque_origen.tipo
        if tipo == BloqueOperativo.Tipo.DECISION:
            if self.es_fallback:
                if self.variable or self.operador or self.valor:
                    raise ValidationError("La ruta fallback de una decision no debe tener expresion.")
            elif not (self.variable and self.operador and self.valor):
                raise ValidationError("Una ruta condicional requiere variable, operador y valor.")
            if self.resultado_aprobacion:
                raise ValidationError("resultado_aprobacion no aplica a una decision.")
        elif tipo == BloqueOperativo.Tipo.APROBACION:
            if self.es_fallback or self.variable or self.operador or self.valor:
                raise ValidationError("Una ruta de aprobacion no admite expresion condicional.")
            if not self.resultado_aprobacion:
                raise ValidationError("Una ruta de aprobacion requiere resultado_aprobacion.")
        else:
            if self.es_fallback or self.variable or self.operador or self.valor or self.resultado_aprobacion:
                raise ValidationError("Las reglas de ruta solo aplican a decision o aprobacion.")

    def save(self, *args, **kwargs):
        self.bloque_origen.version.exigir_editable()
        self.clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.bloque_origen.version.exigir_editable()
        super().delete(*args, **kwargs)

    @property
    def etapa_destino(self):
        return self.bloque_destino

    @property
    def etapa_origen_id(self):
        return self.bloque_origen_id

    def __str__(self):
        etiqueta = f" [{self.nombre}]" if self.nombre else ""
        destino = "Finalizar flujo" if self.finaliza else self.bloque_destino
        return f"{self.bloque_origen} -> {destino}{etiqueta}"


class PosicionVisualBloque(RegistroBase):
    """Fase 4.B — POSICIÓN VISUAL de una actividad en el lienzo del Studio. Solo presentación: el motor NUNCA la
    lee (el orden de ejecución es `BloqueOperativo.orden` y las rutas explícitas). Pertenece al Servicio y se
    identifica por la CLAVE ESTABLE del bloque (no por su pk), así sobrevive a las versiones de la configuración y
    a la reconciliación con otra versión de la plantilla. `y` es el desplazamiento vertical dentro de la columna
    de su fase; `x` queda reservado (hoy 0: las columnas fijan la horizontal)."""

    servicio = models.ForeignKey("catalogo.Servicio", on_delete=models.CASCADE, related_name="posiciones_lienzo")
    clave = models.CharField(max_length=claves.LARGO_MAXIMO + 10)
    x = models.IntegerField(default=0)
    y = models.IntegerField(default=0)

    class Meta:
        constraints = [UniqueConstraint(fields=["servicio", "clave"], name="uq_posicion_lienzo_servicio_clave")]

    def __str__(self):
        return f"{self.servicio_id}:{self.clave} ({self.x}, {self.y})"
