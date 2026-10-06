"""Formularios de Configuración (`apps/core/configuracion.py`).

Solo validación y forma de los datos; quién puede usarlos lo decide la vista
por capacidades. Ningún formulario elimina filas: retirar algo es marcarlo
inactivo, para conservar la trazabilidad histórica.
"""

from django import forms
from django.contrib.auth.forms import BaseUserCreationForm
from django.db.models import Q
from django.utils import timezone

from apps.core.models import (
    Area,
    AsignacionRol,
    ConfiguracionSistema,
    PerfilOrganizacional,
    Permiso,
    RolFuncional,
    UnidadNegocio,
    Usuario,
)

_AREA_TEXTO = forms.Textarea(attrs={"rows": 3})


class ConfiguracionSistemaForm(forms.ModelForm):
    quitar_logo = forms.BooleanField(required=False, label="Quitar el logo actual")

    class Meta:
        model = ConfiguracionSistema
        fields = ("nombre", "logo")
        labels = {"nombre": "Nombre del sistema", "logo": "Logo"}
        help_texts = {
            "nombre": "Se muestra en el encabezado, en el inicio de sesión y en el título de cada página.",
            "logo": "PNG, JPG o WebP, hasta 1 MB. Se ve mejor cuadrado y con fondo transparente.",
        }
        widgets = {"logo": forms.FileInput(attrs={"accept": ".png,.jpg,.jpeg,.webp"})}

    def clean_nombre(self):
        nombre = self.cleaned_data["nombre"].strip()
        if not nombre:
            raise forms.ValidationError("El sistema necesita un nombre.")
        return nombre

    def save(self, commit=True):
        if self.cleaned_data.get("quitar_logo") and "logo" not in self.changed_data:
            self.instance.logo = ""
        return super().save(commit)


# --- Usuarios ---------------------------------------------------------------


class _PerfilMixin(forms.Form):
    """Cargo y teléfono viven en `PerfilOrganizacional`, separado de la cuenta
    (RQF-015), pero se editan junto con ella."""

    cargo = forms.CharField(max_length=150, required=False)
    telefono = forms.CharField(max_length=30, required=False, label="Teléfono")

    def perfil_con_datos(self, usuario):
        """Perfil (sin guardar) con los datos del formulario y, si ya
        existía, su estado previo serializable para la auditoría."""
        perfil = PerfilOrganizacional.objects.filter(usuario=usuario).first()
        existia = perfil is not None
        perfil = perfil or PerfilOrganizacional(usuario=usuario)
        perfil.cargo = self.cleaned_data["cargo"]
        perfil.telefono = self.cleaned_data["telefono"]
        return perfil, existia


class UsuarioCrearForm(_PerfilMixin, BaseUserCreationForm):
    class Meta:
        model = Usuario
        fields = ("username", "first_name", "last_name", "email")
        labels = {"username": "Usuario", "first_name": "Nombres", "last_name": "Apellidos", "email": "Correo"}

    field_order = ("username", "first_name", "last_name", "email", "cargo", "telefono", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].label = "Contraseña inicial"
        self.fields["password2"].label = "Confirmar contraseña"
        self.fields["password2"].help_text = ""


class UsuarioDatosForm(_PerfilMixin, forms.ModelForm):
    class Meta:
        model = Usuario
        fields = ("username", "first_name", "last_name", "email", "is_active", "is_staff", "is_superuser")
        labels = {
            "username": "Usuario",
            "first_name": "Nombres",
            "last_name": "Apellidos",
            "email": "Correo",
            "is_active": "Cuenta activa",
            "is_staff": "Puede entrar a la administración avanzada",
            "is_superuser": "Superusuario",
        }
        help_texts = {
            "is_active": "Una cuenta inactiva no puede iniciar sesión; su historial se conserva.",
            "is_staff": "",
            "is_superuser": "Tiene todos los permisos de la administración avanzada sin asignación explícita.",
        }

    field_order = ("username", "first_name", "last_name", "email", "cargo", "telefono")

    def __init__(self, *args, editor, **kwargs):
        super().__init__(*args, **kwargs)
        self.editor = editor
        perfil = PerfilOrganizacional.objects.filter(usuario=self.instance).first()
        if perfil:
            self.fields["cargo"].initial = perfil.cargo
            self.fields["telefono"].initial = perfil.telefono
        # Solo un superusuario decide quién más lo es o entra a Django Admin.
        if not editor.is_superuser:
            del self.fields["is_staff"]
            del self.fields["is_superuser"]

    def clean(self):
        datos = super().clean()
        if self.instance.pk == self.editor.pk:
            if datos.get("is_active") is False:
                self.add_error("is_active", "No puedes desactivar tu propia cuenta.")
            if "is_superuser" in self.fields and datos.get("is_superuser") is False and self.editor.is_superuser:
                self.add_error("is_superuser", "No puedes quitarte a ti mismo el acceso de superusuario.")
        return datos


