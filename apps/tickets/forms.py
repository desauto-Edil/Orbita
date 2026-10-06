"""Formularios de entrada HTTP de Tickets — 4.A2 (prórrogas).

Solo forma y tipos (UX): la autoridad sigue siendo `apps.tickets.prorrogas`, que
revalida estado, política, permiso y fechas bajo lock."""

from django import forms

from apps.tickets.prorrogas import LIMITE_MOTIVO

_FORMATOS_FECHA_HORA = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]


class SolicitudProrrogaForm(forms.Form):
    nueva_fecha = forms.DateTimeField(
        label="Nueva fecha objetivo",
        input_formats=_FORMATOS_FECHA_HORA,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    motivo = forms.CharField(
        label="Motivo", max_length=LIMITE_MOTIVO, widget=forms.Textarea(attrs={"rows": 3, "maxlength": LIMITE_MOTIVO})
    )


class CancelacionProrrogaForm(forms.Form):
    motivo = forms.CharField(required=False, max_length=LIMITE_MOTIVO, widget=forms.Textarea(attrs={"rows": 2}))
