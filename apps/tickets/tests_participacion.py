"""Bloque 3 — participación colaborativa en las colecciones de un Proceso.

Un PARTICIPANTE de una colección (persona o miembro activo de un equipo, configurado en el Studio y congelado en
el ticket) crea y edita ítems de ESA colección mientras el ticket está EN ATENCIÓN, sin ser responsable del ticket:
no retira, no excluye, no cierra el alcance ni coordina gacetas, y ve solo la colección. Se entregan SIN ejecutar.
"""

from types import SimpleNamespace

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.catalogo.models import Servicio
from apps.catalogo.parrilla import configurar_participantes
from apps.core.models import Equipo, MiembroEquipo, RegistroAuditoria
from apps.tickets import parrilla as parrilla_ops
from apps.tickets.autorizacion import (
    es_participante_coleccion,
    puede_consultar_coleccion,
    puede_consultar_ticket,
    puede_editar_parrilla,
    puede_operar_coleccion,
)
from apps.tickets.models import ParticipanteParrilla
from apps.tickets.operaciones import crear_borrador, radicar_ticket, tomar_ticket
from apps.tickets.tests_inicio import CLAVE, Usuario, _BaseInicio


class _BaseParticipacion(_BaseInicio):
    def _preparar_participacion(self):
        self._preparar_inicio()
        self.participante = Usuario.objects.create_user("b3_participante", password=CLAVE)
        self.miembro = Usuario.objects.create_user("b3_miembro", password=CLAVE)
        self.ajeno = Usuario.objects.create_user("b3_ajeno", password=CLAVE)
        self.equipo = Equipo.objects.create(nombre="Equipo de contenidos B3")
        self.membresia = MiembroEquipo.objects.create(equipo=self.equipo, usuario=self.miembro)
        self.proceso, _ = self._publicar("Parrilla B3", tipo=Servicio.Tipo.PROCESO, coleccion=True)
        configurar_participantes(self.proceso, self.admin, usuarios=[self.participante], equipos=[self.equipo])

    def _ticket(self, *, tomar=True):
        ticket = crear_borrador(self.solicitante, self.proceso)
        radicar_ticket(ticket, self.solicitante)
        if tomar:
            tomar_ticket(ticket, self.atiende)
        ticket.refresh_from_db()
        return ticket, parrilla_ops.parrilla_de(ticket)

    def _columna(self, parrilla):
        return parrilla_ops.columnas_de(parrilla)[0]


