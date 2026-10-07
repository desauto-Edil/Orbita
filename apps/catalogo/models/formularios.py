"""Formulario configurable — capacidad transversal de captura de
información (CU-012/013, RQF-038 a RQF-047, RN-012/013).

`Formulario` es una PLANTILLA, conceptualmente equivalente a un formulario
de Google Forms: define campos, opciones y reglas, pero no sabe quién la
usa. **No tiene FK hacia `Servicio`, `Ticket` ni `Proceso`** — es el
consumidor quien referencia la plantilla (hoy `Servicio.formulario`, ver
`apps/catalogo/models/catalogo.py`; en el futuro, únicamente cuando su
sprint lo exija, p. ej. `Proceso.formulario`). Vive dentro de
`apps.catalogo` en 1.2 para no crear una app nueva prematuramente, pero es
una capacidad transversal, no una propiedad exclusiva del catálogo de
servicios — no asumir lo contrario al extenderla.

`RespuestaFormulario`/`RespuestaCampo` no existen todavía: capturar
respuestas es Sprint 2, fuera de este archivo.

**Inmutabilidad (RN-013)**: una `FormularioVersion` fuera de `BORRADOR` es
estructuralmente inmutable por las vías ordinarias de dominio/Admin —
`Campo`/`OpcionCampo`/`ReglaCondicional` verifican `version.exigir_editable()`
en `save()`/`delete()`. Esto protege las vías ordinarias (formularios de
Admin, `apps/catalogo/versionamiento.py`), no una escritura masiva vía
`QuerySet.update()`/`bulk_update()` (esos métodos no pasan por `save()`) ni
acceso directo a la base de datos — deliberado: 1.2 no construye
infraestructura adicional para blindarse contra un desarrollador/DBA con
acceso directo al ORM, que ya es código/operación de confianza en el resto
del proyecto (`CLAUDE.md`, sección Calidad).
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from apps.catalogo import claves
from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.reglas import validar_composicion_reglas, validar_integridad_regla
from apps.core.models import RegistroBase


class Formulario(RegistroBase):
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    version_activa = models.ForeignKey(
        "FormularioVersion", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return self.nombre


class FormularioVersion(RegistroBase):
    class Estado(models.TextChoices):
        BORRADOR = "BORRADOR", "Borrador"
        ACTIVA = "ACTIVA", "Activa"
        HISTORICA = "HISTORICA", "Histórica"

    # PROTECT (no CASCADE): borrar un Formulario nunca debe destruir en
    # cascada versiones que representan estructura histórica (RN-013) — el
    # mismo razonamiento aplicará a las futuras respuestas que referencien
    # una FormularioVersion en Sprint 2.
    formulario = models.ForeignKey(Formulario, on_delete=models.PROTECT, related_name="versiones")
    numero = models.PositiveIntegerField()
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.BORRADOR)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["formulario", "numero"], name="uq_formularioversion_numero"),
        ]
        ordering = ["formulario_id", "numero"]

    def exigir_editable(self):
        if self.estado != self.Estado.BORRADOR:
            raise ValidationError(
                "Esta versión ya no está en borrador: modificarla alteraría estructura histórica "
                "(RN-013). Cree una nueva versión para hacer cambios."
            )

    def __str__(self):
        return f"{self.formulario} — v{self.numero} ({self.get_estado_display()})"


class Campo(RegistroBase):
    class TipoCampo(models.TextChoices):
        TEXTO = "TEXTO", "Texto"
        TEXTO_LARGO = "TEXTO_LARGO", "Texto largo"
        NUMERO = "NUMERO", "Número"
        FECHA = "FECHA", "Fecha"
        FECHA_HORA = "FECHA_HORA", "Fecha y hora"
        LISTA = "LISTA", "Lista"
        MULTILISTA = "MULTILISTA", "Multilista"
        BOOLEANO = "BOOLEANO", "Booleano"
        ARCHIVO = "ARCHIVO", "Archivo"
        USUARIO = "USUARIO", "Usuario"
        AREA = "AREA", "Área"
        UNIDAD = "UNIDAD", "Unidad de negocio"
        CORREO = "CORREO", "Correo electrónico"
        URL = "URL", "URL"

    version = models.ForeignKey(FormularioVersion, on_delete=models.CASCADE, related_name="campos")
    tipo = models.CharField(max_length=20, choices=TipoCampo.choices)
    etiqueta = models.CharField(max_length=200)
    # Identidad estable (4.B0): es la referencia de los Workflows (`formulario.<clave>`).
    # NO es la etiqueta (cambia) ni el pk (cambia al clonar la versión). Única DENTRO
    # de su versión, se genera una sola vez desde la etiqueta, se copia siempre al
    # versionar y solo se edita mientras la versión es BORRADOR (ver `claves.py`).
    # `blank` solo para filas históricas que aún no la tienen: `save()` la completa.
    clave = models.SlugField(
        max_length=claves.LARGO_MAXIMO, blank=True, default="", db_index=False,
        validators=[claves.validar_clave],
        help_text="Identificador estable del campo para reglas y flujos (p. ej. valor_estimado). "
        "No cambia al renombrar la etiqueta.",
    )
    ayuda = models.TextField(blank=True)
    obligatorio = models.BooleanField(default=False)
    orden = models.PositiveIntegerField(default=0)
    # 4.F2 — marca SEMÁNTICA: este campo es la «fecha requerida por el solicitante». Nunca se
    # infiere del nombre ni del tipo (una fecha de nacimiento o de un evento no es un plazo):
    # la decide quien configura el formulario. Solo FECHA o FECHA_HORA, y como mucho UNO por
    # versión. Se copia al versionar y no depende de la etiqueta.
    es_fecha_requerida = models.BooleanField(
        default=False,
        help_text="Órbita comparará esta fecha con el tiempo objetivo del servicio y avisará al "
        "solicitante si pide un plazo menor al establecido.",
    )
    # Validado por la Strategy del tipo (`campos.py`) — nunca un contenedor
    # libre: claves fuera del esquema de su tipo son rechazadas en clean().
    configuracion = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["orden", "id"]
        constraints = [
            UniqueConstraint(
                fields=["version", "clave"], condition=~Q(clave=""), name="uq_campo_version_clave"
            ),
            UniqueConstraint(
                fields=["version"], condition=Q(es_fecha_requerida=True), name="uq_campo_version_fecha_requerida"
            ),
            models.CheckConstraint(
                condition=Q(es_fecha_requerida=False) | Q(tipo__in=["FECHA", "FECHA_HORA"]),
                name="ck_campo_fecha_requerida_tipo",
            ),
        ]

    def clean(self):
        estrategia = ESTRATEGIAS_POR_TIPO.get(self.tipo)
        if estrategia is None:
            raise ValidationError({"tipo": "Tipo de campo no soportado."})
        estrategia.validar_configuracion(self.configuracion or {})
        if self.es_fecha_requerida:
            if self.tipo not in (self.TipoCampo.FECHA, self.TipoCampo.FECHA_HORA):
                raise ValidationError(
                    {"es_fecha_requerida": "Solo un campo de fecha, o de fecha y hora, puede ser la fecha requerida."}
                )
            if self.version_id is not None:
                otra = Campo.objects.filter(version_id=self.version_id, es_fecha_requerida=True).exclude(pk=self.pk).first()
                if otra is not None:
                    raise ValidationError(
                        {"es_fecha_requerida": f"El formulario ya tiene una fecha requerida («{otra.etiqueta}»). "
                         "Solo puede haber una: desmárcala primero."}
                    )
        if self.clave and self.version_id is not None:
            repetida = Campo.objects.filter(version_id=self.version_id, clave=self.clave).exclude(pk=self.pk)
            if repetida.exists():
                raise ValidationError({"clave": "Ya existe otro campo con esa clave en este formulario."})

    def _asegurar_clave(self):
        """Genera la clave desde la etiqueta SOLO si no tiene (campo nuevo o fila
        histórica). Nunca regenera una clave existente: renombrar no cambia la identidad."""
        if self.clave:
            return False
        existentes = Campo.objects.filter(version_id=self.version_id).exclude(pk=self.pk).values_list("clave", flat=True)
        self.clave = claves.generar_clave(self.etiqueta, existentes, por_defecto="campo")
        return True

    def save(self, *args, **kwargs):
        self.version.exigir_editable()
        if self._asegurar_clave() and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = [*kwargs["update_fields"], "clave"]
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return self.etiqueta


class OpcionCampo(RegistroBase):
    """Opciones de campos LISTA/MULTILISTA, administrables sin código
    (RQF-042). Vive y muere con el `BORRADOR` de su `Campo` — no tiene
    `activo` propio, ver docstring del módulo.
    """

    campo = models.ForeignKey(Campo, on_delete=models.CASCADE, related_name="opciones")
    valor = models.CharField(max_length=100)
    etiqueta = models.CharField(max_length=200)
    orden = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [UniqueConstraint(fields=["campo", "valor"], name="uq_opcioncampo_valor")]
        ordering = ["orden", "id"]

    def save(self, *args, **kwargs):
        self.campo.version.exigir_editable()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.campo.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.campo} — {self.etiqueta}"


class ReglaCondicional(RegistroBase):
    """Regla condicional (CU-012, RQF-043) — ver `apps/catalogo/reglas.py`
    (Specification) para su evaluación e integridad.
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

    class Efecto(models.TextChoices):
        MOSTRAR = "MOSTRAR", "Mostrar"
        OCULTAR = "OCULTAR", "Ocultar"
        REQUERIR = "REQUERIR", "Requerir"
        NO_REQUERIR = "NO_REQUERIR", "No requerir"

    campo_origen = models.ForeignKey(Campo, on_delete=models.CASCADE, related_name="reglas_como_origen")
    operador = models.CharField(max_length=20, choices=Operador.choices)
    valor = models.CharField(max_length=255, blank=True)
    campo_objetivo = models.ForeignKey(Campo, on_delete=models.CASCADE, related_name="reglas_como_objetivo")
    efecto = models.CharField(max_length=20, choices=Efecto.choices)

    def clean(self):
        validar_integridad_regla(self)
        validar_composicion_reglas(self)

    def save(self, *args, **kwargs):
        self.campo_origen.version.exigir_editable()
        # 2.2: impide configuraciones contradictorias (MOSTRAR+OCULTAR,
        # REQUERIR+NO_REQUERIR sobre el mismo objetivo) desde el momento en
        # que se configura la regla, no solo al radicar — ver reglas.py.
        validar_composicion_reglas(self)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.campo_origen.version.exigir_editable()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Si {self.campo_origen} {self.operador} {self.valor!r} → {self.efecto} {self.campo_objetivo}"
