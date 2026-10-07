"""Formularios del Studio (4.4) — superficie empresarial única para
configurar Servicio/Proceso.

Ninguno de estos Forms sustituye a la operación de dominio como autoridad:
cada vista vuelve a invocar `full_clean()`/la operación real antes de
guardar (mismo criterio que el resto del proyecto, ver `apps/workflows/
forms.py`) — aquí solo se valida forma/UX, nunca la regla de negocio final.
"""

from django import forms
from django.db.models import Q

from apps.catalogo.models import (
    Campo,
    Categoria,
    DefinicionEntregable,
    Formulario,
    OpcionCampo,
    ProgramacionProceso,
    ReglaCondicional,
    Servicio,
    ServicioResponsable,
    ServicioVisibilidad,
    TerminoServicio,
)
from apps.core.models import Area, Equipo, UnidadNegocio, Usuario
from apps.workflows.models import ConfiguracionEtapaAprobacion, TransicionEtapa

# --- GENERAL -------------------------------------------------------------


class ServicioGeneralForm(forms.ModelForm):
    class Meta:
        model = Servicio
        fields = ["nombre", "descripcion", "categoria", "tipo", "instrucciones", "alcance_visibilidad"]
        widgets = {
            "descripcion": forms.Textarea(attrs={"rows": 3}),
            "instrucciones": forms.Textarea(attrs={"rows": 3}),
        }


class TiempoObjetivoForm(forms.Form):
    """4.A1 — tiempo objetivo de atención (Studio → Básico). Vacío = sin
    compromiso temporal. Solo valida forma/UX: la regla final vive en
    `operaciones.configurar_tiempo_objetivo`."""

    cantidad = forms.IntegerField(required=False, min_value=1, max_value=999, label="Tiempo objetivo")
    unidad = forms.ChoiceField(
        required=False, choices=[("", "—")] + list(Servicio.UnidadTiempo.choices), label="Unidad"
    )
    habiles = forms.BooleanField(required=False, label="Solo cuentan días hábiles (lunes a viernes)")

    def clean(self):
        datos = super().clean()
        cantidad, unidad = datos.get("cantidad"), datos.get("unidad")
        if cantidad and not unidad:
            raise forms.ValidationError("Seleccione la unidad del tiempo objetivo.")
        if unidad and not cantidad:
            raise forms.ValidationError("Indique la cantidad del tiempo objetivo.")
        return datos


class PoliticaProrrogaForm(forms.Form):
    """4.A2 — política de prórroga (Studio → Básico). Solo forma/UX: la regla
    final vive en `operaciones.configurar_politica_prorroga`."""

    politica = forms.ChoiceField(
        choices=[("", "— Seleccione —")] + list(Servicio.PoliticaProrroga.choices), label="Prórroga de la fecha objetivo"
    )
    aprobador_usuario = forms.ModelChoiceField(
        queryset=Usuario.objects.filter(is_active=True).order_by("username"), required=False, label="Aprobador (usuario)"
    )
    aprobador_equipo = forms.ModelChoiceField(
        queryset=Equipo.objects.filter(activo=True).order_by("nombre"), required=False, label="o aprobador (equipo)"
    )

    def clean(self):
        datos = super().clean()
        if datos.get("politica") == Servicio.PoliticaProrroga.CON_APROBACION:
            if bool(datos.get("aprobador_usuario")) == bool(datos.get("aprobador_equipo")):
                raise forms.ValidationError("Con aprobación, indique un aprobador: un usuario o un equipo, no ambos.")
        return datos


