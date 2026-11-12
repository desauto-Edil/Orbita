"""Revisión de piezas (UX-5): salidas de la revisión, comentario obligatorio, anotación visual (con un dibujo solo se
puede corregir) y la revisión de datos en tabla o formulario."""

import base64
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

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

    def test_acepta_la_anotacion_como_archivo(self):
        """El visor la envía como archivo (multipart): como texto, un PNG grande supera DATA_UPLOAD_MAX_MEMORY_SIZE."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        imagen = revision_piezas.decodificar_anotacion(SimpleUploadedFile("anotacion.png", PNG_MINIMO, "image/png"))
        self.assertEqual(imagen.read()[:8], b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(ValidationError):
            revision_piezas.decodificar_anotacion(SimpleUploadedFile("x.png", b"GIF89a", "image/png"))

    def test_rechaza_lo_que_no_es_png(self):
        for texto in ("data:image/jpeg;base64,AAAA", "data:image/png;base64,no-es-base64!", "data:image/png;base64," + base64.b64encode(b"GIF89a").decode()):
            with self.assertRaises(ValidationError):
                revision_piezas.decodificar_anotacion(texto)


class DibujoSoloCorregirTests(TestCase):
    """Con un dibujo sobre la pieza la revisión solo puede ser «Corregir»: el servidor lo exige aunque el navegador
    no deshabilite Aprobar y Eliminar."""

    def _decidir(self, decision):
        anotacion = "data:image/png;base64," + base64.b64encode(PNG_MINIMO).decode()
        aprobacion = mock.Mock()
        with mock.patch.object(revision_piezas, "_aprobacion_del_ticket", return_value=aprobacion), \
                mock.patch("apps.aprobaciones.autorizacion.puede_aprobar", return_value=True), \
                mock.patch("apps.aprobaciones.views._decision_form", return_value=DecisionAprobacionForm(permite_excluir=True)), \
                mock.patch("apps.aprobaciones.views._contexto_item", return_value={"elemento": mock.Mock()}), \
                mock.patch.object(revision_piezas, "es_de_gaceta", return_value=False), \
                mock.patch("apps.aprobaciones.decisiones.aplicar") as aplicar, \
                mock.patch.object(revision_piezas.AnotacionRevision.objects, "create"), \
                mock.patch.object(revision_piezas.Adjunto.objects, "filter"):
            revision_piezas.decidir(mock.Mock(), 1, mock.Mock(), decision=decision, comentario="Revisar", anotacion=anotacion)
            return aplicar

    def test_con_dibujo_no_se_aprueba_ni_se_elimina(self):
        for decision in ("APROBADA", "EXCLUIDA"):
            with self.subTest(decision=decision), self.assertRaises(ValidationError):
                self._decidir(decision)

    def test_con_dibujo_se_corrige(self):
        self._decidir("DEVUELTA").assert_called_once()


class RevisionDeDatosTests(SimpleTestCase):
    """Ítems solo con texto: tabla mientras quepa, formulario con muchas columnas; con archivos, revisión visual."""

    def _fila(self, valores, archivos=(), portada=None):
        return {"valores": [{"etiqueta": e, "texto": t} for e, t in valores], "archivos": list(archivos), "portada": portada}

    def test_sin_archivos_es_tabla_con_el_texto_completo(self):
        from apps.tickets.espacio import revision_de_datos

        largo = "Texto largo " * 40
        datos = revision_de_datos([self._fila([("Copy", largo), ("Medio", "LinkedIn")]), self._fila([("Copy", "b")])])
        self.assertEqual(datos["modo"], "tabla")
        self.assertEqual(datos["columnas"], ["Copy", "Medio"])
        self.assertEqual(datos["filas"][0]["celdas"][0]["texto"], largo)
        self.assertEqual(datos["filas"][1]["celdas"][1]["texto"], "—")

    def test_muchas_columnas_es_formulario_y_con_audiovisual_no_aplica(self):
        from apps.tickets.espacio import COLUMNAS_MAXIMAS_EN_TABLA, revision_de_datos

        columnas = [(f"C{i}", "x") for i in range(COLUMNAS_MAXIMAS_EN_TABLA + 1)]
        self.assertEqual(revision_de_datos([self._fila(columnas)])["modo"], "formulario")
        # Por el contenido: un PDF de evidencia no la vuelve visual; una imagen o un video, sí.
        self.assertEqual(revision_de_datos([self._fila(columnas, archivos=[object()])])["modo"], "formulario")
        self.assertIsNone(revision_de_datos([self._fila(columnas, archivos=[object()], portada=object())]))
        self.assertIsNone(revision_de_datos([]))
