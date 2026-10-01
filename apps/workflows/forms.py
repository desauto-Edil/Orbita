"""Formularios de Workflows — 3.UI.3 (metadatos del contenedor `Workflow`)
+ 3.UI.4 (editor de Etapas/Transiciones de una `WorkflowVersion` BORRADOR).

Responsabilidad estrictamente de entrada HTTP (presencia/formato/tipos) —
`apps.workflows.versionamiento`/`apps.workflows.editor` siguen siendo la
única autoridad de dominio; ningún `clean()` de aquí reimplementa
`validar_integridad_transicion`/`validar_estructura`/las Strategies, solo
ofrece una validación de UX barata antes del viaje de red (la operación de
dominio vuelve a validar siempre, ver `apps/workflows/editor.py`).

3.UI.4 mantiene deliberadamente `TransicionForm` único (no una subclase
por tipo de etapa origen) — la vista decide qué subconjunto de campos
mostrar/habilitar según `etapa_origen.tipo`, y la normalización/rechazo
real de campos incompatibles vive en `apps.workflows.editor.
_normalizar_campos_transicion` + `validar_integridad_transicion`, nunca
aquí ni en JavaScript."""

from django import forms
from django.contrib.auth import get_user_model

from apps.core.models import Equipo
from apps.workflows.editor import TIPOS_NO_DISPONIBLES_EN_EDITOR
from apps.workflows.models import ConfiguracionEtapaAprobacion, ConfiguracionEtapaTarea, Etapa, ParticipanteEtapaAprobacion, TransicionEtapa

Usuario = get_user_model()


class WorkflowForm(forms.Form):
    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea, label="Descripción")


# --- 3.UI.4 — Etapa ------------------------------------------------------

# Decisión R2: TICKET/GACETA aparecen en el selector (el usuario debe saber
# que existen como tipos futuros) pero con la etiqueta marcada — el rechazo
# real de todas formas ocurre en `clean_tipo()` y, con más autoridad, en
# `apps.workflows.editor.crear_etapa`/`cambiar_tipo_etapa` (nunca solo
# `disabled` HTML).
_CHOICES_TIPO_ETAPA = [
    (valor, f"{etiqueta} — No disponible todavía" if valor in TIPOS_NO_DISPONIBLES_EN_EDITOR else etiqueta)
    for valor, etiqueta in Etapa.Tipo.choices
]


class EtapaForm(forms.Form):
    tipo = forms.ChoiceField(choices=_CHOICES_TIPO_ETAPA, label="Tipo")
    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea, label="Descripción")

    def clean_tipo(self):
        tipo = self.cleaned_data["tipo"]
        if tipo in TIPOS_NO_DISPONIBLES_EN_EDITOR:
            raise forms.ValidationError("Este tipo todavía no está disponible para crear desde este editor.")
        return tipo


class EtapaEdicionForm(forms.Form):
    """Solo metadatos comunes — nunca `tipo` (ver `CambiarTipoEtapaForm`,
    acción separada y deliberadamente más cautelosa)."""

    nombre = forms.CharField(max_length=150, label="Nombre")
    descripcion = forms.CharField(required=False, widget=forms.Textarea, label="Descripción")


class CambiarTipoEtapaForm(forms.Form):
    tipo = forms.ChoiceField(choices=_CHOICES_TIPO_ETAPA, label="Nuevo tipo")

    def clean_tipo(self):
        tipo = self.cleaned_data["tipo"]
        if tipo in TIPOS_NO_DISPONIBLES_EN_EDITOR:
            raise forms.ValidationError("Este tipo todavía no está disponible desde este editor.")
        return tipo


# --- 3.UI.4 — Configuración TAREA ---------------------------------------


class ConfiguracionTareaForm(forms.Form):
    tipo_responsable = forms.ChoiceField(
        choices=[("", "Sin responsable")] + list(ConfiguracionEtapaTarea.TipoResponsable.choices),
        required=False,
        label="Tipo de responsable",
    )
    usuario_responsable = forms.ModelChoiceField(queryset=Usuario.objects.none(), required=False, label="Usuario")
    equipo_responsable = forms.ModelChoiceField(queryset=Equipo.objects.none(), required=False, label="Equipo")
    permite_subtareas = forms.BooleanField(required=False, label="Permite subtareas")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usuario_responsable"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        self.fields["equipo_responsable"].queryset = Equipo.objects.filter(activo=True).order_by("nombre")

    def clean(self):
        cleaned = super().clean()
        tipo_responsable = cleaned.get("tipo_responsable") or ""
        if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.USUARIO and not cleaned.get(
            "usuario_responsable"
        ):
            raise forms.ValidationError("Seleccione un usuario responsable.")
        if tipo_responsable == ConfiguracionEtapaTarea.TipoResponsable.EQUIPO and not cleaned.get(
            "equipo_responsable"
        ):
            raise forms.ValidationError("Seleccione un equipo responsable.")
        cleaned["tipo_responsable"] = tipo_responsable
        return cleaned