class InicioProcesoForm(forms.Form):
    """4.G1 — «¿Cómo inicia este proceso?». Vive dentro del formulario de crear y del de Básico de
    un Proceso (no es una pantalla aparte). Solo valida forma/UX: la regla final (compatibilidad,
    responsable activo, calendario) está en `apps.catalogo.programacion.configurar_programacion`.

    El responsable inicial se elige entre personas y equipos activos (al crear un Proceso todavía
    no hay responsables configurados). Al guardar, `programacion.asegurar_responsable_inicial` lo
    deja como responsable del proceso si aún no lo era. `cleaned_data["responsable_inicial"]` es la
    pareja `(tipo_responsable, usuario | equipo)`."""

    MANUAL = "MANUAL"
    PROGRAMADO = "PROGRAMADO"

    modo = forms.ChoiceField(
        choices=[(MANUAL, "Manual"), (PROGRAMADO, "Programado")],
        widget=forms.RadioSelect, label="¿Cómo inicia este proceso?", required=False,
    )
    frecuencia = forms.ChoiceField(
        choices=ProgramacionProceso.Frecuencia.choices, required=False, label="Frecuencia"
    )
    dia_creacion = forms.IntegerField(
        required=False, min_value=1, max_value=28, label="Crear ejecución el día"
    )
    periodo = forms.ChoiceField(
        choices=[("", "— Seleccione —")] + list(ProgramacionProceso.Periodo.choices), required=False, label="Periodo"
    )
    responsable_inicial = forms.ChoiceField(required=False, label="Responsable inicial")

    def __init__(self, *args, **kwargs):
        kwargs.pop("servicio", None)  # compatibilidad: ya no depende del proceso
        super().__init__(*args, **kwargs)
        personas = [
            (f"U:{u.pk}", u.get_full_name() or u.get_username())
            for u in Usuario.objects.filter(is_active=True).order_by("first_name", "last_name", "username")
        ]
        equipos = [(f"E:{e.pk}", e.nombre) for e in Equipo.objects.filter(activo=True).order_by("nombre")]
        self.fields["responsable_inicial"].choices = [
            ("", "— Seleccione —"), ("Personas", personas), ("Equipos", equipos),
        ]

    @staticmethod
    def valor_responsable(responsable):
        """El valor del selector para un `ServicioResponsable` (o `""`)."""
        if responsable is None:
            return ""
        if responsable.tipo_responsable == ServicioResponsable.TipoResponsable.USUARIO:
            return f"U:{responsable.usuario_id}"
        return f"E:{responsable.equipo_id}"

    def clean_modo(self):
        return self.cleaned_data.get("modo") or self.MANUAL

    def clean(self):
        datos = super().clean()
        if datos.get("modo") != self.PROGRAMADO:
            return datos
        if not datos.get("dia_creacion"):
            self.add_error("dia_creacion", "Indique el día del mes (1 a 28) en que se crea la ejecución.")
        if not datos.get("periodo"):
            self.add_error("periodo", "Seleccione si la ejecución cubre el mes actual o el siguiente.")
        valor = datos.get("responsable_inicial") or ""
        tipo, _, pk = valor.partition(":")
        Tipo = ServicioResponsable.TipoResponsable
        responsable = None
        if tipo == "U" and pk.isdigit():
            usuario = Usuario.objects.filter(pk=int(pk), is_active=True).first()
            responsable = (Tipo.USUARIO, usuario) if usuario else None
        elif tipo == "E" and pk.isdigit():
            equipo = Equipo.objects.filter(pk=int(pk), activo=True).first()
            responsable = (Tipo.EQUIPO, equipo) if equipo else None
        if responsable is None:
            self.add_error("responsable_inicial", "Seleccione quién recibe cada ejecución (una persona o un equipo).")
        datos["responsable_inicial"] = responsable
        return datos


