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
    # Fase 2 — en un proceso MANUAL: los datos de inicio pueden completarse dentro de la ejecución (guardar
    # avance / completar diligenciamiento). Un proceso programado siempre funciona así.
    datos_inicio_en_ejecucion = forms.BooleanField(
        required=False, label="Los datos de inicio se completan durante la ejecución",
    )

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


class ParrillaForm(forms.Form):
    """4.G2B1 — cómo se llama cada elemento de la estructura repetible de un Proceso."""

    nombre_elemento = forms.CharField(
        max_length=60, label="Nombre de cada elemento", initial="Elemento",
        help_text="Por ejemplo «Publicación» o «Actividad».",
    )
    permite_altas_extraordinarias = forms.BooleanField(
        required=False, label="Después de cerrar el alcance, permitir altas extraordinarias justificadas",
    )


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
            "EQUIPO": ("solo_activos",),
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

# 4.G2D2 — único bloque extra: solo se ofrece en un Proceso que ya configuró sus tareas por elemento.
TIPO_BLOQUE_TAREAS_ELEMENTOS = ("TAREAS_ELEMENTOS", "Tareas por elemento")

# El flujo clásico (Etapas: Flujos reutilizables y servicios legacy) no tiene bloque
# ENTREGABLE: un Flujo no pertenece a un Servicio y no tiene entregables que elegir.
TIPOS_BLOQUE_LEGACY_CHOICES = [opcion for opcion in TIPOS_BLOQUE_CHOICES if opcion[0] != "ENTREGABLE"]

TIPOS_BLOQUE_DESCRIPCION = {
    "ACTIVIDAD": "Trabajo que debe realizar una persona o equipo.",
    "ENTREGABLE": "Un resultado definido del ticket debe estar completo para continuar.",
    "APROBACION": "Una persona o equipo debe revisar y tomar una decisión.",
    "DECISION": "El camino depende de una condición.",
    "TAREAS_ELEMENTOS": "Cada elemento del ticket se convierte en una tarea; el flujo continúa cuando todas se completan.",
    "GRUPO_ACTIVIDADES": "Varias actividades que se hacen al mismo tiempo; el flujo continúa cuando terminan todas.",
    "RECORRIDO_ITEMS": "Cada ítem de una colección recorre sus propias actividades y aprobaciones, a su ritmo.",
    "GACETA": "Abre una gaceta del proceso, presenta sus ítems y, si quieres, espera a que se resuelva.",
}
TIPO_BLOQUE_GACETA = ("GACETA", "Gaceta")

# Fase 2 — contenedores. «Actividades en paralelo» se ofrece en cualquier configuración por fases;
# «Recorrido por ítem», solo en un Proceso con colecciones de ítems.
TIPO_BLOQUE_GRUPO = ("GRUPO_ACTIVIDADES", "Actividades en paralelo")
TIPO_BLOQUE_RECORRIDO = ("RECORRIDO_ITEMS", "Recorrido por ítem")
# Lo que se puede agregar DENTRO de un contenedor, por rama (el modelo lo vuelve a validar).
TIPOS_CONTENIDOS_CHOICES = {
    "ITEM": [("ACTIVIDAD", "Actividad"), ("APROBACION", "Aprobación"), ("DECISION", "Decisión")],
    "GENERAL": [("ACTIVIDAD", "Actividad"), ("APROBACION", "Aprobación")],
}


class BloqueGeneralForm(forms.Form):
    tipo = forms.ChoiceField(
        choices=[
            *TIPOS_BLOQUE_CHOICES, TIPO_BLOQUE_TAREAS_ELEMENTOS, TIPO_BLOQUE_GRUPO, TIPO_BLOQUE_RECORRIDO,
            ("GACETA", "Gaceta"),
        ]
    )
    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Descripción")


class BloqueEditarForm(forms.Form):
    """Solo metadatos — el tipo no se puede cambiar desde el Studio (ver
    `apps.workflows.editor.cambiar_tipo_etapa`: queda como capacidad del
    editor técnico, no se expone aquí para no complicar V1)."""

    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Descripción")


# Fase 4.A — en el orden de uso habitual; los valores (contrato) no cambian.
ACTOR_CHOICES_OBLIGATORIO = [
    ("USUARIO", "Una persona"),
    ("EQUIPO", "Un equipo"),
    ("RESPONSABLE_TICKET", "El responsable del ticket"),
    ("SOLICITANTE", "Quien solicitó el ticket"),
]
# Fase 2 — solo se ofrece cuando el proceso tiene columnas de persona o equipo en sus colecciones; el
# servidor exige además que el bloque esté en un recorrido por ítem de esa colección. Fase 4.A: opción avanzada.
ACTOR_COLUMNA = ("COLUMNA", "Avanzado: la persona o el equipo de una columna del ítem")


