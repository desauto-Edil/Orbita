"""Bloque 2 — coherencia del Studio y la publicación.

La navegación de las cinco secciones, el panel de Formulario y «Revisión y publicación» leen la MISMA evaluación
(`apps.catalogo.evaluacion`): estado vigente, estado preparado (borradores activados) y bloqueos reales, obtenidos
simulando la publicación sin dejar efectos. Se entregan SIN ejecutar.
"""

from django.test import TestCase
from django.urls import reverse

from apps.catalogo import evaluacion, publicacion
from apps.catalogo.configuracion_ejecucion import (
    activar_configuracion_ejecucion,
    preparar_configuracion_ejecucion,
)
from apps.catalogo.models import (
    Campo,
    ConfiguracionEjecucionVersion,
    Formulario,
    FormularioVersion,
    ProgramacionProceso,
    Servicio,
    ServicioResponsable,
)
from apps.catalogo.operaciones import MENSAJE_SIN_ACCESO, MENSAJE_SIN_ATENCION, activar_servicio
from apps.catalogo.parrilla import agregar_coleccion
from apps.catalogo.studio import _estado_secciones
from apps.catalogo.tests_fase4a import CLAVE, Usuario, _Base4A
from apps.catalogo.versionamiento import activar_version, crear_nueva_version
from apps.core.models import AsignacionRol, RegistroAuditoria


class _BaseCoherencia(_Base4A):
    def _formulario(self, nombre, campos=1, *, activar=True):
        formulario = Formulario.objects.create(nombre=nombre)
        version = crear_nueva_version(formulario, self.admin)
        for orden in range(1, campos + 1):
            Campo.objects.create(version=version, tipo="TEXTO", etiqueta=f"Campo {orden}", orden=orden)
        if activar:
            activar_version(formulario, version, self.admin)
        return formulario

    def _servicio_simple(self, nombre, *, formulario=None):
        return Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, tipo=Servicio.Tipo.SERVICIO, formulario=formulario,
            alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO, activo=False,
        )

    def _proceso(self, nombre, *, formulario=None, flujo="activo"):
        """`flujo`: "activo" (configuración activa válida), "borrador" (solo un borrador válido) o None."""
        workflow = self._flujo(f"Flujo {nombre}") if flujo else None
        proceso = Servicio.objects.create(
            nombre=nombre, categoria=self.categoria, tipo=Servicio.Tipo.PROCESO, workflow=workflow,
            formulario=formulario, alcance_visibilidad=Servicio.AlcanceVisibilidad.PUBLICO_INTERNO, activo=False,
        )
        if flujo:
            config = preparar_configuracion_ejecucion(proceso, self.admin)
            self._actividad(config, self._fase(workflow, "Recepción"), "Atender")
            if flujo == "activo":
                activar_configuracion_ejecucion(proceso, config, self.admin)
        proceso.refresh_from_db()
        return proceso

    def _coleccion(self, proceso, nombre, *, columnas=1, activar=True):
        definicion = agregar_coleccion(proceso, self.admin, nombre_elemento=nombre)
        version = definicion.formulario.versiones.get(numero=1)
        for orden in range(1, columnas + 1):
            Campo.objects.create(version=version, tipo="TEXTO", etiqueta=f"Columna {orden}", orden=orden)
        if activar:
            activar_version(definicion.formulario, version, self.admin)
        return definicion

    def _evaluar(self, servicio):
        servicio.refresh_from_db()
        return evaluacion.evaluar(servicio, self.admin)

    def _estados(self, servicio):
        ev = self._evaluar(servicio)
        return {s["clave"]: s for s in _estado_secciones(servicio, ev)}, ev

    def _publicar_pagina(self, servicio):
        return self.client.get(f"{reverse('catalogo:studio', args=[servicio.pk])}?tab=publicacion")