class MembresiaForm(forms.Form):
    """Añadir a un usuario a un Área o a una Unidad de negocio."""

    destino = forms.ModelChoiceField(queryset=None)
    es_principal = forms.BooleanField(required=False, label="Es la principal")

    def __init__(self, *args, queryset, etiqueta, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destino"].queryset = queryset
        self.fields["destino"].label = etiqueta
        self.fields["destino"].empty_label = "Selecciona…"


class AsignacionRolForm(forms.ModelForm):
    """Asignar un rol con alcance y vigencia (RQF-023/024/025)."""

    class Meta:
        model = AsignacionRol
        fields = ("rol", "tipo_alcance", "area", "unidad_negocio", "fecha_inicio", "fecha_fin")
        labels = {
            "rol": "Rol",
            "tipo_alcance": "Alcance",
            "area": "Área",
            "unidad_negocio": "Unidad de negocio",
            "fecha_inicio": "Desde",
            "fecha_fin": "Hasta",
        }
        help_texts = {"fecha_fin": "Opcional. Vacío = sin fecha de fin."}
        widgets = {
            "fecha_inicio": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "fecha_fin": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, usuario, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.usuario = usuario
        self.fields["rol"].queryset = RolFuncional.objects.filter(activo=True).order_by("nombre")
        self.fields["rol"].empty_label = "Selecciona…"
        self.fields["area"].empty_label = self.fields["unidad_negocio"].empty_label = "Selecciona…"
        self.fields["tipo_alcance"].choices = AsignacionRol.TipoAlcance.choices
        self.fields["tipo_alcance"].initial = AsignacionRol.TipoAlcance.GLOBAL
        self.fields["area"].queryset = Area.objects.filter(activo=True).order_by("nombre")
        self.fields["unidad_negocio"].queryset = UnidadNegocio.objects.filter(activo=True).order_by("nombre")
        self.fields["fecha_inicio"].initial = timezone.localdate

    def clean(self):
        datos = super().clean()
        tipo = datos.get("tipo_alcance")
        area, unidad = datos.get("area"), datos.get("unidad_negocio")
        if tipo == AsignacionRol.TipoAlcance.GLOBAL:
            datos["area"] = datos["unidad_negocio"] = area = unidad = None
        elif tipo == AsignacionRol.TipoAlcance.AREA:
            datos["unidad_negocio"] = unidad = None
            if area is None:
                self.add_error("area", "Elige el área a la que se limita este rol.")
        elif tipo == AsignacionRol.TipoAlcance.UNIDAD:
            datos["area"] = area = None
            if unidad is None:
                self.add_error("unidad_negocio", "Elige la unidad a la que se limita este rol.")

        inicio, fin = datos.get("fecha_inicio"), datos.get("fecha_fin")
        if inicio and fin and fin < inicio:
            self.add_error("fecha_fin", "La fecha de fin no puede ser anterior a la de inicio.")

        rol = datos.get("rol")
        if rol and tipo and not self.errors:
            repetida = AsignacionRol.objects.filter(
                usuario=self.instance.usuario, rol=rol, tipo_alcance=tipo, area=area, unidad_negocio=unidad, activo=True
            )
            if repetida.exists():
                raise forms.ValidationError("Esta persona ya tiene ese rol con el mismo alcance.")
        return datos


# --- Roles y permisos -------------------------------------------------------


class RolForm(forms.ModelForm):
    permisos = forms.ModelMultipleChoiceField(
        queryset=Permiso.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Permisos del rol",
    )

    class Meta:
        model = RolFuncional
        fields = ("nombre", "descripcion", "activo")
        labels = {"descripcion": "Descripción", "activo": "Rol activo"}
        help_texts = {"activo": "Un rol inactivo deja de otorgar sus permisos a quienes lo tienen asignado."}
        widgets = {"descripcion": _AREA_TEXTO}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        actuales = Permiso.objects.none()
        if self.instance.pk:
            actuales = Permiso.objects.filter(rolpermiso__rol=self.instance, rolpermiso__activo=True)
        # Activos, más los que el rol ya tiene aunque hoy estén inactivos (para
        # no quitarlos en silencio al guardar).
        self.fields["permisos"].queryset = (
            Permiso.objects.filter(Q(activo=True) | Q(pk__in=actuales.values("pk"))).distinct().order_by("codigo")
        )
        self.fields["permisos"].label_from_instance = lambda p: f"{p.nombre} · {p.codigo}"
        self.fields["permisos"].initial = list(actuales.values_list("pk", flat=True))

    def clean_nombre(self):
        nombre = self.cleaned_data["nombre"].strip()
        if RolFuncional.objects.filter(nombre__iexact=nombre).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Ya existe un rol con ese nombre.")
        return nombre


class PermisoForm(forms.ModelForm):
    class Meta:
        model = Permiso
        fields = ("codigo", "nombre", "descripcion", "activo")
        labels = {"codigo": "Código", "descripcion": "Descripción", "activo": "Permiso activo"}
        help_texts = {
            "codigo": "Identificador que usa la plataforma, por ejemplo «tickets.atender». No se puede cambiar después.",
            "activo": "Un permiso inactivo no autoriza a nadie, aunque esté en un rol.",
        }
        widgets = {"descripcion": _AREA_TEXTO}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # El código es lo que la plataforma comprueba: cambiarlo dejaría
            # sin efecto a quienes ya lo tienen.
            self.fields["codigo"].disabled = True


# --- Organización -----------------------------------------------------------


class _CatalogoOrganizacionalForm(forms.ModelForm):
    """Área y Unidad comparten forma: catálogos independientes unidos por una
    relación transversal sin jerarquía (RQF-014), que se marca desde ambos."""

    relacionadas = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple
    )
    modelo_relacionado = None
    campo_propio = campo_relacionado = ""

    class Meta:
        fields = ("nombre", "codigo", "descripcion", "activo")
        labels = {"codigo": "Código", "descripcion": "Descripción"}
        widgets = {"descripcion": _AREA_TEXTO}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        actuales = self.relaciones().filter(activo=True).values_list(f"{self.campo_relacionado}_id", flat=True)
        self.fields["relacionadas"].queryset = self.modelo_relacionado.objects.filter(
            Q(activo=True) | Q(pk__in=actuales)
        ).order_by("nombre")
        self.fields["relacionadas"].initial = list(actuales)

    def relaciones(self):
        from apps.core.models import AreaUnidadNegocio

        if not self.instance.pk:
            return AreaUnidadNegocio.objects.none()
        return AreaUnidadNegocio.objects.filter(**{self.campo_propio: self.instance})


class AreaForm(_CatalogoOrganizacionalForm):
    modelo_relacionado = UnidadNegocio
    campo_propio, campo_relacionado = "area", "unidad_negocio"

    class Meta(_CatalogoOrganizacionalForm.Meta):
        model = Area
        labels = {**_CatalogoOrganizacionalForm.Meta.labels, "activo": "Área activa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["relacionadas"].label = "Unidades de negocio relacionadas"


class UnidadNegocioForm(_CatalogoOrganizacionalForm):
    modelo_relacionado = Area
    campo_propio, campo_relacionado = "unidad_negocio", "area"

    class Meta(_CatalogoOrganizacionalForm.Meta):
        model = UnidadNegocio
        labels = {**_CatalogoOrganizacionalForm.Meta.labels, "activo": "Unidad activa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["relacionadas"].label = "Áreas relacionadas"


def categoria_form():
    """Formulario de Categoría, construido de forma perezosa: `core` no
    depende del dominio de catálogo de forma permanente."""
    from apps.catalogo.models import Categoria

    class CategoriaForm(forms.ModelForm):
        class Meta:
            model = Categoria
            fields = ("nombre", "descripcion", "activo")
            labels = {"descripcion": "Descripción", "activo": "Categoría activa"}
            help_texts = {"activo": "Una categoría inactiva no se ofrece al crear servicios nuevos."}
            widgets = {"descripcion": _AREA_TEXTO}

        def clean_nombre(self):
            nombre = self.cleaned_data["nombre"].strip()
            if Categoria.objects.filter(nombre__iexact=nombre).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("Ya existe una categoría con ese nombre.")
            return nombre

    return CategoriaForm
