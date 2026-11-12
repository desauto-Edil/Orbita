"""Demos avanzadas: Imperandina después de la gaceta (`--fase avanzar`) y «Plan de trabajo — Desarrollo de software»
(Proceso manual: el aprobador aprueba todo el plan, después la gaceta y solo entonces la ejecución)."""

import tempfile
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalogo.demo import contrato, fases
from apps.catalogo.models import ProgramacionProceso
from apps.catalogo.tests import _permiso_get_or_create
from apps.catalogo.tests_demo import CLAVE, _EscenarioDemo, _localdate
from apps.core.models import Area, Usuario
from apps.core.pruebas import correo_de_prueba


class _ConMedia:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="orbita_demo_avanzado_"))
        cls._media.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media.disable()
        super().tearDownClass()


def _paso(instancia):
    from apps.catalogo.demo import avance

    ejecucion = avance.ejecucion_en_espera(instancia)
    return avance.nombre_del_paso(ejecucion) if ejecucion is not None else instancia.estado


def _conteos_operativos():
    from apps.aprobaciones.models import Aprobacion
    from apps.gacetas.models import ObservacionGaceta, RondaRevisionGaceta
    from apps.tareas.models import Tarea
    from apps.tickets.models import Adjunto, ElementoParrilla, Ticket, VersionElemento

    return {
        modelo.__name__: modelo.objects.count()
        for modelo in (Ticket, ElementoParrilla, Tarea, Aprobacion, RondaRevisionGaceta, ObservacionGaceta, Adjunto,
                       VersionElemento)
    }