class ParticipanteOperaTests(_BaseParticipacion, TestCase):
    def setUp(self):
        self._preparar_participacion()

    def test_participante_crea_edita_y_versiona_sin_ser_responsable(self):
        ticket, parrilla = self._ticket()
        columna = self._columna(parrilla)
        self.assertTrue(puede_operar_coleccion(self.participante, parrilla))
        self.assertFalse(puede_editar_parrilla(self.participante, ticket))  # no es responsable del ticket
        elemento = parrilla_ops.crear_elemento(ticket, self.participante, {columna.pk: "Publicación 1"})
        parrilla_ops.actualizar_valores(elemento, self.participante, {columna.pk: "Publicación 1 (corregida)"})
        version = parrilla_ops.registrar_version(elemento, self.participante, "Lista para revisar")
        self.assertEqual(version.creada_por, self.participante)
        ticket.refresh_from_db()
        self.assertEqual(ticket.usuario_responsable, self.atiende)  # el responsable no cambia
        # La auditoría registra al participante como actor.
        self.assertTrue(
            RegistroAuditoria.objects.filter(modelo="tickets.elementoparrilla", object_id=elemento.pk, usuario=self.participante)
            .exists()
        )

    def test_miembro_activo_de_un_equipo_participante_opera(self):
        ticket, parrilla = self._ticket()
        self.assertTrue(es_participante_coleccion(self.miembro, parrilla))
        parrilla_ops.crear_elemento(ticket, self.miembro, {self._columna(parrilla).pk: "Del equipo"})

    def test_miembro_retirado_o_equipo_inactivo_no_opera(self):
        ticket, parrilla = self._ticket()
        MiembroEquipo.objects.filter(pk=self.membresia.pk).update(activo=False)
        self.assertFalse(puede_operar_coleccion(self.miembro, parrilla))
        MiembroEquipo.objects.filter(pk=self.membresia.pk).update(activo=True)
        Equipo.objects.filter(pk=self.equipo.pk).update(activo=False)
        self.assertFalse(puede_operar_coleccion(self.miembro, parrilla))
        with self.assertRaises(PermissionDenied):
            parrilla_ops.crear_elemento(ticket, self.miembro, {})

    def test_participante_no_retira_no_excluye_ni_cierra_el_alcance(self):
        ticket, parrilla = self._ticket()
        elemento = parrilla_ops.crear_elemento(ticket, self.participante, {self._columna(parrilla).pk: "Uno"})
        with self.assertRaises(PermissionDenied):
            parrilla_ops.retirar_elemento(elemento, self.participante)
        with self.assertRaises(PermissionDenied):
            parrilla_ops.cerrar_alcance(ticket, self.participante)
        parrilla_ops.cerrar_alcance(ticket, self.atiende)  # el responsable sí
        with self.assertRaises(PermissionDenied):
            parrilla_ops.excluir_elemento(elemento, self.participante, "No va")
        # Con el alcance cerrado tampoco agrega (eso es un alta extraordinaria, del responsable).
        with self.assertRaises((PermissionDenied, ValidationError)):
            parrilla_ops.crear_elemento(ticket, self.participante, {})

    def test_participante_no_coordina_gacetas(self):
        from apps.gacetas.autorizacion import puede_coordinar

        ticket, _parrilla = self._ticket()
        gaceta_sin_coordinador = SimpleNamespace(configuracion={}, ticket=ticket)
        self.assertFalse(puede_coordinar(self.participante, gaceta_sin_coordinador))
        self.assertTrue(puede_coordinar(self.atiende, gaceta_sin_coordinador))


class AccesoDenegadoTests(_BaseParticipacion, TestCase):
    def setUp(self):
        self._preparar_participacion()

    def test_quien_no_participa_no_opera(self):
        ticket, parrilla = self._ticket()
        for usuario in (self.ajeno, self.solicitante):
            with self.subTest(usuario=usuario.username):
                self.assertFalse(puede_operar_coleccion(usuario, parrilla))
                with self.assertRaises(PermissionDenied):
                    parrilla_ops.crear_elemento(ticket, usuario, {})

    def test_solo_con_el_ticket_en_atencion(self):
        ticket, parrilla = self._ticket(tomar=False)  # RADICADO
        self.assertFalse(puede_operar_coleccion(self.participante, parrilla))
        with self.assertRaises(PermissionDenied):
            parrilla_ops.crear_elemento(ticket, self.participante, {})

    def test_un_ticket_historico_conserva_su_lista_congelada(self):
        configurar_participantes(self.proceso, self.admin, usuarios=[], equipos=[])
        ticket, parrilla = self._ticket()  # nace sin participantes
        self.assertFalse(ParticipanteParrilla.objects.filter(parrilla=parrilla).exists())
        configurar_participantes(self.proceso, self.admin, usuarios=[self.participante], equipos=[])
        self.assertFalse(puede_operar_coleccion(self.participante, parrilla))  # cambiar el Studio no lo altera
        _nuevo, parrilla_nueva = self._ticket()
        self.assertTrue(puede_operar_coleccion(self.participante, parrilla_nueva))

    def test_los_participantes_congelados_no_se_modifican(self):
        _ticket, parrilla = self._ticket()
        fila = ParticipanteParrilla.objects.filter(parrilla=parrilla).first()
        fila.usuario = self.ajeno
        with self.assertRaises(ValidationError):
            fila.save()