class ServicioCoherenciaTests(_BaseCoherencia, TestCase):
    def setUp(self):
        self._preparar()

    def test_1_servicio_con_formulario_activo(self):
        servicio = self._servicio_simple("Activo", formulario=self._formulario("Entrada activa"))
        estados, ev = self._estados(servicio)
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertTrue(ev.publicable)
        self.assertEqual(estados["publicacion"]["texto"], "Listo para publicar")

    def test_2_servicio_con_formulario_en_borrador_valido(self):
        servicio = self._servicio_simple("Borrador", formulario=self._formulario("Entrada borrador", activar=False))
        estados, ev = self._estados(servicio)
        self.assertEqual(estados["entrada"]["estado"], "progress")  # cambios sin activar, no un error
        self.assertTrue(ev.publicable)
        self.assertTrue(ev.vigentes)  # lo vigente aún no se podría publicar…
        self.assertIn("Formulario de inicio v1", ev.activaria)  # …pero al activar el borrador sí
        pagina = self._publicar_pagina(servicio)
        self.assertTrue(pagina.context["publicable"])
        self.assertEqual(pagina.context["errores_por_seccion"], [])

    def test_3_servicio_sin_formulario(self):
        servicio = self._servicio_simple("Sin formulario")
        estados, ev = self._estados(servicio)
        self.assertEqual(estados["entrada"]["estado"], "empty")
        self.assertFalse(ev.publicable)
        self.assertIn("entrada", ev.por_seccion)


class ProcesoFormularioCoherenciaTests(_BaseCoherencia, TestCase):
    def setUp(self):
        self._preparar()

    def test_4_proceso_sin_formulario_ni_colecciones(self):
        estados, ev = self._estados(self._proceso("Vacío"))
        self.assertEqual(estados["entrada"]["estado"], "optional")
        self.assertTrue(ev.publicable)

    def test_5_proceso_con_solo_formulario_activo(self):
        estados, ev = self._estados(self._proceso("Solo datos", formulario=self._formulario("Datos", 2)))
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertIn("2 campos", estados["entrada"]["texto"])
        self.assertTrue(ev.publicable)

    def test_6_proceso_con_solo_coleccion_activa(self):
        proceso = self._proceso("Solo colección")
        self._coleccion(proceso, "Pieza")
        estados, ev = self._estados(proceso)
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertIn("1 colección", estados["entrada"]["texto"])
        self.assertTrue(ev.publicable)

    def test_7_proceso_con_varias_colecciones_las_evalua_todas(self):
        proceso = self._proceso("Varias")
        self._coleccion(proceso, "Pieza")
        self._coleccion(proceso, "Canal")
        estados, ev = self._estados(proceso)
        self.assertIn("2 colecciones", estados["entrada"]["texto"])
        self.assertEqual([c.nombre for c in ev.colecciones], ["Pieza", "Canal"])
        pagina = self.client.get(f"{reverse('catalogo:studio', args=[proceso.pk])}?tab=entrada")
        self.assertEqual(len(pagina.context["tarjetas_proceso"]["colecciones"]), 2)
        self.assertContains(pagina, "«Canal»")

    def test_8_coleccion_activa_y_otra_en_borrador(self):
        proceso = self._proceso("Activa y borrador")
        self._coleccion(proceso, "Pieza")
        self._coleccion(proceso, "Canal", activar=False)  # borrador VÁLIDO (con columna)
        estados, ev = self._estados(proceso)
        self.assertEqual(estados["entrada"]["estado"], "complete")
        self.assertIn("cambios sin activar", estados["entrada"]["texto"])
        self.assertTrue(ev.publicable)
        self.assertTrue(any("Canal" in m for m in ev.vigentes))  # vigente: sin versión activa
        pagina = self._publicar_pagina(proceso)
        self.assertEqual(pagina.context["errores_por_seccion"], [])
        # Un borrador INCOMPLETO (sin columnas) sí bloquea: la sección queda en progreso y Publicar dice dónde.
        self._coleccion(proceso, "Formato", columnas=0, activar=False)
        estados, ev = self._estados(proceso)
        self.assertEqual(estados["entrada"]["estado"], "progress")
        self.assertFalse(ev.publicable)
        self.assertIn("elementos", ev.por_seccion)
        self.assertEqual(estados["publicacion"]["estado"], "attention")

    def test_9_formulario_tecnico_vacio_no_cuenta_como_configurado(self):
        proceso = self._proceso("Técnico")
        activar_servicio(proceso, self.admin)
        proceso.refresh_from_db()
        self.assertIsNotNone(proceso.formulario_id)  # existe el técnico vacío…
        estados, ev = self._estados(proceso)
        self.assertFalse(ev.datos_inicio.configurada)  # …pero no es una entrada configurada
        self.assertEqual(estados["entrada"]["estado"], "optional")
        self.assertNotIn("formulario", [p.clave for p in publicacion.piezas(proceso)])


