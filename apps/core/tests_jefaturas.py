"""Jefaturas y «Mi equipo»: relación explícita jefe → colaborador (con vigencia) y vista de solo lectura."""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.core import jefaturas
from apps.core.models import JefaturaUsuario, RegistroAuditoria, Usuario

CLAVE = "Clave-Segura-123"


class JefaturaDominioTests(TestCase):
    def setUp(self):
        self.jefa = Usuario.objects.create_user("jefa@pruebas.orbita")
        self.ana = Usuario.objects.create_user("ana@pruebas.orbita")
        self.beto = Usuario.objects.create_user("beto@pruebas.orbita")
        self.admin = Usuario.objects.create_user("admin_jef@pruebas.orbita")

    def test_asignar_jefe_y_consultar(self):
        jefaturas.asignar_jefe(self.ana, self.jefa, self.admin)
        self.assertEqual(jefaturas.jefe_de(self.ana), self.jefa)
        self.assertEqual(list(jefaturas.colaboradores_de(self.jefa)), [self.ana])
        self.assertTrue(jefaturas.tiene_colaboradores(self.jefa))
        self.assertFalse(jefaturas.tiene_colaboradores(self.ana))
        self.assertTrue(RegistroAuditoria.objects.filter(usuario=self.admin).exists())

    def test_cambiar_de_jefe_cierra_la_vigencia_sin_borrar(self):
        jefaturas.asignar_jefe(self.ana, self.jefa, self.admin)
        jefaturas.asignar_jefe(self.ana, self.beto, self.admin)
        self.assertEqual(jefaturas.jefe_de(self.ana), self.beto)
        anterior = JefaturaUsuario.objects.get(jefe=self.jefa, colaborador=self.ana)
        self.assertFalse(anterior.activo)
        self.assertIsNotNone(anterior.fecha_fin)
        jefaturas.asignar_jefe(self.ana, None, self.admin)  # quitar
        self.assertIsNone(jefaturas.jefe_de(self.ana))
        self.assertEqual(JefaturaUsuario.objects.filter(colaborador=self.ana).count(), 2)

    def test_repetir_el_mismo_jefe_no_hace_nada(self):
        primera = jefaturas.asignar_jefe(self.ana, self.jefa, self.admin)
        self.assertEqual(jefaturas.asignar_jefe(self.ana, self.jefa, self.admin), primera)
        self.assertEqual(JefaturaUsuario.objects.filter(colaborador=self.ana).count(), 1)

    def test_nadie_es_su_propio_jefe_ni_hay_ciclos(self):
        with self.assertRaises(ValidationError):
            jefaturas.asignar_jefe(self.ana, self.ana, self.admin)
        jefaturas.asignar_jefe(self.ana, self.jefa, self.admin)
        jefaturas.asignar_jefe(self.beto, self.ana, self.admin)
        with self.assertRaises(ValidationError):
            jefaturas.asignar_jefe(self.jefa, self.beto, self.admin)  # jefa → ana → beto → jefa


class MiEquipoVistaTests(TestCase):
    def setUp(self):
        self.jefa = Usuario.objects.create_user("jefa_v@pruebas.orbita", first_name="Julia")
        self.ana = Usuario.objects.create_user("ana_v@pruebas.orbita", first_name="Ana")
        self.ajeno = Usuario.objects.create_user("ajeno_v@pruebas.orbita")
        jefaturas.asignar_jefe(self.ana, self.jefa, None)

    def test_solo_quien_tiene_personal_a_cargo_ve_mi_equipo(self):
        self.client.force_login(self.jefa)
        pagina = self.client.get(reverse("core:mi_equipo"))
        self.assertEqual(pagina.status_code, 200)
        self.assertContains(pagina, "Ana")
        self.assertContains(pagina, reverse("core:mi_equipo"))  # opción del menú de usuario
        self.client.force_login(self.ajeno)
        self.assertEqual(self.client.get(reverse("core:mi_equipo")).status_code, 404)
        self.assertNotContains(self.client.get(reverse("core:inicio")), reverse("core:mi_equipo"))

    def test_el_detalle_solo_de_un_colaborador_directo(self):
        self.client.force_login(self.jefa)
        detalle = self.client.get(reverse("core:mi_equipo_persona", args=[self.ana.pk]))
        self.assertEqual(detalle.status_code, 200)
        self.assertEqual(detalle.context["detalle"]["persona"], self.ana)
        self.assertEqual(self.client.get(reverse("core:mi_equipo_persona", args=[self.ajeno.pk])).status_code, 404)
        self.client.force_login(self.ana)  # el colaborador no ve a su jefa
        self.assertEqual(self.client.get(reverse("core:mi_equipo_persona", args=[self.jefa.pk])).status_code, 404)

    def test_tarjeta_con_foto_y_cifras_sin_barra_al_dia(self):
        self.ana.google_foto_url = "https://lh3.googleusercontent.com/a/foto=s96-c"
        self.ana.save(update_fields=["google_foto_url"])
        resumen = jefaturas.resumen_de_equipo(self.jefa)[0]
        self.assertEqual(resumen["persona"], self.ana)
        self.assertEqual((resumen["pendientes"], resumen["vencidos"]), (0, 0))
        self.assertNotIn("al_dia", resumen)
        self.assertIsNone(resumen["proxima"])
        self.client.force_login(self.jefa)
        pagina = self.client.get(reverse("core:mi_equipo"))
        self.assertContains(pagina, "=s192-c")  # la foto de Google en el tamaño de la tarjeta
        self.assertNotContains(pagina, "% al día")  # la barra «al día» se retiró
        detalle = self.client.get(reverse("core:mi_equipo_persona", args=[self.ana.pk]))
        self.assertContains(detalle, "Sin trabajo pendiente")