class ServicioCreacionForm(forms.Form):
    """4.4.1 — alta mínima de un Servicio/Proceso desde Studio. Solo lo
    necesario para existir como BORRADOR: nada de Workflow, Formulario,
    Entregables, responsables ni publicación (se completan en las pestañas).

    La categoría es obligatoria en el dominio (`Servicio.categoria`, PROTECT):
    se elige una existente o, si el catálogo aún no tiene la adecuada, se
    escribe una nueva — así Studio no obliga a pasar por Django Admin."""

    tipo = forms.ChoiceField(choices=Servicio.Tipo.choices, label="Tipo")
    nombre = forms.CharField(max_length=150, label="Nombre")
    categoria = forms.ModelChoiceField(
        queryset=Categoria.objects.filter(activo=True).order_by("nombre"),
        required=False, label="Categoría", empty_label="— Seleccione —",
    )
    categoria_nueva = forms.CharField(max_length=150, required=False, label="o escriba una categoría nueva")
    descripcion = forms.CharField(required=False, label="Descripción", widget=forms.Textarea(attrs={"rows": 3}))
    instrucciones = forms.CharField(
        required=False, label="Instrucciones para quien solicita", widget=forms.Textarea(attrs={"rows": 3})
    )

    def clean_nombre(self):
        return self.cleaned_data["nombre"].strip()

    def clean(self):
        datos = super().clean()
        nueva = (datos.get("categoria_nueva") or "").strip()
        datos["categoria_nueva"] = nueva
        if datos.get("categoria") and nueva:
            raise forms.ValidationError("Elija una categoría de la lista o escriba una nueva, no ambas.")
        if not datos.get("categoria") and not nueva:
            raise forms.ValidationError("Indique la categoría.")
        return datos


class VisibilidadForm(forms.Form):
    """Concesión de visibilidad: los tres alcances reales de
    `ServicioVisibilidad` (usuario, área, unidad de negocio)."""

    tipo_alcance = forms.ChoiceField(choices=ServicioVisibilidad.TipoAlcance.choices, label="Conceder a")
    usuario = forms.ModelChoiceField(
        queryset=Usuario.objects.filter(is_active=True).order_by("username"), required=False, label="Usuario"
    )
    area = forms.ModelChoiceField(queryset=Area.objects.filter(activo=True).order_by("nombre"), required=False, label="Área")
    unidad_negocio = forms.ModelChoiceField(
        queryset=UnidadNegocio.objects.filter(activo=True).order_by("nombre"), required=False, label="Unidad de negocio"
    )

    def clean(self):
        datos = super().clean()
        tipo = datos.get("tipo_alcance")
        if tipo == ServicioVisibilidad.TipoAlcance.USUARIO and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario.")
        if tipo == ServicioVisibilidad.TipoAlcance.AREA and not datos.get("area"):
            raise forms.ValidationError("Seleccione un área.")
        if tipo == ServicioVisibilidad.TipoAlcance.UNIDAD and not datos.get("unidad_negocio"):
            raise forms.ValidationError("Seleccione una unidad de negocio.")
        return datos


class ResponsableForm(forms.Form):
    """Quién puede atender: los dos tipos reales de `ServicioResponsable`
    (usuario o equipo). Sin semántica de "líder de equipo"."""

    tipo_responsable = forms.ChoiceField(choices=ServicioResponsable.TipoResponsable.choices, label="Agregar")
    usuario = forms.ModelChoiceField(
        queryset=Usuario.objects.filter(is_active=True).order_by("username"), required=False, label="Usuario"
    )
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.filter(activo=True).order_by("nombre"), required=False, label="Equipo")

    def clean(self):
        datos = super().clean()
        tipo = datos.get("tipo_responsable")
        if tipo == ServicioResponsable.TipoResponsable.USUARIO and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario.")
        if tipo == ServicioResponsable.TipoResponsable.EQUIPO and not datos.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo.")
        return datos


# --- ENTRADA (Form Builder) ----------------------------------------------


class FormularioForm(forms.ModelForm):
    """Solo para la primera creación — la plantilla en sí (nombre/descripción),
    distinta de sus versiones/campos."""

    class Meta:
        model = Formulario
        fields = ["nombre", "descripcion"]
        widgets = {"descripcion": forms.Textarea(attrs={"rows": 2})}


