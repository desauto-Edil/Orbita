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
from django.db.models import UniqueConstraint

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
    ayuda = models.TextField(blank=True)
    obligatorio = models.BooleanField(default=False)
    orden = models.PositiveIntegerField(default=0)
    # Validado por la Strategy del tipo (`campos.py`) — nunca un contenedor
    # libre: claves fuera del esquema de su tipo son rechazadas en clean().
    configuracion = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["orden", "id"]

    def clean(self):
        estrategia = ESTRATEGIAS_POR_TIPO.get(self.tipo)
        if estrategia is None:
            raise ValidationError({"tipo": "Tipo de campo no soportado."})
        estrategia.validar_configuracion(self.configuracion or {})

    def save(self, *args, **kwargs):
        self.version.exigir_editable()
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
