"""Strategy Pattern para el comportamiento de cada tipo de campo (RQF-040/041).

`EstrategiaCampo` concentra todo lo que varía por tipo de campo: qué claves
admite `Campo.configuracion`, cómo se interpreta/normaliza un valor crudo,
qué lo hace válido, y qué widget usa la previsualización (CU-013). No sabe
nada de reglas condicionales — `EspecificacionRegla` (`reglas.py`) reutiliza
`normalizar()` de aquí para comparar valores sin duplicar esa
interpretación, pero Strategy y Specification son responsabilidades
separadas (ver propuesta 1.2 aprobada).

El mapping `TipoCampo -> EstrategiaCampo` (`ESTRATEGIAS_POR_TIPO`) es un
diccionario plano: no se introduce una clase Factory porque un `dict` ya
resuelve el único punto de variación real (qué tipo usa qué estrategia).

Los 14 tipos de RQF-040 se agrupan en 7 estrategias por comportamiento
realmente equivalente, no una por tipo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator

from apps.core.models import Area, UnidadNegocio, Usuario


class EstrategiaCampo(ABC):
    """Interfaz común. `widget` es el tipo de control HTML de la
    previsualización (CU-013/RQF-045) — no hay carga real de datos aquí,
    solo qué se renderiza.
    """

    claves_configuracion: frozenset = frozenset()
    widget: str = "text"

    def validar_configuracion(self, configuracion):
        desconocidas = set(configuracion) - self.claves_configuracion
        if desconocidas:
            raise ValidationError(
                f"Claves de configuración no admitidas para este tipo: {sorted(desconocidas)}."
            )
        self._validar_configuracion(configuracion)

    def _validar_configuracion(self, configuracion):
        return None

    @abstractmethod
    def normalizar(self, valor):
        """Convierte `valor` (crudo, de un JSON/formulario) a su tipo Python
        canónico. Usado tanto por `validar_valor` como por
        `EspecificacionRegla` para comparar de forma consistente.
        """

    @abstractmethod
    def validar_valor(self, campo, valor):
        """Lanza `ValidationError` si `valor` no es válido para `campo`
        (tipo + `campo.configuracion`). Punto único reutilizado por la
        previsualización hoy y, en Sprint 2, por la validación real de
        RQF-044/RN-012 al radicar un Ticket.
        """


class EstrategiaTexto(EstrategiaCampo):
    """TEXTO, TEXTO_LARGO, CORREO, URL — todas son cadenas; solo difieren en
    el widget (línea simple vs. área) y en un formato de validación fijo por
    tipo (no configurable: `formato` lo decide `ESTRATEGIAS_POR_TIPO`, no el
    Gestor).
    """

    claves_configuracion = frozenset({"longitud_maxima", "placeholder"})

    def __init__(self, *, multilinea=False, formato=None):
        self.multilinea = multilinea
        self.formato = formato  # None | "email" | "url"

    @property
    def widget(self):
        if self.multilinea:
            return "textarea"
        if self.formato == "email":
            return "email"
        if self.formato == "url":
            return "url"
        return "text"

    def _validar_configuracion(self, configuracion):
        longitud = configuracion.get("longitud_maxima")
        if longitud is not None and (not isinstance(longitud, int) or longitud <= 0):
            raise ValidationError("longitud_maxima debe ser un entero positivo.")

    def normalizar(self, valor):
        return "" if valor is None else str(valor)

    def validar_valor(self, campo, valor):
        texto = self.normalizar(valor)
        longitud = campo.configuracion.get("longitud_maxima")
        if longitud is not None and len(texto) > longitud:
            raise ValidationError(f"El valor excede la longitud máxima de {longitud} caracteres.")
        if self.formato == "email":
            EmailValidator()(texto)
        elif self.formato == "url":
            URLValidator()(texto)


class EstrategiaNumerica(EstrategiaCampo):
    claves_configuracion = frozenset({"permite_decimales", "minimo", "maximo"})
    widget = "number"

    def _validar_configuracion(self, configuracion):
        minimo, maximo = configuracion.get("minimo"), configuracion.get("maximo")
        for clave, valor in (("minimo", minimo), ("maximo", maximo)):
            if valor is not None and not isinstance(valor, (int, float)):
                raise ValidationError(f"{clave} debe ser numérico.")
        if minimo is not None and maximo is not None and minimo > maximo:
            raise ValidationError("minimo no puede ser mayor que maximo.")

    def normalizar(self, valor):
        if valor in (None, ""):
            return None
        try:
            return float(valor)
        except (TypeError, ValueError) as exc:
            raise ValidationError("El valor debe ser numérico.") from exc

    def validar_valor(self, campo, valor):
        numero = self.normalizar(valor)
        if numero is None:
            raise ValidationError("Este campo requiere un valor numérico.")
        if not campo.configuracion.get("permite_decimales", False) and numero != int(numero):
            raise ValidationError("Este campo no admite decimales.")
        minimo, maximo = campo.configuracion.get("minimo"), campo.configuracion.get("maximo")
        if minimo is not None and numero < minimo:
            raise ValidationError(f"El valor debe ser mayor o igual a {minimo}.")
        if maximo is not None and numero > maximo:
            raise ValidationError(f"El valor debe ser menor o igual a {maximo}.")


class EstrategiaTemporal(EstrategiaCampo):
    claves_configuracion = frozenset({"fecha_minima", "fecha_maxima"})

    def __init__(self, *, incluye_hora=False):
        self.incluye_hora = incluye_hora

    @property
    def widget(self):
        return "datetime-local" if self.incluye_hora else "date"

    def _parsear(self, valor):
        if valor in (None, ""):
            return None
        if isinstance(valor, (date, datetime)):
            return valor
        try:
            return datetime.fromisoformat(str(valor)) if self.incluye_hora else date.fromisoformat(str(valor))
        except ValueError as exc:
            raise ValidationError("Formato de fecha inválido.") from exc

    def _validar_configuracion(self, configuracion):
        minima = self._parsear(configuracion.get("fecha_minima"))
        maxima = self._parsear(configuracion.get("fecha_maxima"))
        if minima is not None and maxima is not None and minima > maxima:
            raise ValidationError("fecha_minima no puede ser posterior a fecha_maxima.")

    def normalizar(self, valor):
        return self._parsear(valor)

    def validar_valor(self, campo, valor):
        fecha = self.normalizar(valor)
        if fecha is None:
            raise ValidationError("Este campo requiere una fecha.")
        minima = self._parsear(campo.configuracion.get("fecha_minima"))
        maxima = self._parsear(campo.configuracion.get("fecha_maxima"))
        if minima is not None and fecha < minima:
            raise ValidationError(f"La fecha debe ser posterior o igual a {minima}.")
        if maxima is not None and fecha > maxima:
            raise ValidationError(f"La fecha debe ser anterior o igual a {maxima}.")


class EstrategiaSeleccion(EstrategiaCampo):
    """LISTA (única) y MULTILISTA — las opciones viven en `OpcionCampo`
    (RQF-042), nunca en el JSON de `configuracion`.
    """

    def __init__(self, *, multiple=False):
        self.multiple = multiple
        self.claves_configuracion = (
            frozenset({"minimo_selecciones", "maximo_selecciones"}) if multiple else frozenset()
        )

    @property
    def widget(self):
        return "select-multiple" if self.multiple else "select"

    def _validar_configuracion(self, configuracion):
        if not self.multiple:
            return
        minimo, maximo = configuracion.get("minimo_selecciones"), configuracion.get("maximo_selecciones")
        for clave, valor in (("minimo_selecciones", minimo), ("maximo_selecciones", maximo)):
            if valor is not None and (not isinstance(valor, int) or valor < 0):
                raise ValidationError(f"{clave} debe ser un entero no negativo.")
        if minimo is not None and maximo is not None and minimo > maximo:
            raise ValidationError("minimo_selecciones no puede ser mayor que maximo_selecciones.")

    def normalizar(self, valor):
        if self.multiple:
            return list(valor) if valor else []
        return valor

    def validar_valor(self, campo, valor):
        opciones_validas = set(campo.opciones.values_list("valor", flat=True))
        if self.multiple:
            seleccionados = self.normalizar(valor)
            invalidos = set(seleccionados) - opciones_validas
            if invalidos:
                raise ValidationError(f"Opciones inválidas: {sorted(invalidos)}.")
            minimo = campo.configuracion.get("minimo_selecciones")
            maximo = campo.configuracion.get("maximo_selecciones")
            if minimo is not None and len(seleccionados) < minimo:
                raise ValidationError(f"Debe seleccionar al menos {minimo} opción(es).")
            if maximo is not None and len(seleccionados) > maximo:
                raise ValidationError(f"Debe seleccionar como máximo {maximo} opción(es).")
        elif valor not in opciones_validas:
            raise ValidationError("La opción seleccionada no es válida para este campo.")


class EstrategiaBooleano(EstrategiaCampo):
    widget = "checkbox"

    _VERDADEROS = {True, "true", "True", "1", 1}
    _FALSOS = {False, "false", "False", "0", 0}

    def normalizar(self, valor):
        if valor in self._VERDADEROS:
            return True
        if valor in self._FALSOS:
            return False
        return None

    def validar_valor(self, campo, valor):
        if self.normalizar(valor) is None:
            raise ValidationError("Este campo requiere un valor booleano.")


class EstrategiaArchivo(EstrategiaCampo):
    """La carga real de archivos (almacenamiento, RQF-007) es Sprint 2 — no
    existe todavía dónde guardarlos. Aquí solo se valida la configuración
    declarada y, dado un valor ya descrito como metadatos
    (`{"nombre": ..., "tamano_mb": ...}`), que ese archivo cumpliría las
    restricciones. La previsualización (CU-013) NO ofrece una carga
    funcional de este tipo — ver `templates/catalogo/formulario_preview.html`.
    """

    claves_configuracion = frozenset({"extensiones_permitidas", "tamano_maximo_mb"})
    widget = "file"

    def _validar_configuracion(self, configuracion):
        extensiones = configuracion.get("extensiones_permitidas")
        if extensiones is not None:
            if not isinstance(extensiones, list) or not all(isinstance(e, str) for e in extensiones):
                raise ValidationError("extensiones_permitidas debe ser una lista de texto.")
        tamano = configuracion.get("tamano_maximo_mb")
        if tamano is not None and (not isinstance(tamano, (int, float)) or tamano <= 0):
            raise ValidationError("tamano_maximo_mb debe ser un número positivo.")

    def normalizar(self, valor):
        return valor

    def validar_valor(self, campo, valor):
        if not valor or "nombre" not in valor:
            raise ValidationError("Este campo requiere un archivo.")
        extensiones = campo.configuracion.get("extensiones_permitidas")
        if extensiones:
            extension = valor["nombre"].rsplit(".", 1)[-1].lower()
            if extension not in [e.lower() for e in extensiones]:
                raise ValidationError(f"Extensión no permitida. Use: {', '.join(extensiones)}.")
        tamano_maximo = campo.configuracion.get("tamano_maximo_mb")
        if tamano_maximo is not None and valor.get("tamano_mb", 0) > tamano_maximo:
            raise ValidationError(f"El archivo excede el tamaño máximo de {tamano_maximo} MB.")


class EstrategiaReferenciaOrganizacional(EstrategiaCampo):
    """USUARIO, AREA, UNIDAD — reutilizan los modelos reales de `apps.core`,
    sin duplicar datos organizacionales (mismo principio que `apps.catalogo`
    aplica en `visibilidad.py`).
    """

    claves_configuracion = frozenset({"solo_activos"})
    widget = "select"

    def __init__(self, *, modelo):
        self.modelo = modelo

    def _validar_configuracion(self, configuracion):
        solo_activos = configuracion.get("solo_activos")
        if solo_activos is not None and not isinstance(solo_activos, bool):
            raise ValidationError("solo_activos debe ser booleano.")

    def normalizar(self, valor):
        if valor in (None, ""):
            return None
        try:
            return int(valor)
        except (TypeError, ValueError):
            return None

    def queryset(self, campo):
        base = self.modelo.objects.all()
        if campo.configuracion.get("solo_activos", True):
            filtro = "is_active" if self.modelo is Usuario else "activo"
            base = base.filter(**{filtro: True})
        return base

    def validar_valor(self, campo, valor):
        pk = self.normalizar(valor)
        if pk is None:
            raise ValidationError("Este campo requiere una referencia válida.")
        if not self.queryset(campo).filter(pk=pk).exists():
            raise ValidationError("La referencia seleccionada no existe o no está activa.")


ESTRATEGIAS_POR_TIPO = {
    "TEXTO": EstrategiaTexto(),
    "TEXTO_LARGO": EstrategiaTexto(multilinea=True),
    "CORREO": EstrategiaTexto(formato="email"),
    "URL": EstrategiaTexto(formato="url"),
    "NUMERO": EstrategiaNumerica(),
    "FECHA": EstrategiaTemporal(),
    "FECHA_HORA": EstrategiaTemporal(incluye_hora=True),
    "LISTA": EstrategiaSeleccion(),
    "MULTILISTA": EstrategiaSeleccion(multiple=True),
    "BOOLEANO": EstrategiaBooleano(),
    "ARCHIVO": EstrategiaArchivo(),
    "USUARIO": EstrategiaReferenciaOrganizacional(modelo=Usuario),
    "AREA": EstrategiaReferenciaOrganizacional(modelo=Area),
    "UNIDAD": EstrategiaReferenciaOrganizacional(modelo=UnidadNegocio),
}