class CampoForm(forms.ModelForm):
    """Expone los parámetros reales de cada Strategy (`apps/catalogo/campos.py`)
    como campos simples, siempre visibles — sin un editor JSON crudo
    (`configuracion` sigue siendo la única fuente de verdad, armada por la
    vista a partir de estos campos según el `tipo` elegido, nunca reinventada
    aquí). V1 no distingue visualmente qué campos aplican a cada tipo —
    documentado como simplificación aceptada en el informe de entrega."""

    longitud_maxima = forms.IntegerField(required=False, min_value=1, label="Longitud máxima")
    minimo = forms.FloatField(required=False, label="Valor mínimo")
    maximo = forms.FloatField(required=False, label="Valor máximo")
    permite_decimales = forms.BooleanField(required=False, label="Permite decimales")
    fecha_minima = forms.DateField(required=False, label="Fecha mínima")
    fecha_maxima = forms.DateField(required=False, label="Fecha máxima")
    minimo_selecciones = forms.IntegerField(required=False, min_value=0, label="Mínimo de selecciones")
    maximo_selecciones = forms.IntegerField(required=False, min_value=0, label="Máximo de selecciones")
    extensiones_permitidas = forms.CharField(
        required=False, label="Extensiones permitidas",
        help_text="Separadas por coma, ej: pdf,jpg,png",
    )
    tamano_maximo_mb = forms.FloatField(required=False, min_value=0, label="Tamaño máximo (MB)")
    solo_activos = forms.BooleanField(required=False, initial=True, label="Solo registros activos")

    class Meta:
        model = Campo
        fields = ["tipo", "etiqueta", "clave", "ayuda", "obligatorio", "orden", "es_fecha_requerida"]
        widgets = {"ayuda": forms.Textarea(attrs={"rows": 2})}
        labels = {"clave": "Clave (para flujos)", "es_fecha_requerida": "Usar como fecha requerida por el solicitante"}
        help_texts = {
            "clave": "Identificador estable que usan las decisiones de un flujo "
            "(formulario.<clave>). Vacío: se genera desde la etiqueta y no cambia al renombrarla."
        }

    def clean_clave(self):
        """Vacío al crear = generarla desde la etiqueta (`Campo.save`); vacío al
        editar = conservar la actual (nunca cambiar la identidad por omisión)."""
        clave = (self.cleaned_data.get("clave") or "").strip().lower().replace("-", "_")
        if not clave and self.instance.pk:
            return self.instance.clave
        return clave

    def configuracion_desde_tipo(self, tipo):
        """Arma el dict que la Strategy del tipo elegido realmente admite —
        nunca inventa claves fuera de `claves_configuracion`."""
        datos = self.cleaned_data
        mapas = {
            "TEXTO": ("longitud_maxima",),
            "TEXTO_LARGO": ("longitud_maxima",),
            "CORREO": ("longitud_maxima",),
            "URL": ("longitud_maxima",),
            "NUMERO": ("permite_decimales", "minimo", "maximo"),
            "FECHA": ("fecha_minima", "fecha_maxima"),
            "FECHA_HORA": ("fecha_minima", "fecha_maxima"),
            "MULTILISTA": ("minimo_selecciones", "maximo_selecciones"),
            "ARCHIVO": ("extensiones_permitidas", "tamano_maximo_mb"),
            "USUARIO": ("solo_activos",),
            "AREA": ("solo_activos",),
            "UNIDAD": ("solo_activos",),
        }
        claves = mapas.get(tipo, ())
        configuracion = {}
        for clave in claves:
            valor = datos.get(clave)
            if clave == "extensiones_permitidas" and valor:
                valor = [parte.strip() for parte in valor.split(",") if parte.strip()]
            if clave == "fecha_minima" and valor:
                valor = valor.isoformat()
            if clave == "fecha_maxima" and valor:
                valor = valor.isoformat()
            if valor not in (None, "", []):
                configuracion[clave] = valor
        return configuracion


class OpcionCampoForm(forms.ModelForm):
    class Meta:
        model = OpcionCampo
        fields = ["valor", "etiqueta", "orden"]