class ImperandinaAvanzadoTests(_ConMedia, _EscenarioDemo, TestCase):
    HOY = date(2026, 10, 29)

    def setUp(self):
        from apps.catalogo.demo.imperandina import ticket_de_prueba

        self._preparar()
        crear = lambda alias, **campos: Usuario.objects.create_user(correo_de_prueba(alias), **campos)  # noqa: E731
        self.lau, self.amasso, self.emm = crear("lau"), crear("amasso"), crear("emm")
        self.rbuitrago, self.scortes = crear("rbuitrago"), crear("scortes")
        self.recurso, _ = self._preparar_caso(contrato.IMPERANDINA)
        with _localdate(self.HOY):
            fases.publicar(self.recurso, self.admin)
            ticket_de_prueba(self.recurso, hoy=self.HOY)
        self.ticket = self.recurso.servicio.tickets.get().ticket

    def _avanzar(self):
        with _localdate(self.HOY):
            return fases.avanzar(self.recurso, self.admin)

    def _hijas(self):
        from apps.workflows.models import InstanciaWorkflow

        return {
            h.elemento.numero: h
            for h in InstanciaWorkflow.objects.filter(instancia_padre_id=self.ticket.instancia_workflow_id)
            .select_related("elemento")
        }

    def test_lleva_las_publicaciones_a_despues_de_la_gaceta_sin_resolver_nada(self):
        from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
        from apps.tickets import parrilla as parrilla_ops

        lineas = self._avanzar()
        self.assertIn("no se crea otro", lineas[0])
        self.assertEqual(self.recurso.servicio.tickets.count(), 1)
        pasos = {n: _paso(h) for n, h in self._hijas().items()}
        # 1 y 2: publicadas con su evidencia (recorrido terminado); 3, 4 y 5 esperan que el equipo suba la evidencia.
        self.assertEqual(pasos, {1: "COMPLETADA", 2: "COMPLETADA", 3: "Publicación", 4: "Publicación", 5: "Publicación"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.estado, "EN_ATENCION")
        gaceta = GacetaTicket.objects.get(ticket=self.ticket)
        # Nadie aprueba en la gaceta: la coordinadora cerró el periodo (todo había llegado) y eso aceptó lo que no
        # tenía comentarios pendientes. No se resuelve.
        self.assertEqual(gaceta.estado, GacetaTicket.Estado.PERIODO_CERRADO)
        self.assertTrue(any("cerrado por lau" in linea for linea in lineas))
        participaciones = {p.elemento.numero: p for p in ParticipacionItemGaceta.objects.filter(gaceta=gaceta)}
        self.assertTrue(all(p.estado == ParticipacionItemGaceta.Estado.APROBADA for p in participaciones.values()))
        # La pieza 2 tuvo una observación, su corrección (archivo v2) y una segunda ronda.
        self.assertEqual(participaciones[2].ronda, 2)
        self.assertEqual(participaciones[2].observaciones.get().autor, self.rbuitrago)
        self.assertTrue(participaciones[2].observaciones.get().respuesta)
        elemento_2 = participaciones[2].elemento
        columna = next(c for c in parrilla_ops.columnas_de(elemento_2.parrilla) if c.clave == contrato.ARCHIVO_PIEZA)
        vigentes = parrilla_ops.archivos_de(elemento_2, columna)
        self.assertEqual([a.nombre_original for a in vigentes], ["pieza-2-v2.png"])
        self.assertEqual(elemento_2.archivos.filter(campo_elemento=columna).count(), 2)  # el v1 se retiró, no se borró
        self.assertGreaterEqual(len(parrilla_ops.versiones_de(elemento_2)), 2)
        # La programación mensual no cambia.
        # Con audiovisuales (las piezas de Imperandina) la gaceta sigue siendo una galería de tarjetas con visor.
        from apps.tickets.espacio import galeria_gaceta

        galeria = galeria_gaceta(self.lau, {"gaceta": gaceta, "acciones": []})
        self.assertEqual(galeria["modo"], "visual")
        # Tras la gaceta todos ven qué sigue y las pruebas; lau (Mercadeo BPO) puede subir la evidencia de la 3.
        tarjetas = {t["elemento"].numero: t for t in galeria["tarjetas"]}
        self.assertEqual(tarjetas[3]["siguiente"]["paso"], "Publicación")
        self.assertTrue(tarjetas[3]["siguiente"]["mision"].startswith("tarea-"))
        self.assertTrue(tarjetas[1]["evidencias"][0]["completo"])
        revisora = galeria_gaceta(self.rbuitrago, {"gaceta": gaceta, "acciones": []})
        self.assertEqual({t["siguiente"]["mision"] for t in revisora["tarjetas"]}, {""})  # los interesados solo miran
        programacion = ProgramacionProceso.objects.get(servicio=self.recurso.servicio)
        self.assertEqual((programacion.activa, programacion.dia_creacion), (True, 28))
        self.assertEqual(fases.discrepancias(self.recurso), [])

    def test_las_acciones_las_hacen_las_personas_asignadas(self):
        from apps.tareas.models import Tarea
        from apps.workflows.models import TareaWorkflow

        self._avanzar()
        completadas = Tarea.objects.filter(
            pk__in=TareaWorkflow.objects.filter(
                instancia_etapa__instancia_workflow__instancia_padre_id=self.ticket.instancia_workflow_id
            ).values("tarea_id"),
            estado="COMPLETADA",
        )
        self.assertEqual({t.completada_por.email.split("@")[0] for t in completadas}, {"emm"})  # producción y publicación
        abiertas = Tarea.objects.filter(titulo__startswith="Publicación", estado__in=("PENDIENTE", "EN_PROGRESO"))
        self.assertEqual(abiertas.count(), 3)
        # Del equipo (nadie la toma) y responde emm.
        self.assertTrue(all(t.equipo_responsable is not None and t.usuario_responsable is None for t in abiertas))
        self.assertEqual({t.responde_por.email.split("@")[0] for t in abiertas}, {"emm"})

    def test_repetir_no_duplica_operaciones(self):
        self._avanzar()
        antes = _conteos_operativos()
        lineas = self._avanzar()
        self.assertEqual(_conteos_operativos(), antes)
        self.assertTrue(all("pendiente de" in l or "publicada" in l for l in lineas if l.startswith("  #")))

    def test_conserva_los_avances_manuales(self):
        from apps.catalogo.demo import avance

        self._avanzar()
        # Alguien publica a mano la pieza 3: al repetir queda donde la dejaron (más adelante que su objetivo).
        from apps.tickets import evidencias
        from apps.tickets import parrilla as parrilla_ops

        hija = self._hijas()[3]
        tarea = avance.tarea_de(avance.ejecucion_en_espera(hija))
        lista = next(c for c in parrilla_ops.columnas_de(hija.elemento.parrilla) if c.clave == contrato.EVIDENCIA_PUBLICACION)
        evidencias.marcar_punto_elemento(hija.elemento, lista, contrato.PUNTO_PUBLICADA, self.emm, enlace="https://ejemplo.com/p/3")
        avance.completar(tarea, self.emm, verificar=False)
        lineas = self._avanzar()
        self.assertEqual(_paso(self._hijas()[3]), "COMPLETADA")
        self.assertIn("se conserva", next(l for l in lineas if l.startswith("  #3")))

    def test_continua_desde_un_estado_manual_previo(self):
        from apps.catalogo.demo import avance
        from apps.workflows import recorridos

        # A mano: la lluvia se envió y la idea 5 se devolvió («Ajustar la idea» queda pendiente de Laura).
        recorridos.enviar_conjunto(self.ticket, self.ticket.parrilla, self.lau)
        hija = self._hijas()[5]
        avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(hija)), "DEVUELTA", "Cambiar el copy")
        self.assertEqual(_paso(hija), "Ajustar la idea")
        self._avanzar()
        self.assertEqual(_paso(self._hijas()[5]), "Publicación")

    def test_solo_aplica_a_imperandina(self):
        recurso, _ = self._preparar_caso(contrato.PARRILLA)
        self.assertIn("solo aplica", fases.avanzar(recurso, self.admin)[0])


