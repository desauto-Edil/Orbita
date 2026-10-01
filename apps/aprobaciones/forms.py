"""Formularios de Aprobaciones — 3.UI.2.

Mismo criterio que `apps/tareas/forms.py` (su precedente directo):
responsabilidad estrictamente de entrada HTTP — presencia/formato/tipos —
nunca autorización ni reglas de dominio. `apps.aprobaciones.operaciones`
sigue siendo la única autoridad: `resolver_aprobacion` revalida
`estado == PENDIENTE` bajo lock y `puede_aprobar` server-side, y
`reasignar_aprobacion` vuelve a validar que se indique exactamente un
nuevo aprobador de un solo tipo. La validación de `clean()` aquí abajo es
UX (evitar un viaje de red innecesario), nunca un sustituto.

Sin Form para "consultar" — esa acción no recibe datos, mismo criterio ya
aplicado a tomar/iniciar en 3.UI.1."""

from django import forms
from django.contrib.auth import get_user_model

from apps.aprobaciones.models import Aprobacion
from apps.core.models import Equipo

Usuario = get_user_model()


class DecisionAprobacionForm(forms.Form):
    """RQF-080: observación obligatoria para RECHAZAR/DEVOLVER, opcional
    para APROBAR — reflejado aquí para UX, pero
    `apps.aprobaciones.operaciones.resolver_aprobacion` es quien realmente
    la exige (`ValidationError` si falta) y esta vista deja propagar esa
    excepción de dominio como mensaje si, por lo que sea, este Form no la
    hubiera atrapado antes."""

    DECISIONES = [
        (Aprobacion.Estado.APROBADA, "Aprobar"),
        (Aprobacion.Estado.RECHAZADA, "Rechazar"),
        (Aprobacion.Estado.DEVUELTA, "Devolver"),
    ]

    decision = forms.ChoiceField(choices=DECISIONES, label="Decisión", widget=forms.RadioSelect)
    observacion = forms.CharField(required=False, widget=forms.Textarea, label="Observación")

    def clean(self):
        cleaned = super().clean()
        decision = cleaned.get("decision")
        observacion = cleaned.get("observacion")
        if decision in (Aprobacion.Estado.RECHAZADA, Aprobacion.Estado.DEVUELTA) and not observacion:
            raise forms.ValidationError("La observación es obligatoria para rechazar o devolver (RQF-080).")
        return cleaned


class ReasignacionAprobacionForm(forms.Form):
    """RQF-083. Mismo patrón de queryset acotado en `__init__` que
    `apps.tareas.forms.AsignacionTareaForm` — comodidad de UI, nunca
    autorización real; `apps.aprobaciones.autorizacion` no documenta
    ningún alcance Área/Unidad para Aprobaciones (mismo hallazgo ya
    aplicado a Tareas), así que no se inventa un filtro adicional aquí."""

    usuario = forms.ModelChoiceField(queryset=Usuario.objects.none(), required=False, label="Usuario")
    equipo = forms.ModelChoiceField(queryset=Equipo.objects.none(), required=False, label="Equipo")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["usuario"].queryset = Usuario.objects.filter(is_active=True).order_by("username")
        self.fields["equipo"].queryset = Equipo.objects.filter(activo=True).order_by("nombre")

    def clean(self):
        cleaned = super().clean()
        usuario = cleaned.get("usuario")
        equipo = cleaned.get("equipo")
        if not usuario and not equipo:
            raise forms.ValidationError("Indique un nuevo usuario o equipo aprobador.")
        if usuario and equipo:
            raise forms.ValidationError("Indique un nuevo aprobador de un solo tipo (usuario o equipo).")
        return cleaned