class FlujoCoherenciaTests(_BaseCoherencia, TestCase):
    def setUp(self):
        self._preparar()

    def test_10_flujo_obligatorio_ausente(self):
        estados, ev = self._estados(self._proceso("Sin flujo", flujo=None))
        self.assertEqual(estados["ejecucion"]["estado"], "empty")
        self.assertFalse(ev.publicable)
        self.assertIn("ejecucion", ev.por_seccion)

    def test_11_flujo_con_borrador_valido(self):
        proceso = self._proceso("Flujo borrador", flujo="borrador")
        estados, ev = self._estados(proceso)
        self.assertEqual(estados["ejecucion"]["estado"], "progress")
        self.assertEqual(estados["ejecucion"]["texto"], "Lista para activar al publicar")
        self.assertTrue(ev.publicable)
        self.assertEqual(
            ConfiguracionEjecucionVersion.objects.filter(servicio=proceso, estado="BORRADOR").count(), 1
        )  # consultar no activó nada

    def test_servicio_sin_flujo_es_opcional(self):
        servicio = self._servicio_simple("Sin flujo", formulario=self._formulario("Entrada"))
        estados, _ev = self._estados(servicio)
        self.assertEqual(estados["ejecucion"]["estado"], "optional")


class AccesoYAtencionTests(_BaseCoherencia, TestCase):
    def setUp(self):
        self._preparar()

    def _sin_atencion_global(self):
        AsignacionRol.objects.filter(usuario=self.atencion_global).update(activo=False)

    def test_12_visibilidad_y_responsables(self):
        servicio = self._servicio_simple("Acceso", formulario=self._formulario("Entrada"))
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="RESTRINGIDO")
        estados, ev = self._estados(servicio)
        self.assertIn(MENSAJE_SIN_ACCESO, ev.bloqueos)
        self.assertEqual(estados["general"]["estado"], "progress")
        Servicio.objects.filter(pk=servicio.pk).update(alcance_visibilidad="PUBLICO_INTERNO")
        # Sin nadie con alcance global ni responsables con permiso de atender: tampoco se publica.
        self._sin_atencion_global()
        _estados, ev = self._estados(servicio)
        self.assertIn(MENSAJE_SIN_ATENCION, ev.bloqueos)
        # Un EQUIPO responsable basta si alguno de sus miembros puede atender (sin persona individual).
        from apps.core.models import Area, Equipo, MiembroEquipo, Permiso, RolFuncional, RolPermiso

        area = Area.objects.create(nombre="Área coherencia")
        miembro = Usuario.objects.create_user("coherencia_miembro", password=CLAVE)
        equipo = Equipo.objects.create(nombre="Equipo coherencia")
        MiembroEquipo.objects.create(equipo=equipo, usuario=miembro)
        permiso, _ = Permiso.objects.get_or_create(codigo="tickets.atender", defaults={"nombre": "Atender"})
        rol = RolFuncional.objects.create(nombre="Rol área coherencia")
        RolPermiso.objects.create(rol=rol, permiso=permiso)
        AsignacionRol.objects.create(usuario=miembro, rol=rol, tipo_alcance=AsignacionRol.TipoAlcance.AREA, area=area)
        ServicioResponsable.objects.create(servicio=servicio, tipo_responsable="EQUIPO", equipo=equipo)
        estados, ev = self._estados(servicio)
        self.assertNotIn(MENSAJE_SIN_ATENCION, ev.bloqueos)
        self.assertTrue(ev.publicable)
        self.assertEqual(estados["general"]["estado"], "complete")

    def test_13_proceso_programado_no_exige_visibilidad_y_si_responsable_inicial(self):
        from apps.catalogo.programacion import asegurar_responsable_inicial, configurar_programacion

        proceso = self._proceso("Programado")
        Servicio.objects.filter(pk=proceso.pk).update(alcance_visibilidad="RESTRINGIDO")
        self._sin_atencion_global()
        responsable = asegurar_responsable_inicial(proceso, self.admin, (ServicioResponsable.TipoResponsable.USUARIO, self.persona))
        configurar_programacion(
            proceso, self.admin, dia_creacion=1, periodo=ProgramacionProceso.Periodo.MES_ACTUAL,
            responsable_inicial=responsable,
        )
        _estados, ev = self._estados(proceso)
        self.assertNotIn(MENSAJE_SIN_ACCESO, ev.bloqueos)
        self.assertNotIn(MENSAJE_SIN_ATENCION, ev.bloqueos)
        self.assertTrue(ev.publicable)
        self.persona.is_active = False
        self.persona.save()
        _estados, ev = self._estados(proceso)
        self.assertFalse(ev.publicable)
        self.assertIn("general", ev.por_seccion)