class DesarrolloDemoTests(_ConMedia, _EscenarioDemo, TestCase):
    HOY = date(2026, 11, 9)

    def setUp(self):
        self._preparar()
        crear = lambda alias, **campos: Usuario.objects.create_user(correo_de_prueba(alias), **campos)  # noqa: E731
        # Las personas FIJAS del contrato: ylizz desarrolla, amasso aprueba, scortes solicita y revisa el plan.
        self.dev = crear("ylizz")
        self.jefe = crear("amasso")
        self.scortes = crear("scortes")
        _permiso_get_or_create(self.dev, "tickets.atender", "Rol demo atender desarrollo")
        self.tecnologia = Area.objects.create(nombre="Tecnología demo", codigo="TI-DEMO")

    def _opciones_desarrollo(self, **extra):
        return self._opciones(**extra)

    def _listo(self):
        recurso, _ = self._preparar_caso(contrato.DESARROLLO, **self._opciones_desarrollo())
        with _localdate(self.HOY):
            fases.publicar(recurso, self.admin)
        return recurso

    def _ejemplo(self, recurso):
        with _localdate(self.HOY):
            return fases.ejemplos(recurso, self.admin, area=self.tecnologia, hoy=self.HOY)  # solicitante del contrato

    def _principal(self, ticket):
        from apps.workflows.models import InstanciaWorkflow

        ticket.refresh_from_db()
        return InstanciaWorkflow.objects.get(pk=ticket.instancia_workflow_id)

    def _de_ejecucion(self, ticket):
        from apps.workflows.models import InstanciaWorkflow

        return list(InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_EJECUCION,
        ).select_related("elemento"))

    def test_no_entra_en_los_casos_por_defecto_y_exige_sus_personas(self):
        self.assertNotIn("desarrollo", contrato.ALIAS_POR_DEFECTO)
        antes = self._conteos()
        self.scortes.is_active = False
        self.scortes.save(update_fields=["is_active"])
        with self.assertRaisesMessage(ValidationError, "scortes"):
            self._preparar_caso(contrato.DESARROLLO, **self._opciones())
        self.assertEqual(self._conteos(), antes)

    def test_usa_el_equipo_desarrollo_existente_sin_duplicarlo(self):
        from apps.catalogo.models import ServicioResponsable
        from apps.core.models import Equipo, MiembroEquipo

        equipo = Equipo.objects.create(nombre="Desarrollo")
        for usuario in (self.dev, self.jefe):
            MiembroEquipo.objects.create(equipo=equipo, usuario=usuario, fecha_inicio=self.HOY)
        recurso, _ = self._preparar_caso(contrato.DESARROLLO, **self._opciones())
        self.assertEqual(Equipo.objects.filter(nombre__iexact="Desarrollo").count(), 1)
        self.assertEqual(MiembroEquipo.objects.filter(equipo=equipo).count(), 2)
        responsable = ServicioResponsable.objects.get(servicio=recurso.servicio, activo=True)
        self.assertEqual(responsable.equipo, equipo)

    def test_prepara_un_proceso_manual_que_coincide_y_se_publica(self):
        recurso, creado = self._preparar_caso(contrato.DESARROLLO, **self._opciones_desarrollo())
        self.assertTrue(creado)
        self.assertEqual(recurso.servicio.tipo, "PROCESO")
        self.assertEqual(fases.discrepancias(recurso), [])
        publicable, errores = fases.simular_publicacion(recurso, self.admin)
        self.assertTrue(publicable, errores)
        with _localdate(self.HOY):
            fases.publicar(recurso, self.admin)
        recurso.servicio.refresh_from_db()
        self.assertTrue(recurso.servicio.activo)
        self.assertFalse(ProgramacionProceso.objects.filter(servicio=recurso.servicio).exists())  # manual

    def _del_plan(self, ticket):
        from apps.workflows.models import InstanciaWorkflow

        return list(InstanciaWorkflow.objects.filter(
            instancia_padre_id=ticket.instancia_workflow_id,
            ejecucion_padre__bloque_operativo__nombre=contrato.RECORRIDO_PLAN,
        ).select_related("elemento").order_by("elemento__numero"))

    def _avanzar(self, recurso):
        with _localdate(self.HOY):
            return fases.avanzar(recurso, self.admin)

    def test_el_ejemplo_deja_el_plan_completo_esperando_al_aprobador(self):
        from apps.aprobaciones.models import Aprobacion
        from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta
        from apps.tickets import parrilla as parrilla_ops

        recurso = self._listo()
        self._ejemplo(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        self.assertEqual((ticket.estado, ticket.solicitante, ticket.usuario_responsable),
                         ("EN_ATENCION", self.scortes, self.dev))
        self.assertEqual(ticket.origen, "MANUAL")
        parrilla = parrilla_ops.parrilla_de(ticket)
        self.assertTrue(parrilla.alcance_cerrado)
        self.assertEqual(parrilla.elementos.count(), len(contrato.PLAN_ORBITA))
        # Sin aprobación inicial: ylizz revisó la solicitud y el plan espera a amasso, actividad por actividad.
        self.assertEqual({_paso(h) for h in self._del_plan(ticket)}, {"Aprobación de la actividad"})
        self.assertEqual(
            Aprobacion.objects.filter(aprobador_usuario=self.jefe, estado="PENDIENTE").count(), len(contrato.PLAN_ORBITA)
        )
        self.assertFalse(Aprobacion.objects.exclude(estado="PENDIENTE").exists())
        # La gaceta ni se abrió.
        self.assertEqual(GacetaTicket.objects.get(ticket=ticket).estado, "PENDIENTE")
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta__ticket=ticket).exists())
        # amasso ve el ticket para aprobar.
        self.client.force_login(self.jefe)
        self.assertEqual(self.client.get(f"/tickets/{ticket.pk}/").status_code, 200)
        # Repetir no crea otro ticket ni duplica operaciones.
        antes = _conteos_operativos()
        lineas = self._ejemplo(recurso)
        self.assertIn("reutiliza", lineas[0])
        self.assertEqual(_conteos_operativos(), antes)

    def test_lo_enviado_a_revision_no_se_edita_hasta_que_lo_devuelven(self):
        """2026-11-12: enviado a revisión = solo lectura para TODOS; devuelto con correcciones, se vuelve a editar."""
        from apps.aprobaciones.decisiones import aplicar
        from apps.aprobaciones.models import Aprobacion
        from apps.tickets import en_revision, espacio
        from apps.tickets import parrilla as parrilla_ops

        recurso = self._listo()
        self._ejemplo(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        parrilla = parrilla_ops.parrilla_de(ticket)
        item = parrilla.elementos.order_by("numero").first()
        columna = next(c for c in parrilla_ops.columnas_de(parrilla) if c.tipo in ("TEXTO", "TEXTO_LARGO"))
        self.assertTrue(en_revision.motivo(item))
        # Ni quien atiende el ticket (responsable individual) puede cambiarlo mientras espera la aprobación.
        with self.assertRaisesMessage(ValidationError, "en revisión"):
            parrilla_ops.actualizar_valores(item, self.dev, {columna.pk: "Cambio a escondidas"})
        ficha = espacio.ficha(self.dev, ticket, item)
        self.assertFalse(ficha["opera"])
        self.assertIsNone(ficha["celdas"])
        self.assertTrue(ficha["en_revision"])
        self.assertTrue(next(f for f in parrilla_ops.tabla(ticket, self.dev)["filas"] if f["elemento"] == item)["en_revision"])
        # Devuelto con correcciones: deja de estar en revisión y quien debe ajustarlo vuelve a editar.
        decidida = Aprobacion.objects.get(
            aprobador_usuario=self.jefe, estado="PENDIENTE",
            esquema__vinculo_workflow__instancia_etapa__instancia_workflow__elemento=item,
        )
        aplicar(decidida, self.jefe, decision=Aprobacion.Estado.DEVUELTA, observacion="Ajusta el alcance.")
        item.refresh_from_db()
        self.assertIsNone(en_revision.motivo(item))
        parrilla_ops.actualizar_valores(item, self.dev, {columna.pk: "Alcance ajustado"})
        # Aprobado: tampoco se edita desde la colección (solo una tarea abierta de un paso posterior lo cambiaría).
        otro = parrilla.elementos.order_by("numero")[1]
        aplicar(
            Aprobacion.objects.get(
                aprobador_usuario=self.jefe, estado="PENDIENTE",
                esquema__vinculo_workflow__instancia_etapa__instancia_workflow__elemento=otro,
            ),
            self.jefe, decision=Aprobacion.Estado.APROBADA, observacion="De acuerdo.",
        )
        self.assertTrue(en_revision.aprobado(otro))
        with self.assertRaisesMessage(ValidationError, "Ya está aprobado"):
            parrilla_ops.actualizar_valores(otro, self.dev, {columna.pk: "Cambio después de aprobado"})
        self.assertFalse(espacio.ficha(self.dev, ticket, otro)["opera"])

    def test_el_plan_se_revisa_y_comenta_en_una_lista_compacta(self):
        """Sin audiovisuales (un plan de trabajo) la revisión del aprobador y la gaceta muestran la INFORMACIÓN en una
        lista compacta, no tarjetas con portada; en la gaceta nadie aprueba: solo se comenta."""
        recurso = self._listo()
        self._ejemplo(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        self.client.force_login(self.jefe)
        pagina = self.client.get(f"/tickets/{ticket.pk}/")
        self.assertEqual(pagina.context["revision_datos"]["modo"], "formulario")  # 7 columnas: lista compacta
        self.assertContains(pagina, 'class="pi-lista"')
        self.assertNotContains(pagina, 'class="pz-rejilla"')
        self._avanzar(recurso)
        self.client.force_login(self.scortes)
        pagina = self.client.get(f"/tickets/{ticket.pk}/")
        galeria = pagina.context["galerias"][0]
        self.assertEqual(galeria["modo"], "datos")
        self.assertContains(pagina, 'class="pi-lista"')
        self.assertContains(pagina, "Comentar")
        self.assertTrue(all(t["decision"] is None and t["salidas"] == [] for t in galeria["tarjetas"]))
        primera = next(t for t in galeria["tarjetas"] if t["puede_observar"])
        self.assertTrue(primera["valores"])  # la información se ve en la fila, completa
        self.assertContains(pagina, "Levantamiento de requisitos")

    def test_el_plan_se_avisa_completo_y_no_actividad_por_actividad(self):
        from apps.notificaciones.models import Notificacion

        recurso = self._listo()
        with self.captureOnCommitCallbacks(execute=True):
            self._ejemplo(recurso)
        turnos = Notificacion.objects.filter(destinatario=self.jefe, evento="APROBACION_TURNO")
        self.assertEqual(turnos.count(), 1)  # el plan se envió completo: UN aviso, no uno por actividad
        self.assertEqual(turnos.get().cantidad, len(contrato.PLAN_ORBITA))
        self.assertIn(f"{len(contrato.PLAN_ORBITA)} aprobaciones pendientes", turnos.get().titulo)
        self.assertFalse(Notificacion.objects.filter(evento="RECORRIDO_COMPLETADO").exists())
        with self.captureOnCommitCallbacks(execute=True):
            self._avanzar(recurso)
        completo = Notificacion.objects.filter(destinatario=self.dev, evento="RECORRIDO_COMPLETADO")
        self.assertEqual(completo.count(), 1)  # todo el plan aprobado: un aviso al responsable
        self.assertIn(contrato.RECORRIDO_PLAN, completo.get().titulo)
        # En la gaceta nadie aprueba: el solicitante solo recibe la apertura, nunca «es tu turno de aprobar».
        self.assertFalse(Notificacion.objects.filter(destinatario=self.scortes, evento="APROBACION_TURNO").exists())
        self.assertTrue(Notificacion.objects.filter(destinatario=self.scortes, evento="GACETA_ABIERTA").exists())

    def test_avanzar_aprueba_el_plan_y_lo_lleva_a_la_gaceta(self):
        from apps.aprobaciones.models import Aprobacion
        from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta

        recurso = self._listo()
        self._ejemplo(recurso)
        self._avanzar(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        self.assertTrue(all(h.estado == "COMPLETADA" for h in self._del_plan(ticket)))
        self.assertEqual(Aprobacion.objects.filter(aprobador_usuario=self.jefe, estado="DEVUELTA").count(), 1)
        gaceta = GacetaTicket.objects.get(ticket=ticket)
        self.assertEqual(gaceta.estado, "ABIERTA")
        # amasso no participa en la gaceta: solo scortes, que comenta (nadie aprueba en una gaceta).
        self.assertNotIn("aprobadores", gaceta.configuracion)
        self.assertEqual([a["usuario_id"] for a in gaceta.configuracion["revisores"]], [self.scortes.pk])
        estados = list(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).values_list("estado", flat=True))
        self.assertEqual((estados.count("EN_REVISION"), estados.count("EN_CORRECCION")), (9, 1))
        self.assertEqual(_paso(self._principal(ticket)), contrato.REVISION_PLAN)
        self.assertEqual(self._de_ejecucion(ticket), [])
        antes = _conteos_operativos()
        self._avanzar(recurso)
        self.assertEqual(_conteos_operativos(), antes)

    def test_la_gaceta_se_abre_solo_con_todo_el_plan_aprobado(self):
        from apps.catalogo.demo import avance
        from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta

        recurso = self._listo()
        self._ejemplo(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        hijas = self._del_plan(ticket)
        for hija in hijas[:-1]:
            self.assertEqual(
                avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(hija)), "APROBADA", "Bien."),
                [self.jefe],
            )
        # Falta UNA aprobación: la gaceta ni se abre ni recibe nada.
        self.assertEqual(GacetaTicket.objects.get(ticket=ticket).estado, "PENDIENTE")
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta__ticket=ticket).exists())
        ultima = hijas[-1]
        avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(ultima)), "DEVUELTA", "Ajusta la fecha.")
        self.assertEqual(_paso(ultima), "Ajustar la actividad")
        self.assertFalse(ParticipacionItemGaceta.objects.filter(gaceta__ticket=ticket).exists())
        avance.completar(avance.tarea_de(avance.ejecucion_en_espera(ultima)), self.dev, elemento=ultima.elemento)
        avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(ultima)), "APROBADA", "Bien.")
        # Todo aprobado: el plan completo entra a la gaceta de una vez.
        self.assertEqual(GacetaTicket.objects.get(ticket=ticket).estado, "ABIERTA")
        self.assertEqual(
            ParticipacionItemGaceta.objects.filter(gaceta__ticket=ticket, estado="EN_REVISION").count(),
            len(contrato.PLAN_ORBITA),
        )
        self.assertEqual(_paso(self._principal(ticket)), contrato.REVISION_PLAN)

    def test_silencio_positivo_y_la_ejecucion_empieza_solo_al_resolver_la_gaceta(self):
        from apps.catalogo.demo import avance
        from apps.gacetas import operaciones as gacetas_ops
        from apps.gacetas.models import GacetaTicket, ParticipacionItemGaceta, RondaRevisionGaceta
        from apps.tareas.models import Tarea
        from apps.tickets import parrilla as parrilla_ops

        recurso = self._listo()
        self._ejemplo(recurso)
        self._avanzar(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        gaceta = GacetaTicket.objects.get(ticket=ticket)
        comentada = ParticipacionItemGaceta.objects.get(gaceta=gaceta, estado="EN_CORRECCION")
        # Vence el periodo sin más comentarios: el Sistema lo cierra y acepta lo que no tiene observaciones.
        GacetaTicket.objects.filter(pk=gaceta.pk).update(vence_revision_en=timezone.now() - timedelta(minutes=1))
        gacetas_ops.cerrar_periodo(gaceta)
        estados = list(ParticipacionItemGaceta.objects.filter(gaceta=gaceta).values_list("estado", flat=True))
        self.assertEqual((estados.count("APROBADA"), estados.count("EN_CORRECCION")), (9, 1))
        # Lo comentado nunca se acepta solo: sin resolver, nada se ejecuta.
        gaceta.refresh_from_db()
        self.assertEqual(gaceta.estado, "PERIODO_CERRADO")
        self.assertEqual(self._de_ejecucion(ticket), [])
        self.assertFalse(Tarea.objects.filter(titulo__icontains="Ejecución de la actividad").exists())
        # ylizz corrige y reenvía con el periodo ya cerrado: la versión corregida queda aceptada (nadie aprueba en
        # la gaceta), la gaceta se resuelve sola y empieza la ejecución.
        ronda = RondaRevisionGaceta.objects.get(participacion=comentada, numero=comentada.ronda)
        avance.completar(ronda.tarea_correccion, self.dev, verificar=False)
        comentada.refresh_from_db()
        self.assertEqual(comentada.estado, "APROBADA")
        self.assertIsNone(RondaRevisionGaceta.objects.get(participacion=comentada, numero=comentada.ronda).esquema)
        gaceta.refresh_from_db()
        self.assertEqual(gaceta.estado, "RESUELTA")
        hijas = self._de_ejecucion(ticket)
        self.assertEqual(len(hijas), len(contrato.PLAN_ORBITA))
        for hija in hijas:
            self.assertEqual(_paso(hija), "Ejecución de la actividad")
            tarea = avance.tarea_de(avance.ejecucion_en_espera(hija))
            valores = parrilla_ops.valores_de(hija.elemento)
            self.assertEqual(tarea.usuario_responsable, self.dev)
            self.assertEqual(timezone.localtime(tarea.fecha_limite).date(), valores[contrato.ACT_FECHA_LIMITE])
        # Aprobación individual: devolver regresa a ejecución solo esa actividad.
        primera, segunda = hijas[0], hijas[1]
        avance.completar(avance.tarea_de(avance.ejecucion_en_espera(primera)), self.dev)
        self.assertEqual(_paso(primera), "Aprobación del resultado")
        avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(primera)), "DEVUELTA", "Falta el informe.")
        self.assertEqual(_paso(primera), "Ejecución de la actividad")
        avance.completar(avance.tarea_de(avance.ejecucion_en_espera(segunda)), self.dev)
        avance.decidir(avance.esquema_del_paso(avance.ejecucion_en_espera(segunda)), "APROBADA", "Listo.")
        segunda.refresh_from_db()
        self.assertEqual(segunda.estado, "COMPLETADA")
        self.assertEqual(_paso(self._principal(ticket)), contrato.RECORRIDO_EJECUCION)

    def test_el_espacio_del_ticket_abre_para_quien_atiende(self):
        recurso = self._listo()
        self._ejemplo(recurso)
        ticket = recurso.servicio.tickets.get().ticket
        self.client.force_login(self.dev)
        respuesta = self.client.get(f"/tickets/{ticket.pk}/")
        self.assertEqual(respuesta.status_code, 200)