class ReglaCondicionalForm(forms.ModelForm):
    class Meta:
        model = ReglaCondicional
        fields = ["campo_origen", "operador", "valor", "campo_objetivo", "efecto"]

    def __init__(self, *args, campos_queryset, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["campo_origen"].queryset = campos_queryset
        self.fields["campo_objetivo"].queryset = campos_queryset


# --- EJECUCIÓN (bloques empresariales) -----------------------------------

# Vocabulario V1 (4.B1): ESPERA ya no es un bloque que un administrador pueda crear
# (el estado técnico EN_ESPERA del motor sigue existiendo: lo usan APROBACION y ENTREGABLE).
TIPOS_BLOQUE_CHOICES = [
    ("ACTIVIDAD", "Actividad"),
    ("ENTREGABLE", "Entregable"),
    ("APROBACION", "Aprobación"),
    ("DECISION", "Decisión"),
]

# El flujo clásico (Etapas: Flujos reutilizables y servicios legacy) no tiene bloque
# ENTREGABLE: un Flujo no pertenece a un Servicio y no tiene entregables que elegir.
TIPOS_BLOQUE_LEGACY_CHOICES = [opcion for opcion in TIPOS_BLOQUE_CHOICES if opcion[0] != "ENTREGABLE"]

TIPOS_BLOQUE_DESCRIPCION = {
    "ACTIVIDAD": "Trabajo que debe realizar una persona o equipo.",
    "ENTREGABLE": "Un resultado definido del ticket debe estar completo para continuar.",
    "APROBACION": "Una persona o equipo debe revisar y tomar una decisión.",
    "DECISION": "El camino depende de una condición.",
}


class BloqueGeneralForm(forms.Form):
    tipo = forms.ChoiceField(choices=TIPOS_BLOQUE_CHOICES)
    nombre = forms.CharField(max_length=150)
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class BloqueEditarForm(forms.Form):
    """Solo metadatos — el tipo no se puede cambiar desde el Studio (ver
    `apps.workflows.editor.cambiar_tipo_etapa`: queda como capacidad del
    editor técnico, no se expone aquí para no complicar V1)."""

    nombre = forms.CharField(max_length=150)
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


ACTOR_CHOICES_OBLIGATORIO = [
    ("RESPONSABLE_TICKET", "Responsable actual del Ticket"),
    ("SOLICITANTE", "Solicitante"),
    ("USUARIO", "Usuario específico"),
    ("EQUIPO", "Equipo"),
]


class ActividadConfigForm(forms.Form):
    # Sin opción "sin asignar": `apps.catalogo.ejecucion._configurar_actividad`
    # exige un actor real (fijo o dinámico) al crear/reconfigurar una
    # ACTIVIDAD desde Studio — a diferencia del editor técnico, que sí
    # tolera un candidato vacío. No se puede relajar esa regla desde aquí
    # (vive en `ejecucion.py`, fuera de alcance de 4.4), así que el Form
    # jamás ofrece un valor que el dominio rechazaría.
    tipo_actor = forms.ChoiceField(choices=ACTOR_CHOICES_OBLIGATORIO, label="Responsable")
    usuario = forms.ModelChoiceField(queryset=Usuario.objects.filter(is_active=True), required=False)
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.filter(activo=True), required=False)
    permite_subtareas = forms.BooleanField(required=False, label="Permite subtareas")

    def clean(self):
        datos = super().clean()
        tipo_actor = datos.get("tipo_actor") or ""
        if tipo_actor == "USUARIO" and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario responsable.")
        if tipo_actor == "EQUIPO" and not datos.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo responsable.")
        return datos


