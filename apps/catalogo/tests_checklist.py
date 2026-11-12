"""Tipo de campo «Lista de evidencias» (CHECKLIST): estrategia y reglas que no dependen de un ticket."""

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.catalogo.campos import ESTRATEGIAS_POR_TIPO
from apps.catalogo.reglas import OPERADOR_TIPOS_COMPATIBLES


class _Opciones:
    def __init__(self, valores):
        self._valores = valores

    def values_list(self, *_campos, flat=False):
        return list(self._valores)


def _campo(puntos, **configuracion):
    return SimpleNamespace(opciones=_Opciones(puntos), configuracion=configuracion)


class EstrategiaChecklistTests(SimpleTestCase):
    def setUp(self):
        self.estrategia = ESTRATEGIAS_POR_TIPO["CHECKLIST"]

    def test_completo_cuando_todos_los_puntos_tienen_su_prueba(self):
        campo = _campo(["captura", "enlace"])
        self.estrategia.validar_valor(campo, ["captura", "enlace"])
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(campo, ["captura"])

    def test_sin_puntos_no_es_valido(self):
        with self.assertRaises(ValidationError):
            self.estrategia.validar_valor(_campo([]), [])

    def test_cada_prueba_es_un_archivo_o_un_enlace(self):
        campo = _campo(["publicada"], extensiones_permitidas=["png"], tamano_maximo_mb=1)
        self.estrategia.validar_prueba(campo, enlace="https://ejemplo.com/post/1")
        self.estrategia.validar_prueba(campo, archivo=SimpleUploadedFile("prueba.png", b"x"))
        with self.assertRaises(ValidationError):
            self.estrategia.validar_prueba(campo)  # ninguna
        with self.assertRaises(ValidationError):
            self.estrategia.validar_prueba(campo, archivo=SimpleUploadedFile("prueba.png", b"x"), enlace="https://a.co")
        with self.assertRaises(ValidationError):
            self.estrategia.validar_prueba(campo, archivo=SimpleUploadedFile("prueba.exe", b"x"))  # extensión
        with self.assertRaises(ValidationError):
            self.estrategia.validar_prueba(campo, enlace="no es un enlace")

    def test_la_configuracion_solo_admite_los_limites_de_archivo(self):
        self.estrategia.validar_configuracion({"extensiones_permitidas": ["pdf"], "tamano_maximo_mb": 5})
        with self.assertRaises(ValidationError):
            self.estrategia.validar_configuracion({"longitud_maxima": 10})

    def test_no_puede_ser_origen_de_una_regla_condicional(self):
        self.assertNotIn("CHECKLIST", OPERADOR_TIPOS_COMPATIBLES["ESTA_VACIO"])
        self.assertNotIn("CHECKLIST", OPERADOR_TIPOS_COMPATIBLES["IGUAL_A"])
