"""Revisión de piezas (UX-5): salidas de la revisión, comentario obligatorio y anotación visual."""

import base64

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.aprobaciones.forms import DecisionAprobacionForm
from apps.tickets import revision_piezas

PNG_MINIMO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class SalidasTests(SimpleTestCase):
    def test_solo_aprobar_corregir_y_eliminar_donde_se_puede_excluir(self):
        con_exclusion = DecisionAprobacionForm(permite_excluir=True)
        self.assertEqual(
            [s["etiqueta"] for s in revision_piezas.salidas_de(con_exclusion)], ["Aprobar", "Corregir", "Eliminar"]
        )
        sin_exclusion = DecisionAprobacionForm(permite_excluir=False)
        self.assertEqual([s["valor"] for s in revision_piezas.salidas_de(sin_exclusion)], ["APROBADA", "DEVUELTA"])
        self.assertEqual(revision_piezas.salidas_de(None), [])


class AnotacionTests(SimpleTestCase):
    def test_acepta_un_png_valido_y_nada_si_no_hay_dibujo(self):
        imagen = revision_piezas.decodificar_anotacion("data:image/png;base64," + base64.b64encode(PNG_MINIMO).decode())
        self.assertEqual(imagen.read()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIsNone(revision_piezas.decodificar_anotacion(""))

    def test_rechaza_lo_que_no_es_png(self):
        for texto in ("data:image/jpeg;base64,AAAA", "data:image/png;base64,no-es-base64!", "data:image/png;base64," + base64.b64encode(b"GIF89a").decode()):
            with self.assertRaises(ValidationError):
                revision_piezas.decodificar_anotacion(texto)
