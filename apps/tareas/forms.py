"""Formularios de Tareas — 3.UI.1.

Primera introducción de Django Forms en el proyecto (decisión U.5 del
diagnóstico de 3.UI, aprobada explícitamente). Responsabilidad
estrictamente de entrada HTTP — presencia/formato/tipos — nunca
autorización, estado, relaciones o reglas funcionales: eso sigue siendo
exclusivo de `apps.tareas.operaciones`/`apps.tareas.autorizacion`, que la
vista vuelve a invocar después de que el Form valida, nunca en su lugar
(un Form válido no es un permiso concedido).

Sin Form para tomar/iniciar/completar: ninguna de las tres recibe datos
más allá de CSRF — un POST simple ya es suficiente (instrucción explícita:
no crear Forms vacíos solo por uniformidad).

Los `queryset` de usuario/equipo se acotan en `__init__` (nunca a nivel de
clase, que los congelaría en tiempo de import) a activos únicamente —
mismo criterio, sin ampliar ni reducir, que ya usa `apps/tickets/views.py`
para sus propios selects de responsable: `apps.tareas.autorizacion` no
documenta ningún alcance Área/Unidad para Tarea (hallazgo explícito de
3.3), así que no se inventa aquí un filtro adicional que el backend no
respalda. El backend vuelve a validar en todos los casos — estas
queryset son una comodidad de UI, nunca la autorización real."""

from django import forms
from django.contrib.auth import get_user_model

from apps.core.models import Equipo

Usuario = get_user_model()

_FORMATOS_FECHA_HORA = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"]


class AsignacionTareaForm(forms.Form):
    usuario = forms.ModelChoiceField(queryset=Usuario.objects.none(), required=False, label="Usuario")
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.none(), required=False, label="Equipo")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usuario"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        self.fields["equipo"].queryset = Equipo.objects.filter(activo=True).order_by("nombre")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("usuario") and not cleaned.get("equipo"):
            raise forms.ValidationError("Indique un usuario y/o un equipo.")
        return cleaned


class ReasignacionTareaForm(AsignacionTareaForm):
    """Mismos campos y misma validación de entrada que
    `AsignacionTareaForm` — la diferencia entre asignar y reasignar es de
    dominio (`puede_asignar_tarea` vs `puede_reasignar_tarea`, y si ya
    existe o no un responsable), no de qué datos HTTP se reciben."""


class DelegacionTareaForm(forms.Form):
    delegado_a = forms.ModelChoiceField(queryset=Usuario.objects.none(), label="Delegar a")
    desde = forms.DateTimeField(label="Desde", input_formats=_FORMATOS_FECHA_HORA)
    hasta = forms.DateTimeField(label="Hasta", input_formats=_FORMATOS_FECHA_HORA)
    motivo = forms.CharField(required=False, widget=forms.Textarea, label="Motivo")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["delegado_a"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        for campo in ("desde", "hasta"):
            self.fields[campo].widget = forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format=_FORMATOS_FECHA_HORA[0]
            )

    # Sin validación de "desde < hasta" aquí a propósito: es una regla de
    # dominio (ya expresada en `DelegacionTarea.clean()` y en el
    # `CheckConstraint` de BD) — duplicarla en el Form sería reimplementar
    # una regla funcional, exactamente lo que la instrucción prohíbe. El
    # Form solo garantiza que ambos valores lleguen con formato de fecha
    # válido; `delegar_tarea` deja propagar el `ValidationError` real si
    # la vigencia es inválida, y la vista lo muestra como mensaje.


class SubtareaForm(forms.Form):
    titulo = forms.CharField(max_length=200, label="Título")
    descripcion = forms.CharField(required=False, widget=forms.Textarea, label="Descripción")
    usuario_responsable = forms.ModelChoiceField(
        queryset=Usuario.objects.none(), required=False, label="Usuario responsable"
    )
    equipo_responsable = forms.ModelChoiceField(
        queryset=Equipo.objects.none(), required=False, label="Equipo responsable"
    )
    fecha_limite = forms.DateTimeField(required=False, label="Fecha límite", input_formats=_FORMATOS_FECHA_HORA)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usuario_responsable"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        self.fields["equipo_responsable"].queryset = Equipo.objects.filter(activo=True).order_by("nombre")
        self.fields["fecha_limite"].widget = forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format=_FORMATOS_FECHA_HORA[0]
        )


class ComentarioTareaForm(forms.Form):
    """Solo texto — a diferencia de `apps.tickets.operaciones.comentar_ticket`,
    `apps.tareas.operaciones.comentar_tarea` no acepta un archivo junto al
    comentario (revisado en el backend real, no asumido por analogía): la
    evidencia es una acción separada (`EvidenciaTareaForm`)."""

    contenido = forms.CharField(widget=forms.Textarea, label="Comentario")


class EvidenciaTareaForm(forms.Form):
    archivo = forms.FileField(label="Archivo")