class NavegacionVsPublicarTests(_BaseCoherencia, TestCase):
    def setUp(self):
        self._preparar()

    def test_14_la_navegacion_coincide_con_publicar(self):
        casos = [self._proceso("Listo"), self._proceso("Sin flujo", flujo=None)]
        incompleto = self._proceso("Colección incompleta")
        self._coleccion(incompleto, "Vacía", columnas=0, activar=False)
        casos.append(incompleto)
        for servicio in casos:
            with self.subTest(servicio=servicio.nombre):
                pagina = self._publicar_pagina(servicio)
                secciones = {s["clave"]: s for s in pagina.context["secciones"]}
                publicable = pagina.context["publicable"]
                self.assertEqual(secciones["publicacion"]["estado"] == "attention", not publicable)
                grupos = {g["seccion"] for g in pagina.context["errores_por_seccion"]}
                for clave, tab in (("ejecucion", "ejecucion"), ("salida", "salida")):
                    if secciones[tab]["estado"] == "attention":
                        self.assertIn(clave, grupos)
                if secciones["entrada"]["estado"] == "attention":
                    self.assertTrue({"entrada", "elementos"} & grupos)
                # Ninguna fila «Requiere atención» del resumen sin un bloqueo real.
                if publicable:
                    self.assertFalse([f for f in pagina.context["verificacion"] if f["estado"] == "atencion"])

    def test_15_la_simulacion_no_deja_efectos(self):
        proceso = self._proceso("Simulación", flujo="borrador")
        borrador_coleccion = self._coleccion(proceso, "Pieza", activar=False)
        auditoria = RegistroAuditoria.objects.count()
        self._publicar_pagina(proceso)
        self._evaluar(proceso)
        proceso.refresh_from_db()
        self.assertFalse(proceso.activo)
        self.assertIsNone(proceso.formulario_id)  # no se creó el formulario técnico
        columnas = Formulario.objects.get(pk=borrador_coleccion.formulario_id)
        self.assertTrue(columnas.versiones.filter(estado=FormularioVersion.Estado.BORRADOR).exists())
        self.assertIsNone(columnas.version_activa_id)
        self.assertTrue(ConfiguracionEjecucionVersion.objects.filter(servicio=proceso, estado="BORRADOR").exists())
        self.assertEqual(RegistroAuditoria.objects.count(), auditoria)

    def test_16_compatibilidad_con_tickets_historicos(self):
        from apps.tickets.operaciones import crear_borrador, radicar_ticket

        proceso = self._proceso("Histórico", formulario=self._formulario("Datos históricos"))
        activar_servicio(proceso, self.admin)
        ticket = crear_borrador(self.persona, proceso)
        radicar_ticket(ticket, self.persona)
        congelada = ticket.detalle_servicio.formulario_version_id
        crear_nueva_version(proceso.formulario, self.admin)  # borrador nuevo
        self._evaluar(proceso)
        self._publicar_pagina(proceso)
        ticket.refresh_from_db()
        self.assertEqual(ticket.detalle_servicio.formulario_version_id, congelada)
        self.assertEqual(ticket.estado, "RADICADO")