class EntregableConfigForm(forms.Form):
    """Configuración de un bloque ENTREGABLE: qué entregable DEFINIDO del Servicio exige.
    Solo ofrece las definiciones activas de ese Servicio (más la que el bloque ya usa, para
    poder mostrarla aunque haya sido retirada)."""

    definicion = forms.ModelChoiceField(
        queryset=DefinicionEntregable.objects.none(), label="Entregable requerido",
        empty_label="— Elige un entregable —",
    )

    def __init__(self, *args, servicio=None, incluir_pk=None, **kwargs):
        super().__init__(*args, **kwargs)
        if servicio is not None:
            consulta = servicio.definiciones_entregables.all()
            filtro = Q(activo=True) | Q(pk=incluir_pk) if incluir_pk else Q(activo=True)
            self.fields["definicion"].queryset = consulta.filter(filtro)


class EsperaConfigForm(forms.Form):  # histórico (4.B1): solo edita una ESPERA ya configurada
    modo = forms.ChoiceField(choices=[("DURACION", "Duración"), ("FECHA", "Fecha específica")])
    duracion_valor = forms.IntegerField(required=False, min_value=1, label="Cantidad")
    duracion_unidad = forms.ChoiceField(
        choices=[("DIAS", "Días"), ("HORAS", "Horas")], required=False, label="Unidad"
    )
    fecha_objetivo = forms.DateField(required=False, label="Fecha objetivo")

    def clean(self):
        datos = super().clean()
        if datos.get("modo") == "DURACION":
            if not datos.get("duracion_valor") or not datos.get("duracion_unidad"):
                raise forms.ValidationError("Indique cantidad y unidad para la duración.")
        elif datos.get("modo") == "FECHA" and not datos.get("fecha_objetivo"):
            raise forms.ValidationError("Indique la fecha objetivo.")
        return datos


MAX_APROBADORES = 10


class AprobacionConfigForm(forms.Form):
    # 4.F1 — configuración PROGRESIVA: primero cuántos aprobadores participan; el modo solo se
    # pregunta con más de uno (con uno no cambia nada). `cantidad` NO se persiste: guía la
    # construcción del formulario y se valida contra los aprobadores recibidos; lo guardado
    # (y la única fuente de verdad) son los participantes. Sin `cantidad` en el envío (flujos
    # del lienzo y clientes anteriores) el formulario se comporta como siempre: modo obligatorio.
    cantidad = forms.IntegerField(
        required=False, min_value=1, max_value=MAX_APROBADORES, initial=1,
        label="¿Cuántos aprobadores participan?",
        widget=forms.NumberInput(attrs={"data-aprobacion-cantidad": "", "min": 1, "max": MAX_APROBADORES}),
    )
    modo = forms.ChoiceField(
        choices=[
            (ConfiguracionEtapaAprobacion.Modo.SECUENCIAL, "Secuencial — uno a la vez, en orden"),
            (ConfiguracionEtapaAprobacion.Modo.PARALELA, "Paralela — todos al mismo tiempo"),
        ],
        required=False,
        label="Modo de aprobación",
    )
    politica = forms.ChoiceField(
        choices=[("", "—")] + list(ConfiguracionEtapaAprobacion.Politica.choices),
        required=False,
        label="¿Cuándo se considera aprobado?",
    )
    # 4.E2 — solo la configuración por fases la usa (un Flujo no tiene entregables de Servicio):
    # qué bloque ENTREGABLE revisa esta aprobación. Vacío = aprobación general.
    revisa = forms.ChoiceField(
        choices=[("", "— Ninguno: aprobación general —")], required=False, label="Revisa el entregable",
    )

    def __init__(self, *args, bloques_entregable=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["revisa"].choices = [("", "— Ninguno: aprobación general —")] + [
            (bloque.pk, f"{bloque.fase.nombre} - {bloque.nombre}") for bloque in bloques_entregable
        ]

    def clean(self):
        datos = super().clean()
        cantidad = datos.get("cantidad")
        if cantidad is not None and cantidad <= 1:
            # Un solo aprobador: no hay orden ni política que decidir. Se usa el comportamiento
            # secuencial, que con un participante es idéntico al de siempre.
            datos["modo"] = ConfiguracionEtapaAprobacion.Modo.SECUENCIAL
            datos["politica"] = ""
            return datos
        if not datos.get("modo"):
            self.add_error("modo", "Seleccione el modo de aprobación.")
        elif datos["modo"] == ConfiguracionEtapaAprobacion.Modo.PARALELA and not datos.get("politica"):
            raise forms.ValidationError("Seleccione la política de aprobación paralela.")
        return datos


class ParticipanteAprobacionForm(forms.Form):
    tipo = forms.ChoiceField(choices=ACTOR_CHOICES_OBLIGATORIO, label="Aprobador")
    usuario = forms.ModelChoiceField(queryset=Usuario.objects.filter(is_active=True), required=False)
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.filter(activo=True), required=False)

    def clean(self):
        datos = super().clean()
        tipo = datos.get("tipo")
        if tipo == "USUARIO" and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario.")
        if tipo == "EQUIPO" and not datos.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo.")
        return datos