class ConsultaTests(_BaseParticipacion, TestCase):
    def setUp(self):
        self._preparar_participacion()

    def test_el_participante_ve_su_coleccion_y_no_el_ticket(self):
        ticket, parrilla = self._ticket()
        self.assertTrue(puede_consultar_coleccion(self.participante, parrilla))
        self.assertFalse(puede_consultar_ticket(self.participante, ticket))
        self.client.force_login(self.participante)
        pagina = self.client.get(reverse("tickets:coleccion", args=[ticket.pk, parrilla.clave]))
        self.assertEqual(pagina.status_code, 200)
        self.assertTrue(pagina.context["parrilla"]["puede_operar"])
        self.assertFalse(pagina.context["parrilla"]["puede_editar"])  # sin retirar/excluir/cerrar alcance
        self.assertEqual(self.client.get(reverse("tickets:detalle", args=[ticket.pk])).status_code, 403)

    def test_agrega_desde_su_pantalla_y_vuelve_a_ella(self):
        ticket, parrilla = self._ticket()
        self.client.force_login(self.participante)
        destino = reverse("tickets:coleccion", args=[ticket.pk, parrilla.clave])
        respuesta = self.client.post(
            reverse("tickets:elemento_agregar", args=[ticket.pk]),
            {"coleccion": parrilla.clave, f"campo_{self._columna(parrilla).pk}": "Desde la pantalla", "next": destino},
        )
        self.assertRedirects(respuesta, destino, fetch_redirect_response=False)
        self.assertEqual(parrilla_ops.elementos_vigentes(parrilla).count(), 1)

    def test_quien_no_participa_no_ve_la_coleccion(self):
        ticket, parrilla = self._ticket()
        self.client.force_login(self.ajeno)
        self.assertEqual(self.client.get(reverse("tickets:coleccion", args=[ticket.pk, parrilla.clave])).status_code, 403)

    def test_mi_trabajo_lista_la_coleccion_del_participante(self):
        ticket, parrilla = self._ticket()
        self.assertIn(parrilla, list(parrilla_ops.colecciones_de_participante(self.miembro)))
        self.assertNotIn(parrilla, list(parrilla_ops.colecciones_de_participante(self.atiende)))  # ya lo atiende
        self.client.force_login(self.participante)
        pagina = self.client.get(reverse("core:mi_trabajo"))
        self.assertContains(pagina, reverse("tickets:coleccion", args=[ticket.pk, parrilla.clave]))


class ConfiguracionEnStudioTests(_BaseParticipacion, TestCase):
    def setUp(self):
        self._preparar_participacion()

    def test_configurar_participantes_es_auditado_y_exige_activos(self):
        from apps.catalogo.parrilla import definicion_de, participantes_de

        usuarios, equipos = participantes_de(definicion_de(self.proceso))
        self.assertEqual((usuarios, equipos), ([self.participante], [self.equipo]))
        self.assertTrue(
            RegistroAuditoria.objects.filter(modelo="catalogo.definicionparrilla", usuario=self.admin).exists()
        )
        self.ajeno.is_active = False
        self.ajeno.save()
        with self.assertRaises(ValidationError):
            configurar_participantes(self.proceso, self.admin, usuarios=[self.ajeno], equipos=[])

    def test_el_studio_muestra_y_guarda_los_participantes(self):
        self.client.force_login(self.admin)
        url = reverse("catalogo:studio", args=[self.proceso.pk]) + "?tab=entrada&uso=parrilla"
        self.assertContains(self.client.get(url), 'id="participantes-coleccion"')
        self.client.post(reverse("catalogo:studio_parrilla_participantes", args=[self.proceso.pk]), {"usuarios": [self.ajeno.pk]})
        from apps.catalogo.parrilla import definicion_de, participantes_de

        self.assertEqual(participantes_de(definicion_de(self.proceso)), ([self.ajeno], []))