# --- 3.UI.4 — Configuración APROBACION ----------------------------------


class ConfiguracionAprobacionForm(forms.Form):
    modo = forms.ChoiceField(choices=ConfiguracionEtapaAprobacion.Modo.choices, label="Modo")
    politica = forms.ChoiceField(
        choices=[("", "—")] + list(ConfiguracionEtapaAprobacion.Politica.choices), required=False, label="Política"
    )

    def clean(self):
        cleaned = super().clean()
        modo = cleaned.get("modo")
        politica = cleaned.get("politica") or ""
        if modo == ConfiguracionEtapaAprobacion.Modo.PARALELA and not politica:
            raise forms.ValidationError("modo=PARALELA requiere una política (TODOS/CUALQUIERA).")
        if modo == ConfiguracionEtapaAprobacion.Modo.SECUENCIAL and politica:
            raise forms.ValidationError("modo=SECUENCIAL no admite política de cierre propia.")
        cleaned["politica"] = politica
        return cleaned


class ParticipanteAprobacionForm(forms.Form):
    tipo_aprobador = forms.ChoiceField(choices=ParticipanteEtapaAprobacion.TipoAprobador.choices, label="Tipo")
    usuario = forms.ModelChoiceField(queryset=Usuario.objects.none(), required=False, label="Usuario")
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.none(), required=False, label="Equipo")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usuario"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        self.fields["equipo"].queryset = Equipo.objects.filter(activo=True).order_by("nombre")

    def clean(self):
        cleaned = super().clean()
        tipo_aprobador = cleaned.get("tipo_aprobador")
        if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.USUARIO and not cleaned.get("usuario"):
            raise forms.ValidationError("Seleccione un usuario.")
        if tipo_aprobador == ParticipanteEtapaAprobacion.TipoAprobador.EQUIPO and not cleaned.get("equipo"):
            raise forms.ValidationError("Seleccione un equipo.")
        return cleaned


ParticipanteAprobacionFormSet = forms.formset_factory(
    ParticipanteAprobacionForm, extra=1, can_delete=True, min_num=1, validate_min=True
)


# --- 3.UI.4 — Configuración ESPERA --------------------------------------


class ConfiguracionEsperaForm(forms.Form):
    """Campos normales — nunca un `<textarea>` de JSON libre. La operación
    de dominio (`apps.workflows.editor.configurar_etapa_espera`) transforma
    esto al dict mínimo que `EstrategiaEspera` espera, sin conservar claves
    residuales del modo anterior."""

    MODOS = [("DURACION", "Duración"), ("FECHA", "Fecha objetivo")]
    UNIDADES = [("DIAS", "Días"), ("HORAS", "Horas")]

    modo = forms.ChoiceField(choices=MODOS, label="Modo")
    duracion_valor = forms.IntegerField(required=False, min_value=1, label="Duración")
    duracion_unidad = forms.ChoiceField(choices=UNIDADES, required=False, label="Unidad")
    fecha_objetivo = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="Fecha objetivo"
    )

    def clean(self):
        cleaned = super().clean()
        modo = cleaned.get("modo")
        if modo == "DURACION":
            if not cleaned.get("duracion_valor") or not cleaned.get("duracion_unidad"):
                raise forms.ValidationError("Indique duración y unidad.")
        elif modo == "FECHA":
            if not cleaned.get("fecha_objetivo"):
                raise forms.ValidationError("Indique la fecha objetivo.")
        return cleaned


# --- 3.UI.4 — Transición -------------------------------------------------


class TransicionForm(forms.Form):
    etapa_destino = forms.ModelChoiceField(queryset=Etapa.objects.none(), label="Etapa destino")
    nombre = forms.CharField(max_length=150, required=False, label="Nombre (opcional)")
    prioridad = forms.IntegerField(required=False, min_value=0, initial=0, label="Prioridad")
    variable = forms.CharField(max_length=150, required=False, label="Variable (CONDICION)")
    operador = forms.ChoiceField(
        choices=[("", "—")] + list(TransicionEtapa.Operador.choices), required=False, label="Operador (CONDICION)"
    )
    valor = forms.CharField(max_length=255, required=False, label="Valor (CONDICION)")
    es_fallback = forms.BooleanField(required=False, label="Salida por defecto / fallback (CONDICION)")
    resultado_aprobacion = forms.ChoiceField(
        choices=[("", "—"), ("APROBADA", "Aprobada"), ("RECHAZADA", "Rechazada"), ("DEVUELTA", "Devuelta")],
        required=False,
        label="Resultado (APROBACION)",
    )

    def __init__(self, *args, etapas_destino_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if etapas_destino_queryset is not None:
            self.fields["etapa_destino"].queryset = etapas_destino_queryset