ParticipanteAprobacionFormSet = forms.formset_factory(
    ParticipanteAprobacionForm, extra=1, can_delete=True, min_num=1, validate_min=True
)


class RutaAprobacionForm(forms.Form):
    """Las 3 rutas de una etapa APROBACION — una por `resultado_aprobacion`."""

    destino_aprobada = forms.ChoiceField(label="Si aprueba, ir a")
    destino_devuelta = forms.ChoiceField(label="Si devuelve, ir a", required=False)
    destino_rechazada = forms.ChoiceField(label="Si rechaza, ir a")

    def __init__(self, *args, destinos, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destino_aprobada"].choices = destinos
        self.fields["destino_devuelta"].choices = [("", "— (no definida)")] + list(destinos)
        self.fields["destino_rechazada"].choices = destinos


class CondicionalForm(forms.Form):
    variable = forms.CharField(max_length=150, label="Variable")
    operador = forms.ChoiceField(choices=TransicionEtapa.Operador.choices)
    valor = forms.CharField(max_length=255, label="Valor")
    prioridad = forms.IntegerField(initial=0, min_value=0)
    destino = forms.ChoiceField(label="Ir a")

    def __init__(self, *args, destinos, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destino"].choices = destinos
        # Sugerencias (<datalist id="variables-decision"> de Studio): se puede elegir una
        # referencia sin escribir su clave; el campo sigue siendo texto libre.
        self.fields["variable"].widget.attrs["list"] = "variables-decision"


class FallbackForm(forms.Form):
    destino = forms.ChoiceField(label="En otro caso, ir a")

    def __init__(self, *args, destinos, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destino"].choices = destinos


# --- SALIDA (Entregables) -------------------------------------------------


class DefinicionEntregableForm(forms.Form):
    nombre = forms.CharField(max_length=150)
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    tipo = forms.ChoiceField(choices=DefinicionEntregable.Tipo.choices)
    obligatorio = forms.BooleanField(required=False)
    orden = forms.IntegerField(initial=0, min_value=0)


class TerminoServicioForm(forms.Form):
    """4.D — un término de búsqueda de un Servicio/Proceso. Las reglas de dominio
    (palabra concreta, sin equivalentes, tope) las valida `apps.catalogo.terminos_busqueda`."""

    termino = forms.CharField(max_length=TerminoServicio.LARGO_MAXIMO, label="Término")


class PoliticaEntregaForm(forms.Form):
    """4.5 — política de entrega (Studio → Salida). Solo las dos políticas V1;
    ninguna deja el Ticket esperando indefinidamente."""

    politica = forms.ChoiceField(
        choices=[("", "— Seleccione —")] + list(Servicio.PoliticaEntrega.choices), label="Al entregar el resultado"
    )
    dias_observacion = forms.IntegerField(
        required=False, min_value=1, max_value=90, label="Días para que el solicitante responda"
    )

    def clean(self):
        datos = super().clean()
        if datos.get("politica") == Servicio.PoliticaEntrega.PERIODO_OBSERVACIONES and not datos.get("dias_observacion"):
            raise forms.ValidationError("Indique cuántos días tiene el solicitante para responder (1 a 90).")
        return datos