def _con_columna(choices, columnas):
    return [*choices, ACTOR_COLUMNA] if columnas else list(choices)


def _sin_solicitante(choices, quitar, actual):
    """Fase 4.A — un proceso programado no tiene solicitante: la opción no se ofrece (el servidor ya lo rechaza al
    publicar), salvo que sea el valor actual de una configuración guardada."""
    if not quitar or actual == "SOLICITANTE":
        return choices
    return [c for c in choices if c[0] != "SOLICITANTE"]


UNIDAD_PLAZO_CHOICES = [("DIAS", "días"), ("HORAS", "horas")]


class ActividadConfigForm(forms.Form):
    # Sin opción "sin asignar": `apps.catalogo.ejecucion._configurar_actividad`
    # exige un actor real (fijo o dinámico) al crear/reconfigurar una
    # ACTIVIDAD desde Studio — a diferencia del editor técnico, que sí
    # tolera un candidato vacío. No se puede relajar esa regla desde aquí
    # (vive en `ejecucion.py`, fuera de alcance de 4.4), así que el Form
    # jamás ofrece un valor que el dominio rechazaría.
    tipo_actor = forms.ChoiceField(choices=ACTOR_CHOICES_OBLIGATORIO, label="¿Quién la realiza?")
    usuario = forms.ModelChoiceField(queryset=Usuario.objects.filter(is_active=True), required=False, label="Persona")
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.filter(activo=True), required=False, label="Equipo")
    permite_subtareas = forms.BooleanField(required=False, label="Permite dividirla en subtareas")
    # Fase 4.A — guía de la experiencia (NO se guarda): de dónde sale la fecha. Al guardar descarta lo que no
    # corresponde al modo elegido; sin ella (clientes anteriores) el formulario se comporta como antes.
    MODOS_FECHA = [("SIN_FECHA", "Sin fecha"), ("PLAZO", "Plazo desde que llega la actividad"), ("DATO", "Fecha tomada de un dato")]
    modo_fechas = forms.ChoiceField(choices=MODOS_FECHA, required=False, label="Fechas", widget=forms.RadioSelect)
    # Fase 1 — solo la configuración por fases los guarda (`apps.catalogo.actividades`).
    requiere_evidencia = forms.BooleanField(required=False, label="Exige evidencia para completarse")
    plazo_previsto_cantidad = forms.IntegerField(
        required=False, min_value=1, max_value=999, label="Fecha prevista: a los"
    )
    plazo_previsto_unidad = forms.ChoiceField(choices=UNIDAD_PLAZO_CHOICES, required=False, initial="DIAS")
    plazo_limite_cantidad = forms.IntegerField(required=False, min_value=1, max_value=999, label="Fecha límite: a los")
    plazo_limite_unidad = forms.ChoiceField(choices=UNIDAD_PLAZO_CHOICES, required=False, initial="DIAS")
    plazo_habiles = forms.BooleanField(required=False, label="Contar solo días y horas hábiles")
    verificacion_obligatoria = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3}),
        label="Lista de verificación obligatoria (un ítem por línea)",
    )
    verificacion_opcional = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}),
        label="Lista de verificación opcional (un ítem por línea)",
    )
    # Fase 2 — responsable desde una columna del ítem, fechas tomadas de un dato y exigencia de datos de inicio.
    columna = forms.ChoiceField(required=False, choices=[("", "— Elige la columna —")], label="Columna del ítem")
    fecha_prevista_campo = forms.ChoiceField(
        required=False, choices=[("", "— Usar el plazo —")], label="Fecha prevista tomada de",
    )
    fecha_limite_campo = forms.ChoiceField(
        required=False, choices=[("", "— Usar el plazo —")], label="Fecha límite tomada de",
    )
    exige_datos_inicio = forms.BooleanField(
        required=False, label="Para completarla, los datos de inicio deben estar completos",
    )
    # Bloque 3.1 — solo en una actividad «para cada ítem»: qué columnas de SU ítem edita quien la realiza.
    columnas_editables = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple,
        label="Columnas del ítem que puede modificar quien realiza la actividad",
    )

    def __init__(self, *args, columnas_actor=(), referencias_fecha=(), sin_solicitante=False, columnas_editables=(), **kwargs):
        """`columnas_actor`: `[(clave, etiqueta)]` de columnas de persona/equipo de las colecciones;
        `referencias_fecha`: `[(referencia, etiqueta)]` de campos y columnas con fecha. Vacíos (camino LEGACY o
        servicio sin esos datos): no se ofrecen esas opciones. `sin_solicitante` (Fase 4.A, proceso programado):
        no se ofrece «Quien solicitó el ticket», salvo que ya sea el valor guardado (para poder verlo y cambiarlo)."""
        super().__init__(*args, **kwargs)
        self.fields["tipo_actor"].choices = _sin_solicitante(
            _con_columna(ACTOR_CHOICES_OBLIGATORIO, columnas_actor), sin_solicitante, (self.initial or {}).get("tipo_actor"),
        )
        self.fields["columna"].choices = [("", "— Elige la columna —"), *columnas_actor]
        for nombre in ("fecha_prevista_campo", "fecha_limite_campo"):
            self.fields[nombre].choices = [("", "— Usar el plazo —"), *referencias_fecha]
        self.fields["columnas_editables"].choices = list(columnas_editables)

    @staticmethod
    def modo_fechas_de(inicial):
        """Modo de fechas que corresponde a una configuración ya guardada (valores iniciales del formulario)."""
        if (inicial or {}).get("fecha_prevista_campo") or (inicial or {}).get("fecha_limite_campo"):
            return "DATO"
        if (inicial or {}).get("plazo_previsto_cantidad") or (inicial or {}).get("plazo_limite_cantidad"):
            return "PLAZO"
        return "SIN_FECHA"

    def ampliacion(self):
        """Parte ampliada de la configuración (fechas, evidencia, verificación) en el contrato JSON de
        `apps.catalogo.actividades`. Llamar solo tras `is_valid()`."""
        datos = dict(self.cleaned_data)
        modo = datos.get("modo_fechas")
        if modo in ("SIN_FECHA", "DATO"):
            datos["plazo_previsto_cantidad"] = datos["plazo_limite_cantidad"] = None
        if modo in ("SIN_FECHA", "PLAZO"):
            datos["fecha_prevista_campo"] = datos["fecha_limite_campo"] = ""

        def plazo(prefijo):
            cantidad = datos.get(f"{prefijo}_cantidad")
            return {"cantidad": cantidad, "unidad": datos.get(f"{prefijo}_unidad") or "DIAS"} if cantidad else None

        def lineas(campo, obligatorio):
            return [
                {"texto": linea.strip(), "obligatorio": obligatorio}
                for linea in (datos.get(campo) or "").splitlines()
                if linea.strip()
            ]

        ampliacion = {
            "requiere_evidencia": bool(datos.get("requiere_evidencia")),
            "plazo_habiles": bool(datos.get("plazo_habiles")),
            "plazo_previsto": plazo("plazo_previsto"),
            "plazo_limite": plazo("plazo_limite"),
            "verificacion": lineas("verificacion_obligatoria", True) + lineas("verificacion_opcional", False),
        }
        # Fase 2 — solo se guardan si se usan (las configuraciones sin ellas no cambian).
        for nombre in ("fecha_prevista_campo", "fecha_limite_campo"):
            if datos.get(nombre):
                ampliacion[nombre] = datos[nombre]
        if datos.get("exige_datos_inicio"):
            ampliacion["exige_datos_inicio"] = True
        if datos.get("columnas_editables"):
            ampliacion["columnas_editables"] = list(datos["columnas_editables"])
        return ampliacion

    def clean(self):
        datos = super().clean()
        tipo_actor = datos.get("tipo_actor") or ""
        if tipo_actor == "USUARIO" and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario responsable.")
        if tipo_actor == "EQUIPO" and not datos.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo responsable.")
        if tipo_actor == "COLUMNA" and not datos.get("columna"):
            raise forms.ValidationError("Seleccione la columna del ítem de la que sale el responsable.")
        if not self.errors:
            from apps.catalogo.actividades import errores_de_actividad

            for error in errores_de_actividad(self.ampliacion()):
                self.add_error(None, error)
        return datos