class CostoDeLaEvaluacionTests(_BaseCoherencia, TestCase):
    """Bloque 2.1 — una sola evaluación por petición; sin borradores no se simula (ni transacción ni escrituras) y
    la simulación, cuando hace falta, no toma el lock de la fila del Servicio. Los resultados no cambian."""

    def setUp(self):
        self._preparar()

    def test_sin_borradores_no_simula_y_aplica_la_misma_regla(self):
        from unittest import mock

        from apps.catalogo.operaciones import errores_para_publicar

        proceso = self._proceso("Sin borradores")
        with mock.patch("apps.catalogo.publicacion.simular", wraps=publicacion.simular) as simular:
            ev = self._evaluar(proceso)
        simular.assert_not_called()
        self.assertEqual(ev.bloqueos, errores_para_publicar(proceso))
        self.assertEqual(ev.vigentes, ev.bloqueos)
        self.assertEqual(ev.activaria, [])
        self.assertTrue(ev.publicable)

    def test_con_borradores_simula_una_vez_por_peticion_y_sin_lock(self):
        from unittest import mock

        proceso = self._proceso("Con borrador", flujo="borrador")
        for tab in ("general", "entrada", "salida", "ejecucion", "publicacion"):
            with self.subTest(tab=tab), mock.patch(
                "apps.catalogo.publicacion.simular", wraps=publicacion.simular
            ) as simular:
                respuesta = self.client.get(f"{reverse('catalogo:studio', args=[proceso.pk])}?tab={tab}")
                self.assertEqual(respuesta.status_code, 200)
                self.assertEqual(simular.call_count, 1)
                self.assertIs(simular.call_args.kwargs["bloquear"], False)

    def test_vigente_y_preparado_se_distinguen_con_un_borrador(self):
        proceso = self._proceso("Vigente y preparado", flujo="borrador")
        ev = self._evaluar(proceso)
        self.assertTrue(ev.publicable)  # preparado: el borrador se activaría sin errores
        self.assertTrue(ev.vigentes)  # vigente: sin configuración activa (se calcula bajo demanda)
        self.assertEqual(len(ev.activaria), 1)

    def test_publicar_de_verdad_sigue_tomando_el_lock(self):
        from unittest import mock

        proceso = self._proceso("Lock real")
        with mock.patch.object(Servicio.objects, "select_for_update", wraps=Servicio.objects.select_for_update) as lock:
            publicacion.publicar_cambios(proceso, self.admin)
        self.assertTrue(lock.called)
        proceso.refresh_from_db()
        self.assertTrue(proceso.activo)

    def test_los_grupos_traducidos_coinciden_con_los_de_siempre(self):
        from apps.catalogo.experiencia_proceso import errores_por_seccion
        from apps.catalogo.studio import _traducir_error_ejecucion

        for servicio in (self._proceso("Sin flujo", flujo=None), self._servicio_simple("Sin formulario")):
            with self.subTest(servicio=servicio.nombre):
                ev = self._evaluar(servicio)
                self.assertEqual(
                    ev.grupos_traducidos(_traducir_error_ejecucion),
                    errores_por_seccion(servicio, ev.bloqueos, _traducir_error_ejecucion),
                )