class EntregableConfigForm(forms.Form):
    """Configuración de un bloque ENTREGABLE: qué entregable DEFINIDO del Servicio exige.
    Solo ofrece las definiciones activas de ese Servicio (más la que el bloque ya usa, para
    poder mostrarla aunque haya sido retirada)."""

    definicion = forms.ModelChoiceField(
        queryset=DefinicionEntregable.objects.none(), label="Entregable requerido",
        empty_label="— Elige un entregable —",
    )

    def __init__(self, *args, servicio=None, incluir_pk=None, revision=None, **kwargs):
        """Fase 4.A — `revision=True` solo ofrece revisiones de elementos; `False`, solo resultados simples (las
        dos opciones de «¿Qué necesitas que suceda?» que terminan en un bloque ENTREGABLE)."""
        super().__init__(*args, **kwargs)
        if servicio is not None:
            consulta = servicio.definiciones_entregables.all()
            filtro = Q(activo=True) | Q(pk=incluir_pk) if incluir_pk else Q(activo=True)
            consulta = consulta.filter(filtro)
            if revision is True:
                consulta = consulta.filter(tipo=DefinicionEntregable.Tipo.ELEMENTOS)
            elif revision is False:
                consulta = consulta.exclude(tipo=DefinicionEntregable.Tipo.ELEMENTOS)
            self.fields["definicion"].queryset = consulta


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
        label="Orden de las aprobaciones",
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

    # Fase 2 — decisiones permitidas y justificación.
    permite_exclusion = forms.BooleanField(
        required=False, label="Permitir excluir el ítem del alcance (solo en el recorrido de un ítem)",
    )
    justificacion_al_aprobar = forms.BooleanField(
        required=False, label="Exigir justificación también al aprobar",
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
    columna = forms.ChoiceField(required=False, choices=[("", "— Columna —")])

    def __init__(self, *args, columnas_actor=(), sin_solicitante=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tipo"].choices = _sin_solicitante(
            _con_columna(ACTOR_CHOICES_OBLIGATORIO, columnas_actor), sin_solicitante, (self.initial or {}).get("tipo"),
        )
        self.fields["columna"].choices = [("", "— Columna —"), *columnas_actor]

    def clean(self):
        datos = super().clean()
        tipo = datos.get("tipo")
        if tipo == "USUARIO" and not datos.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario.")
        if tipo == "EQUIPO" and not datos.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo.")
        if tipo == "COLUMNA" and not datos.get("columna"):
            raise forms.ValidationError("Seleccione la columna del ítem.")
        return datos


ParticipanteAprobacionFormSet = forms.formset_factory(
    ParticipanteAprobacionForm, extra=1, can_delete=True, min_num=1, validate_min=True
)


class RutaAprobacionForm(forms.Form):
    # Fase 4.A — `permite_vacios=True` (alta de una aprobación): cada ruta puede quedar «Definir después», igual
    # que se podía crearla sin rutas; las que se elijan se crean junto con el bloque.
    """Las 3 rutas de una etapa APROBACION — una por `resultado_aprobacion`.

    Fase 2 — `en_recorrido=True` (aprobación del recorrido de un ítem): «si aprueba» es opcional (sin ella
    sigue la secuencia del recorrido), «si devuelve» es obligatoria y no hay ruta de rechazo: rechazar o
    excluir termina el recorrido de ESE ítem."""

    destino_aprobada = forms.ChoiceField(label="Si aprueba, ir a")
    destino_devuelta = forms.ChoiceField(label="Si devuelve, ir a", required=False)
    destino_rechazada = forms.ChoiceField(label="Si rechaza, ir a")

    def __init__(self, *args, destinos, en_recorrido=False, permite_vacios=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.en_recorrido = en_recorrido
        self.fields["destino_aprobada"].choices = destinos
        self.fields["destino_devuelta"].choices = [("", "— (no definida)")] + list(destinos)
        self.fields["destino_rechazada"].choices = destinos
        if en_recorrido:
            self.fields["destino_aprobada"].required = False
            self.fields["destino_aprobada"].choices = [("", "— Seguir con el siguiente paso del ítem —")] + list(destinos)
            self.fields["destino_devuelta"].required = True
            self.fields["destino_devuelta"].choices = list(destinos)
            del self.fields["destino_rechazada"]
        if permite_vacios:
            for campo in self.fields.values():
                campo.required = False
                if not campo.choices or campo.choices[0][0] != "":
                    campo.choices = [("", "— Definir después —")] + list(campo.choices)


class RecorridoConfigForm(forms.Form):
    """Fase 2 — configuración de un RECORRIDO POR ÍTEM: qué colección recorre, qué ítems entran (condición
    opcional sobre una columna), si incorpora ítems agregados después y si espera el cierre del alcance. El
    servidor valida que la columna sea de la colección elegida."""

    coleccion = forms.ChoiceField(label="Colección que se recorre")
    condicion_variable = forms.ChoiceField(required=False, label="Solo entran los ítems cuya columna")
    condicion_operador = forms.ChoiceField(
        required=False, choices=[("", "—"), *TransicionEtapa.Operador.choices], label="Operador",
    )
    condicion_valor = forms.CharField(required=False, max_length=255, label="Valor")
    incorporar_nuevos = forms.BooleanField(
        required=False, label="Los ítems agregados después también hacen su recorrido",
    )
    espera_alcance_cerrado = forms.BooleanField(
        required=False, label="No continuar hasta que se cierre el alcance de la colección",
    )

    def __init__(self, *args, colecciones=(), columnas=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["coleccion"].choices = list(colecciones)
        self.fields["condicion_variable"].choices = [("", "— Todos los ítems —"), *columnas]

    def clean(self):
        datos = super().clean()
        if datos.get("condicion_variable") and not datos.get("condicion_operador"):
            raise forms.ValidationError("Elige el operador de la condición de ingreso.")
        return datos

    def configuracion(self):
        datos = self.cleaned_data
        cfg = {
            "coleccion": datos["coleccion"],
            "incorporar_nuevos": bool(datos.get("incorporar_nuevos")),
            "espera_alcance_cerrado": bool(datos.get("espera_alcance_cerrado")),
        }
        if datos.get("condicion_variable"):
            cfg["condicion_ingreso"] = {
                "variable": datos["condicion_variable"], "operador": datos["condicion_operador"],
                "valor": datos.get("condicion_valor") or "",
            }
        return cfg


class CierreAlcanceForm(forms.Form):
    """Fase 2 — al llegar a este bloque, cerrar el alcance de las colecciones elegidas (cualquier bloque)."""

    cierra_alcance = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple,
        label="Al llegar a este bloque, cerrar el alcance de",
    )

    def __init__(self, *args, colecciones=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cierra_alcance"].choices = list(colecciones)


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
    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Descripción")
    tipo = forms.ChoiceField(choices=DefinicionEntregable.Tipo.choices, label="Tipo de resultado")
    obligatorio = forms.BooleanField(required=False, label="Obligatorio para resolver")
    orden = forms.IntegerField(initial=0, min_value=0, label="Orden")
    # 4.G2C1 — solo para «Revisión de elementos»; el dominio exige columnas y revisores en ese tipo
    # (`catalogo.revision_elementos.validar_configuracion`) y los ignora en los demás.
    columnas_revisadas = forms.MultipleChoiceField(
        required=False, choices=[], label="Columnas que se revisan",
        widget=forms.CheckboxSelectMultiple,
    )
    revisores_usuarios = forms.ModelMultipleChoiceField(
        queryset=Usuario.objects.filter(is_active=True).order_by("username"), required=False,
        label="Revisan (personas)", widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )
    revisores_equipos = forms.ModelMultipleChoiceField(
        queryset=Equipo.objects.filter(activo=True).order_by("nombre"), required=False, label="Revisan (equipos)",
        widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )

    def __init__(self, *args, servicio=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.catalogo.revision_elementos import columnas_disponibles

        columnas = columnas_disponibles(servicio) if servicio is not None else []
        self.fields["columnas_revisadas"].choices = [(c.clave, c.etiqueta) for c in columnas]
        self.hay_estructura = bool(columnas)
        # Fase 4.A: la revisión de elementos solo existe en un Proceso: un Servicio no la ofrece.
        if servicio is not None and (servicio.tipo != "PROCESO" or servicio.es_ticket_general):
            self.fields["tipo"].choices = [c for c in self.fields["tipo"].choices if c[0] != DefinicionEntregable.Tipo.ELEMENTOS]


class TerminoServicioForm(forms.Form):
    """4.D — un término de búsqueda de un Servicio/Proceso. Las reglas de dominio
    (palabra concreta, sin equivalentes, tope) las valida `apps.catalogo.terminos_busqueda`."""

    termino = forms.CharField(max_length=TerminoServicio.LARGO_MAXIMO, label="Término")


class PoliticaEntregaForm(forms.Form):
    """4.5 — política de entrega (Studio → Salida). Solo las dos políticas V1;
    ninguna deja el Ticket esperando indefinidamente."""

    # Fase 4.A: la entrega formal se puede retirar (vuelve a «se resuelve y cierra manualmente»).
    SIN_ENTREGA = "SIN_ENTREGA"
    politica = forms.ChoiceField(
        choices=[("", "— Seleccione —")] + list(Servicio.PoliticaEntrega.choices)
        + [(SIN_ENTREGA, "Sin entrega formal: el ticket se resuelve y cierra manualmente")],
        label="Al entregar el resultado",
    )
    dias_observacion = forms.IntegerField(
        required=False, min_value=1, max_value=90, label="Días para que el solicitante responda"
    )

    def clean(self):
        datos = super().clean()
        if datos.get("politica") == self.SIN_ENTREGA:
            datos["politica"] = ""
            datos["dias_observacion"] = None
            return datos
        if datos.get("politica") == Servicio.PoliticaEntrega.PERIODO_OBSERVACIONES and not datos.get("dias_observacion"):
            raise forms.ValidationError("Indique cuántos días tiene el solicitante para responder (1 a 90).")
        return datos


# --- Fase 3: gacetas, reglas de finalización y bloque GACETA ------------------------------------------------

INGRESO_GACETA_CHOICES = [
    ("MANUAL", "Solo cuando alguien lo presenta (o el flujo)"),
    ("AL_CUMPLIR_CONDICION", "Automáticamente, cuando el ítem cumple la condición"),
    ("AL_TERMINAR_RECORRIDO", "Automáticamente, al terminar su recorrido"),
]
APERTURA_GACETA_CHOICES = [("AL_INICIAR", "Al iniciar el ticket"), ("POR_FLUJO", "Cuando el flujo llega a su bloque")]
PENDIENTES_GACETA_CHOICES = [
    ("IMPEDIR", "No se puede resolver con ítems pendientes"),
    ("EXCLUIR", "Los pendientes se excluyen del alcance"),
    ("RECHAZAR", "Los pendientes se rechazan"),
]
RESULTADOS_ITEM_CHOICES = [
    ("APROBADO", "Aprobado en sus gacetas"), ("COMPLETADO", "Completó su recorrido (sin gaceta)"),
    ("RECHAZADO", "Rechazado"), ("EXCLUIDO", "Excluido"),
]


def _plazo_de(datos, prefijo):
    cantidad = datos.get(f"{prefijo}_cantidad")
    if not cantidad:
        return None
    return {"cantidad": cantidad, "unidad": datos.get(f"{prefijo}_unidad") or "DIAS",
            "habiles": bool(datos.get(f"{prefijo}_habiles"))}


class DefinicionGacetaForm(forms.Form):
    """Fase 3 — una gaceta del Proceso, sin JSON: el servidor arma la configuración (`configuracion()`) y la
    valida contra el Proceso (`apps.catalogo.gacetas`)."""

    nombre = forms.CharField(max_length=150)
    descripcion = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    colecciones = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple, label="Colecciones que participan")
    condicion_variable = forms.ChoiceField(required=False, label="Ingresan solo los ítems cuya columna")
    condicion_operador = forms.ChoiceField(
        required=False, choices=[("", "—"), *TransicionEtapa.Operador.choices], label="Operador",
    )
    condicion_valor = forms.CharField(required=False, max_length=255, label="Valor")
    ingreso = forms.ChoiceField(choices=INGRESO_GACETA_CHOICES, label="¿Cómo ingresan los ítems?")
    apertura = forms.ChoiceField(choices=APERTURA_GACETA_CHOICES, label="¿Cuándo se abre?")
    exige_item_completo = forms.BooleanField(required=False, initial=True, label="Solo ingresan ítems completos")
    revisores_usuarios = forms.ModelMultipleChoiceField(
        queryset=Usuario.objects.filter(is_active=True), required=False, label="Revisores (personas)",
        widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )
    revisores_equipos = forms.ModelMultipleChoiceField(
        queryset=Equipo.objects.filter(activo=True), required=False, label="Revisores (equipos)",
        widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )
    aprobadores_usuarios = forms.ModelMultipleChoiceField(
        queryset=Usuario.objects.filter(is_active=True), required=False, label="Aprobadores (personas)",
        widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )
    aprobadores_equipos = forms.ModelMultipleChoiceField(
        queryset=Equipo.objects.filter(activo=True), required=False, label="Aprobadores (equipos)",
        widget=forms.SelectMultiple(attrs={"data-multiselect": ""}),
    )
    politica = forms.ChoiceField(
        choices=[("TODOS", "Todos deben aprobar"), ("CUALQUIERA", "Cualquiera aprueba")],
        label="Con varios aprobadores",
    )
    plazo_revision_cantidad = forms.IntegerField(required=False, min_value=1, max_value=999, label="Periodo de revisión")
    plazo_revision_unidad = forms.ChoiceField(choices=UNIDAD_PLAZO_CHOICES, required=False)
    plazo_revision_habiles = forms.BooleanField(required=False, label="hábiles")
    plazo_correccion_cantidad = forms.IntegerField(required=False, min_value=1, max_value=999, label="Plazo de corrección")
    plazo_correccion_unidad = forms.ChoiceField(choices=UNIDAD_PLAZO_CHOICES, required=False)
    plazo_correccion_habiles = forms.BooleanField(required=False, label="hábiles")
    aceptacion_automatica = forms.BooleanField(
        required=False, label="Al vencer el periodo, aceptar los ítems en revisión sin observaciones pendientes",
    )
    permite_exclusion = forms.BooleanField(required=False, label="Los aprobadores pueden excluir un ítem")
    justificacion_al_aprobar = forms.BooleanField(required=False, label="Exigir justificación también al aprobar")
    al_resolver_pendientes = forms.ChoiceField(choices=PENDIENTES_GACETA_CHOICES, label="Al resolver la gaceta")
    resolucion_automatica = forms.BooleanField(
        required=False, label="Resolver sola al cerrar el periodo, cuando se pueda",
    )
    responsable_correccion_columna = forms.ChoiceField(
        required=False, label="La corrección la recibe",
    )
    # Fase 4.A — coordinador propio (opcional) y obligatoriedad para finalizar.
    coordinador_usuario = forms.ModelChoiceField(
        queryset=Usuario.objects.filter(is_active=True), required=False, label="Coordina (persona)",
    )
    coordinador_equipo = forms.ModelChoiceField(
        queryset=Equipo.objects.filter(activo=True), required=False, label="o coordina (equipo)",
    )
    obligatoria = forms.BooleanField(
        required=False, label="Obligatoria: el ticket no se puede resolver si la gaceta no se abrió y resolvió",
    )
    # Bloque 3.1 — qué columnas del ítem edita quien recibe la tarea de corrección.
    columnas_editables = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple,
        label="Columnas del ítem que puede modificar quien corrige",
    )

    def __init__(self, *args, colecciones=(), columnas=(), columnas_actor=(), columnas_editables=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["columnas_editables"].choices = list(columnas_editables)
        self.fields["colecciones"].choices = list(colecciones)
        self.fields["condicion_variable"].choices = [("", "— Todos los ítems —"), *columnas]
        self.fields["responsable_correccion_columna"].choices = [
            ("", "El responsable del ticket"), *[(clave, f"La persona o equipo de «{etiqueta}»") for clave, etiqueta in columnas_actor],
        ]

    def clean(self):
        datos = super().clean()
        if datos.get("condicion_variable") and not datos.get("condicion_operador"):
            raise forms.ValidationError("Elige el operador de la condición de ingreso.")
        if datos.get("coordinador_usuario") and datos.get("coordinador_equipo"):
            self.add_error("coordinador_equipo", "Elige una persona o un equipo coordinador, no ambos.")
        return datos

    def configuracion(self):
        datos = self.cleaned_data
        coordinador = None
        if datos.get("coordinador_usuario"):
            coordinador = {"tipo": "USUARIO", "usuario_id": datos["coordinador_usuario"].pk}
        elif datos.get("coordinador_equipo"):
            coordinador = {"tipo": "EQUIPO", "equipo_id": datos["coordinador_equipo"].pk}
        condicion = None
        if datos.get("condicion_variable"):
            condicion = {
                "variable": datos["condicion_variable"], "operador": datos["condicion_operador"],
                "valor": datos.get("condicion_valor") or "",
            }
        return {
            "colecciones": list(datos.get("colecciones") or []),
            "condicion_ingreso": condicion,
            "ingreso": datos["ingreso"],
            "apertura": datos["apertura"],
            "exige_item_completo": bool(datos.get("exige_item_completo")),
            "revisores": [{"tipo": "USUARIO", "usuario_id": u.pk} for u in datos.get("revisores_usuarios") or []]
            + [{"tipo": "EQUIPO", "equipo_id": e.pk} for e in datos.get("revisores_equipos") or []],
            "aprobadores": [{"tipo": "USUARIO", "usuario_id": u.pk} for u in datos.get("aprobadores_usuarios") or []]
            + [{"tipo": "EQUIPO", "equipo_id": e.pk} for e in datos.get("aprobadores_equipos") or []],
            "politica": datos["politica"],
            "plazo_revision": _plazo_de(datos, "plazo_revision"),
            "plazo_correccion": _plazo_de(datos, "plazo_correccion"),
            "aceptacion_automatica": bool(datos.get("aceptacion_automatica")),
            "permite_exclusion": bool(datos.get("permite_exclusion")),
            "justificacion_al_aprobar": bool(datos.get("justificacion_al_aprobar")),
            "al_resolver_pendientes": datos["al_resolver_pendientes"],
            "resolucion_automatica": bool(datos.get("resolucion_automatica")),
            "responsable_correccion_columna": datos.get("responsable_correccion_columna") or "",
            "coordinador": coordinador,
            "obligatoria": bool(datos.get("obligatoria")),
            "columnas_editables": list(datos.get("columnas_editables") or []),
        }

    @staticmethod
    def inicial(definicion):
        cfg = definicion.configuracion or {}
        condicion = cfg.get("condicion_ingreso") or {}
        revisa, prueba = cfg.get("plazo_revision") or {}, cfg.get("plazo_correccion") or {}
        actores = lambda lista, tipo, llave: [a[llave] for a in cfg.get(lista) or [] if a.get("tipo") == tipo]  # noqa: E731
        return {
            "nombre": definicion.nombre, "descripcion": definicion.descripcion,
            "colecciones": cfg.get("colecciones") or [],
            "condicion_variable": condicion.get("variable") or "", "condicion_operador": condicion.get("operador") or "",
            "condicion_valor": condicion.get("valor") or "",
            "ingreso": cfg.get("ingreso", "MANUAL"), "apertura": cfg.get("apertura", "AL_INICIAR"),
            "exige_item_completo": cfg.get("exige_item_completo", True),
            "revisores_usuarios": actores("revisores", "USUARIO", "usuario_id"),
            "revisores_equipos": actores("revisores", "EQUIPO", "equipo_id"),
            "aprobadores_usuarios": actores("aprobadores", "USUARIO", "usuario_id"),
            "aprobadores_equipos": actores("aprobadores", "EQUIPO", "equipo_id"),
            "politica": cfg.get("politica", "TODOS"),
            "plazo_revision_cantidad": revisa.get("cantidad"), "plazo_revision_unidad": revisa.get("unidad") or "DIAS",
            "plazo_revision_habiles": revisa.get("habiles", False),
            "plazo_correccion_cantidad": prueba.get("cantidad"), "plazo_correccion_unidad": prueba.get("unidad") or "DIAS",
            "plazo_correccion_habiles": prueba.get("habiles", False),
            "aceptacion_automatica": cfg.get("aceptacion_automatica", False),
            "permite_exclusion": cfg.get("permite_exclusion", False),
            "justificacion_al_aprobar": cfg.get("justificacion_al_aprobar", False),
            "al_resolver_pendientes": cfg.get("al_resolver_pendientes", "IMPEDIR"),
            "resolucion_automatica": cfg.get("resolucion_automatica", False),
            "responsable_correccion_columna": cfg.get("responsable_correccion_columna") or "",
            "coordinador_usuario": (cfg.get("coordinador") or {}).get("usuario_id"),
            "coordinador_equipo": (cfg.get("coordinador") or {}).get("equipo_id"),
            "obligatoria": cfg.get("obligatoria", False),
            "columnas_editables": cfg.get("columnas_editables") or [],
        }


class ReglasFinalizacionForm(forms.Form):
    actividades_cerradas = forms.BooleanField(required=False, label="No quedan actividades abiertas")
    recorridos_terminados = forms.BooleanField(required=False, label="Los recorridos por ítem terminaron")
    aprobaciones_resueltas = forms.BooleanField(required=False, label="No quedan aprobaciones pendientes")
    gacetas_resueltas = forms.BooleanField(required=False, initial=True, label="Las gacetas están resueltas")
    sin_correcciones = forms.BooleanField(required=False, initial=True, label="No quedan correcciones abiertas")
    alcance_cerrado = forms.BooleanField(required=False, label="El alcance de las colecciones está cerrado")
    items_con_resultado = forms.BooleanField(required=False, label="Cada ítem tiene un resultado final admitido")
    resultados_admitidos = forms.MultipleChoiceField(
        choices=RESULTADOS_ITEM_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        label="Resultados finales admitidos",
    )
    minimo_aprobados = forms.IntegerField(required=False, min_value=0, initial=0, label="Mínimo de ítems aprobados")

    def configuracion(self):
        datos = dict(self.cleaned_data)
        datos["resultados_admitidos"] = list(datos.get("resultados_admitidos") or [])
        datos["minimo_aprobados"] = datos.get("minimo_aprobados") or 0
        return datos


class GacetaBloqueForm(forms.Form):
    gaceta = forms.ChoiceField(label="Gaceta que coordina")
    presentar_items = forms.BooleanField(
        required=False, initial=True, label="Al llegar, presentar los ítems que cumplan sus requisitos",
    )
    esperar = forms.ChoiceField(
        choices=[("RESOLUCION", "Esperar a que la gaceta se resuelva"), ("NINGUNA", "Continuar sin esperar")],
        label="El flujo",
    )

    def __init__(self, *args, gacetas=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["gaceta"].choices = list(gacetas)

    def configuracion(self):
        datos = self.cleaned_data
        return {"gaceta": datos["gaceta"], "presentar_items": bool(datos.get("presentar_items")), "esperar": datos["esperar"]}
